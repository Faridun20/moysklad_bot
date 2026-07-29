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

    ret = await db.get_return(return_id)  # native async после asyncpg Stage 10
    if not ret:
        return {"ok": False, "reason": "Возврат не найден"}
    if ret.get("moysklad_return_id"):
        return {"ok": True, "ms_id": ret["moysklad_return_id"], "skipped": "already-synced"}

    order = await db.get_order(ret["order_id"])  # native async (asyncpg Stage 19)
    if not order or not order.get("agent_id"):
        return {"ok": False, "reason": "Нет заказа/контрагента для возврата"}

    rows = await db.get_return_positions_for_ms(return_id)  # native async (Stage 10)
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
                "price": int(r.get("price_cents") or 0),
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
            won = await db.set_return_ms_id(return_id, ms_id)  # «выиграл ли гонку»
            if not won:
                # Гонку проиграли: другой параллельный create_salesreturn уже
                # записал id. Наш только что созданный документ в МС — orphan-дубль
                # (склад и баланс контрагента задвоились бы) → удаляем best-effort
                # (WP-14: SECURITY.md RACE-3 для этого и вернул bool).
                logger.warning(
                    "salesreturn возврата #%s: проиграли гонку set_return_ms_id — "
                    "удаляю orphan-документ %s в МС",
                    return_id, ms_id,
                )
                try:
                    async with sess.delete(f"{MS_BASE}/entity/salesreturn/{ms_id}") as dresp:
                        if dresp.status >= 400:
                            logger.error(
                                "salesreturn orphan-delete %s: HTTP %s", ms_id, dresp.status
                            )
                except Exception as de:  # noqa: BLE001 — удаление best-effort
                    logger.warning(
                        "salesreturn orphan-delete %s не удался: %s",
                        ms_id, redact_ms_error(str(de)[:200]),
                    )
                fresh = await db.get_return(return_id)
                winner_id = (fresh or {}).get("moysklad_return_id") or ms_id
                return {"ok": True, "ms_id": winner_id, "skipped": skipped, "adopted": True}
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
