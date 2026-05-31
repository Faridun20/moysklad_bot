"""
Smoke-тесты WebApp-эндпоинтов кредитных лимитов (/api/credit/*).

Через FastAPI TestClient (синхронный). Мокаем только границу — verify_init_data
(auth проверяется в test_auth.py). БД и роли — настоящие (isolated_db).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


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

    # Заказ с контрагентом → попадёт в overview.
    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, "agent-uuid", "Client X")
    db.add_order_item(oid, "Product A", "", 3, "шт", 100.0)
    db.update_order_status(oid, "shipped")

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)
    return client, db, {"boss": boss_id, "mgr": mgr_id}


def test_overview_lists_agent(client_env):
    client, db, ids = client_env
    resp = client.post("/api/credit/overview", json={"initData": str(ids["boss"])})
    assert resp.status_code == 200, resp.text
    agents = resp.json()["agents"]
    assert any(a["agent_id"] == "agent-uuid" for a in agents)


def test_set_limit_persists(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/credit/set",
        json={
            "initData": str(ids["boss"]),
            "agent_id": "agent-uuid",
            "agent_name": "Client X",
            "limit_amount": 7500,
        },
    )
    assert resp.status_code == 200, resp.text
    assert asyncio.run(db.get_credit_limit("agent-uuid")) == 7500.0


def test_manager_forbidden(client_env):
    client, db, ids = client_env
    resp = client.post("/api/credit/overview", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 403


def test_set_limit_validation(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/credit/set",
        json={"initData": str(ids["boss"]), "agent_id": "agent-uuid", "limit_amount": -5},
    )
    assert resp.status_code == 400


def test_set_limit_requires_order(client_env):
    """Лимит нельзя задать контрагенту, на которого нет ни одного заказа —
    иначе плодятся лимиты-сироты вне overview."""
    client, db, ids = client_env
    resp = client.post(
        "/api/credit/set",
        json={
            "initData": str(ids["boss"]),
            "agent_id": "ghost-no-orders",
            "agent_name": "Ghost",
            "limit_amount": 5000,
        },
    )
    assert resp.status_code == 400
    assert "заказ" in resp.json()["detail"].lower()
    # И лимит не сохранился.
    assert asyncio.run(db.get_credit_limit("ghost-no-orders")) != 5000.0
