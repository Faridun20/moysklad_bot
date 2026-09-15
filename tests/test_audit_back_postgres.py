"""Правки аудита бэкенда на НАСТОЯЩЕМ Postgres (TEST_PG_URL).

SQLite прощает то, на чём Postgres спотыкается: типы в UPSERT с условием,
NUMERIC-остатки, advisory-lock и FOR UPDATE внутри одной транзакции.
Здесь — приход товара по возврату и защита ручного курса от синка.
Без переменной файл пропускается (как tests/test_money_postgres.py).
"""

from __future__ import annotations

import asyncio

import pytest

from tests import test_money_postgres as _pgm

pytestmark = pytest.mark.skipif(not _pgm.PG_URL, reason="TEST_PG_URL не задан")

# Фикстура живой временной базы — та же, что у денежных тестов.
pg_db = _pgm.pg_db


def _run(coro):
    return asyncio.run(coro)


def _stock(db, pid) -> float:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COALESCE(SUM(quantity), 0) AS q FROM stock WHERE product_id = ?"), (pid,))
        return float(cur.fetchone()["q"])


def test_return_receipt_on_postgres(pg_db):
    from services import container_receipt, order_shipment, warehouse

    db = pg_db
    pid = _run(container_receipt.create_product("Кабель"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    assert _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))["ok"]
    oid = db.create_order(1, "Manager", "")
    iid = db.add_order_item(oid, "Кабель", "", 4, "шт", 5.0, product_id=pid)
    db.update_order_status(oid, "shipped")
    assert _run(order_shipment.ship_order(
        _run(db.get_order(oid)), _run(db.get_order_items(oid)), user_id=2
    ))["ok"]
    assert _stock(db, pid) == 6

    ret = _run(db.create_return(oid, "partial", "брак", [(iid, 1.5, 0)], "debt_reduction", 1))
    assert ret["ok"], ret
    assert _run(db.mark_return_goods_received(ret["return_id"], 2))["ok"]
    res = _run(db.confirm_return(ret["return_id"], 2, "Boss"))

    assert res["ok"], res
    assert _stock(db, pid) == 7.5
    assert _run(db.confirm_return(ret["return_id"], 2, "Boss"))["ok"] is False
    assert _stock(db, pid) == 7.5


def test_manual_daily_rate_survives_auto_upsert_on_postgres(pg_db):
    db = pg_db
    day = db.now_str()[:10]
    assert db.set_currency_rate_manual("UZS", 1 / 13000, updated_by=2)[0]
    assert db.set_currency_rate_daily("UZS", day, 1 / 12000, source="cbu")[0]
    assert db.get_currency_rate_daily_source("UZS", day) == "manual"
    assert db.get_currency_rate_asof("UZS", day) == pytest.approx(1 / 13000)
    assert db.set_currency_rate_manual("UZS", 1 / 12800, updated_by=2)[0]
    assert db.get_currency_rate_asof("UZS", day) == pytest.approx(1 / 12800)
    # Обычный день без ручной записи синк по-прежнему обновляет.
    assert db.set_currency_rate_daily("UZS", "2026-01-05", 1 / 12000, source="cbu")[0]
    assert db.set_currency_rate_daily("UZS", "2026-01-05", 1 / 12100, source="cbu")[0]
    assert db.get_currency_rate_asof("UZS", "2026-01-05") == pytest.approx(1 / 12100)
