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


def _counterparty(name, phone=""):
    """Контрагент в справочнике → его id строкой (в таком виде он и лежит в
    `orders.agent_id`)."""
    from services import counterparties as cp

    return str(asyncio.run(cp.create(name, phone=phone or None))["counterparty_id"])


def test_get_orders_by_agent(isolated_db):
    db = isolated_db
    oid = _shipped_order(db, "A1", "Client A", 100.0, 3)  # 300.00
    _shipped_order(db, "B1", "Client B", 50.0, 1)         # другой агент
    rows = asyncio.run(db.get_orders_by_agent("A1"))
    assert len(rows) == 1
    assert rows[0]["id"] == oid
    assert rows[0]["total_cents"] == 30000
    assert asyncio.run(db.get_orders_by_agent("")) == []




def test_clients_overview_debt_split_by_currency(isolated_db):
    """Долг клиента в разных валютах НЕ складывается: debt_by_currency содержит
    отдельную запись на каждую валюту."""
    db = isolated_db
    for cur, qty, price in [("USD", 2, 100.0), ("UZS", 3, 50000.0)]:
        oid = _shipped_order(db, "MULTI", "Multi Cur", price, qty)
        db.update_order_currency(oid, cur)
    rows = {r["agent_id"]: r for r in asyncio.run(db.get_clients_overview())}
    dbc = {x["currency"]: x["amount"] for x in rows["MULTI"]["debt_by_currency"]}
    assert dbc.get("USD") == 200.0
    assert dbc.get("UZS") == 150000.0
    assert len(rows["MULTI"]["debt_by_currency"]) == 2


def test_clients_detail_boss(isolated_db, monkeypatch):
    """Карточка собирает имя и телефон из справочника, заказы — из наших,
    покупки — из расходных накладных."""
    from services import container_receipt, warehouse

    db = isolated_db
    agent_id = _counterparty("Client A", "+7")
    _shipped_order(db, agent_id, "Client A", 100.0, 2)

    pid = asyncio.run(container_receipt.create_product("Товар"))["product_id"]
    wid = asyncio.run(warehouse.default_warehouse_id())
    asyncio.run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 5, "price_cents": None}],
    ))
    asyncio.run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid, counterparty_id=int(agent_id),
        items=[{"product_id": pid, "quantity": 5, "price_cents": 10000}],
    ))

    client = _client(db, monkeypatch, 700, "boss")
    body = client.post(
        "/api/clients/detail", json={"initData": "700", "agent_id": agent_id}
    ).json()
    assert body["ok"] and body["name"] == "Client A"
    assert body["phone"] == "+7"
    assert len(body["orders"]) == 1 and body["orders"][0]["total_cents"] == 20000
    assert body["purchases"]["top_products"][0]["name"] == "Товар"
    assert body["purchases"]["total_cents"] == 50000


def test_clients_detail_forbidden_for_manager(isolated_db, monkeypatch):
    db = isolated_db
    client = _client(db, monkeypatch, 701, "manager")
    r = client.post("/api/clients/detail", json={"initData": "701", "agent_id": "A1"})
    assert r.status_code == 403


