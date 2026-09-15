"""B8 — несколько складов: справочник, перемещение остатка, выбор склада у
заказа/накладной, разбивка каталога.

Инвариант, который держит весь раздел: пока склад один (сегодняшний случай на
проде), поведение системы не меняется НИ НА БИТ — `default_warehouse_id()`,
`get_catalog()`/`/api/stock`, отгрузка заказа отвечают ровно так же, как до
этого раздела. Это проверяется явно (`test_single_warehouse_*`), а не
предполагается.

Мокать здесь нечего — БД настоящая (`isolated_db`), внешних границ у модуля
нет. API-тесты мокают только `verify_init_data`, как `test_warehouse_api.py`.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def wh(isolated_db):
    """Схема (склад #1 «Основной склад» уже засеян isolated_db) + пара товаров."""
    import importlib

    import services.warehouse as warehouse

    importlib.reload(warehouse)

    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for name, sku in (("Болт М8", "B8"), ("Гайка М8", "G8")):
            cur.execute(
                db.q("INSERT INTO products (name, sku, unit, created_at) VALUES (?, ?, ?, ?)"),
                (name, sku, "шт", db.now_str()),
            )
        conn.commit()
    return warehouse


async def _qty(product_id, warehouse_id):
    from services import adb_core

    v = await adb_core.fetchval(
        "SELECT quantity FROM stock WHERE product_id = $1 AND warehouse_id = $2",
        product_id, warehouse_id,
    )
    return float(v) if v is not None else 0.0


# ─── Справочник складов: CRUD ────────────────────────────────────────────────


def test_single_warehouse_by_default(wh):
    """isolated_db сеет ровно один склад — как на проде до этого раздела."""

    async def go():
        rows = await wh.list_warehouses(include_archived=False)
        assert len(rows) == 1
        assert rows[0]["name"] == "Основной склад"
        assert await wh.active_warehouse_count() == 1
        assert await wh.default_warehouse_id() == rows[0]["id"]

    _run(go())


def test_create_warehouse_and_dedupe_by_name(wh):
    async def go():
        res = await wh.create_warehouse("Склад в Бекабаде")
        assert res["ok"] and not res["existed"]
        wid = res["warehouse_id"]

        # Тёзка (регистр/пробелы не важны) — не заводится, отдаётся прежний id.
        dupe = await wh.create_warehouse("  склад в бекабаде  ")
        assert dupe["existed"] and dupe["warehouse_id"] == wid

        assert await wh.active_warehouse_count() == 2

    _run(go())


def test_create_warehouse_rejects_empty_name(wh):
    async def go():
        with pytest.raises(wh.WarehouseError) as exc:
            await wh.create_warehouse("   ")
        assert exc.value.code == "bad_name"

    _run(go())


def test_rename_warehouse(wh):
    async def go():
        res = await wh.create_warehouse("Второй склад")
        wid = res["warehouse_id"]
        renamed = await wh.rename_warehouse(wid, "Склад №2")
        assert renamed["ok"]
        rows = await wh.list_warehouses()
        assert any(r["id"] == wid and r["name"] == "Склад №2" for r in rows)

    _run(go())


def test_rename_unknown_warehouse_raises(wh):
    async def go():
        with pytest.raises(wh.WarehouseError) as exc:
            await wh.rename_warehouse(999, "Х")
        assert exc.value.code == "not_found"

    _run(go())


def test_archive_last_active_warehouse_refused(wh):
    """Архивировать ЕДИНСТВЕННЫЙ активный склад нельзя — иначе накладную
    провести стало бы некуда, и это молча ломает всё складское движение."""

    async def go():
        default_id = await wh.default_warehouse_id()
        with pytest.raises(wh.WarehouseError) as exc:
            await wh.archive_warehouse(default_id)
        assert exc.value.code == "last_active_warehouse"

    _run(go())


def test_archive_warehouse_with_nonzero_stock_refused(wh):
    async def go():
        default_id = await wh.default_warehouse_id()
        second = (await wh.create_warehouse("Второй склад"))["warehouse_id"]
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=default_id,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
        )
        with pytest.raises(wh.WarehouseError) as exc:
            await wh.archive_warehouse(default_id)
        assert exc.value.code == "nonzero_stock"
        # Второй склад пуст — архивируется без проблем.
        res = await wh.archive_warehouse(second)
        assert res["ok"]

    _run(go())


def test_archive_then_unarchive_roundtrip(wh):
    async def go():
        default_id = await wh.default_warehouse_id()
        second = (await wh.create_warehouse("Второй склад"))["warehouse_id"]
        await wh.archive_warehouse(second)
        rows = await wh.list_warehouses(include_archived=False)
        assert all(r["id"] != second for r in rows)
        # Архивный склад больше не кандидат в default_warehouse_id, даже если
        # у него меньший id.
        assert await wh.default_warehouse_id() == default_id

        restored = await wh.unarchive_warehouse(second)
        assert restored["restored"]
        rows2 = await wh.list_warehouses(include_archived=False)
        assert any(r["id"] == second for r in rows2)

    _run(go())


def test_create_invoice_refuses_archived_warehouse(wh):
    """Прямой вызов с id архивного склада (в обход пикера, который его не
    предлагает) не должен тихо провести накладную на закрытый склад."""

    async def go():
        second = (await wh.create_warehouse("Второй склад"))["warehouse_id"]
        await wh.archive_warehouse(second)
        res = await wh.create_invoice(
            invoice_type="incoming", warehouse_id=second,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
        )
        assert not res["ok"]
        assert res["code"] == "archived_warehouse"
        assert await _qty(1, second) == 0.0

    _run(go())


# ─── Перемещение остатка ─────────────────────────────────────────────────────


def test_transfer_moves_stock_atomically(wh):
    async def go():
        a = await wh.default_warehouse_id()
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=a,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
        res = await wh.transfer_stock(
            product_id=1, quantity=4, from_warehouse_id=a, to_warehouse_id=b,
        )
        assert res["ok"], res
        assert await _qty(1, a) == 6.0
        assert await _qty(1, b) == 4.0

        rows = await wh.list_stock_transfers()
        assert len(rows) == 1
        assert rows[0]["quantity"] == 4.0
        assert rows[0]["from_warehouse_id"] == a
        assert rows[0]["to_warehouse_id"] == b

    _run(go())


def test_transfer_never_goes_negative(wh):
    """Нехватка на складе отправления откатывает перемещение целиком — остаток
    обоих складов остаётся как был, ни один не уходит в минус."""

    async def go():
        a = await wh.default_warehouse_id()
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=a,
            items=[{"product_id": 1, "quantity": 3, "price_cents": 100}],
        )
        res = await wh.transfer_stock(
            product_id=1, quantity=10, from_warehouse_id=a, to_warehouse_id=b,
        )
        assert not res["ok"]
        assert res["code"] == "insufficient_stock"
        assert await _qty(1, a) == 3.0
        assert await _qty(1, b) == 0.0

    _run(go())


def test_transfer_same_warehouse_refused(wh):
    async def go():
        a = await wh.default_warehouse_id()
        res = await wh.transfer_stock(
            product_id=1, quantity=1, from_warehouse_id=a, to_warehouse_id=a,
        )
        assert not res["ok"] and res["code"] == "same_warehouse"

    _run(go())


def test_transfer_bad_quantity_refused(wh):
    async def go():
        a = await wh.default_warehouse_id()
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        for bad in (0, -1):
            res = await wh.transfer_stock(
                product_id=1, quantity=bad, from_warehouse_id=a, to_warehouse_id=b,
            )
            assert not res["ok"] and res["code"] == "bad_quantity"

    _run(go())


def test_transfer_unknown_or_archived_warehouse_refused(wh):
    async def go():
        a = await wh.default_warehouse_id()
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        res = await wh.transfer_stock(
            product_id=1, quantity=1, from_warehouse_id=a, to_warehouse_id=999,
        )
        assert not res["ok"] and res["code"] == "unknown_warehouse"

        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=a,
            items=[{"product_id": 1, "quantity": 1, "price_cents": 100}],
        )
        await wh.archive_warehouse(b)
        res2 = await wh.transfer_stock(
            product_id=1, quantity=1, from_warehouse_id=a, to_warehouse_id=b,
        )
        assert not res2["ok"] and res2["code"] == "archived_warehouse"

    _run(go())


def test_transfer_unknown_product_refused(wh):
    async def go():
        a = await wh.default_warehouse_id()
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        res = await wh.transfer_stock(
            product_id=999, quantity=1, from_warehouse_id=a, to_warehouse_id=b,
        )
        assert not res["ok"] and res["code"] == "unknown_product"

    _run(go())


# ─── Каталог: разбивка по складам ────────────────────────────────────────────


def test_stock_breakdown_only_meaningful_with_two_warehouses(wh):
    async def go():
        a = await wh.default_warehouse_id()
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=a,
            items=[{"product_id": 1, "quantity": 7, "price_cents": 100}],
        )
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=b,
            items=[{"product_id": 1, "quantity": 3, "price_cents": 100}],
        )
        breakdown = await wh.stock_breakdown([1])
        by_wh = {row["warehouse_id"]: row["quantity"] for row in breakdown[1]}
        assert by_wh == {a: 7.0, b: 3.0}
        # Каталог остаётся суммой по складам — доступное считается по общему
        # остатку, как раньше.
        catalog = await wh.get_catalog()
        row = next(r for r in catalog if r["product_id"] == 1)
        assert row["quantity"] == 10.0

    _run(go())


# ─── Заказ: выбор склада отгрузки ────────────────────────────────────────────


def test_order_warehouse_defaults_to_default_warehouse(wh):
    """Без явного выбора (однoскладской случай) — resolve совпадает с
    `default_warehouse_id()`, и `order_warehouse` не заводит ни одной строки."""

    async def go():
        assert await wh.get_order_warehouse(42) is None
        default_id = await wh.default_warehouse_id()
        assert await wh.resolve_order_warehouse(42) == default_id

    _run(go())


def test_order_warehouse_explicit_choice_wins(wh):
    async def go():
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        await wh.set_order_warehouse(7, b)
        assert await wh.get_order_warehouse(7) == b
        assert await wh.resolve_order_warehouse(7) == b

        # Повторный выбор — UPSERT, не дублирует строку.
        a = await wh.default_warehouse_id()
        await wh.set_order_warehouse(7, a)
        assert await wh.get_order_warehouse(7) == a

    _run(go())


def test_order_warehouse_rejects_archived(wh):
    async def go():
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        await wh.archive_warehouse(b)
        with pytest.raises(wh.WarehouseError) as exc:
            await wh.set_order_warehouse(1, b)
        assert exc.value.code == "unknown_warehouse"

    _run(go())


def test_ship_order_uses_resolved_warehouse(wh):
    """`order_shipment.ship_order` списывает СО СКЛАДА, выбранного менеджером,
    а не всегда с дефолтного — конец до конца, не только сервис warehouse."""

    async def go():
        import services.database as db
        import services.order_shipment as order_shipment

        a = await wh.default_warehouse_id()
        b = (await wh.create_warehouse("Склад Б"))["warehouse_id"]
        await wh.create_invoice(
            invoice_type="incoming", warehouse_id=b,
            items=[{"product_id": 1, "quantity": 5, "price_cents": 100}],
        )
        order_id = db.create_order(1, "Manager", "")
        await wh.set_order_warehouse(order_id, b)
        # ship_order списывает только одобренный заказ (_SHIPPABLE_STATUSES) —
        # свежий db.create_order даёт 'draft', его нужно перевести вручную,
        # как это в реальном потоке делает одобрение заявки.
        from services import adb_core

        await adb_core.execute(
            "UPDATE orders SET status = 'approved' WHERE id = $1", order_id
        )
        result = await order_shipment.ship_order(
            {"id": order_id, "agent_id": "", "currency": "USD"},
            [{"product_id": 1, "quantity": 2, "price_cents": 500}],
        )
        assert result["ok"], result
        assert await _qty(1, b) == 3.0
        assert await _qty(1, a) == 0.0

    _run(go())


# ═══════════════════════════════════════════════════════════════════════════
# API: права, идемпотентность, форма ответа /api/stock не меняется при одном
# складе.
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def api(isolated_db, monkeypatch):
    import importlib

    import services.rate_limit as rate_limit
    import services.roles as roles
    import services.warehouse as warehouse
    import webapp.server as server

    importlib.reload(roles)
    importlib.reload(warehouse)
    rate_limit.reset()

    db = isolated_db
    ids = {"admin": 1, "boss": 100, "mgr": 200, "mgr2": 201, "guest": 300}
    db.set_role(ids["admin"], "admin_user", "Admin", "admin")
    db.set_role(ids["boss"], "boss_user", "Boss", "boss")
    db.set_role(ids["mgr"], "mgr_user", "Manager", "manager")
    db.set_role(ids["mgr2"], "mgr2_user", "Manager2", "manager")
    db.set_role(ids["guest"], "guest_user", "Guest", "guest")

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for name, sku in (("Болт М8", "B8"), ("Гайка М8", "G8")):
            cur.execute(
                db.q("INSERT INTO products (name, sku, unit, created_at) VALUES (?, ?, ?, ?)"),
                (name, sku, "шт", db.now_str()),
            )
        conn.commit()

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, ids


def _post(client, path, uid, **body):
    body.setdefault("initData", str(uid))
    return client.post(path, json=body)


def test_me_reports_multi_warehouse_flag(api):
    """`/api/me` — источник для фронта («показывать ли пикер склада»):
    флаг переключается вместе с числом активных складов."""
    client, db, ids = api
    r = _post(client, "/api/me", ids["mgr"])
    assert r.status_code == 200
    assert r.json()["multi_warehouse"] is False

    _post(client, "/api/warehouses/create", ids["boss"], name="Склад Б")
    r2 = _post(client, "/api/me", ids["mgr"])
    assert r2.json()["multi_warehouse"] is True


def test_stock_response_unchanged_shape_with_one_warehouse(api):
    """Форма ответа `/api/stock` не должна получить НИ ОДНОГО нового поля,
    пока склад один — иначе фронт видит перемену там, где её нет."""
    client, db, ids = api
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)"),
                    (1, 1, 5))
        conn.commit()
    r = _post(client, "/api/stock", ids["mgr"])
    assert r.status_code == 200
    body = r.json()
    assert body["multi_warehouse"] is False
    item = next(p for p in body["products"] if p["product_id"] == 1)
    assert "by_warehouse" not in item
    assert item["stock"] == 5.0


def test_stock_response_includes_breakdown_with_two_warehouses(api):
    client, db, ids = api
    r = _post(client, "/api/warehouses/create", ids["boss"], name="Склад Б")
    assert r.status_code == 200
    wid = r.json()["warehouse_id"]
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)"),
                    (1, 1, 5))
        cur.execute(db.q("INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)"),
                    (1, wid, 2))
        conn.commit()
    r = _post(client, "/api/stock", ids["mgr"])
    body = r.json()
    assert body["multi_warehouse"] is True
    item = next(p for p in body["products"] if p["product_id"] == 1)
    assert item["stock"] == 7.0
    by_id = {row["warehouse_id"]: row["quantity"] for row in item["by_warehouse"]}
    assert by_id == {1: 5.0, wid: 2.0}


def test_warehouses_crud_requires_boss(api):
    client, db, ids = api
    assert _post(client, "/api/warehouses/create", ids["mgr"], name="X").status_code == 403
    assert _post(client, "/api/warehouses/create", ids["guest"], name="X").status_code == 403
    r = _post(client, "/api/warehouses/create", ids["boss"], name="Склад Б")
    assert r.status_code == 200
    wid = r.json()["warehouse_id"]

    assert _post(client, "/api/warehouses/rename", ids["mgr"], warehouse_id=wid, name="Y").status_code == 403
    r2 = _post(client, "/api/warehouses/rename", ids["admin"], warehouse_id=wid, name="Склад В")
    assert r2.status_code == 200 and r2.json()["name"] == "Склад В"

    assert _post(client, "/api/warehouses/archive", ids["mgr"], warehouse_id=wid).status_code == 403
    r3 = _post(client, "/api/warehouses/archive", ids["boss"], warehouse_id=wid)
    assert r3.status_code == 200


def test_warehouses_active_open_to_manager(api):
    client, db, ids = api
    r = _post(client, "/api/warehouses/active", ids["mgr"])
    assert r.status_code == 200
    assert len(r.json()["warehouses"]) == 1
    assert _post(client, "/api/warehouses/active", ids["guest"]).status_code == 403


def test_stock_transfer_manager_allowed_guest_forbidden(api):
    client, db, ids = api
    wid = _post(client, "/api/warehouses/create", ids["boss"], name="Склад Б").json()["warehouse_id"]
    default_id = 1

    assert _post(
        client, "/api/stock/transfer", ids["guest"],
        product_id=1, quantity=1, from_warehouse_id=default_id, to_warehouse_id=wid,
    ).status_code == 403

    # Нет остатка — 409 с причиной, ничего не списалось.
    r = _post(
        client, "/api/stock/transfer", ids["mgr"],
        product_id=1, quantity=5, from_warehouse_id=default_id, to_warehouse_id=wid,
    )
    assert r.status_code == 409
    assert r.json()["code"] == "insufficient_stock"

    # Приход, затем успешное перемещение.
    _post(client, "/api/wh/invoices/create", ids["mgr"], type="incoming", warehouse_id=default_id,
          items=[{"product_id": 1, "quantity": 10, "price_cents": 100}])
    r2 = _post(
        client, "/api/stock/transfer", ids["mgr"],
        product_id=1, quantity=4, from_warehouse_id=default_id, to_warehouse_id=wid,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["quantity"] == 4.0


def test_stock_transfer_idempotent(api):
    client, db, ids = api
    wid = _post(client, "/api/warehouses/create", ids["boss"], name="Склад Б").json()["warehouse_id"]
    _post(client, "/api/wh/invoices/create", ids["mgr"], type="incoming", warehouse_id=1,
          items=[{"product_id": 1, "quantity": 10, "price_cents": 100}])
    key = "same-key-123"
    r1 = _post(client, "/api/stock/transfer", ids["mgr"], product_id=1, quantity=3,
               from_warehouse_id=1, to_warehouse_id=wid, idempotency_key=key)
    r2 = _post(client, "/api/stock/transfer", ids["mgr"], product_id=1, quantity=3,
               from_warehouse_id=1, to_warehouse_id=wid, idempotency_key=key)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    # Повтор с тем же ключом не переместил товар дважды — на складе назначения
    # ровно одна порция (3), а не 6.
    assert _run(_qty(1, wid)) == 3.0


def test_stock_transfers_history_boss_only(api):
    client, db, ids = api
    assert _post(client, "/api/stock/transfers", ids["mgr"]).status_code == 403
    assert _post(client, "/api/stock/transfers", ids["boss"]).status_code == 200


def test_order_set_warehouse_draft_only_and_own_order(api):
    client, db, ids = api
    wid = _post(client, "/api/warehouses/create", ids["boss"], name="Склад Б").json()["warehouse_id"]
    order_id = db.create_order(ids["mgr"], "Manager", "")

    # Чужой заказ — 403.
    assert _post(client, "/api/orders/set_warehouse", ids["mgr2"],
                 order_id=order_id, warehouse_id=wid).status_code == 403

    r = _post(client, "/api/orders/set_warehouse", ids["mgr"], order_id=order_id, warehouse_id=wid)
    assert r.status_code == 200

    # Заказ больше не черновик — выбор склада отклоняется.
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET status = 'approved' WHERE id = ?"), (order_id,))
        conn.commit()
    r2 = _post(client, "/api/orders/set_warehouse", ids["mgr"], order_id=order_id, warehouse_id=wid)
    assert r2.status_code in (400, 409)
