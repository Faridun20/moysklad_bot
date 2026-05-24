"""
Тесты tasks/_cron_runner.run_cron — обёртка для cron-CLI.

Проверяем:
  * sync-main → record_cron_run пишет ok + duration_ms
  * async-main → asyncio.run + record_cron_run
  * exception → record_cron_run пишет failed + error_message, rc=1
  * rc=2 от main → status=failed (нон-нулевой)
"""

import logging

from tasks._cron_runner import run_cron


def test_run_cron_sync_success(isolated_db):
    """Sync main с rc=0 → status=ok."""

    def my_main() -> int:
        return 0

    rc = run_cron("test_sync_ok", my_main)
    assert rc == 0
    last = {r["task_name"]: r for r in isolated_db.get_last_cron_runs()}
    assert "test_sync_ok" in last
    assert last["test_sync_ok"]["status"] == "ok"


def test_run_cron_sync_none_treated_as_success(isolated_db):
    """main вернул None (нет explicit return) → status=ok, rc=0."""

    def my_main():
        return None

    rc = run_cron("test_sync_none", my_main)
    assert rc == 0
    last = {r["task_name"]: r for r in isolated_db.get_last_cron_runs()}
    assert last["test_sync_none"]["status"] == "ok"


def test_run_cron_async_success(isolated_db):
    """Async main с rc=0 → asyncio.run + status=ok."""

    async def my_main() -> int:
        return 0

    rc = run_cron("test_async_ok", my_main)
    assert rc == 0
    last = {r["task_name"]: r for r in isolated_db.get_last_cron_runs()}
    assert last["test_async_ok"]["status"] == "ok"


def test_run_cron_exception_records_failed(isolated_db, caplog):
    """main raises → record_cron_run пишет failed с error_message; rc=1."""

    def my_main() -> int:
        raise RuntimeError("simulated outage")

    caplog.set_level(logging.WARNING)
    rc = run_cron("test_fail", my_main)
    assert rc == 1
    last = {r["task_name"]: r for r in isolated_db.get_last_cron_runs()}
    assert last["test_fail"]["status"] == "failed"
    # error_message содержит и тип, и текст
    assert "RuntimeError" in last["test_fail"]["error_message"]
    assert "simulated outage" in last["test_fail"]["error_message"]


def test_run_cron_async_exception_records_failed(isolated_db):
    """Async main raises внутри asyncio.run → ловим и пишем failed."""

    async def my_main() -> int:
        raise ValueError("async boom")

    rc = run_cron("test_async_fail", my_main)
    assert rc == 1
    last = {r["task_name"]: r for r in isolated_db.get_last_cron_runs()}
    assert last["test_async_fail"]["status"] == "failed"
    assert "async boom" in last["test_async_fail"]["error_message"]


def test_run_cron_nonzero_rc_is_failed(isolated_db):
    """main вернул rc=2 (как unknown report type) → status=failed."""

    def my_main() -> int:
        return 2

    rc = run_cron("test_rc2", my_main)
    assert rc == 2
    last = {r["task_name"]: r for r in isolated_db.get_last_cron_runs()}
    assert last["test_rc2"]["status"] == "failed"


def test_run_cron_args_forwarded(isolated_db):
    """run_cron(name, main, *args) пробрасывает args в main."""
    captured = {}

    def my_main(x: int, y: int) -> int:
        captured["x"] = x
        captured["y"] = y
        return 0

    rc = run_cron("test_args", my_main, 42, y=7)
    assert rc == 0
    assert captured == {"x": 42, "y": 7}
