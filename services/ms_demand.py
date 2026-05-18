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
from datetime import datetime
from typing import Any

from services.moysklad import ms_get, get_session, MS_BASE
from utils.helpers import extract_id_from_href

logger = logging.getLogger(__name__)


# Кэш метаданных МойСклад, инициализируется один раз в init_demand_context()
_CTX: dict[str, Any] = {
    "ready": False,
    "org_meta": None,        # {"href": "...", "type": "organization", "mediaType": "application/json"}
    "store_meta": None,
    "attribute_meta": None,  # meta кастомного атрибута для записи telegram_full_name
    "attribute_name": "telegram_full_name",
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
            logger.error("Нет ни одного %s в МойСклад — demand создать не получится",
                         entity_type)
            return None
        return rows[0].get("meta")
    except Exception as e:
        logger.exception("Не удалось получить %s: %s", entity_type, e)
        return None


async def _ensure_custom_attribute(name: str) -> dict | None:
    """
    Проверить, есть ли на entity/demand кастомный атрибут с именем `name`.
    Если нет — создать. Возвращает meta атрибута для использования в позиции
    `attributes` тела demand-документа.
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

    # Создаём
    payload = {"name": name, "type": "string"}
    try:
        sess = await get_session()
        async with sess.post(
            f"{MS_BASE}/entity/demand/metadata/attributes",
            json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "Не удалось создать custom attribute '%s' (HTTP %s): %s",
                    name, resp.status, body[:300],
                )
                return None
            import json
            created = json.loads(body)
            logger.info("Создан MS demand attribute '%s'", name)
            return created.get("meta")
    except Exception as e:
        logger.exception("create custom attribute failed: %s", e)
        return None


async def init_demand_context() -> dict:
    """
    Один раз при старте бота: подтянуть meta организации, склада и
    кастомного атрибута. Без них create_demand_from_request не работает.
    """
    attr_name = os.environ.get("MS_TG_ATTRIBUTE_NAME", "").strip() \
        or _CTX["attribute_name"]
    _CTX["attribute_name"] = attr_name

    org = await _pick_first("organization", "MS_ORG_ID", "organization")
    store = await _pick_first("store", "MS_STORE_ID", "store")
    attr = await _ensure_custom_attribute(attr_name)

    _CTX["org_meta"] = org
    _CTX["store_meta"] = store
    _CTX["attribute_meta"] = attr
    _CTX["ready"] = bool(org and store)  # attribute опционален, без него demand тоже создастся

    logger.info(
        "ms_demand context: org=%s, store=%s, attr=%s",
        bool(org), bool(store), bool(attr),
    )
    return {
        "ready": _CTX["ready"],
        "org": bool(org),
        "store": bool(store),
        "attribute": bool(attr),
        "attribute_name": attr_name,
    }


def is_ready() -> bool:
    return _CTX["ready"]


async def create_demand_from_request(
    order: dict,
    items: list[dict],
    telegram_full_name: str,
) -> dict:
    """
    Создать demand-документ в МойСклад на основе заявки бота.

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
        positions.append({
            "quantity": float(it.get("quantity", 1)),
            "price": 0,        # цена 0 — складской редактирует при необходимости
            "discount": 0,
            "vat": 0,
            "assortment": {
                "meta": {
                    "href": href,
                    "type": "product",
                    "mediaType": "application/json",
                }
            },
        })

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
        "moment": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.000"),
        "description": _build_description(order, telegram_full_name),
        # applicable=false — отгрузка создаётся черновиком, остатки не
        # списываются. Складской руками проводит документ когда реально
        # отгрузил товар.
        "applicable": False,
    }

    # Кастомный атрибут «кто оформил через бота»
    if _CTX["attribute_meta"]:
        attr_meta_dict = _CTX["attribute_meta"]
        if isinstance(attr_meta_dict, dict) and "href" in attr_meta_dict:
            payload["attributes"] = [{
                "meta": {
                    "href": attr_meta_dict["href"],
                    "type": "attributemetadata",
                    "mediaType": "application/json",
                },
                "value": telegram_full_name[:255],
            }]

    try:
        sess = await get_session()
        async with sess.post(f"{MS_BASE}/entity/demand", json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "MS create demand HTTP %s: %s", resp.status, body[:500]
                )
                return {
                    "ok": False,
                    "reason": f"HTTP {resp.status}: {body[:250]}",
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
        f"Создано через Telegram-бота.",
        f"Менеджер: {telegram_full_name}",
        f"Внутренний номер заявки: #{order['id']}",
    ]
    if order.get("comment"):
        lines.append(f"Комментарий: {order['comment']}")
    return "\n".join(lines)
