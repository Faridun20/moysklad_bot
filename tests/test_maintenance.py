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










