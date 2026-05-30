"""
Тесты управления ценами (PR C): product_prices DB + API + enforcement.

Ключевое:
  * set/get/батч/кэш-инвалидация
  * валидация (отрицательные/inf/слишком большие)
  * enforcement минимума при add_item (ниже→400, выше→ок, prefill)
  * profit для boss, cost/profit НЕ утекает менеджеру
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient


# ─── DB-уровень ──────────────────────────────────────────────────────────────


def test_set_and_get_product_price(isolated_db):
    db = isolated_db
    ok, err = db.set_product_price("prod-1", "Гвозди", 150.0, 100.0, "USD", updated_by=1)
    assert ok and err is None
    pp = db.get_product_price("prod-1")
    assert pp["sale_price"] == 150.0
    assert pp["cost_price"] == 100.0
    assert pp["currency"] == "USD"


def test_get_product_price_none_when_unset(isolated_db):
    assert isolated_db.get_product_price("nonexistent") is None


def test_set_product_price_optional_fields(isolated_db):
    """sale без cost — допустимо (cost None)."""
    db = isolated_db
    ok, _ = db.set_product_price("prod-2", "Болты", 50.0, None, "USD", updated_by=1)
    assert ok
    pp = db.get_product_price("prod-2")
    assert pp["sale_price"] == 50.0
    assert pp["cost_price"] is None


def test_set_product_price_validates_amounts(isolated_db):
    db = isolated_db
    for bad in (-1.0, float("inf"), float("nan"), 10_000_001.0):
        ok, err = db.set_product_price("p", "X", bad, None, "USD", updated_by=1)
        assert ok is False, f"должно отклонить sale={bad!r}"
        assert err is not None


def test_set_product_price_requires_ms_id(isolated_db):
    ok, err = isolated_db.set_product_price("", "X", 10.0, None, "USD", updated_by=1)
    assert ok is False and "ms_id" in err


def test_set_product_price_upsert_and_cache_invalidation(isolated_db):
    db = isolated_db
    db.set_product_price("p", "X", 100.0, None, "USD", updated_by=1)
    assert db.get_product_price("p")["sale_price"] == 100.0  # заполняет кэш
    # Обновляем — кэш должен инвалидироваться
    db.set_product_price("p", "X", 200.0, None, "USD", updated_by=1)
    assert db.get_product_price("p")["sale_price"] == 200.0


def test_get_product_prices_by_ids_batch(isolated_db):
    db = isolated_db
    db.set_product_price("a", "A", 10.0, 5.0, "USD", updated_by=1)
    db.set_product_price("b", "B", 20.0, 12.0, "USD", updated_by=1)
    res = asyncio.run(db.get_product_prices_by_ids(["a", "b", "missing"]))
    assert set(res) == {"a", "b"}
    assert res["a"]["cost_price"] == 5.0


def test_get_all_product_prices(isolated_db):
    db = isolated_db
    db.set_product_price("a", "Aaa", 10.0, None, "USD", updated_by=1)
    db.set_product_price("b", "Bbb", 20.0, None, "USD", updated_by=1)
    rows = asyncio.run(db.get_all_product_prices())
    assert len(rows) == 2


# ─── E2E endpoints + enforcement ─────────────────────────────────────────────


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
        lambda init_data: {
            "id": int(init_data),
            "first_name": "U",
            "last_name": "",
            "username": "u",
        },
    )
    return TestClient(server.app), db, {"boss": boss_id, "mgr": mgr_id}


def test_prices_set_forbidden_for_manager(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/products/prices/set",
        json={"initData": str(ids["mgr"]), "ms_id": "p1", "sale_price": 100},
    )
    assert resp.status_code == 403


def test_prices_set_and_list_for_boss(client_env):
    client, db, ids = client_env
    resp = client.post(
        "/api/products/prices/set",
        json={
            "initData": str(ids["boss"]),
            "ms_id": "p1",
            "product_name": "Товар",
            "sale_price": 150,
            "cost_price": 90,
        },
    )
    assert resp.status_code == 200, resp.text
    assert db.get_product_price("p1")["cost_price"] == 90.0
    # Список доступен boss
    lst = client.post("/api/products/prices", json={"initData": str(ids["boss"])})
    assert lst.status_code == 200
    assert any(r["ms_id"] == "p1" for r in lst.json()["prices"])


def test_prices_list_forbidden_for_manager(client_env):
    client, _db, ids = client_env
    resp = client.post("/api/products/prices", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 403


def _make_draft(db, mgr_id):
    oid = db.create_order(mgr_id, "Manager", "")
    return oid


def test_add_item_enforces_minimum_price(client_env):
    """Цена ниже минимума → 400."""
    client, db, ids = client_env
    db.set_product_price("PRODX", "Товар X", 100.0, None, "USD", updated_by=ids["boss"])
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/add_item",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "product_name": "Товар X",
            "product_href": "https://api.moysklad.ru/entity/product/PRODX",
            "quantity": 2,
            "price": 80,  # ниже минимума 100
        },
    )
    assert resp.status_code == 400
    assert "минимальной" in resp.json()["detail"]


def test_add_item_allows_price_above_minimum(client_env):
    client, db, ids = client_env
    db.set_product_price("PRODY", "Товар Y", 100.0, None, "USD", updated_by=ids["boss"])
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/add_item",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "product_name": "Товар Y",
            "product_href": "https://api.moysklad.ru/entity/product/PRODY",
            "quantity": 1,
            "price": 120,  # выше минимума — ок
        },
    )
    assert resp.status_code == 200, resp.text


def test_add_item_prefills_min_when_price_zero(client_env):
    """price не задан/0 → подставляется sale_price."""
    client, db, ids = client_env
    db.set_product_price("PRODZ", "Товар Z", 100.0, None, "USD", updated_by=ids["boss"])
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/add_item",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "product_name": "Товар Z",
            "product_href": "https://api.moysklad.ru/entity/product/PRODZ",
            "quantity": 1,
            "price": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    items = asyncio.run(db.get_order_items(oid))
    assert items[0]["price"] == 100.0  # префилл минимумом


def test_orders_profit_visible_to_boss_hidden_from_manager(client_env):
    """boss видит profit; менеджер — нет (поля отсутствуют)."""
    client, db, ids = client_env
    db.set_product_price("PRODP", "Товар P", 150.0, 100.0, "USD", updated_by=ids["boss"])
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.add_order_item(oid, "Товар P", "https://api.moysklad.ru/entity/product/PRODP", 2, "шт", 150.0)

    # Менеджер: profit отсутствует
    r_mgr = client.post("/api/orders", json={"initData": str(ids["mgr"])})
    mgr_order = next(o for o in r_mgr.json()["orders"] if o["id"] == oid)
    assert "profit" not in mgr_order
    assert "cost_price" not in str(mgr_order.get("items"))

    # Boss: profit = (150-100)*2 = 100
    r_boss = client.post("/api/orders", json={"initData": str(ids["boss"])})
    boss_order = next(o for o in r_boss.json()["orders"] if o["id"] == oid)
    assert boss_order["profit"] == 100.0
    assert boss_order["profit_partial"] is False


def test_orders_profit_partial_when_cost_unknown(client_env):
    """Себестоимость не задана → profit_partial=True."""
    client, db, ids = client_env
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.add_order_item(oid, "БезЦены", "https://api.moysklad.ru/entity/product/NOPRICE", 1, "шт", 50.0)
    r_boss = client.post("/api/orders", json={"initData": str(ids["boss"])})
    boss_order = next(o for o in r_boss.json()["orders"] if o["id"] == oid)
    assert boss_order["profit_partial"] is True


def test_stock_hides_cost_from_manager(client_env, monkeypatch):
    """/api/stock: менеджер видит sale_price, но НЕ cost_price."""
    client, db, ids = client_env
    db.set_product_price("STK1", "Складской", 200.0, 150.0, "USD", updated_by=ids["boss"])

    async def fake_stock():
        return [
            {
                "name": "Складской",
                "stock": 10,
                "reserve": 0,
                "uom": {"name": "шт"},
                "meta": {"href": "https://api.moysklad.ru/entity/product/STK1"},
                "folder": {"meta": {"href": ""}, "name": ""},
            }
        ]

    async def fake_cats():
        return []

    import webapp.server as server

    monkeypatch.setattr(server, "get_role", lambda uid: db.get_role(uid))
    import services.moysklad as ms

    monkeypatch.setattr(ms, "get_all_stock", fake_stock)
    monkeypatch.setattr(ms, "get_categories", fake_cats)

    # Менеджер
    r_mgr = client.post("/api/stock", json={"initData": str(ids["mgr"])})
    p_mgr = r_mgr.json()["products"][0]
    assert p_mgr["sale_price"] == 200.0
    assert "cost_price" not in p_mgr
    # Boss
    r_boss = client.post("/api/stock", json={"initData": str(ids["boss"])})
    p_boss = r_boss.json()["products"][0]
    assert p_boss["cost_price"] == 150.0
