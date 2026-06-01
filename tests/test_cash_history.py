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
