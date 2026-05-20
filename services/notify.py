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
