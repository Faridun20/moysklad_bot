"""Перенос из МойСклад на НАСТОЯЩЕМ Postgres: SQL ключей, курсов и запретов.

Остальные тесты переноса гоняют SQLite, а он прощает то, на чём падает прод:
типы булевых выражений в SELECT, `ON CONFLICT` по составному ключу, строгость
сравнения текста с числом. Здесь те же сценарии — перенос истории дважды,
дубли имён, сумовый документ с курсом, запрет отмены истории, отказ повторного
переноса справочников — на живом сервере.

Запуск: `TEST_PG_URL=postgresql://user:pass@host:5432/postgres pytest
tests/test_ms_migration_postgres.py`. Без переменной файл пропускается. База —
своя на тест (фикстура `pg_db` из `test_money_postgres`).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from tests import test_migrate_history as hist
from tests import test_money_postgres as pgm

pytestmark = pgm.pytestmark

pg_db = pgm.pg_db
ms_api = hist.ms_api


@pytest.fixture
def pg_seeded(pg_db):
    db = pg_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for name, ms_id in (("ООО Ромашка", hist.CP_MS), ("Завод", "cp-supplier")):
            cur.execute(
                db.q("INSERT INTO counterparties (name, type, legacy_ms_id, created_at) "
                     "VALUES (?, 'customer', ?, ?)"),
                (name, ms_id, db.now_str()),
            )
        for ms_id, name in ((hist.P1_MS, "Труба"), (hist.P2_MS, "Уголок")):
            cur.execute(
                db.q("INSERT INTO products (name, unit, legacy_ms_id, created_at) "
                     "VALUES (?, 'шт', ?, ?)"),
                (name, ms_id, db.now_str()),
            )
        cur.execute(db.q("SELECT id FROM products WHERE legacy_ms_id = ?"), (hist.P1_MS,))
        pid = cur.fetchone()["id"]
        cur.execute(db.q("SELECT id FROM warehouses ORDER BY id LIMIT 1"))
        wid = cur.fetchone()["id"]
        cur.execute(
            db.q("INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)"),
            (pid, wid, 50.0),
        )
        conn.commit()
    return db


def test_history_migration_on_postgres(pg_seeded, ms_api):
    from services import warehouse

    db = pg_seeded
    assert db.USE_POSTGRES
    ms_api["customerorder"] = [hist._order(sum_minor=600000,
                                           positions=[hist._pos(hist.P1_MS, "Труба", 6, 100000)])]
    ms_api["demand"] = [
        hist._demand(ms_id="dem-1", name="D001",
                     positions=[hist._pos(hist.P1_MS, "Труба", 2, 100000)], sum_minor=200000),
        hist._demand(ms_id="dem-2", name="D001",
                     positions=[hist._pos(hist.P1_MS, "Труба", 4, 100000)], sum_minor=400000),
        hist._uzs_sale(moment="2026-03-31 23:30:00.000"),
    ]
    p = hist._paymentin(ms_id="pay-uzs", sum_minor=12650000, op=("demand", "dem-uzs"))
    p["rate"] = hist._rate(hist.UZS_CUR, 12600.0)
    ms_api["paymentin"] = [p]
    ms_api["supply"] = [hist._supply(agent="cp-supplier")]
    ms_api["paymentout"] = [hist._paymentout(agent="cp-supplier")]
    before = hist._rows(db, "SELECT product_id, quantity FROM stock ORDER BY product_id")

    hist._run(ms_api)
    stats, _, problems = hist._run(ms_api)  # повтор — ни дублей, ни смены номеров

    assert problems == []
    assert stats["counterparties_to_supplier"] == 0, "уже supplier после первого прогона"
    numbers = [r["invoice_number"] for r in
               hist._rows(db, "SELECT invoice_number FROM invoices ORDER BY id")]
    assert numbers == ["MS-D-D001", "MS-D-D001-2", "MS-D-D-UZS", "MS-S-S001"]
    sale = hist._rows(db, "SELECT currency, fx_rate_to_base, submitted_at FROM orders "
                          "WHERE ms_demand_id = 'dem-uzs' AND ms_customerorder_id IS NULL")[0]
    assert sale["currency"] == "UZS"
    assert sale["fx_rate_to_base"] == pytest.approx(1 / 12650.0)
    assert sale["submitted_at"] == "2026-04-01 01:30:00"
    types = {r["legacy_ms_id"]: r["type"]
             for r in hist._rows(db, "SELECT legacy_ms_id, type FROM counterparties")}
    assert types == {hist.CP_MS: "customer", "cp-supplier": "supplier"}
    assert hist._rows(db, "SELECT product_id, quantity FROM stock ORDER BY product_id") == before

    # Запреты отмены истории — тот же SQL на Postgres.
    inv_ids = [r["id"] for r in hist._rows(db, "SELECT id FROM invoices ORDER BY id")]
    for inv_id in inv_ids:
        res = asyncio.run(warehouse.cancel_invoice(inv_id, cancelled_by=2))
        assert res["code"] == "historical", res
    flags = {r["invoice_number"]: r["historical"] for r in asyncio.run(warehouse.list_invoices())}
    assert set(flags.values()) == {True}

    from services import order_shipment

    order_id = hist._rows(db, "SELECT id FROM orders WHERE ms_customerorder_id = 'ord-1'")[0]["id"]
    assert "МойСклад" in asyncio.run(order_shipment.historical_cancel_refusal(order_id))


def test_reference_rerun_refused_on_postgres(pg_db):
    import importlib

    import scripts.migrate_from_moysklad as m
    from services import adb_core, warehouse

    importlib.reload(m)
    products = [{"ms_id": "uuid-p1", "name": "Болт", "category": None, "sku": None, "unit": "шт"}]
    stock = {"uuid-p1": Decimal("10")}
    asyncio.run(m.apply_migration(products, [], stock))
    asyncio.run(adb_core.execute("UPDATE ms_id_map SET migrated_at = '2026-01-01 00:00:00'"))

    async def live_out():
        pid = await adb_core.fetchval("SELECT id FROM products WHERE legacy_ms_id = 'uuid-p1'")
        wid = await adb_core.fetchval("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        return await warehouse.create_invoice(
            invoice_type="outgoing", warehouse_id=int(wid),
            items=[{"product_id": pid, "quantity": 3, "price_cents": 1}], created_by=2,
        )

    assert asyncio.run(live_out())["ok"]
    live = asyncio.run(m.live_activity())
    assert (live["invoices"], live["product_ms_ids"]) == (1, {"uuid-p1"})
    assert m.live_data_refusal(live)

    stats = asyncio.run(m.apply_migration(products, [], stock, protect_ms_ids={"uuid-p1"}))
    assert stats["stock_protected"] == 1
    qty = asyncio.run(adb_core.fetchval("SELECT quantity FROM stock"))
    assert Decimal(str(qty)) == Decimal("7")
    assert asyncio.run(m.verify(products, [], stock, skip_ms_ids={"uuid-p1"})) == []


# ─── Перенос на базу со ВСЕМИ ограничениями прода ────────────────────────────


def _full_history(ms_api):
    """История, в которой есть всё, что до правок роняло транзакцию на CHECK или
    давало выдуманные долги: нулевая позиция, нулевой платёж, приходный ордер по
    FIFO, расходный ордер поставщику, аренда, частичная отгрузка, заказ без
    отгрузки, приход с общей выплатой."""
    ms_api["customerorder"] = [
        hist._order(ms_id="ord-part", name="P1", sum_minor=600000,
                    positions=[hist._pos(hist.P1_MS, "Труба", 6, 100000)]),
        hist._order(ms_id="ord-open", name="O1", sum_minor=100000,
                    positions=[hist._pos(hist.P2_MS, "Уголок", 1, 100000)]),
    ]
    ms_api["demand"] = [
        hist._demand(ms_id="dem-part", name="D1", order_ms_id="ord-part", sum_minor=200000,
                     positions=[hist._pos(hist.P1_MS, "Труба", 2, 100000),
                                hist._pos(hist.P2_MS, "Уголок", 0, 5000)]),
        hist._demand(ms_id="dem-sale", name="D2", order_ms_id=None, sum_minor=300000),
    ]
    ms_api["paymentin"] = [
        hist._paymentin(ms_id="pay-part", sum_minor=200000, op=("customerorder", "ord-part")),
        hist._paymentin(ms_id="pay-zero", sum_minor=0, op=None),
    ]
    ms_api["cashin"] = [hist._cashin(ms_id="cin-sale", sum_minor=300000, op=None)]
    ms_api["supply"] = [hist._supply(agent="cp-supplier")]
    ms_api["paymentout"] = [
        hist._paymentout(ms_id="po-general", sum_minor=50000, agent="cp-supplier", op=None),
        hist._paymentout(ms_id="po-rent", sum_minor=70000, agent="cp-rent", op=None),
    ]
    ms_api["cashout"] = [hist._cashout(ms_id="cout-sup", sum_minor=100000, agent="cp-supplier")]


@pytest.fixture
def pg_constrained(pg_seeded):
    from scripts import apply_constraints

    report = apply_constraints.run(dry_run=False)
    assert report.failed == [] and report.violations == {} and report.not_valid == []
    hist._add_counterparty(pg_seeded, "Арендодатель", "cp-rent")
    return pg_seeded


def _assert_constraints_still_clean():
    from scripts import apply_constraints
    from services import startup_checks

    again = apply_constraints.run(dry_run=True)
    assert again.failed == [] and again.violations == {} and again.not_valid == []
    assert startup_checks.check_schema() == []


def _queue_keys():
    from services import work_queue

    # pg_db заводит user 2 руководителем.
    return {item["key"] for item in asyncio.run(work_queue.gather(2, "boss"))}


def test_history_import_under_all_constraints_settled(pg_constrained, ms_api):
    from services import supplier_debts

    db = pg_constrained
    _full_history(ms_api)

    stats, unmatched, problems = hist._run(ms_api, supplier_history="settled")
    hist._run(ms_api, supplier_history="settled")  # повтор на ограничениях тоже чист

    assert problems == []
    assert stats["orders_partially_shipped"] == 1 and stats["orders_unshipped"] == 1
    assert stats["payments_zero"] == 1 and stats["payments_out_not_supplier"] == 1
    assert stats["payments_out_settled"] == 2 and stats["supplies_settled"] == 1
    assert "позиция с нулевым количеством — не перенесена" in unmatched.buckets
    _assert_constraints_still_clean()

    led = asyncio.run(supplier_debts.ledger())
    assert [d for d in led.debts if d.remaining_cents > 0] == [] and led.advances == {}
    assert hist._rows(db, "SELECT * FROM supplier_payments") == []
    keys = _queue_keys()
    assert not keys & {"supplier_debts", "payments", "deposits"}, keys
    # Долги клиентов: только заказ без отгрузки (о нём есть сведения в отчёте).
    assert stats["client_debts_open"] == 1
    assert "overdue_debts" not in keys, "у перенесённого долга нет выдуманного срока"


def test_history_import_under_all_constraints_ledger(pg_constrained, ms_api):
    from services import supplier_debts

    _full_history(ms_api)

    stats, _, problems = hist._run(ms_api)

    assert problems == []
    _assert_constraints_still_clean()
    # Приход 2 000 USD − выплаты 500 (общая) − 1 000 (ордер к приходу) = 500 долга,
    # просроченного со дня прихода; аренда авансом не стала.
    led = asyncio.run(supplier_debts.ledger())
    open_debts = [d for d in led.debts if d.remaining_cents > 0]
    assert [d.remaining_cents for d in open_debts] == [50000]
    assert led.advances == {}
    assert (stats["supplier_debts_open"], stats["supplier_debts_overdue"],
            stats["supplier_advances"]) == (1, 1, 0)
    assert "supplier_debts" in _queue_keys()
    assert stats["payments_out"] == 2
