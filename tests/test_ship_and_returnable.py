"""
Отгрузка (approved → shipped) и расширенная проверка «возвратопригодности».

DB-уровень на isolated_db + WebApp /api/orders/ship через TestClient.
Покрывает баг: оплаченный (paid_confirmed_at), но не отгруженный заказ должен
пускаться в возврат; и approved-заказ можно отметить отгруженным.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


# ─── DB: mark_order_shipped ──────────────────────────────────────────────────


def test_mark_order_shipped(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "M", "")
    db.add_order_item(oid, "T", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    res = asyncio.run(db.mark_order_shipped(oid, 2, "Boss"))
    assert res["ok"] is True
    assert asyncio.run(db.get_order(oid))["status"] == "shipped"


def test_mark_order_shipped_only_from_approved(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "M", "")
    db.update_order_status(oid, "pending")
    res = asyncio.run(db.mark_order_shipped(oid, 2, "Boss"))
    assert res["ok"] is False
    assert asyncio.run(db.get_order(oid))["status"] == "pending"


# ─── DB: расширенная возвратопригодность ─────────────────────────────────────


def test_returnable_includes_legacy_paid(isolated_db):
    """Заказ в статусе approved, но оплаченный (paid_confirmed_at) — возвратопригоден.

    Публичная обёртка is_order_returnable удалена в T1.7 (ноль вызовов вне
    тестов); предикат _is_returnable живёт внутри create_return, поэтому
    проверяем его через живой путь: до оплаты возврат отклоняется, после —
    проходит."""
    db = isolated_db
    oid = db.create_order(1, "M", "")
    db.add_order_item(oid, "T", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    items = asyncio.run(db.get_order_items(oid))

    # approved + не оплачен → создать возврат нельзя.
    denied = asyncio.run(
        db.create_return(
            oid,
            "full",
            "брак",
            [(items[0]["id"], 1, 100.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert denied["ok"] is False

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET paid_confirmed_at = ? WHERE id = ?"),
            (db.now_str(), oid),
        )
        conn.commit()

    r = asyncio.run(
        db.create_return(
            oid,
            "full",
            "брак",
            [(items[0]["id"], 1, 100.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert r["ok"] is True


# ─── WebApp /api/orders/ship ─────────────────────────────────────────────────


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **k):
        self.sent.append((a, k))


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    boss, mgr, wh = 100, 200, 300
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    db.set_role(wh, "w", "WH", "warehouse_keeper")
    oid = db.create_order(mgr, "Mgr", "")
    db.add_order_item(oid, "T", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")

    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, {"boss": boss, "mgr": mgr, "wh": wh, "order": oid}


def test_api_ship_by_warehouse(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/orders/ship", json={"initData": str(ids["wh"]), "order_id": ids["order"]}
    )
    assert resp.status_code == 200, resp.text
    assert asyncio.run(db.get_order(ids["order"]))["status"] == "shipped"


def test_api_ship_forbidden_for_manager(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/orders/ship", json={"initData": str(ids["mgr"]), "order_id": ids["order"]}
    )
    assert resp.status_code == 403
    assert asyncio.run(db.get_order(ids["order"]))["status"] == "approved"


def test_api_ship_conflict_when_not_approved(client_env):
    client, db, ids = client_env
    db.update_order_status(ids["order"], "shipped")
    resp = client.post(
        "/api/orders/ship", json={"initData": str(ids["boss"]), "order_id": ids["order"]}
    )
    assert resp.status_code == 409
