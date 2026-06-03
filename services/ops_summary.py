"""
Сбор операционной сводки — ОБЩИЙ источник данных для:
  • WebApp endpoint `/api/ops-summary` (boss/admin смотрят сводку в WebApp);
  • дневного пинга бота (`tasks/run_ops_monitor`) — короткое уведомление
    «есть N событий, откройте WebApp».

Всё внутри — ЛОКАЛЬНЫЕ запросы к нашей БД (без обращений к МойСклад API).
Тяжёлый dead-stock (МС shipments+positions) сюда сознательно НЕ входит — он
не годится для on-demand вызова из webapp; остаётся только в ночных задачах.

Каждая секция отдаётся как {"count": int, "items": [...]} (+ доп. поля порогов),
сырыми строками — экранирование делает потребитель (esc в Telegram, escapeHtml
во фронте).
"""

import asyncio

from services.database import (
    get_batches_expiring_within,
    get_ms_sync_anomalies,
    get_overdue_undeposited_orders,
    get_pending_cash_deposits,
    get_pending_returns,
    get_setting,
    get_stale_crons,
    get_stale_pending_orders,
)
from utils.helpers import utc_now

# Cron-пороги — те же, что проверял ops_monitor, но БЕЗ report_daily/weekly/monthly:
# отчёты из бота убраны (смотрим в WebApp Аналитике), их cron'ов больше нет —
# иначе был бы ложный алерт «cron не запускался».
CRON_THRESHOLDS_HOURS: dict[str, float] = {
    "ms_sync_retry": 1.0,  # */15 минут → ≤1ч назад
    "ops_monitor": 26.0,  # 1×/день → 26ч с запасом
    "maintenance": 26.0,
    "debts_notify": 26.0,
    "backup": 26.0,
}

_ITEM_CAP = 15


def _cap(items: list[dict]) -> list[dict]:
    return items[:_ITEM_CAP]


async def gather_ops_summary() -> dict:
    """Собрать операционные блоки одним проходом (всё локально, без МС API).

    Возвращает dict секций; `total` — суммарное число «требующих внимания»
    позиций (для текста дневного пинга).
    """
    from datetime import timedelta

    stale_hours = int(get_setting("stale_pending_hours", 48))
    cash_days = int(get_setting("cash_deposit_escalation_days", 2))
    batch_days = 7
    low_stock_threshold = float(get_setting("low_stock_threshold", 5))

    stale = await get_stale_pending_orders(hours=stale_hours)
    deposits = await get_pending_cash_deposits()
    returns = await get_pending_returns()
    overdue = await get_overdue_undeposited_orders(days=cash_days)
    batches = await get_batches_expiring_within(days=batch_days)

    # snapshot.get_low_stock — sync (локальная БД). Не блокируем event loop.
    from services.snapshot import get_low_stock

    low_stock = await asyncio.to_thread(get_low_stock, low_stock_threshold)

    stale_crons = await get_stale_crons(CRON_THRESHOLDS_HOURS)

    ms_since = (utc_now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        ms = await get_ms_sync_anomalies(ms_since)
    except Exception:
        ms = {"drift": [], "deleted": [], "demand_failed": []}
    drift = ms.get("drift") or []
    deleted = ms.get("deleted") or []
    demand_failed = ms.get("demand_failed") or []

    sections: dict[str, object] = {
        "stale_orders": {
            "count": len(stale),
            "threshold_hours": stale_hours,
            "items": [
                {
                    "id": o["id"],
                    "agent_name": o.get("agent_name") or "—",
                    "full_name": o.get("full_name") or "—",
                }
                for o in _cap(stale)
            ],
        },
        "deposits": {
            "count": len(deposits),
            "total": sum(float(d.get("amount", 0) or 0) for d in deposits),
            "items": [
                {"id": d["id"], "amount": float(d.get("amount", 0) or 0)} for d in _cap(deposits)
            ],
        },
        "returns": {
            "count": len(returns),
            "items": [
                {
                    "id": r["id"],
                    "order_id": r.get("order_id"),
                    "total_amount": float(r.get("total_amount", 0) or 0),
                }
                for r in _cap(returns)
            ],
        },
        "overdue_undeposited": {
            "count": len(overdue),
            "threshold_days": cash_days,
            "items": [
                {
                    "id": o["id"],
                    "agent_name": o.get("agent_name") or "—",
                    "full_name": o.get("full_name") or "—",
                }
                for o in _cap(overdue)
            ],
        },
        "expiring_batches": {
            "count": len(batches),
            "threshold_days": batch_days,
            "items": [
                {
                    "code": b.get("batch_code") or b.get("product_id") or "—",
                    "expiry_date": b.get("expiry_date") or "—",
                    "qty_remaining": float(b.get("qty_remaining", 0) or 0),
                }
                for b in _cap(batches)
            ],
        },
        "low_stock": {
            "count": len(low_stock),
            "threshold": low_stock_threshold,
            "items": [
                {
                    "name": r.get("name") or "—",
                    "available": float(r.get("stock", 0) or 0) - float(r.get("reserve", 0) or 0),
                    "unit": r.get("unit") or "шт",
                }
                for r in _cap(low_stock)
            ],
        },
        "stale_crons": {
            "count": len(stale_crons),
            "items": [
                {
                    "task_name": c["task_name"],
                    "hours_ago": c.get("hours_ago"),
                    "last_status": c.get("last_status"),
                    "threshold_hours": c.get("threshold_hours", 0),
                    "never_ran": c.get("last_success_at") is None,
                }
                for c in stale_crons
            ],
        },
        "ms_anomalies": {
            "drift": len(drift),
            "deleted": len(deleted),
            "demand_failed": len(demand_failed),
            "items": {
                "drift": [
                    {"id": o["id"], "agent_name": o.get("agent_name") or "—"} for o in drift[:10]
                ],
                "deleted": [
                    {
                        "id": o["id"],
                        "agent_name": o.get("agent_name") or "—",
                        "status": o.get("status") or "",
                    }
                    for o in deleted[:10]
                ],
                "demand_failed": [
                    {"id": o["id"], "agent_name": o.get("agent_name") or "—"}
                    for o in demand_failed[:10]
                ],
            },
        },
    }

    total = (
        len(stale)
        + len(deposits)
        + len(returns)
        + len(overdue)
        + len(batches)
        + len(low_stock)
        + len(stale_crons)
        + len(drift)
        + len(deleted)
        + len(demand_failed)
    )
    sections["total"] = total
    return sections
