"""
Хэндлеры: решения по сдаче наличных в кассу (IMPLEMENTATION.md §7).

T3.3: создание сдачи (/deposit), свои сдачи (/my_deposits) и очередь
(/deposits) вырезаны — это WebApp («Финансы → Касса»). В боте остались кнопки
«Подтвердить»/«Отклонить» под push-карточкой: бухгалтер решает прямо в
уведомлении, отклонение спрашивает причину.

`_notify_confirmers` зовёт и WebApp (webapp/server.py) при создании сдачи.

Логика — в services.database (create/confirm/reject_cash_deposit); тут только
Telegram-UI и уведомления.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services import async_db as adb
from services.roles import can_confirm_deposit
from handlers._ui import drop_keyboard, finish_message, webapp_keyboard
from utils.helpers import esc
from utils.formatters import DIV

logger = logging.getLogger(__name__)
router = Router()


class DepositReject(StatesGroup):
    waiting_for_reason = State()


def _confirm_keyboard(deposit_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"dep_ok:{deposit_id}")
    kb.button(text="❌ Отклонить", callback_data=f"dep_no:{deposit_id}")
    kb.adjust(2)
    return kb.as_markup()


def _fmt_amount(x: float) -> str:
    from services import money

    return money.format_cents(money.to_cents(x or 0), decimals=2, sep=" ")


def _base_cur() -> str:
    """Валюта кассы. T2.13 (§3.7): касса ведётся в BASE_CURRENCY, а подпись
    была захардкожена как «USD» — при смене базовой валюты цифры оказывались
    подписаны неверно."""
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


def _fmt_cents(cents: int) -> str:
    """Копейки → строка. Деньги хранятся только в копейках (T1.3), поэтому
    экраны сдач читают *_cents, а не мажорный float."""
    from services import money

    return money.format_cents(int(cents or 0), decimals=2, sep=" ")


async def _notify_confirmers(bot: Bot, deposit_id: int, manager_name: str, amount: float):
    """Отправить боссам/бухгалтерам карточку сдачи с кнопками."""
    allocations = await adb.get_cash_deposit_orders(deposit_id)
    orders_line = (
        "\n".join(
            f"  • заказ #{a['order_id']} — {_fmt_cents(a['amount_allocated_cents'])} {_base_cur()}"
            for a in allocations
        )
        or "  <i>нет открытых заказов для привязки</i>"
    )
    text = (
        f"{DIV}\n"
        f"💵 <b>Сдача наличных #{deposit_id}</b>\n\n"
        f"👨‍💼 Менеджер: <b>{esc(manager_name)}</b>\n"
        f"💰 Сумма: <b>{_fmt_amount(amount)} {_base_cur()}</b>\n"
        f"📦 Закрывает заказы:\n{orders_line}"
    )
    recipients = await adb.get_deposit_confirmers()
    for uid in recipients:
        try:
            await bot.send_message(
                uid, text, parse_mode="HTML", reply_markup=_confirm_keyboard(deposit_id)
            )
        except Exception as e:
            logger.warning("deposit notify %d failed: %s", uid, e)


@router.callback_query(F.data.startswith("dep_ok:"))
async def cb_deposit_confirm(call: CallbackQuery, bot: Bot):
    if not can_confirm_deposit(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    deposit_id = int(call.data.split(":")[1])
    name = call.from_user.full_name or str(call.from_user.id)

    dep = await adb.get_cash_deposit(deposit_id)
    res = await adb.confirm_cash_deposit(deposit_id, call.from_user.id, name)
    if not res.get("ok"):
        return await call.answer(f"⚠️ {res.get('error', 'уже обработано')}", show_alert=True)

    await call.answer("✅ Подтверждено")
    # Round 6 (S1): html_text сохраняет HTML-entities из оригинала; .text
    # отдаёт Telegram-decoded строку, на которой повторный parse_mode="HTML"
    # ломается если в оригинале был `<` или `>` (например, esc(<Имя>) в
    # карточке от _notify_confirmers). getattr с fallback — для совместимости
    # с тестовыми моками без свойства html_text.
    original = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        original + f"\n\n{DIV}\n✅ <b>Подтверждено</b> — {esc(name)}",
        parse_mode="HTML",
        reply_markup=webapp_keyboard("🌐 Ещё сдачи — в WebApp"),
    )
    if dep and dep.get("manager_id"):
        closed = res.get("closed_orders") or []
        extra = f" Закрыты заказы: {', '.join('#' + str(o) for o in closed)}." if closed else ""
        try:
            await bot.send_message(
                dep["manager_id"],
                f"✅ Ваша сдача #{deposit_id} подтверждена.{extra}",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("dep_no:"))
async def cb_deposit_reject(call: CallbackQuery, state: FSMContext):
    if not can_confirm_deposit(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    deposit_id = int(call.data.split(":")[1])
    await state.set_state(DepositReject.waiting_for_reason)
    await state.update_data(
        deposit_id=deposit_id, msg_chat=call.message.chat.id, msg_id=call.message.message_id
    )
    await call.answer()
    # T3.2: пока вводится причина, кнопки карточки не должны работать — иначе
    # ту же сдачу можно подтвердить параллельно с отклонением.
    await drop_keyboard(call)
    await call.message.answer("✍️ Укажите причину отклонения сдачи (одним сообщением):")


@router.message(DepositReject.waiting_for_reason)
async def process_deposit_reject_reason(message: Message, state: FSMContext, bot: Bot):
    # Round 6 (L_R1): повторный role-check после FSM-перехода. Между callback'ом
    # (cb_deposit_reject) и сообщением с причиной админ мог снять роль; без
    # этого юзер успевает выполнить reject уже не будучи confirmer'ом.
    if not can_confirm_deposit(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — действие отменено.")
    # Round 6 (L_R8): жёсткий cap на reason — DB-колонка TEXT, шлётся в Telegram.
    reason = (message.text or "").strip()[:500]
    if len(reason) < 3:
        return await message.answer("❌ Причина слишком короткая. Повторите.")
    data = await state.get_data()
    await state.clear()
    deposit_id = data.get("deposit_id")
    name = message.from_user.full_name or str(message.from_user.id)

    dep = await adb.get_cash_deposit(deposit_id)
    res = await adb.reject_cash_deposit(deposit_id, message.from_user.id, name, reason)
    if not res.get("ok"):
        return await message.answer(f"⚠️ {res.get('error', 'уже обработано')}")

    # T3.2: пометку вешаем на саму карточку сдачи (кнопки с неё сняты ещё на
    # входе в FSM) — иначе карточка навсегда остаётся «на подтверждении», а
    # решение теряется отдельной строкой ниже в чате.
    note = f"❌ Сдача #{deposit_id} отклонена"
    if not await finish_message(bot, data.get("msg_chat"), data.get("msg_id"), note):
        await message.answer(f"{note}.")
    if dep and dep.get("manager_id"):
        try:
            await bot.send_message(
                dep["manager_id"],
                f"❌ Ваша сдача #{deposit_id} отклонена.\nПричина: {esc(reason)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
