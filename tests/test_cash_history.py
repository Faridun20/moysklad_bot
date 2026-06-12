"""
get_cash_history — единая лента движения денег для босса (платежи + сдачи +
возвраты, новые сверху, с именем «кто»). Реальная БД (isolated_db).
"""

import asyncio


def test_cash_history_merges_kinds_with_names(isolated_db):
    db = isolated_db
    db.set_role(10, "m1", "Иван Менеджер", "manager")

    # Платёж (manual).
    db.add_payment(10, "m1", "Иван Менеджер", 1500.0, "USD", "аренда")
    # Сдача наличных.
    asyncio.run(db.create_cash_deposit(10, 500.0))
    # Возврат по заказу.
    oid = db.create_order(10, "Иван Менеджер", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 200.0)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "created_by, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)"
            ),
            (oid, "full", "брак", 200.0, 10, db.now_str()),
        )
        conn.commit()

    hist = asyncio.run(db.get_cash_history(80))
    kinds = {h["kind"] for h in hist}
    assert {"payment", "deposit", "return"} <= kinds
    # Имя резолвится из user_roles, а не голый id.
    assert all(h["who"] == "Иван Менеджер" for h in hist)
    # Суммы проброшены.
    pay = next(h for h in hist if h["kind"] == "payment")
    assert pay["amount"] == 1500.0
    # Лента отсортирована по дате убыв. (created_at — строка).
    dates = [h["created_at"] for h in hist]
    assert dates == sorted(dates, reverse=True)


def test_cash_history_empty(isolated_db):
    assert asyncio.run(isolated_db.get_cash_history(80)) == []


def test_cash_history_uses_order_currency_for_returns(isolated_db):
    """WP-07: возврат подписан валютой заказа (не хардкод USD), сумма из cents."""
    db = isolated_db
    db.set_role(10, "m", "Mgr", "manager")
    oid = db.create_order(10, "Mgr", "")
    db.add_order_item(oid, "P", "", 1, "шт", 100)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET currency='UZS' WHERE id=?"), (oid,))
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "total_amount_cents, refund_method, created_by, status, created_at) "
                "VALUES (?, 'partial', 'x', ?, ?, 'debt_reduction', 10, 'confirmed', ?)"
            ),
            (oid, 5_000_000.0, 500_000_000, db.now_str()),
        )
        conn.commit()
    ret = next(h for h in asyncio.run(db.get_cash_history(80)) if h["kind"] == "return")
    assert ret["currency"] == "UZS"  # не «USD»
    assert ret["amount"] == 5_000_000.0


def test_cash_history_deposit_currency_is_base(isolated_db):
    """WP-07: сдача подписана базовой валютой, а не литералом USD."""
    db = isolated_db
    db.set_role(10, "m", "Mgr", "manager")
    asyncio.run(db.create_cash_deposit(10, 500.0))
    dep = next(h for h in asyncio.run(db.get_cash_history(80)) if h["kind"] == "deposit")
    from config import BASE_CURRENCY

    assert dep["currency"] == (BASE_CURRENCY or "USD").upper()


def test_cash_history_filters_by_period(isolated_db):
    """WP-11: лента ограничена периодом (по COALESCE(confirmed_at, created_at))."""
    db = isolated_db
    db.set_role(10, "m", "Mgr", "manager")
    old = db.add_payment(10, "@m", "Mgr", 100.0, "USD", "old")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE payments SET created_at='2020-01-01 00:00:00' WHERE id=?"), (old,)
        )
        conn.commit()
    db.add_payment(10, "@m", "Mgr", 50.0, "USD", "new")  # created_at = now
    hist = asyncio.run(
        db.get_cash_history(80, since="2026-01-01 00:00:00", until="2099-01-01 00:00:00")
    )
    amounts = {h["amount"] for h in hist if h["kind"] == "payment"}
    assert 50.0 in amounts and 100.0 not in amounts  # старый вне периода


def test_cash_history_hides_returns_of_deleted_orders(isolated_db):
    """WP-11: возврат по удалённому в МС заказу скрыт (как и его платежи)."""
    db = isolated_db
    db.set_role(10, "m", "Mgr", "manager")
    oid = db.create_order(10, "Mgr", "")
    db.add_order_item(oid, "P", "", 1, "шт", 100)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET ms_deleted_at='2026-06-03 00:00:00' WHERE id=?"), (oid,))
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "total_amount_cents, refund_method, created_by, status, created_at) "
                "VALUES (?, 'partial', 'x', 50.0, 5000, 'debt_reduction', 10, 'confirmed', ?)"
            ),
            (oid, db.now_str()),
        )
        conn.commit()
    hist = asyncio.run(db.get_cash_history(80))
    assert not any(h["kind"] == "return" for h in hist)


def test_cash_history_hides_payments_of_deleted_orders(isolated_db):
    """Платёж по удалённому в МС заказу НЕ показываем в ленте — он исключён и из
    итога «Деньги» (get_money_totals), лента обязана с ним совпадать. standalone-
    платёж (без заказа) и платёж по живому заказу остаются."""
    db = isolated_db
    db.set_role(10, "m1", "Mgr", "manager")

    live = db.create_order(10, "Mgr", "")
    db.add_order_item(live, "P", "", 1, "шт", 100.0)
    db.add_payment(10, "m1", "Mgr", 100.0, "USD", "по живому", order_id=live)

    phantom = db.create_order(10, "Mgr", "")
    db.add_order_item(phantom, "P", "", 1, "шт", 30.0)
    db.add_payment(10, "m1", "Mgr", 30.0, "USD", "по удалённому", order_id=phantom)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET ms_deleted_at='2026-06-03 00:00:00' WHERE id=?"), (phantom,)
        )
        conn.commit()

    db.add_payment(10, "m1", "Mgr", 50.0, "USD", "standalone")  # order_id=None

    hist = asyncio.run(db.get_cash_history(80))
    pay_amounts = {h["amount"] for h in hist if h["kind"] == "payment"}
    assert pay_amounts == {100.0, 50.0}  # платёж по удалённому (30) скрыт
