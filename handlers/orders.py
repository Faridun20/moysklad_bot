"""
Хэндлеры: заказы и заявки на отгрузку

Флоу:
1. Менеджер создаёт заказ (/neworder)
2. Добавляет товары из каталога МойСклад
3. Выбирает клиента (контрагента)
4. Отправляет заявку на отгрузку
5. Руководитель одобряет или отклоняет
6. Менеджер получает уведомление
"""

import asyncio
import html
import logging
from datetime import datetime

from config import BASE_CURRENCY as _BASE_CURRENCY, ALLOWED_CURRENCIES


# Единая реализация в utils.helpers.esc — оставлен _esc-алиас чтобы
# не править каждый callsite в этом большом файле.
from utils.helpers import esc as _esc  # noqa: E402


def _cur(amount: float, currency: str | None = None) -> str:
    """Форматирует сумму с валютой: «150 USD». Если валюта не передана —
    дефолт из BASE_CURRENCY (для обратной совместимости старых вызовов)."""
    return f"{_fmt_num(amount)} {currency or _BASE_CURRENCY}"

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.roles import can_create_orders, is_boss
from services.database import (
    create_order, get_order, get_orders_by_ids, get_user_orders, get_all_orders,
    update_order_status, update_order_agent, update_order_currency,
    add_order_item, get_order_items, remove_order_item,
    create_shipment_request, get_shipment_request,
    get_pending_requests, approve_shipment_request, reject_shipment_request,
    get_role, add_audit_log, get_all_users,
)
from services import async_db as adb
from services.moysklad import get_all_stock, get_categories, ms_get
from services.notifier import get_notify_recipients, send_to_recipients
from utils.helpers import extract_id_from_href, extract_href, safe_get, user_safe_error
from utils.formatters import DIV, DIV2

logger = logging.getLogger(__name__)
router = Router()


# ─── FSM состояния ────────────────────────────────────────────────────────────


class OrderState(StatesGroup):
    choosing_product  = State()  # выбор товара
    entering_quantity = State()  # ввод количества
    entering_price    = State()  # ввод цены за единицу
    choosing_agent    = State()  # выбор клиента
    entering_comment  = State()  # комментарий к заявке


# ─── Форматирование ───────────────────────────────────────────────────────────


STATUS_EMOJI = {
    "draft":    "📝",
    "pending":  "⏳",
    "approved": "✅",
    "rejected": "❌",
    "shipped":  "🚚",
}

STATUS_NAME = {
    "draft":    "Черновик",
    "pending":  "На рассмотрении",
    "approved": "Одобрено",
    "rejected": "Отклонено",
    "shipped":  "Отгружено",
}


def _line_total(item: dict) -> float:
    return float(item.get("quantity", 0)) * float(item.get("price", 0) or 0)


def _fmt_num(n: float) -> str:
    """Без бесконечных нулей: 150.0 → 150, 49.99 → 49.99."""
    if float(n).is_integer():
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def format_order(order: dict, items: list[dict]) -> str:
    status_emoji = STATUS_EMOJI.get(order["status"], "📋")
    status_name  = STATUS_NAME.get(order["status"], order["status"])
    agent_str    = (
        f"\n👤 Клиент: <b>{_esc(order['agent_name'])}</b>"
        if order.get("agent_name") else ""
    )
    comment_str  = f"\n📝 {_esc(order['comment'])}" if order.get("comment") else ""
    currency     = order.get("currency") or _BASE_CURRENCY

    lines = [
        DIV,
        f"{status_emoji} <b>Заказ #{order['id']}</b>   <code>{_esc(status_name)}</code>",
        f"<code>{_esc(order['created_at'][:16])}</code> · <code>{_esc(currency)}</code>",
        f"👤 Менеджер: {_esc(order['full_name'])}{agent_str}{comment_str}",
        "",
    ]

    if items:
        lines.append(f"<b>📦 Товары ({len(items)}):</b>")
        lines.append(DIV2)
        total_items = len(items)
        for i, item in enumerate(items[:10]):
            note_str = f"  <i>{_esc(item['note'])}</i>" if item.get("note") else ""
            price = float(item.get("price", 0) or 0)
            qty = float(item.get("quantity", 0))
            unit = _esc(item.get("unit") or "шт")
            if price > 0:
                price_str = (
                    f"     <code>{_fmt_num(qty)} {unit} × "
                    f"{_cur(price, currency)} = {_cur(qty * price, currency)}</code>"
                )
            else:
                price_str = f"     <code>{_fmt_num(qty)} {unit}</code>"
            lines.append(
                f"  {i+1}. <b>{_esc(item['product_name'])}</b>\n{price_str}{note_str}"
            )
        if total_items > 10:
            lines.append(f"  <i>...и ещё {total_items - 10} позиций</i>")

        grand_total = sum(_line_total(it) for it in items)
        if grand_total > 0:
            lines.append(DIV2)
            lines.append(f"<b>💰 Итого: {_cur(grand_total, currency)}</b>")
    else:
        lines.append("<i>Товары не добавлены</i>")

    return "\n".join(lines)


def format_request_notify(order: dict, items: list[dict], req_id: int) -> str:
    """Сообщение боссу о новой заявке. Видны цены — нужны для апрува."""
    currency = order.get("currency") or _BASE_CURRENCY
    lines = []
    for it in items[:10]:
        qty = float(it.get("quantity", 0))
        price = float(it.get("price", 0) or 0)
        sub = qty * price
        name = _esc(it["product_name"])
        unit = _esc(it.get("unit") or "шт")
        if price > 0:
            lines.append(
                f"  • {name}: {_fmt_num(qty)} {unit} "
                f"× {_cur(price, currency)} = <b>{_cur(sub, currency)}</b>"
            )
        else:
            lines.append(
                f"  • {name}: {_fmt_num(qty)} {unit} "
                f"<i>(цена не указана)</i>"
            )
    items_text = "\n".join(lines)
    grand_total = sum(_line_total(it) for it in items)
    if len(items) > 10:
        items_text += f"\n  ...и ещё {len(items) - 10} поз."

    agent_str = (
        f"\n👤 Клиент: <b>{_esc(order['agent_name'])}</b>"
        if order.get("agent_name") else ""
    )
    comment_str = f"\n📝 {_esc(order['comment'])}" if order.get("comment") else ""
    total_str = (
        f"\n\n<b>💰 Итого: {_cur(grand_total, currency)}</b>"
        if grand_total > 0 else ""
    )

    # Тип оплаты: для credit'а явно показываем дату возврата,
    # чтобы босс ещё на этапе апрува видел условия и решал,
    # давать ли клиенту в долг.
    payment_type = order.get("payment_type") or "paid"
    if payment_type == "credit":
        due = order.get("due_date") or "—"
        payment_str = f"\n💳 <b>В долг</b>, погасить до <b>{_esc(due)}</b>"
    else:
        payment_str = "\n💵 Оплата сразу"

    return (
        f"{DIV}\n"
        f"🔔 <b>Новая заявка на отгрузку #{req_id}</b>\n"
        f"\n"
        f"👨‍💼 Менеджер: <b>{_esc(order['full_name'])}</b>{agent_str}{comment_str}"
        f"{payment_str}\n"
        f"\n"
        f"<b>📦 Товары:</b>\n{items_text}"
        f"{total_str}"
    )


# ─── Клавиатуры ──────────────────────────────────────────────────────────────


def order_actions_keyboard(order_id: int, status: str, is_owner: bool):
    kb = InlineKeyboardBuilder()
    if status == "draft" and is_owner:
        kb.button(text="➕ Добавить товар",  callback_data=f"ord_add:{order_id}")
        kb.button(text="👤 Выбрать клиента", callback_data=f"ord_agent:{order_id}")
        kb.button(text="💱 Валюта",          callback_data=f"ord_cur:{order_id}")
        kb.button(text="🚀 Отправить заявку", callback_data=f"ord_submit:{order_id}")
        kb.button(text="🗑 Удалить заказ",   callback_data=f"ord_delete:{order_id}")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def currency_picker_keyboard(order_id: int):
    kb = InlineKeyboardBuilder()
    for c in ALLOWED_CURRENCIES:
        kb.button(text=c, callback_data=f"ord_cur_set:{order_id}:{c}")
    kb.button(text="❌ Отмена", callback_data=f"ord_view:{order_id}")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def request_approve_keyboard(req_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить",   callback_data=f"req_ok:{req_id}")
    kb.button(text="❌ Отклонить",  callback_data=f"req_no:{req_id}")
    kb.adjust(2)
    return kb.as_markup()


def my_orders_keyboard(orders: list[dict]):
    kb = InlineKeyboardBuilder()
    for o in orders[:10]:
        emoji = STATUS_EMOJI.get(o["status"], "📋")
        kb.button(
            text=f"{emoji} Заказ #{o['id']} · {STATUS_NAME.get(o['status'], '')}",
            callback_data=f"ord_view:{o['id']}",
        )
    kb.button(text="➕ Новый заказ", callback_data="ord_new")
    kb.button(text="🏠 Меню",        callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def pending_requests_keyboard(requests: list[dict], orders_by_id: dict):
    kb = InlineKeyboardBuilder()
    head = requests[:10]
    for r in head:
        order = orders_by_id.get(r["order_id"])
        name = order["full_name"] if order else "—"
        kb.button(
            text=f"⏳ Заявка #{r['id']} · {name}",
            callback_data=f"req_view:{r['id']}",
        )
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


# ─── Команды ─────────────────────────────────────────────────────────────────


@router.message(Command("neworder"))
async def cmd_new_order(message: Message):
    if not can_create_orders(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")

    user = message.from_user
    full_name = user.full_name or user.username or str(user.id)
    order_id, role = await asyncio.gather(
        adb.create_order(user.id, full_name),
        adb.get_role(user.id),
    )
    await adb.add_audit_log(user.id, full_name, role, "order_created", f"Создан заказ #{order_id}")

    await message.answer(
        f"{DIV}\n"
        f"✅ <b>Заказ #{order_id} создан</b>\n\n"
        f"Теперь добавьте товары и выберите клиента.\n\n"
        f"Команды:\n"
        f"/myorders — мои заказы\n"
        f"/orders — заявки (для руководителя)",
        parse_mode="HTML",
        reply_markup=order_actions_keyboard(order_id, "draft", True),
    )


@router.message(Command("myorders"))
async def cmd_my_orders(message: Message):
    if not can_create_orders(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")

    orders = await adb.get_user_orders(message.from_user.id)
    if not orders:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Создать первый заказ", callback_data="ord_new")
        kb.adjust(1)
        return await message.answer(
            "📋 <b>У вас пока нет заказов</b>\n\n"
            "Самый удобный способ собрать заказ — открыть WebApp:\n"
            "выберите товары, клиента и отправьте на одобрение.\n\n"
            "🌐 Кнопка «Открыть» снизу чата.\n"
            "Или быстро создать заказ через бота — кнопкой ниже.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    await message.answer(
        f"📋 <b>Мои заказы</b> ({len(orders)}):",
        parse_mode="HTML",
        reply_markup=my_orders_keyboard(orders),
    )


@router.message(Command("orders"))
async def cmd_orders(message: Message):
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")

    requests = await adb.get_pending_requests()
    if not requests:
        return await message.answer(
            f"{DIV}\n⏳ <b>Заявки на отгрузку</b>\n\n<i>Нет новых заявок</i>",
            parse_mode="HTML",
        )

    orders_by_id = await adb.get_orders_by_ids([r["order_id"] for r in requests[:10]])
    await message.answer(
        f"{DIV}\n"
        f"⏳ <b>Заявки на отгрузку</b>\n"
        f"<code>Ожидают рассмотрения: {len(requests)}</code>",
        parse_mode="HTML",
        reply_markup=pending_requests_keyboard(requests, orders_by_id),
    )


# ─── Callback: просмотр заказа ────────────────────────────────────────────────


@router.callback_query(F.data == "ord_new")
async def cb_new_order(call: CallbackQuery):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    user = call.from_user
    full_name = user.full_name or user.username or str(user.id)
    order_id, role = await asyncio.gather(
        adb.create_order(user.id, full_name),
        adb.get_role(user.id),
    )
    await adb.add_audit_log(user.id, full_name, role, "order_created", f"Создан заказ #{order_id}")

    await call.message.answer(
        f"{DIV}\n✅ <b>Заказ #{order_id} создан</b>\n\nДобавьте товары:",
        parse_mode="HTML",
        reply_markup=order_actions_keyboard(order_id, "draft", True),
    )


@router.callback_query(F.data.startswith("ord_view:"))
async def cb_view_order(call: CallbackQuery):
    await call.answer()
    try:
        order_id = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        return await call.message.answer("❌ Некорректный запрос.")

    order = await adb.get_order(order_id)
    if not order:
        return await call.message.answer("❌ Заказ не найден.")

    # Защита от информации utечки: видеть заказ может только владелец
    # или босс/админ. Раньше любой Telegram-юзер с угадаенным ID видел
    # содержимое заявок чужой компании.
    is_owner = order["user_id"] == call.from_user.id
    if not is_owner and not is_boss(call.from_user.id):
        return await call.message.answer("⛔ Нет доступа к этому заказу.")

    items = await adb.get_order_items(order_id)
    txt = format_order(order, items)
    kb = order_actions_keyboard(order_id, order["status"], is_owner)
    await call.message.answer(txt, parse_mode="HTML", reply_markup=kb)


# ─── Callback: добавление товара ──────────────────────────────────────────────


@router.callback_query(F.data.startswith("ord_add:"))
async def cb_add_item(call: CallbackQuery, state: FSMContext):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    order_id = int(call.data.split(":")[1])
    order = await adb.get_order(order_id)
    if not order or order["status"] != "draft":
        return await call.message.answer("❌ Заказ недоступен для редактирования.")
    if order["user_id"] != call.from_user.id:
        return await call.message.answer("⛔ Это не ваш заказ.")

    await state.update_data(order_id=order_id)
    await call.message.answer("⏳ Загружаю каталог товаров…")

    try:
        # Загружаем категории
        cats = await get_categories()
        kb = InlineKeyboardBuilder()
        kb.button(text="📦 Все товары", callback_data=f"cat_pick:all:{order_id}")
        for cat in cats[:15]:
            cat_id = extract_id_from_href(extract_href(cat))
            name = cat.get("name", "—")[:25]
            kb.button(text=f"📁 {name}", callback_data=f"cat_pick:{cat_id}:{order_id}")
        kb.button(text="❌ Отмена", callback_data=f"ord_view:{order_id}")
        kb.adjust(1)
        await call.message.answer(
            "📂 Выберите категорию товаров:",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await call.message.answer(user_safe_error(e, "ord_add catalog"))


@router.callback_query(F.data.startswith("cat_pick:"))
async def cb_cat_pick(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = call.data.split(":")
    cat_id = parts[1]
    order_id = int(parts[2])

    await call.message.answer("⏳ Загружаю товары…")
    try:
        all_stock = await get_all_stock()

        if cat_id != "all":
            filtered = [
                r for r in all_stock
                if extract_id_from_href(extract_href(r, "folder")) == cat_id
            ]
        else:
            filtered = all_stock

        if not filtered:
            return await call.message.answer("📦 Нет товаров в этой категории.")

        # Показываем по 10 товаров
        kb = InlineKeyboardBuilder()
        head = filtered[:20]
        for i, r in enumerate(head):
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = r.get("uom", {}).get("name", "шт")
            # Кодируем href кратко через индекс
            kb.button(
                text=f"{name} ({stock} {unit})",
                callback_data=f"prod_pick:{order_id}:{i}",
            )

        kb.button(text="◀️ Назад", callback_data=f"ord_add:{order_id}")
        kb.button(text="❌ Отмена", callback_data=f"ord_view:{order_id}")
        kb.adjust(1)

        # Сохраняем список товаров в state
        await state.update_data(
            order_id=order_id,
            products=[{
                "name": r.get("name", "—"),
                "href": extract_href(r),
                "unit": safe_get(r, "uom", "name", default="шт"),
                "stock": r.get("stock", 0),
            } for r in head]
        )
        await state.set_state(OrderState.choosing_product)

        await call.message.answer(
            f"📦 Выберите товар (показано {min(20, len(filtered))} из {len(filtered)}):",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await call.message.answer(user_safe_error(e, "cat_pick"))


@router.callback_query(F.data.startswith("prod_pick:"), OrderState.choosing_product)
async def cb_prod_pick(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = call.data.split(":")
    order_id = int(parts[1])
    idx = int(parts[2])

    data = await state.get_data()
    products = data.get("products", [])

    if idx >= len(products):
        return await call.message.answer("❌ Товар не найден.")

    product = products[idx]
    await state.update_data(selected_product=product)
    await state.set_state(OrderState.entering_quantity)

    await call.message.answer(
        f"✅ Выбран: <b>{product['name']}</b>\n"
        f"На складе: <code>{product['stock']} {product['unit']}</code>\n\n"
        f"Введите количество:",
        parse_mode="HTML",
    )


@router.message(OrderState.entering_quantity)
async def process_quantity(message: Message, state: FSMContext):
    try:
        qty = float(message.text.strip().replace(",", "."))
        if qty <= 0:
            raise ValueError
    except ValueError:
        return await message.answer(
            "❌ Введите корректное количество, например: <code>5</code>",
            parse_mode="HTML",
        )

    data = await state.get_data()
    if not data.get("selected_product") or not data.get("order_id"):
        await state.clear()
        return await message.answer(
            "⚠️ Сессия сброшена (возможно, бот перезагружался). "
            "Откройте заказ снова: /myorders",
        )
    product = data["selected_product"]
    order_id = data["order_id"]
    order = await adb.get_order(order_id)
    currency = (order or {}).get("currency") or _BASE_CURRENCY
    await state.update_data(quantity=qty)
    await state.set_state(OrderState.entering_price)

    await message.answer(
        f"✅ Количество: <b>{qty} {product['unit']}</b>\n\n"
        f"💰 Введите <b>цену за {product['unit']}</b> в <b>{currency}</b>.\n"
        f"<i>(валюту заказа можно сменить кнопкой «💱 Валюта»)</i>\n"
        f"Например: <code>150</code> или <code>49.99</code>.\n"
        f"Если цена ещё не известна — введите <code>0</code>.",
        parse_mode="HTML",
    )


@router.message(OrderState.entering_price)
async def process_price(message: Message, state: FSMContext):
    text = (message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        price = float(text)
        if price < 0:
            raise ValueError
    except ValueError:
        return await message.answer(
            "❌ Введите корректную цену, например: <code>150</code> или <code>49.99</code>",
            parse_mode="HTML",
        )

    data = await state.get_data()
    if not data.get("order_id") or not data.get("selected_product"):
        await state.clear()
        return await message.answer(
            "⚠️ Сессия сброшена (возможно, бот перезагружался). "
            "Откройте заказ снова: /myorders",
        )
    order_id = data["order_id"]
    product = data["selected_product"]
    qty = data["quantity"]

    await adb.add_order_item(
        order_id=order_id,
        product_name=product["name"],
        product_href=product["href"],
        quantity=qty,
        unit=product["unit"],
        price=price,
    )

    await state.clear()

    # Показываем текущий заказ
    order, items = await asyncio.gather(
        adb.get_order(order_id),
        adb.get_order_items(order_id),
    )
    subtotal = qty * price

    currency = (order or {}).get("currency") or _BASE_CURRENCY
    full_name = message.from_user.full_name or str(message.from_user.id)
    role = await adb.get_role(message.from_user.id)
    await adb.add_audit_log(
        message.from_user.id, full_name, role,
        "order_item_added", f"Заказ #{order_id}: {product['name']} × {qty} @ {price}",
    )
    await message.answer(
        f"✅ Добавлено: <b>{_esc(product['name'])}</b>\n"
        f"   {qty} {product['unit']} × {_cur(price, currency)} = <b>{_cur(subtotal, currency)}</b>\n\n"
        + format_order(order, items),
        parse_mode="HTML",
        reply_markup=order_actions_keyboard(order_id, "draft", True),
    )


# ─── Callback: выбор валюты заказа ───────────────────────────────────────────


@router.callback_query(F.data.startswith("ord_cur:"))
async def cb_choose_currency(call: CallbackQuery):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    order_id = int(call.data.split(":")[1])
    order = await adb.get_order(order_id)
    if not order or order["user_id"] != call.from_user.id:
        return await call.message.answer("⛔ Это не ваш заказ.")
    current = order.get("currency") or _BASE_CURRENCY
    await call.message.answer(
        f"💱 Текущая валюта заказа: <b>{current}</b>\n\nВыберите новую:",
        parse_mode="HTML",
        reply_markup=currency_picker_keyboard(order_id),
    )


@router.callback_query(F.data.startswith("ord_cur_set:"))
async def cb_set_currency(call: CallbackQuery):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    parts = call.data.split(":")
    order_id = int(parts[1])
    currency = parts[2]
    if currency not in ALLOWED_CURRENCIES:
        return await call.answer("Недопустимая валюта", show_alert=True)
    order = await adb.get_order(order_id)
    if not order or order["user_id"] != call.from_user.id:
        return await call.answer("Нет доступа", show_alert=True)
    await adb.update_order_currency(order_id, currency)
    await call.answer(f"✅ Валюта: {currency}")
    # Перерисовываем карточку заказа
    order, items = await asyncio.gather(
        adb.get_order(order_id),
        adb.get_order_items(order_id),
    )
    await call.message.answer(
        format_order(order, items),
        parse_mode="HTML",
        reply_markup=order_actions_keyboard(order_id, "draft", True),
    )


# ─── Callback: выбор клиента ─────────────────────────────────────────────────


@router.callback_query(F.data.startswith("ord_agent:"))
async def cb_choose_agent(call: CallbackQuery, state: FSMContext):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    order_id = int(call.data.split(":")[1])
    await call.message.answer("⏳ Загружаю список клиентов…")

    try:
        # Сначала пробуем snapshot — мгновенно, без удара по МойСклад API
        from services import snapshot
        snap_rows = snapshot.get_counterparties(limit=50)
        if snap_rows:
            agents = [
                {
                    "id": r["ms_id"],
                    "name": r.get("name", "—"),
                    "phone": r.get("phone", ""),
                    "meta": {"href": ""},  # пустой, для совместимости с extract_href
                }
                for r in snap_rows
            ]
        else:
            data = await ms_get(
                "entity/counterparty",
                params={"limit": 50, "order": "name"},
            )
            agents = data.get("rows", [])
        if not agents:
            return await call.message.answer("❌ Клиенты не найдены.")

        kb = InlineKeyboardBuilder()
        for i, agent in enumerate(agents[:20]):
            name = agent.get("name", "—")[:30]
            kb.button(text=f"👤 {name}", callback_data=f"agent_pick:{order_id}:{i}")
        kb.button(text="❌ Отмена", callback_data=f"ord_view:{order_id}")
        kb.adjust(1)

        # Сохраняем в state — id берём напрямую из dict (snapshot) или
        # извлекаем из meta.href (live API fallback).
        await state.update_data(
            order_id=order_id,
            agents=[{
                "id": a.get("id") or extract_id_from_href(extract_href(a)),
                "name": a.get("name", "—"),
            } for a in agents[:20]]
        )
        await state.set_state(OrderState.choosing_agent)

        await call.message.answer(
            f"👤 Выберите клиента ({len(agents)} найдено):",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await call.message.answer(user_safe_error(e, "choose_agent"))


@router.callback_query(F.data.startswith("agent_pick:"), OrderState.choosing_agent)
async def cb_agent_pick(call: CallbackQuery, state: FSMContext):
    await call.answer()
    parts = call.data.split(":")
    order_id = int(parts[1])
    idx = int(parts[2])

    data = await state.get_data()
    agents = data.get("agents", [])

    if idx >= len(agents):
        return await call.message.answer("❌ Клиент не найден.")

    agent = agents[idx]
    await adb.update_order_agent(order_id, agent["id"], agent["name"])
    await state.clear()

    order, items = await asyncio.gather(
        adb.get_order(order_id),
        adb.get_order_items(order_id),
    )

    await call.message.answer(
        f"✅ Клиент выбран: <b>{agent['name']}</b>\n\n" + format_order(order, items),
        parse_mode="HTML",
        reply_markup=order_actions_keyboard(order_id, "draft", True),
    )


# ─── Callback: отправка заявки ────────────────────────────────────────────────


@router.callback_query(F.data.startswith("ord_submit:"))
async def cb_submit_order(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    order_id = int(call.data.split(":")[1])
    order = await adb.get_order(order_id)

    if not order:
        return await call.message.answer("❌ Заказ не найден.")
    if order["user_id"] != call.from_user.id:
        return await call.message.answer("⛔ Это не ваш заказ.")
    if order["status"] != "draft":
        return await call.message.answer("⚠️ Заказ уже отправлен.")

    items = await adb.get_order_items(order_id)
    if not items:
        return await call.message.answer(
            "❌ Нельзя отправить пустой заказ.\n"
            "Сначала добавьте товары."
        )

    if not order.get("agent_name"):
        return await call.message.answer(
            "❌ Нельзя отправить заявку без клиента.\n"
            "Нажмите «👤 Выбрать клиента»."
        )

    # Создаём заявку
    user = call.from_user
    full_name = user.full_name or user.username or str(user.id)
    req_id, role = await asyncio.gather(
        adb.create_shipment_request(order_id, user.id, full_name),
        adb.get_role(user.id),
    )
    await asyncio.gather(
        adb.update_order_status(order_id, "pending"),
        adb.add_audit_log(
            user.id, full_name, role,
            "shipment_request_sent", f"Отправлена заявка #{req_id} (заказ #{order_id})",
        ),
    )

    await call.message.answer(
        f"{DIV}\n"
        f"✅ <b>Заявка #{req_id} отправлена!</b>\n\n"
        f"Ожидайте подтверждения от руководителя.\n"
        f"Вы получите уведомление.",
        parse_mode="HTML",
    )

    # Уведомляем руководителей
    from services.notify import notify_shipment_request
    notify_text = format_request_notify(order, items, req_id)
    await notify_shipment_request(
        bot, notify_text, req_id,
        approve_keyboard=request_approve_keyboard(req_id),
    )


# ─── Callback: удаление заказа ────────────────────────────────────────────────


@router.callback_query(F.data.startswith("ord_delete:"))
async def cb_delete_order(call: CallbackQuery):
    await call.answer()
    order_id = int(call.data.split(":")[1])
    order = await adb.get_order(order_id)

    if not order or order["user_id"] != call.from_user.id:
        return await call.message.answer("⛔ Нет доступа.")
    if order["status"] != "draft":
        return await call.message.answer("❌ Нельзя удалить отправленный заказ.")

    full_name = call.from_user.full_name or str(call.from_user.id)
    role, _ = await asyncio.gather(
        adb.get_role(call.from_user.id),
        adb.update_order_status(order_id, "rejected"),
    )
    await adb.add_audit_log(
        call.from_user.id, full_name, role,
        "order_deleted", f"Удалён черновик заказа #{order_id}",
    )

    await call.message.answer(f"🗑 Заказ #{order_id} удалён.")


# ─── Callback: просмотр заявки (руководитель) ─────────────────────────────────


@router.callback_query(F.data.startswith("req_view:"))
async def cb_view_request(call: CallbackQuery):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    req_id = int(call.data.split(":")[1])
    req = await adb.get_shipment_request(req_id)
    if not req:
        return await call.message.answer("❌ Заявка не найдена.")

    order = await adb.get_order(req["order_id"])
    if not order:
        return await call.message.answer("❌ Заказ не найден.")

    items = await adb.get_order_items(req["order_id"])
    txt = format_request_notify(order, items, req_id)

    if req["status"] == "pending":
        await call.message.answer(txt, parse_mode="HTML",
                                  reply_markup=request_approve_keyboard(req_id))
    else:
        status_str = "✅ Одобрено" if req["status"] == "approved" else "❌ Отклонено"
        await call.message.answer(
            txt + f"\n\n{status_str} — {req.get('approved_by_name', '—')}",
            parse_mode="HTML",
        )


# ─── Callback: одобрение/отклонение заявки ────────────────────────────────────


@router.callback_query(F.data.startswith("req_ok:"))
async def cb_approve_request(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    req_id = int(call.data.split(":")[1])
    req = await adb.get_shipment_request(req_id)

    if not req:
        return await call.answer("❌ Заявка не найдена", show_alert=True)
    if req["status"] != "pending":
        return await call.answer("⚠️ Заявка уже обработана", show_alert=True)

    boss_name = call.from_user.full_name or str(call.from_user.id)
    # Атомарный UPDATE ... WHERE status='pending' — защита от race condition
    # когда два босса одновременно жмут «Одобрить». Только один из них
    # получит rowcount==1, остальные — False, и мы прервёмся.
    ok = await adb.approve_shipment_request(req_id, call.from_user.id, boss_name)
    if not ok:
        return await call.answer(
            "⚠️ Заявка уже обработана другим пользователем", show_alert=True
        )
    await call.answer("✅ Заявка одобрена")

    from utils.helpers import local_now
    now = local_now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text + f"\n\n{DIV}\n✅ <b>Одобрено</b>  <code>{now}</code>  — {boss_name}",
        parse_mode="HTML",
    )

    # Push отгрузки в МойСклад. Делаем это до уведомления менеджера, чтобы
    # включить ссылку на demand в сообщение. Если МойСклад не отвечает —
    # заявка всё равно остаётся одобренной в боте, ошибку логируем + шлём
    # боссу для разбора.
    order, boss_role = await asyncio.gather(
        adb.get_order(req["order_id"]),
        adb.get_role(call.from_user.id),
    )
    items = await adb.get_order_items(req["order_id"]) if order else []
    manager_name = (order or {}).get("full_name") or req.get("full_name") or "—"
    manager_user_id = (order or {}).get("user_id") or req.get("user_id")

    # Цепочка в МойСклад:
    #   1) customerorder — для PDF печатной формы (через export endpoint)
    #   2) demand линкованный с customerorder — чтобы СРАЗУ списать остатки
    #      и не зависеть от того, не забыл ли бухгалтер вручную создать
    #      отгрузку. Без этого был риск разрыва между snapshot бота
    #      (остатки старые) и реальностью (товар уже зарезервирован).
    # Backend URL'ы в чат не отправляем — только PDF файлом.
    from services.ms_demand import (
        is_ready as ms_ready,
        create_demand_from_request,
    )
    from services.ms_customerorder import create_customerorder_from_request
    demand_line = ""
    pdf_to_send: tuple[bytes, str] | None = None
    if order and items and ms_ready():
        co_result = await create_customerorder_from_request(
            order, items, manager_name, telegram_user_id=manager_user_id,
        )
        if co_result.get("ok"):
            from services.database import (
                set_order_ms_customerorder_id, set_order_ms_demand_id,
            )
            co_id = co_result.get("customerorder_id")
            # Audit СРАЗУ после успешного POST в МойСклад — до того
            # как мы пытаемся сохранить co_id в БД. SECURITY.md H6:
            # если бот упадёт между POST и UPDATE (OOM/kill/network),
            # без этого audit-записи orphan-CO в МойСклад будет
            # никак не найти. С audit-записью админ ищет в логах
            # `ms_co_created` и привязывает вручную.
            if co_id:
                await asyncio.gather(
                    adb.add_audit_log(
                        call.from_user.id, boss_name, boss_role,
                        "ms_co_created",
                        f"Заявка #{req_id} → customerorder {co_id} (до db-write)",
                    ),
                    adb.set_order_ms_customerorder_id(order["id"], co_id),
                )

            # PDF берём от customerorder — он содержит читаемую форму заказа
            if co_result.get("pdf_bytes"):
                pdf_to_send = (
                    co_result["pdf_bytes"],
                    co_result.get("pdf_filename") or f"order_{order['id']}.pdf",
                )

            # Сразу создаём demand, линкованный с customerorder.
            # Если demand упадёт — customerorder остаётся, бухгалтер
            # должен будет вручную создать отгрузку. Это худший случай,
            # но в чат явно об этом скажем.
            from services.moysklad import MS_BASE
            co_href = f"{MS_BASE}/entity/customerorder/{co_id}" if co_id else None
            demand_result = await create_demand_from_request(
                order, items, manager_name,
                telegram_user_id=manager_user_id,
                customerorder_href=co_href,
            )
            if demand_result.get("ok"):
                demand_id = demand_result.get("demand_id")
                # Audit ДО db-write (H6) — см. комментарий выше для CO.
                if demand_id:
                    await asyncio.gather(
                        adb.add_audit_log(
                            call.from_user.id, boss_name, boss_role,
                            "ms_demand_created",
                            f"Заявка #{req_id} → demand {demand_id} (до db-write)",
                        ),
                        adb.set_order_ms_demand_id(order["id"], demand_id),
                    )
                demand_line = (
                    f"\n📄 Заказ покупателя создан в МойСклад"
                    f"\n📦 Отгрузка проведена, остатки списаны"
                )
                if pdf_to_send:
                    demand_line += " — печатная форма ниже 👇"
                await adb.add_audit_log(
                    call.from_user.id, boss_name, boss_role,
                    "ms_co_and_demand_created",
                    f"Заявка #{req_id} → customerorder {co_id} + demand {demand_id}",
                )
            else:
                reason = demand_result.get("reason", "неизвестная ошибка")
                logger.warning(
                    "Customerorder создан, но demand упал для #%s: %s",
                    req_id, reason,
                )
                demand_line = (
                    f"\n📄 Заказ покупателя создан в МойСклад"
                    f"\n⚠️ <b>Отгрузка НЕ создана автоматически:</b>\n"
                    f"<code>{_esc(reason[:200])}</code>\n"
                    f"Остатки не списались — создайте demand вручную в МойСклад "
                    f"(или из карточки этого заказа покупателя)."
                )
                if pdf_to_send:
                    demand_line += "\nПечатная форма ниже 👇"
                await adb.add_audit_log(
                    call.from_user.id, boss_name, boss_role,
                    "ms_demand_failed",
                    f"Заявка #{req_id} → CO {co_id} ok, demand fail: {reason[:200]}",
                )
                # Личное уведомление боссу для разбора
                try:
                    await bot.send_message(
                        call.from_user.id,
                        f"⚠️ MS demand fail для заявки #{req_id} "
                        f"(customerorder {co_id[:8]} создан):\n"
                        f"<code>{_esc(reason[:500])}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        else:
            reason = co_result.get("reason", "неизвестная ошибка")
            logger.warning(
                "Не удалось создать customerorder для заявки #%s: %s",
                req_id, reason,
            )
            demand_line = (
                f"\n⚠️ <b>Не удалось создать заказ в МойСклад:</b>\n"
                f"<code>{_esc(reason[:300])}</code>\n"
                f"Заявка в боте одобрена — заказ нужно завести вручную."
            )
            try:
                await bot.send_message(
                    call.from_user.id,
                    f"⚠️ MS customerorder fail для заявки #{req_id}:\n"
                    f"<code>{_esc(reason[:500])}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    elif not ms_ready():
        logger.info(
            "MS context не готов — не создаём документы для заявки #%s", req_id,
        )

    # Уведомляем менеджера
    from services.notify import notify_order_approved
    await notify_order_approved(bot, req["user_id"], req_id, boss_name, now, demand_line)

    # ─── Шлём PDF печатной формы заказчику и руководителю ──────────
    # Файл вместо ссылки — никаких URL на бэкенд МойСклад. PDF
    # содержит имя клиента, состав заказа, цены — то, что нужно
    # для распечатки/отправки клиенту.
    if pdf_to_send:
        from aiogram.types import BufferedInputFile
        pdf_bytes, pdf_name = pdf_to_send
        file = BufferedInputFile(pdf_bytes, filename=pdf_name)
        caption = f"📄 Печатная форма — заявка #{req_id}"
        # Менеджеру (создателю)
        try:
            await bot.send_document(
                chat_id=req["user_id"], document=file, caption=caption,
            )
        except Exception:
            logger.exception("Не удалось отправить PDF менеджеру")
        # И боссу-апруверу — он же может захотеть подшить копию
        if call.from_user.id != req["user_id"]:
            try:
                file2 = BufferedInputFile(pdf_bytes, filename=pdf_name)
                await bot.send_document(
                    chat_id=call.from_user.id, document=file2, caption=caption,
                )
            except Exception:
                logger.exception("Не удалось отправить PDF боссу")

    # ─── Для paid-заказов автоматически создаём payment-pending ────
    # Раньше «оплачено сразу» проходило мимо подтверждения боссом —
    # система верила менеджеру на слово. Теперь даже paid-заказы
    # требуют второго клика «Принято» от босса, чтобы зафиксировать
    # факт получения денег в кассу и при этом синхронизировать платёж
    # с МойСклад. Credit-заказы НЕ трогаем — у них своя двухступенчатая
    # логика через mark_paid + confirm.
    if order and (order.get("payment_type") or "paid") == "paid":
        total = sum(
            float(it.get("quantity", 0)) * float(it.get("price", 0) or 0)
            for it in items
        )
        if total > 0.01:
            currency = order.get("currency") or "USD"
            try:
                existing = [
                    p for p in await adb.get_payments_for_order(order["id"])
                    if p["status"] in ("pending", "confirmed")
                ]
                if not existing:
                    payment_id = await adb.add_payment(
                        user_id=order["user_id"],
                        username="",
                        full_name=manager_name,
                        amount=total,
                        currency=currency,
                        comment=f"Оплата по заказу #{order['id']} (отгрузка одобрена)",
                        order_id=order["id"],
                    )
                    # Шлём боссам тот же confirm-push, что используется
                    # для credit-заказов. Реюз кода — единая точка
                    # сборки сообщения и кнопок Принять/Отклонить.
                    from handlers.debts import _push_payment_confirmation
                    await _push_payment_confirmation(
                        bot, order["id"], manager_name, payment_id,
                    )
            except Exception:
                logger.exception(
                    "Не удалось создать auto-payment для paid-заказа #%s", order["id"],
                )


@router.callback_query(F.data.startswith("req_no:"))
async def cb_reject_request(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    req_id = int(call.data.split(":")[1])
    req = await adb.get_shipment_request(req_id)

    if not req:
        return await call.answer("❌ Заявка не найдена", show_alert=True)
    if req["status"] != "pending":
        return await call.answer("⚠️ Заявка уже обработана", show_alert=True)

    boss_name = call.from_user.full_name or str(call.from_user.id)
    ok = await adb.reject_shipment_request(req_id, call.from_user.id, boss_name)
    if not ok:
        return await call.answer(
            "⚠️ Заявка уже обработана другим пользователем", show_alert=True
        )
    await call.answer("❌ Заявка отклонена")

    from utils.helpers import local_now
    now = local_now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{now}</code>  — {boss_name}",
        parse_mode="HTML",
    )

    from services.notify import notify_order_rejected
    await notify_order_rejected(bot, req["user_id"], req_id, boss_name, now)
