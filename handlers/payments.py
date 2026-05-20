"""
Хэндлеры: учёт платежей от сотрудников — улучшенный визуал
"""

import logging
from datetime import timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from services.roles import can_manage_payments, _has_role


def _can_send_payment(user_id: int) -> bool:
    """Кто может ОТПРАВИТЬ платёж на одобрение: manager (и admin для теста).
    Босс эти платежи апрувит — отправлять ему нечего."""
    return _has_role(user_id, "admin", "manager")


from utils.formatters import (
    format_payments_report,
    DIV,
)
from services import async_db as adb

from config import ALLOWED_CURRENCIES as CURRENCIES

logger = logging.getLogger(__name__)
router = Router()


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
    if not _can_send_payment(message.from_user.id):
        return await message.answer("⛔ Платежи отправляют только менеджеры.")
    await state.clear()
    await state.set_state(PaymentState.waiting_for_amount)
    await message.answer(
        f"{DIV}\n"
        f"💵 <b>Отправка платежа</b>  ·  <i>Шаг 1/3</i>\n\n"
        f"Введите сумму (только цифры):\n"
        f"<code>1500</code>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "pay_start")
async def cb_pay_start(call: CallbackQuery, state: FSMContext):
    if not _can_send_payment(call.from_user.id):
        return await call.answer("⛔ Платежи отправляют только менеджеры", show_alert=True)
    await call.answer()
    await state.clear()
    await state.set_state(PaymentState.waiting_for_amount)
    await call.message.answer(
        f"{DIV}\n"
        f"💵 <b>Отправка платежа</b>  ·  <i>Шаг 1/3</i>\n\n"
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
        f"✅ Сумма: <b>{amount:,.0f}</b>  ·  <i>Шаг 2/3</i>\n\nВыберите валюту:",
        parse_mode="HTML",
        reply_markup=currency_keyboard(),
    )


@router.callback_query(F.data.startswith("pay_cur:"), PaymentState.waiting_for_currency)
async def process_currency(call: CallbackQuery, state: FSMContext):
    currency = call.data.split(":")[1]
    await state.update_data(currency=currency)
    await state.set_state(PaymentState.waiting_for_comment)
    await call.message.edit_text(
        f"✅ Валюта: <b>{currency}</b>  ·  <i>Шаг 3/3</i>\n\n"
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

    payment_id = await adb.add_payment(
        user_id=user.id,
        username=username,
        full_name=full_name,
        amount=amount,
        currency=currency,
        comment=comment,
    )

    # Аудит лог
    await adb.add_audit_log(
        user.id,
        full_name,
        await adb.get_role(user.id),
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

    from services.notify import notify_payment_sent

    await notify_payment_sent(
        bot,
        payment_id,
        full_name,
        username,
        amount,
        currency,
        comment,
        confirm_keyboard=confirm_keyboard(payment_id),
    )


# ─── Подтверждение / Отклонение ───────────────────────────────────────────────


@router.callback_query(F.data.startswith("pay_ok:"))
async def confirm_pay(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    payment_id = int(call.data.split(":")[1])
    payment = await adb.get_payment(payment_id)

    if not payment:
        return await call.answer("❌ Платёж не найден", show_alert=True)
    if payment["status"] != "pending":
        return await call.answer("⚠️ Уже обработан", show_alert=True)

    admin_name = call.from_user.full_name or str(call.from_user.id)
    await adb.confirm_payment(payment_id, call.from_user.id, admin_name)

    await call.answer("✅ Принято")
    now = local_now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text + f"\n\n{DIV}\n✅ <b>Принято</b>  <code>{now}</code>  — {admin_name}",
        parse_mode="HTML",
    )

    from services.notify import notify_payment_confirmed as _npayc

    await _npayc(bot, payment)


@router.callback_query(F.data.startswith("pay_no:"))
async def reject_pay(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)

    payment_id = int(call.data.split(":")[1])
    payment = await adb.get_payment(payment_id)

    if not payment:
        return await call.answer("❌ Платёж не найден", show_alert=True)
    if payment["status"] != "pending":
        return await call.answer("⚠️ Уже обработан", show_alert=True)

    admin_name = call.from_user.full_name or str(call.from_user.id)
    await adb.reject_payment(payment_id, call.from_user.id, admin_name)

    await call.answer("❌ Отклонено")
    now = local_now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{now}</code>  — {admin_name}",
        parse_mode="HTML",
    )

    from services.notify import notify_payment_rejected as _npayr

    await _npayr(bot, payment)


# ─── Отчёт ────────────────────────────────────────────────────────────────────


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

    if period == "menu":
        return await call.message.answer(
            "📊 За какой период показать отчёт?", reply_markup=pay_report_keyboard()
        )

    now = local_now()
    if period == "today":
        since = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        until, label = None, "сегодня"
    elif period == "week":
        since = (now - timedelta(weeks=1)).strftime("%Y-%m-%d %H:%M:%S")
        until, label = None, "эта неделя"
    elif period == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        until, label = None, "этот месяц"
    else:
        since, until, label = None, None, "всё время"

    summary = await adb.get_summary_by_employee(since, until)
    payments = await adb.get_payments_report(since, until)

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


# ─── /sync_payments: показать состояние и ретрайнуть синки в МойСклад ───────


from utils.helpers import esc as _esc, local_now  # единая реализация — utils/helpers.py


async def _format_sync_status() -> tuple[str, bool]:
    """HTML-сообщение про статус синхронизации + флаг «есть что ретрайнуть»."""
    stats = await adb.get_ms_sync_stats()
    pending_count = stats["failed"] + stats["never_tried"]

    lines = [
        f"{DIV}",
        "🔄 <b>Синхронизация платежей в МойСклад</b>",
        "",
        f"✅ Синхронизированы:    <b>{stats['synced']}</b>",
        f"❌ Failed:               <b>{stats['failed']}</b>",
        f"⏳ Ещё не пробовали:     <b>{stats['never_tried']}</b>",
    ]
    if stats["failed"] > 0:
        fails = await adb.get_recent_ms_sync_failures(limit=5)
        if fails:
            lines.append("\n<b>Последние ошибки:</b>")
            for f in fails:
                err = (f.get("ms_sync_error") or "")[:120]
                cur_ = f.get("currency") or "—"
                amount = f.get("amount") or 0
                oid = f.get("order_id") or "—"
                lines.append(
                    f"  • Платёж #{f['id']} (#{oid}, {int(round(float(amount)))} {_esc(cur_)}): "
                    f"<code>{_esc(err)}</code>"
                )
    if pending_count == 0:
        lines.append("\n<i>Всё синхронизировано, ретрайнуть нечего.</i>")
    return ("\n".join(lines), pending_count > 0)


@router.message(Command("sync_payments"))
async def cmd_sync_payments(message: Message):
    """Только admin/boss. Показывает статус и кнопку Retry."""
    if not can_manage_payments(message.from_user.id):
        return

    text, has_pending = await _format_sync_status()
    kb = InlineKeyboardBuilder()
    if has_pending:
        kb.button(text="🔄 Retry all", callback_data="ms_sync_retry")
    kb.button(text="🔄 Обновить", callback_data="ms_sync_refresh")
    kb.adjust(1)
    await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "ms_sync_refresh")
async def cb_sync_refresh(call: CallbackQuery):
    if not can_manage_payments(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    text, has_pending = await _format_sync_status()
    kb = InlineKeyboardBuilder()
    if has_pending:
        kb.button(text="🔄 Retry all", callback_data="ms_sync_retry")
    kb.button(text="🔄 Обновить", callback_data="ms_sync_refresh")
    kb.adjust(1)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        pass
    await call.answer("Обновлено")


@router.callback_query(F.data == "ms_sync_retry")
async def cb_sync_retry(call: CallbackQuery):
    """Прогнать все failed/never_tried прямо сейчас."""
    if not can_manage_payments(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    await call.answer("⏳ Начал синхронизацию…")
    # Получаем кандидатов и ретраим по очереди
    pending = await adb.get_payments_needing_ms_sync(limit=100)
    if not pending:
        await call.message.answer("Нечего синхронизировать.")
        return

    from services.ms_payments import create_paymentin_for_payment

    ok_count = 0
    fail_count = 0
    for p in pending:
        result = await create_paymentin_for_payment(p["id"])
        if result.get("ok"):
            ok_count += 1
        else:
            fail_count += 1

    # Перечитываем актуальные цифры и обновляем сообщение
    text, has_pending = await _format_sync_status()
    summary = f"\n\n<b>Итог retry:</b> ✅ {ok_count} · ❌ {fail_count}"
    kb = InlineKeyboardBuilder()
    if has_pending:
        kb.button(text="🔄 Retry all", callback_data="ms_sync_retry")
    kb.button(text="🔄 Обновить", callback_data="ms_sync_refresh")
    kb.adjust(1)
    try:
        await call.message.edit_text(
            text + summary,
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass
