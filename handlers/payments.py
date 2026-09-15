"""
Хэндлеры: платежи сотрудников — отправка, подтверждение, статус МС-синка.

T3.3: отчёт /payreport вырезан — история платежей и сводки есть в WebApp
(«Финансы», /api/payments/history). Отправка платежа (/pay) осталась: это
три касания в чате против захода в WebApp, а подтверждение боссом (pay_ok/
pay_no) приходит push-карточкой.
"""

import logging

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from services.roles import _has_role, can_confirm_payment


def _can_send_payment(user_id: int) -> bool:
    """Кто может ОТПРАВИТЬ платёж на одобрение: manager (и admin для теста).
    Босс эти платежи апрувит — отправлять ему нечего."""
    return _has_role(user_id, "admin", "manager")


from handlers._ui import webapp_keyboard
from utils.formatters import DIV
from utils.helpers import esc as _esc, local_now  # единая реализация — utils/helpers.py
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
    """Кто решает по платежу под push-карточкой: руководитель или бухгалтер
    (менеджер — совмещением ролей, пока бухгалтера нет), как в WebApp."""
    return can_confirm_payment(user_id)


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
    if currency not in CURRENCIES:
        # callback_data подделывается клиентом — валюту берём только из списка.
        return await call.answer("Неизвестная валюта", show_alert=True)

    # Двойной тап по валюте: aiogram обрабатывает апдейты параллельно, и оба
    # колбэка проходили фильтр состояния раньше, чем первый успевал его
    # сбросить, — уходило ДВА платежа. Одна клавиатура = один платёж: ключ по
    # сообщению с кнопками столбится в общей БД (переживает и второй процесс).
    key = f"bot_pay_currency:{call.from_user.id}:{call.message.chat.id}:{call.message.message_id}"
    prev = await adb.idem_claim(key, "bot_pay_currency", call.from_user.id)
    if prev is not None:
        return await call.answer("Платёж уже отправлен" if prev else "Платёж уже отправляется…")

    data = await state.get_data()
    await state.clear()
    if "amount" not in data:
        await adb.idem_release(key)
        return await call.answer("Ввод устарел — начните заново: /pay", show_alert=True)
    await call.answer()
    try:
        await call.message.edit_text(
            f"✅ Валюта: <b>{currency}</b>", parse_mode="HTML"
        )
    except Exception:
        pass
    # Сбой внутри не освобождает ключ: платёж мог уже записаться, и второй тап
    # по той же клавиатуре не должен создать ещё один. Новый ввод — /pay.
    await _finalize_payment(
        call.message,
        call.from_user,
        bot,
        data["amount"],
        currency,
        data.get("comment", ""),
    )
    await adb.idem_store(key, {"ok": True})


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
    from services import order_payments

    if await order_payments.payment_method(payment_id) == "cash":
        # Наличные у менеджера подтверждаются сдачей в кассу — не этой кнопкой.
        return await call.answer(
            "Это наличные: они подтверждаются сдачей в кассу (WebApp → Деньги)", show_alert=True
        )

    admin_name = call.from_user.full_name or str(call.from_user.id)
    if not await adb.confirm_payment(payment_id, call.from_user.id, admin_name):
        return await call.answer("⚠️ Уже обработан", show_alert=True)

    await call.answer("✅ Принято")
    now = local_now().strftime("%d.%m.%Y %H:%M")
    base = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        base + f"\n\n{DIV}\n✅ <b>Принято</b>  <code>{now}</code>  — {_esc(admin_name)}",
        parse_mode="HTML",
        reply_markup=webapp_keyboard("🌐 Долги — в WebApp"),
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
    if not await adb.reject_payment(payment_id, call.from_user.id, admin_name):
        return await call.answer(
            "⚠️ Уже обработан или наличные уже в сдаче — отклоните сдачу", show_alert=True
        )

    await call.answer("❌ Отклонено")
    now = local_now().strftime("%d.%m.%Y %H:%M")
    base = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        base + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{now}</code>  — {_esc(admin_name)}",
        parse_mode="HTML",
        reply_markup=webapp_keyboard("🌐 Долги — в WebApp"),
    )

    from services.notify import notify_payment_rejected as _npayr

    await _npayr(bot, payment)
