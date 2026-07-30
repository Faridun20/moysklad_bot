"""
Централизованные уведомления — вся логика send_message в одном месте.

Handlers вызывают функции этого модуля, не зная о форматировании
и не итерируя получателей вручную. Это упрощает поиск «кто шлёт
какие сообщения» и позволяет менять формат в одном месте.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot

from services.notifier import get_notify_recipients
from utils.formatters import (
    format_payment_notify,
    format_payment_confirmed,
    format_payment_rejected,
    DIV,
)
from utils.helpers import esc

logger = logging.getLogger(__name__)


async def _send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> None:
    """Отправить сообщение, не давая исключению пройти дальше."""
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
    except Exception as e:
        logger.warning("notify._send(%s): %s", chat_id, e)


async def _broadcast(bot: Bot, text: str, recipients: list[int], **kwargs: Any) -> None:
    """Разослать сообщение нескольким получателям."""
    for uid in recipients:
        await _send(bot, uid, text, **kwargs)


# ─── Заявки на отгрузку ───────────────────────────────────────────────────────


async def notify_shipment_request(
    bot: Bot,
    notify_text: str,
    req_id: int,
    *,
    approve_keyboard: Any,
) -> None:
    """Уведомить руководителей о новой заявке на отгрузку."""
    recipients = get_notify_recipients()
    for uid in recipients:
        try:
            await bot.send_message(
                uid,
                notify_text,
                parse_mode="HTML",
                reply_markup=approve_keyboard,
            )
        except Exception as e:
            logger.warning("Не удалось уведомить %d о заявке #%d: %s", uid, req_id, e)


async def notify_order_approved(
    bot: Bot,
    manager_user_id: int,
    req_id: int,
    boss_name: str,
    now: str,
    demand_line: str,
) -> None:
    """Уведомить менеджера об одобрении заявки."""
    text = (
        f"{DIV}\n"
        f"✅ <b>Заявка #{req_id} одобрена!</b>\n\n"
        f"👨‍💼 Одобрил: {esc(boss_name)}\n"
        f"🕐 {now}{demand_line}\n\n"
        f"Можно приступать к отгрузке."
    )
    await _send(bot, manager_user_id, text, disable_web_page_preview=True)


async def notify_order_rejected(
    bot: Bot,
    manager_user_id: int,
    req_id: int,
    boss_name: str,
    now: str,
) -> None:
    """Уведомить менеджера об отклонении заявки."""
    text = (
        f"{DIV}\n"
        f"❌ <b>Заявка #{req_id} отклонена</b>\n\n"
        f"👨‍💼 Отклонил: {esc(boss_name)}\n"
        f"🕐 {now}\n\n"
        f"Свяжитесь с руководителем для уточнения."
    )
    await _send(bot, manager_user_id, text)


async def notify_order_returned(
    bot: Bot,
    manager_user_id: int,
    req_id: int,
    boss_name: str,
    comment: str,
    now: str,
    frozen: bool,
    rejection_count: int,
) -> None:
    """Уведомить менеджера, что заявка возвращена на доработку (заказ → черновик).

    Если frozen — заказ заморожен после серии отклонений, переотправка заблокирована
    до разморозки администратором.
    """
    if frozen:
        tail = (
            f"\n\n🧊 <b>Заказ заморожен</b> после {rejection_count} отклонений — "
            f"переотправка заблокирована.\nОбратитесь к администратору для разморозки."
        )
    else:
        tail = (
            f"\n\nОтредактируйте заказ и отправьте заново "
            f"(попытка {rejection_count})."
        )
    text = (
        f"{DIV}\n"
        f"↩️ <b>Заявка #{req_id} возвращена на доработку</b>\n\n"
        f"👨‍💼 Вернул: {esc(boss_name)}\n"
        f"🕐 {now}\n"
        f"📝 Причина: {esc(comment)}{tail}"
    )
    await _send(bot, manager_user_id, text)


# ─── Платежи ──────────────────────────────────────────────────────────────────


async def notify_payment_sent(
    bot: Bot,
    payment_id: int,
    full_name: str,
    username: str,
    amount: float,
    currency: str,
    comment: str,
    confirm_keyboard: Any,
) -> None:
    """Уведомить руководителей о новом платеже (ожидает подтверждения)."""
    text = format_payment_notify(payment_id, full_name, username, amount, currency, comment)
    recipients = get_notify_recipients()
    for uid in recipients:
        try:
            await bot.send_message(
                uid,
                text,
                parse_mode="HTML",
                reply_markup=confirm_keyboard,
            )
        except Exception as e:
            logger.warning("Не удалось уведомить %d о платеже #%d: %s", uid, payment_id, e)


def _fmt_amount(n: float) -> str:
    """1234567 → '1 234 567'."""
    from services import money

    return money.format_cents(money.to_cents(n or 0), decimals=0, sep=" ")


def _to_ru(iso: str) -> str:
    """YYYY-MM-DD → ДД.ММ.ГГГГ."""
    if not iso or len(iso) < 10:
        return iso or ""
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


async def notify_payment_confirmation_needed(
    bot: Bot,
    order_id: int,
    manager_name: str,
    payment_id: int,
) -> None:
    """Запрос подтверждения оплаты по кредит-заказу — boss/admin.

    T3.3: переехало из `handlers/debts._push_payment_confirmation` вместе со
    срезом бот-экрана «Долги». Зовётся из `order_workflow` на пути mark_paid
    (WebApp), поэтому уведомление не может жить в хендлере, которого больше
    нет. Кнопки — те же pay_ok/pay_no, что у обычного платежа: подтверждение
    остаётся в боте, а сумму вводят в WebApp.
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from config import BASE_CURRENCY
    from services import async_db as adb

    order = await adb.get_order(order_id)
    if not order:
        return
    summary = await adb.get_order_payment_summary(order_id)
    payment = await adb.get_payment(payment_id)
    if not payment:
        return
    currency = order.get("currency") or BASE_CURRENCY
    agent = esc(order.get("agent_name") or "—")
    due = order.get("due_date") or "—"
    amount = float(payment.get("amount") or 0)
    confirmed_before = max(0.0, summary["confirmed"])
    # summary["remaining"] = total - confirmed (без учёта pending).
    # «Останется после подтверждения ЭТОГО платежа» — отнимаем amount.
    remaining_after = max(0.0, summary["remaining"] - amount)

    lines = [
        f"{DIV}",
        "💳 <b>Требуется подтверждение оплаты</b>",
        "",
        f"Заказ #{order_id}",
        f"👨‍💼 Менеджер: <b>{esc(manager_name)}</b>",
        f"🏢 Клиент: <b>{agent}</b>",
        f"💵 Сумма платежа: <b>{_fmt_amount(amount)} {esc(currency)}</b>",
        f"📦 По заказу всего: <b>{_fmt_amount(summary['total'])} {esc(currency)}</b>",
    ]
    if summary.get("total_base") is not None and currency != summary.get("base_currency"):
        lines.append(
            f"   ≈ <b>{_fmt_amount(summary['total_base'])} {esc(summary['base_currency'])}</b>"
        )
    if confirmed_before > 0:
        lines.append(f"✅ Уже оплачено ранее: <b>{_fmt_amount(confirmed_before)} {esc(currency)}</b>")
    if remaining_after <= 0:
        lines.append("🎉 Этот платёж <b>закрывает долг полностью</b>")
    else:
        lines.append(
            f"📎 Останется к получению: <b>{_fmt_amount(remaining_after)} {esc(currency)}</b>"
        )
    lines.append(f"📅 Срок: {esc(_to_ru(due))}")
    lines.append("")
    lines.append("Подтвердите, что эта сумма реально пришла в кассу.")

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"pay_ok:{payment_id}")
    kb.button(text="❌ Отклонить", callback_data=f"pay_no:{payment_id}")
    kb.adjust(2)
    await _broadcast(
        bot, "\n".join(lines), get_notify_recipients(), reply_markup=kb.as_markup()
    )


async def notify_payment_confirmed(
    bot: Bot,
    payment: dict,
) -> None:
    """Уведомить сотрудника о принятом платеже."""
    text = format_payment_confirmed(payment["amount"], payment["currency"], payment["comment"])
    await _send(bot, payment["user_id"], text)


async def notify_payment_rejected(
    bot: Bot,
    payment: dict,
) -> None:
    """Уведомить сотрудника об отклонённом платеже."""
    text = format_payment_rejected(payment["amount"], payment["currency"], payment["comment"])
    await _send(bot, payment["user_id"], text)
