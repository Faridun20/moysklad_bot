"""
Хэндлеры: возвраты товара (IMPLEMENTATION.md §8).

Менеджер/кладовщик/босс: /return <order_id> → причина → способ возврата денег
→ создаётся возврат на весь заказ (full) и уходит на подтверждение.
Кладовщик: «📦 Товар получен». Босс/админ: «✅ Подтвердить возврат».

Частичный возврат (выбор позиций) — через WebApp/отдельной фазой; здесь
быстрый полный возврат, чтобы флоу был кликабельным.

Логика — в services.database (create/confirm/mark_return_*); тут Telegram-UI.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services import async_db as adb
from services.roles import can_confirm_return, can_create_return, is_warehouse_keeper
from utils.formatters import DIV
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()

_REFUND_LABELS = {
    "cash": "💵 Наличными",
    "debt_reduction": "📉 В счёт долга",
    "no_refund": "🚫 Без возврата денег",
}


class ReturnFlow(StatesGroup):
    waiting_reason = State()
    waiting_refund = State()


def _fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def _refund_keyboard():
    kb = InlineKeyboardBuilder()
    for code, label in _REFUND_LABELS.items():
        kb.button(text=label, callback_data=f"ret_rm:{code}")
    kb.button(text="❌ Отмена", callback_data="ret_cancel")
    kb.adjust(1)
    return kb.as_markup()


def _confirm_keyboard(return_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить возврат", callback_data=f"ret_ok:{return_id}")
    kb.button(text="📦 Товар получен", callback_data=f"ret_got:{return_id}")
    kb.adjust(1)
    return kb.as_markup()


async def _order_total(items: list[dict]) -> float:
    return sum(float(i.get("quantity", 0)) * float(i.get("price", 0) or 0) for i in items)


@router.message(Command("return"))
async def cmd_return(message: Message, state: FSMContext):
    if not can_create_return(message.from_user.id):
        return await message.answer("⛔ Нет доступа к оформлению возвратов.")
    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer(
            "↩️ Формат: <code>/return НОМЕР_ЗАКАЗА</code>\nНапример: <code>/return 142</code>",
            parse_mode="HTML",
        )
    order_id = int(parts[1])
    order = await adb.get_order(order_id)
    if not order:
        return await message.answer("❌ Заказ не найден.")
    if order.get("status") not in ("shipped", "paid", "partially_returned"):
        return await message.answer("⚠️ Возврат доступен только для отгруженных/оплаченных заказов.")

    items = await adb.get_order_items(order_id)
    total = await _order_total(items)
    await state.clear()
    await state.set_state(ReturnFlow.waiting_reason)
    await state.update_data(order_id=order_id)
    await message.answer(
        f"{DIV}\n↩️ <b>Возврат по заказу #{order_id}</b>\n"
        f"💰 Сумма заказа: <b>{_fmt(total)} USD</b>\n\n"
        f"Опишите причину возврата (одним сообщением):",
        parse_mode="HTML",
    )


@router.message(ReturnFlow.waiting_reason)
async def process_return_reason(message: Message, state: FSMContext):
    reason = (message.text or "").strip()
    if len(reason) < 3:
        return await message.answer("❌ Причина слишком короткая. Повторите.")
    await state.update_data(reason=reason)
    await state.set_state(ReturnFlow.waiting_refund)
    await message.answer("Как вернуть деньги клиенту?", reply_markup=_refund_keyboard())


@router.callback_query(F.data == "ret_cancel")
async def cb_return_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer("Отменено")
    await call.message.edit_text("↩️ Оформление возврата отменено.")


@router.callback_query(ReturnFlow.waiting_refund, F.data.startswith("ret_rm:"))
async def cb_return_refund(call: CallbackQuery, state: FSMContext, bot: Bot):
    refund = call.data.split(":")[1]
    data = await state.get_data()
    await state.clear()
    order_id = data.get("order_id")
    reason = data.get("reason", "")

    items = await adb.get_order_items(order_id)
    ret_items = [
        (
            it["id"],
            float(it.get("quantity", 0)),
            round(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0), 2),
        )
        for it in items
    ]
    res = await adb.create_return(
        order_id,
        "full",
        reason,
        ret_items,
        refund_method=refund,
        created_by=call.from_user.id,
        force=True,
    )
    if not res.get("ok"):
        return await call.answer(f"⚠️ {res.get('error', 'не удалось')}", show_alert=True)

    await call.answer("Возврат создан")
    await call.message.edit_text(
        f"{DIV}\n↩️ <b>Возврат #{res['return_id']}</b> по заказу #{order_id} создан.\n"
        f"💰 Сумма: <b>{_fmt(res['total_amount'])} USD</b>\n"
        f"Способ: {_REFUND_LABELS.get(refund, refund)}\n\n"
        f"Отправлено на подтверждение.",
        parse_mode="HTML",
    )
    await _notify_confirmers(bot, res["return_id"], order_id, res["total_amount"], refund)


async def _notify_confirmers(bot: Bot, return_id, order_id, total, refund):
    users = await adb.get_all_users()
    recipients = [u["user_id"] for u in users if u["role"] in ("admin", "boss", "warehouse_keeper")]
    text = (
        f"{DIV}\n↩️ <b>Возврат #{return_id}</b> · заказ #{order_id}\n"
        f"💰 {_fmt(total)} USD · {_REFUND_LABELS.get(refund, refund)}"
    )
    for uid in recipients:
        try:
            await bot.send_message(
                uid, text, parse_mode="HTML", reply_markup=_confirm_keyboard(return_id)
            )
        except Exception as e:
            logger.warning("return notify %d failed: %s", uid, e)


@router.callback_query(F.data.startswith("ret_got:"))
async def cb_return_goods_received(call: CallbackQuery):
    if not is_warehouse_keeper(call.from_user.id) and not can_confirm_return(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    return_id = int(call.data.split(":")[1])
    res = await adb.mark_return_goods_received(return_id, call.from_user.id)
    if not res.get("ok"):
        return await call.answer("⚠️ Уже обработано", show_alert=True)
    await call.answer("📦 Отмечено: товар получен")


@router.callback_query(F.data.startswith("ret_ok:"))
async def cb_return_confirm(call: CallbackQuery, bot: Bot):
    if not can_confirm_return(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    return_id = int(call.data.split(":")[1])
    name = call.from_user.full_name or str(call.from_user.id)

    res = await adb.confirm_return(return_id, call.from_user.id, name)
    if not res.get("ok"):
        return await call.answer(f"⚠️ {res.get('error', 'уже обработано')}", show_alert=True)

    await call.answer("✅ Возврат подтверждён")
    await call.message.edit_text(
        (call.message.text or "")
        + f"\n\n{DIV}\n✅ <b>Подтверждено</b> ({res['order_status']}) — {esc(name)}",
        parse_mode="HTML",
    )
