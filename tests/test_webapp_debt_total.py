"""
WebApp /api/debts: единый остаток к получению в базовой валюте (объединяет
разные валюты через convert_to_base). Серверная часть — тестируема.
"""

import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _credit_shipped(db, mgr, total, currency, agent):
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, agent, agent)
    db.add_order_item(oid, "P", "", 1, "шт", total)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "UPDATE orders SET payment_type='credit', due_date='2099-01-01', currency=? WHERE id=?"
            ),
            (currency, oid),
        )
        conn.commit()
    return oid


def _client(db, monkeypatch, mgr):
    import webapp.server as server

    importlib.reload(roles)
    db.set_role(mgr, "m", "Mgr", "manager")
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def test_debts_unified_base_total(isolated_db, monkeypatch):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 200
    _credit_shipped(db, mgr, 100.0, "USD", "A-1")  # 100 USD
    _credit_shipped(db, mgr, 1_000_000.0, "UZS", "A-2")  # 1M UZS
    db.set_currency_rate("UZS", 0.00008, mgr)  # 1M * 0.00008 = 80 USD
    db._invalidate_currency_rates_cache()

    client = _client(db, monkeypatch, mgr)
    resp = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base_currency"] == "USD"
    assert body["remaining_base_total"] == 180.0  # 100 + 80
    assert body["remaining_base_partial"] is False


def test_debts_base_total_partial_without_rate(isolated_db, monkeypatch):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 201
    _credit_shipped(db, mgr, 100.0, "USD", "A-1")  # учитывается
    _credit_shipped(db, mgr, 50.0, "EUR", "A-2")  # курс EUR не задан → пропуск

    client = _client(db, monkeypatch, mgr)
    body = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"}).json()
    assert body["remaining_base_total"] == 100.0  # только USD
    assert body["remaining_base_partial"] is True  # EUR без курса


def test_debts_base_total_none_when_empty(isolated_db, monkeypatch):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 202
    client = _client(db, monkeypatch, mgr)
    body = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"}).json()
    assert body["remaining_base_total"] is None
