"""
Аналитика менеджеров (#36): get_manager_performance — агрегация локальных orders
по user_id. Реальная БД (isolated_db).
"""

import asyncio


def test_manager_performance_aggregates_by_user(isolated_db):
    db = isolated_db
    db.set_role(10, "m1", "Manager One", "manager")
    db.set_role(20, "m2", "Manager Two", "manager")

    # M1: shipped 200 (выручка + долг 200, не оплачен)
    o1 = db.create_order(10, "Manager One", "")
    db.update_order_agent(o1, "A-1", "C1")
    db.add_order_item(o1, "P", "", 2, "шт", 100.0)
    db.update_order_status(o1, "shipped")
    # M1: approved (не выручка)
    o2 = db.create_order(10, "Manager One", "")
    db.add_order_item(o2, "P", "", 1, "шт", 50.0)
    db.update_order_status(o2, "approved")
    # M2: paid 300 (выручка, без долга)
    o3 = db.create_order(20, "Manager Two", "")
    db.update_order_agent(o3, "A-2", "C2")
    db.add_order_item(o3, "P", "", 3, "шт", 100.0)
    db.update_order_status(o3, "paid")

    rows = asyncio.run(
        db.get_manager_performance("2000-01-01 00:00:00", "2999-01-01 00:00:00")
    )
    by = {r["user_id"]: r for r in rows}

    assert by[10]["orders_count"] == 2
    assert by[10]["shipped"] == 1
    assert by[10]["approved"] == 1
    assert by[10]["revenue"] == 200.0
    assert by[10]["debt"] == 200.0
    assert by[10]["full_name"] == "Manager One"

    assert by[20]["revenue"] == 300.0
    assert by[20]["debt"] == 0.0  # paid не в debt_statuses

    # Сортировка по выручке убыв. → M2 (300) первый.
    assert rows[0]["user_id"] == 20


def test_manager_performance_period_filter(isolated_db):
    db = isolated_db
    db.set_role(10, "m1", "M1", "manager")
    o = db.create_order(10, "M1", "")
    db.add_order_item(o, "P", "", 1, "шт", 100.0)
    db.update_order_status(o, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET created_at='2020-01-01 00:00:00' WHERE id=?"), (o,)
        )
        conn.commit()

    # Период без 2020 → пусто.
    assert asyncio.run(
        db.get_manager_performance("2023-01-01 00:00:00", "2999-01-01 00:00:00")
    ) == []
    # Период с 2020 → есть.
    rows = asyncio.run(
        db.get_manager_performance("2019-01-01 00:00:00", "2021-01-01 00:00:00")
    )
    assert len(rows) == 1 and rows[0]["revenue"] == 100.0


def test_manager_performance_debt_minus_confirmed(isolated_db):
    db = isolated_db
    db.set_role(10, "m1", "M1", "manager")
    o = db.create_order(10, "M1", "")
    db.update_order_agent(o, "A-1", "C1")
    db.add_order_item(o, "P", "", 1, "шт", 100.0)
    db.update_order_status(o, "shipped")
    # Подтверждённый платёж 40 → долг 60.
    pid = db.add_payment(
        user_id=10, username="", full_name="M1", amount=40.0, currency="USD",
        comment="", order_id=o,
    )
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status='confirmed' WHERE id=?"), (pid,))
        conn.commit()

    rows = asyncio.run(
        db.get_manager_performance("2000-01-01 00:00:00", "2999-01-01 00:00:00")
    )
    assert rows[0]["debt"] == 60.0
    assert rows[0]["revenue"] == 100.0


def test_manager_performance_empty(isolated_db):
    db = isolated_db
    assert asyncio.run(
        db.get_manager_performance("2000-01-01 00:00:00", "2999-01-01 00:00:00")
    ) == []
