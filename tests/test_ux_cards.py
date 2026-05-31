"""
UX: самодостаточные карточки — вся инфа в одном месте без перехода между
разделами. Кредит-контекст в заявке, блок оплаты в карточке заказа, обогащённые
списки-кнопки, превью позиций в долге.

Реальная БД (isolated_db).
"""

import asyncio


def _credit_order(db, total, agent="A-1", status="draft"):
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, agent, "Ромашка")
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type='credit' WHERE id=?"), (oid,))
        conn.commit()
    db.update_order_status(oid, status)
    return oid


# ─── кредит-контекст в заявке ────────────────────────────────────────────────


def test_credit_context_over_limit(isolated_db):
    from handlers.orders import build_credit_context

    db = isolated_db
    oid = _credit_order(db, 3000.0)  # лимит по умолчанию 2000
    order = asyncio.run(db.get_order(oid))
    items = asyncio.run(db.get_order_items(oid))
    ctx = asyncio.run(build_credit_context(order, items))
    assert "Кредит клиента" in ctx
    assert "ПРЕВЫШЕНИЕ" in ctx


def test_credit_context_under_limit(isolated_db):
    from handlers.orders import build_credit_context

    db = isolated_db
    oid = _credit_order(db, 500.0)
    order = asyncio.run(db.get_order(oid))
    items = asyncio.run(db.get_order_items(oid))
    ctx = asyncio.run(build_credit_context(order, items))
    assert "в пределах лимита" in ctx


def test_credit_context_empty_for_paid(isolated_db):
    from handlers.orders import build_credit_context

    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")  # payment_type=paid по умолчанию
    db.update_order_agent(oid, "A-1", "Ромашка")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    order = asyncio.run(db.get_order(oid))
    items = asyncio.run(db.get_order_items(oid))
    assert asyncio.run(build_credit_context(order, items)) == ""


# ─── блок оплаты в карточке заказа ───────────────────────────────────────────


def test_order_card_shows_payment_block(isolated_db):
    from handlers.orders import format_order

    db = isolated_db
    oid = _credit_order(db, 100.0, status="shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET due_date='2099-01-01' WHERE id=?"), (oid,))
        conn.commit()
    order = asyncio.run(db.get_order(oid))
    items = asyncio.run(db.get_order_items(oid))
    summary = asyncio.run(db.get_order_payment_summary(oid))
    txt = format_order(order, items, summary=summary)
    assert "В долг" in txt
    assert "Остаток" in txt  # remaining 100, не оплачен


def test_order_card_draft_no_payment_block(isolated_db):
    from handlers.orders import format_order

    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    order = asyncio.run(db.get_order(oid))
    items = asyncio.run(db.get_order_items(oid))
    txt = format_order(order, items)  # без summary
    assert "Оплата сразу" not in txt and "В долг" not in txt  # draft — блока нет


# ─── обогащённый список «мои заказы» ─────────────────────────────────────────


def test_my_orders_keyboard_shows_client_and_amount(isolated_db):
    from handlers.orders import my_orders_keyboard

    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", "Ромашка")
    db.add_order_item(oid, "Товар", "", 2, "шт", 100.0)  # 200
    orders = asyncio.run(db.get_user_orders(1))
    markup = asyncio.run(my_orders_keyboard(orders))
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Ромашка" in t and "200" in t for t in texts)


# ─── превью позиций в долге ──────────────────────────────────────────────────


def test_debt_card_shows_item_preview():
    from handlers.debts import _format_debt_card

    order = {"id": 5, "agent_name": "Клиент", "currency": "USD", "due_date": None}
    items = [{"product_name": "Хлеб"}, {"product_name": "Соль"}, {"product_name": "Сахар"}]
    summary = {"total": 100.0, "confirmed": 0, "pending": 0, "remaining": 100.0}
    txt = _format_debt_card(order, items, "2026-01-01", show_owner=False, summary=summary)
    assert "3 поз." in txt
    assert "Хлеб" in txt
    assert "+1" in txt  # показали 2, ещё 1


# ─── тир 2: «что дальше» + цена в каталоге ───────────────────────────────────


def test_next_actions_keyboard():
    from utils.keyboards import next_actions_keyboard

    kb = next_actions_keyboard([("⏳ Ещё", "ord_requests"), ("🏠 Меню", "menu")])
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert cbs == ["ord_requests", "menu"]


def test_stock_page_shows_attached_price():
    from utils.formatters import format_stock_page

    rows = [
        {
            "name": "Вода",
            "stock": 50,
            "reserve": 0,
            "uom": {"name": "шт"},
            "folder": {},
            "_sale_price_str": "💵 15 USD",
        }
    ]
    txt = format_stock_page(rows, 0)
    assert "Вода" in txt
    assert "💵 15 USD" in txt


def test_stock_page_no_price_when_unset():
    from utils.formatters import format_stock_page

    rows = [{"name": "Сок", "stock": 10, "reserve": 0, "uom": {"name": "шт"}, "folder": {}}]
    txt = format_stock_page(rows, 0)
    assert "Сок" in txt
    assert "💵" not in txt  # цена не задана — строки нет
