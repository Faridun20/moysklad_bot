"""
Создание документов «Отгрузка» (entity/demand) в МойСклад из бота.

Поток:
1. Босс жмёт «✅ Одобрить» в Telegram → handlers/orders.cb_approve_request
2. Бот обновляет статус заявки в БД
3. Бот вызывает create_demand_from_request() — создаётся demand-документ
   в МойСклад с позициями, контрагентом и кастомным атрибутом
   `telegram_full_name` (кто оформил заявку через бота).

Зачем кастомный атрибут: МойСклад API позволяет создавать сотрудников
только на платных тарифах, поэтому привязка отгрузки к юзеру бота через
поле `owner` часто невозможна. Кастомный атрибут — работает на любом тарифе
и виден в карточке отгрузки + в фильтрах/отчётах МойСклад.

Зависимости конфига (env):
- MS_ORG_ID   — UUID организации (опционально; по умолчанию первая из API)
- MS_STORE_ID — UUID склада (опционально; по умолчанию первый из API)
- MS_TG_ATTRIBUTE_NAME — имя кастомного атрибута (по умолчанию telegram_full_name)
"""

import logging
import os
from typing import Any

from services.moysklad import ms_get, get_session, MS_BASE
from utils.helpers import extract_id_from_href, utc_now

logger = logging.getLogger(__name__)


# Кэш метаданных МойСклад, инициализируется один раз в init_demand_context()
_CTX: dict[str, Any] = {
    "ready": False,
    "org_meta": None,  # {"href": "...", "type": "organization", "mediaType": "application/json"}
    "store_meta": None,
    # Кастомные атрибуты на entity/demand. Имя — человекочитаемое,
    # ID — стабильный числовой, не теряется при смене имени в TG.
    "attribute_name_meta": None,
    "attribute_uid_meta": None,
    "attribute_name": "telegram_full_name",
    "attribute_uid": "telegram_user_id",
}


def _meta(href: str, entity_type: str) -> dict:
    return {
        "meta": {
            "href": href,
            "type": entity_type,
            "mediaType": "application/json",
        }
    }


async def _pick_first(path: str, env_var: str, entity_type: str) -> dict | None:
    """
    Вернуть meta для организации/склада. Если есть env_var с UUID —
    строим href вручную. Иначе берём первый элемент из API-списка.
    """
    forced_id = os.environ.get(env_var, "").strip()
    if forced_id:
        href = f"{MS_BASE}/entity/{path}/{forced_id}"
        return _meta(href, entity_type)["meta"]

    try:
        data = await ms_get(f"entity/{path}", params={"limit": 1})
        rows = data.get("rows", []) if isinstance(data, dict) else []
        if not rows:
            logger.error("Нет ни одного %s в МойСклад — demand создать не получится", entity_type)
            return None
        return rows[0].get("meta")
    except Exception as e:
        logger.exception("Не удалось получить %s: %s", entity_type, e)
        return None


async def _ensure_custom_attribute(name: str, attr_type: str = "string") -> dict | None:
    """
    Проверить, есть ли на entity/demand кастомный атрибут с именем `name`.
    Если нет — создать. Возвращает meta атрибута для использования в
    позиции `attributes` тела demand-документа.

    `attr_type`: "string" | "long" (числовой 64-битный) и т.д.
    """
    try:
        data = await ms_get("entity/demand/metadata/attributes")
        attrs = data.get("rows", []) if isinstance(data, dict) else []
        for a in attrs:
            if a.get("name") == name:
                logger.info("MS demand attribute '%s' уже существует", name)
                return a.get("meta")
    except Exception as e:
        logger.exception("Не удалось прочитать attribute metadata: %s", e)
        return None

    payload = {"name": name, "type": attr_type}
    try:
        sess = await get_session()
        async with sess.post(
            f"{MS_BASE}/entity/demand/metadata/attributes",
            json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                # Race-condition guard (SECURITY.md H5): два процесса в
                # rolling deploy могли одновременно прочитать metadata,
                # оба не нашли атрибут, оба POSTят. Второй получит
                # ошибку «такое имя уже есть» — это НЕ настоящий fail.
                # Перечитываем список и пытаемся достать только что
                # созданный первым процессом.
                logger.warning(
                    "Создание attribute '%s' дало HTTP %s — пробуем "
                    "перечитать (возможно race с другим процессом): %s",
                    name,
                    resp.status,
                    body[:200],
                )
                try:
                    data = await ms_get("entity/demand/metadata/attributes")
                    attrs = data.get("rows", []) if isinstance(data, dict) else []
                    for a in attrs:
                        if a.get("name") == name:
                            logger.info(
                                "Recovery успешен: attribute '%s' создан конкурентным процессом",
                                name,
                            )
                            return a.get("meta")
                except Exception:
                    logger.exception("Recovery после конкурентного POST упал")
                logger.error(
                    "Не удалось создать custom attribute '%s' (HTTP %s): %s",
                    name,
                    resp.status,
                    body[:300],
                )
                return None
            import json

            created = json.loads(body)
            logger.info("Создан MS demand attribute '%s' (%s)", name, attr_type)
            return created.get("meta")
    except Exception as e:
        logger.exception("create custom attribute failed: %s", e)
        return None


async def init_demand_context() -> dict:
    """
    Один раз при старте бота: подтянуть meta организации, склада и
    двух кастомных атрибутов (имя + telegram_user_id).
    """
    attr_name = os.environ.get("MS_TG_ATTRIBUTE_NAME", "").strip() or _CTX["attribute_name"]
    attr_uid = os.environ.get("MS_TG_UID_ATTRIBUTE_NAME", "").strip() or _CTX["attribute_uid"]
    _CTX["attribute_name"] = attr_name
    _CTX["attribute_uid"] = attr_uid

    org = await _pick_first("organization", "MS_ORG_ID", "organization")
    store = await _pick_first("store", "MS_STORE_ID", "store")
    attr_name_meta = await _ensure_custom_attribute(attr_name, "string")
    attr_uid_meta = await _ensure_custom_attribute(attr_uid, "long")

    _CTX["org_meta"] = org
    _CTX["store_meta"] = store
    _CTX["attribute_name_meta"] = attr_name_meta
    _CTX["attribute_uid_meta"] = attr_uid_meta
    _CTX["ready"] = bool(org and store)

    logger.info(
        "ms_demand context: org=%s, store=%s, attr_name=%s, attr_uid=%s",
        bool(org),
        bool(store),
        bool(attr_name_meta),
        bool(attr_uid_meta),
    )
    return {
        "ready": _CTX["ready"],
        "org": bool(org),
        "store": bool(store),
        "attribute_name": bool(attr_name_meta),
        "attribute_uid": bool(attr_uid_meta),
    }


def is_ready() -> bool:
    return _CTX["ready"]


async def create_demand_from_request(
    order: dict,
    items: list[dict],
    telegram_full_name: str,
    telegram_user_id: int | None = None,
    customerorder_href: str | None = None,
) -> dict:
    """
    Создать demand-документ в МойСклад на основе заявки бота.

    customerorder_href (optional) — если передан, demand линкуется с
    указанным заказом покупателя через customerOrder.meta. В МойСклад
    эти два документа будут связаны: бухгалтер увидит цепочку
    «Заказ → Отгрузка» в карточке заказа. Это нужно для нового
    workflow где бот auto-create'ит оба документа: customerorder
    для PDF + demand для списания остатков.

    Возвращает:
        {"ok": True, "demand_id": "...", "url": "https://online.moysklad..."}
        {"ok": False, "reason": "..."}
    """
    if not _CTX["ready"]:
        return {
            "ok": False,
            "reason": "Контекст МойСклад не инициализирован (org/store не найдены)",
        }

    if not order.get("agent_id"):
        return {
            "ok": False,
            "reason": "У заявки нет клиента (agent_id) — нельзя создать отгрузку",
        }

    # Позиции
    positions = []
    skipped = []
    for it in items:
        href = it.get("product_href") or ""
        product_id = extract_id_from_href(href)
        if not product_id:
            skipped.append(it.get("product_name", "?"))
            continue
        # МойСклад хранит цену в минорных единицах валюты (центы/копейки).
        price_major = float(it.get("price", 0) or 0)
        price_minor = int(round(price_major * 100))
        positions.append(
            {
                "quantity": float(it.get("quantity", 1)),
                "price": price_minor,
                "discount": 0,
                "vat": 0,
                "assortment": {
                    "meta": {
                        "href": href,
                        "type": "product",
                        "mediaType": "application/json",
                    }
                },
            }
        )

    if not positions:
        return {
            "ok": False,
            "reason": f"Не удалось разобрать ни одной позиции (skipped: {skipped})",
        }

    agent_href = f"{MS_BASE}/entity/counterparty/{order['agent_id']}"

    # МойСклад ждёт каждую ссылку как {"meta": {"href", "type", "mediaType"}}.
    # _CTX["org_meta"] и _CTX["store_meta"] хранят сам meta-dict (плоский),
    # поэтому здесь оборачиваем его в { "meta": ... }.
    payload: dict[str, Any] = {
        "name": f"Заявка #{order['id']} (бот)",
        "organization": _meta(_CTX["org_meta"]["href"], "organization"),
        "agent": _meta(agent_href, "counterparty"),
        "store": _meta(_CTX["store_meta"]["href"], "store"),
        "positions": positions,
        "moment": utc_now().strftime("%Y-%m-%d %H:%M:%S.000"),
        "description": _build_description(order, telegram_full_name),
        # applicable=true — отгрузка сразу проведена и остатки списываются
        # в МойСклад в момент апрува заявки боссом. Складскому не нужно
        # отдельно подтверждать документ.
        "applicable": True,
    }
    # Связь с заказом покупателя — МойСклад покажет цепочку «Заказ →
    # Отгрузка» в карточке заказа. Без этого documents висят отдельно
    # и бухгалтеру неочевидно что они про одно и то же.
    if customerorder_href:
        payload["customerOrder"] = _meta(customerorder_href, "customerorder")

    # Кастомные атрибуты — кто оформил заявку через бот.
    # Два атрибута: читаемое имя + стабильный user_id (на случай если
    # менеджер сменит имя в Telegram — id остаётся прежним).
    attrs: list[dict] = []
    for meta_obj, value in (
        (_CTX["attribute_name_meta"], telegram_full_name[:255] if telegram_full_name else ""),
        (_CTX["attribute_uid_meta"], telegram_user_id),
    ):
        if not (isinstance(meta_obj, dict) and meta_obj.get("href")):
            continue
        if value in (None, ""):
            continue
        attrs.append(
            {
                "meta": {
                    "href": meta_obj["href"],
                    "type": "attributemetadata",
                    "mediaType": "application/json",
                },
                "value": value,
            }
        )
    if attrs:
        payload["attributes"] = attrs

    try:
        sess = await get_session()
        async with sess.post(f"{MS_BASE}/entity/demand", json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                from services.moysklad import redact_ms_error

                safe = redact_ms_error(body)
                logger.error("MS create demand HTTP %s: %s", resp.status, safe)
                return {
                    "ok": False,
                    "reason": f"HTTP {resp.status}: {safe}",
                }
            import json

            created = json.loads(body)
            demand_id = created.get("id", "")
            return {
                "ok": True,
                "demand_id": demand_id,
                "name": created.get("name", ""),
                # Ссылка в веб-кабинете МойСклад на созданную отгрузку
                "url": f"https://online.moysklad.ru/app/#demand/edit?id={demand_id}",
                "skipped": skipped,
            }
    except Exception as e:
        logger.exception("create demand failed")
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def _build_description(order: dict, telegram_full_name: str) -> str:
    lines = [
        "Создано через Telegram-бота.",
        f"Менеджер: {telegram_full_name}",
        f"Внутренний номер заявки: #{order['id']}",
    ]
    if order.get("comment"):
        lines.append(f"Комментарий: {order['comment']}")
    return "\n".join(lines)
