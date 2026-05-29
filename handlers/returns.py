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
from services.roles import (
    _has_role,
    can_confirm_return,
    can_create_return,
    is_warehouse_keeper,
)
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
    choosing_items = State()  # выбор позиций для частичного возврата
    entering_item_qty = State()  # ввод количества по выбранной позиции


def _fmt(x: float) -> str:
    from services import money

    return money.format_cents(money.to_cents(x or 0), decimals=2, sep=" ")


def _q(x: float) -> str:
    """Количество без хвостовых нулей: 2.0 → '2', 1.5 → '1.5'."""
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:g}"


def _items_keyboard(ritems: list[dict], selected: dict):
    """Клавиатура выбора позиций частичного возврата.
    selected: {str(order_item_id): qty}. Выбранные помечаем галочкой+кол-вом."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Весь заказ", callback_data="ret_full")
    for i, it in enumerate(ritems):
        sel = selected.get(str(it["id"]))
        mark = f"✓{_q(sel)} " if sel else ""
        name = it["name"][:24]
        kb.button(
            text=f"{mark}поз {i + 1}: {name} · до {_q(it['avail'])} {it['unit']}",
            callback_data=f"ret_pi:{i}",
        )
    if selected:
        kb.button(text=f"✅ Готово ({len(selected)})", callback_data="ret_pdone")
    kb.button(text="❌ Отмена", callback_data="ret_cancel")
    kb.adjust(1)
    return kb.as_markup()


async def _finalize_return(call, bot, *, order_id, reason, refund, ret_items, return_type, user_id):
    """Создать возврат (full/partial), отрисовать итог и уведомить подтверждающих."""
    # H2: force (обход дедлайна) — только начальству/складу, не менеджеру.
    privileged = _has_role(user_id, "admin", "boss", "warehouse_keeper")
    res = await adb.create_return(
        order_id,
        return_type,
        reason,
        ret_items,
        refund_method=refund,
        created_by=user_id,
        force=privileged,
    )
    if not res.get("ok"):
        return await call.answer(f"⚠️ {res.get('error', 'не удалось')}", show_alert=True)

    await call.answer("Возврат создан")
    kind = "полный" if return_type == "full" else "частичный"
    await call.message.edit_text(
        f"{DIV}\n↩️ <b>Возврат #{res['return_id']}</b> по заказу #{order_id} ({kind}).\n"
        f"💰 Сумма: <b>{_fmt(res['total_amount'])} USD</b>\n"
        f"Способ: {_REFUND_LABELS.get(refund, refund)}\n\n"
        f"Отправлено на подтверждение.",
        parse_mode="HTML",
    )
    await _notify_confirmers(bot, res["return_id"], order_id, res["total_amount"], refund)


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
    # H2: менеджер вправе вернуть только свой заказ; начальство/склад — любой.
    privileged = _has_role(message.from_user.id, "admin", "boss", "warehouse_keeper")
    if not privileged and order.get("user_id") != message.from_user.id:
        return await message.answer("⛔ Можно оформлять возврат только по своим заказам.")
    # Отгружен/оплачен/частично-возвращён ИЛИ оплачен по легаси (paid_confirmed_at).
    if order.get("status") not in ("shipped", "paid", "partially_returned") and not order.get(
        "paid_confirmed_at"
    ):
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
    # Round 6 (L_R1): повторный role-check после FSM-перехода (роль могла быть
    # снята после старта flow).
    if not can_create_return(message.from_user.id):
        await state.clear()
        return await message.answer("⛔ Нет доступа — оформление возврата отменено.")
    # Round 6 (L_R8): жёсткий cap на reason.
    reason = (message.text or "").strip()[:500]
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
async def cb_return_refund(call: CallbackQuery, state: FSMContext):
    """Способ возврата выбран → переходим к выбору позиций (полный/частичный)."""
    refund = call.data.split(":")[1]
    data = await state.get_data()
    order_id = data.get("order_id")

    # Готовим список позиций с доступным к возврату остатком (qty - returned_qty).
    items = await adb.get_order_items(order_id)
    ritems = []
    for it in items:
        avail = float(it.get("quantity", 0) or 0) - float(it.get("returned_qty", 0) or 0)
        if avail <= 0:
            continue
        ritems.append(
            {
                "id": it["id"],
                "name": it.get("product_name", "—"),
                "unit": it.get("unit") or "шт",
                "avail": avail,
                "price": float(it.get("price", 0) or 0),
            }
        )
    if not ritems:
        await state.clear()
        return await call.answer("⚠️ Нет позиций, доступных к возврату", show_alert=True)

    await state.update_data(refund=refund, ritems=ritems, selected={})
    await state.set_state(ReturnFlow.choosing_items)
    await call.answer()
    await call.message.edit_text(
        f"{DIV}\n↩️ <b>Что вернуть по заказу #{order_id}?</b>\n\n"
        f"«📦 Весь заказ» — вернуть всё. Либо выберите позиции по одной "
        f"(спрошу количество), затем «✅ Готово».",
        parse_mode="HTML",
        reply_markup=_items_keyboard(ritems, {}),
    )


@router.callback_query(ReturnFlow.choosing_items, F.data == "ret_full")
async def cb_return_full(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Шорткат «Весь заказ» — полный возврат всех доступных позиций."""
    data = await state.get_data()
    await state.clear()
    ritems = data.get("ritems", [])
    ret_items = [(it["id"], it["avail"], round(it["avail"] * it["price"], 2)) for it in ritems]
    await _finalize_return(
        call,
        bot,
        order_id=data.get("order_id"),
        reason=data.get("reason", ""),
        refund=data.get("refund", "no_refund"),
        ret_items=ret_items,
        return_type="full",
        user_id=call.from_user.id,
    )


@router.callback_query(ReturnFlow.choosing_items, F.data.startswith("ret_pi:"))
async def cb_return_pick_item(call: CallbackQuery, state: FSMContext):
    """Выбрана позиция → спрашиваем количество к возврату."""
    idx = int(call.data.split(":")[1])
    data = await state.get_data()
    ritems = data.get("ritems", [])
    if idx >= len(ritems):
        return await call.answer("Позиция не найдена", show_alert=True)
    it = ritems[idx]
    await state.update_data(pending_idx=idx)
    await state.set_state(ReturnFlow.entering_item_qty)
    await call.answer()
    await call.message.answer(
        f"Сколько вернуть: <b>{esc(it['name'])}</b>?\n"
        f"Доступно <b>{_q(it['avail'])} {it['unit']}</b>. "
        f"Отправьте число (или <code>0</code> чтобы убрать позицию).",
        parse_mode="HTML",
    )


@router.message(ReturnFlow.entering_item_qty)
async def process_item_qty(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        qty = float(raw)
    except ValueError:
        return await message.answer("❌ Введите число, например <code>2</code>.", parse_mode="HTML")
    data = await state.get_data()
    ritems = data.get("ritems", [])
    selected = dict(data.get("selected", {}))
    idx = int(data.get("pending_idx", 0))
    if idx >= len(ritems):
        await state.set_state(ReturnFlow.choosing_items)
        return await message.answer("Позиция не найдена.")
    it = ritems[idx]
    key = str(it["id"])
    if qty <= 0:
        selected.pop(key, None)  # 0 — убрать позицию из выбора
    else:
        qty = min(qty, float(it["avail"]))  # клампим к доступному
        selected[key] = qty
    await state.update_data(selected=selected)
    await state.set_state(ReturnFlow.choosing_items)
    chosen = (
        ", ".join(f"{esc(r['name'][:16])}×{_q(selected[str(r['id'])])}"
                  for r in ritems if str(r["id"]) in selected)
        or "пока ничего"
    )
    await message.answer(
        f"Выбрано: {chosen}.\nДобавьте ещё позиции или нажмите «✅ Готово».",
        parse_mode="HTML",
        reply_markup=_items_keyboard(ritems, selected),
    )


@router.callback_query(ReturnFlow.choosing_items, F.data == "ret_pdone")
async def cb_return_partial_done(call: CallbackQuery, state: FSMContext, bot: Bot):
    """Завершить частичный возврат по выбранным позициям."""
    data = await state.get_data()
    ritems = data.get("ritems", [])
    selected = data.get("selected", {})
    if not selected:
        return await call.answer("Выберите хотя бы одну позицию", show_alert=True)
    price_by_id = {str(it["id"]): it["price"] for it in ritems}
    ret_items = [
        (int(oid), float(qty), round(float(qty) * price_by_id.get(oid, 0.0), 2))
        for oid, qty in selected.items()
    ]
    # Если выбраны все позиции в полном объёме — это фактически полный возврат.
    is_full = len(selected) == len(ritems) and all(
        abs(float(selected.get(str(it["id"]), 0)) - float(it["avail"])) < 1e-9 for it in ritems
    )
    await state.clear()
    await _finalize_return(
        call,
        bot,
        order_id=data.get("order_id"),
        reason=data.get("reason", ""),
        refund=data.get("refund", "no_refund"),
        ret_items=ret_items,
        return_type="full" if is_full else "partial",
        user_id=call.from_user.id,
    )


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


async def _show_pending_returns(target):
    """Список возвратов на подтверждении с кнопками (склад/босс/админ).
    Раньше — только из push-уведомления; теперь есть команда /returns."""
    pending = await adb.get_pending_returns()
    if not pending:
        return await target.answer("✅ Нет возвратов на подтверждении.")
    await target.answer(
        f"↩️ <b>Возвраты на подтверждении ({len(pending)}):</b>", parse_mode="HTML"
    )
    for r in pending:
        gr = "📦 товар получен" if r.get("goods_received") else "⏳ товар не отмечен"
        refund = _REFUND_LABELS.get(r.get("refund_method"), r.get("refund_method") or "—")
        text = (
            f"{DIV}\n↩️ <b>Возврат #{r['id']}</b> · заказ #{r['order_id']}\n"
            f"💰 {_fmt(r['total_amount'])} USD · {refund}\n"
            f"{gr}\n📝 {esc(r.get('reason') or '')}"
        )
        await target.answer(text, parse_mode="HTML", reply_markup=_confirm_keyboard(r["id"]))


@router.message(Command("returns"))
async def cmd_pending_returns(message: Message):
    if not can_confirm_return(message.from_user.id) and not is_warehouse_keeper(message.from_user.id):
        return await message.answer("⛔ Возвраты подтверждают склад/босс/админ.")
    await _show_pending_returns(message)


@router.callback_query(F.data == "ret_pending")
async def cb_pending_returns(call: CallbackQuery):
    if not can_confirm_return(call.from_user.id) and not is_warehouse_keeper(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()
    await _show_pending_returns(call.message)


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
    )
