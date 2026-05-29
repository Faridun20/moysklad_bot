"""
asyncpg-миграция Stage 1: пилотная функция get_paid_orders_awaiting_confirmation
переведена на нативный async через services.adb_core.

Проверяем, что async-путь работает на реальной схеме (SQLite-бэкенд adb_core
читает тот же файл, что заполнил sync isolated_db) и что фильтр по user_id
корректен. Это доказывает живой end-to-end async-путь до Postgres-rewrite.
"""

import asyncio
import inspect


def _setup_paid_order_with_pending_payment(db, mgr):
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")  # payment_type по умолчанию 'paid'
    db.add_payment(mgr, "m", "Mgr", 100.0, "USD", "", order_id=oid)  # pending
    return oid


def test_pilot_function_is_now_coroutine(isolated_db):
    # async_db пропускает coroutine-функции без to_thread — поэтому важно,
    # что после миграции функция именно async.
    assert inspect.iscoroutinefunction(isolated_db.get_paid_orders_awaiting_confirmation)


def test_paid_orders_awaiting_confirmation_async(isolated_db):
    db = isolated_db
    mgr = 300
    oid = _setup_paid_order_with_pending_payment(db, mgr)

    rows = asyncio.run(db.get_paid_orders_awaiting_confirmation())
    assert any(r["id"] == oid for r in rows)


def test_paid_orders_awaiting_confirmation_filters_by_user(isolated_db):
    db = isolated_db
    mgr = 301
    oid = _setup_paid_order_with_pending_payment(db, mgr)

    mine = asyncio.run(db.get_paid_orders_awaiting_confirmation(mgr))
    assert any(r["id"] == oid for r in mine)

    other = asyncio.run(db.get_paid_orders_awaiting_confirmation(99999))
    assert all(r["id"] != oid for r in other)


def test_async_db_passes_through_coroutine(isolated_db):
    # Через фасад services.async_db вызов тоже работает (без двойной обёртки).
    from services import async_db as adb

    mgr = 302
    oid = _setup_paid_order_with_pending_payment(db=isolated_db, mgr=mgr)
    rows = asyncio.run(adb.get_paid_orders_awaiting_confirmation())
    assert any(r["id"] == oid for r in rows)


# ─── Stage 3: get_pending_requests ──────────────────────────────────────────────


def test_get_pending_requests_async(isolated_db):
    db = isolated_db
    assert inspect.iscoroutinefunction(db.get_pending_requests)

    mgr = 303
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_status(oid, "draft")
    req_id = db.create_shipment_request(oid, mgr, "Mgr", "срочно")

    rows = asyncio.run(db.get_pending_requests())
    assert any(r["id"] == req_id for r in rows)


# ─── Stage 6: get_audit_log, get_all_orders ─────────────────────────────────────


def test_get_audit_log_async(isolated_db):
    db = isolated_db
    assert inspect.iscoroutinefunction(db.get_audit_log)

    db.add_audit_log(11, "Alice", "boss", "login", "вошла")
    db.add_audit_log(22, "Bob", "manager", "payment_sent", "оплата")

    all_rows = asyncio.run(db.get_audit_log(limit=50))
    actions = {r["action"] for r in all_rows}
    assert {"login", "payment_sent"} <= actions

    only_bob = asyncio.run(db.get_audit_log(limit=50, user_id=22))
    assert only_bob and all(r["user_id"] == 22 for r in only_bob)


def test_get_all_orders_async(isolated_db):
    db = isolated_db
    assert inspect.iscoroutinefunction(db.get_all_orders)

    mgr = 304
    db.set_role(mgr, "m", "Mgr", "manager")
    o1 = db.create_order(mgr, "Mgr", "")
    o2 = db.create_order(mgr, "Mgr", "")
    db.update_order_status(o2, "approved")

    all_orders = asyncio.run(db.get_all_orders())
    ids = {o["id"] for o in all_orders}
    assert {o1, o2} <= ids

    approved = asyncio.run(db.get_all_orders(status="approved"))
    aids = {o["id"] for o in approved}
    assert o2 in aids and o1 not in aids


# ─── Stage 7: get_payments_report, get_overdue_undeposited_orders ────────────────


def test_get_payments_report_async(isolated_db):
    db = isolated_db
    assert inspect.iscoroutinefunction(db.get_payments_report)
    assert inspect.iscoroutinefunction(db.get_overdue_undeposited_orders)

    pid = db.add_payment(400, "m", "M", 123.0, "USD", "за товар")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status = 'confirmed' WHERE id = ?"), (pid,))
        conn.commit()

    rows = asyncio.run(db.get_payments_report())
    assert any(r["id"] == pid for r in rows)
    # все строки отчёта — только confirmed
    assert all(r["status"] == "confirmed" for r in rows)
