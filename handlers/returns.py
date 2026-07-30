"""
Хэндлеры: решения по возвратам товара (IMPLEMENTATION.md §8).

T3.3: оформление возврата (/return: причина → способ возврата денег → выбор
позиций) и очередь /returns вырезаны — это WebApp, где с T3.1 есть и частичный
возврат по позициям, чего бот не умел вовсе. В боте остались кнопки под
push-карточкой: «📦 Товар получен» (кладовщик) и «✅ Подтвердить возврат»
(босс/админ) — решение принимается там, где пришло уведомление.

`_notify_confirmers` зовёт и WebApp (webapp/server.py) при создании возврата.

Логика — в services.database (create/confirm/mark_return_*); тут Telegram-UI.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services import async_db as adb
from services.roles import can_confirm_return, is_warehouse_keeper
from utils.formatters import DIV
from handlers._ui import replace_keyboard, webapp_keyboard
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()

_REFUND_LABELS = {
    "cash": "💵 Наличными",
    "debt_reduction": "📉 В счёт долга",
    "no_refund": "🚫 Без возврата денег",
}


def _fmt(x: float) -> str:
    from services import money

    return money.format_cents(money.to_cents(x or 0), decimals=2, sep=" ")


def _confirm_keyboard(return_id: int, *, goods_received: bool = False):
    """Клавиатура карточки возврата.

    goods_received=True — приёмка уже отмечена, кнопку убираем: повторное
    нажатие ничего не меняет (T3.2), а «Подтвердить возврат» должна остаться,
    иначе после T2.8 боссу нечем закрыть возврат.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить возврат", callback_data=f"ret_ok:{return_id}")
    if not goods_received:
        kb.button(text="📦 Товар получен", callback_data=f"ret_got:{return_id}")
    kb.adjust(1)
    return kb.as_markup()


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
    # T3.2: помечаем результат в карточке и убираем отработавшую кнопку;
    # «Подтвердить возврат» оставляем — процесс продолжается.
    await replace_keyboard(
        call, "📦 Товар получен", _confirm_keyboard(return_id, goods_received=True)
    )


@router.callback_query(F.data.startswith("ret_ok:"))
async def cb_return_confirm(call: CallbackQuery, bot: Bot):
    if not can_confirm_return(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    return_id = int(call.data.split(":")[1])
    name = call.from_user.full_name or str(call.from_user.id)

    res = await adb.confirm_return(return_id, call.from_user.id, name)
    if not res.get("ok"):
        return await call.answer(f"⚠️ {res.get('error', 'уже обработано')}", show_alert=True)

    # Best-effort: документ «Возврат покупателя» в МойСклад (no-op без контекста).
    from services import ms_returns

    try:
        await ms_returns.create_salesreturn(return_id)
    except Exception:
        logger.warning("MS salesreturn create failed", exc_info=True)

    await call.answer("✅ Возврат подтверждён")
    # Round 6 (S1): html_text сохраняет HTML-entities. См. handlers/deposits.py.
    original = getattr(call.message, "html_text", None) or call.message.text or ""
    await call.message.edit_text(
        original + f"\n\n{DIV}\n✅ <b>Подтверждено</b> ({res['order_status']}) — {esc(name)}",
        parse_mode="HTML",
        reply_markup=webapp_keyboard("🌐 Ещё возвраты — в WebApp"),
    )
