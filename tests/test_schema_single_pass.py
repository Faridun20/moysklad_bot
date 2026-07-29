"""T1.1 — схема создаётся за один проход, инкрементальных миграций нет.

Раньше `_create_tables` объявлял урезанные таблицы, а недостающие колонки
доезжали цепочкой `ALTER TABLE ADD COLUMN` в `run_migrations()`. Значит
`init_db()` на свежей БД давал НЕполную схему, и всё держалось на том, что
кто-то не забудет позвать миграции (`payments.ms_sync_claimed_at` в
`_create_tables` не было вовсе — на свежей SQLite колонка появлялась только
после `run_migrations`).

Теперь каждая таблица объявлена один раз, сразу целиком. Эти тесты стерегут
инвариант: колонку добавляют в определение таблицы, а не новым ALTER'ом.
"""

import pathlib
import sqlite3

import pytest

# Колонки, которые раньше приезжали ТОЛЬКО через run_migrations. Если фолдинг
# развалится (или кто-то добавит колонку мимо определения таблицы) — упадёт тут.
FOLDED_COLUMNS = {
    "user_roles": {"deactivated_at", "deactivated_by"},
    "payments": {
        "order_id",
        "ms_paymentin_id",
        "ms_sync_status",
        "ms_sync_error",
        "ms_sync_claimed_at",
        "amount_cents",
    },
    "orders": {
        "currency",
        "payment_type",
        "due_date",
        "paid_at",
        "paid_confirmed_at",
        "paid_confirmed_by",
        "paid_confirmed_by_name",
        "payment_confirmed",
        "payment_confirmed_at",
        "rejection_comment",
        "rejection_count",
        "frozen",
        "credit_limit_override",
        "credit_limit_override_by",
        "return_status",
        "submitted_at",
        "shipped_at",
        "shipped_by",
        "cancelled_at",
        "cancelled_by",
        "cancellation_reason",
        "ms_demand_id",
        "ms_customerorder_id",
        "ms_cancel_synced_at",
        "ms_deleted_at",
        "ms_drift_at",
        "ms_transition_blocked_at",
        "ms_demand_failed_at",
    },
    "order_items": {"price", "price_cents", "returned_qty"},
    "credit_limits": {"limit_amount_cents"},
    "cash_deposits": {"amount_cents"},
    "cash_deposit_orders": {"amount_allocated_cents"},
    "returns": {"total_amount_cents"},
    "return_items": {"amount_cents"},
    "product_prices": {"sale_price_cents", "cost_price_cents"},
    "ms_counterparties": {"balance_cents"},
}

# T1.2 — колонки-призраки: объявлены миграцией, но нигде не читались и не
# писались. Проверка таблично-адресная: approved_by/approved_at у
# shipment_requests — живые (их пишет approve_shipment_request), убирали
# только одноимённую пару у orders.
GHOST_COLUMNS = {
    "orders": {
        "approved_by",
        "approved_at",
        "price_check_warnings",
        "client_notification_sent",
    },
    "order_items": {"stock_snap", "price_at_submit", "price_at_submit_cents"},
    "user_roles": {"active", "email", "phone"},
}

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _columns(db_path: str, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


@pytest.mark.parametrize("table,expected", sorted(FOLDED_COLUMNS.items()))
def test_init_db_creates_full_schema_in_one_pass(isolated_db, table, expected):
    """init_db() без каких-либо миграций даёт полный набор колонок."""
    db_path = isolated_db.DB_PATH
    actual = _columns(db_path, table)
    assert actual, f"таблица {table} не создана init_db()"
    missing = expected - actual
    assert not missing, (
        f"{table}: init_db() не создал колонки {sorted(missing)} — "
        f"добавь их в определение таблицы в _create_tables, НЕ через ALTER"
    )


@pytest.mark.parametrize("table,ghosts", sorted(GHOST_COLUMNS.items()))
def test_ghost_columns_removed(isolated_db, table, ghosts):
    """Колонок-призраков в схеме нет."""
    actual = _columns(isolated_db.DB_PATH, table)
    assert actual, f"таблица {table} не создана init_db()"
    still_there = ghosts & actual
    assert not still_there, f"{table}: колонки-призраки вернулись: {sorted(still_there)}"


def test_shipment_requests_keeps_its_approval_columns(isolated_db):
    """Страховка от переусердствования: у shipment_requests пара живая."""
    actual = _columns(isolated_db.DB_PATH, "shipment_requests")
    assert {"approved_by", "approved_by_name", "approved_at"} <= actual


def test_no_alter_table_anywhere_in_project():
    """Инкрементальных миграций в проекте нет. Схема — один CREATE TABLE."""
    offenders = []
    for path in PROJECT_ROOT.rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT)
        if rel.parts[0] in {".git", ".tools", "node_modules"} or rel.name == pathlib.Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "ALTER TABLE" in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "ALTER TABLE запрещён — правь определение таблицы:\n" + "\n".join(offenders)


def test_run_migrations_is_gone():
    """Символа run_migrations больше нет — чтобы его не начали звать заново."""
    import services.database as db

    assert not hasattr(db, "run_migrations")
