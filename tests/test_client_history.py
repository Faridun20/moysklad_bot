"""
Карточка клиента: история денег и состав заказов.

Раньше карточка отвечала «заказ #12 на 25 000» и молчала о том, ЧТО в этом
заказе и когда клиент вообще платил. Теперь `get_orders_by_agent` отдаёт
позиции (они всё равно грузятся ради суммы), а `get_agent_money_history` —
ленту платежей, сдач и возвратов по его заказам.

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _order(db, agent_id="AG-1", *, uid=1, items=((2, 100.0),), status="shipped"):
    oid = db.create_order(uid, "Manager", "")
    db.update_order_agent(oid, agent_id, "Клиент")
    for qty, price in items:
        db.add_order_item(oid, "Экскаватор JCB", "", qty, "шт", price)
    db.update_order_status(oid, status)
    return oid


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


# ─── Состав заказа в карточке ─────────────────────────────────────────────────


def test_orders_carry_their_items(isolated_db):
    """Главное, ради чего это делалось: видно, ЧТО клиент заказывал."""
    db = isolated_db
    _setup(db)
    _order(db, items=((3, 150.0), (1, 20.0)))

    orders = _run(db.get_orders_by_agent("AG-1"))
    assert len(orders) == 1
    items = orders[0]["items"]
    assert [i["name"] for i in items] == ["Экскаватор JCB", "Экскаватор JCB"]
    assert items[0]["quantity"] == 3
    assert items[0]["price_cents"] == 15000  # копейки, не float
    assert orders[0]["total_cents"] == 3 * 15000 + 1 * 2000


def test_order_without_items_is_not_broken(isolated_db):
    """Пустой черновик клиента не должен ронять карточку."""
    db = isolated_db
    _setup(db)
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "AG-1", "Клиент")

    orders = _run(db.get_orders_by_agent("AG-1"))
    assert orders[0]["items"] == []
    assert orders[0]["total_cents"] == 0


# ─── История денег ────────────────────────────────────────────────────────────


def test_history_shows_payments_of_this_client_only(isolated_db):
    """Платёж по чужому заказу в карточку попасть не должен."""
    db = isolated_db
    _setup(db)
    mine = _order(db, "AG-1")
    theirs = _order(db, "AG-2")
    db.add_payment(1, "u", "Manager", 500.0, "USD", "оплата", order_id=mine)
    db.add_payment(1, "u", "Manager", 700.0, "USD", "чужая", order_id=theirs)

    rows = _run(db.get_agent_money_history("AG-1"))
    assert [r["kind"] for r in rows] == ["payment"]
    assert rows[0]["amount"] == 500.0
    assert rows[0]["order_id"] == mine
    assert rows[0]["who"] == "Manager"


def test_history_includes_deposits_only_for_allocated_part(isolated_db):
    """Сдача может закрывать заказы разных клиентов — показываем ту часть,
    что пришлась на его заказы, иначе карточка врёт о сумме."""
    db = isolated_db
    _setup(db)
    mine = _order(db, "AG-1", items=((1, 300.0),))
    theirs = _order(db, "AG-2", items=((1, 700.0),))
    dep = _run(db.create_cash_deposit(1, 1000.0, allocations=[(mine, 300.0), (theirs, 700.0)]))
    assert dep["ok"], dep

    rows = _run(db.get_agent_money_history("AG-1"))
    deposits = [r for r in rows if r["kind"] == "deposit"]
    assert len(deposits) == 1
    assert deposits[0]["amount"] == 300.0  # не 1000


def test_history_includes_returns(isolated_db):
    db = isolated_db
    _setup(db)
    oid = _order(db, "AG-1", items=((2, 100.0),))
    items = _run(db.get_order_items(oid))
    res = _run(
        db.create_return(oid, "full", "брак", [(items[0]["id"], 2, 200.0)], "cash", 1)
    )
    assert res["ok"], res

    kinds = [r["kind"] for r in _run(db.get_agent_money_history("AG-1"))]
    assert "return" in kinds


def test_history_hides_movements_of_phantom_orders(isolated_db):
    """Заказ, удалённый в МойСклад, исключён из всех денежных итогов — его
    платежи не должны всплывать в карточке клиента."""
    db = isolated_db
    _setup(db)
    oid = _order(db, "AG-1")
    db.add_payment(1, "u", "Manager", 500.0, "USD", "", order_id=oid)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET ms_deleted_at = ? WHERE id = ?"), (db.now_str(), oid))
        conn.commit()

    assert _run(db.get_agent_money_history("AG-1")) == []


def test_history_is_newest_first(isolated_db):
    db = isolated_db
    _setup(db)
    oid = _order(db, "AG-1")
    db.add_payment(1, "u", "Manager", 100.0, "USD", "старый", order_id=oid)
    db.add_payment(1, "u", "Manager", 200.0, "USD", "новый", order_id=oid)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET created_at = ? WHERE comment = ?"),
                    ("2020-01-01 10:00:00", "старый"))
        conn.commit()

    rows = _run(db.get_agent_money_history("AG-1"))
    assert [r["note"] for r in rows] == ["новый", "старый"]


def test_history_empty_for_unknown_agent(isolated_db):
    db = isolated_db
    _setup(db)
    assert _run(db.get_agent_money_history("НЕТ-ТАКОГО")) == []
    assert _run(db.get_agent_money_history("")) == []


# ─── Ручка карточки ───────────────────────────────────────────────────────────


def _client(db, monkeypatch, uid, role="boss"):
    import webapp.server as server
    from services import moysklad

    importlib.reload(roles)
    db.set_role(uid, "u", "U", role)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})

    # Покупки из МС карточка тянет best-effort. Без заглушки тест ходит в сеть с
    # фейковым токеном: медленно и оставляет незакрытую сессию aiohttp.
    async def _no_purchases(_agent_id):
        return {"top_products": [], "recent": [], "total_cents": 0, "count": 0}

    monkeypatch.setattr(moysklad, "get_counterparty_purchases", _no_purchases)
    return TestClient(server.app)


def test_detail_returns_history_and_items(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    oid = _order(db, "AG-1", items=((2, 100.0),))
    db.add_payment(1, "u", "Manager", 500.0, "USD", "оплата", order_id=oid)

    client = _client(db, monkeypatch, 2)
    r = client.post("/api/clients/detail", json={"initData": "2", "agent_id": "AG-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["orders"][0]["items"][0]["name"] == "Экскаватор JCB"
    assert [h["kind"] for h in body["money_history"]] == ["payment"]


DEMAND_ID = "a1b2c3d4-1111-2222-3333-444455556666"


def _positions_stub(rows, monkeypatch):
    from services import moysklad

    async def _positions(demand_id):
        assert demand_id == DEMAND_ID
        return rows

    monkeypatch.setattr(moysklad, "get_shipment_positions", _positions)


def test_shipment_returns_its_contents(isolated_db, monkeypatch):
    """Отгрузка показывалась одной суммой — увидеть, ЧТО уехало, было нельзя."""
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 2)
    _positions_stub(
        [
            {"assortment": {"name": "Кабель PV 0.6"}, "quantity": 29, "price": 8000,
             "uom": {"name": "шт"}},
            {"assortment": {"name": "Автомат C16"}, "quantity": 2.5, "price": 15000},
        ],
        monkeypatch,
    )

    r = client.post("/api/clients/shipment", json={"initData": "2", "demand_id": DEMAND_ID})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["name"] for p in body["positions"]] == ["Кабель PV 0.6", "Автомат C16"]
    assert body["positions"][0]["sum_cents"] == 29 * 8000
    assert body["positions"][1]["unit"] == "шт"  # единицы нет в ответе МС — дефолт
    assert body["sum_cents"] == 29 * 8000 + int(round(2.5 * 15000))


def test_shipment_rejects_non_uuid(isolated_db, monkeypatch):
    """`demand_id` уходит в ПУТЬ запроса к МС: строка вида `../../entity/...`
    увела бы его в другую сущность."""
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 2)

    for bad in ("", "../../entity/counterparty/xxx", "12345"):
        r = client.post("/api/clients/shipment", json={"initData": "2", "demand_id": bad})
        assert r.status_code == 400, bad


def test_shipment_ms_failure_is_502(isolated_db, monkeypatch):
    """МС не ответил — это не поломка карточки: остальное в ней уже отрисовано."""
    from services import moysklad

    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 2)

    async def _boom(_demand_id):
        raise RuntimeError("MS 503")

    monkeypatch.setattr(moysklad, "get_shipment_positions", _boom)
    r = client.post("/api/clients/shipment", json={"initData": "2", "demand_id": DEMAND_ID})
    assert r.status_code == 502


def test_shipment_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 3, role="manager")
    r = client.post("/api/clients/shipment", json={"initData": "3", "demand_id": DEMAND_ID})
    assert r.status_code == 403


def test_detail_stays_boss_only(isolated_db, monkeypatch):
    """История платежей клиента — чувствительные данные; ручка как была
    admin/boss, так и остаётся."""
    db = isolated_db
    _setup(db)
    _order(db, "AG-1")

    client = _client(db, monkeypatch, 3, role="manager")
    r = client.post("/api/clients/detail", json={"initData": "3", "agent_id": "AG-1"})
    assert r.status_code == 403
