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
        return await message.answer("⛔ Отмечать отгрузку может босс/кладовщик/админ.")
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer(
            "🚚 Формат: <code>/ship НОМЕР_ЗАКАЗА</code>\nНапример: <code>/ship 142</code>",
            parse_mode="HTML",
        )
    order_id = int(parts[1])
    order = await adb.get_order(order_id)
    name = message.from_user.full_name or str(message.from_user.id)
    res = await adb.mark_order_shipped(order_id, message.from_user.id, name)
    if not res.get("ok"):
        return await message.answer(f"⚠️ {res.get('error', 'не удалось отгрузить')}")

    await message.answer(f"🚚 Заказ #{order_id} отмечен <b>отгруженным</b>.", parse_mode="HTML")
    creator = order.get("user_id") if order else None
    if creator and creator != message.from_user.id:
        try:
            await bot.send_message(creator, f"🚚 Ваш заказ #{order_id} отгружен ({esc(name)}).")
        except Exception as e:
            logger.warning("ship notify %s failed: %s", creator, e)
