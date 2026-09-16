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
from services.notify_policy import (
    ORDER_REQUEST,
    PAYMENT,
    should_notify_now,
)
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
    """Уведомить руководителей о новой заявке на отгрузку.

    Заявка блокирует работу менеджера до решения — `notify_policy.
    ORDER_REQUEST` всегда «сразу» (не режется дайджестом), но проверяем через
    неё же, а не литералом True: один источник правды на все точки входа.
    """
    if not should_notify_now(ORDER_REQUEST):
        return
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


def approved_order_keyboard(payment_type: str | None) -> Any:
    """Кнопки под «Заявка одобрена» у менеджера — следующий шаг и его порядок.

    «Оплата сразу» не отгружается, пока не внесена разбивка оплаты
    (`database.mark_order_shipped` → `payment_required`). Поэтому шаги рисуются
    по порядку: живая кнопка «Внести оплату» в WebApp и под ней НЕАКТИВНАЯ
    (Bot API 10.3) «Отгрузка — после ввода оплаты» — видно, что отгрузка
    заблокирована и чем. «В долг» отгружается сразу — одна кнопка в WebApp.
    web_app-кнопку Telegram принимает только с https-URL; без него у «оплаты
    сразу» остаётся одна неактивная подсказка, у «в долг» — ничего.
    """
    import config
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    from utils.keyboards import disabled_button

    url = config.WEBAPP_URL or ""
    webapp = WebAppInfo(url=url) if url.startswith("https://") else None
    rows = []
    if (payment_type or "paid") == "paid":
        if webapp:
            rows.append([InlineKeyboardButton(text="💳 Внести оплату — в WebApp", web_app=webapp)])
        rows.append([disabled_button("🚚 Отгрузка — после ввода оплаты")])
    elif webapp:
        rows.append([InlineKeyboardButton(text="🚚 Отгрузить — в WebApp", web_app=webapp)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def notify_order_approved(
    bot: Bot,
    manager_user_id: int,
    req_id: int,
    boss_name: str,
    now: str,
    demand_line: str,
    *,
    payment_type: str | None = None,
    reply_markup: Any = None,
) -> None:
    """Уведомить менеджера об одобрении заявки.

    `payment_type="paid"` — последняя строка говорит «сначала оплата», а не
    «можно отгружать»: отгрузка «оплаты сразу» без разбивки получит отказ.
    """
    if (payment_type or "") == "paid":
        tail = "Сначала внесите оплату, потом отгружайте."
    else:
        tail = "Можно отгружать."
    text = (
        f"{DIV}\n"
        f"✅ <b>Заявка #{req_id} одобрена</b>\n\n"
        f"👨‍💼 Одобрил: {esc(boss_name)}\n"
        f"🕐 {now}{demand_line}\n\n"
        f"{tail}"
    )
    extra: dict[str, Any] = {"disable_web_page_preview": True}
    if reply_markup is not None:
        extra["reply_markup"] = reply_markup
    await _send(bot, manager_user_id, text, **extra)


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
        f"Спросите у руководителя, что не так, и отправьте заявку заново."
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
            f"\n\n🧊 <b>Заказ заморожен</b>: заявку отклоняли {rejection_count} раз(а), "
            f"отправить заново нельзя.\nПопросите администратора разморозить заказ."
        )
    else:
        tail = (
            f"\n\nПоправьте заказ и отправьте заявку заново "
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
    """Уведомить руководителей о новом платеже (ожидает подтверждения).

    Денежное событие — режется порогом `boss_instant_threshold_usd`
    (`notify_policy.should_notify_now`). Ниже порога карточку боссу НЕ шлём:
    платёж остаётся pending в БД и попадёт в вечерний дайджест
    (`services.boss_digest`) — само создание платежа это уже сделало, здесь
    только решаем, пушим ли отдельно СЕЙЧАС.
    """
    if not should_notify_now(PAYMENT, amount, currency):
        return
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
    amount = float(payment.get("amount") or 0)
    if not should_notify_now(PAYMENT, amount, currency):
        return
    agent = esc(order.get("agent_name") or "—")
    due = order.get("due_date") or "—"
    confirmed_before = max(0.0, summary["confirmed"])
    # summary["remaining"] = total - confirmed (без учёта pending).
    # «Останется после подтверждения ЭТОГО платежа» — отнимаем amount.
    remaining_after = max(0.0, summary["remaining"] - amount)

    lines = [
        f"{DIV}",
        "💳 <b>Подтвердите оплату</b>",
        "",
        f"Заказ #{order_id}",
        f"👨‍💼 Менеджер: <b>{esc(manager_name)}</b>",
        f"👤 Клиент: <b>{agent}</b>",
        f"💵 Сумма платежа: <b>{_fmt_amount(amount)} {esc(currency)}</b>",
        f"📦 Всего по заказу: <b>{_fmt_amount(summary['total'])} {esc(currency)}</b>",
    ]
    if summary.get("total_base") is not None and currency != summary.get("base_currency"):
        lines.append(
            f"   ≈ <b>{_fmt_amount(summary['total_base'])} {esc(summary['base_currency'])}</b>"
        )
    if confirmed_before > 0:
        lines.append(f"✅ Оплачено раньше: <b>{_fmt_amount(confirmed_before)} {esc(currency)}</b>")
    if remaining_after <= 0:
        lines.append("🎉 Этот платёж <b>закрывает долг полностью</b>")
    else:
        lines.append(
            f"📎 Останется долга: <b>{_fmt_amount(remaining_after)} {esc(currency)}</b>"
        )
    lines.append(f"📅 Срок оплаты: {esc(_to_ru(due))}")
    lines.append("")
    lines.append("Подтвердите, что эти деньги действительно пришли.")

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять платёж", callback_data=f"pay_ok:{payment_id}")
    kb.button(text="❌ Отклонить платёж", callback_data=f"pay_no:{payment_id}")
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
