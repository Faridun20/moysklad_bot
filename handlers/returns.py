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
from services.notify_policy import RETURN, should_notify_now
from services.roles import can_confirm_return, can_mark_return_goods_received, notify_recipients
from utils.formatters import DIV
from handlers._ui import (
    disabled_button,
    outcome_label,
    replace_keyboard,
    settle_card,
    settle_markup,
    webapp_keyboard,
)
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


def _confirm_keyboard(
    return_id: int, *, goods_received: bool = False, received_label: str | None = None
):
    """Клавиатура карточки возврата.

    goods_received=True — приёмка уже отмечена, кнопку убираем: повторное
    нажатие ничего не меняет (T3.2), а «Подтвердить возврат» должна остаться,
    иначе после T2.8 боссу нечем закрыть возврат. `received_label` — на месте
    «Товар получен» неактивная кнопка с тем, кто и когда принял товар.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить возврат", callback_data=f"ret_ok:{return_id}")
    if not goods_received:
        kb.button(text="📦 Товар получен", callback_data=f"ret_got:{return_id}")
    elif received_label:
        kb.add(disabled_button(received_label))
    kb.adjust(1)
    return kb.as_markup()


def _return_callbacks(return_id: int) -> set[str]:
    return {f"ret_ok:{return_id}", f"ret_got:{return_id}"}


async def _settle_stale_return(call: CallbackQuery, return_id: int) -> bool:
    """Возврат уже подтверждён (WebApp или другой руководитель) — гасим
    кнопки карточки исходом. Возврат ещё ждёт — не трогаем."""
    ret = await adb.get_return(return_id)
    status = (ret or {}).get("status")
    if not status or status == "pending":
        return False
    await settle_card(
        call,
        _return_callbacks(return_id),
        "✅ Возврат уже подтверждён" if status == "confirmed" else "ℹ️ Возврат уже обработан",
        tail=webapp_keyboard("🌐 Ещё возвраты — в WebApp"),
    )
    return True


async def _notify_confirmers(bot: Bot, return_id, order_id, total, refund):
    """Карточка возврата — ДВА получателя с разной логикой отправки.

    «Товар получен» — физическая приёмка (кладовщик, а без него менеджер-
    заместитель, services.roles.ROLE_ALSO_ACTS_AS): не зависит от суммы,
    товар везут и принимают вне зависимости от того, идёт ли боссу пуш.
    Склад получает карточку ВСЕГДА.

    «Подтвердить возврат» — денежное решение (admin/boss), режется порогом
    `boss_instant_threshold_usd` (services.notify_policy): ниже порога боссу
    карточка не идёт, возврат остаётся pending и попадает в вечерний
    дайджест (services.boss_digest) — решение по-прежнему видно в WebApp.
    """
    # `total` — сумма возврата в валюте ЗАКАЗА (services.database.create_return
    # считает её из order_items.price_cents), а не всегда USD: хардкод "USD"
    # здесь сравнивал бы курицу с яйцами — 6000 сум мимо порога считались бы
    # как $6000 и улетали боссу мгновенной карточкой вместо ~$0.47.
    order = await adb.get_order(order_id)
    currency = (order or {}).get("currency") or "USD"

    users = await adb.get_all_users()
    warehouse_recipients = notify_recipients(users, ("warehouse_keeper",))
    recipients = list(warehouse_recipients)
    if should_notify_now(RETURN, total, currency):
        boss_recipients = notify_recipients(users, ("admin", "boss"))
        recipients += [uid for uid in boss_recipients if uid not in recipients]
    if not recipients:
        return
    text = (
        f"{DIV}\n↩️ <b>Возврат #{return_id}</b> · заказ #{order_id}\n"
        f"💰 {_fmt(total)} {currency} · {_REFUND_LABELS.get(refund, refund)}"
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
    if not can_mark_return_goods_received(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    return_id = int(call.data.split(":")[1])
    res = await adb.mark_return_goods_received(return_id, call.from_user.id)
    if not res.get("ok"):
        await call.answer("⚠️ Уже обработано", show_alert=True)
        if not await _settle_stale_return(call, return_id):
            # Возврат ждёт, но приёмку уже отметили (на другой карточке или
            # в WebApp) — погасить только «Товар получен».
            await settle_card(call, {f"ret_got:{return_id}"}, "📦 Товар уже получен")
        return
    await call.answer("📦 Отмечено: товар получен")
    # T3.2: помечаем результат в карточке и убираем отработавшую кнопку;
    # «Подтвердить возврат» оставляем — процесс продолжается.
    await replace_keyboard(
        call,
        "📦 Товар получен",
        _confirm_keyboard(
            return_id,
            goods_received=True,
            received_label=outcome_label("📦 Товар получен", call.from_user),
        ),
    )


@router.callback_query(F.data.startswith("ret_ok:"))
async def cb_return_confirm(call: CallbackQuery, bot: Bot):
    if not can_confirm_return(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    return_id = int(call.data.split(":")[1])
    name = call.from_user.full_name or str(call.from_user.id)

    res = await adb.confirm_return(return_id, call.from_user.id, name)
    if not res.get("ok"):
        await call.answer(f"⚠️ {res.get('error', 'уже обработано')}", show_alert=True)
        await _settle_stale_return(call, return_id)
        return

    await call.answer("✅ Возврат подтверждён")
    # Round 6 (S1): html_text сохраняет HTML-entities. См. handlers/deposits.py.
    original = getattr(call.message, "html_text", None) or call.message.text or ""
    # Куда делся товар — видно сразу: без строки о накладной кладовщик не
    # знает, вернулся ли остаток, и идёт проверять склад руками.
    stock_line = (
        f"\n📦 Оприходовано накладной {esc(str(res['invoice_number']))}"
        if res.get("invoice_number")
        else f"\n⚠️ На склад не оприходовано: {esc(str(res.get('stock_skipped') or '—'))}"
    )
    await call.message.edit_text(
        original
        + f"\n\n{DIV}\n✅ <b>Подтверждено</b> ({res['order_status']}) — {esc(name)}"
        + stock_line,
        parse_mode="HTML",
        reply_markup=settle_markup(
            getattr(call.message, "reply_markup", None),
            _return_callbacks(return_id),
            outcome_label("✅ Возврат подтверждён", call.from_user),
            tail=webapp_keyboard("🌐 Ещё возвраты — в WebApp"),
        ),
    )
