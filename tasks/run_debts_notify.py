"""
CLI: ежедневное напоминание о долгах. Запускается из Railway Cron.

Что делает:
  1. Берёт все открытые долги (credit + paid_at IS NULL) с due_date <= сегодня.
  2. Группирует:
     - Каждому МЕНЕДЖЕРУ — его собственные долги.
     - Каждому boss/admin — сводку по всей компании.
  3. Шлёт одно сообщение на получателя (а не одно на долг — чтобы не
     спамить чат).

Использование:
    python -m tasks.run_debts_notify

Расписание в Railway Cron: 0 6 * * *  (6:00 UTC = 11:00 Ташкент)
       или 0 9 * * * если предпочитаешь UTC.
"""

import logging
import sys
from datetime import date

from config import BASE_CURRENCY
from services.database import (
    init_db,
    get_open_debts,
    get_payments_for_orders,
    get_all_users,
)
from services.moysklad import close_session
from services.notifier import tg_send_message, close_tg_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("debts_notify")


from utils.helpers import esc as _esc  # единая реализация — utils/helpers.py


def _fmt_amount(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ")


def _fmt_cents(cents: int) -> str:
    """Копейки → строка для сообщения (деньги хранятся только в копейках)."""
    from services import money

    return money.format_cents(int(cents or 0), decimals=0, sep=" ")


def _to_ru(iso: str) -> str:
    if not iso or len(iso) < 10:
        return iso or ""
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


def _format_message(
    debts: list[dict], balances: dict, today_str: str, is_boss_view: bool
) -> str:
    """Свёрнутое сообщение про долги для одного получателя.

    T2.1: показываем ОСТАТОК (services.debts), а не полную сумму заказа.
    Раньше здесь складывались позиции и всё: ни подтверждённые платежи, ни
    сдачи, ни возвраты не вычитались — клиент внёс 80%, а в утреннем
    напоминании всё равно висело 100%, при том что WebApp показывал 20%.
    """
    overdue = [d for d in debts if d.get("due_date") and d["due_date"] < today_str]
    today = [d for d in debts if d.get("due_date") == today_str]

    def _row(d: dict) -> str:
        bal = balances.get(d["id"])
        remaining_cents = bal.remaining_cents if bal is not None else 0
        currency = (bal.currency if bal is not None else None) or d.get("currency") or BASE_CURRENCY
        agent = _esc(d.get("agent_name") or "—")
        owner_part = f" — {_esc(d.get('full_name') or '—')}" if is_boss_view else ""
        due_human = _to_ru(d.get("due_date") or "")
        return (
            f"  • #{d['id']} · {agent} · "
            f"<b>{_fmt_cents(remaining_cents)} {_esc(currency)}</b> "
            f"({due_human}{owner_part})"
        )

    parts = ["💳 <b>Напоминание о долгах</b>\n"]
    if overdue:
        parts.append(f"⚠️ <b>Просрочено ({len(overdue)}):</b>")
        parts.extend(_row(d) for d in overdue[:20])
        if len(overdue) > 20:
            parts.append(f"  …и ещё {len(overdue) - 20}")
    if today:
        if overdue:
            parts.append("")  # пустая строка между блоками
        parts.append(f"⏰ <b>Сегодня к оплате ({len(today)}):</b>")
        parts.extend(_row(d) for d in today[:20])
        if len(today) > 20:
            parts.append(f"  …и ещё {len(today) - 20}")
    parts.append(
        "\n<i>Откройте WebApp → «Долги», чтобы отметить оплаченные, "
        "или используйте команду /debts в чате.</i>"
    )
    return "\n".join(parts)


async def main() -> int:
    init_db()

    today_str = date.today().isoformat()

    # get_open_debts возвращает все credit+paid_confirmed_at IS NULL.
    # Делим по платежам:
    #   - менеджеру нужно действие, если по долгу НЕТ pending payments
    #     (он ещё ничего не отмечал или босс отклонил всё)
    #   - боссу — всё, ему важно видеть и долги с pending'ами для approve
    all_debts_full = await get_open_debts(due_through=today_str)
    if not all_debts_full:
        logger.info("Нет долгов к оплате сегодня — никому ничего не шлём.")
        return 0

    # Один батч на остатки и на платежи. Остаток — из services.debts, тем же
    # кодом, что /api/debts и карточка заказа (T2.1).
    from services.debts import calc_order_balances

    debt_ids = [d["id"] for d in all_debts_full]
    balances = await calc_order_balances(debt_ids)
    payments_by_order = await get_payments_for_orders(debt_ids)

    def _has_pending(order_id):
        return any(p["status"] == "pending" for p in payments_by_order.get(order_id, []))

    all_debts_for_managers = [d for d in all_debts_full if not _has_pending(d["id"])]
    all_debts_for_bosses = all_debts_full

    # Группируем по user_id (менеджеру-владельцу заказа)
    by_manager: dict[int, list[dict]] = {}
    for d in all_debts_for_managers:
        by_manager.setdefault(d["user_id"], []).append(d)

    users = get_all_users()
    # deactivated_at — как в notifier.get_notify_recipients: уволенный
    # руководитель не должен продолжать получать сводку долгов компании.
    bosses = [
        u
        for u in users
        if u["role"] in ("admin", "boss") and not u.get("deactivated_at")
    ]
    managers_by_id = {u["user_id"]: u for u in users if u["role"] == "manager"}

    sent = 0
    try:
        # 1. Менеджерам — их собственные долги
        for uid, debts in by_manager.items():
            if uid not in managers_by_id and not any(
                u["user_id"] == uid and u["role"] in ("admin", "boss") for u in users
            ):
                # У владельца заказа нет активного аккаунта — пропускаем
                # (босс всё равно получит сводку ниже).
                continue
            text = _format_message(debts, balances, today_str, is_boss_view=False)
            await tg_send_message(uid, text)
            sent += 1

        # 2. Boss/admin — сводка по всей компании (включая awaiting confirmation)
        boss_text = _format_message(
            all_debts_for_bosses, balances, today_str, is_boss_view=True
        )
        # Дополним подсказкой про подтверждения, если они есть
        awaiting_count = sum(1 for d in all_debts_for_bosses if _has_pending(d["id"]))
        if awaiting_count:
            boss_text += (
                f"\n\n⏳ <b>Требуют подтверждения: {awaiting_count}</b>"
                f"\nОткройте WebApp → «Финансы» → «Долги» или /debts в чате."
            )
        for boss in bosses:
            await tg_send_message(boss["user_id"], boss_text)
            sent += 1

        logger.info(
            "debts_notify: отправлено %d сообщений (для менеджеров: %d, для боссов: %d)",
            sent,
            len(all_debts_for_managers),
            len(all_debts_for_bosses),
        )
        return 0
    except Exception:
        logger.exception("debts_notify: ошибка")
        return 1
    finally:
        await close_session()
        await close_tg_session()


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("debts_notify", main))
