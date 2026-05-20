"""
Синхронизация входящих платежей с МойСклад.

Что и когда:
  - Только платежи привязанные к заказу (payments.order_id IS NOT NULL).
  - Только после approve босса (вызывается из confirm_payment hook).
  - Самостоятельные /pay-платежи (без order_id) НЕ синхронизируются —
    они обычно бытовые («аренда», «канцелярия») и бухгалтер вводит их
    в МойСклад руками.

API МойСклад:
  POST entity/paymentin
  https://dev.moysklad.ru/doc/api/remap/1.2/documents/#dokumenty-vhodyaschij-platezh

Payload:
  - organization → из ms_demand._CTX["org_meta"]
  - agent        → entity/counterparty/{order.agent_id}
  - sum          → amount * 100 (минорные единицы)
  - rate         → { currency: {meta:...} } — резолвим по isoCode
  - operations   → [meta of demand] — чтобы платёж привязался к отгрузке
                   (только если order.ms_demand_id заполнен)
  - applicable   → True (платёж сразу проведён)

Идемпотентность: если у payment уже есть ms_paymentin_id, не создаём
повторно. set_payment_ms_sync проставляется ДО возврата — повторный
confirm не наплодит дубликатов даже если хук вызовется второй раз.
"""

import json
import logging
from typing import Any

from services.database import (
    get_order,
    get_payment,
    set_payment_ms_sync,
    claim_payment_for_ms_sync,
)
from services.moysklad import MS_BASE, get_session, ms_get, redact_ms_error

logger = logging.getLogger(__name__)


# Кэш: isoCode → meta currency (curr.meta для rate.currency)
_currency_meta_cache: dict[str, dict] = {}


def _meta(href: str, entity_type: str) -> dict:
    return {
        "meta": {
            "href": href,
            "type": entity_type,
            "mediaType": "application/json",
        }
    }


async def _resolve_currency_meta(iso_code: str) -> dict | None:
    """Найти meta валюты МойСклад по ISO-коду (USD/UZS/RUB/EUR).
    Кэшируется на время процесса — валюты практически не меняются."""
    iso_code = (iso_code or "").upper()
    if not iso_code:
        return None
    if iso_code in _currency_meta_cache:
        return _currency_meta_cache[iso_code]
    try:
        data = await ms_get("entity/currency", params={"limit": 100})
        rows = data.get("rows", []) if isinstance(data, dict) else []
        for c in rows:
            if (c.get("isoCode") or "").upper() == iso_code:
                meta = c.get("meta")
                if meta:
                    _currency_meta_cache[iso_code] = meta
                    return meta
        logger.warning("MS currency %s не найдена в справочнике", iso_code)
    except Exception:
        logger.exception("Не удалось получить справочник валют МойСклад")
    return None


async def create_paymentin_for_payment(payment_id: int) -> dict:
    """Создать «Входящий платёж» в МойСклад на основе подтверждённого
    платежа из нашей БД.

    Возвращает:
        {"ok": True,  "paymentin_id": "...", "url": "..."}
        {"ok": False, "reason": "..."}
    """
    payment = get_payment(payment_id)
    if not payment:
        return {"ok": False, "reason": "Платёж не найден в БД"}
    if not payment.get("order_id"):
        return {"ok": False, "reason": "Платёж не связан с заказом (skip)"}
    if payment.get("ms_paymentin_id"):
        return {
            "ok": True,
            "paymentin_id": payment["ms_paymentin_id"],
            "url": _paymentin_url(payment["ms_paymentin_id"]),
            "already": True,
        }

    # Атомарно «застолбили» платёж — кто-то другой между чтением и
    # этой проверкой мог уже начать syncать. UPDATE-WHERE-not-in-progress
    # возвращает True только тому, кто выиграл гонку.
    if not claim_payment_for_ms_sync(payment_id):
        return {
            "ok": False,
            "reason": "concurrent sync already in progress",
            "concurrent": True,
        }

    order = get_order(payment["order_id"])
    if not order:
        msg = "Заказ-родитель не найден"
        set_payment_ms_sync(payment_id, status="failed", error=msg)
        return {"ok": False, "reason": msg}
    if not order.get("agent_id"):
        msg = "У заказа нет agent_id — нельзя создать paymentin"
        set_payment_ms_sync(payment_id, status="failed", error=msg)
        return {"ok": False, "reason": msg}

    # Контекст МойСклад: организация + meta валюты + (опционально) demand
    from services import ms_demand

    if not ms_demand.is_ready():
        msg = "Контекст ms_demand не готов (org не резолвлена)"
        set_payment_ms_sync(payment_id, status="failed", error=msg)
        return {"ok": False, "reason": msg}
    org_meta = ms_demand._CTX["org_meta"]

    currency = payment.get("currency") or "USD"
    currency_meta = await _resolve_currency_meta(currency)
    if not currency_meta:
        msg = f"Валюта {currency} не найдена в МойСклад"
        set_payment_ms_sync(payment_id, status="failed", error=msg)
        return {"ok": False, "reason": msg}

    agent_href = f"{MS_BASE}/entity/counterparty/{order['agent_id']}"
    amount_minor = int(round(float(payment["amount"]) * 100))

    payload: dict[str, Any] = {
        "name": f"Платёж #{payment_id} по заказу #{order['id']} (бот)",
        "organization": _meta(org_meta["href"], "organization"),
        "agent": _meta(agent_href, "counterparty"),
        "sum": amount_minor,
        "rate": {"currency": {"meta": currency_meta}},
        "applicable": True,
        "description": (
            f"{payment.get('comment') or ''}\nМенеджер: {payment.get('full_name') or '—'}"
        ).strip(),
    }

    # Привязка платежа к документу. Предпочитаем customerorder (новый
    # workflow), fallback на legacy demand. operations — массив,
    # формально можно прицепить несколько; у нас один.
    if order.get("ms_customerorder_id"):
        op_href = f"{MS_BASE}/entity/customerorder/{order['ms_customerorder_id']}"
        payload["operations"] = [_meta(op_href, "customerorder")]
    elif order.get("ms_demand_id"):
        op_href = f"{MS_BASE}/entity/demand/{order['ms_demand_id']}"
        payload["operations"] = [_meta(op_href, "demand")]

    try:
        sess = await get_session()
        async with sess.post(f"{MS_BASE}/entity/paymentin", json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                # redact_ms_error выкидывает PII из МС-body перед логом
                err = f"HTTP {resp.status}: {redact_ms_error(body)}"
                logger.error("MS paymentin failed: %s", err)
                set_payment_ms_sync(payment_id, status="failed", error=err)
                return {"ok": False, "reason": err}
            created = json.loads(body)
            paymentin_id = created.get("id", "")
            set_payment_ms_sync(
                payment_id,
                paymentin_id=paymentin_id,
                status="synced",
                error="",
            )
            logger.info(
                "MS paymentin создан: id=%s sum=%s %s (payment #%d, order #%d)",
                paymentin_id,
                payment["amount"],
                currency,
                payment_id,
                order["id"],
            )
            return {
                "ok": True,
                "paymentin_id": paymentin_id,
                "url": _paymentin_url(paymentin_id),
            }
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.exception("MS paymentin exception")
        set_payment_ms_sync(payment_id, status="failed", error=err)
        return {"ok": False, "reason": err}


def _paymentin_url(paymentin_id: str) -> str:
    return f"https://online.moysklad.ru/app/#paymentin/edit?id={paymentin_id}"
