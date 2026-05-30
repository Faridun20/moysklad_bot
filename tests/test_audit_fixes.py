"""
Регрессы на находки аудита: H1 (дубли/сверх-кол-во возвратов),
H2 (возврат только своего заказа + дедлайн для менеджера),
M3 (pending-сдачи не дают двойного распределения остатка).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


def _shipped_order(db, owner=1, qty=2, price=100.0):
    oid = db.create_order(owner, "M", "")
    db.update_order_agent(oid, "A", "Client")
    db.add_order_item(oid, "Товар", "", qty, "шт", price)
    db.update_order_status(oid, "shipped")
    return oid


# ─── H1 ──────────────────────────────────────────────────────────────────────


def test_duplicate_return_blocked(isolated_db):
    db = isolated_db
    oid = _shipped_order(db)
    items = asyncio.run(db.get_order_items(oid))
    r1 = asyncio.run(
        db.create_return(
            oid,
            "full",
            "брак",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert r1["ok"]
    r2 = asyncio.run(
        db.create_return(
            oid,
            "full",
            "ещё",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert r2["ok"] is False
    assert "уже есть" in r2["error"]


def test_return_qty_clamped_to_available(isolated_db):
    db = isolated_db
    oid = _shipped_order(db, qty=2, price=100.0)
    items = asyncio.run(db.get_order_items(oid))
    # просят 5 при доступных 2 → режется до 2, сумма пересчитывается
    r = asyncio.run(
        db.create_return(
            oid,
            "full",
            "брак",
            [(items[0]["id"], 5, 500.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert r["ok"] is True
    assert r["total_amount"] == 200.0


# ─── H2 (DB-уровень: дедлайн через force) ────────────────────────────────────


def test_manager_return_blocked_after_deadline(isolated_db):
    db = isolated_db
    oid = _shipped_order(db)
    # отгружен давно
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET shipped_at = ? WHERE id = ?"), ("2000-01-01 00:00:00", oid)
        )
        conn.commit()
    items = asyncio.run(db.get_order_items(oid))
    # менеджер (force=False) — за дедлайном, нельзя
    r_mgr = asyncio.run(
        db.create_return(
            oid,
            "full",
            "x",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=1,
            force=False,
        )
    )
    assert r_mgr["ok"] is False
    # начальство (force=True) — можно
    r_boss = asyncio.run(
        db.create_return(
            oid,
            "full",
            "x",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=2,
            force=True,
        )
    )
    assert r_boss["ok"] is True


# ─── M3 ──────────────────────────────────────────────────────────────────────


def test_pending_deposit_not_double_allocated(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    _shipped_order(db, owner=1, qty=1, price=250.0)
    d1 = asyncio.run(db.create_cash_deposit(1, 250.0))
    assert d1["ok"] and asyncio.run(db.get_cash_deposit_orders(d1["deposit_id"]))
    # после первой pending-сдачи остаток заказа исчерпан — вторая ничего не берёт
    assert asyncio.run(db.get_manager_open_orders_for_deposit(1)) == []
    d2 = asyncio.run(db.create_cash_deposit(1, 250.0))
    assert d2["ok"]
    assert asyncio.run(db.get_cash_deposit_orders(d2["deposit_id"])) == []


# ─── H2 (webapp: чужой заказ) ────────────────────────────────────────────────


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    db.set_role(200, "a", "MgrA", "manager")
    db.set_role(201, "b", "MgrB", "manager")
    oid = _shipped_order(db, owner=200, qty=1, price=100.0)

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, oid


def test_manager_cannot_return_foreign_order(client_env):
    client, db, oid = client_env
    resp = client.post(
        "/api/returns/create",
        json={
            "initData": "201",
            "order_id": oid,
            "reason": "норм причина",
            "refund_method": "cash",
        },
    )
    assert resp.status_code == 403
