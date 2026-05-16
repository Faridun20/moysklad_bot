"""
Хэндлеры: учёт платежей от сотрудников
"""

import logging
import re
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_IDS
from utils.roles import can_manage_payments
from services.database import (
    add_payment, confirm_payment, reject_payment,
    get_payment, get_payments_report, get_summary_by_employee
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


def format_date(dt_str: str) -> str:
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except Exception:
        return dt_str


def currency_keyboard():
    kb = InlineKeyboardBuilder()
    for cur in CURRENCIES:
        kb.button(text=cur, callback_data=f"pay_cur:{cur}")
    kb.button(text="❌ Отмена", callback_data="pay_cancel")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def confirm_keyboard(payment_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять",   callback_data=f"pay_ok:{payment_id}")
    kb.button(text="❌ Отклонить", callback_data=f"pay_no:{payment_id}")
    kb.adjust(2)
    return kb.as_markup()


def pay_report_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сегодня",    callback_data="pr:today")
    kb.button(text="📅 Эта неделя", callback_data="pr:week")
    kb.button(text="📅 Этот месяц", callback_data="pr:month")
    kb.button(text="📅 Всё время",  callback_data="pr:all")
    kb.button(text="🏠 Меню",       callback_data="menu")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


# ─── Команды сотрудника ───────────────────────────────────────────────────────

@router.message(Command("pay"))
async def cmd_pay(message: Message, state: FSMContext):
    """Начать процесс отправки платежа."""
    await state.clear()
    await state.set_state(PaymentState.waiting_for_amount)
    await message.answer(
        "💵 <b>Отправка платежа</b>\n\n"
        "Введите сумму платежа (только цифры):\n"
        "Например: <code>1500</code>",
        parse_mode="HTML"
    )


@router.message(PaymentState.waiting_for_amount)
async def process_amount(message: Message, state: FSMContext):
    text = message.text.strip().replace(",", ".").replace(" ", "")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("❌ Введите корректную сумму, например: <code>1500</code>", parse_mode="HTML")

    await state.update_data(amount=amount)
    await state.set_state(PaymentState.waiting_for_currency)
    await message.answer(
        f"✅ Сумма: <b>{amount:,.0f}</b>\n\n"
        "Выберите валюту:",
        parse_mode="HTML",
        reply_markup=currency_keyboard()
    )


@router.callback_query(F.data.startswith("pay_cur:"), PaymentState.waiting_for_currency)
async def process_currency(call: CallbackQuery, state: FSMContext):
    currency = call.data.split(":")[1]
    await state.update_data(currency=currency)
    await state.set_state(PaymentState.waiting_for_comment)
    await call.message.edit_text(
        f"✅ Валюта: <b>{currency}</b>\n\n"
        "📝 Напишите комментарий — за что переданы деньги?\n"
        "Например: <code>за май, оплата аренды</code>",
        parse_mode="HTML"
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

    # Сохраняем в базу
    payment_id = add_payment(
        user_id=user.id,
        username=username,
        full_name=full_name,
        amount=amount,
        currency=currency,
        comment=comment,
    )

    # Подтверждение сотруднику
    await message.answer(
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"✅ <b>Платёж отправлен на подтверждение</b>\n\n"
        f"💰 Сумма: <b>{amount:,.0f} {currency}</b>\n"
        f"📝 Комментарий: {comment}\n\n"
        f"<i>Ожидайте подтверждения от руководителя</i>",
        parse_mode="HTML"
    )

    # Уведомление руководителям
    notify_text = (
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"💵 <b>Новый платёж #{payment_id}</b>\n\n"
        f"👤 Сотрудник: <b>{full_name}</b> ({username})\n"
        f"💰 Сумма: <b>{amount:,.0f} {currency}</b>\n"
        f"📝 Комментарий: {comment}\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                notify_text,
                parse_mode="HTML",
                reply_markup=confirm_keyboard(payment_id)
            )
        except Exception as e:
            logger.warning("Не удалось отправить уведомление админу %d: %s", admin_id, e)


# ─── Подтверждение/отклонение (только для админов) ───────────────────────────

@router.callback_query(F.data.startswith("pay_ok:"))
async def confirm_pay(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    payment_id = int(call.data.split(":")[1])
    payment = get_payment(payment_id)

    if not payment:
        return await call.answer("❌ Платёж не найден", show_alert=True)

    if payment["status"] != "pending":
        return await call.answer("⚠️ Платёж уже обработан", show_alert=True)

    confirm_payment(payment_id)
    await call.answer("✅ Платёж принят")

    # Обновляем сообщение у админа
    await call.message.edit_text(
        call.message.text + f"\n\n✅ <b>Принято</b> — {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode="HTML"
    )

    # Уведомляем сотрудника
    try:
        await bot.send_message(
            payment["user_id"],
            f"✅ <b>Ваш платёж принят!</b>\n\n"
            f"💰 {payment['amount']:,.0f} {payment['currency']}\n"
            f"📝 {payment['comment']}",
            parse_mode="HTML"
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
        return await call.answer("⚠️ Платёж уже обработан", show_alert=True)

    reject_payment(payment_id)
    await call.answer("❌ Платёж отклонён")

    await call.message.edit_text(
        call.message.text + f"\n\n❌ <b>Отклонено</b> — {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        parse_mode="HTML"
    )

    try:
        await bot.send_message(
            payment["user_id"],
            f"❌ <b>Ваш платёж отклонён</b>\n\n"
            f"💰 {payment['amount']:,.0f} {payment['currency']}\n"
            f"📝 {payment['comment']}\n\n"
            f"<i>Свяжитесь с руководителем для уточнения</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning("Не удалось уведомить сотрудника: %s", e)


# ─── Отчёт (только для админов) ───────────────────────────────────────────────

@router.message(Command("payreport"))
async def cmd_payreport(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer("📊 За какой период показать отчёт?", reply_markup=pay_report_keyboard())


@router.callback_query(F.data.startswith("pr:"))
async def cb_payreport(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]
    now = datetime.now()

    if period == "today":
        since = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        until = None
        label = "сегодня"
    elif period == "week":
        since = (now - timedelta(weeks=1)).strftime("%Y-%m-%d %H:%M:%S")
        until = None
        label = "эта неделя"
    elif period == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        until = None
        label = "этот месяц"
    else:
        since = None
        until = None
        label = "всё время"

    summary = get_summary_by_employee(since, until)
    payments = get_payments_report(since, until)

    if not payments:
        return await call.message.answer(f"📊 Нет платежей за {label}.")

    lines = []
    lines.append("<code>━━━━━━━━━━━━━━━━━━━━</code>")
    lines.append(f"📊 <b>Отчёт по платежам · {label}</b>")
    lines.append("")

    # Итог по сотрудникам
    lines.append("<b>По сотрудникам:</b>")
    for s in summary:
        lines.append(
            f"👤 <b>{s['full_name']}</b>\n"
            f"    {s['total']:,.0f} {s['currency']} · {s['count']} платежей"
        )

    # Общий итог
    lines.append("")
    total_by_currency: dict[str, float] = {}
    for p in payments:
        total_by_currency[p["currency"]] = total_by_currency.get(p["currency"], 0) + p["amount"]
    lines.append("<b>Итого:</b>")
    for cur, total in total_by_currency.items():
        lines.append(f"  💰 {total:,.0f} {cur}")

    # Список всех платежей
    lines.append("")
    lines.append(f"<b>Все платежи ({len(payments)}):</b>")
    for p in payments:
        lines.append(
            f"<code>━━━━━━━━━━━━</code>\n"
            f"👤 {p['full_name']}\n"
            f"💰 {p['amount']:,.0f} {p['currency']}\n"
            f"📝 {p['comment']}\n"
            f"🕐 {format_date(p['created_at'])}"
        )

    # Отправляем по частям если длинно
    text = "\n".join(lines)
    if len(text) > 4000:
        # Отправляем сводку отдельно
        summary_text = "\n".join(lines[:lines.index("") + 1 if "" in lines else len(lines)])
        await call.message.answer(summary_text, parse_mode="HTML")
        # Потом детали
        for p in payments:
            detail = (
                f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
                f"👤 {p['full_name']}\n"
                f"💰 {p['amount']:,.0f} {p['currency']}\n"
                f"📝 {p['comment']}\n"
                f"🕐 {format_date(p['created_at'])}"
            )
            await call.message.answer(detail, parse_mode="HTML")
    else:
        await call.message.answer(text, parse_mode="HTML")

    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Другой период", callback_data="pr:menu")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    await call.message.answer("Выберите действие:", reply_markup=kb.as_markup())


@router.callback_query(F.data == "pr:menu")
async def cb_pr_menu(call: CallbackQuery):
    await call.answer()
    await call.message.answer("📊 За какой период показать отчёт?", reply_markup=pay_report_keyboard())
