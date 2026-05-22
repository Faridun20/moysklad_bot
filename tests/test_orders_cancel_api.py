"""
Smoke-тесты WebApp-эндпоинта отмены заказа (/api/orders/cancel).
FastAPI TestClient; мок границ verify_init_data/get_notify_bot, БД/роли настоящие.
"""

import pytest
from fastapi.testclient import TestClient


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
    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")
    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, "agent-uuid", "Client X")
    db.add_order_item(oid, "Product A", "", 1, "шт", 250.0)
    db.update_order_status(oid, "approved")

    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)
    return client, db, {"boss": boss_id, "mgr": mgr_id, "order": oid}


def test_cancel_approved_notifies_creator(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/orders/cancel",
        json={"initData": str(ids["boss"]), "order_id": ids["order"], "reason": "клиент отказался"},
    )
    assert resp.status_code == 200, resp.text
    assert db.get_order(ids["order"])["status"] == "cancelled"


def test_cancel_requires_reason(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/orders/cancel",
        json={"initData": str(ids["boss"]), "order_id": ids["order"], "reason": "x"},
    )
    assert resp.status_code == 400


def test_cancel_forbidden_for_manager(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/orders/cancel",
        json={"initData": str(ids["mgr"]), "order_id": ids["order"], "reason": "повод нормальный"},
    )
    assert resp.status_code == 403
    assert db.get_order(ids["order"])["status"] == "approved"


def test_cancel_shipped_conflict(client_env):
    client, db, ids = client_env
    db.update_order_status(ids["order"], "shipped")
    resp = client.post(
        "/api/orders/cancel",
        json={"initData": str(ids["boss"]), "order_id": ids["order"], "reason": "повод нормальный"},
    )
    assert resp.status_code == 409
