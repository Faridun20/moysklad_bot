"""Себестоимость на НАСТОЯЩЕМ Postgres.

SQLite не проверяет типы параметров и не знает `FOR UPDATE`, а здесь их много:
количество — NUMERIC (asyncpg отдаёт Decimal), курс — TEXT, суммы в сумах —
BIGINT за пределами int4, а параллельные отгрузки одного товара обязаны
разобрать партии FIFO без двойного списания одной и той же единицы.

Запуск: `TEST_PG_URL=postgresql://user:pass@host:5432/postgres pytest
tests/test_costing_postgres.py`. Без переменной файл пропускается. База — своя
на каждый тест (фикстура `pg_db` из test_money_postgres).
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import date, datetime, timedelta

import pytest

from tests.test_money_postgres import PG_URL, pg_db  # noqa: F401 — фикстура

pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

BOSS = 2


@pytest.fixture
def pg(pg_db):  # noqa: F811
    # Эти модули копируют USE_POSTGRES при импорте — без перезагрузки они
    # остались бы в режиме SQLite, с которой их импортировали первыми.
    import services.container_receipt as container_receipt
    import services.containers as containers
    import services.order_shipment as order_shipment

    for mod in (containers, container_receipt, order_shipment):
        importlib.reload(mod)
    pg_db.set_setting("accounting_enabled", True)
    ok, err = pg_db.set_currency_rate("UZS", 1 / 13000.0, 0)
    assert ok, err
    today = date.today().strftime("%Y-%m-%d")
    pg_db.set_currency_rate_daily("UZS", today, 1 / 13000.0, "cbu")
    pg_db.set_currency_rate_daily("CNY", today, 1800 / 13000.0, "cbu")
    yield pg_db
    for mod in (containers, container_receipt, order_shipment):
        importlib.reload(mod)


def _drop_pool():
    from tests.test_money_postgres import _drop_async_pool

    _drop_async_pool()


def _flow(coro_fn):
    """Одна корутина = один event loop = один пул asyncpg."""
    try:
        return asyncio.run(coro_fn())
    finally:
        _drop_pool()


def test_container_to_report_on_postgres(pg):
    from services import (
        adb_core, container_receipt, containers, costing, warehouse,
    )

    async def scenario():
        pid = (await container_receipt.create_product("Гидроцилиндр"))["product_id"]
        cid = (await containers.create_container(number="PGCONT1", created_by=BOSS))["container_id"]
        item = (await containers.add_item(cid, name="Гидроцилиндр", expected_qty=10.5,
                                          product_id=pid))["item_id"]
        assert (await containers.mark_arrived(cid, user_id=BOSS))["ok"]
        assert (await containers.set_arrived_quantities(cid, {item: "10.5"}, user_id=BOSS))["ok"]
        rec = await container_receipt.receive(cid, user_id=BOSS)
        assert rec["ok"], rec
        saved = await costing.save_container_costing(
            cid, currency="USD", uzs_per_usd="12500", prices={item: "1000"}, user_id=BOSS,
        )
        assert saved["ok"], saved
        wid = await warehouse.default_warehouse_id()
        # 2.5 шт × 13 000 000 сум = 32 500 000 сум = 3 250 000 000 копеек > int4.
        sale = await warehouse.create_invoice(
            invoice_type="outgoing", warehouse_id=wid, currency="UZS",
            items=[{"product_id": pid, "quantity": 2.5, "price_cents": 1_300_000_000}],
        )
        assert sale["ok"], sale
        start = datetime.combine(date.today(), datetime.min.time())
        rep = await costing.period_report(start - timedelta(days=1), start + timedelta(days=1))
        summary = (await costing.container_summaries([cid]))[cid]
        cancelled = await warehouse.cancel_invoice(sale["invoice_id"])
        after = (await costing.container_summaries([cid]))[cid]
        inv_price = await adb_core.fetchval(
            "SELECT price_cents FROM invoice_items WHERE invoice_id = $1", rec["invoice_id"]
        )
        return rep, summary, cancelled, after, inv_price

    rep, summary, cancelled, after, inv_price = _flow(scenario)
    # 3 250 000 000 сум-копеек / 13 000 = 250 000 центов; по курсу прибытия — 260 000.
    assert rep["totals"]["revenue_cents"] == 250_000
    assert rep["totals"]["cogs_cents"] == 250_000  # 2.5 × $1000
    assert rep["fx"]["diff_cents"] == -10_000
    assert summary["sold_qty"] == 2.5 and summary["remaining_qty"] == 8.0
    assert summary["remaining_cost_cents"] == 800_000
    assert cancelled["ok"] and after["sold_qty"] == 0 and after["remaining_qty"] == 10.5
    assert inv_price == 100_000


def test_parallel_sales_split_batches_without_double_counting(pg):
    from services import adb_core, container_receipt, costing, warehouse

    async def scenario():
        pid = (await container_receipt.create_product("Палец ковша"))["product_id"]
        wid = await warehouse.default_warehouse_id()
        for price in (1000, 2000):
            res = await warehouse.create_invoice(
                invoice_type="incoming", warehouse_id=wid,
                items=[{"product_id": pid, "quantity": 10, "price_cents": price}],
            )
            assert res["ok"], res
        sales = await asyncio.gather(*(
            warehouse.create_invoice(
                invoice_type="outgoing", warehouse_id=wid,
                items=[{"product_id": pid, "quantity": 3, "price_cents": 5000}],
            )
            for _ in range(6)
        ))
        assert all(s["ok"] for s in sales), sales
        per_batch = await adb_core.fetch(
            "SELECT b.unit_price_cents, SUM(s.quantity) AS qty, SUM(s.cost_base_cents) AS cost "
            "FROM sale_costs s JOIN cost_batches b ON b.id = s.batch_id "
            "GROUP BY b.unit_price_cents ORDER BY b.unit_price_cents"
        )
        left = await costing.current_costs([pid])
        return per_batch, left

    per_batch, left = _flow(scenario)
    assert [(int(r["unit_price_cents"]), float(r["qty"]), int(r["cost"])) for r in per_batch] == [
        (1000, 10.0, 10_000), (2000, 8.0, 16_000),
    ]
    assert left[next(iter(left))] == {"unit_cost_cents": 2000, "qty": 2.0, "uncovered_qty": 0.0}
