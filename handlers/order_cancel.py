"""
Хэндлеры: отмена заказа (IMPLEMENTATION.md §6.7).

Босс/админ: /cancel <order_id> → причина → cancel_order. Отмена доступна
только для approved-заказов (shipped → через возврат /return). Reverse-demand
в МойСклад — отдельной фазой; здесь DB-часть + уведомление менеджеру.

Логика — в services.database.cancel_order; тут Telegram-UI.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder


def _abort_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="cancel_abort")
    return kb.as_markup()

from services import async_db as adb
from services.roles import _has_role
from utils.formatters import DIV
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()


def _can_cancel(user_id: int) -> bool:
    return _has_role(user_id, "admin", "boss")


class CancelFlow(StatesGroup):
    waiting_reason = State()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not _can_cancel(message.from_user.id):
        return await message.answer("⛔ Отменять заказы может босс/админ.")
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer(
            "🚫 Формат: <code>/cancel НОМЕР_ЗАКАЗА</code>\nНапример: <code>/cancel 142</code>",
            parse_mode="HTML",
        )
    order_id = int(parts[1])
    order = await adb.get_order(order_id)
    if not order:
        return await message.answer("❌ Заказ не найден.")
    if order.get("status") != "approved":
        return await message.answer(
            "⚠️ Отмена доступна только для одобренных (approved) заказов.\n"
            "Отгруженные оформляйте через возврат: <code>/return</code>.",
            parse_mode="HTML",
        )
    await state.clear()
    await state.set_state(CancelFlow.waiting_reason)
    await state.update_data(order_id=order_id)
    await message.answer(
        f"{DIV}\n🚫 <b>Отмена заказа #{order_id}</b>\n\nУкажите причину отмены (одним сообщением):",
        parse_mode="HTML",
        reply_markup=_abort_keyboard(),
    )


@router.callback_query(F.data == "cancel_abort")
async def cb_cancel_abort(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Отменено")
    try:
        await call.message.edit_text("🚫 Отмена заказа прервана.")
    except Exception:
        pass


@router.message(CancelFlow.waiting_reason)
async def process_cancel_reason(message: Message, state: FSMContext, bot: Bot):
    # Round 6 (L_R1): повторный role-check после FSM-перехода.
    if not _can_cancel(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — отмена сброшена.")
    # Round 6 (L_R8): жёсткий cap на reason.
    reason = (message.text or "").strip()[:500]
    if len(reason) < 3:
        return await message.answer("❌ Причина слишком короткая. Повторите.")
    data = await state.get_data()
    await state.clear()
    order_id = data.get("order_id")
    name = message.from_user.full_name or str(message.from_user.id)

    order = await adb.get_order(order_id)
    res = await adb.cancel_order(order_id, message.from_user.id, name, reason)
    if not res.get("ok"):
        return await message.answer(f"⚠️ {res.get('error', 'не удалось отменить')}")

    await message.answer(
        f"🚫 Заказ #{order_id} отменён.\nПричина: {esc(reason)}", parse_mode="HTML"
    )
    # Уведомить создателя заказа (если это не сам отменяющий).
    creator = order.get("user_id") if order else None
    if creator and creator != message.from_user.id:
        try:
            await bot.send_message(
                creator,
                f"🚫 Ваш заказ #{order_id} отменён боссом.\nПричина: {esc(reason)}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("cancel notify %s failed: %s", creator, e)
