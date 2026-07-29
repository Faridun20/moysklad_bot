"""T1.8 — индексы объявлены в схеме и реально работают.

Уникальные частичные индексы — не «оптимизация», а инвариант на уровне БД:
двойной сабмит и двойной возврат по одному заказу должны падать на
constraint, а не расходиться в две заявки (и, следом, в два документа
в МойСклад с двойным списанием остатков).
"""

import sqlite3

import pytest


def _index_names(db) -> set[str]:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
        return {r[0] for r in cur.fetchall()}


def _plan(db, sql: str) -> str:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("EXPLAIN QUERY PLAN " + sql)
        return " | ".join(str(r[-1]) for r in cur.fetchall())


EXPECTED_INDEXES = [
    "idx_shipment_requests_one_pending",
    "idx_returns_one_pending",
    "idx_orders_ms_customerorder",
    "idx_orders_ms_demand",
    "idx_cash_deposit_orders_order",
    "idx_payments_order_status",
    "idx_payments_pending",
    "idx_user_roles_role",
    "idx_orders_debt_lookup",
]


@pytest.mark.parametrize("name", EXPECTED_INDEXES)
def test_index_created_by_init_db(isolated_db, name):
    assert name in _index_names(isolated_db)


def test_stale_credit_due_index_is_gone(isolated_db):
    """idx_orders_credit_due был (payment_type, paid_at, due_date) — запрос
    get_open_debts им не покрывался, поэтому заменён на idx_orders_debt_lookup."""
    assert "idx_orders_credit_due" not in _index_names(isolated_db)


def test_open_debts_query_uses_new_index(isolated_db):
    """Готовность T1.8: EXPLAIN на get_open_debts использует новый индекс."""
    plan = _plan(
        isolated_db,
        "SELECT * FROM orders WHERE payment_type = 'credit' AND paid_confirmed_at IS NULL "
        "AND status IN ('approved','shipped','partially_returned') AND (ms_deleted_at IS NULL)",
    )
    assert "idx_orders_debt_lookup" in plan, plan


# ─── Уникальность: инвариант, а не оптимизация ───────────────────────────────


def _insert_request(db, order_id: int, status: str = "pending"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO shipment_requests (order_id, user_id, full_name, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            (order_id, 1, "M", status, db.now_str()),
        )
        conn.commit()


def test_second_pending_shipment_request_is_rejected(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "M", "")
    _insert_request(db, oid)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_request(db, oid)


def test_non_pending_requests_may_repeat(isolated_db):
    """Частичность индекса: закрытых заявок по заказу может быть сколько угодно
    (заказ отклоняли и переотправляли) — уникальна только pending."""
    db = isolated_db
    oid = db.create_order(1, "M", "")
    _insert_request(db, oid, status="rejected")
    _insert_request(db, oid, status="rejected")
    _insert_request(db, oid, status="pending")  # одна активная — ок


def _insert_return(db, order_id: int, status: str = "pending"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
                "created_by, status, created_at) VALUES (?, 'partial', 'x', 100, 1, ?, ?)"
            ),
            (order_id, status, db.now_str()),
        )
        conn.commit()


def test_second_pending_return_is_rejected(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "M", "")
    _insert_return(db, oid)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_return(db, oid)


def _set_co(db, order_id: int, co_id: str):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET ms_customerorder_id = ? WHERE id = ?"), (co_id, order_id)
        )
        conn.commit()


def test_ms_customerorder_id_is_unique_across_orders(isolated_db):
    """Один документ МС не может висеть на двух заказах — иначе обратный поиск
    (find_order_by_ms_customerorder_id, LIMIT 1) отдаёт произвольный."""
    db = isolated_db
    o1 = db.create_order(1, "M", "")
    o2 = db.create_order(1, "M", "")
    _set_co(db, o1, "CO-1")
    with pytest.raises(sqlite3.IntegrityError):
        _set_co(db, o2, "CO-1")


def test_null_ms_ids_do_not_collide(isolated_db):
    """Частичный индекс: заказов без документа в МС может быть сколько угодно."""
    db = isolated_db
    for _ in range(3):
        db.create_order(1, "M", "")
    with isolated_db.get_conn() as conn:
        cur = isolated_db.get_cursor(conn)
        cur.execute("SELECT COUNT(*) FROM orders WHERE ms_customerorder_id IS NULL")
        assert cur.fetchone()[0] == 3
