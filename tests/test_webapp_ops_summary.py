"""
WebApp: блок «Требует внимания» (/api/home) + экран операционной сводки
(/api/ops-summary). Реальная БД (isolated_db); Telegram initData мокаем;
МС-границу для boss-ветки /api/home мокаем, чтобы тест не зависел от сети.
"""

import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _client(db, monkeypatch, uid, role):
    import webapp.server as server

    importlib.reload(roles)
    db.set_role(uid, "u", "U", role)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})

    # boss-ветка /api/home ходит в МойСклад — мокаем границу (без сети).
    import services.moysklad as ms

    async def _stats(*a, **k):
        return {"total": 0, "count": 0, "clients": 0, "top_products": []}

    async def _ships(*a, **k):
        return []

    monkeypatch.setattr(ms, "get_sales_stats", _stats)
    monkeypatch.setattr(ms, "get_shipments", _ships)
    return TestClient(server.app)


def _credit_shipped(db, mgr, total, currency, agent):
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, agent, agent)
    db.add_order_item(oid, "P", "", 1, "шт", total)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "UPDATE orders SET payment_type='credit', due_date='2099-01-01', "
                "currency=? WHERE id=?"
            ),
            (currency, oid),
        )
        conn.commit()
    return oid


def test_home_attention_for_boss(isolated_db, monkeypatch):
    db = isolated_db
    boss = 500
    _credit_shipped(db, boss, 100.0, "USD", "Acme")  # один открытый долг
    client = _client(db, monkeypatch, boss, "boss")
    body = client.post("/api/home", json={"initData": str(boss)}).json()
    att = body["attention"]
    assert att["debts"] == 1
    for k in ("requests", "payments", "deposits", "returns", "debts"):
        assert isinstance(att[k], int)


def test_home_no_attention_for_manager(isolated_db, monkeypatch):
    db = isolated_db
    mgr = 501
    client = _client(db, monkeypatch, mgr, "manager")
    body = client.post("/api/home", json={"initData": str(mgr)}).json()
    assert "attention" not in body  # блок только для босса


def test_ops_summary_boss(isolated_db, monkeypatch):
    db = isolated_db
    boss = 502
    client = _client(db, monkeypatch, boss, "boss")
    body = client.post("/api/ops-summary", json={"initData": str(boss)}).json()
    assert "total" in body
    assert "stale_orders" in body and "ms_anomalies" in body


def test_ops_summary_forbidden_for_manager(isolated_db, monkeypatch):
    db = isolated_db
    mgr = 503
    client = _client(db, monkeypatch, mgr, "manager")
    r = client.post("/api/ops-summary", json={"initData": str(mgr)})
    assert r.status_code == 403
