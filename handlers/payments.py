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
from utils.keyboards import next_actions_keyboard
from services import async_db as adb

from config import ALLOWED_CURRENCIES as CURRENCIES

logger = logging.getLogger(__name__)
router = Router()


class PaymentState(StatesGroup):
    waiting_for_input = State()
    waiting_for_currency = State()


def _parse_payment_input(text: str) -> tuple[float | None, str | None, str]:
    """Парсит «1500 USD за аренду» → (amount, currency|None, comment).

    amount=None, если первое слово не положительное число. Валюта — опциональна
    (распознаётся только как ровно одно из ALLOWED_CURRENCIES сразу после суммы),
    остальное — комментарий."""
    parts = (text or "").strip().split()
    if not parts:
        return None, None, ""
    try:
        amount = float(parts[0].replace(",", "."))
        if amount <= 0:
            return None, None, ""
    except ValueError:
        return None, None, ""
    rest = parts[1:]
    currency: str | None = None
    if rest and rest[0].upper() in CURRENCIES:
        currency = rest[0].upper()
        rest = rest[1:]
    return amount, currency, " ".join(rest).strip()


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


_PAY_PROMPT = (
    f"{DIV}\n"
    f"💵 <b>Отправка платежа</b>\n\n"
    f"Напишите одним сообщением: <b>сумма валюта комментарий</b>\n"
    f"<code>1500 USD за аренду</code>\n\n"
    f"<i>Если не укажете валюту — спрошу кнопками. Комментарий можно опустить.</i>"
)


def _pay_cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="pay_cancel")
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
    await state.set_state(PaymentState.waiting_for_input)
    await message.answer(_PAY_PROMPT, parse_mode="HTML", reply_markup=_pay_cancel_keyboard())


@router.callback_query(F.data == "pay_start")
async def cb_pay_start(call: CallbackQuery, state: FSMContext):
    if not _can_send_payment(call.from_user.id):
        return await call.answer("⛔ Платежи отправляют только менеджеры", show_alert=True)
    await call.answer()
    await state.clear()
    await state.set_state(PaymentState.waiting_for_input)
    await call.message.answer(_PAY_PROMPT, parse_mode="HTML", reply_markup=_pay_cancel_keyboard())


@router.message(PaymentState.waiting_for_input)
async def process_input(message: Message, state: FSMContext, bot: Bot):
    amount, currency, comment = _parse_payment_input(message.text)
    if amount is None:
        return await message.answer(
            "❌ Не понял сумму. Начните с числа, например:\n"
            "<code>1500 USD за аренду</code>",
            parse_mode="HTML",
        )
    if currency is None:
        await state.update_data(amount=amount, comment=comment)
        await state.set_state(PaymentState.waiting_for_currency)
        return await message.answer(
            f"✅ Сумма: <b>{amount:,.0f}</b>\n\nВыберите валюту:",
            parse_mode="HTML",
            reply_markup=currency_keyboard(),
        )
    await state.clear()
    await _finalize_payment(message, message.from_user, bot, amount, currency, comment)


@router.callback_query(F.data.startswith("pay_cur:"), PaymentState.waiting_for_currency)
async def process_currency(call: CallbackQuery, state: FSMContext, bot: Bot):
    currency = call.data.split(":")[1]
    data = await state.get_data()
    await state.clear()
    await call.answer()
    try:
        await call.message.edit_text(
            f"✅ Валюта: <b>{currency}</b>", parse_mode="HTML"
        )
    except Exception:
        pass
    await _finalize_payment(
        call.message,
        call.from_user,
        bot,
        data["amount"],
        currency,
        data.get("comment", ""),
    )


@router.callback_query(F.data == "pay_cancel")
async def pay_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отправка платежа отменена.")
    await call.answer()


async def _finalize_payment(target: Message, user, bot: Bot, amount, currency, comment):
    """Создаёт платёж, пишет аудит, шлёт подтверждение отправителю и нотифай боссу.
    target — сообщение, в чат которого отвечаем; user — отправитель платежа."""
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

    await adb.add_audit_log(
        user.id,
        full_name,
        await adb.get_role(user.id),
        "payment_sent",
        f"Платёж #{payment_id}: {amount:,.0f} {currency} — {comment}",
    )

    comment_line = f"<b>📝 Комментарий:</b> {_esc(comment)}\n" if comment else ""
    await target.answer(
        f"{DIV}\n"
        f"✅ <b>Платёж отправлен!</b>\n\n"
        f"<b>💰 Сумма:</b> {amount:,.0f} {currency}\n"
        f"{comment_line}\n"
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
    base = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        base + f"\n\n{DIV}\n✅ <b>Принято</b>  <code>{now}</code>  — {_esc(admin_name)}",
        parse_mode="HTML",
        reply_markup=next_actions_keyboard([("💳 Долги", "debts_my"), ("🏠 Меню", "menu")]),
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
    base = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        base + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{now}</code>  — {_esc(admin_name)}",
        parse_mode="HTML",
        reply_markup=next_actions_keyboard([("💳 Долги", "debts_my"), ("🏠 Меню", "menu")]),
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
