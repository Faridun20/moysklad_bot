"""
Хэндлеры: учёт платежей от сотрудников — улучшенный визуал
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS
from services.roles import can_manage_payments
from utils.formatters import (
    format_payment_notify,
    format_payment_confirmed,
    format_payment_rejected,
    format_payments_report,
    DIV,
)
from services.database import (
    add_payment,
    confirm_payment,
    reject_payment,
    get_payment,
    get_payments_report,
    get_summary_by_employee,
    add_audit_log,
    get_role,
)

logger = logging.getLogger(__name__)
router = Router()

CURRENCIES = ["USD", "UZS", "RUB", "EUR"]


class PaymentState(StatesGroup):
    waiting_for_amount = State()
    waiting_for_currency = State()
    waiting_for_comment = State()


def is_admin(user_id: int) -> bool:
    return can_manage_payments(user_id)


# ─── Клавиатуры ──────────────────────────────────────────────────────────────


def currency_keyboard():
    kb = InlineKeyboardBuilder()
    for cur in CURRENCIES:
        kb.button(text=cur, callback_data=f"pay_cur:{cur}")
    kb.button(text="❌ Отмена", callback_data="pay_cancel")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def confirm_keyboard(payment_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"pay_ok:{payment_id}")
    kb.button(text="❌ Отклонить", callback_data=f"pay_no:{payment_id}")
    kb.adjust(2)
    return kb.as_markup()


def pay_report_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сегодня", callback_data="pr:today")
    kb.button(text="📅 Эта неделя", callback_data="pr:week")
    kb.button(text="📅 Этот месяц", callback_data="pr:month")
    kb.button(text="📅 Всё время", callback_data="pr:all")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


# ─── Запуск платежа ───────────────────────────────────────────────────────────


@router.message(Command("pay"))
async def cmd_pay(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(PaymentState.waiting_for_amount)
    await message.answer(
        f"{DIV}\n"
        f"💵 <b>Отправка платежа</b>\n\n"
        f"Введите сумму (только цифры):\n"
        f"<code>1500</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "pay_start")
async def cb_pay_start(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.clear()
    await state.set_state(PaymentState.waiting_for_amount)
    await call.message.answer(
        f"{DIV}\n"
        f"💵 <b>Отправка платежа</b>\n\n"
        f"Введите сумму (только цифры):\n"
        f"<code>1500</code>",
        parse_mode="HTML",
    )


@router.message(PaymentState.waiting_for_amount)
async def process_amount(message: Message, state: FSMContext):
    text = message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await message.answer(
            "❌ Введите корректную сумму, например: <code>1500</code>",
            parse_mode="HTML",
        )
    await state.update_data(amount=amount)
    await state.set_state(PaymentState.waiting_for_currency)
    await message.answer(
        f"✅ Сумма: <b>{amount:,.0f}</b>\n\nВыберите валюту:",
        parse_mode="HTML",
        reply_markup=currency_keyboard(),
    )


@router.callback_query(F.data.startswith("pay_cur:"), PaymentState.waiting_for_currency)
async def process_currency(call: CallbackQuery, state: FSMContext):
    currency = call.data.split(":")[1]
    await state.update_data(currency=currency)
    await state.set_state(PaymentState.waiting_for_comment)
    await call.message.edit_text(
        f"✅ Валюта: <b>{currency}</b>\n\n"
        f"📝 Напишите комментарий — за что переданы деньги?\n"
        f"<code>за май, оплата аренды</code>",
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "pay_cancel")
async def pay_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отправка платежа отменена.")
    await call.answer()


@router.message(PaymentState.waiting_for_comment)
async def process_comment(message: Message, state: FSMContext, bot: Bot):
    comment = message.text.strip()
    data = await state.get_data()
    await state.clear()

    amount = data["amount"]
    currency = data["currency"]
    user = message.from_user
    full_name = user.full_name or user.username or str(user.id)
    username = f"@{user.username}" if user.username else "—"

    payment_id = add_payment(
        user_id=user.id,
        username=username,
        full_name=full_name,
        amount=amount,
        currency=currency,
        comment=comment,
    )

    # Аудит лог
    add_audit_log(
        user.id,
        full_name,
        get_role(user.id),
        "payment_sent",
        f"Платёж #{payment_id}: {amount:,.0f} {currency} — {comment}",
    )

    await message.answer(
        f"{DIV}\n"
        f"✅ <b>Платёж отправлен!</b>\n\n"
        f"<b>💰 Сумма:</b> {amount:,.0f} {currency}\n"
        f"<b>📝 Комментарий:</b> {comment}\n\n"
        f"<i>⏳ Ожидайте подтверждения</i>",
        parse_mode="HTML",
    )

    notify = format_payment_notify(
        payment_id, full_name, username, amount, currency, comment
    )
    from services.notifier import get_notify_recipients
    recipients = get_notify_recipients()
    for uid in recipients:
        try:
            await bot.send_message(
                uid,
                notify,
                parse_mode="HTML",
                reply_markup=confirm_keyboard(payment_id),
            )
        except Exception as e:
            logger.warning("Не удалось уведомить %d: %s", uid, e)


# ─── Подтверждение / Отклонение ───────────────────────────────────────────────


@router.callback_query(F.data.startswith("pay_ok:"))
async def confirm_pay(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    payment_id = int(call.data.split(":")[1])
    payment = get_payment(payment_id)

    if not payment:
        return await call.answer("❌ Платёж не найден", show_alert=True)
    if payment["status"] != "pending":
        return await call.answer("⚠️ Уже обработан", show_alert=True)

    confirm_payment(payment_id)

    admin_name = call.from_user.full_name or str(call.from_user.id)
    add_audit_log(
        call.from_user.id,
        admin_name,
        get_role(call.from_user.id),
        "payment_confirmed",
        f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']}",
    )

    await call.answer("✅ Принято")
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text
        + f"\n\n{DIV}\n✅ <b>Принято</b>  <code>{now}</code>  — {admin_name}",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            payment["user_id"],
            format_payment_confirmed(
                payment["amount"], payment["currency"], payment["comment"]
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить сотрудника: %s", e)


@router.callback_query(F.data.startswith("pay_no:"))
async def reject_pay(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    payment_id = int(call.data.split(":")[1])
    payment = get_payment(payment_id)

    if not payment:
        return await call.answer("❌ Платёж не найден", show_alert=True)
    if payment["status"] != "pending":
        return await call.answer("⚠️ Уже обработан", show_alert=True)

    reject_payment(payment_id)

    admin_name = call.from_user.full_name or str(call.from_user.id)
    add_audit_log(
        call.from_user.id,
        admin_name,
        get_role(call.from_user.id),
        "payment_rejected",
        f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']}",
    )

    await call.answer("❌ Отклонено")
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text
        + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{now}</code>  — {admin_name}",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            payment["user_id"],
            format_payment_rejected(
                payment["amount"], payment["currency"], payment["comment"]
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить сотрудника: %s", e)


# ─── Отчёт ────────────────────────────────────────────────────────────────────


@router.message(Command("payreport"))
async def cmd_payreport(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer(
        "📊 За какой период показать отчёт?", reply_markup=pay_report_keyboard()
    )


@router.callback_query(F.data.startswith("pr:"))
async def cb_payreport(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]

    if period == "menu":
        return await call.message.answer(
            "📊 За какой период показать отчёт?", reply_markup=pay_report_keyboard()
        )

    now = datetime.now()
    if period == "today":
        since = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        until, label = None, "сегодня"
    elif period == "week":
        since = (now - timedelta(weeks=1)).strftime("%Y-%m-%d %H:%M:%S")
        until, label = None, "эта неделя"
    elif period == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        until, label = None, "этот месяц"
    else:
        since, until, label = None, None, "всё время"

    summary = get_summary_by_employee(since, until)
    payments = get_payments_report(since, until)

    if not payments:
        return await call.message.answer(
            f"{DIV}\n📊 <b>Платежи · {label}</b>\n\n<i>Нет платежей за период</i>",
            parse_mode="HTML",
        )

    messages = format_payments_report(summary, payments, label)
    for msg in messages:
        await call.message.answer(msg, parse_mode="HTML")

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Другой период", callback_data="pr:menu")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    await call.message.answer("Выберите действие:", reply_markup=kb.as_markup())
