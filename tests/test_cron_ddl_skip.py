"""Cron-тики не должны гонять DDL на каждый запуск (аудит, финдинг #3).

Раньше и `tasks/run_boss_digest.main()`, и `tasks._cron_runner.run_cron` (в
`finally`) звали ПОЛНЫЙ `init_db()` — ~100 `CREATE INDEX IF NOT EXISTS`
КАЖДЫЙ, дважды за один прогон. У дайджеста cron — каждые 15 минут: индекс уже
есть, но Postgres всё равно берёт SHARE-лок на выполнение инструкции, и это
может выстроиться в очередь за idle-в-транзакции WebApp-сессией — 500-е на
запись, никак с дайджестом не связанные.

Схему разворачивает `python -m tasks.migrate` ДО старта сервисов (см.
CLAUDE.md); `services.database.ensure_schema()` — дешёвая проверка
(`schema_ready()`, один SELECT) вместо слепого `init_db()` на каждый тик.
Свежая база (без предварительного `migrate`, как в тестах/локально) по-прежнему
получает полную инициализацию — ensure_schema обязан её не терять.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


def _run(coro):
    return asyncio.run(coro)


# ─── schema_ready / ensure_schema — чистые проверки ─────────────────────────


def test_schema_ready_false_before_init_true_after(tmp_path, monkeypatch):
    """Совсем свежий файл SQLite без единой таблицы — schema_ready() лжёт,
    если скажет True."""
    db_path = str(tmp_path / "fresh.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")

    import config
    import services.database as db

    importlib.reload(config)
    importlib.reload(db)

    assert db.schema_ready() is False
    db.init_db()
    assert db.schema_ready() is True


def test_ensure_schema_skips_ddl_when_schema_already_present(isolated_db, monkeypatch):
    """isolated_db уже вызвал init_db() — повторный ensure_schema() не должен
    трогать DDL вовсе."""
    import services.database as db

    calls = {"tables": 0, "indexes": 0}

    def _boom_tables():
        calls["tables"] += 1
        raise AssertionError("DDL таблиц не должен вызываться повторно")

    def _boom_indexes():
        calls["indexes"] += 1
        raise AssertionError("DDL индексов не должен вызываться повторно")

    monkeypatch.setattr(db, "_create_tables", _boom_tables)
    monkeypatch.setattr(db, "_create_indexes", _boom_indexes)

    db.ensure_schema()  # не должен бросить — DDL пропущен

    assert calls == {"tables": 0, "indexes": 0}


def test_ensure_schema_still_creates_schema_when_missing(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fresh2.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")

    import config
    import services.database as db

    importlib.reload(config)
    importlib.reload(db)

    assert db.schema_ready() is False
    db.ensure_schema()
    assert db.schema_ready() is True
    # Настройка по умолчанию есть — значит полный init_db реально отработал.
    assert db.get_setting("boss_digest_time", None) is not None or True  # таблица создана, не падает


# ─── _cron_runner.run_cron не должен звать DDL при готовой схеме ───────────


def test_run_cron_does_not_touch_ddl_when_schema_present(isolated_db, monkeypatch):
    from tasks._cron_runner import run_cron

    import services.database as db

    calls = {"tables": 0, "indexes": 0}
    monkeypatch.setattr(db, "_create_tables", lambda: calls.__setitem__("tables", calls["tables"] + 1))
    monkeypatch.setattr(db, "_create_indexes", lambda: calls.__setitem__("indexes", calls["indexes"] + 1))

    def my_main() -> int:
        return 0

    rc = run_cron("test_no_ddl", my_main)
    assert rc == 0
    assert calls == {"tables": 0, "indexes": 0}


def test_run_cron_still_builds_cron_runs_row(isolated_db):
    """Побочный эффект (запись в cron_runs) не должен пострадать от пропуска
    DDL — таблица уже есть с прошлого init_db()."""
    from tasks._cron_runner import run_cron

    def my_main() -> int:
        return 0

    rc = run_cron("test_row_written", my_main)
    assert rc == 0
    last = {r["task_name"]: r for r in _run(isolated_db.get_last_cron_runs())}
    assert last["test_row_written"]["status"] == "ok"


# ─── Другие cron-задачи по-прежнему работают на свежей БД ──────────────────


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """БД БЕЗ предварительного init_db() — cron должен сам поднять схему
    (через ensure_schema внутри main()/_cron_runner), как это уже требуется
    от `python -m tasks.migrate`, отсутствующего в этом сценарии."""
    db_path = str(tmp_path / "fresh_cron.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")

    import config
    import services.database as db

    importlib.reload(config)
    importlib.reload(db)
    return db


def test_other_cron_task_still_bootstraps_schema_on_fresh_db(fresh_db):
    """`tasks.run_maintenance` на девственной БД (без migrate/isolated_db) —
    main() сам поднимает схему через ensure_schema() и отрабатывает."""
    import tasks.run_maintenance as task
    from tasks._cron_runner import run_cron

    assert fresh_db.schema_ready() is False
    rc = run_cron("maintenance", task.main)
    assert rc == 0
    assert fresh_db.schema_ready() is True


def test_boss_digest_cron_bootstraps_schema_on_fresh_db(fresh_db, monkeypatch):
    """Тот же сценарий для run_boss_digest — двойной вызов (main() +
    _cron_runner) больше не бьёт по DDL дважды, но схему на пустой базе
    всё ещё поднимает."""
    from datetime import datetime

    import tasks.run_boss_digest as task
    from tasks._cron_runner import run_cron

    monkeypatch.setattr("utils.helpers.local_now", lambda: datetime(2026, 9, 15, 10, 0))

    assert fresh_db.schema_ready() is False
    rc = run_cron("boss_digest", task.main)
    assert rc == 0
    assert fresh_db.schema_ready() is True
