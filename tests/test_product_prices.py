"""
Тесты управления ценами (PR C): product_prices DB + API + enforcement.

Ключевое:
  * set/get/батч/кэш-инвалидация
  * валидация (отрицательные/inf/слишком большие)
  * enforcement минимума при add_item (ниже→400, выше→ок, prefill)
  * profit для boss, cost/profit НЕ утекает менеджеру

B7/D4 (прайс-листы и подсказка прошлой цены):
  * `get_last_price_for_agent_product` — последняя цена «клиент+товар»
  * `wholesale_price` («для постоянных») — хранение, выдача, пол цены
  * `/api/orders/price_hint` — приоритет подсказок и пустой ответ
"""

import asyncio
import importlib
import re

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
        json={"initData": str(ids["mgr"]), "product_id": 1, "sale_price": 100},
    )
    assert resp.status_code == 403


def test_prices_set_and_list_for_boss(client_env):
    client, db, ids = client_env
    pid = _product(db, "Товар")
    resp = client.post(
        "/api/products/prices/set",
        json={
            "initData": str(ids["boss"]),
            "product_id": pid,
            "product_name": "Товар",
            "sale_price": 150,
            "cost_price": 90,
        },
    )
    assert resp.status_code == 200, resp.text
    assert db.get_product_price(str(pid))["cost_price"] == 90.0
    # Список доступен boss
    lst = client.post("/api/products/prices", json={"initData": str(ids["boss"])})
    assert lst.status_code == 200
    assert any(r["ms_id"] == str(pid) for r in lst.json()["prices"])


def test_prices_list_forbidden_for_manager(client_env):
    client, _db, ids = client_env
    resp = client.post("/api/products/prices", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 403


def _make_draft(db, mgr_id):
    oid = db.create_order(mgr_id, "Manager", "")
    return oid


def _product(db, name):
    """Карточка товара → её id. Им же ключуется `product_prices` после перехода
    на локальный склад (колонка называется `ms_id`, но хранит наш id)."""
    from services import container_receipt

    return asyncio.run(container_receipt.create_product(name))["product_id"]


def test_add_item_enforces_minimum_price(client_env):
    """Цена ниже минимума → 400."""
    client, db, ids = client_env
    pid = _product(db, "Товар X")
    db.set_product_price(str(pid), "Товар X", 100.0, None, "USD", updated_by=ids["boss"])
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/add_item",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "product_name": "Товар X",
            "product_id": pid,
            "quantity": 2,
            "price": 80,  # ниже минимума 100
        },
    )
    assert resp.status_code == 400
    assert "минимальной" in resp.json()["detail"]


def test_add_item_allows_price_above_minimum(client_env):
    client, db, ids = client_env
    pid = _product(db, "Товар Y")
    db.set_product_price(str(pid), "Товар Y", 100.0, None, "USD", updated_by=ids["boss"])
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/add_item",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "product_name": "Товар Y",
            "product_id": pid,
            "quantity": 1,
            "price": 120,  # выше минимума — ок
        },
    )
    assert resp.status_code == 200, resp.text


def test_add_item_prefills_min_when_price_zero(client_env):
    """price не задан/0 → подставляется sale_price."""
    client, db, ids = client_env
    pid = _product(db, "Товар Z")
    db.set_product_price(str(pid), "Товар Z", 100.0, None, "USD", updated_by=ids["boss"])
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/add_item",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "product_name": "Товар Z",
            "product_id": pid,
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
    pid = _product(db, "Товар P")
    db.set_product_price(str(pid), "Товар P", 150.0, 100.0, "USD", updated_by=ids["boss"])
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.add_order_item(oid, "Товар P", "", 2, "шт", 150.0, product_id=pid)

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
    db.add_order_item(oid, "БезЦены", "", 1, "шт", 50.0, product_id=_product(db, "БезЦены"))
    r_boss = client.post("/api/orders", json={"initData": str(ids["boss"])})
    boss_order = next(o for o in r_boss.json()["orders"] if o["id"] == oid)
    assert boss_order["profit_partial"] is True


def test_stock_hides_cost_from_manager(client_env):
    """/api/stock: менеджер видит sale_price, но НЕ cost_price."""
    from services import warehouse

    client, db, ids = client_env
    pid = _product(db, "Складской")
    db.set_product_price(str(pid), "Складской", 200.0, 150.0, "USD", updated_by=ids["boss"])
    asyncio.run(warehouse.create_invoice(
        invoice_type="incoming",
        warehouse_id=asyncio.run(warehouse.default_warehouse_id()),
        items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))

    # Менеджер
    r_mgr = client.post("/api/stock", json={"initData": str(ids["mgr"])})
    p_mgr = r_mgr.json()["products"][0]
    assert p_mgr["sale_price"] == 200.0
    assert "cost_price" not in p_mgr
    # Boss
    r_boss = client.post("/api/stock", json={"initData": str(ids["boss"])})
    p_boss = r_boss.json()["products"][0]
    assert p_boss["cost_price"] == 150.0


def test_stock_shows_reserved_and_available(client_env):
    """Одобренный, но не отгруженный заказ держит товар: доступное меньше
    остатка. Раньше резерв приезжал из МойСклад, теперь считается по заказам."""
    from services import warehouse

    client, db, ids = client_env
    pid = _product(db, "Гвозди")
    asyncio.run(warehouse.create_invoice(
        invoice_type="incoming",
        warehouse_id=asyncio.run(warehouse.default_warehouse_id()),
        items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.add_order_item(oid, "Гвозди", "", 4, "шт", 1.0, product_id=pid)
    db.update_order_status(oid, "approved")

    row = client.post("/api/stock", json={"initData": str(ids["boss"])}).json()["products"][0]
    assert row["stock"] == 10
    assert row["reserve"] == 4
    assert row["available"] == 6


# ─── B7/D4: последняя цена клиент+товар ──────────────────────────────────────


def _sold_order(db, mgr_id, agent_id, pid, name, price, *, status="shipped", currency="USD"):
    """Заказ этому контрагенту с этой позицией, доведённый до `status`."""
    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, str(agent_id), "ООО Клиент")
    db.add_order_item(oid, name, "", 1, "шт", price, product_id=pid)
    if currency:
        db.update_order_currency(oid, currency)
    if status != "draft":
        db.update_order_status(oid, status)
    return oid


def _last_price(db, agent_id, pid):
    return asyncio.run(db.get_last_price_for_agent_product(str(agent_id), pid))


def test_last_price_returns_most_recent_order_for_this_agent(isolated_db):
    """Тому же клиенту тот же товар продавали дважды — подсказка от последней."""
    db = isolated_db
    pid = _product(db, "Кабель")
    _sold_order(db, 200, 7, pid, "Кабель", 40.0)
    _sold_order(db, 200, 7, pid, "Кабель", 45.0)
    res = _last_price(db, 7, pid)
    assert res["price"] == 45.0
    assert res["currency"] == "USD"
    assert res["price_cents"] == 4500


def test_last_price_is_per_agent_and_per_product(isolated_db):
    """Цена другого клиента и цена другого товара в подсказку не попадают."""
    db = isolated_db
    pid, other = _product(db, "Кабель"), _product(db, "Труба")
    _sold_order(db, 200, 7, pid, "Кабель", 45.0)
    _sold_order(db, 200, 8, pid, "Кабель", 99.0)     # другой клиент
    _sold_order(db, 200, 7, other, "Труба", 11.0)    # другой товар
    assert _last_price(db, 7, pid)["price"] == 45.0
    assert _last_price(db, 8, pid)["price"] == 99.0
    assert _last_price(db, 9, pid) is None


def test_last_price_ignores_draft_rejected_and_cancelled(isolated_db):
    """Черновик ещё правят, отклонённое и отменённое не состоялось —
    ценой клиенту это не называли."""
    db = isolated_db
    pid = _product(db, "Кабель")
    for status in ("draft", "rejected", "cancelled"):
        _sold_order(db, 200, 7, pid, "Кабель", 1.0, status=status)
    assert _last_price(db, 7, pid) is None
    _sold_order(db, 200, 7, pid, "Кабель", 45.0, status="pending")
    assert _last_price(db, 7, pid)["price"] == 45.0


def test_last_price_empty_without_history(isolated_db):
    """Пустая история и пустой контрагент — None, а не 0."""
    db = isolated_db
    pid = _product(db, "Кабель")
    assert _last_price(db, 7, pid) is None
    assert asyncio.run(db.get_last_price_for_agent_product("", pid)) is None


def test_last_price_skips_items_without_price(isolated_db):
    """Позиция без цены (0) подсказкой не становится — иначе она предлагала
    бы отдать товар даром."""
    db = isolated_db
    pid = _product(db, "Кабель")
    _sold_order(db, 200, 7, pid, "Кабель", 45.0)
    _sold_order(db, 200, 7, pid, "Кабель", 0.0)
    assert _last_price(db, 7, pid)["price"] == 45.0


# ─── B7: «цена для постоянных клиентов» ──────────────────────────────────────


def test_wholesale_price_roundtrips(isolated_db):
    db = isolated_db
    ok, err = db.set_product_price("p", "X", 100.0, 60.0, "USD", 1, 90.0)
    assert ok, err
    pp = db.get_product_price("p")
    assert pp["wholesale_price"] == 90.0
    assert pp["sale_price"] == 100.0


def test_wholesale_price_validated_like_the_others(isolated_db):
    ok, err = isolated_db.set_product_price("p", "X", 100.0, None, "USD", 1, -5.0)
    assert ok is False and "wholesale_price" in err


def test_wholesale_price_in_batch_and_list(isolated_db):
    db = isolated_db
    db.set_product_price("a", "A", 10.0, None, "USD", 1, 8.0)
    assert asyncio.run(db.get_product_prices_by_ids(["a"]))["a"]["wholesale_price"] == 8.0
    assert asyncio.run(db.get_all_product_prices())[0]["wholesale_price"] == 8.0


def test_prices_set_accepts_wholesale_for_boss(client_env):
    client, db, ids = client_env
    pid = _product(db, "Товар W")
    resp = client.post(
        "/api/products/prices/set",
        json={
            "initData": str(ids["boss"]),
            "product_id": pid,
            "product_name": "Товар W",
            "sale_price": 100,
            "wholesale_price": 90,
        },
    )
    assert resp.status_code == 200, resp.text
    assert db.get_product_price(str(pid))["wholesale_price"] == 90.0


def test_stock_shows_wholesale_to_manager(client_env):
    """«Для постоянных» — подсказка менеджеру, а не себестоимость: он её видит."""
    from services import warehouse

    client, db, ids = client_env
    pid = _product(db, "Складской W")
    db.set_product_price(str(pid), "Складской W", 200.0, 150.0, "USD", ids["boss"], 180.0)
    asyncio.run(warehouse.create_invoice(
        invoice_type="incoming",
        warehouse_id=asyncio.run(warehouse.default_warehouse_id()),
        items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))
    row = client.post("/api/stock", json={"initData": str(ids["mgr"])}).json()["products"][0]
    assert row["wholesale_price"] == 180.0
    assert "cost_price" not in row


def test_add_item_floor_drops_to_wholesale_when_set(client_env):
    """Цена для постоянных ниже обычной — и форма её предлагает. Значит пол
    минимума опускается до неё, иначе сервер отвергал бы собственную подсказку.
    Ниже самой «постоянной» — по-прежнему 400."""
    client, db, ids = client_env
    pid = _product(db, "Товар F")
    db.set_product_price(str(pid), "Товар F", 100.0, None, "USD", ids["boss"], 90.0)
    oid = _make_draft(db, ids["mgr"])
    body = {
        "initData": str(ids["mgr"]), "order_id": oid,
        "product_name": "Товар F", "product_id": pid, "quantity": 1,
    }
    assert client.post("/api/orders/add_item", json={**body, "price": 90}).status_code == 200
    resp = client.post("/api/orders/add_item", json={**body, "price": 89})
    assert resp.status_code == 400 and "минимальной" in resp.json()["detail"]


def test_add_item_prefill_stays_on_sale_price_with_wholesale_set(client_env):
    """Префилл сервера (price=0) не меняется — «для постоянных» это выбор
    менеджера в форме, а не новый дефолт."""
    client, db, ids = client_env
    pid = _product(db, "Товар FP")
    db.set_product_price(str(pid), "Товар FP", 100.0, None, "USD", ids["boss"], 90.0)
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/add_item",
        json={
            "initData": str(ids["mgr"]), "order_id": oid, "product_name": "Товар FP",
            "product_id": pid, "quantity": 1, "price": 0,
        },
    )
    assert resp.status_code == 200, resp.text
    assert asyncio.run(db.get_order_items(oid))[0]["price"] == 100.0


# ─── B7/D4: /api/orders/price_hint ───────────────────────────────────────────


def _hint(client, uid, oid, pid):
    resp = client.post(
        "/api/orders/price_hint",
        json={"initData": str(uid), "order_id": oid, "product_id": pid},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_price_hint_empty_when_nothing_known(client_env):
    """Ни истории, ни цены товара — три null'а. Поле в форме останется пустым,
    ровно как до B7."""
    client, db, ids = client_env
    pid = _product(db, "Товар H0")
    oid = _make_draft(db, ids["mgr"])
    assert _hint(client, ids["mgr"], oid, pid) == {
        "ok": True, "last": None, "default": None, "wholesale": None,
    }


def test_price_hint_returns_prices_without_history(client_env):
    """Первый заказ клиента: истории нет, но цены товара есть."""
    client, db, ids = client_env
    pid = _product(db, "Товар H1")
    db.set_product_price(str(pid), "Товар H1", 100.0, None, "USD", ids["boss"], 90.0)
    oid = _make_draft(db, ids["mgr"])
    db.update_order_agent(oid, "7", "ООО Клиент")
    body = _hint(client, ids["mgr"], oid, pid)
    assert body["last"] is None
    assert body["default"] == {"price": 100.0, "currency": "USD"}
    assert body["wholesale"] == {"price": 90.0, "currency": "USD"}


def test_price_hint_returns_last_price_for_this_agent(client_env):
    """Повторный клиент: приезжает и прошлая цена с датой, и цена товара —
    порядок выбирает фронт."""
    client, db, ids = client_env
    pid = _product(db, "Товар H2")
    db.set_product_price(str(pid), "Товар H2", 100.0, None, "USD", ids["boss"])
    _sold_order(db, ids["mgr"], 7, pid, "Товар H2", 45.0)
    oid = _make_draft(db, ids["mgr"])
    db.update_order_agent(oid, "7", "ООО Клиент")
    body = _hint(client, ids["mgr"], oid, pid)
    assert body["last"]["price"] == 45.0
    assert body["last"]["currency"] == "USD"
    assert re.fullmatch(r"\d{2}\.\d{2}", body["last"]["date"]), body["last"]["date"]
    assert body["default"]["price"] == 100.0


def test_price_hint_refuses_foreign_order(client_env):
    """Контрагент берётся из ЗАКАЗА, и чужой заказ не читается: иначе ручка
    отвечала бы «почём покупает вот этот клиент» кому угодно."""
    client, db, ids = client_env
    pid = _product(db, "Товар H3")
    oid = db.create_order(ids["boss"], "Boss", "")
    resp = client.post(
        "/api/orders/price_hint",
        json={"initData": str(ids["mgr"]), "order_id": oid, "product_id": pid},
    )
    assert resp.status_code == 403


def test_price_hint_requires_product(client_env):
    client, db, ids = client_env
    oid = _make_draft(db, ids["mgr"])
    resp = client.post(
        "/api/orders/price_hint", json={"initData": str(ids["mgr"]), "order_id": oid},
    )
    assert resp.status_code == 400
