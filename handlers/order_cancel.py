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
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message

from handlers._ui import finish_message, prompt_keyboard


def _abort_keyboard():
    """«❌ Отмена» под вопросом о причине + force_reply (Bot API 10.3): поле
    ввода открывается ответом на вопрос сразу."""
    return prompt_keyboard(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_abort"))

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
        return await message.answer("⛔ Отменить заказ может руководитель или администратор.")
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer(
            "🚫 Напишите номер заказа: <code>/cancel НОМЕР</code>\n"
            "Например: <code>/cancel 142</code>",
            parse_mode="HTML",
        )
    order_id = int(parts[1])
    order = await adb.get_order(order_id)
    if not order:
        return await message.answer("❌ Заказа с таким номером нет — проверьте номер.")
    if order.get("status") != "approved":
        return await message.answer(
            "⚠️ Отменить можно только одобренный заказ.\n"
            "Отгруженный оформляйте возвратом — в WebApp: заказ → «Оформить возврат».",
            parse_mode="HTML",
        )
    await state.clear()
    await state.set_state(CancelFlow.waiting_reason)
    prompt = await message.answer(
        f"{DIV}\n🚫 <b>Отмена заказа #{order_id}</b>\n\n"
        "Напишите одним сообщением, почему отменяете:",
        parse_mode="HTML",
        reply_markup=_abort_keyboard(),
    )
    # T3.2: запоминаем сообщение с кнопкой «❌ Отмена», чтобы погасить её, когда
    # причина принята. Иначе кнопка живёт и на уже отменённом заказе отвечает
    # «отмена прервана» — прямая ложь, заказ-то отменён.
    await state.update_data(
        order_id=order_id, msg_chat=prompt.chat.id, msg_id=prompt.message_id
    )


@router.callback_query(F.data == "cancel_abort")
async def cb_cancel_abort(call: CallbackQuery, state: FSMContext):
    if await state.get_state() != CancelFlow.waiting_reason.state:
        # Кнопка со старого вопроса (заказ уже отменён или ввод сброшен):
        # «отмена прервана» было бы неправдой.
        await call.answer("Это уже не актуально")
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
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
        return await message.answer(
            "⛔ Отменять заказы вы больше не можете — отмена прервана."
        )
    # Round 6 (L_R8): жёсткий cap на reason.
    reason = (message.text or "").strip()[:500]
    if len(reason) < 3:
        return await message.answer(
            "❌ Причина слишком короткая — напишите хотя бы несколько слов."
        )
    data = await state.get_data()
    await state.clear()
    order_id = data.get("order_id")
    name = message.from_user.full_name or str(message.from_user.id)

    order = await adb.get_order(order_id)
    # T2.6: отмена + реверс customerorder в МойСклад — общий код для бота и
    # WebApp. Реверс best-effort и идемпотентен, ошибка МС не ломает уже
    # выполненную отмену в БД.
    from services.order_workflow import cancel_order_full

    res = await cancel_order_full(order_id, message.from_user.id, name, reason)
    if not res.get("ok"):
        return await message.answer(
            f"⚠️ {res.get('error', 'заказ не отменился — обновите экран и попробуйте снова')}"
        )

    note = f"🚫 Заказ #{order_id} отменён.\nПричина: {esc(reason)}"
    if not await finish_message(
        bot, data.get("msg_chat"), data.get("msg_id"), note, outcome="🚫 Заказ отменён"
    ):
        await message.answer(note, parse_mode="HTML")
    # Уведомить создателя заказа (если это не сам отменяющий).
    creator = order.get("user_id") if order else None
    if creator and creator != message.from_user.id:
        try:
            await bot.send_message(
                creator,
                f"🚫 Ваш заказ #{order_id} отменил руководитель.\nПричина: {esc(reason)}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("cancel notify %s failed: %s", creator, e)
