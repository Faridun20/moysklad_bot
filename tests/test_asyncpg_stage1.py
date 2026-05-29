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
