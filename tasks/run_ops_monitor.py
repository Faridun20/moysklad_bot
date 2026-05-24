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

import logging
import sys

from datetime import datetime

from services.database import (
    claim_ops_monitor_run,
    get_all_users,
    get_batches_expiring_within,
    get_overdue_undeposited_orders,
    get_pending_cash_deposits,
    get_pending_returns,
    get_setting,
    get_stale_crons,
    get_stale_pending_orders,
    init_db,
    run_migrations,
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


def build_cron_health_block(stale_crons: list[dict]) -> str | None:
    """Алерт о cron'ах, которые не отчитались success'ом дольше порога.

    Источник — services.database.get_stale_crons (порядок по task_name).
    Включаем в дайджест только если есть что-то — boss'у в текущий
    digest идут реальные проблемы (как зависшие заявки), не «всё ОК».
    """
    if not stale_crons:
        return None
    lines = [f"🛑 <b>Cron: не отчитались ({len(stale_crons)})</b>"]
    for c in stale_crons:
        task = _esc(c["task_name"])
        thr = c.get("threshold_hours", 0)
        if c.get("last_success_at") is None:
            lines.append(f"  • <code>{task}</code> · ни разу не запускался (порог {thr}ч)")
            continue
        ago = c.get("hours_ago") or 0
        status = c.get("last_status") or "?"
        err = _esc(str(c.get("last_error") or "")[:120])
        suffix = f" · err: {err}" if err else ""
        lines.append(
            f"  • <code>{task}</code> · {ago}ч назад · status={status} (порог {thr}ч){suffix}"
        )
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
    run_migrations()  # M1: на свежей БД догнать колонки (идемпотентно)

    # Round 6 RACE-4: idempotency-guard. Railway Cron при сетевом hiccup'е
    # может ретраить запуск, или ручной запуск пересечётся с плановым —
    # без этого дайджест разойдётся всем 2 раза в день.
    today = datetime.now().strftime("%Y-%m-%d")
    if not claim_ops_monitor_run(today):
        logger.info("ops_monitor: уже запускался сегодня (%s) — пропускаю", today)
        return 0

    stale_hours = int(get_setting("stale_pending_hours", 48))
    cash_days = int(get_setting("cash_deposit_escalation_days", 2))
    batch_days = 7

    stale = get_stale_pending_orders(hours=stale_hours)
    deposits = get_pending_cash_deposits()
    returns = get_pending_returns()
    overdue = get_overdue_undeposited_orders(days=cash_days)
    batches = get_batches_expiring_within(days=batch_days)
    # Cron health: проверяем что регулярные cron'ы реально отчитываются.
    # Пороги подобраны с большим запасом к реальному расписанию (`*/15` →
    # порог 1ч; ежедневный → 26ч). Если cron не запускался / упал —
    # boss увидит в дайджесте.
    cron_thresholds = {
        "ms_sync_retry": 1.0,  # */15 минут → должен бегать ≤1ч назад
        "ops_monitor": 26.0,  # 1×/день → 26ч с запасом
        "maintenance": 26.0,
        "debts_notify": 26.0,
        "report_daily": 26.0,
        "report_weekly": 7 * 24 + 2.0,
        "report_monthly": 31 * 24 + 6.0,
    }
    stale_crons = get_stale_crons(cron_thresholds)

    b_stale = build_stale_orders_block(stale, stale_hours)
    b_dep = build_pending_deposits_block(deposits)
    b_ret = build_pending_returns_block(returns)
    b_over = build_overdue_undeposited_block(overdue, cash_days)
    b_batch = build_expiring_batches_block(batches, batch_days)
    b_cron = build_cron_health_block(stale_crons)

    boss_digest = assemble_digest(
        "📋 <b>Операционная сводка</b>", [b_stale, b_dep, b_ret, b_over, b_batch, b_cron]
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
            "ops_monitor: stale=%d deposits=%d returns=%d overdue=%d batches=%d crons_stale=%d → %d сообщений",
            len(stale),
            len(deposits),
            len(returns),
            len(overdue),
            len(batches),
            len(stale_crons),
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
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("ops_monitor", main))
