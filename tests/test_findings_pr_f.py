"""
PR F — 5 прошлых недочётов: индексы (F1), фикс архива аудита (F2), N+1 в долге
агента (F3), reject_payment idempotency (F5). (F4 — мех. to_thread, без теста.)

Реальная БД (isolated_db); граница (Telegram/export) — мок.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import services.roles as roles


# ─── F1: индексы ─────────────────────────────────────────────────────────────


def test_perf_indexes_created(isolated_db):
    db = isolated_db
    db._create_indexes()  # идемпотентно
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
        names = {r[0] for r in cur.fetchall()}
    assert "idx_orders_agent_id" in names
    assert "idx_orders_user_created" in names
    assert "idx_audit_log_created" in names


# ─── F3: долг агента без N+1 ──────────────────────────────────────────────────


def test_agent_debt_batched_correct(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    o1 = db.create_order(1, "M", "")
    db.update_order_agent(o1, "A-1", "C")
    db.add_order_item(o1, "P", "", 2, "шт", 100.0)  # total 200
    db.update_order_status(o1, "shipped")
    o2 = db.create_order(1, "M", "")
    db.update_order_agent(o2, "A-1", "C")
    db.add_order_item(o2, "P", "", 1, "шт", 100.0)  # total 100
    db.update_order_status(o2, "shipped")

    pid = db.add_payment(
        user_id=1, username="", full_name="M", amount=50.0, currency="USD",
        comment="", order_id=o1,
    )
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status='confirmed' WHERE id=?"), (pid,))
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "created_by, status, created_at) VALUES (?, ?, ?, ?, ?, 'confirmed', ?)"
            ),
            (o1, "partial", "x", 30.0, 1, db.now_str()),
        )
        conn.commit()

    # o1: 200 − 50 платёж = 150 − 30 возврат = 120; o2: 100 → итого 220.
    assert asyncio.run(db.get_agent_current_debt("A-1")) == 220.0


# ─── F2: архив аудита ────────────────────────────────────────────────────────


def _insert_old_audit(db, created_at="2020-01-01 00:00:00"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO audit_log (user_id, full_name, role, action, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (1, "U", "admin", "a", "d", created_at),
        )
        conn.commit()


def _count_old_audit(db):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT COUNT(*) FROM audit_log WHERE created_at < '2021-01-01 00:00:00'")
        return cur.fetchone()[0]


def test_maintenance_prunes_old_audit(isolated_db):
    """run_maintenance чистит аудит старше ретеншена. Drive-архив убран —
    prune теперь безусловный (раньше гейтился на успех архивации)."""
    import tasks.run_maintenance as rm

    db = isolated_db
    _insert_old_audit(db)
    assert _count_old_audit(db) == 1
    assert rm.main() == 0
    assert _count_old_audit(db) == 0  # старый аудит вычищен


# ─── F5: reject_payment идемпотентность ──────────────────────────────────────


@pytest.fixture
def client_rp(isolated_db, monkeypatch):
    import importlib

    import services.notifier as notifier
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    boss, mgr = 100, 200
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, "A-1", "C")
    db.add_order_item(oid, "P", "", 1, "шт", 100.0)
    db.update_order_status(oid, "shipped")
    db.add_payment(
        user_id=mgr, username="", full_name="Mgr", amount=100.0, currency="USD",
        comment="", order_id=oid,
    )  # pending

    sent = []

    async def _send(uid, text, **k):
        sent.append((uid, text))

    monkeypatch.setattr(notifier, "tg_send_message", _send)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    client = TestClient(server.app)
    return client, db, {"boss": boss, "oid": oid}, sent


def test_reject_payment_idempotent(client_rp):
    client, db, ids, sent = client_rp
    body = {"initData": str(ids["boss"]), "order_id": ids["oid"], "idempotency_key": "k1"}

    r1 = client.post("/api/orders/reject_payment", json=body)
    assert r1.status_code == 200, r1.text
    assert r1.json()["rejected_count"] == 1
    sent_after_first = len(sent)
    assert sent_after_first >= 1  # менеджер уведомлён

    r2 = client.post("/api/orders/reject_payment", json=body)
    assert r2.status_code == 200
    assert r2.json()["rejected_count"] == 1  # из кэша
    assert len(sent) == sent_after_first  # повторно НЕ уведомляли
