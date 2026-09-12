"""
/api/home: оркестрация ответа. Выручка и лидерборд считаются по НАШИМ данным
(расходные накладные + локальные заказы), поэтому мокать здесь нечего — БД,
роли и склад настоящие. Проверяем, что эндпоинт правильно собирает ответ и не
роняет экран, если одна из цифр не посчиталась.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def home_env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)  # сброс кэша ролей (reload очищает _role_cache)
    db = isolated_db

    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)
    return client, db, {"boss": boss_id, "mgr": mgr_id}


def _run(coro):
    return asyncio.run(coro)


def _sold(db, name, qty, price_cents, manager_id):
    """Продать товар: приход на склад + расходная накладная от менеджера.

    Через заказ, а не прямой накладной: лидерборд считает менеджеров по
    ЛОКАЛЬНЫМ заказам (`get_manager_performance`), и выручка дня — по
    накладным. Сценарий должен поднять оба.
    """
    from services import container_receipt, order_shipment, warehouse

    pid = _run(container_receipt.create_product(name))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": qty, "price_cents": None}],
    ))
    oid = db.create_order(manager_id, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, name, "", qty, "шт", price_cents / 100, product_id=pid)
    db.update_order_status(oid, "shipped")
    order = _run(db.get_order(oid))
    items = _run(db.get_order_items(oid))
    res = _run(order_shipment.ship_order(order, items, user_id=manager_id))
    assert res["ok"], res
    return oid


def test_home_boss_ok(home_env):
    client, db, ids = home_env
    _sold(db, "Кабель PV 0.6", 3, 100000, ids["mgr"])

    r = client.post("/api/home", json={"initData": str(ids["boss"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "boss"
    assert body["today"]["revenue"] == 3000.0  # 3 × 1000
    assert body["today"]["shipments"] == 1
    assert body["today"]["scope"] == "company"
    assert "pending_requests" in body
    emp = {e["name"]: e for e in body["top_employees"]}
    assert "Manager" in emp


def test_home_boss_survives_a_broken_counter(home_env, monkeypatch):
    """Одна цифра не посчиталась → нули, а не 500. Экран важнее показателя."""
    import webapp.server as server

    async def _boom(*a, **k):
        raise RuntimeError("счётчик сломан")

    monkeypatch.setattr(server, "sales_stats", _boom, raising=False)
    import services.warehouse as warehouse

    monkeypatch.setattr(warehouse, "sales_stats", _boom)

    client, db, ids = home_env
    r = client.post("/api/home", json={"initData": str(ids["boss"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["today"]["revenue"] == 0
    assert body["today"]["shipments"] == 0


def test_home_manager_counts_own_orders(home_env):
    """Менеджер видит СВОИ показатели, а не компанейские."""
    client, db, ids = home_env
    r = client.post("/api/home", json={"initData": str(ids["mgr"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "manager"
    assert body["today"]["scope"] == "personal"
