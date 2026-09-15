"""
Прямое напоминание клиенту-должнику в Telegram (продуктовый аудит, B3).

`counterparties.telegram_id` уже существовал (привязка для доставки PDF
накладной), но для НАПОМИНАНИЙ не использовался — их получали только
менеджер и руководство (`tasks/run_debts_notify.py`). Решения:

* **Выключено по умолчанию** (`app_settings.client_debt_reminders_enabled`) —
  сообщение живому клиенту без явного решения владельца недопустимо; включает
  руководство переключателем в «Настройках» (`/api/settings/
  client_debt_reminders`).
* Уважает общий `app_settings.client_notifications_enabled` (уже был в
  дефолтах, ни к одному каналу подключён не был — теперь подключается).
* **Сообщение — ТОЛЬКО информационное**: сумма, номер заказа, срок. Без
  кнопок в WebApp — клиент не должен получить путь в интерфейс менеджера или
  руководителя, и без данных других клиентов (выборка уже отфильтрована по
  ЕГО заказам).
* **Рейт-лимит — раз в сутки на долг**, не «один раз за всё время»: долг
  остаётся открытым день за днём, и напоминание законно повторяется, пока
  его не погасили — но повторный прогон cron в тот же день не должен
  дублировать. Sidecar `client_debt_reminders` (order_id PK, last_sent_date) —
  хранит только ПОСЛЕДНЮЮ отметку, история — в audit_log.
* Best-effort по клиентам: сбой отправки одному не должен остановить
  рассылку остальным (как `tg_send_message` сам по себе).
"""

from __future__ import annotations

import asyncio
import logging

from services import adb_core
from services.database import USE_POSTGRES, add_audit_log, debt_due_date, get_setting, now_str
from services.notifier import tg_send_message
from utils.helpers import esc

logger = logging.getLogger(__name__)

_SYSTEM_ACTOR = (0, "Cron: напоминания должникам", "system")


def _to_ru(iso: str) -> str:
    if not iso or len(iso) < 10:
        return iso or ""
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


def build_reminder_text(order_id: int, amount_cents: int, currency: str, due_date: str) -> str:
    """Текст напоминания клиенту. Вежливо, по делу, без давления и без ссылок
    внутрь WebApp — клиенту нечего делать в интерфейсе менеджера."""
    from services import money

    amount = money.format_cents(int(amount_cents or 0), decimals=0, sep=" ")
    due_human = _to_ru(due_date)
    lines = [
        "Здравствуйте!",
        "",
        f"Напоминаем, что по заказу №{int(order_id)} остаётся задолженность:",
        f"<b>{amount} {esc(currency)}</b>",
    ]
    if due_human:
        lines.append(f"Срок оплаты: {esc(due_human)}.")
    lines += [
        "",
        "Пожалуйста, свяжитесь с нами, чтобы закрыть задолженность. Если оплата "
        "уже произведена — извините за беспокойство, просто проигнорируйте это "
        "сообщение.",
        "Спасибо, что работаете с нами!",
    ]
    return "\n".join(lines)


async def _reminders_enabled() -> bool:
    on = await asyncio.to_thread(get_setting, "client_debt_reminders_enabled", False)
    if not on:
        return False
    # Общий выключатель клиентских уведомлений — уважаем ЛЮБОЙ канал к клиенту.
    global_on = await asyncio.to_thread(get_setting, "client_notifications_enabled", True)
    return bool(global_on)


async def _already_sent_today(order_ids: list[int], today_str: str) -> set[int]:
    ids = sorted({int(i) for i in order_ids})
    if not ids:
        return set()
    placeholders = ",".join(f"${i + 1}" for i in range(len(ids)))
    rows = await adb_core.fetch(
        f"SELECT order_id FROM client_debt_reminders WHERE last_sent_date = ${len(ids) + 1} "
        f"AND order_id IN ({placeholders})",
        *ids,
        today_str,
    )
    return {int(r["order_id"]) for r in rows}


async def _mark_sent(order_id: int, today_str: str) -> None:
    now = now_str()
    if USE_POSTGRES:
        await adb_core.execute(
            "INSERT INTO client_debt_reminders (order_id, last_sent_date, last_sent_at) "
            "VALUES ($1, $2, $3) ON CONFLICT (order_id) DO UPDATE SET "
            "last_sent_date = EXCLUDED.last_sent_date, last_sent_at = EXCLUDED.last_sent_at",
            int(order_id),
            today_str,
            now,
        )
    else:
        await adb_core.execute(
            "INSERT INTO client_debt_reminders (order_id, last_sent_date, last_sent_at) "
            "VALUES ($1, $2, $3) ON CONFLICT(order_id) DO UPDATE SET "
            "last_sent_date = excluded.last_sent_date, last_sent_at = excluded.last_sent_at",
            int(order_id),
            today_str,
            now,
        )


async def send_overdue_client_reminders(
    overdue_debts: list[dict], balances: dict, today_str: str
) -> int:
    """Разослать напоминания клиентам-должникам с telegram_id.

    `overdue_debts` — подмножество `get_open_debts()` с due_date < сегодня
    (та же «просрочка», что в сводке для менеджера/руководства). `balances` —
    результат `services.debts.calc_order_balances` по тем же id.
    Возвращает число реально отправленных сообщений.
    """
    if not overdue_debts:
        return 0
    if not await _reminders_enabled():
        return 0

    order_ids = [int(d["id"]) for d in overdue_debts]
    already = await _already_sent_today(order_ids, today_str)
    pending = [d for d in overdue_debts if int(d["id"]) not in already]
    if not pending:
        return 0

    from services.counterparties import get_telegram_ids

    agent_ids: list[int] = []
    for d in pending:
        try:
            agent_ids.append(int(d.get("agent_id")))
        except (TypeError, ValueError):
            continue
    if not agent_ids:
        return 0
    tg_by_agent = await get_telegram_ids(agent_ids)
    if not tg_by_agent:
        return 0

    sent = 0
    for d in pending:
        try:
            agent_id = int(d.get("agent_id"))
        except (TypeError, ValueError):
            continue
        chat_id = tg_by_agent.get(agent_id)
        if not chat_id:
            continue
        order_id = int(d["id"])
        bal = balances.get(order_id)
        remaining_cents = bal.remaining_cents if bal is not None else 0
        if remaining_cents <= 0:
            continue  # погашено между выборкой и рассылкой — напоминать не о чем
        currency = (bal.currency if bal is not None else None) or d.get("currency") or "USD"
        due = debt_due_date(d) or ""
        text = build_reminder_text(order_id, remaining_cents, currency, due)
        try:
            ok = await tg_send_message(chat_id, text)
        except Exception:
            logger.exception("client_debt_reminder: сбой отправки заказ #%s", order_id)
            continue
        if not ok:
            logger.warning("client_debt_reminder: не доставлено, заказ #%s", order_id)
            continue
        await _mark_sent(order_id, today_str)
        uid, name, role = _SYSTEM_ACTOR
        await asyncio.to_thread(
            add_audit_log,
            uid,
            name,
            role,
            "client_debt_reminder_sent",
            f"заказ #{order_id} · контрагент #{agent_id} · chat {chat_id} · "
            f"{remaining_cents} коп. {currency}",
        )
        sent += 1
    return sent
