"""Проверки при старте: схема БД против определений и часовой пояс процесса.

Обе проблемы не роняют работу, а тихо портят данные, поэтому ожидание одно:
громкий ERROR + алерт админам, процесс продолжает работать. Отправка в
Telegram подменена на границе (`notifier.tg_send_message`).

Схема проверяется на SQLite всегда и на НАСТОЯЩЕМ Postgres при `TEST_PG_URL`
(information_schema — ровно то, что читает проверка на проде).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from urllib.parse import urlparse

import pytest

from tests.test_money_postgres import PG_URL, _drop_async_pool, _reload_modules


@pytest.fixture
def sent(monkeypatch):
    import config
    from services import error_alerts, notifier

    error_alerts.reset()
    monkeypatch.setattr(config, "ADMIN_IDS", [7001], raising=False)
    box: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, **kw):
        box.append((chat_id, text))
        return True

    monkeypatch.setattr(notifier, "tg_send_message", fake_send)
    yield box
    error_alerts.reset()


@pytest.fixture
def local_tz():
    """Сменить TZ процесса на время теста (настоящий tzset, не мок)."""
    old = os.environ.get("TZ")

    def set_tz(value: str | None):
        if value is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = value
        time.tzset()

    yield set_tz
    set_tz(old)


# ─── Разбор определений ──────────────────────────────────────────────────────


def test_expected_schema_parses_columns_not_constraints():
    from services.startup_checks import expected_schema

    ddl = """CREATE TABLE IF NOT EXISTS t (
        id      INTEGER PRIMARY KEY,  -- комментарий, с запятой
        amount  NUMERIC(10, 2) NOT NULL DEFAULT 0,
        kind    TEXT CHECK (kind IN ('a', 'b')),
        note    TEXT DEFAULT '--не комментарий',
        PRIMARY KEY (id, kind),
        UNIQUE (amount, note),
        CONSTRAINT t_chk CHECK (amount >= 0)
    )"""
    assert expected_schema([ddl]) == {"t": {"id", "amount", "kind", "note"}}


def test_expected_schema_covers_every_table_of_init_db(isolated_db):
    """Разбор не теряет таблиц: каждое определение даёт непустой набор колонок."""
    from services.startup_checks import expected_schema

    ddls = isolated_db._table_ddls()
    schema = expected_schema(ddls)
    assert len(schema) == len(ddls)
    assert all(schema.values())
    assert {"quantity", "returned_qty", "price_cents"} <= schema["order_items"]


# ─── Схема на SQLite ─────────────────────────────────────────────────────────


def test_fresh_schema_has_no_drift(isolated_db):
    from services.startup_checks import check_schema

    assert check_schema() == []


def test_missing_table_and_column_are_reported(isolated_db, sent, caplog):
    """База отстала: таблицу снесли, а старая таблица без новой колонки —
    ровно то, что оставляет CREATE TABLE IF NOT EXISTS на существующей базе."""
    import logging

    from services import startup_checks

    db = isolated_db
    with db.get_conn() as conn:
        conn.execute("DROP TABLE return_receipt")
        conn.execute("DROP TABLE order_shipment")
        conn.execute(
            "CREATE TABLE order_shipment (order_id BIGINT PRIMARY KEY, invoice_id BIGINT)"
        )
        conn.commit()

    with caplog.at_level(logging.ERROR):
        result = asyncio.run(startup_checks.run_startup_checks(process="test"))

    assert "нет таблицы return_receipt" in result["schema"]
    assert "order_shipment: нет колонок error, failed_at, shipped_at" in result["schema"]
    assert "Схема БД отстаёт от кода" in caplog.text
    assert len(sent) == 1 and sent[0][0] == 7001
    assert "return_receipt" in sent[0][1] and "shipped_at" in sent[0][1]


def test_extra_legacy_columns_are_not_an_alarm(isolated_db):
    from services.startup_checks import check_schema, expected_schema

    expected = expected_schema(isolated_db._table_ddls())
    actual = {t: set(cols) | {"ms_legacy_ghost"} for t, cols in expected.items()}
    actual["some_old_table"] = {"id"}
    assert check_schema(expected, actual) == []


def test_check_failure_never_raises(isolated_db, sent, monkeypatch):
    """Страховка не имеет права стать причиной падения старта."""
    from services import startup_checks

    def broken():
        raise RuntimeError("information_schema недоступна")

    monkeypatch.setattr(startup_checks, "check_schema", broken)
    result = asyncio.run(startup_checks.run_startup_checks())
    assert "RuntimeError" in result["schema"][0]
    assert sent, "сбой самой сверки тоже повод сказать админу"


# ─── Часовой пояс ────────────────────────────────────────────────────────────


def test_business_timezone_passes(local_tz):
    from services.startup_checks import check_timezone

    local_tz("Asia/Tashkent")
    assert check_timezone() == []


def test_same_offset_other_name_is_not_an_alarm(local_tz):
    from services.startup_checks import check_timezone

    local_tz("Asia/Samarkand")
    assert check_timezone() == []


@pytest.mark.parametrize("tz", ["UTC", "Europe/Moscow", None])
def test_wrong_timezone_is_reported_loudly(isolated_db, local_tz, sent, caplog, tz):
    import logging

    from services import startup_checks

    local_tz(tz)
    with caplog.at_level(logging.ERROR):
        result = asyncio.run(startup_checks.run_startup_checks(process="test"))

    assert result["schema"] == []
    assert len(result["timezone"]) == 1
    assert "Asia/Tashkent" in result["timezone"][0]
    assert "Процесс не в бизнес-зоне" in caplog.text
    assert any("бизнес-зоне" in text for _, text in sent)


def test_timezone_name_without_tzdata_is_caught_by_offset(local_tz):
    """glibc без tzdata молча откатывается на UTC при верном имени зоны —
    поэтому сверка по смещению, а не по строке TZ."""
    from services.startup_checks import check_timezone

    local_tz("Nowhere/Tashkent_typo")  # неизвестная зона → glibc отдаёт UTC
    problems = check_timezone()
    assert problems and "UTC+00:00" in problems[0]


def test_business_tz_is_configurable(local_tz, monkeypatch):
    from services.startup_checks import check_timezone

    local_tz("Europe/Moscow")
    monkeypatch.setenv("BUSINESS_TZ", "Europe/Moscow")
    assert check_timezone() == []


# ─── Postgres (TEST_PG_URL) ──────────────────────────────────────────────────


pg = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")


@pytest.fixture
def pg_empty(monkeypatch):
    """Пустая временная база на живом Postgres; init_db зовёт сам тест."""
    import psycopg2

    name = f"t_start_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    url = urlparse(PG_URL)._replace(path=f"/{name}").geturl()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")
    _drop_async_pool()
    db = _reload_modules()
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


@pg
def test_postgres_fresh_schema_has_no_drift(pg_empty):
    from services.startup_checks import actual_schema, check_schema

    pg_empty.init_db()
    assert "return_receipt" in actual_schema()
    assert check_schema() == []


@pg
def test_postgres_reports_column_missing_on_existing_table(pg_empty):
    db = pg_empty
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        # «Старая» таблица на проде: определение с тех пор получило колонки.
        cur.execute("CREATE TABLE currency_rate_daily (currency_code TEXT, rate_date TEXT)")
        conn.commit()
    db.init_db()

    from services.startup_checks import check_schema

    problems = check_schema()
    assert problems == ["currency_rate_daily: нет колонок created_at, rate_to_base, source"]
