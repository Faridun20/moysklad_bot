"""Ограничения, типы и индексы схемы — и то, что их отказ не остаётся немым.

Аудит БД нашёл четыре класса молчаливых поломок:
* количества в REAL (float4 на Postgres) — дробный возврат не сходился;
* UNIQUE на бизнес-ключах не было — одна накладная могла висеть на двух
  документах;
* индекс, не созданный из-за данных, уходил в DEBUG и нигде не всплывал;
* кириллица сортировалась по кодам символов, поиск различал «е» и «ё».

Здесь — SQLite-часть (всегда). Поведение на НАСТОЯЩЕМ Postgres, включая прогон
`scripts/apply_constraints` на базе с нарушителями, — в
`tests/test_db_constraints_postgres.py` (TEST_PG_URL).
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import pathlib
import sqlite3
from decimal import Decimal

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def _index_names(db) -> set[str]:
    with db.get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _plan(db, sql: str) -> str:
    with db.get_conn() as conn:
        return " | ".join(str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql))


def _exec(db, sql: str, params: tuple = ()) -> None:
    with db.get_conn() as conn:
        conn.execute(sql, params)
        conn.commit()


# ─── Индексы: новые, убранные, отказ создания ────────────────────────────────


NEW_INDEXES = [
    "idx_orders_created",
    "idx_payments_user_created",
    "idx_payments_confirmed_period",
    "idx_invoices_type_status_date",
    "idx_order_shipment_invoice",
    "idx_return_receipt_invoice",
    "idx_container_receipt_invoice",
    "idx_return_items_return_item",
    "idx_acc_day_closes_doc",
    # Сверка кассы: один пересчёт — одна строка на валюту (идемпотентность
    # записи и склейка строк одного пересчёта в истории).
    "idx_daily_cash_counts_key",
    "idx_daily_cash_counts_user",
]


@pytest.mark.parametrize("name", NEW_INDEXES)
def test_new_index_created_by_init_db(isolated_db, name):
    assert name in _index_names(isolated_db)


def test_dropped_indexes_do_not_come_back(isolated_db):
    """Убранный индекс не вернулся в определения — иначе скрипт снимает его на
    проде, а старт тут же создаёт заново."""
    from services.startup_checks import expected_indexes

    declared = expected_indexes(isolated_db._index_ddls())
    assert not set(isolated_db.DROPPED_INDEXES) & declared
    assert not set(isolated_db.DROPPED_INDEXES) & _index_names(isolated_db)


def test_hot_queries_use_new_indexes(isolated_db):
    db = isolated_db
    plan = _plan(db, "SELECT * FROM payments WHERE user_id = 1 ORDER BY created_at DESC LIMIT 50")
    assert "idx_payments_user_created" in plan, plan
    plan = _plan(
        db,
        "SELECT id FROM invoices i WHERE i.type = 'outgoing' AND i.status = 'confirmed' "
        "AND i.invoice_date >= '2026-01-01' ORDER BY i.invoice_date DESC, i.id DESC LIMIT 100",
    )
    assert "idx_invoices_type_status_date" in plan, plan
    assert "TEMP B-TREE" not in plan, f"сортировка должна идти индексом: {plan}"
    plan = _plan(
        db,
        "SELECT p.currency FROM payments p WHERE p.status = 'confirmed' "
        "AND COALESCE(p.confirmed_at, p.created_at) >= '2026-01-01'",
    )
    assert "idx_payments_confirmed_period" in plan, plan
    plan = _plan(db, "SELECT * FROM orders WHERE (ms_deleted_at IS NULL) ORDER BY created_at DESC")
    assert "idx_orders_created" in plan, plan


def test_failed_index_is_logged_as_error_and_reported_at_startup(isolated_db, caplog, monkeypatch):
    """UNIQUE не создался из-за дублей — ERROR в лог, а сверка старта называет
    индекс и шлёт алерт. Раньше отказ уходил в DEBUG и не был виден ничем."""
    import config
    from services import error_alerts, notifier, startup_checks

    db = isolated_db
    _exec(db, "DROP INDEX idx_order_shipment_invoice")
    _exec(db, "INSERT INTO order_shipment (order_id, invoice_id) VALUES (1, 77)")
    _exec(db, "INSERT INTO order_shipment (order_id, invoice_id) VALUES (2, 77)")

    with caplog.at_level(logging.ERROR, logger="services.database"):
        failed = db._create_indexes()
    assert [s for s in failed if "idx_order_shipment_invoice" in s], failed
    assert "Индекс не создан" in caplog.text and "idx_order_shipment_invoice" in caplog.text

    error_alerts.reset()
    monkeypatch.setattr(config, "ADMIN_IDS", [7001], raising=False)
    sent: list[str] = []

    async def fake_send(chat_id, text, **kw):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "tg_send_message", fake_send)
    result = _run(startup_checks.run_startup_checks(process="test"))
    error_alerts.reset()
    assert "нет индексов: idx_order_shipment_invoice" in result["schema"]
    assert sent and "idx_order_shipment_invoice" in sent[0]


def test_fresh_sqlite_schema_passes_index_and_type_checks(isolated_db):
    from services.startup_checks import check_column_types, check_indexes

    assert check_indexes() == []
    assert check_column_types() == []


def test_column_type_check_sees_real_quantity_and_integer_cents():
    """Сверка типов по семействам: NUMERIC и BIGINT из определения против базы."""
    from services.startup_checks import check_column_types, expected_column_types

    ddl = """CREATE TABLE IF NOT EXISTS order_items (
        id          SERIAL PRIMARY KEY,
        quantity    NUMERIC NOT NULL DEFAULT 1,  -- количество
        price_cents BIGINT NOT NULL DEFAULT 0,
        note        TEXT
    )"""
    expected = expected_column_types([ddl])
    assert expected == {("order_items", "quantity"): "numeric",
                        ("order_items", "price_cents"): "bigint"}
    actual = {("order_items", "quantity"): "float", ("order_items", "price_cents"): "integer",
              ("order_items", "note"): "text"}
    problems = check_column_types(expected, actual)
    assert len(problems) == 1
    assert "order_items.price_cents integer вместо bigint" in problems[0]
    assert "order_items.quantity float вместо numeric" in problems[0]
    # Колонки нет вовсе — это забота сверки колонок, типы молчат.
    assert check_column_types(expected, {}) == []


# ─── UNIQUE на бизнес-ключах ─────────────────────────────────────────────────


@pytest.mark.parametrize("table,key", [
    ("order_shipment", "order_id"),
    ("return_receipt", "return_id"),
    ("container_receipt", "container_id"),
])
def test_one_invoice_one_owner(isolated_db, table, key):
    db = isolated_db
    extra = ", order_id" if table == "return_receipt" else ""
    extra_v = ", 1" if table == "return_receipt" else ""
    _exec(db, f"INSERT INTO {table} ({key}, invoice_id{extra}) VALUES (1, 500{extra_v})")
    with pytest.raises(sqlite3.IntegrityError):
        _exec(db, f"INSERT INTO {table} ({key}, invoice_id{extra}) VALUES (2, 500{extra_v})")
    # Пустая ссылка законна у любого числа строк (отгрузка не прошла и т.п.).
    _exec(db, f"INSERT INTO {table} ({key}, invoice_id{extra}) VALUES (3, NULL{extra_v})")
    _exec(db, f"INSERT INTO {table} ({key}, invoice_id{extra}) VALUES (4, NULL{extra_v})")


def test_return_item_once_per_return(isolated_db):
    db = isolated_db
    _exec(db, "INSERT INTO return_items (return_id, order_item_id, qty, amount_cents) "
              "VALUES (1, 10, 1, 100)")
    _exec(db, "INSERT INTO return_items (return_id, order_item_id, qty, amount_cents) "
              "VALUES (2, 10, 1, 100)")  # та же позиция в ДРУГОМ возврате — законно
    with pytest.raises(sqlite3.IntegrityError):
        _exec(db, "INSERT INTO return_items (return_id, order_item_id, qty, amount_cents) "
                  "VALUES (1, 10, 2, 200)")


def test_day_close_unique_per_document_not_per_day(isolated_db):
    """Пересчитать кассу дважды за день законно (`test_close_day_records_difference`
    так и делает) — поэтому UNIQUE на документе, а не на (счёт, дата)."""
    db = isolated_db
    row = ("INSERT INTO acc_day_closes (account_id, close_date, expected_cents, counted_cents, "
           "diff_cents, doc_id, created_by, created_at) VALUES (1, '2026-09-15', 0, 0, 0, ?, 1, 'x')")
    _exec(db, row, (1,))
    _exec(db, row, (2,))
    with pytest.raises(sqlite3.IntegrityError):
        _exec(db, row, (1,))


@pytest.fixture
def api(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient

    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server

    db = isolated_db
    db.set_role(801, "mgr", "Mgr", "manager")
    importlib.reload(roles)
    rate_limit.reset()
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db


def test_return_api_rejects_same_position_twice(api):
    """Две строки на одну позицию — 400 текстом, а не 500 на UNIQUE."""
    client, db = api
    oid = db.create_order(801, "Mgr", "")
    iid = db.add_order_item(oid, "Товар", "href", 5, "шт", 100.0)
    for st in ("pending", "approved", "shipped"):
        db.update_order_status(oid, st)
    r = client.post("/api/returns/create", json={
        "initData": "801", "order_id": oid, "reason": "брак в двух коробках",
        "refund_method": "no_refund",
        "items": [{"item_id": iid, "quantity": 1}, {"item_id": iid, "quantity": 2}],
    })
    assert r.status_code == 400, r.text
    assert "дважды" in r.json()["detail"]


# ─── Количества: дробный возврат сходится ────────────────────────────────────


def test_fractional_return_of_the_rest_closes_order(isolated_db):
    """1.1 − 0.9 во float = 0.20000000000000007: такой «остаток» не пролезал в
    `returned_qty + qty <= quantity`, и заказ оставался «возвращён частично»."""
    db = isolated_db
    oid = db.create_order(1, "M", "")
    iid = db.add_order_item(oid, "Кабель", "", 1.1, "м", 10.0)
    for st in ("pending", "approved", "shipped"):
        db.update_order_status(oid, st)

    first = _run(db.create_return(oid, "partial", "брак", [(iid, 0.9, 0)], "no_refund", 1))
    assert first["ok"], first
    assert _run(db.mark_return_goods_received(first["return_id"], 2))["ok"]
    assert _run(db.confirm_return(first["return_id"], 2, "Boss"))["ok"]

    items = _run(db.get_order_items(oid))
    rest = items[0]["quantity"] - items[0]["returned_qty"]
    assert rest != 0.2  # сам float-хвост, ради которого правка
    second = _run(db.create_return(oid, "full", "остаток", [(iid, rest, 0)], "no_refund", 1))
    assert second["ok"], second
    assert _run(db.mark_return_goods_received(second["return_id"], 2))["ok"]
    res = _run(db.confirm_return(second["return_id"], 2, "Boss"))
    assert res["ok"], res
    assert _run(db.get_order(oid))["status"] == "returned"


def test_order_items_quantities_are_json_floats(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "M", "")
    db.add_order_item(oid, "Кабель", "", 2.5, "м", 10.0)
    items = _run(db.get_order_items(oid))
    assert isinstance(items[0]["quantity"], float) and isinstance(items[0]["returned_qty"], float)
    json.dumps(items)


# ─── Поиск и сортировка по названию ──────────────────────────────────────────


def test_order_by_name_is_plain_on_sqlite(isolated_db):
    from services import adb_core

    assert adb_core.order_by_name("p.name") == "p.name"


def test_search_treats_yo_as_ye_both_ways(isolated_db):
    from services import container_receipt, counterparties, warehouse

    _run(counterparties.create("Ёлкин Азиз"))
    _run(counterparties.create("Елена Савдо"))
    found = {r["name"] for r in _run(counterparties.search("ЕЛК"))}
    assert found == {"Ёлкин Азиз"}
    found = {r["name"] for r in _run(counterparties.search("ёлена"))}
    assert found == {"Елена Савдо"}

    _run(container_receipt.create_product("Ёрш трубный"))
    assert [r["name"] for r in _run(warehouse.search_products("ерш"))] == ["Ёрш трубный"]


def test_wh_counterparties_endpoint_searches_yo(api):
    from services import counterparties

    client, db = api
    db.set_role(900, "boss", "Boss", "boss")
    _run(counterparties.create("Сёмга ООО"))
    r = client.post("/api/wh/counterparties", json={"initData": "900", "search": "семга"})
    assert r.status_code == 200, r.text
    assert [c["name"] for c in r.json()["counterparties"]] == ["Сёмга ООО"]


# ─── Параметры драйвера и пула ───────────────────────────────────────────────


def test_float_params_become_exact_decimals_for_asyncpg():
    from services import adb_core

    args = adb_core._pg_args((2.3, 5, True, None, "x", Decimal("1.5")))
    assert args == (Decimal("2.3"), 5, True, None, "x", Decimal("1.5"))
    assert type(args[2]) is bool
    plain = (1, "a")
    assert adb_core._pg_args(plain) is plain


def test_idle_in_transaction_timeout_option(isolated_db, monkeypatch):
    db = isolated_db
    monkeypatch.delenv("PG_IDLE_IN_TX_TIMEOUT_MS", raising=False)
    assert db.pg_session_options() == {
        "options": "-c idle_in_transaction_session_timeout=300000"
    }
    monkeypatch.setenv("PG_IDLE_IN_TX_TIMEOUT_MS", "0")
    assert db.pg_session_options() == {}
    monkeypatch.setenv("PG_IDLE_IN_TX_TIMEOUT_MS", "60000")
    assert db.pg_session_options()["options"].endswith("=60000")
    # Свои options в URL не перетираем.
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://u@h/db?options=-c%20search_path%3Dx")
    assert db.pg_session_options() == {}


# ─── Скрипт ──────────────────────────────────────────────────────────────────


def test_constraints_script_is_not_wired_into_startup():
    startup = [PROJECT_ROOT / "bot.py", PROJECT_ROOT / "webapp" / "server.py"]
    startup += sorted((PROJECT_ROOT / "tasks").glob("*.py"))
    offenders = [
        str(p.relative_to(PROJECT_ROOT)) for p in startup
        if "apply_constraints" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, offenders


def test_constraints_script_refuses_sqlite(isolated_db):
    from scripts import apply_constraints

    assert apply_constraints.main(["--dry-run"]) == 2


def test_check_values_follow_the_code():
    """Статусы в CHECK — те, что пишет код: граф заказа и виды документов
    бухгалтерии берутся из модулей, а не переписываются руками."""
    from scripts import apply_constraints
    from services.accounting import ACCOUNT_KINDS, DOC_KINDS
    from services.order_workflow import TRANSITIONS

    checks = {c.name: c.expr for c in apply_constraints._checks()}
    for status in TRANSITIONS:
        assert f"'{status}'" in checks["orders_status_chk"]
    for kind in DOC_KINDS:
        assert f"'{kind}'" in checks["acc_docs_kind_chk"]
    for kind in ACCOUNT_KINDS:
        assert f"'{kind}'" in checks["acc_accounts_kind_chk"]
    from services.order_payments import METHODS, RATE_SOURCES

    for method in METHODS:
        assert f"'{method}'" in checks["payment_parts_method_chk"]
    for source in RATE_SOURCES:
        assert f"'{source}'" in checks["payment_parts_rate_source_chk"]
    from services import machine_deal_requests as mdr

    for kind in mdr.KINDS:
        assert f"'{kind}'" in checks["machine_deal_requests_kind_chk"]
    for status in mdr.STATUSES:
        assert f"'{status}'" in checks["machine_deal_requests_status_chk"]
    from services.machines import RECEIPT_METHODS

    assert set(RECEIPT_METHODS) == set(METHODS), "способы рассрочки = способы оплаты заказа"
    for method in METHODS:
        assert f"'{method}'" in checks["machine_receipt_methods_method_chk"]
    for mode in mdr.APPROVAL_MODES:
        assert f"'{mode}'" in checks["machine_deal_requests_mode_chk"]
    names = [c.name for c in apply_constraints._checks()]
    names += [fk.name for fk in apply_constraints.FOREIGN_KEYS]
    assert len(names) == len(set(names))
    assert all(len(n) <= 63 for n in names)  # предел имени в Postgres
