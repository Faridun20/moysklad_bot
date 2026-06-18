"""
End-to-end: /api/currency/rates GET + POST set.

Мокаем границу verify_init_data, БД настоящая.
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss", "Boss", "boss")
    db.set_role(mgr_id, "mgr", "Manager", "manager")

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, {"boss": boss_id, "mgr": mgr_id}


def test_get_rates_returns_seeded_usd(client_env):
    client, _db, ids = client_env
    resp = client.post("/api/currency/rates", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["base"] == "USD"
    by = {r["currency_code"]: r for r in data["rates"]}
    assert "USD" in by
    assert by["USD"]["rate_to_base"] == 1.0


def test_get_rates_open_to_all_authed_roles(client_env):
    """Manager тоже может прочитать — для UI отображения цен."""
    client, _db, ids = client_env
    resp = client.post("/api/currency/rates", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 200


def test_set_rate_forbidden_for_manager(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/currency/rates/set",
        json={"initData": str(ids["mgr"]), "currency_code": "UZS", "rate_to_base": 0.00008},
    )
    assert resp.status_code == 403


def test_set_rate_ok_for_boss_and_persists(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/currency/rates/set",
        json={"initData": str(ids["boss"]), "currency_code": "UZS", "rate_to_base": 0.00008},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "currency_code": "UZS", "rate_to_base": 0.00008}
    assert db.get_currency_rate("UZS") == 0.00008


def test_set_rate_rejects_invalid_currency_400(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/currency/rates/set",
        json={"initData": str(ids["boss"]), "currency_code": "BTC", "rate_to_base": 50000},
    )
    assert resp.status_code == 400


def test_set_rate_rejects_invalid_amount_400(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/currency/rates/set",
        json={"initData": str(ids["boss"]), "currency_code": "UZS", "rate_to_base": -1.0},
    )
    assert resp.status_code == 400
