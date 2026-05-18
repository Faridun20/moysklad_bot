"""
Создание «Заказа покупателя» (customerorder) в МойСклад.

Заменяет ms_demand для нового workflow: бот не создаёт сразу отгрузку
(demand) — потому что отгрузка списывает остатки и даёт доступ к
бэкенду по edit-ссылке. Вместо этого создаётся customerorder и
скачивается PDF печатной формы, который отправляется как файл в
Telegram — без ссылок на онлайн-кабинет МойСклад.

Бухгалтер потом сам решает: создать отгрузку (если товар реально
ушёл) в МойСклад из этого customerorder, или оставить как есть.

Контекст org/store/attributes наследуем из services.ms_demand —
там уже всё инициализируется при старте.
"""

import json
import logging
from datetime import datetime
from typing import Any

from services import ms_demand
from services.moysklad import MS_BASE, get_session, ms_get
from utils.helpers import extract_id_from_href

logger = logging.getLogger(__name__)


def _meta(href: str, entity_type: str) -> dict:
    return {
        "meta": {
            "href": href,
            "type": entity_type,
            "mediaType": "application/json",
        }
    }


# Кэшируем template один раз на процесс — справочник шаблонов редко меняется
_template_cache: dict | None = None


async def _resolve_print_template() -> dict | None:
    """Найти первый доступный print-шаблон для customerorder.

    Сначала смотрим встроенные (embedded), потом пользовательские (custom).
    МойСклад всегда возвращает хотя бы один embedded — это «Стандартная
    форма заказа покупателя».
    """
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    for endpoint in ("embeddedtemplates", "customtemplates"):
        try:
            data = await ms_get(f"entity/customerorder/metadata/{endpoint}")
            rows = data.get("rows", []) if isinstance(data, dict) else []
            if rows:
                _template_cache = rows[0]
                logger.info(
                    "customerorder template: %s (%s)",
                    rows[0].get("name"), endpoint,
                )
                return _template_cache
        except Exception:
            logger.exception("template resolve from %s failed", endpoint)
    logger.warning("Не найдено ни одного print-шаблона для customerorder")
    return None


async def _try_get_print_pdf(co_id: str) -> tuple[bytes | None, str | None]:
    """Получить PDF печатной формы заказа.

    Шаги:
      1) Создаём publication через POST /entity/customerorder/{id}/publication
         с meta print-шаблона. Это возвращает объект publication с
         downloadUrl или meta.href.
      2) Скачиваем PDF по тому URL (с API-токеном бота).

    Возвращает (bytes, filename). Если что-то пошло не так — (None, None);
    вызывающая сторона ничего не отправит в чат вместо PDF.
    """
    template = await _resolve_print_template()
    if not template:
        return None, None
    template_meta = template.get("meta")
    if not template_meta:
        return None, None

    sess = await get_session()
    # POST publication. Принципиальный момент: МойСклад отвечает 303 See Other
    # и в Location ставит URL с готовым файлом. Перехватываем редирект.
    try:
        async with sess.post(
            f"{MS_BASE}/entity/customerorder/{co_id}/export",
            json={"template": {"meta": template_meta}, "extension": "pdf"},
            allow_redirects=False,
        ) as resp:
            # 303 → Location указывает на готовый файл; сам файл часто
            # доступен после короткой задержки. 200 → файл inline в body.
            if resp.status == 200:
                ctype = resp.headers.get("Content-Type", "")
                if "pdf" in ctype.lower() or "octet-stream" in ctype.lower():
                    data = await resp.read()
                    return data, _filename_from_resp(resp, co_id)
                # JSON-ответ — значит publication не пришла напрямую
                logger.warning("export 200 but non-PDF content-type: %s", ctype)
                return None, None
            if resp.status in (302, 303):
                location = resp.headers.get("Location")
                if not location:
                    logger.warning("export redirect without Location")
                    return None, None
                # location может быть относительной — нормализуем
                if location.startswith("/"):
                    location = "https://api.moysklad.ru" + location
                # Скачиваем по location тем же sess (auth header сохранится)
                # Может быть задержка на rendering — ретраим до 3 раз
                import asyncio
                for attempt in range(3):
                    async with sess.get(location, allow_redirects=False) as dl:
                        if dl.status == 200:
                            return await dl.read(), _filename_from_resp(dl, co_id)
                        if dl.status in (202, 425):
                            # Rendering, попробуем ещё раз
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        logger.warning("download status %d", dl.status)
                        return None, None
                logger.warning("download still not ready after retries")
                return None, None
            body = await resp.text()
            logger.warning(
                "export failed %d: %s", resp.status, body[:200],
            )
            return None, None
    except Exception:
        logger.exception("export exception")
        return None, None


def _filename_from_resp(resp, co_id: str) -> str:
    """Достать filename из Content-Disposition либо сгенерировать дефолт."""
    cd = resp.headers.get("Content-Disposition", "")
    # filename="..." или filename*=UTF-8''...
    import re
    m = re.search(r'filename\*?=(?:UTF-8\'\')?\"?([^\";]+)\"?', cd)
    if m:
        try:
            from urllib.parse import unquote
            return unquote(m.group(1))
        except Exception:
            return m.group(1)
    return f"order_{co_id[:8]}.pdf"


async def create_customerorder_from_request(
    order: dict,
    items: list[dict],
    telegram_full_name: str,
    telegram_user_id: int | None = None,
) -> dict:
    """Создать customerorder в МойСклад на основе заявки бота.

    В отличие от ms_demand.create_demand_from_request:
      - не списывает остатки (это делает demand)
      - возвращает PDF печатной формы (pdf_bytes / pdf_filename)
        вместо ссылки на edit-страницу

    Возвращает:
        {"ok": True, "customerorder_id": "...", "name": "...",
         "pdf_bytes": <bytes|None>, "pdf_filename": "..."}
        {"ok": False, "reason": "..."}
    """
    if not ms_demand.is_ready():
        return {
            "ok": False,
            "reason": "Контекст МойСклад не инициализирован (org/store не найдены)",
        }
    if not order.get("agent_id"):
        return {"ok": False, "reason": "У заявки нет клиента (agent_id)"}

    positions = []
    skipped = []
    for it in items:
        href = it.get("product_href") or ""
        product_id = extract_id_from_href(href)
        if not product_id:
            skipped.append(it.get("product_name", "?"))
            continue
        price_minor = int(round(float(it.get("price", 0) or 0) * 100))
        positions.append({
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
        })

    if not positions:
        return {
            "ok": False,
            "reason": f"Не удалось разобрать позиции (skipped: {skipped})",
        }

    agent_href = f"{MS_BASE}/entity/counterparty/{order['agent_id']}"

    payload: dict[str, Any] = {
        "name": f"Заказ #{order['id']} (бот)",
        "organization": _meta(ms_demand._CTX["org_meta"]["href"], "organization"),
        "agent": _meta(agent_href, "counterparty"),
        "positions": positions,
        "moment": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.000"),
        "description": ms_demand._build_description(order, telegram_full_name),
        # applicable=True — заказ проведён (а не draft). Остатков НЕ
        # касается, в отличие от demand.
        "applicable": True,
    }
    # store у customerorder есть, но опциональный. Ставим если резолвили.
    store_meta = ms_demand._CTX.get("store_meta")
    if store_meta and store_meta.get("href"):
        payload["store"] = _meta(store_meta["href"], "store")["meta"] \
            if False else {"meta": _meta(store_meta["href"], "store")["meta"]}
        # Хм, упрощу:
        payload["store"] = {"meta": {
            "href": store_meta["href"],
            "type": "store",
            "mediaType": "application/json",
        }}

    # ВАЖНО: кастомные атрибуты (telegram_full_name / telegram_user_id)
    # из ms_demand._CTX зарегистрированы в МойСклад ТОЛЬКО для demand'а.
    # Каждая сущность в МойСклад имеет свой набор кастомных атрибутов;
    # если попробовать передать demand-атрибут на customerorder, API
    # отдаст HTTP 400 «href указывает на сущность неправильного типа».
    #
    # Поэтому attributes здесь не ставим. Для трекинга достаточно
    # description (включает имя менеджера и номер заявки бота),
    # а аналитика по boss-stats группирует по demand-атрибутам —
    # demand'у мы их корректно передаём в ms_demand.create_demand_from_request.

    try:
        sess = await get_session()
        async with sess.post(
            f"{MS_BASE}/entity/customerorder", json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "MS create customerorder HTTP %s: %s",
                    resp.status, body[:500],
                )
                return {
                    "ok": False,
                    "reason": f"HTTP {resp.status}: {body[:250]}",
                }
            created = json.loads(body)
            co_id = created.get("id", "")
    except Exception as e:
        logger.exception("create customerorder failed")
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    # Заказ создан — пробуем получить печатную форму
    pdf_bytes, pdf_filename = await _try_get_print_pdf(co_id)
    return {
        "ok": True,
        "customerorder_id": co_id,
        "name": created.get("name", ""),
        "pdf_bytes": pdf_bytes,
        "pdf_filename": pdf_filename,
        "skipped": skipped,
    }
