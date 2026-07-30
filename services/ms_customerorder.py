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
from typing import Any

from services import ms_demand
from services.metrics import measure_async
from services.moysklad import MS_BASE, get_session, ms_get
from utils.helpers import extract_id_from_href, utc_now

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
    # МойСклад использует SINGULAR в путях: /embeddedtemplate, /customtemplate
    # (а не /embeddedtemplates с 's'). Это критично — plural-форма даёт 404,
    # из-за чего предыдущая версия молча падала и PDF не приходил.
    for endpoint in ("embeddedtemplate", "customtemplate"):
        try:
            data = await ms_get(f"entity/customerorder/metadata/{endpoint}")
            rows = data.get("rows", []) if isinstance(data, dict) else []
            logger.info(
                "templates from %s: count=%d",
                endpoint,
                len(rows),
            )
            if rows:
                _template_cache = rows[0]
                logger.info(
                    "Выбран print-шаблон: %s (тип meta: %s)",
                    rows[0].get("name"),
                    (rows[0].get("meta") or {}).get("type"),
                )
                return _template_cache
        except Exception:
            logger.exception("template resolve from %s failed", endpoint)
    logger.warning(
        "Не найдено ни одного print-шаблона для customerorder. "
        "PDF не будет отправлен — проверь что в МойСклад → Настройки → "
        "Шаблоны печати есть хотя бы один шаблон для заказов покупателей."
    )
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
        logger.warning("template без поля meta: %s", template)
        return None, None

    # Чистим meta — оставляем только href/type/mediaType (МойСклад ругается
    # на лишние поля типа uuidHref). Тип нормализуем — должен быть
    # 'embeddedtemplate' или 'customtemplate', а не 'embeddedtemplates'.
    template_meta_clean = {
        "href": template_meta.get("href"),
        "type": (template_meta.get("type") or "embeddedtemplate").rstrip("s"),
        "mediaType": template_meta.get("mediaType", "application/json"),
    }
    if "/customtemplate" in (template_meta_clean["href"] or ""):
        template_meta_clean["type"] = "customtemplate"
    elif "/embeddedtemplate" in (template_meta_clean["href"] or ""):
        template_meta_clean["type"] = "embeddedtemplate"

    logger.info(
        "PDF request: co_id=%s, template href=%s, type=%s",
        co_id,
        template_meta_clean["href"],
        template_meta_clean["type"],
    )

    sess = await get_session()
    export_url = f"{MS_BASE}/entity/customerorder/{co_id}/export"
    body = {"template": {"meta": template_meta_clean}, "extension": "pdf"}

    try:
        async with sess.post(export_url, json=body, allow_redirects=False) as resp:
            logger.info(
                "export response: status=%d, ctype=%s, location=%s",
                resp.status,
                resp.headers.get("Content-Type", ""),
                resp.headers.get("Location", "")[:200],
            )
            if resp.status == 200:
                ctype = resp.headers.get("Content-Type", "")
                if "pdf" in ctype.lower() or "octet-stream" in ctype.lower():
                    data = await resp.read()
                    logger.info("PDF inline: %d bytes", len(data))
                    return data, _filename_from_resp(resp, co_id)
                body_text = await resp.text()
                logger.warning(
                    "export 200 but non-PDF ctype=%s; body[:400]=%s",
                    ctype,
                    body_text[:400],
                )
                return None, None
            if resp.status in (302, 303):
                location = resp.headers.get("Location")
                if not location:
                    logger.warning("export redirect без Location header")
                    return None, None
                if location.startswith("/"):
                    location = "https://api.moysklad.ru" + location
                logger.info("PDF redirect → %s", location)
                import asyncio

                for attempt in range(5):
                    async with sess.get(location, allow_redirects=False) as dl:
                        logger.info(
                            "PDF download attempt %d: status=%d",
                            attempt + 1,
                            dl.status,
                        )
                        if dl.status == 200:
                            data = await dl.read()
                            logger.info("PDF downloaded: %d bytes", len(data))
                            return data, _filename_from_resp(dl, co_id)
                        if dl.status in (202, 425):
                            # Rendering ещё в процессе — ретрай
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        if dl.status in (302, 303):
                            # Ещё один редирект — иногда МойСклад шлёт цепочку
                            location = dl.headers.get("Location") or location
                            if location.startswith("/"):
                                location = "https://api.moysklad.ru" + location
                            continue
                        err_body = await dl.text()
                        logger.warning(
                            "PDF download status=%d, body[:300]=%s",
                            dl.status,
                            err_body[:300],
                        )
                        return None, None
                logger.warning("PDF не готов после 5 попыток")
                return None, None
            err_body = await resp.text()
            logger.warning(
                "export failed: status=%d, body=%s",
                resp.status,
                err_body[:400],
            )
            return None, None
    except Exception:
        logger.exception("export exception for co=%s", co_id)
        return None, None


def _filename_from_resp(resp, co_id: str) -> str:
    """Достать filename из Content-Disposition либо сгенерировать дефолт."""
    cd = resp.headers.get("Content-Disposition", "")
    # filename="..." или filename*=UTF-8''...
    import re

    m = re.search(r"filename\*?=(?:UTF-8\'\')?\"?([^\";]+)\"?", cd)
    if m:
        try:
            from urllib.parse import unquote

            return unquote(m.group(1))
        except Exception:
            return m.group(1)
    return f"order_{co_id[:8]}.pdf"


@measure_async("ms.create_customerorder")
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
        # Цена уже в копейках (T1.3) — МС ждёт минорные единицы, конвертация
        # не нужна. Было int(round(float(...) * 100)) → дрейф на x.xx5.
        price_minor = int(it.get("price_cents") or 0)
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
            "reason": f"Не удалось разобрать позиции (skipped: {skipped})",
        }

    agent_href = f"{MS_BASE}/entity/counterparty/{order['agent_id']}"

    # Детерминированный syncId (T2.4): идемпотентный ключ документа от id
    # заказа. Даёт МС-стороннюю дедупликацию и позволяет подхватить уже
    # созданный customerorder, если процесс упал между прошлым POST и
    # локальной записью ms_customerorder_id — иначе повторный approve
    # создавал ВТОРОЙ документ и резервировал товар дважды (§2.1).
    from services.moysklad import find_by_sync_id, order_sync_id

    sync_id = order_sync_id("customerorder", order["id"])

    adopted = await find_by_sync_id("customerorder", sync_id)
    if adopted:
        logger.info(
            "MS customerorder уже существует (syncId), подхвачен: id=%s (заказ #%s)",
            adopted, order["id"],
        )
        pdf_bytes, pdf_filename = await _try_get_print_pdf(adopted)
        return {
            "ok": True,
            "customerorder_id": adopted,
            "name": f"Заказ #{order['id']} (бот)",
            "skipped": skipped,
            "adopted": True,
            "pdf_bytes": pdf_bytes,
            "pdf_filename": pdf_filename,
        }

    payload: dict[str, Any] = {
        "name": f"Заказ #{order['id']} (бот)",
        "syncId": sync_id,
        "organization": _meta(ms_demand._CTX["org_meta"]["href"], "organization"),
        "agent": _meta(agent_href, "counterparty"),
        "positions": positions,
        "moment": utc_now().strftime("%Y-%m-%d %H:%M:%S.000"),
        "description": ms_demand._build_description(order, telegram_full_name),
        # applicable=True — заказ проведён (а не draft). Остатков НЕ
        # касается, в отличие от demand.
        "applicable": True,
    }
    # store у customerorder есть, но опциональный. Ставим если резолвили.
    store_meta = ms_demand._CTX.get("store_meta")
    if store_meta and store_meta.get("href"):
        payload["store"] = {
            "meta": {
                "href": store_meta["href"],
                "type": "store",
                "mediaType": "application/json",
            }
        }

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
            f"{MS_BASE}/entity/customerorder",
            json=payload,
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                from services.moysklad import redact_ms_error

                safe = redact_ms_error(body)
                logger.error(
                    "MS create customerorder HTTP %s: %s",
                    resp.status,
                    safe,
                )
                return {
                    "ok": False,
                    "reason": f"HTTP {resp.status}: {safe}",
                }
            created = json.loads(body)
            co_id = created.get("id", "")
    except Exception as e:
        logger.exception("create customerorder failed")
        # Только имя класса — текст исключения (host/URL в aiohttp-ошибках)
        # остаётся в logger.exception, не утекает в user-facing reason.
        return {"ok": False, "reason": type(e).__name__}

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
