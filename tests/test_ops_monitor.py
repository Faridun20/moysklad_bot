"""
Тесты операционного монитора (tasks/run_ops_monitor). Проверяем чистую логику
билдеров дайджестов и сборку — без сети и БД (это и есть зона риска: что и
кому уходит). Граница с Telegram мокается на уровне HTTP в e2e-тестах
notifier'а; здесь — формирование текста.
"""

from tasks.run_ops_monitor import (
    assemble_digest,
    build_cron_health_block,
    build_expiring_batches_block,
    build_overdue_undeposited_block,
    build_pending_deposits_block,
    build_pending_returns_block,
    build_stale_orders_block,
)


# ─── Round 6 RACE-4: idempotency-guard ────────────────────────────────────────


def test_claim_ops_monitor_run_idempotent_per_day(isolated_db):
    """Первый вызов за день — True, второй — False. Защищает от двойной рассылки
    дайджеста при Railway-cron retry / случайном параллельном запуске."""
    db = isolated_db
    assert db.claim_ops_monitor_run("2026-05-24") is True
    assert db.claim_ops_monitor_run("2026-05-24") is False
    # Следующий день — новая запись, проходит.
    assert db.claim_ops_monitor_run("2026-05-25") is True


def test_blocks_return_none_when_empty():
    assert build_stale_orders_block([], 48) is None
    assert build_pending_deposits_block([]) is None
    assert build_pending_returns_block([]) is None
    assert build_overdue_undeposited_block([], 2) is None
    assert build_expiring_batches_block([], 7) is None


def test_stale_block_counts_and_truncates():
    orders = [{"id": i, "agent_name": f"A{i}", "full_name": "Mgr"} for i in range(20)]
    block = build_stale_orders_block(orders, 48)
    assert "20" in block
    assert "и ещё 5" in block  # показываем 15, остальные свёрнуты


def test_deposits_block_sums_amount():
    deposits = [{"id": 1, "amount": 100.0}, {"id": 2, "amount": 250.0}]
    block = build_pending_deposits_block(deposits)
    assert "350" in block
    assert "#1" in block and "#2" in block


def test_returns_block_shows_order():
    returns = [{"id": 5, "order_id": 42, "total_amount": 200.0}]
    block = build_pending_returns_block(returns)
    assert "#5" in block and "#42" in block and "200" in block


def test_assemble_skips_empty_blocks():
    digest = assemble_digest("T", [None, "БЛОК-A", None, "БЛОК-B"])
    assert "БЛОК-A" in digest and "БЛОК-B" in digest
    assert assemble_digest("T", [None, None]) is None


def test_assemble_none_when_all_empty():
    assert assemble_digest("Заголовок", []) is None


# ─── #5 ops hardening: cron-health block ──────────────────────────────────────


def test_cron_health_block_none_when_no_stale():
    assert build_cron_health_block([]) is None


def test_cron_health_block_shows_never_run():
    """task с last_success_at=None — никогда не запускался."""
    block = build_cron_health_block(
        [
            {
                "task_name": "ms_sync_retry",
                "last_success_at": None,
                "hours_ago": None,
                "threshold_hours": 1.0,
                "last_status": None,
                "last_error": None,
            }
        ]
    )
    assert block is not None
    assert "ms_sync_retry" in block
    assert "ни разу" in block


def test_cron_health_block_shows_stale_with_error():
    block = build_cron_health_block(
        [
            {
                "task_name": "ops_monitor",
                "last_success_at": "2026-05-24 06:00:00",
                "hours_ago": 30.5,
                "threshold_hours": 26.0,
                "last_status": "failed",
                "last_error": "psycopg2.OperationalError: connection refused",
            }
        ]
    )
    assert block is not None
    assert "ops_monitor" in block
    assert "30.5" in block
    assert "failed" in block
    assert "connection refused" in block


# ─── #5 ops hardening: record/get/stale crons ─────────────────────────────────


def test_record_cron_run_and_get_last(isolated_db):
    """Базовый sanity: пишем 2 запуска одной task, get_last_cron_runs возвращает один (последний)."""
    db = isolated_db
    db.record_cron_run("ms_sync_retry", "ok", "2026-05-24 10:00:00", "2026-05-24 10:00:05", 5000)
    db.record_cron_run(
        "ms_sync_retry",
        "failed",
        "2026-05-24 10:15:00",
        "2026-05-24 10:15:02",
        2000,
        error_message="429 Too Many Requests",
    )
    db.record_cron_run("maintenance", "ok", "2026-05-24 03:00:00", "2026-05-24 03:00:30", 30000)

    last = db.get_last_cron_runs()
    by = {r["task_name"]: r for r in last}
    assert set(by) == {"ms_sync_retry", "maintenance"}
    assert by["ms_sync_retry"]["status"] == "failed"  # последний
    assert "429" in (by["ms_sync_retry"]["error_message"] or "")
    assert by["maintenance"]["status"] == "ok"


def test_get_stale_crons_finds_overdue_and_never_run(isolated_db):
    """Stale: либо last_started > threshold назад, либо вообще ни разу."""
    db = isolated_db
    # Свежий ok-run для ms_sync_retry, очень старый для maintenance.
    from datetime import datetime, timedelta

    fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    very_old = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    db.record_cron_run("ms_sync_retry", "ok", fresh, fresh, 100)
    db.record_cron_run("maintenance", "ok", very_old, very_old, 100)

    stale = db.get_stale_crons(
        {
            "ms_sync_retry": 1.0,
            "maintenance": 26.0,
            "debts_notify": 26.0,  # ни разу
        }
    )
    by = {s["task_name"]: s for s in stale}
    # ms_sync_retry — свежий, не в stale.
    assert "ms_sync_retry" not in by
    # maintenance — старый, в stale.
    assert "maintenance" in by
    assert by["maintenance"]["hours_ago"] is not None and by["maintenance"]["hours_ago"] >= 26.0
    # debts_notify — ни разу не запускался.
    assert "debts_notify" in by
    assert by["debts_notify"]["last_success_at"] is None


def test_get_stale_crons_flags_failed_last_run(isolated_db):
    """Даже свежий failed-run попадает в stale (нужно внимание)."""
    db = isolated_db
    from datetime import datetime

    fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.record_cron_run("ms_sync_retry", "failed", fresh, fresh, 100, error_message="boom")

    stale = db.get_stale_crons({"ms_sync_retry": 1.0})
    assert len(stale) == 1
    assert stale[0]["last_status"] == "failed"
    assert stale[0]["last_error"] == "boom"


# ─── #5 ops hardening: pool stats (SQLite path) ───────────────────────────────


def test_pool_stats_empty_on_sqlite(isolated_db):
    """SQLite — пустой dict, чтобы caller мог if not stats: skip."""
    assert isolated_db.get_pool_stats() == {}
