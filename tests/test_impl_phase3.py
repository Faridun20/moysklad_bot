"""
IMPLEMENTATION.md Фаза 3 (сервисный слой, адаптировано под текущую схему):
кредитные лимиты, reject→draft+freeze, журнал изменений + diff resubmit,
отмена в окно, зависшие pending. Всё на настоящей SQLite (isolated_db),
кроме чистого compute_resubmit_summary.
"""

import asyncio

from services.order_workflow import compute_resubmit_summary


def _agent_order(db, total: float, qty: int = 1, status: str = "approved", agent="A-1"):
    mgr = 200
    db.set_role(mgr, "mgr", "Manager", "manager")
    oid = db.create_order(mgr, "Manager", "")
    db.update_order_agent(oid, agent, "Клиент")
    db.add_order_item(oid, "Товар", "", qty, "шт", total / qty)
    db.update_order_status(oid, status)
    return oid, mgr


# ─── Кредитные лимиты ────────────────────────────────────────────────────────


def test_credit_limit_default_and_override(isolated_db):
    db = isolated_db
    # Нет строки → дефолт из app_settings.
    assert asyncio.run(db.get_credit_limit("A-NEW")) == 2000.0
    asyncio.run(db.ensure_credit_limit("A-NEW", "Клиент"))
    assert asyncio.run(db.get_credit_limit("A-NEW")) == 2000.0
    # Изменение лимита.
    asyncio.run(db.set_credit_limit("A-NEW", "Клиент", 5000.0, set_by=1, notes="VIP"))
    assert asyncio.run(db.get_credit_limit("A-NEW")) == 5000.0


def test_current_debt_and_limit_check(isolated_db):
    db = isolated_db
    oid, mgr = _agent_order(db, 300.0, status="shipped", agent="A-2")
    # Нет подтверждённых платежей → весь total в долге.
    assert asyncio.run(db.get_agent_current_debt("A-2")) == 300.0

    over = asyncio.run(db.check_credit_limit("A-2", 1800.0))  # 300+1800=2100 > 2000
    assert over["over_limit"] is True
    assert over["projected"] == 2100.0

    under = asyncio.run(db.check_credit_limit("A-2", 100.0))  # 300+100=400 < 2000
    assert under["over_limit"] is False


def test_confirmed_return_reduces_debt(isolated_db):
    db = isolated_db
    oid, mgr = _agent_order(db, 300.0, status="shipped", agent="A-3")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
                "created_by, status, created_at) VALUES (?, ?, ?, ?, ?, 'confirmed', ?)"
            ),
            (oid, "partial", "брак", 10000, mgr, db.now_str()),
        )
        conn.commit()
    # 300 остаток − 100 возврат = 200.
    assert asyncio.run(db.get_agent_current_debt("A-3")) == 200.0


# ─── reject → draft + freeze ─────────────────────────────────────────────────


def test_reject_to_draft_increments_and_freezes(isolated_db):
    db = isolated_db
    oid, mgr = _agent_order(db, 100.0, status="pending", agent="A-4")

    r1 = asyncio.run(db.reject_order_to_draft(oid, 1, "Boss", "мало позиций"))
    assert r1["ok"] is True and r1["rejection_count"] == 1 and r1["frozen"] is False
    assert asyncio.run(db.get_order(oid))["status"] == "draft"
    assert asyncio.run(db.get_order(oid))["rejection_comment"] == "мало позиций"

    # Повторные reject'ы (нужно вернуть в pending между ними).
    db.update_order_status(oid, "pending")
    asyncio.run(db.reject_order_to_draft(oid, 1, "Boss", "ещё раз"))
    db.update_order_status(oid, "pending")
    r3 = asyncio.run(db.reject_order_to_draft(oid, 1, "Boss", "третий"))
    assert r3["rejection_count"] == 3
    assert r3["frozen"] is True  # reject_max_cycles=3
    assert asyncio.run(db.get_order(oid))["frozen"] == 1


def test_reject_only_from_pending(isolated_db):
    db = isolated_db
    oid, mgr = _agent_order(db, 100.0, status="approved", agent="A-5")
    r = asyncio.run(db.reject_order_to_draft(oid, 1, "Boss", "x"))
    assert r["ok"] is False


# ─── Журнал изменений + diff ─────────────────────────────────────────────────


def test_log_order_change_writes_row(isolated_db):
    db = isolated_db
    oid, mgr = _agent_order(db, 100.0, agent="A-6")
    db.log_order_change(
        oid, mgr, "edit", before={"x": 1}, after={"x": 2}, summary={"items_added": 1}
    )
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT change_type, summary FROM order_change_log WHERE order_id = ?"), (oid,)
        )
        row = cur.fetchone()
    assert row is not None


def test_compute_resubmit_summary():
    before = [
        {"product_href": "a", "quantity": 2, "price": 10},
        {"product_href": "b", "quantity": 1, "price": 5},
    ]
    after = [
        {"product_href": "a", "quantity": 3, "price": 10},  # modified (qty)
        {"product_href": "c", "quantity": 1, "price": 7},  # added
    ]  # b — removed
    s = compute_resubmit_summary(before, after, 25.0, 37.0, payment_type_changed=True)
    assert s["items_added"] == 1
    assert s["items_removed"] == 1
    assert s["items_modified"] == 1
    assert s["total_before"] == 25.0
    assert s["total_after"] == 37.0
    assert s["total_diff"] == 12.0
    assert s["payment_type_changed"] is True
    # Канонические копейки.
    assert s["total_before_cents"] == 2500
    assert s["total_after_cents"] == 3700
    assert s["total_diff_cents"] == 1200


def test_compute_resubmit_summary_no_float_drift():
    # Цены с дробной частью, которые во float сравнивались бы неточно.
    # Одинаковые позиции → items_modified == 0 (раньше float `!=` мог дать 1).
    before = [{"product_href": "a", "quantity": 3, "price": 0.1 + 0.2}]
    after = [{"product_href": "a", "quantity": 3, "price": 0.3}]
    s = compute_resubmit_summary(before, after, 0.9, 0.9)
    assert s["items_modified"] == 0
    assert s["total_diff_cents"] == 0


def test_compute_resubmit_summary_price_change_detected():
    before = [{"product_href": "a", "quantity": 1, "price": 10.00}]
    after = [{"product_href": "a", "quantity": 1, "price": 10.01}]
    s = compute_resubmit_summary(before, after, 10.0, 10.01)
    assert s["items_modified"] == 1
    assert s["total_diff_cents"] == 1


# ─── Отмена + зависшие pending ───────────────────────────────────────────────


def test_cancel_approved_works(isolated_db):
    """Отмена approved-заказа проходит. (M2: окно cancellation_deadline убрано —
    поле нигде не заполнялось, проверка была мёртвой; столбец удалён.)"""
    db = isolated_db
    oid, mgr = _agent_order(db, 100.0, status="approved", agent="A-7")
    r = asyncio.run(db.cancel_order(oid, 1, "Boss", "клиент передумал"))
    assert r["ok"] is True
    assert asyncio.run(db.get_order(oid))["status"] == "cancelled"


def test_cancel_blocked_for_shipped(isolated_db):
    db = isolated_db
    oid, mgr = _agent_order(db, 100.0, status="shipped", agent="A-9")
    r = asyncio.run(db.cancel_order(oid, 1, "Boss", "x"))
    assert r["ok"] is False


def test_get_stale_pending_orders(isolated_db):
    db = isolated_db
    old_oid, mgr = _agent_order(db, 100.0, status="pending", agent="A-10")
    fresh_oid, _ = _agent_order(db, 100.0, status="pending", agent="A-11")
    from datetime import datetime, timedelta

    old_ts = (datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET submitted_at = ? WHERE id = ?"), (old_ts, old_oid))
        conn.commit()

    stale_ids = {
        o["id"] for o in asyncio.run(db.get_stale_pending_orders(hours=48))
    }  # async после asyncpg Stage 8
    assert old_oid in stale_ids
    assert fresh_oid not in stale_ids
