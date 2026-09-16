"""
Хэндлер: отметка отгрузки заказа (approved → shipped).

Босс/админ/кладовщик: /ship <order_id>. Альтернатива МС-вебхуку для аккаунтов,
где у статусов заказа нет типа «Успешный». После отгрузки становится доступен
возврат.

Логика — services.database.mark_order_shipped; тут Telegram-UI.
"""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from services import async_db as adb
from services.roles import can_confirm_shipment
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("ship"))
async def cmd_ship(message: Message, bot: Bot):
    if not can_confirm_shipment(message.from_user.id):
        return await message.answer(
            "⛔ Отметить отгрузку может кладовщик, руководитель или администратор."
        )
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer(
            "🚚 Напишите номер заказа: <code>/ship НОМЕР</code>\n"
            "Например: <code>/ship 142</code>",
            parse_mode="HTML",
        )
    order_id = int(parts[1])
    order = await adb.get_order(order_id)
    name = message.from_user.full_name or str(message.from_user.id)
    res = await adb.mark_order_shipped(order_id, message.from_user.id, name)
    if not res.get("ok"):
        if res.get("code") == "payment_required":
            # Разбивка оплаты вводится в WebApp (строки, валюты, курс) — в чате
            # её не набрать. Правило то же, что у кнопки «Отгрузить» в WebApp.
            from aiogram.types import InlineKeyboardMarkup

            from handlers._ui import disabled_button, webapp_keyboard

            # Порядок шагов виден кнопками: живая «Внести оплату» и под ней
            # неактивная (Bot API 10.3) отгрузка с причиной — как в
            # уведомлении об одобрении (services.notify.approved_order_keyboard).
            pay = webapp_keyboard("💳 Внести оплату — в WebApp")
            rows = [
                *(pay.inline_keyboard if pay else []),
                [disabled_button("🚚 Отгрузка — после ввода оплаты")],
            ]
            return await message.answer(
                f"⚠️ {res['error']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
        return await message.answer(
            f"⚠️ {res.get('error', 'заказ не отгрузился — обновите экран и попробуйте снова')}"
        )

    await message.answer(f"🚚 Заказ #{order_id} <b>отгружен</b>.", parse_mode="HTML")
    creator = order.get("user_id") if order else None
    if creator and creator != message.from_user.id:
        try:
            await bot.send_message(creator, f"🚚 Ваш заказ #{order_id} отгружен ({esc(name)}).")
        except Exception as e:
            logger.warning("ship notify %s failed: %s", creator, e)
