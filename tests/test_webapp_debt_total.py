"""
WebApp /api/debts: единый остаток к получению в базовой валюте (объединяет
разные валюты через convert_to_base). Серверная часть — тестируема.
"""

import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _credit_shipped(db, mgr, total, currency, agent):
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, agent, agent)
    db.add_order_item(oid, "P", "", 1, "шт", total)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "UPDATE orders SET payment_type='credit', due_date='2099-01-01', currency=? WHERE id=?"
            ),
            (currency, oid),
        )
        conn.commit()
    return oid


def _client(db, monkeypatch, mgr):
    import webapp.server as server

    importlib.reload(roles)
    db.set_role(mgr, "m", "Mgr", "manager")
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def test_debts_unified_base_total(isolated_db, monkeypatch):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 200
    _credit_shipped(db, mgr, 100.0, "USD", "A-1")  # 100 USD
    _credit_shipped(db, mgr, 1_000_000.0, "UZS", "A-2")  # 1M UZS
    db.set_currency_rate("UZS", 0.00008, mgr)  # 1M * 0.00008 = 80 USD
    db._invalidate_currency_rates_cache()

    client = _client(db, monkeypatch, mgr)
    resp = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base_currency"] == "USD"
    assert body["remaining_base_total"] == 180.0  # 100 + 80
    assert body["remaining_base_partial"] is False


def test_debts_base_total_partial_without_rate(isolated_db, monkeypatch):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 201
    _credit_shipped(db, mgr, 100.0, "USD", "A-1")  # учитывается
    _credit_shipped(db, mgr, 50.0, "EUR", "A-2")  # курс EUR не задан → пропуск

    client = _client(db, monkeypatch, mgr)
    body = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"}).json()
    assert body["remaining_base_total"] == 100.0  # только USD
    assert body["remaining_base_partial"] is True  # EUR без курса


def test_debts_base_total_none_when_empty(isolated_db, monkeypatch):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 202
    client = _client(db, monkeypatch, mgr)
    body = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"}).json()
    assert body["remaining_base_total"] is None


def _confirmed_payment(db, mgr, amount, currency, order_id):
    pid = db.add_payment(mgr, "m", "Mgr", amount, currency, "", order_id=order_id)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status='confirmed' WHERE id=?"), (pid,))
        conn.commit()
    return pid


def _confirmed_return(db, order_id, amount, status="confirmed"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "total_amount_cents, refund_method, created_by, status, created_at) "
                "VALUES (?, 'partial', 'x', ?, ?, 'debt_reduction', 1, ?, ?)"
            ),
            (order_id, amount, int(round(amount * 100)), status, db.now_str()),
        )
        conn.commit()


def test_debts_remaining_subtracts_returns_matches_agent_debt(isolated_db, monkeypatch):
    """WP-06: остаток в /api/debts вычитает подтверждённые возвраты и совпадает с
    get_agent_current_debt (кредит-чек/«Клиенты»). partially_returned виден."""
    import asyncio

    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 220
    oid = _credit_shipped(db, mgr, 100.0, "USD", "AG")
    _confirmed_return(db, oid, 30.0)  # non-cash возврат уменьшает долг
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET status='partially_returned' WHERE id=?"), (oid,))
        conn.commit()

    client = _client(db, monkeypatch, mgr)
    body = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"}).json()
    row = next(d for d in body["debts"] if d["id"] == oid)  # виден несмотря на статус
    assert row["remaining"] == 70.0
    assert asyncio.run(db.get_agent_current_debt("AG")) == 70.0  # та же цифра


def test_money_received_excludes_deleted_orders(isolated_db, monkeypatch):
    """«Получено» не должно учитывать платежи по удалённым/фантомным заказам
    (ms_deleted_at), но обязано считать платежи по живым заказам и standalone-
    платежи без order_id."""
    db = isolated_db
    db._invalidate_currency_rates_cache()
    mgr = 210

    live = _credit_shipped(db, mgr, 100.0, "USD", "A-live")
    _confirmed_payment(db, mgr, 100.0, "USD", live)

    phantom = _credit_shipped(db, mgr, 50.0, "USD", "A-phantom")
    _confirmed_payment(db, mgr, 50.0, "USD", phantom)

    _confirmed_payment(db, mgr, 30.0, "USD", None)  # standalone /pay

    client = _client(db, monkeypatch, mgr)

    # До удаления: 100 (live) + 50 (phantom) + 30 (standalone) = 180.
    body = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"}).json()
    received = {r["currency"]: r["total"] for r in body["money_received"]}
    assert received.get("USD") == 180.0

    # Помечаем phantom удалённым в МС — его 50 должны уйти из «получено».
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET ms_deleted_at='2026-06-03 00:00:00' WHERE id=?"), (phantom,))
        conn.commit()

    body2 = client.post("/api/debts", json={"initData": str(mgr), "mode": "all"}).json()
    received2 = {r["currency"]: r["total"] for r in body2["money_received"]}
    assert received2.get("USD") == 130.0  # 100 live + 30 standalone, без 50 phantom
