"""
Тесты janitor-чистки БД (services.database.prune_audit_log / purge_soft_deleted).
Настоящая SQLite (isolated_db); время «старим» прямой записью created_at/deleted_at.
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


def test_purge_soft_deleted(isolated_db):
    db = isolated_db
    keep = db.create_order(1, "M", "")  # активный
    old_del = db.create_order(1, "M", "")  # давно удалён
    fresh_del = db.create_order(1, "M", "")  # удалён недавно
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET deleted_at = ? WHERE id = ?"), (_old(400), old_del))
        cur.execute(db.q("UPDATE orders SET deleted_at = ? WHERE id = ?"), (_old(10), fresh_del))
        conn.commit()

    out = db.purge_soft_deleted(retention_days=365)
    assert out["orders"] == 1  # удалён только давний
    assert db.get_order(keep) is not None
    assert db.get_order(old_del) is None
    assert db.get_order(fresh_del) is not None


def test_purge_returns_all_tables_keys(isolated_db):
    db = isolated_db
    out = db.purge_soft_deleted(retention_days=365)
    assert set(out.keys()) == {"orders", "cash_deposits", "returns"}
