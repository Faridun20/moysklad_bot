"""Сброс бизнес-данных перед повторным переносом из МойСклад
(`scripts/reset_business_data.py`).

Главные обещания скрипта, которые здесь стерегутся:
* каждая таблица схемы классифицирована — новая фича без решения «стирать или
  хранить» роняет тест, а не молча переживает сброс (или молча стирается);
* на базе «как на проде» (все FK/CHECK из `scripts/apply_constraints`, данные во
  всех денежных и складских таблицах) --apply проходит ОДНОЙ транзакцией:
  сотрудники и технические таблицы целы, деловое пусто, после `run_backfills`
  настройки по умолчанию на месте, схема сходится, нумерация накладных
  заново, id заказов — нет;
* любой отказ посреди удаления откатывает всё;
* без подтверждения и свежего читаемого бэкапа --apply не начинается.

Postgres-часть идёт при TEST_PG_URL (локальная CI); проверки бэкапа, порядка и
классификации по схеме — без базы.
"""

from __future__ import annotations

import gzip
import os
import re
import time

import psycopg2
import pytest

from scripts import reset_business_data as rbd
from tests import test_money_postgres as _pgm

pg_db = _pgm.pg_db
needs_pg = pytest.mark.skipif(not _pgm.PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

MGR, BOSS = 1, 2


# ─── Без базы ────────────────────────────────────────────────────────────────


def _schema_tables() -> set[str]:
    from services import database as db

    names = set()
    for ddl in db._table_ddls():
        m = re.search(r"CREATE TABLE IF NOT EXISTS\s+([a-z_0-9]+)", ddl)
        if m:
            names.add(m.group(1))
    return names


def test_every_schema_table_is_classified():
    """Новая таблица обязана попасть в KEEP или WIPE осознанно."""
    tables = _schema_tables()
    assert len(tables) > 50, "разбор схемы сломался — таблиц подозрительно мало"
    classified = set(rbd.KEEP) | set(rbd.WIPE) | {rbd.SETTINGS_TABLE}
    assert sorted(tables - classified) == []
    assert set(rbd.KEEP) & set(rbd.WIPE) == set()
    assert rbd.SETTINGS_TABLE not in rbd.KEEP and rbd.SETTINGS_TABLE not in rbd.WIPE


def test_employees_are_kept_and_business_is_wiped():
    """Решение владельца — в самих списках, а не только в докстринге."""
    assert {"user_roles", "user_permissions", "user_prefs"} <= set(rbd.KEEP)
    for table in ("orders", "payments", "products", "counterparties", "stock", "invoices",
                  "acc_accounts", "machines", "containers", "leads", "audit_log",
                  "ms_id_map", "invoice_counters", "idempotency_keys", "supplier_payments"):
        assert table in rbd.WIPE, table


@pytest.mark.parametrize(
    ("key", "kept"),
    [
        ("backfill_done:local_identifiers", True),
        ("fx_sync_last_run", True),
        ("boss_digest_last_run_at", True),
        ("company_name", False),
        ("boss_digest_time", False),
        ("accounting_enabled", False),
        ("backfill_done", False),
    ],
)
def test_technical_settings_survive(key, kept):
    assert rbd.keep_setting(key) is kept


def test_delete_order_puts_children_first():
    fks = [
        ("order_items", "orders", "f1"),
        ("payment_parts", "payments", "f2"),
        ("payment_parts", "orders", "f3"),
        ("payments", "orders", "f4"),
        ("cash_deposit_parts", "payment_parts", "f5"),
        ("user_prefs", "orders", "не наша"),  # сохраняемая таблица в порядок не входит
        ("orders", "orders", "самоссылка"),
    ]
    order = rbd.delete_order({"orders", "order_items", "payments", "payment_parts",
                              "cash_deposit_parts"}, fks)
    pos = {t: i for i, t in enumerate(order)}
    for child, parent, _ in fks:
        if child in pos and parent in pos and child != parent:
            assert pos[child] < pos[parent], (child, parent, order)


def test_delete_order_refuses_a_cycle():
    with pytest.raises(rbd.ResetRefused, match="Цикл"):
        rbd.delete_order({"a", "b"}, [("a", "b", "x"), ("b", "a", "y")])


def _dump(path, *, marker=True, compress=True) -> str:
    body = b"--\n-- PostgreSQL database cluster dump\n--\n"
    if marker:
        body += b"CREATE TABLE public.user_roles (\n    user_id bigint NOT NULL\n);\n"
    body += b"x" * 5000
    if compress:
        with gzip.open(path, "wb") as fh:
            fh.write(body)
    else:
        path.write_bytes(body)
    return str(path)


def test_backup_must_exist_be_fresh_readable_and_ours(tmp_path):
    with pytest.raises(rbd.ResetRefused, match="--backup"):
        rbd.check_backup(None, 2)
    with pytest.raises(rbd.ResetRefused, match="не найден"):
        rbd.check_backup(str(tmp_path / "nope.sql.gz"), 2)
    empty = tmp_path / "empty.sql.gz"
    empty.write_bytes(b"")
    with pytest.raises(rbd.ResetRefused, match="пустой"):
        rbd.check_backup(str(empty), 2)

    good = _dump(tmp_path / "all.sql.gz")
    assert "схема бота есть" in rbd.check_backup(good, 2)
    assert "схема бота есть" in rbd.check_backup(_dump(tmp_path / "plain.sql", compress=False), 2)

    old = time.time() - 3 * 3600
    os.utime(good, (old, old))
    with pytest.raises(rbd.ResetRefused, match="старше 2 ч"):
        rbd.check_backup(good, 2)
    assert rbd.check_backup(good, 4)

    truncated = tmp_path / "cut.sql.gz"
    raw = (tmp_path / "all.sql.gz").read_bytes()
    truncated.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(rbd.ResetRefused, match="не читается"):
        rbd.check_backup(str(truncated), 24)

    with pytest.raises(rbd.ResetRefused, match="нет таблиц бота"):
        rbd.check_backup(_dump(tmp_path / "other.sql.gz", marker=False), 2)


def test_apply_needs_confirmation_and_backup_before_touching_the_db(tmp_path, caplog):
    """Отказ по флагам — до подключения: DATABASE_URL в этом тесте нет вовсе."""
    assert rbd.main(["--apply", "--backup", _dump(tmp_path / "a.sql.gz")]) == 1
    assert rbd.CONFIRM_FLAG in caplog.text
    caplog.clear()
    assert rbd.main(["--apply", rbd.CONFIRM_FLAG]) == 1
    assert "--backup" in caplog.text


# ─── Postgres: база «как на проде» ───────────────────────────────────────────


def _count(db, table: str, where: str = "TRUE") -> int:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")
        return int(cur.fetchone()["n"])


def _exec(db, sql: str, params=()) -> None:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(sql, params)
        conn.commit()


def _prod_like(db) -> None:
    """Все ограничения прода + данные во всех денежных и складских таблицах."""
    from scripts import apply_constraints
    from tests.test_db_constraints_postgres import _service_flows

    rep = apply_constraints.run(dry_run=False)
    assert rep.failed == [] and rep.not_valid == [], rep
    _service_flows(db, "R")
    db.set_setting("company_name", "ООО Тест")
    db.set_setting("boss_digest_time", "21:30")
    db.set_setting("backfill_done:local_identifiers", "2026-09-15 14:06:10")
    db.set_setting("fx_sync_last_run", "2026-09-16T05:30:03")
    db.add_audit_log(BOSS, "Boss", "boss", "test_action", "до сброса")
    _exec(db, "INSERT INTO ms_id_map (entity_type, ms_id, local_id, migrated_at) "
              "VALUES ('product', 'uuid-1', 1, '2026-09-13 17:30:57')")
    _exec(db, "INSERT INTO user_prefs (user_id, pref_key, value, updated_at) "
              "VALUES (%s, 'work_actions', 'true', '2026-09-16 10:00:00')", (BOSS,))
    _exec(db, "INSERT INTO cron_runs (task_name, started_at, finished_at, status) "
              "VALUES ('run_backup', '2026-09-16 04:00:00', '2026-09-16 04:00:05', 'success')")


def _drop_async_pool():
    _pgm._drop_async_pool()


@needs_pg
def test_reset_on_prod_like_db_keeps_employees_and_wipes_business(pg_db):
    from scripts import apply_constraints
    from services import startup_checks, warehouse

    db = pg_db
    _prod_like(db)
    _drop_async_pool()  # asyncpg-пул теста — «другой клиент» для проверки сессий
    with db.get_conn() as conn:
        plan = rbd.build_plan(db.get_cursor(conn))
        conn.rollback()
    assert plan.unclassified == [] and plan.bad_links == []
    assert "company_name" in plan.settings_delete
    assert "backfill_done:local_identifiers" in plan.settings_keep
    wiped_with_data = {t for t, n in plan.counts.items() if n > 0}
    assert {"orders", "payments", "payment_parts", "invoices", "stock", "products",
            "counterparties", "acc_accounts", "supplier_payments", "audit_log",
            "ms_id_map", "invoice_counters"} <= wiped_with_data
    users_before = _count(db, "user_roles")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT MAX(id) AS m FROM orders")
        max_order_id = int(cur.fetchone()["m"])

    with db.get_conn() as conn:
        deleted = rbd.apply_reset(conn, backup_note="test", ignore_sessions=True)
    assert deleted["orders"] >= 1 and deleted["app_settings"] >= 2

    for table in plan.counts:
        if table != "audit_log":
            assert _count(db, table) == 0, table
    assert _count(db, "audit_log") == 1
    assert _count(db, "audit_log", f"action = '{rbd.AUDIT_ACTION}'") == 1
    assert _count(db, "user_roles") == users_before
    assert _count(db, "user_prefs") == 1 and _count(db, "cron_runs") == 1
    assert db.get_setting("fx_sync_last_run") == "2026-09-16T05:30:03"
    assert _count(db, "app_settings", "key = 'company_name'") == 0

    # Что сделает `tasks.migrate`: настройки по умолчанию и склад вернулись,
    # разовые backfill'ы по отметкам НЕ повторяются.
    db._settings_cache.clear()
    report = db.run_backfills()
    assert report["local_identifiers"] == "skipped"
    assert db.get_setting("boss_digest_time") == "19:00"
    assert _count(db, "warehouses") == 1
    assert startup_checks.check_schema() == []

    # Ограничения прода на месте и данным не мешают.
    after = apply_constraints.run(dry_run=True)
    assert after.violations == {} and after.planned == [], (after.violations, after.planned)

    # Нумерация накладных — заново; id заказов — продолжение (кнопки старых карточек).
    from services import container_receipt

    pid = _pgm._run(container_receipt.create_product("Новый товар"))["product_id"]
    wid = _pgm._run(warehouse.default_warehouse_id())
    res = _pgm._run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 1, "price_cents": 100}],
    ))
    assert res["ok"], res
    assert res["invoice_number"].endswith("0001"), res
    new_order = db.create_order(MGR, "Manager", "")
    assert new_order > max_order_id


@needs_pg
def test_restart_ids_is_opt_in_and_starts_wiped_tables_from_one(pg_db, caplog):
    """По умолчанию id продолжаются (см. тест выше); `--restart-ids` начинает
    стираемые таблицы с 1 в той же транзакции. Сотрудники и их данные не
    трогаются, план dry-run показывает, где последовательности сейчас."""
    db = pg_db
    _prod_like(db)
    _drop_async_pool()
    with db.get_conn() as conn:
        plan = rbd.build_plan(db.get_cursor(conn))
        conn.rollback()
    tables = {t for t, _ in plan.sequences.values()}
    assert {"orders", "payments", "invoices", "audit_log"} <= tables
    assert not tables & (set(rbd.KEEP) | {rbd.SETTINGS_TABLE}), "чужие последовательности не трогаем"
    assert plan.sequences["orders_id_seq"][1] >= 1
    caplog.set_level("INFO", logger="reset_business_data")
    rbd.print_plan(plan)
    assert "id-последовательности стираемых таблиц" in caplog.text

    with db.get_conn() as conn:
        rbd.apply_reset(conn, backup_note="test", ignore_sessions=True, restart_ids=True)
    assert db.create_order(MGR, "Manager", "") == 1
    # Запись о сбросе встала в audit_log ДО нового id — она и есть первая строка.
    assert _count(db, "audit_log", f"id = 1 AND action = '{rbd.AUDIT_ACTION}'") == 1


@needs_pg
def test_failure_mid_delete_rolls_back_everything(pg_db):
    db = pg_db
    _prod_like(db)
    before = {t: _count(db, t) for t in ("orders", "payments", "invoices", "products", "audit_log")}
    settings_before = _count(db, "app_settings")
    # Отказ на одной из последних таблиц порядка: всё, что удалено до неё, обязано вернуться.
    _exec(db, "CREATE FUNCTION msreset_forbid() RETURNS trigger AS $$ BEGIN "
              "RAISE EXCEPTION 'нельзя'; END $$ LANGUAGE plpgsql")
    _exec(db, "CREATE TRIGGER msreset_forbid BEFORE DELETE ON counterparties "
              "FOR EACH ROW EXECUTE FUNCTION msreset_forbid()")
    _drop_async_pool()
    with db.get_conn() as conn, pytest.raises(psycopg2.Error):
        rbd.apply_reset(conn, backup_note="test", ignore_sessions=True)
    assert {t: _count(db, t) for t in before} == before
    assert _count(db, "app_settings") == settings_before


@needs_pg
def test_refusals_on_postgres(pg_db, monkeypatch):
    db = pg_db
    _drop_async_pool()

    # Другой клиент подключён — сброс не начинается.
    other = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with db.get_conn() as conn:
            plan = rbd.build_plan(db.get_cursor(conn))
            conn.rollback()
        assert any("подключены другие клиенты" in r for r in rbd.refusals(plan))
        assert not any("подключены" in r for r in rbd.refusals(plan, ignore_sessions=True))
        with db.get_conn() as conn, pytest.raises(rbd.ResetRefused, match="подключены"):
            rbd.apply_reset(conn, backup_note="test")
    finally:
        other.close()

    # Новая таблица без решения — отказ.
    _exec(db, "CREATE TABLE msreset_new_feature (id SERIAL PRIMARY KEY)")
    with db.get_conn() as conn, pytest.raises(rbd.ResetRefused, match="msreset_new_feature"):
        rbd.apply_reset(conn, backup_note="test", ignore_sessions=True)
    _exec(db, "DROP TABLE msreset_new_feature")

    # Сохраняемая таблица ссылается на стираемую — отказ (иначе DELETE упал бы на FK
    # или оставил бы висячую ссылку). FK order_items → orders ставит apply_constraints.
    from scripts import apply_constraints

    assert apply_constraints.run(dry_run=False).failed == []
    _drop_async_pool()
    with monkeypatch.context() as mp:
        mp.setattr(rbd, "KEEP", dict(rbd.KEEP, order_items="для теста"))
        mp.setattr(rbd, "WIPE", {k: v for k, v in rbd.WIPE.items() if k != "order_items"})
        with db.get_conn() as conn, pytest.raises(rbd.ResetRefused, match="order_items → orders"):
            rbd.apply_reset(conn, backup_note="test", ignore_sessions=True)

    # Некому войти после сброса — отказ.
    _exec(db, "UPDATE user_roles SET deactivated_at = '2026-09-16 10:00:00'")
    with db.get_conn() as conn, pytest.raises(rbd.ResetRefused, match="некому войти"):
        rbd.apply_reset(conn, backup_note="test", ignore_sessions=True)
    assert _count(db, "user_roles") == 2, "отказ не должен ничего стирать"
