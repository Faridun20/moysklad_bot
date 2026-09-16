"""Перенос справочников МойСклад на НАСТОЯЩЕМ Postgres с ограничениями прода.

FK/CHECK денежных и складских таблиц живут только на проде (их ставит
`scripts/apply_constraints`), поэтому здесь они ставятся на пустую базу ДО
переноса — как на базе после `scripts/reset_business_data`. Проверяется ровно
то, что ломало прошлый перенос: отрицательный остаток из МС (из-за него не
ставился `stock_quantity_chk`) и дубль артикула (UNIQUE `idx_products_sku`
ронял бы весь перенос), плюс цены.

Запуск: `TEST_PG_URL=postgresql://… pytest tests/test_ms_reference_postgres.py`.
"""

from __future__ import annotations

import asyncio
import importlib
from decimal import Decimal

from tests import test_money_postgres as pgm

pytestmark = pgm.pytestmark

pg_db = pgm.pg_db


def _run(coro):
    return asyncio.run(coro)


PRICE = {"sale_price_cents": 1250000, "wholesale_price_cents": 1100000,
         "cost_price_cents": 900000, "currency": "USD"}
PRODUCTS = [
    {"ms_id": "uuid-p1", "name": "Болт", "category": "Крепёж", "sku": "DUP", "unit": "шт",
     "price": dict(PRICE), "price_issues": []},
    {"ms_id": "uuid-p2", "name": "Гайка", "category": None, "sku": "DUP", "unit": "шт",
     "price": None, "price_issues": ["«Гайка»: валюта EUR"]},
    {"ms_id": "uuid-p3", "name": "Шайба", "category": None, "sku": None, "unit": "шт"},
]
COUNTERPARTIES = [{"ms_id": "uuid-c1", "name": "Ромашка", "phone": None, "type": "customer"}]
STOCK = {"uuid-p1": Decimal("-32.5"), "uuid-p2": Decimal("4"), "uuid-p3": Decimal("0.3")}


def test_reference_import_holds_all_prod_constraints(pg_db):
    import scripts.migrate_from_moysklad as m
    from scripts import apply_constraints
    from services import adb_core, startup_checks

    importlib.reload(m)
    empty = apply_constraints.run(dry_run=False)
    assert empty.failed == [] and empty.violations == {} and empty.not_valid == []

    preview = _run(m.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK, dry_run=True))
    assert preview["verify_problems"] == []
    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM products")) == 0

    stats = _run(m.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    assert (stats["stock_negative_clamped"], stats["sku_duplicates"]) == (1, 1)
    assert (stats["prices"], stats["prices_skipped"]) == (1, 1)
    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM stock WHERE quantity < 0")) == 0
    qty = _run(adb_core.fetchval("SELECT quantity::text FROM stock s JOIN products p "
                                 "ON p.id = s.product_id WHERE p.legacy_ms_id = 'uuid-p3'"))
    assert qty == "0.3"

    # Повтор — идемпотентен и тоже проходит под ограничениями.
    _run(m.apply_migration(PRODUCTS, COUNTERPARTIES, STOCK))
    assert _run(m.verify(PRODUCTS, COUNTERPARTIES, STOCK)) == []

    after = apply_constraints.run(dry_run=True)
    assert after.violations == {} and after.not_valid == [] and after.failed == []
    row = _run(adb_core.fetchrow(
        "SELECT convalidated FROM pg_constraint WHERE conname = 'stock_quantity_chk'"))
    assert row is not None and row["convalidated"] is True
    assert startup_checks.check_schema() == []

    # Приход на обнулённый остаток проходит (на −32.5 с CHECK он бы упал).
    from services import warehouse

    pid = _run(adb_core.fetchval("SELECT id FROM products WHERE legacy_ms_id = 'uuid-p1'"))
    wid = _run(warehouse.default_warehouse_id())
    res = _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 5, "price_cents": None}], created_by=2))
    assert res["ok"], res
    assert Decimal(str(_run(adb_core.fetchval(
        "SELECT quantity FROM stock WHERE product_id = $1", pid)))) == 5
