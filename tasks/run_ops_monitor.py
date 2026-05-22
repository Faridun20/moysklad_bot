"""
CLI: операционный монитор (IMPLEMENTATION.md Фаза 6). Запускается из Railway Cron.

Один прогон собирает «висящие» сущности и шлёт КАЖДОМУ получателю ОДНУ сводку
(дайджест), а не по сообщению на каждую запись — чтобы не спамить чат:

  • boss/admin   — всё: зависшие заявки, неподтверждённые сдачи/возвраты,
                   просроченные несданные наличные, истекающие партии;
  • bookkeeper   — неподтверждённые сдачи;
  • warehouse    — неподтверждённые возвраты + истекающие партии.

Пороговые значения берём из app_settings (stale_pending_hours,
cash_deposit_escalation_days, и т.д.) с дефолтами.

Использование:
    python -m tasks.run_ops_monitor

Расписание Railway Cron (пример): 0 7 * * *  (7:00 UTC = 12:00 Ташкент).
"""

import asyncio
import logging
import sys

from services.database import (
    get_all_users,
    get_batches_expiring_within,
    get_overdue_undeposited_orders,
    get_pending_cash_deposits,
    get_pending_returns,
    get_setting,
    get_stale_pending_orders,
    init_db,
)
from services.moysklad import close_session
from services.notifier import close_tg_session, tg_send_message
from utils.helpers import esc as _esc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ops_monitor")

DIV = "─" * 16


def _fmt_amount(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


# ─── Чистые билдеры блоков (тестируются без сети/БД) ──────────────────────────


def build_stale_orders_block(orders: list[dict], hours: int) -> str | None:
    if not orders:
        return None
    lines = [f"⏳ <b>Зависшие заявки (>{hours}ч): {len(orders)}</b>"]
    for o in orders[:15]:
        agent = _esc(o.get("agent_name") or "—")
        owner = _esc(o.get("full_name") or "—")
        lines.append(f"  • #{o['id']} · {agent} · {owner}")
    if len(orders) > 15:
        lines.append(f"  …и ещё {len(orders) - 15}")
    return "\n".join(lines)


def build_pending_deposits_block(deposits: list[dict]) -> str | None:
    if not deposits:
        return None
    total = sum(float(d.get("amount", 0) or 0) for d in deposits)
    lines = [f"💵 <b>Сдачи на подтверждении: {len(deposits)}</b> (на {_fmt_amount(total)} USD)"]
    for d in deposits[:15]:
        lines.append(f"  • сдача #{d['id']} — {_fmt_amount(float(d.get('amount', 0) or 0))} USD")
    if len(deposits) > 15:
        lines.append(f"  …и ещё {len(deposits) - 15}")
    return "\n".join(lines)


def build_pending_returns_block(returns: list[dict]) -> str | None:
    if not returns:
        return None
    lines = [f"↩️ <b>Возвраты на подтверждении: {len(returns)}</b>"]
    for r in returns[:15]:
        amt = _fmt_amount(float(r.get("total_amount", 0) or 0))
        lines.append(f"  • возврат #{r['id']} · заказ #{r.get('order_id', '?')} — {amt} USD")
    if len(returns) > 15:
        lines.append(f"  …и ещё {len(returns) - 15}")
    return "\n".join(lines)


def build_overdue_undeposited_block(orders: list[dict], days: int) -> str | None:
    if not orders:
        return None
    lines = [f"🚨 <b>Отгружено, деньги не сданы (>{days}д): {len(orders)}</b>"]
    for o in orders[:15]:
        agent = _esc(o.get("agent_name") or "—")
        owner = _esc(o.get("full_name") or "—")
        lines.append(f"  • #{o['id']} · {agent} · {owner}")
    if len(orders) > 15:
        lines.append(f"  …и ещё {len(orders) - 15}")
    return "\n".join(lines)


def build_expiring_batches_block(batches: list[dict], days: int) -> str | None:
    if not batches:
        return None
    lines = [f"⌛ <b>Истекают партии (≤{days}д): {len(batches)}</b>"]
    for b in batches[:15]:
        code = _esc(b.get("batch_code") or b.get("product_id") or "—")
        exp = _esc(b.get("expiry_date") or "—")
        qty = _fmt_amount(float(b.get("qty_remaining", 0) or 0))
        lines.append(f"  • {code} · до {exp} · остаток {qty}")
    if len(batches) > 15:
        lines.append(f"  …и ещё {len(batches) - 15}")
    return "\n".join(lines)


def assemble_digest(title: str, blocks: list[str | None]) -> str | None:
    """Склеить непустые блоки в одну сводку. None — если всё пусто."""
    present = [b for b in blocks if b]
    if not present:
        return None
    return f"{DIV}\n{title}\n\n" + "\n\n".join(present)


# ─── Оркестрация ──────────────────────────────────────────────────────────────


async def main() -> int:
    init_db()

    stale_hours = int(get_setting("stale_pending_hours", 48))
    cash_days = int(get_setting("cash_deposit_escalation_days", 2))
    batch_days = 7

    stale = get_stale_pending_orders(hours=stale_hours)
    deposits = get_pending_cash_deposits()
    returns = get_pending_returns()
    overdue = get_overdue_undeposited_orders(days=cash_days)
    batches = get_batches_expiring_within(days=batch_days)

    b_stale = build_stale_orders_block(stale, stale_hours)
    b_dep = build_pending_deposits_block(deposits)
    b_ret = build_pending_returns_block(returns)
    b_over = build_overdue_undeposited_block(overdue, cash_days)
    b_batch = build_expiring_batches_block(batches, batch_days)

    boss_digest = assemble_digest(
        "📋 <b>Операционная сводка</b>", [b_stale, b_dep, b_ret, b_over, b_batch]
    )
    bookkeeper_digest = assemble_digest("📋 <b>Сводка: финансы</b>", [b_dep])
    warehouse_digest = assemble_digest("📋 <b>Сводка: склад</b>", [b_ret, b_batch])

    users = get_all_users()
    sent = 0
    try:
        for u in users:
            role = u["role"]
            digest = None
            if role in ("admin", "boss"):
                digest = boss_digest
            elif role == "bookkeeper":
                digest = bookkeeper_digest
            elif role == "warehouse_keeper":
                digest = warehouse_digest
            if digest:
                await tg_send_message(u["user_id"], digest)
                sent += 1
        logger.info(
            "ops_monitor: stale=%d deposits=%d returns=%d overdue=%d batches=%d → %d сообщений",
            len(stale),
            len(deposits),
            len(returns),
            len(overdue),
            len(batches),
            sent,
        )
        return 0
    except Exception:
        logger.exception("ops_monitor: ошибка")
        return 1
    finally:
        await close_session()
        await close_tg_session()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
