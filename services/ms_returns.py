"""
Создание документа «Возврат покупателя» (entity/salesreturn) в МойСклад при
подтверждении возврата (IMPLEMENTATION.md §8, MS-интеграция).

Поток:
  возврат подтверждён в БД (confirm_return) → handler best-effort зовёт
  create_salesreturn(return_id). Документ линкуется с исходной отгрузкой
  (order.ms_demand_id), если она известна — бухгалтер видит цепочку
  «Отгрузка → Возврат».

Принципы (как у ms_demand/ms_payments):
  • контекст org/store берём из ms_demand._CTX (общий для аккаунта), gate
    через ms_demand.is_ready() — без него no-op (тесты/локалка без токена);
  • идемпотентность: если moysklad_return_id уже стоит — не дублируем;
  • best-effort: ошибка MS не ломает бизнес-флоу (возврат уже подтверждён
    локально), только лог через redact_ms_error;
  • НЕ ставим кастомные атрибуты (их meta привязана к demand — на salesreturn
    дало бы HTTP 400, см. CLAUDE.md про meta-reuse).
"""

import json
import logging

from services import database as db
from services import ms_demand
from services.metrics import measure_async
from services.moysklad import MS_BASE, get_session, redact_ms_error
from utils.helpers import extract_id_from_href, utc_now

logger = logging.getLogger(__name__)


def _meta(href: str, entity_type: str) -> dict:
    return {"meta": {"href": href, "type": entity_type, "mediaType": "application/json"}}


@measure_async("ms.create_salesreturn")
async def create_salesreturn(return_id: int) -> dict:
    """Создать «Возврат покупателя» в МойСклад. Возвращает
    {ok, ms_id?, url?} / {ok: False, reason}. Безопасно вызывать повторно."""
    ctx = ms_demand._CTX
    if not ms_demand.is_ready():
        return {"ok": False, "reason": "MS context не готов (org/store)"}

    ret = await _to_thread(db.get_return, return_id)
    if not ret:
        return {"ok": False, "reason": "Возврат не найден"}
    if ret.get("moysklad_return_id"):
        return {"ok": True, "ms_id": ret["moysklad_return_id"], "skipped": "already-synced"}

    order = await _to_thread(db.get_order, ret["order_id"])
    if not order or not order.get("agent_id"):
        return {"ok": False, "reason": "Нет заказа/контрагента для возврата"}

    rows = await _to_thread(db.get_return_positions_for_ms, return_id)
    positions = []
    skipped = []
    for r in rows:
        product_id = extract_id_from_href(r.get("product_href") or "")
        if not product_id:
            skipped.append(r.get("product_name", "?"))
            continue
        positions.append(
            {
                "quantity": float(r.get("qty", 0) or 0),
                "price": int(round(float(r.get("price", 0) or 0) * 100)),
                "vat": 0,
                "assortment": _meta(r["product_href"], "product"),
            }
        )
    if not positions:
        return {"ok": False, "reason": f"Нет разобранных позиций (skipped: {skipped})"}
    if skipped:
        # L1: возврат в МС уйдёт неполным относительно локального — заметно в логах.
        logger.warning(
            "salesreturn возврата #%s: пропущены позиции без product_href: %s",
            return_id,
            skipped,
        )

    payload = {
        "name": f"Возврат по заказу #{order['id']} (бот)",
        "organization": _meta(ctx["org_meta"]["href"], "organization"),
        "agent": _meta(f"{MS_BASE}/entity/counterparty/{order['agent_id']}", "counterparty"),
        "store": _meta(ctx["store_meta"]["href"], "store"),
        "positions": positions,
        "moment": utc_now().strftime("%Y-%m-%d %H:%M:%S.000"),
        "description": (ret.get("reason") or "")[:255],
        "applicable": True,
    }
    # Связь с исходной отгрузкой (если она создавалась ботом).
    demand_id = order.get("ms_demand_id")
    if demand_id:
        payload["demand"] = _meta(f"{MS_BASE}/entity/demand/{demand_id}", "demand")

    try:
        sess = await get_session()
        async with sess.post(f"{MS_BASE}/entity/salesreturn", json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                safe = redact_ms_error(body)
                logger.error("MS create salesreturn HTTP %s: %s", resp.status, safe)
                return {"ok": False, "reason": f"HTTP {resp.status}: {safe}"}
            created = json.loads(body)
            ms_id = created.get("id", "")
            await _to_thread(db.set_return_ms_id, return_id, ms_id)
            return {
                "ok": True,
                "ms_id": ms_id,
                "url": f"https://online.moysklad.ru/app/#salesreturn/edit?id={ms_id}",
                "skipped": skipped,
            }
    except Exception as e:
        logger.exception("create salesreturn failed")
        # Round 6 (S5): redact_ms_error на str(e) — exception repr
        # (особенно от aiohttp.ClientConnectorError) может включать URL/internals.
        # HTTP-error путь выше уже использует redact_ms_error; этот путь — нет;
        # делаем поведение единообразным на случай если caller начнёт
        # сурфейсить `reason` в API-ответ.
        return {"ok": False, "reason": f"{type(e).__name__}: {redact_ms_error(str(e)[:200])}"}


async def _to_thread(fn, *args):
    """Синхронные db-вызовы — в thread pool, чтобы не блокировать event loop
    (как services.async_db)."""
    import asyncio

    return await asyncio.to_thread(fn, *args)
