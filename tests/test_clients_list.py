"""Список ПОКУПАТЕЛЕЙ («Клиенты» в WebApp): `/api/clients/list`.

Жалоба владельца, с которой начался раздел: «Я нигде не нашёл, где можно
посмотреть клиентов. Сколько отдано, когда была проведена отгрузка, на какую
общую сумму он покупал. Где эти все данные?» Данные были — списка не было:
раздел с именем «Клиенты» вёл в воронку ОБРАЩЕНИЙ.

Здесь проверяется ровно то, на что смотрит владелец: итог покупок (за всё
время, по валютам — складывать USD и UZS нельзя), текущий долг (та же цифра,
что в `/api/debts`), дата последней отгрузки, порядок строк и права.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _client(db, monkeypatch, uid, role="manager"):
    import webapp.server as server

    importlib.reload(roles)
    db.set_role(uid, "u", "U", role)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _counterparty(name, phone=""):
    from services import counterparties as cp

    return str(_run(cp.create(name, phone=phone or None))["counterparty_id"])


def _ship(db, counterparty_id, *, price_cents, qty=1, currency="USD", date=None, name="Товар"):
    """Приход + расходная накладная на контрагента → id накладной."""
    from services import container_receipt, warehouse

    pid = _run(container_receipt.create_product(name))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": qty, "price_cents": None}],
    ))
    res = _run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid, counterparty_id=int(counterparty_id),
        items=[{"product_id": pid, "quantity": qty, "price_cents": price_cents}],
        currency=currency,
    ))
    assert res["ok"], res
    if date:
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(db.q("UPDATE invoices SET invoice_date = ? WHERE id = ?"),
                        (date, res["invoice_id"]))
            conn.commit()
    return res["invoice_id"]


def _debt_order(db, agent_id, agent_name, price, qty, mgr=2, currency="USD"):
    """Отгруженный неоплаченный заказ → долг контрагента."""
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, agent_id, agent_name)
    db.add_order_item(oid, "Товар", "", qty, "шт", price)
    if currency != "USD":
        db.update_order_currency(oid, currency)
    db.update_order_status(oid, "shipped")
    return oid


def _rows(client, uid, **body):
    r = client.post("/api/clients/list", json={"initData": str(uid), **body})
    assert r.status_code == 200, r.text
    return r.json()


# ─── Итоги ────────────────────────────────────────────────────────────────────


def test_list_shows_lifetime_total_debt_and_last_shipment(isolated_db, monkeypatch):
    db = isolated_db
    cid = _counterparty("ООО Ромашка", "+998901234567")
    _ship(db, cid, price_cents=400000, date="2026-05-14")
    _ship(db, cid, price_cents=850000, date="2026-09-01")
    _debt_order(db, cid, "ООО Ромашка", 190.0, 2)

    client = _client(db, monkeypatch, 800, "manager")
    body = _rows(client, 800)
    row = next(c for c in body["clients"] if c["agent_id"] == cid)
    assert row["name"] == "ООО Ромашка"
    assert row["phone"] == "+998901234567"
    # Итог — по ОБЕИМ накладным, а не по последней.
    assert row["bought_by_currency"] == [{"currency": "USD", "amount_cents": 1250000}]
    assert row["bought_base"] == 12500.0
    assert row["shipments"] == 2
    assert row["last_shipment"] == "2026-09-01"
    assert row["debt"] == 380.0


def test_list_does_not_add_up_different_currencies(isolated_db, monkeypatch):
    """USD и UZS в одну сумму не складываются — по строке на валюту."""
    db = isolated_db
    cid = _counterparty("Мульти")
    _ship(db, cid, price_cents=100000, currency="USD")
    _ship(db, cid, price_cents=5_000_000_00, currency="UZS", name="Товар-2")

    client = _client(db, monkeypatch, 801, "boss")
    row = next(c for c in _rows(client, 801)["clients"] if c["agent_id"] == cid)
    by_cur = {x["currency"]: x["amount_cents"] for x in row["bought_by_currency"]}
    assert by_cur == {"USD": 100000, "UZS": 5_000_000_00}
    assert row["shipments"] == 2


def test_list_debt_matches_debts_endpoint(isolated_db, monkeypatch):
    """Долг в списке — та же цифра, что считает `/api/debts` (одна формула:
    `get_agents_current_debt`). Второй ответ на тот же вопрос расходился бы."""
    db = isolated_db
    cid = _counterparty("Должник")
    _debt_order(db, cid, "Должник", 100.0, 3)

    client = _client(db, monkeypatch, 802, "boss")
    row = next(c for c in _rows(client, 802)["clients"] if c["agent_id"] == cid)
    debts = client.post("/api/debts", json={"initData": "802", "mode": "all"}).json()["debts"]
    total = sum(float(d["remaining"] if d["remaining"] > 0 else d["total"]) for d in debts)
    assert row["debt"] == total == 300.0


def test_client_without_orders_stays_in_the_list(isolated_db, monkeypatch):
    """Контрагент без отгрузок и долга не выпадает — он просто в конце.
    «Его тут нет» читалось бы как «его не завели»."""
    db = isolated_db
    quiet = _counterparty("Тихий клиент")

    client = _client(db, monkeypatch, 803, "manager")
    body = _rows(client, 803)
    row = next(c for c in body["clients"] if c["agent_id"] == quiet)
    assert row["bought_by_currency"] == []
    assert row["last_shipment"] is None
    assert row["debt"] == 0.0


# ─── Порядок и поиск ──────────────────────────────────────────────────────────


def test_debtors_go_first_then_recent_shipments(isolated_db, monkeypatch):
    db = isolated_db
    quiet = _counterparty("Аноним без долга")
    fresh = _counterparty("Бодрый покупатель")
    debtor = _counterparty("Яков Должников")
    _ship(db, fresh, price_cents=100000, date="2026-09-10")
    _ship(db, quiet, price_cents=100000, date="2026-01-10")
    _debt_order(db, debtor, "Яков Должников", 500.0, 1)

    client = _client(db, monkeypatch, 804, "boss")
    order = [c["agent_id"] for c in _rows(client, 804)["clients"]]
    # Должник — первым (с ним разбираться), дальше — по свежести отгрузки.
    assert order[0] == debtor
    assert order.index(fresh) < order.index(quiet)


def test_search_by_name_and_by_phone(isolated_db, monkeypatch):
    db = isolated_db
    _counterparty("ООО Ромашка", "+998 90 123-45-67")
    _counterparty("Азиз Рахимов", "+998935550011")

    client = _client(db, monkeypatch, 805, "manager")
    by_name = _rows(client, 805, q="ромашка")
    assert [c["name"] for c in by_name["clients"]] == ["ООО Ромашка"]
    # Номер лежит свободным текстом со скобками и дефисами — ищем по цифрам.
    by_phone = _rows(client, 805, q="901234567")
    assert [c["name"] for c in by_phone["clients"]] == ["ООО Ромашка"]
    assert _rows(client, 805, q="нет такого")["clients"] == []


def test_limit_caps_the_answer_but_total_says_how_many_there_are(isolated_db, monkeypatch):
    db = isolated_db
    for i in range(5):
        _counterparty(f"Клиент {i}")

    client = _client(db, monkeypatch, 806, "manager")
    body = _rows(client, 806, limit=2)
    assert len(body["clients"]) == 2
    assert body["shown"] == 2
    assert body["total"] == 5


# ─── Права ───────────────────────────────────────────────────────────────────


def test_list_allowed_for_manager_and_boss(isolated_db, monkeypatch):
    db = isolated_db
    for uid, role in ((810, "manager"), (811, "boss"), (812, "admin")):
        client = _client(db, monkeypatch, uid, role)
        r = client.post("/api/clients/list", json={"initData": str(uid)})
        assert r.status_code == 200, (role, r.text)


def test_list_forbidden_for_warehouse_keeper_and_bookkeeper(isolated_db, monkeypatch):
    """Та же тройка ролей, что у `/api/clients/detail` и `/api/search`."""
    db = isolated_db
    for uid, role in ((813, "warehouse_keeper"), (814, "bookkeeper")):
        client = _client(db, monkeypatch, uid, role)
        r = client.post("/api/clients/list", json={"initData": str(uid)})
        assert r.status_code == 403, (role, r.text)


def test_supplier_who_bought_is_still_a_buyer(isolated_db, monkeypatch):
    """Поставщик, которому однажды продали, из списка не выпадает.

    Тип в справочнике — не приговор: отсутствие такого контрагента в поиске
    читалось бы как «его не завели», а долг по нему живой.
    """
    db = isolated_db
    from services import counterparties as cp

    sup = str(_run(cp.create("ООО Поставщик-Покупатель", cp_type="supplier"))["counterparty_id"])
    _debt_order(db, sup, "ООО Поставщик-Покупатель", 50.0, 1)

    client = _client(db, monkeypatch, 807, "boss")
    row = next(c for c in _rows(client, 807)["clients"] if c["agent_id"] == sup)
    assert row["debt"] == 50.0
    # А поставщик БЕЗ продаж списка не засоряет.
    _run(cp.create("ООО Просто Поставщик", cp_type="supplier"))
    names = [c["name"] for c in _rows(client, 807)["clients"]]
    assert "ООО Просто Поставщик" not in names
