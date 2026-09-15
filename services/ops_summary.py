"""
Сбор операционной сводки — ОБЩИЙ источник данных для:
  • WebApp endpoint `/api/ops-summary` (boss/admin смотрят сводку в WebApp);
  • дневного пинга бота (`tasks/run_ops_monitor`) — короткое уведомление
    «есть N событий, откройте WebApp».

Всё внутри — запросы к нашей БД. Тяжёлый dead-stock (обход всех отгрузок)
сюда сознательно НЕ входит — он не годится для on-demand вызова из webapp;
остаётся только в ночных задачах.

Каждая секция отдаётся как {"count": int, "items": [...]} (+ доп. поля порогов),
сырыми строками — экранирование делает потребитель (esc в Telegram, escapeHtml
во фронте).
"""

from services.database import (
    get_overdue_undeposited_orders,
    get_pending_cash_deposits,
    get_pending_returns,
    get_setting,
    get_stale_crons,
    get_stale_pending_orders,
)

# Cron-пороги — те же, что проверял ops_monitor, но БЕЗ report_daily/weekly/monthly:
# отчёты из бота убраны (смотрим в WebApp Аналитике), их cron'ов больше нет —
# иначе был бы ложный алерт «cron не запускался». По той же причине здесь нет
# ms_sync_retry: задачу убрали вместе с синхронизацией в МС, а порог остался и
# каждый день поднимал «ни разу не запускался».
# Список обязан совпадать с cron-сервисами в docker-compose.yml.
CRON_THRESHOLDS_HOURS: dict[str, float] = {
    "ops_monitor": 26.0,  # 1×/день → 26ч с запасом
    "maintenance": 26.0,
    "debts_notify": 26.0,
    "backup": 26.0,
    "machines_archive": 26.0,
    "fx_sync": 26.0,
    "money_report": 170.0,  # 1×/неделю → 7 суток + 2ч
    "boss_digest": 2.0,  # каждые 15 мин (тик может ничего не послать, но ЗАПУСК есть всегда) → 2ч с большим запасом
}

_ITEM_CAP = 15


def _cap(items: list[dict]) -> list[dict]:
    return items[:_ITEM_CAP]


async def gather_ops_summary() -> dict:
    """Собрать операционные блоки одним проходом (всё локально, без МС API).

    Возвращает dict секций; `total` — суммарное число «требующих внимания»
    позиций (для текста дневного пинга).
    """
    stale_hours = int(get_setting("stale_pending_hours", 48))
    cash_days = int(get_setting("cash_deposit_escalation_days", 2))
    low_stock_threshold = float(get_setting("low_stock_threshold", 5))

    stale = await get_stale_pending_orders(hours=stale_hours)
    deposits = await get_pending_cash_deposits()
    returns = await get_pending_returns()
    overdue = await get_overdue_undeposited_orders(days=cash_days)

    from services.order_shipment import list_failed
    from services.warehouse import get_low_stock

    low_stock = await get_low_stock(low_stock_threshold)
    stale_crons = await get_stale_crons(CRON_THRESHOLDS_HOURS)
    # Заказы, одобренные без списания со склада. Аналог прежнего блока
    # «рассинхрон с МойСклад»: там ловили документы, которые МС не принял,
    # здесь — накладные, которые не прошли (не хватило остатка, позиция без
    # карточки). Такой заказ выглядит отгруженным, а склад с ним не сошёлся.
    shipment_failed = await list_failed(limit=50)

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
        "low_stock": {
            "count": len(low_stock),
            "threshold": low_stock_threshold,
            "items": [
                {
                    "name": r.get("name") or "—",
                    "available": float(r.get("available", 0) or 0),
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
        "shipment_failed": {
            "count": len(shipment_failed),
            "items": [
                {
                    "order_id": r["order_id"],
                    "agent_name": r.get("agent_name") or "—",
                    "error": (r.get("error") or "")[:200],
                }
                for r in _cap(shipment_failed)
            ],
        },
    }

    total = (
        len(stale)
        + len(deposits)
        + len(returns)
        + len(overdue)
        + len(low_stock)
        + len(stale_crons)
        + len(shipment_failed)
    )
    sections["total"] = total
    return sections
