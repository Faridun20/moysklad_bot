"""
Хэндлер: «Отгрузить» из чата — /ship <order_id>.

Одобрение отгрузки больше не обязательно (решение владельца, сентябрь 2026):
менеджер отгружает свой заказ сам, руководителю приходит уведомление.
Кладовщик, руководитель и администратор отгружают одобренные заказы, как раньше.
После отгрузки становится доступен возврат.

Логика — services.order_workflow.ship_order_now (тот же код, что у кнопки
«Отгрузить» в WebApp); тут Telegram-UI. Разбивку оплаты в чате не набрать —
«оплату сразу» бот отправляет в WebApp.
"""

import logging

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from services.roles import can_confirm_shipment, can_create_orders

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("ship"))
async def cmd_ship(message: Message, bot: Bot):
    if not (can_confirm_shipment(message.from_user.id) or can_create_orders(message.from_user.id)):
        return await message.answer(
            "⛔ Отгрузить заказ может менеджер, кладовщик, руководитель или администратор."
        )
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer(
            "🚚 Напишите номер заказа: <code>/ship НОМЕР</code>\n"
            "Например: <code>/ship 142</code>",
            parse_mode="HTML",
        )
    order_id = int(parts[1])
    name = message.from_user.full_name or str(message.from_user.id)

    from services.order_workflow import ship_order_now

    res = await ship_order_now(order_id, message.from_user.id, name, bot)
    if not res.get("ok"):
        from aiogram.types import InlineKeyboardMarkup

        from handlers._ui import disabled_button, webapp_keyboard

        if res.get("code") == "payment_required":
            # Разбивка оплаты вводится в WebApp (строки, валюты, курс) — в чате
            # её не набрать. Правило то же, что у кнопки «Отгрузить» в WebApp.
            # Порядок шагов виден кнопками: живая «Внести оплату» и под ней
            # неактивная (Bot API 10.3) отгрузка с причиной.
            pay = webapp_keyboard("💳 Внести оплату — в WebApp")
            rows = [
                *(pay.inline_keyboard if pay else []),
                [disabled_button("🚚 Отгрузка — после ввода оплаты")],
            ]
            return await message.answer(
                f"⚠️ {res['error']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
            )
        if res.get("code") == "decision_required":
            return await message.answer(
                f"⚠️ {res['error']}", reply_markup=webapp_keyboard("🌐 Открыть заказ — в WebApp")
            )
        return await message.answer(
            f"⚠️ {res.get('error') or 'Заказ не отгрузился — обновите экран и попробуйте снова'}"
        )

    # Руководителям и автору заказа уведомление шлёт сам сервис (фоном).
    await message.answer(f"🚚 Заказ #{order_id} <b>отгружен</b>.", parse_mode="HTML")
