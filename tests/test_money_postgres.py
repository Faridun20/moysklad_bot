"""
Денежный SQL на НАСТОЯЩЕМ Postgres.

Остальные тесты гоняют SQLite, а он в двух местах ведёт себя не так, как прод:
целые там 64-битные при любом имени типа (`CAST(x AS INTEGER)` не
переполняется), а типы выражений не проверяются вовсе. Так прожил баг
«integer out of range»: сумма строки заказа приводилась к 32-битному целому, и
строка дороже 21 474 836.47 (заказ в сумах ≈ $1 800) роняла «Долги», закрытие
заказа, дебиторку и отчёт — ни один тест этого не видел.

Запуск: `TEST_PG_URL=postgresql://user:pass@host:5432/postgres pytest
tests/test_money_postgres.py`. Без переменной файл пропускается. Каждый тест
получает СВОЮ базу (CREATE DATABASE … / DROP … WITH (FORCE)) со схемой из
`init_db()` — той же, что на проде; существующие базы сервера не трогаются.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import pytest

PG_URL = os.environ.get("TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

# Больше 2**31 − 1 копеек в одной строке: 30 000 000 сум = 3 000 000 000 копеек.
BIG_UZS = 30_000_000.0
BIG_CENTS = 3_000_000_000


def _run(coro):
    return asyncio.run(coro)


def _reload_modules():
    import config
    import services.database as db
    import services.machines as machines

    importlib.reload(config)
    importlib.reload(db)
    # machines копирует USE_POSTGRES при импорте — без перезагрузки он остался
    # бы в режиме той базы, с которой модуль импортировали первым.
    importlib.reload(machines)
    return db


def _drop_async_pool():
    from services import adb_core

    if adb_core._pg_pool is not None:
        try:
            adb_core._pg_pool.terminate()
        except Exception:
            pass
    adb_core._pg_pool = None
    adb_core._pg_pool_loop = None


@pytest.fixture
def pg_db(monkeypatch):
    import psycopg2

    name = f"t_money_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    url = urlparse(PG_URL)._replace(path=f"/{name}").geturl()

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")
    _drop_async_pool()
    db = _reload_modules()
    db.init_db()
    db.seed_warehouses()
    import services.roles as roles

    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    try:
        yield db
    finally:
        _drop_async_pool()
        pool = getattr(db, "_pg_connection_pool", None)
        if pool is not None:
            pool.closeall()
        monkeypatch.undo()
        _reload_modules()
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _order(db, *, total, currency="UZS", payment_type="credit", qty=1):
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Экскаватор", "", qty, "шт", total / qty)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type=?, currency=?, due_date=? WHERE id=?"),
            (payment_type, currency, "2030-01-15" if payment_type == "credit" else None, oid),
        )
        conn.commit()
    db.update_order_status(oid, "shipped")
    return oid


def test_order_line_above_int4_does_not_overflow(pg_db):
    """Строка на 3 000 000 000 копеек: остаток, долги, дебиторка и закрытие заказа."""
    from services import receivables
    from services.debts import calc_order_balances

    db = pg_db
    assert db.USE_POSTGRES
    oid = _order(db, total=BIG_UZS)

    bal = _run(calc_order_balances([oid]))[oid]
    assert (bal.total_cents, bal.remaining_cents) == (BIG_CENTS, BIG_CENTS)
    assert [o["id"] for o in _run(db.get_open_debts())] == [oid]
    assert [r.amount_cents for r in _run(receivables.collect())] == [BIG_CENTS]
    summary = _run(db.get_order_payment_summary(oid))
    assert summary["remaining_cents"] == BIG_CENTS

    ok, pid = _run(db.mark_order_paid(oid, 1, "Manager", amount=None))
    assert ok
    assert _run(db.confirm_payment(pid, 2, "Boss"))
    row = _run(db.get_order(oid))
    assert row["paid_confirmed_at"], "заказ закрывается подтверждённой оплатой"
    assert _run(db.get_open_debts()) == []


def test_fractional_quantity_total_on_postgres(pg_db):
    """quantity — REAL: произведение на BIGINT уходит в double и округляется до
    копейки без переполнения."""
    from services.debts import calc_order_balances

    db = pg_db
    oid = _order(db, total=BIG_UZS * 2.5, qty=2.5)
    assert _run(calc_order_balances([oid]))[oid].total_cents == BIG_CENTS * 5 // 2


def test_claimable_and_deposit_fifo_on_postgres(pg_db):
    db = pg_db
    oid = _order(db, total=200.0, currency="USD")
    ok, _pid = _run(db.mark_order_paid(oid, 1, "Manager", amount=150.0))
    assert ok
    res = _run(db.create_cash_deposit(1, 200.0))
    assert res["ok"]
    assert [(o, round(a * 100)) for o, a in res["allocations"]] == [(oid, 5000)]


def test_paid_order_debt_due_date_on_postgres(pg_db):
    """SUBSTR(created_at) в фильтре «к оплате сейчас» — один и тот же SQL для
    SQLite и Postgres."""
    db = pg_db
    oid = _order(db, total=90.0, currency="USD", payment_type="paid")
    today = date.today().isoformat()
    assert [o["id"] for o in _run(db.get_open_debts(due_through=today))] == [oid]
    assert _run(db.get_open_debts(due_through="2000-01-01")) == []
    assert _run(db.count_boss_attention())["debts"] == 1


def test_money_totals_group_by_frozen_rate_on_postgres(pg_db):
    db = pg_db
    assert db.set_currency_rate("UZS", 0.00008, 2)[0]
    first = db.add_payment(1, "@m", "Manager", BIG_UZS, "UZS", "c")
    assert _run(db.confirm_payment(first, 2, "Boss"))
    assert db.set_currency_rate("UZS", 0.0001, 2)[0]
    second = db.add_payment(1, "@m", "Manager", BIG_UZS, "UZS", "c")
    assert _run(db.confirm_payment(second, 2, "Boss"))

    totals = _run(db.get_money_totals())
    assert totals["payments"] == [{"currency": "UZS", "total_cents": 2 * BIG_CENTS, "count": 2}]
    assert sorted((p["fx_rate_to_base"], p["total_cents"]) for p in totals["payments_by_rate"]) == [
        (pytest.approx(0.00008), BIG_CENTS), (pytest.approx(0.0001), BIG_CENTS),
    ]


def test_sales_stats_split_by_currency_on_postgres(pg_db):
    from services import container_receipt, warehouse

    db = pg_db
    assert db.set_currency_rate("UZS", 0.00008, 2)[0]
    wid = _run(warehouse.default_warehouse_id())
    for name, cur, price in (("Кабель", "USD", 100_000), ("Экскаватор", "UZS", BIG_CENTS)):
        pid = _run(container_receipt.create_product(name))["product_id"]
        _run(warehouse.create_invoice(invoice_type="incoming", warehouse_id=wid,
                                      items=[{"product_id": pid, "quantity": 1, "price_cents": None}]))
        res = _run(warehouse.create_invoice(invoice_type="outgoing", warehouse_id=wid, currency=cur,
                                            items=[{"product_id": pid, "quantity": 1,
                                                    "price_cents": price}]))
        assert res["ok"], res

    stats = _run(warehouse.sales_stats("2000-01-01"))
    assert stats["by_currency"] == {"USD": 100_000, "UZS": BIG_CENTS}
    # 3 000 000 000 копеек сум × 0.00008 = 240 000 копеек долларов.
    assert (stats["base_total"], stats["base_partial"]) == (100_000 + 240_000, False)
