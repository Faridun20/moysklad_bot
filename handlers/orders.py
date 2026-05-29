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
import logging

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
from services import async_db as adb
from services.moysklad import get_all_stock, get_categories, ms_get
from utils.helpers import extract_id_from_href, extract_href, safe_get, user_safe_error
from utils.formatters import DIV, DIV2

logger = logging.getLogger(__name__)
router = Router()


# ─── FSM состояния ────────────────────────────────────────────────────────────


class OrderState(StatesGroup):
    choosing_product = State()  # выбор товара
    entering_qty_price = State()  # ввод количества и цены одной строкой
    choosing_agent = State()  # выбор клиента


def _parse_qty_price(text: str) -> tuple[float | None, float | None]:
    """Парсит «5 150» / «5x150» / «5» → (qty, price).

    qty=None → количество некорректно. price=None (при валидном qty) → цена
    указана, но некорректна. Если цена опущена — price=0.0 (цена неизвестна).
    Разделители кол-ва и цены: пробел, x/х/*/×."""
    raw = (text or "").strip().lower().replace(",", ".")
    for sep in ("х", "x", "*", "×"):
        raw = raw.replace(sep, " ")
    parts = raw.split()
    if not parts:
        return None, None
    try:
        qty = float(parts[0])
        if qty <= 0:
            return None, None
    except ValueError:
        return None, None
    if len(parts) == 1:
        return qty, 0.0
    try:
        price = float(parts[1])
        if price < 0:
            raise ValueError
    except ValueError:
        return qty, None
    return qty, price


# ─── Форматирование ───────────────────────────────────────────────────────────


STATUS_EMOJI = {
    "draft": "📝",
    "pending": "⏳",
    "approved": "✅",
    "rejected": "❌",
    "shipped": "🚚",
}

STATUS_NAME = {
    "draft": "Черновик",
    "pending": "На рассмотрении",
    "approved": "Одобрено",
    "rejected": "Отклонено",
    "shipped": "Отгружено",
}


def _line_total(item: dict) -> float:
    return float(item.get("quantity", 0)) * float(item.get("price", 0) or 0)


def _fmt_num(n: float) -> str:
    """Без бесконечных нулей: 150.0 → 150, 49.99 → 49.99."""
    from services import money

    return money.format_cents(money.to_cents(n or 0), decimals=2, grouping=False, trim=True)


def format_order(order: dict, items: list[dict]) -> str:
    status_emoji = STATUS_EMOJI.get(order["status"], "📋")
    status_name = STATUS_NAME.get(order["status"], order["status"])
    agent_str = (
        f"\n👤 Клиент: <b>{_esc(order['agent_name'])}</b>" if order.get("agent_name") else ""
    )
    comment_str = f"\n📝 {_esc(order['comment'])}" if order.get("comment") else ""
    currency = order.get("currency") or _BASE_CURRENCY

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
            lines.append(f"  {i + 1}. <b>{_esc(item['product_name'])}</b>\n{price_str}{note_str}")
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
            lines.append(f"  • {name}: {_fmt_num(qty)} {unit} <i>(цена не указана)</i>")
    items_text = "\n".join(lines)
    grand_total = sum(_line_total(it) for it in items)
    if len(items) > 10:
        items_text += f"\n  ...и ещё {len(items) - 10} поз."

    agent_str = (
        f"\n👤 Клиент: <b>{_esc(order['agent_name'])}</b>" if order.get("agent_name") else ""
    )
    comment_str = f"\n📝 {_esc(order['comment'])}" if order.get("comment") else ""
    total_str = f"\n\n<b>💰 Итого: {_cur(grand_total, currency)}</b>" if grand_total > 0 else ""

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
        kb.button(text="➕ Добавить товар", callback_data=f"ord_add:{order_id}")
        kb.button(text="👤 Выбрать клиента", callback_data=f"ord_agent:{order_id}")
        kb.button(text="💱 Валюта", callback_data=f"ord_cur:{order_id}")
        kb.button(text="🚀 Отправить заявку", callback_data=f"ord_submit:{order_id}")
        kb.button(text="🗑 Удалить заказ", callback_data=f"ord_delete:{order_id}")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def delete_confirm_keyboard(order_id: int):
    """Шаг подтверждения удаления черновика — защита от случайного тапа."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"ord_delete_yes:{order_id}")
    kb.button(text="↩️ Нет, оставить", callback_data=f"ord_view:{order_id}")
    kb.adjust(2)
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
    kb.button(text="✅ Одобрить", callback_data=f"req_ok:{req_id}")
    kb.button(text="❌ Отклонить", callback_data=f"req_no:{req_id}")
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
    kb.button(text="🏠 Меню", callback_data="menu")
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
        f"{DIV}\n⏳ <b>Заявки на отгрузку</b>\n<code>Ожидают рассмотрения: {len(requests)}</code>",
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
                r for r in all_stock if extract_id_from_href(extract_href(r, "folder")) == cat_id
            ]
        else:
            filtered = all_stock

        if not filtered:
            return await call.message.answer("📦 Нет товаров в этой категории.")

        # Показываем по 20 товаров
        head = filtered[:20]
        # Минимальные цены продажи (заданы руководством) — батчем, чтобы
        # показать менеджеру прямо при выборе товара, без захода в каталог.
        ms_ids = [extract_id_from_href(extract_href(r)) for r in head]
        prices = await adb.get_product_prices_by_ids([m for m in ms_ids if m])

        kb = InlineKeyboardBuilder()
        products = []
        for i, r in enumerate(head):
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = safe_get(r, "uom", "name", default="шт")
            mid = ms_ids[i]
            sale_min = (prices.get(mid) or {}).get("sale_price") if mid else None
            # Кодируем href кратко через индекс
            kb.button(
                text=f"{name} ({stock} {unit})",
                callback_data=f"prod_pick:{order_id}:{i}",
            )
            products.append(
                {
                    "name": name,
                    "href": extract_href(r),
                    "ms_id": mid or "",
                    "unit": unit,
                    "stock": stock,
                    "sale_min": sale_min,
                }
            )

        kb.button(text="◀️ Назад", callback_data=f"ord_add:{order_id}")
        kb.button(text="❌ Отмена", callback_data=f"ord_view:{order_id}")
        kb.adjust(1)

        # Сохраняем список товаров (с мин. ценой) в state
        await state.update_data(order_id=order_id, products=products)
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
    idx = int(parts[2])

    data = await state.get_data()
    products = data.get("products", [])

    if idx >= len(products):
        return await call.message.answer("❌ Товар не найден.")

    product = products[idx]
    await state.update_data(selected_product=product)
    await state.set_state(OrderState.entering_qty_price)

    order = await adb.get_order(data["order_id"]) if data.get("order_id") else None
    currency = (order or {}).get("currency") or _BASE_CURRENCY

    sale_min = product.get("sale_min")
    if sale_min:
        price_line = f"💰 Мин. цена продажи: <b>{_cur(sale_min, currency)}</b>\n"
        only_qty_hint = "Только количество — <code>5</code> — возьму минимальную цену."
    else:
        price_line = ""
        only_qty_hint = "Только количество — <code>5</code> — добавит позицию без цены."

    await call.message.answer(
        f"✅ Выбран: <b>{_esc(product['name'])}</b>\n"
        f"На складе: <code>{product['stock']} {product['unit']}</code>\n"
        f"{price_line}\n"
        f"Введите <b>количество и цену</b> за {product['unit']} (в {currency}) одной строкой:\n"
        f"<code>5 150</code>  ·  можно <code>5x150</code>\n\n"
        f"<i>{only_qty_hint}\n"
        f"Валюту заказа можно сменить кнопкой «💱 Валюта».</i>",
        parse_mode="HTML",
    )


@router.message(OrderState.entering_qty_price)
async def process_qty_price(message: Message, state: FSMContext):
    qty, price = _parse_qty_price(message.text)
    if qty is None:
        return await message.answer(
            "❌ Начните с количества. Например: <code>5</code> или <code>5 150</code>",
            parse_mode="HTML",
        )
    if price is None:
        return await message.answer(
            "❌ Цена некорректна. Например: <code>5 150</code> или <code>5 49.99</code>",
            parse_mode="HTML",
        )

    data = await state.get_data()
    if not data.get("order_id") or not data.get("selected_product"):
        await state.clear()
        return await message.answer(
            "⚠️ Сессия сброшена (возможно, бот перезагружался). Откройте заказ снова: /myorders",
        )
    order_id = data["order_id"]
    product = data["selected_product"]

    # Минимальная цена продажи (если задана руководством): пустую цену
    # подставляем минимумом, ниже минимума — не даём (как в WebApp). Менеджеру
    # не нужно идти в каталог сверять цену — она видна ещё на шаге выбора.
    order = await adb.get_order(order_id)
    currency = (order or {}).get("currency") or _BASE_CURRENCY
    sale_min = product.get("sale_min")
    if sale_min:
        if price == 0:
            price = float(sale_min)
        elif price < float(sale_min):
            return await message.answer(
                f"❌ Цена ниже минимальной: <b>{_cur(sale_min, currency)}</b>.\n"
                f"Введите цену ≥ минимума, либо только количество — подставлю минимум.",
                parse_mode="HTML",
            )

    await adb.add_order_item(
        order_id=order_id,
        product_name=product["name"],
        product_href=product["href"],
        quantity=qty,
        unit=product["unit"],
        price=price,
    )

    await state.clear()

    # Показываем текущий заказ (строка ордера не меняется при добавлении
    # позиции — переиспользуем уже загруженный order, тянем только items).
    items = await adb.get_order_items(order_id)
    subtotal = qty * price
    full_name = message.from_user.full_name or str(message.from_user.id)
    role = await adb.get_role(message.from_user.id)
    await adb.add_audit_log(
        message.from_user.id,
        full_name,
        role,
        "order_item_added",
        f"Заказ #{order_id}: {product['name']} × {qty} @ {price}",
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
            agents=[
                {
                    "id": a.get("id") or extract_id_from_href(extract_href(a)),
                    "name": a.get("name", "—"),
                }
                for a in agents[:20]
            ],
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
            "❌ Нельзя отправить пустой заказ.\nСначала добавьте товары."
        )

    if not order.get("agent_name"):
        return await call.message.answer(
            "❌ Нельзя отправить заявку без клиента.\nНажмите «👤 Выбрать клиента»."
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
            user.id,
            full_name,
            role,
            "shipment_request_sent",
            f"Отправлена заявка #{req_id} (заказ #{order_id})",
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
        bot,
        notify_text,
        req_id,
        approve_keyboard=request_approve_keyboard(req_id),
    )


# ─── Callback: удаление заказа ────────────────────────────────────────────────


@router.callback_query(F.data.startswith("ord_delete:"))
async def cb_delete_order(call: CallbackQuery):
    """Первый шаг — спрашиваем подтверждение, ничего не удаляя."""
    await call.answer()
    order_id = int(call.data.split(":")[1])
    order = await adb.get_order(order_id)

    if not order or order["user_id"] != call.from_user.id:
        return await call.message.answer("⛔ Нет доступа.")
    if order["status"] != "draft":
        return await call.message.answer("❌ Нельзя удалить отправленный заказ.")

    await call.message.answer(
        f"🗑 Удалить черновик заказа #{order_id}? Это действие необратимо.",
        reply_markup=delete_confirm_keyboard(order_id),
    )


@router.callback_query(F.data.startswith("ord_delete_yes:"))
async def cb_delete_order_yes(call: CallbackQuery):
    """Второй шаг — фактическое удаление после подтверждения."""
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
        call.from_user.id,
        full_name,
        role,
        "order_deleted",
        f"Удалён черновик заказа #{order_id}",
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
        await call.message.answer(
            txt, parse_mode="HTML", reply_markup=request_approve_keyboard(req_id)
        )
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
    boss_name = call.from_user.full_name or str(call.from_user.id)

    # Вся логика (DB, МойСклад, уведомления, PDF, авто-payment) — в сервисе.
    # Здесь только Telegram-UI: ответ на callback + редактирование сообщения.
    from services.order_workflow import approve_shipment_request

    result = await approve_shipment_request(req_id, call.from_user.id, boss_name, bot)

    if not result["ok"]:
        return await call.answer(f"⚠️ {result['error']}", show_alert=True)

    await call.answer("✅ Заявка одобрена")
    await call.message.edit_text(
        call.message.text
        + f"\n\n{DIV}\n✅ <b>Одобрено</b>  <code>{result['now']}</code>  — {boss_name}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("req_no:"))
async def cb_reject_request(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    req_id = int(call.data.split(":")[1])
    boss_name = call.from_user.full_name or str(call.from_user.id)

    from services.order_workflow import reject_shipment_request

    result = await reject_shipment_request(req_id, call.from_user.id, boss_name, bot)

    if not result["ok"]:
        return await call.answer(f"⚠️ {result['error']}", show_alert=True)

    await call.answer("❌ Заявка отклонена")
    await call.message.edit_text(
        call.message.text
        + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{result['now']}</code>  — {boss_name}",
        parse_mode="HTML",
    )
