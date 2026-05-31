"""
WebApp /api/orders/requests: кредит-контекст клиента в заявке (босс видит
долг/лимит/превышение ПЕРЕД апрувом, не уходя в «Лимиты»). Сервер — тестируем.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _setup(db, monkeypatch, payment_type, total):
    import webapp.server as server

    importlib.reload(roles)
    boss, mgr = 100, 200
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, "A-1", "C1")
    db.add_order_item(oid, "P", "", 1, "шт", total)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type=? WHERE id=?"), (payment_type, oid))
        conn.commit()
    db.update_order_status(oid, "pending")
    req_id = db.create_shipment_request(oid, mgr, "Mgr")
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app), boss, req_id


def _get_req(client, boss, req_id):
    body = client.post("/api/orders/requests", json={"initData": str(boss)}).json()
    return next(r for r in body["requests"] if r["id"] == req_id)


def test_request_has_credit_context_over_limit(isolated_db, monkeypatch):
    db = isolated_db
    client, boss, req_id = _setup(db, monkeypatch, "credit", 3000.0)  # лимит 2000
    req = _get_req(client, boss, req_id)
    assert req["payment_type"] == "credit"
    assert "credit" in req
    assert req["credit"]["over_limit"] is True
    # pending-заявка уже в current_debt → effective = 3000 (НЕ 6000 double-count).
    assert req["credit"]["effective_debt"] == 3000.0


def test_order_credit_context_no_double_count_for_pending(isolated_db):
    from services.order_workflow import order_credit_context

    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", "C")
    db.add_order_item(oid, "P", "", 1, "шт", 3000.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type='credit' WHERE id=?"), (oid,))
        conn.commit()
    db.update_order_status(oid, "pending")  # уже считается в долге
    order = asyncio.run(db.get_order(oid))
    ctx = asyncio.run(order_credit_context(order, 3000.0))
    assert ctx["current_debt"] == 3000.0
    assert ctx["effective_debt"] == 3000.0  # не 6000
    assert ctx["over_limit"] is True


def test_order_credit_context_draft_adds_total(isolated_db):
    from services.order_workflow import order_credit_context

    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")  # draft — не в долге
    db.update_order_agent(oid, "A-1", "C")
    db.add_order_item(oid, "P", "", 1, "шт", 500.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type='credit' WHERE id=?"), (oid,))
        conn.commit()
    order = asyncio.run(db.get_order(oid))
    ctx = asyncio.run(order_credit_context(order, 500.0))
    assert ctx["current_debt"] == 0.0
    assert ctx["effective_debt"] == 500.0  # draft не в долге → 0 + total


def test_request_credit_under_limit(isolated_db, monkeypatch):
    db = isolated_db
    client, boss, req_id = _setup(db, monkeypatch, "credit", 500.0)
    req = _get_req(client, boss, req_id)
    assert req["credit"]["over_limit"] is False


def test_paid_request_has_no_credit_block(isolated_db, monkeypatch):
    db = isolated_db
    client, boss, req_id = _setup(db, monkeypatch, "paid", 9000.0)
    req = _get_req(client, boss, req_id)
    assert req["payment_type"] == "paid"
    assert "credit" not in req  # для paid кредит-контекст не нужен
