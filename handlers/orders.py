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

import logging
from datetime import datetime

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.roles import can_create_orders, is_boss
from services.database import (
    create_order, get_order, get_orders_by_ids, get_user_orders, get_all_orders,
    update_order_status, update_order_agent,
    add_order_item, get_order_items, remove_order_item,
    create_shipment_request, get_shipment_request,
    get_pending_requests, approve_shipment_request, reject_shipment_request,
    get_role, add_audit_log, get_all_users,
)
from services.moysklad import get_all_stock, get_categories, ms_get
from services.notifier import get_notify_recipients, send_to_recipients
from utils.helpers import extract_id_from_href, extract_href, safe_get
from utils.formatters import DIV, DIV2

logger = logging.getLogger(__name__)
router = Router()


# ─── FSM состояния ────────────────────────────────────────────────────────────


class OrderState(StatesGroup):
    choosing_product  = State()  # выбор товара
    entering_quantity = State()  # ввод количества
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


def format_order(order: dict, items: list[dict]) -> str:
    status_emoji = STATUS_EMOJI.get(order["status"], "📋")
    status_name  = STATUS_NAME.get(order["status"], order["status"])
    agent_str    = f"\n👤 Клиент: <b>{order['agent_name']}</b>" if order.get("agent_name") else ""
    comment_str  = f"\n📝 {order['comment']}" if order.get("comment") else ""

    lines = [
        DIV,
        f"{status_emoji} <b>Заказ #{order['id']}</b>   <code>{status_name}</code>",
        f"<code>{order['created_at'][:16]}</code>",
        f"👤 Менеджер: {order['full_name']}{agent_str}{comment_str}",
        "",
    ]

    if items:
        lines.append(f"<b>📦 Товары ({len(items)}):</b>")
        lines.append(DIV2)
        total_items = len(items)
        for i, item in enumerate(items[:10]):
            note_str = f"  <i>{item['note']}</i>" if item.get("note") else ""
            lines.append(
                f"  {i+1}. <b>{item['product_name']}</b>\n"
                f"     <code>{item['quantity']} {item['unit']}</code>{note_str}"
            )
        if total_items > 10:
            lines.append(f"  <i>...и ещё {total_items - 10} позиций</i>")
    else:
        lines.append("<i>Товары не добавлены</i>")

    return "\n".join(lines)


def format_request_notify(order: dict, items: list[dict], req_id: int) -> str:
    items_text = "\n".join(
        f"  • {it['product_name']}: {it['quantity']} {it['unit']}"
        for it in items[:10]
    )
    if len(items) > 10:
        items_text += f"\n  ...и ещё {len(items) - 10} поз."
    agent_str = f"\n👤 Клиент: <b>{order['agent_name']}</b>" if order.get("agent_name") else ""
    comment_str = f"\n📝 {order['comment']}" if order.get("comment") else ""

    return (
        f"{DIV}\n"
        f"🔔 <b>Новая заявка на отгрузку #{req_id}</b>\n"
        f"\n"
        f"👨‍💼 Менеджер: <b>{order['full_name']}</b>{agent_str}{comment_str}\n"
        f"\n"
        f"<b>📦 Товары:</b>\n{items_text}"
    )


# ─── Клавиатуры ──────────────────────────────────────────────────────────────


def order_actions_keyboard(order_id: int, status: str, is_owner: bool):
    kb = InlineKeyboardBuilder()
    if status == "draft" and is_owner:
        kb.button(text="➕ Добавить товар",  callback_data=f"ord_add:{order_id}")
        kb.button(text="👤 Выбрать клиента", callback_data=f"ord_agent:{order_id}")
        kb.button(text="🚀 Отправить заявку", callback_data=f"ord_submit:{order_id}")
        kb.button(text="🗑 Удалить заказ",   callback_data=f"ord_delete:{order_id}")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
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


def pending_requests_keyboard(requests: list[dict]):
    kb = InlineKeyboardBuilder()
    head = requests[:10]
    orders_by_id = get_orders_by_ids([r["order_id"] for r in head])
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
    order_id = create_order(user.id, full_name)

    add_audit_log(
        user.id, full_name, get_role(user.id),
        "order_created", f"Создан заказ #{order_id}",
    )

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

    orders = get_user_orders(message.from_user.id)
    if not orders:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Создать заказ", callback_data="ord_new")
        kb.button(text="🏠 Меню",          callback_data="menu")
        kb.adjust(1)
        return await message.answer(
            "📋 У вас пока нет заказов.",
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

    requests = get_pending_requests()
    if not requests:
        return await message.answer(
            f"{DIV}\n⏳ <b>Заявки на отгрузку</b>\n\n<i>Нет новых заявок</i>",
            parse_mode="HTML",
        )

    await message.answer(
        f"{DIV}\n"
        f"⏳ <b>Заявки на отгрузку</b>\n"
        f"<code>Ожидают рассмотрения: {len(requests)}</code>",
        parse_mode="HTML",
        reply_markup=pending_requests_keyboard(requests),
    )


# ─── Callback: просмотр заказа ────────────────────────────────────────────────


@router.callback_query(F.data == "ord_new")
async def cb_new_order(call: CallbackQuery):
    if not can_create_orders(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    user = call.from_user
    full_name = user.full_name or user.username or str(user.id)
    order_id = create_order(user.id, full_name)

    add_audit_log(
        user.id, full_name, get_role(user.id),
        "order_created", f"Создан заказ #{order_id}",
    )

    await call.message.answer(
        f"{DIV}\n✅ <b>Заказ #{order_id} создан</b>\n\nДобавьте товары:",
        parse_mode="HTML",
        reply_markup=order_actions_keyboard(order_id, "draft", True),
    )


@router.callback_query(F.data.startswith("ord_view:"))
async def cb_view_order(call: CallbackQuery):
    await call.answer()
    order_id = int(call.data.split(":")[1])
    order = get_order(order_id)
    if not order:
        return await call.message.answer("❌ Заказ не найден.")

    items = get_order_items(order_id)
    is_owner = order["user_id"] == call.from_user.id
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
    order = get_order(order_id)
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
        await call.message.answer(f"❌ Ошибка загрузки каталога:\n<code>{e}</code>", parse_mode="HTML")


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
        await call.message.answer(f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")


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
        return await message.answer("❌ Введите корректное количество, например: <code>5</code>", parse_mode="HTML")

    data = await state.get_data()
    order_id = data["order_id"]
    product = data["selected_product"]

    item_id = add_order_item(
        order_id=order_id,
        product_name=product["name"],
        product_href=product["href"],
        quantity=qty,
        unit=product["unit"],
    )

    await state.clear()

    # Показываем текущий заказ
    order = get_order(order_id)
    items = get_order_items(order_id)

    await message.answer(
        f"✅ Добавлено: <b>{product['name']}</b> — {qty} {product['unit']}\n\n"
        + format_order(order, items),
        parse_mode="HTML",
        reply_markup=order_actions_keyboard(order_id, "draft", True),
    )

    add_audit_log(
        message.from_user.id,
        message.from_user.full_name or str(message.from_user.id),
        get_role(message.from_user.id),
        "order_item_added",
        f"Заказ #{order_id}: добавлен {product['name']} × {qty}",
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
        await call.message.answer(f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")


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
    update_order_agent(order_id, agent["id"], agent["name"])
    await state.clear()

    order = get_order(order_id)
    items = get_order_items(order_id)

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
    order = get_order(order_id)

    if not order:
        return await call.message.answer("❌ Заказ не найден.")
    if order["user_id"] != call.from_user.id:
        return await call.message.answer("⛔ Это не ваш заказ.")
    if order["status"] != "draft":
        return await call.message.answer("⚠️ Заказ уже отправлен.")

    items = get_order_items(order_id)
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
    req_id = create_shipment_request(order_id, user.id, full_name)
    update_order_status(order_id, "pending")

    add_audit_log(
        user.id, full_name, get_role(user.id),
        "shipment_request_sent",
        f"Отправлена заявка #{req_id} (заказ #{order_id})",
    )

    await call.message.answer(
        f"{DIV}\n"
        f"✅ <b>Заявка #{req_id} отправлена!</b>\n\n"
        f"Ожидайте подтверждения от руководителя.\n"
        f"Вы получите уведомление.",
        parse_mode="HTML",
    )

    # Уведомляем руководителей
    notify_text = format_request_notify(order, items, req_id)
    recipients = get_notify_recipients()
    for uid in recipients:
        try:
            await bot.send_message(
                uid, notify_text,
                parse_mode="HTML",
                reply_markup=request_approve_keyboard(req_id),
            )
        except Exception as e:
            logger.warning("Не удалось уведомить %d: %s", uid, e)


# ─── Callback: удаление заказа ────────────────────────────────────────────────


@router.callback_query(F.data.startswith("ord_delete:"))
async def cb_delete_order(call: CallbackQuery):
    await call.answer()
    order_id = int(call.data.split(":")[1])
    order = get_order(order_id)

    if not order or order["user_id"] != call.from_user.id:
        return await call.message.answer("⛔ Нет доступа.")
    if order["status"] != "draft":
        return await call.message.answer("❌ Нельзя удалить отправленный заказ.")

    update_order_status(order_id, "rejected")
    add_audit_log(
        call.from_user.id,
        call.from_user.full_name or str(call.from_user.id),
        get_role(call.from_user.id),
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
    req = get_shipment_request(req_id)
    if not req:
        return await call.message.answer("❌ Заявка не найдена.")

    order = get_order(req["order_id"])
    if not order:
        return await call.message.answer("❌ Заказ не найден.")

    items = get_order_items(req["order_id"])
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
    req = get_shipment_request(req_id)

    if not req:
        return await call.answer("❌ Заявка не найдена", show_alert=True)
    if req["status"] != "pending":
        return await call.answer("⚠️ Заявка уже обработана", show_alert=True)

    boss_name = call.from_user.full_name or str(call.from_user.id)
    approve_shipment_request(req_id, call.from_user.id, boss_name)
    await call.answer("✅ Заявка одобрена")

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text + f"\n\n{DIV}\n✅ <b>Одобрено</b>  <code>{now}</code>  — {boss_name}",
        parse_mode="HTML",
    )

    # Уведомляем менеджера
    try:
        order = get_order(req["order_id"])
        await bot.send_message(
            req["user_id"],
            f"{DIV}\n"
            f"✅ <b>Заявка #{req_id} одобрена!</b>\n\n"
            f"👨‍💼 Одобрил: {boss_name}\n"
            f"🕐 {now}\n\n"
            f"Можно приступать к отгрузке.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить менеджера: %s", e)


@router.callback_query(F.data.startswith("req_no:"))
async def cb_reject_request(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)

    req_id = int(call.data.split(":")[1])
    req = get_shipment_request(req_id)

    if not req:
        return await call.answer("❌ Заявка не найдена", show_alert=True)
    if req["status"] != "pending":
        return await call.answer("⚠️ Заявка уже обработана", show_alert=True)

    boss_name = call.from_user.full_name or str(call.from_user.id)
    reject_shipment_request(req_id, call.from_user.id, boss_name)
    await call.answer("❌ Заявка отклонена")

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    await call.message.edit_text(
        call.message.text + f"\n\n{DIV}\n❌ <b>Отклонено</b>  <code>{now}</code>  — {boss_name}",
        parse_mode="HTML",
    )

    try:
        await bot.send_message(
            req["user_id"],
            f"{DIV}\n"
            f"❌ <b>Заявка #{req_id} отклонена</b>\n\n"
            f"👨‍💼 Отклонил: {boss_name}\n"
            f"🕐 {now}\n\n"
            f"Свяжитесь с руководителем для уточнения.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Не удалось уведомить менеджера: %s", e)
