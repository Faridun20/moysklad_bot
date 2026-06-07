"""Карточка контрагента («Клиенты»): заказы по агенту, overview с МС-балансом,
detail-эндпоинт (баланс + долг + заказы + покупки), агрегация покупок из МС.
Реальная БД (isolated_db); граница МойСклад мокается."""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _client(db, monkeypatch, uid, role):
    import webapp.server as server

    importlib.reload(roles)
    db.set_role(uid, "u", "U", role)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _shipped_order(db, agent_id, agent_name, total_per_unit, qty, mgr=2):
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, agent_id, agent_name)
    db.add_order_item(oid, "Товар", "", qty, "шт", total_per_unit)
    db.update_order_status(oid, "shipped")
    return oid


def _set_balance(db, ms_id, name, phone, balance_cents):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO ms_counterparties (ms_id, name, phone, balance_cents, updated_at) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            (ms_id, name, phone, balance_cents, db.now_str()),
        )
        conn.commit()


def test_get_orders_by_agent(isolated_db):
    db = isolated_db
    oid = _shipped_order(db, "A1", "Client A", 100.0, 3)  # 300.00
    _shipped_order(db, "B1", "Client B", 50.0, 1)         # другой агент
    rows = asyncio.run(db.get_orders_by_agent("A1"))
    assert len(rows) == 1
    assert rows[0]["id"] == oid
    assert rows[0]["total_cents"] == 30000
    assert asyncio.run(db.get_orders_by_agent("")) == []


def test_clients_overview_includes_ms_balance(isolated_db):
    db = isolated_db
    _shipped_order(db, "A1", "Client A", 100.0, 1)
    _set_balance(db, "A1", "Client A", "+7", 150000)   # клиент с заказом + баланс
    _set_balance(db, "Z9", "Client Z", "", -5000)       # баланс без заказов/лимита
    rows = {r["agent_id"]: r for r in asyncio.run(db.get_clients_overview())}
    assert rows["A1"]["balance_cents"] == 150000
    assert rows["A1"]["phone"] == "+7"
    assert "Z9" in rows                       # баланс-only контрагент тоже виден
    assert rows["Z9"]["balance_cents"] == -5000


def test_clients_detail_boss(isolated_db, monkeypatch):
    db = isolated_db
    import services.moysklad as moysklad

    async def fake_purchases(agent_id, max_demands=20):
        return {
            "top_products": [{"name": "Товар", "qty": 5, "sum_cents": 50000}],
            "recent": [], "total_cents": 50000, "count": 1,
        }

    monkeypatch.setattr(moysklad, "get_counterparty_purchases", fake_purchases)
    _shipped_order(db, "A1", "Client A", 100.0, 2)
    _set_balance(db, "A1", "Client A", "+7", 99900)
    client = _client(db, monkeypatch, 700, "boss")
    body = client.post("/api/clients/detail", json={"initData": "700", "agent_id": "A1"}).json()
    assert body["ok"] and body["name"] == "Client A"
    assert body["balance_cents"] == 99900
    assert len(body["orders"]) == 1 and body["orders"][0]["total_cents"] == 20000
    assert body["purchases"]["top_products"][0]["name"] == "Товар"


def test_clients_detail_forbidden_for_manager(isolated_db, monkeypatch):
    db = isolated_db
    client = _client(db, monkeypatch, 701, "manager")
    r = client.post("/api/clients/detail", json={"initData": "701", "agent_id": "A1"})
    assert r.status_code == 403


def test_get_counterparty_purchases_aggregates(isolated_db, monkeypatch):
    import services.moysklad as moysklad

    async def fake_ms_get(path, params=None, session=None):
        return {"rows": [
            {"meta": {"href": "x/entity/demand/d1"}, "moment": "2026-06-01 10:00:00", "sum": 30000},
            {"meta": {"href": "x/entity/demand/d2"}, "moment": "2026-05-01 10:00:00", "sum": 20000},
        ]}

    async def fake_positions(demand_id):
        return [{"assortment": {"name": "Товар X"}, "quantity": 2, "price": 10000}]

    monkeypatch.setattr(moysklad, "ms_get", fake_ms_get)
    monkeypatch.setattr(moysklad, "get_shipment_positions", fake_positions)
    res = asyncio.run(moysklad.get_counterparty_purchases("AGG-UNIQUE-1"))
    assert res["count"] == 2
    assert res["total_cents"] == 50000          # 30000 + 20000
    assert res["top_products"][0]["name"] == "Товар X"
    assert res["top_products"][0]["sum_cents"] == 40000  # 2 demands × (2 × 10000)
    assert res["top_products"][0]["qty"] == 4
