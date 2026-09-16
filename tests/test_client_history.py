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

    importlib.reload(roles)
    db.set_role(uid, "u", "U", role)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
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


def _outgoing_invoice(db, positions, counterparty_id=None):
    """Провести расходную накладную и вернуть её id. Позиции — (имя, кол-во, копейки)."""
    from services import container_receipt, warehouse

    items = []
    for name, qty, price_cents in positions:
        pid = _run(container_receipt.create_product(name))["product_id"]
        # Приход, чтобы было что отгружать: расход в минус не уходит.
        items.append({"product_id": pid, "quantity": qty, "price_cents": price_cents})
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{**it, "price_cents": None} for it in items],
    ))
    res = _run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid, items=items,
        counterparty_id=counterparty_id,
    ))
    assert res["ok"], res
    return res["invoice_id"]


def test_shipment_returns_its_contents(isolated_db, monkeypatch):
    """Отгрузка показывалась одной суммой — увидеть, ЧТО уехало, было нельзя."""
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 2)
    invoice_id = _outgoing_invoice(
        db, [("Кабель PV 0.6", 29, 8000), ("Автомат C16", 2.5, 15000)]
    )

    r = client.post("/api/clients/shipment", json={"initData": "2", "invoice_id": invoice_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["name"] for p in body["positions"]] == ["Кабель PV 0.6", "Автомат C16"]
    assert body["positions"][0]["sum_cents"] == 29 * 8000
    assert body["positions"][1]["unit"] == "шт"
    assert body["sum_cents"] == 29 * 8000 + int(round(2.5 * 15000))


def test_shipment_rejects_missing_id(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 2)

    for bad in ("", "не число", 0):
        r = client.post("/api/clients/shipment", json={"initData": "2", "invoice_id": bad})
        assert r.status_code == 400, bad


def test_shipment_unknown_invoice_is_404(isolated_db, monkeypatch):
    """Накладной нет — это не поломка карточки: остальное в ней уже отрисовано."""
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 2)
    r = client.post("/api/clients/shipment", json={"initData": "2", "invoice_id": 999999})
    assert r.status_code == 404


def test_shipment_refuses_an_incoming_invoice(isolated_db, monkeypatch):
    """Приход — не отгрузка клиента: показывать его в карточке значит выдать
    закупку за продажу."""
    from services import container_receipt, warehouse

    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 2)
    pid = _run(container_receipt.create_product("Кабель PV 0.6"))["product_id"]
    res = _run(warehouse.create_invoice(
        invoice_type="incoming",
        warehouse_id=_run(warehouse.default_warehouse_id()),
        items=[{"product_id": pid, "quantity": 5, "price_cents": None}],
    ))
    r = client.post(
        "/api/clients/shipment", json={"initData": "2", "invoice_id": res["invoice_id"]}
    )
    assert r.status_code == 404


def test_shipment_allowed_for_manager(isolated_db, monkeypatch):
    """A3: карточка клиента (и раскрытие состава отгрузки в ней) открыта на
    чтение и менеджеру — не только admin/boss."""
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 3, role="manager")
    r = client.post("/api/clients/shipment", json={"initData": "3", "invoice_id": 1})
    assert r.status_code == 404  # роль пройдена — упёрлись в несуществующую накладную


def test_shipment_forbidden_for_warehouse_keeper(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 4, role="warehouse_keeper")
    r = client.post("/api/clients/shipment", json={"initData": "4", "invoice_id": 1})
    assert r.status_code == 403


def test_detail_allowed_for_manager(isolated_db, monkeypatch):
    """A3: карточка контрагента (баланс + история покупок) — тоже менеджеру,
    на чтение. Не новая утечка: заказы и контрагентов он и так видит по
    отдельности, карточка лишь агрегирует уже доступное."""
    db = isolated_db
    _setup(db)
    _order(db, "AG-1")

    client = _client(db, monkeypatch, 3, role="manager")
    r = client.post("/api/clients/detail", json={"initData": "3", "agent_id": "AG-1"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_detail_forbidden_for_warehouse_keeper(isolated_db, monkeypatch):
    """Роль вне admin/boss/manager по-прежнему без доступа."""
    db = isolated_db
    _setup(db)
    client = _client(db, monkeypatch, 4, role="warehouse_keeper")
    r = client.post("/api/clients/detail", json={"initData": "4", "agent_id": "AG-1"})
    assert r.status_code == 403


# ─── «Сколько отдано и как» (жалоба владельца) ───────────────────────────────


def test_history_says_how_the_client_paid(isolated_db):
    """У платежа виден СПОСОБ — наличные/карта/перечисление.

    «Сколько отдано» без «как отдано» отвечало ровно на половину вопроса,
    хотя разбивка лежит в `payment_parts` с первого дня (services.order_payments).
    """
    from tests.conftest import with_pay_accounts

    db = isolated_db
    _setup(db)
    oid = _order(db, "AG-P", items=((1, 300.0),))
    from services import order_payments

    actor = order_payments.Actor(user_id=1, name="Manager", role="manager")
    rows = with_pay_accounts(
        [{"method": "cash", "amount": "100", "currency": "USD"},
         {"method": "card", "amount": "200", "currency": "USD"}],
        run=_run,
    )
    res = _run(order_payments.record_payment_parts(oid, actor, rows))
    assert res.get("ok"), res

    history = _run(db.get_agent_money_history("AG-P"))
    methods = sorted(h["method"] for h in history if h["kind"] == "payment")
    assert methods == ["card", "cash"]
    labels = [h["method_label"] for h in history if h["kind"] == "payment"]
    assert all(labels)


def test_detail_sums_up_what_the_client_handed_over(isolated_db, monkeypatch):
    """`paid_by_currency` — «Отдал всего» в карточке. Считается по валютам и
    включает наличные, ещё не сданные в кассу: клиент их уже отдал."""
    db = isolated_db
    _setup(db)
    oid = _order(db, "AG-1", items=((2, 100.0),))
    db.add_payment(1, "u", "Manager", 120.0, "USD", "часть", order_id=oid)

    client = _client(db, monkeypatch, 2)
    body = client.post("/api/clients/detail", json={"initData": "2", "agent_id": "AG-1"}).json()
    assert body["paid_by_currency"] == [{"currency": "USD", "amount": 120.0}]
    assert body["returned_by_currency"] == []
