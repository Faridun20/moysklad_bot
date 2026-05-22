"""
Smoke-тесты WebApp-эндпоинтов сдач и возвратов (/api/deposits/*, /api/returns/*).

FastAPI TestClient; мокаем границы: verify_init_data и get_notify_bot (чтобы не
звонить в Telegram). БД/роли — настоящие (isolated_db).
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

    boss_id, mgr_id, wh_id = 100, 200, 300
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")
    db.set_role(wh_id, "wh_user", "Warehouse", "warehouse_keeper")

    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, "agent-uuid", "Client X")
    db.add_order_item(oid, "Product A", "", 1, "шт", 250.0)
    db.update_order_status(oid, "shipped")

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
    ids = {"boss": boss_id, "mgr": mgr_id, "wh": wh_id, "order": oid}
    return client, db, ids, fake_bot


def test_deposit_pending_and_confirm(client_env):
    client, db, ids, fake_bot = client_env
    dep = db.create_cash_deposit(ids["mgr"], 250.0)

    # список
    resp = client.post("/api/deposits/pending", json={"initData": str(ids["boss"])})
    assert resp.status_code == 200, resp.text
    deposits = resp.json()["deposits"]
    assert any(d["id"] == dep["deposit_id"] for d in deposits)

    # подтверждение → заказ paid + уведомление менеджеру
    resp = client.post(
        "/api/deposits/confirm",
        json={"initData": str(ids["boss"]), "deposit_id": dep["deposit_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert db.get_order(ids["order"])["status"] == "paid"
    assert fake_bot.sent  # менеджер уведомлён


def test_deposit_reject_requires_reason(client_env):
    client, db, ids, _ = client_env
    dep = db.create_cash_deposit(ids["mgr"], 250.0)
    resp = client.post(
        "/api/deposits/reject",
        json={"initData": str(ids["boss"]), "deposit_id": dep["deposit_id"], "reason": "x"},
    )
    assert resp.status_code == 400


def test_deposit_confirm_forbidden_for_manager(client_env):
    client, db, ids, _ = client_env
    dep = db.create_cash_deposit(ids["mgr"], 250.0)
    resp = client.post(
        "/api/deposits/confirm",
        json={"initData": str(ids["mgr"]), "deposit_id": dep["deposit_id"]},
    )
    assert resp.status_code == 403


def test_returns_pending_and_confirm(client_env):
    client, db, ids, _ = client_env
    items = db.get_order_items(ids["order"])
    r = db.create_return(
        ids["order"],
        "full",
        "брак",
        [(items[0]["id"], 1, 250.0)],
        refund_method="no_refund",
        created_by=ids["mgr"],
    )

    # warehouse_keeper видит список
    resp = client.post("/api/returns/pending", json={"initData": str(ids["wh"])})
    assert resp.status_code == 200, resp.text
    assert any(x["id"] == r["return_id"] for x in resp.json()["returns"])

    # подтверждает только босс
    resp = client.post(
        "/api/returns/confirm",
        json={"initData": str(ids["wh"]), "return_id": r["return_id"]},
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/returns/confirm",
        json={"initData": str(ids["boss"]), "return_id": r["return_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert db.get_order(ids["order"])["status"] == "returned"
