"""
IMPLEMENTATION.md Фаза 4: сдача наличных (cash deposits), сервисный слой.
На настоящей SQLite (isolated_db). UI/cron — отдельной фазой.
"""

import asyncio

import pytest


@pytest.fixture
def mgr_orders(isolated_db):
    """Менеджер + два отгруженных неоплаченных заказа (200 и 300)."""
    db = isolated_db
    mgr = 200
    db.set_role(mgr, "mgr", "Manager", "manager")

    def _shipped(total: float):
        oid = db.create_order(mgr, "Manager", "")
        db.add_order_item(oid, "Товар", "", 1, "шт", total)
        db.update_order_status(oid, "shipped")
        return oid

    o1 = _shipped(200.0)
    o2 = _shipped(300.0)
    return db, mgr, o1, o2


def test_auto_fifo_and_confirm_closes_covered_order(mgr_orders):
    db, mgr, o1, o2 = mgr_orders
    res = asyncio.run(db.create_cash_deposit(mgr, 200.0))  # авто-FIFO → весь на o1
    assert res["ok"] is True
    assert res["allocations"] == [(o1, 200.0)]

    conf = asyncio.run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))
    assert conf["ok"] is True
    assert conf["closed_orders"] == [o1]
    # o1 закрыт, o2 — нет.
    assert db.get_order(o1)["status"] == "paid"
    assert db.get_order(o1)["payment_confirmed"] == 1
    assert db.get_order(o2)["status"] == "shipped"


def test_partial_deposit_does_not_close(mgr_orders):
    db, mgr, o1, o2 = mgr_orders
    res = asyncio.run(db.create_cash_deposit(mgr, 100.0))  # < total o1 (200)
    asyncio.run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))
    assert db.get_order(o1)["status"] == "shipped"
    assert db.get_order(o1)["payment_confirmed"] == 0
    # Долить ещё 100 → закроется.
    res2 = asyncio.run(db.create_cash_deposit(mgr, 100.0))
    asyncio.run(db.confirm_cash_deposit(res2["deposit_id"], 1, "Boss"))
    assert db.get_order(o1)["status"] == "paid"


def test_manual_allocation(mgr_orders):
    db, mgr, o1, o2 = mgr_orders
    res = asyncio.run(db.create_cash_deposit(mgr, 150.0, allocations=[(o1, 100.0), (o2, 50.0)]))
    assert res["ok"] is True
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "SELECT order_id, amount_allocated, is_manual FROM cash_deposit_orders "
                "WHERE deposit_id = ? ORDER BY order_id"
            ),
            (res["deposit_id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    assert all((r["is_manual"] if db.USE_POSTGRES else r[2]) == 1 for r in rows)


def test_reject_deposit_keeps_orders_open(mgr_orders):
    db, mgr, o1, o2 = mgr_orders
    res = asyncio.run(db.create_cash_deposit(mgr, 200.0))
    r = asyncio.run(db.reject_cash_deposit(res["deposit_id"], 1, "Boss", "сумма не сошлась"))
    assert r["ok"] is True
    assert db.get_order(o1)["status"] == "shipped"
    # Повторная обработка отклонённой → нельзя.
    assert asyncio.run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))["ok"] is False


def test_create_rejects_nonpositive_amount(mgr_orders):
    db, mgr, o1, o2 = mgr_orders
    assert asyncio.run(db.create_cash_deposit(mgr, 0))["ok"] is False
    assert asyncio.run(db.create_cash_deposit(mgr, -5))["ok"] is False


def test_pending_list_and_overdue(mgr_orders):
    db, mgr, o1, o2 = mgr_orders
    res = asyncio.run(db.create_cash_deposit(mgr, 100.0))
    pending = asyncio.run(db.get_pending_cash_deposits())  # async после asyncpg Stage 5
    assert any(p["id"] == res["deposit_id"] for p in pending)

    # Состарим отгрузку o2 → попадёт в overdue.
    from datetime import datetime, timedelta

    old = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET shipped_at = ? WHERE id = ?"), (old, o2))
        conn.commit()
    overdue_ids = {
        o["id"] for o in asyncio.run(db.get_overdue_undeposited_orders(days=2))
    }  # async после asyncpg Stage 7
    assert o2 in overdue_ids


def test_deposit_paid_order_excluded_from_agent_debt(isolated_db):
    """Регресс (найдено smoke-прогоном): заказ, закрытый cash-deposit'ом
    (status=paid, payment_confirmed=1, без строки в payments), не должен
    висеть в долге контрагента."""
    db = isolated_db
    mgr = 200
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.update_order_agent(oid, "A-PAID", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 250.0)
    db.update_order_status(oid, "shipped")
    assert asyncio.run(db.get_agent_current_debt("A-PAID")) == 250.0

    dep = asyncio.run(db.create_cash_deposit(mgr, 250.0))
    asyncio.run(db.confirm_cash_deposit(dep["deposit_id"], 1, "Boss"))
    assert db.get_order(oid)["status"] == "paid"
    assert asyncio.run(db.get_agent_current_debt("A-PAID")) == 0.0
