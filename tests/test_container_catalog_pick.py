"""
Позиция контейнера — товар ИЗ КАТАЛОГА, а не свободный текст «как запомнил».

Жалоба с площадки: товар в контейнер вписывали руками, и менеджер ошибался в
названии — при оприходовании опечатка становилась второй карточкой товара или
позицией, молча выпавшей из прихода. Проверяем то, что это закрывает:

* сравнение названий не видит регистра, лишних пробелов и «ё»;
* «новый товар» с именем, которое в каталоге уже есть, не проходит молча —
  ручка возвращает найденные карточки (`needs_choice`);
* выбор по непривязанным позициям едет в запросе сверки/оприходования
  (`resolve`) и применяется ДО накладной: новые карточки заводятся, тёзки —
  привязываются к существующей, остаток приходит на правильные id;
* поиск каталога для шторки выбора отдаёт список сразу (`browse`), с остатком.

БД настоящая (isolated_db), корутины через asyncio.run.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "acc", "Bookkeeper", "bookkeeper")


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def _product(name, unit="шт"):
    from services import container_receipt

    return _run(container_receipt.create_product(name, unit=unit))["product_id"]


def _container(number="MSKU-1234567"):
    from services import containers

    res = _run(containers.create_container(number=number, created_by=2, creator_name="Boss"))
    assert res["ok"], res
    return res["container_id"]


def _item(cid, name, expected=10, **over):
    from services import containers

    res = _run(containers.add_item(cid, name=name, expected_qty=expected, **over))
    assert res["ok"], res
    return res["item_id"]


def _stock(db, pid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COALESCE(SUM(quantity), 0) FROM stock WHERE product_id = ?"), (pid,))
        return float(cur.fetchone()[0])


def _count(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return cur.fetchone()[0]


# ─── Сравнение названий ───────────────────────────────────────────────────────


def test_name_normalization_ignores_case_spaces_and_yo(isolated_db):
    from services import container_receipt as cr

    assert cr.normalize_name("  Ёлочный   КРОНШТЕЙН ") == cr.normalize_name("елочный кронштейн")
    assert cr.normalize_name("Кабель PV 0.6") != cr.normalize_name("Кабель PV 0.6 чёрный")


def test_same_name_products_finds_catalog_card_written_differently(isolated_db):
    """В каталоге из МойСклад встречаются двойные пробелы и «ё» — дубль всё
    равно дубль, а похожее название — не он."""
    from services import container_receipt as cr

    _setup(isolated_db)
    pid = _product("Ёрш  трубный 50мм")
    _product("Ёрш трубный 50мм усиленный")

    assert [p["id"] for p in _run(cr.same_name_products("ерш трубный 50МM".replace("M", "м")))] == [pid]
    assert _run(cr.same_name_products("Ерш трубный")) == []


def test_create_product_does_not_duplicate_a_yo_variant(isolated_db):
    from services import container_receipt as cr

    _setup(isolated_db)
    first = _run(cr.create_product("Ёлочный кронштейн"))
    again = _run(cr.create_product("елочный  кронштейн"))
    assert again["existed"] is True and again["product_id"] == first["product_id"]
    assert _count(isolated_db, "SELECT COUNT(*) FROM products") == 1


def test_receipt_matches_legacy_free_text_item_by_yo_insensitive_name(isolated_db):
    """Позиции, вписанные текстом до выбора из списка, всё ещё сопоставляются
    по точному названию — теперь и когда «ё» набрали через «е»."""
    from services import container_receipt as cr
    from services import containers

    _setup(isolated_db)
    pid = _product("Ёлочный кронштейн")
    cid = _container()
    item = _item(cid, "елочный кронштейн")
    _run(containers.mark_arrived(cid, user_id=2))
    _run(containers.set_arrived_quantities(cid, {item: 4}, user_id=2))

    res = _run(cr.receive(cid, user_id=2))
    assert res["ok"] and res["matched"] == 1 and res["unmatched"] == [], res
    assert _stock(isolated_db, pid) == 4


# ─── Поиск каталога для шторки выбора ─────────────────────────────────────────


def test_product_search_browse_lists_catalog_with_stock_before_typing(isolated_db, monkeypatch):
    from services import warehouse

    _setup(isolated_db)
    cable = _product("Кабель ВВГ", unit="м")
    _product("Ёлочный кронштейн")
    wid = _run(warehouse.default_warehouse_id())
    assert _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": cable, "quantity": 12.5, "price_cents": None}],
    ))["ok"]
    client = _client(monkeypatch)

    body = _post(client, "/api/products/search", 1, query="", browse=True).json()
    rows = {p["name"]: p for p in body["products"]}
    assert set(rows) == {"Кабель ВВГ", "Ёлочный кронштейн"}
    assert rows["Кабель ВВГ"]["quantity"] == 12.5 and rows["Кабель ВВГ"]["unit"] == "м"
    assert rows["Ёлочный кронштейн"]["quantity"] == 0

    # Один символ в шторке — уже фильтр; ё = е.
    found = _post(client, "/api/products/search", 1, query="елоч", browse=True).json()
    assert [p["name"] for p in found["products"]] == ["Ёлочный кронштейн"]
    # Подсказка под полем (без browse) по-прежнему молчит на пустом вводе.
    assert _post(client, "/api/products/search", 1, query="").json()["products"] == []


def test_product_search_finds_by_sku(isolated_db, monkeypatch):
    _setup(isolated_db)
    pid = _product("Фильтр масляный")
    with isolated_db.get_conn() as conn:
        cur = isolated_db.get_cursor(conn)
        cur.execute(isolated_db.q("UPDATE products SET sku = ? WHERE id = ?"), ("320/04133", pid))
        conn.commit()

    body = _post(_client(monkeypatch), "/api/products/search", 1, query="04133", browse=True).json()
    assert [p["product_id"] for p in body["products"]] == [pid]


def test_product_search_is_closed_to_bookkeeper(isolated_db, monkeypatch):
    _setup(isolated_db)
    r = _post(_client(monkeypatch), "/api/products/search", 3, query="", browse=True)
    assert r.status_code == 403


# ─── Позиция: новый товар с именем из каталога ───────────────────────────────


def test_item_add_offers_existing_card_instead_of_a_namesake(isolated_db, monkeypatch):
    _setup(isolated_db)
    pid = _product("Ёлочный кронштейн")
    cid = _container()
    client = _client(monkeypatch)

    r = _post(client, "/api/containers/item_add", 1, container_id=cid,
              name="ЕЛОЧНЫЙ  кронштейн", expected_qty=3)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["needs_choice"] is True
    assert [p["product_id"] for p in body["existing"]] == [pid]
    assert "уже есть" in body["detail"]
    assert _count(isolated_db, "SELECT COUNT(*) FROM container_items") == 0

    # Выбрали карточку — позиция заводится с привязкой.
    ok = _post(client, "/api/containers/item_add", 1, container_id=cid,
               name="Ёлочный кронштейн", expected_qty=3, product_id=pid)
    assert ok.status_code == 200, ok.text
    assert ok.json()["product_id"] == pid


def test_item_add_accepts_a_genuinely_new_product_as_free_text(isolated_db, monkeypatch):
    from services import containers

    _setup(isolated_db)
    _product("Кабель ВВГ")
    cid = _container()
    r = _post(_client(monkeypatch), "/api/containers/item_add", 1, container_id=cid,
              name="Кабель ВВГ медный", expected_qty=3)
    assert r.status_code == 200, r.text
    assert _run(containers.list_items(cid))[0]["product_id"] is None
    # Карточку НЕ заводим при добавлении позиции — только при оприходовании.
    assert _count(isolated_db, "SELECT COUNT(*) FROM products") == 1


def test_card_shows_catalog_matches_for_unlinked_items(isolated_db, monkeypatch):
    _setup(isolated_db)
    pid = _product("Ёлочный кронштейн")
    cid = _container()
    legacy = _item(cid, "елочный кронштейн")
    unknown = _item(cid, "Штекер тип C")
    linked = _item(cid, "Кабель", product_id=_product("Кабель"))

    items = {i["id"]: i for i in _post(_client(monkeypatch), "/api/containers/card", 1,
                                       container_id=cid).json()["items"]}
    assert [m["product_id"] for m in items[legacy]["catalog_matches"]] == [pid]
    assert items[unknown]["catalog_matches"] == []
    assert items[linked]["catalog_matches"] == []


# ─── Выбор перед оприходованием (resolve) ─────────────────────────────────────


def _arrived_container(db, client):
    """Контейнер с тремя позициями: из каталога, новая, тёзка существующей."""
    from services import containers

    cable = _product("Кабель ВВГ", unit="м")
    bracket = _product("Ёлочный кронштейн")
    cid = _container()
    linked = _item(cid, "Кабель ВВГ", product_id=cable)
    new = _item(cid, "Фильтр масляный JCB", unit="шт")
    namesake = _item(cid, "елочный кронштейн")
    _run(containers.mark_arrived(cid, user_id=2))
    return cid, {"cable": cable, "bracket": bracket}, {"linked": linked, "new": new, "namesake": namesake}


def test_check_with_resolve_creates_new_card_links_namesake_and_receives_all(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    cid, pids, items = _arrived_container(db, client)

    r = _post(client, "/api/containers/check", 1, container_id=cid,
              quantities={str(items["linked"]): 8, str(items["new"]): 5, str(items["namesake"]): 2},
              resolve={str(items["new"]): {"new": True},
                       str(items["namesake"]): {"product_id": pids["bracket"]}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"]["created"] == ["Фильтр масляный JCB"]
    assert body["receipt"]["ok"] and body["receipt"]["matched"] == 3
    assert body["receipt"]["unmatched"] == []

    filt = _count(db, "SELECT id FROM products WHERE name = ?", ("Фильтр масляный JCB",))
    assert _stock(db, pids["cable"]) == 8
    assert _stock(db, filt) == 5
    assert _stock(db, pids["bracket"]) == 2
    assert _count(db, "SELECT COUNT(*) FROM products") == 3, "дублей нет"
    # Накладная — на правильные id товаров.
    inv = body["receipt"]["invoice_id"]
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT product_id, quantity FROM invoice_items WHERE invoice_id = ? "
                         "ORDER BY product_id"), (inv,))
        got = {int(r[0]): float(r[1]) for r in cur.fetchall()}
    assert got == {pids["cable"]: 8, filt: 5, pids["bracket"]: 2}


def test_supply_with_resolve_new_for_a_namesake_links_the_existing_card(isolated_db, monkeypatch):
    """«Новый товар» для позиции, чьё имя в каталоге уже есть, дубля не
    заводит: привязывается существующая карточка."""
    from services import containers

    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    cid, pids, items = _arrived_container(db, client)
    _run(containers.set_arrived_quantities(
        cid, {items["linked"]: 1, items["new"]: 1, items["namesake"]: 6}, user_id=2))

    r = _post(client, "/api/containers/supply", 1, container_id=cid, idempotency_key="s1",
              resolve={str(items["new"]): {"new": True}, str(items["namesake"]): {"new": True}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["resolved"]["existed"] == ["Ёлочный кронштейн"]
    assert body["matched"] == 3
    assert _stock(db, pids["bracket"]) == 6
    assert _count(db, "SELECT COUNT(*) FROM products") == 3

    # Повтор с тем же ключом — итог первого, без второй накладной.
    again = _post(client, "/api/containers/supply", 1, container_id=cid, idempotency_key="s1",
                  resolve={str(items["new"]): {"new": True}})
    assert again.status_code == 200
    assert again.json()["invoice_id"] == body["invoice_id"]
    assert _count(db, "SELECT COUNT(*) FROM invoices WHERE status <> 'cancelled'") == 1


def test_resolve_refuses_item_of_another_container_before_any_write(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    cid, _pids, items = _arrived_container(db, client)
    other = _container("TCLU-7654321")
    stranger = _item(other, "Чужая позиция")

    r = _post(client, "/api/containers/supply", 1, container_id=cid,
              resolve={str(items["new"]): {"new": True}, str(stranger): {"new": True}})
    assert r.status_code == 400
    assert "не из этого контейнера" in r.json()["detail"]
    assert _count(db, "SELECT COUNT(*) FROM products") == 2, "новую карточку не завели"
    assert _count(db, "SELECT COUNT(*) FROM container_item_products WHERE item_id = ?",
                  (items["new"],)) == 0


def test_resolve_rejects_unknown_product_and_bad_shape(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    cid, _pids, items = _arrived_container(db, client)

    missing = _post(client, "/api/containers/supply", 1, container_id=cid,
                    resolve={str(items["new"]): {"product_id": 999}})
    assert missing.status_code == 404
    bad = _post(client, "/api/containers/supply", 1, container_id=cid,
                resolve={str(items["new"]): "new"})
    assert bad.status_code == 400
    assert _count(db, "SELECT COUNT(*) FROM invoices") == 0


def test_resolve_is_refused_after_the_edit_window_closes(isolated_db):
    from datetime import timedelta

    from services import container_receipt as cr
    from utils.helpers import local_now

    db = isolated_db
    _setup(db)
    cid, _pids, items = _arrived_container(db, None)
    past = (local_now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE containers SET arrived_at = ? WHERE id = ?"), (past, cid))
        conn.commit()

    res = _run(cr.resolve_items(cid, {items["namesake"]: {"product_id": _pids["bracket"]}}))
    assert res["ok"] is False and res.get("window_closed") is True
