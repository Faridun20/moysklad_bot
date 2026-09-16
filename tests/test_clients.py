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
    # «Сколько всего купил» — по ВСЕМ накладным и раздельно по валютам: раньше
    # здесь стояла сумма последних двадцати, сложенная через валюты.
    assert body["purchases"]["total_by_currency"] == [{"currency": "USD", "amount_cents": 50000}]
    assert body["purchases"]["total_base_cents"] == 50000
    assert body["purchases"]["count"] == 1
    assert body["purchases"]["last_date"]
    assert body["purchases"]["recent"][0]["number"]


def test_clients_detail_allowed_for_manager(isolated_db, monkeypatch):
    """A3: карточка контрагента — тоже менеджеру, на чтение (не новая утечка:
    заказы и контрагентов он и так видит по отдельности)."""
    db = isolated_db
    agent_id = _counterparty("Client A", "+7")
    _shipped_order(db, agent_id, "Client A", 100.0, 2)

    client = _client(db, monkeypatch, 701, "manager")
    r = client.post("/api/clients/detail", json={"initData": "701", "agent_id": agent_id})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["name"] == "Client A"
    assert len(body["orders"]) == 1


def test_clients_detail_forbidden_for_warehouse_keeper(isolated_db, monkeypatch):
    """Запись (карточка — не про склад) остаётся закрытой ролям вне
    admin/boss/manager — та же тройка, что у /api/search."""
    db = isolated_db
    client = _client(db, monkeypatch, 702, "warehouse_keeper")
    r = client.post("/api/clients/detail", json={"initData": "702", "agent_id": "A1"})
    assert r.status_code == 403


def test_clients_shipment_allowed_for_manager(isolated_db, monkeypatch):
    """Состав отгрузки раскрывается из карточки клиента (A3) — та же роль."""
    from services import container_receipt, warehouse

    db = isolated_db
    agent_id = _counterparty("Client B", "+998")
    pid = asyncio.run(container_receipt.create_product("Товар Б"))["product_id"]
    wid = asyncio.run(warehouse.default_warehouse_id())
    # Расходная накладная без остатка отказала бы («не хватает на складе») —
    # сначала приходуем товар, как и в test_clients_detail_boss.
    asyncio.run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 2, "price_cents": None}],
    ))
    inv = asyncio.run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid, counterparty_id=int(agent_id),
        items=[{"product_id": pid, "quantity": 2, "price_cents": 5000}],
    ))
    assert inv["ok"], inv
    invoice_id = inv["invoice_id"]

    client = _client(db, monkeypatch, 703, "manager")
    r = client.post("/api/clients/shipment", json={"initData": "703", "invoice_id": invoice_id})
    assert r.status_code == 200
    assert r.json()["ok"] and r.json()["positions"][0]["name"] == "Товар Б"


