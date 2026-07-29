"""
Тесты janitor-чистки БД (services.database.prune_audit_log).
Настоящая SQLite (isolated_db); время «старим» прямой записью created_at.
"""

from datetime import datetime, timedelta


def _old(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def test_prune_audit_log_keeps_recent(isolated_db):
    db = isolated_db
    db.add_audit_log(1, "U", "boss", "act", "свежая")
    db.add_audit_log(2, "U", "boss", "act", "старая")
    # состарим вторую запись на ~1 год
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE audit_log SET created_at = ? WHERE user_id = 2"), (_old(400),))
        conn.commit()

    removed = db.prune_audit_log(retention_months=6)
    assert removed == 1
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT COUNT(*) FROM audit_log")
        assert (cur.fetchone()[0]) == 1


# ─── reset_stale_in_progress_payments (orphan-reaper в cron-ms-retry) ────────


def _make_in_progress_payment(db, confirmed_minutes_ago: int) -> int:
    """Создаёт confirmed-платёж с ms_sync_status='in_progress' и заданным
    возрастом confirmed_at. Возвращает payment_id."""
    # add_payment добавляет 'pending'; вручную выставляем то, что нужно reaper'у
    # без раздувания публичного API.
    pid = db.add_payment(
        user_id=1, username="u", full_name="M",
        amount=10.0, currency="USD", comment="t",
    )
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "UPDATE payments SET "
                "status='confirmed', order_id=42, "
                "ms_sync_status='in_progress', ms_paymentin_id=NULL, "
                "confirmed_at=? WHERE id=?"
            ),
            (_old_minutes(confirmed_minutes_ago), pid),
        )
        conn.commit()
    return pid


def _old_minutes(minutes: int) -> str:
    return (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def test_reset_stale_in_progress_payments_resets_only_old(isolated_db):
    """Stuck 'in_progress' старше порога сбрасывается → следующий cron-tick
    сможет их claim'ить. Свежие in_progress не трогаем (легальная гонка)."""
    db = isolated_db
    fresh = _make_in_progress_payment(db, confirmed_minutes_ago=5)
    stale = _make_in_progress_payment(db, confirmed_minutes_ago=45)

    reset = db.reset_stale_in_progress_payments(older_than_minutes=30)

    assert reset == 1
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT ms_sync_status FROM payments WHERE id=?"), (fresh,))
        assert cur.fetchone()[0] == "in_progress"  # моложе порога — не тронут
        cur.execute(db.q("SELECT ms_sync_status FROM payments WHERE id=?"), (stale,))
        assert cur.fetchone()[0] is None  # сброшен — следующий cron возьмёт


def test_reset_stale_skips_synced(isolated_db):
    """Уже syncнутые (ms_paymentin_id IS NOT NULL) не reset'аются даже если
    статус случайно остался 'in_progress'."""
    db = isolated_db
    pid = _make_in_progress_payment(db, confirmed_minutes_ago=120)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE payments SET ms_paymentin_id='abc-123' WHERE id=?"),
            (pid,),
        )
        conn.commit()

    reset = db.reset_stale_in_progress_payments(older_than_minutes=30)

    assert reset == 0


def test_reset_stale_noop_when_nothing_stuck(isolated_db):
    db = isolated_db
    assert db.reset_stale_in_progress_payments(older_than_minutes=30) == 0


def test_claim_sets_claimed_at(isolated_db):
    """WP-10: claim ставит ms_sync_claimed_at (по нему reaper судит устаревание)."""
    db = isolated_db
    pid = db.add_payment(1, "u", "M", 10.0, "USD", "t", order_id=42)
    assert db.claim_payment_for_ms_sync(pid) is True
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT ms_sync_status, ms_sync_claimed_at FROM payments WHERE id=?"), (pid,)
        )
        row = dict(cur.fetchone())
    assert row["ms_sync_status"] == "in_progress"
    assert row["ms_sync_claimed_at"] is not None


def test_reset_stale_uses_claim_time_not_confirmed(isolated_db):
    """WP-10: платёж подтверждён давно, но claim'ен ТОЛЬКО ЧТО (in-flight POST) —
    reaper НЕ сбрасывает. Раньше судил по confirmed_at → сбрасывал sync в полёте
    → второй paymentin в МС (дубль)."""
    db = isolated_db
    pid = _make_in_progress_payment(db, confirmed_minutes_ago=120)  # confirmed давно
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE payments SET ms_sync_claimed_at=? WHERE id=?"),
            (_old_minutes(1), pid),  # claim 1 минуту назад
        )
        conn.commit()
    assert db.reset_stale_in_progress_payments(older_than_minutes=30) == 0  # не тронут
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT ms_sync_status FROM payments WHERE id=?"), (pid,))
        assert cur.fetchone()[0] == "in_progress"
