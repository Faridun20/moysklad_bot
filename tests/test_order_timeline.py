"""
История заказа (C3, services/order_timeline.py) — сборка ленты решений из
структурных таблиц (shipment_requests/payments/cash_deposit_orders+
cash_deposits/returns) и денормализованных полей orders. Не через audit_log
(там нет колонки order_id, см. докстринг модуля) — сторож этого решения:
здесь строки таблиц заводятся напрямую, без бота/WebApp, и ассерты бьют по
полям СОБРАННОЙ ленты, а не по тексту audit_log.
"""

from __future__ import annotations

import asyncio

import pytest

MGR, BOSS, KEEPER = 1, 2, 3


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(isolated_db):
    isolated_db.set_role(MGR, "mgr", "Менеджер Иван", "manager")
    isolated_db.set_role(BOSS, "boss", "Руководитель Пётр", "boss")
    isolated_db.set_role(KEEPER, "keeper", "Кладовщик Олег", "warehouse_keeper")
    return isolated_db


def _order(db, *, uid=MGR, created_at="2026-01-01 10:00:00") -> int:
    oid = db.create_order(uid, "Менеджер Иван", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET created_at=? WHERE id=?"), (created_at, oid))
        conn.commit()
    return oid


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        conn.commit()


def _texts(events):
    return [e["text"] for e in events]


def _actions(events):
    return [e["action"] for e in events]


def test_timeline_missing_order_is_empty(db):
    from services.order_timeline import build_order_timeline

    assert _run(build_order_timeline(999999)) == []


def test_timeline_draft_order_has_only_creation(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    events = _run(build_order_timeline(oid))
    assert _actions(events) == ["order_created"]
    assert events[0]["actor"] == "Менеджер Иван"


def test_timeline_submitted_and_approved(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    _exec(
        db,
        "UPDATE orders SET submitted_at = ? WHERE id = ?",
        ("2026-01-01 10:05:00", oid),
    )
    _exec(
        db,
        "INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, "
        "created_at, approved_by, approved_by_name, approved_at) "
        "VALUES (?, ?, ?, 'approved', '', ?, ?, ?, ?)",
        (oid, MGR, "Менеджер Иван", "2026-01-01 10:05:00", BOSS, "Руководитель Пётр",
         "2026-01-01 11:00:00"),
    )
    events = _run(build_order_timeline(oid))
    assert _actions(events) == [
        "order_created", "order_submitted", "shipment_requested", "shipment_approved",
    ]
    approved = events[-1]
    assert approved["actor"] == "Руководитель Пётр"
    assert approved["ts"] == "2026-01-01 11:00"
    # Хронологический порядок — по возрастанию времени, а не по порядку вставки.
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)


@pytest.mark.parametrize(
    ("status", "action"),
    [("returned", "shipment_returned"), ("rejected", "shipment_rejected")],
)
def test_timeline_shipment_request_rework_or_rejected(db, status, action):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    _exec(
        db,
        "INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, "
        "created_at, approved_by, approved_by_name, approved_at) "
        f"VALUES (?, ?, ?, '{status}', '', ?, ?, ?, ?)",
        (oid, MGR, "Менеджер Иван", "2026-01-01 10:05:00", BOSS, "Руководитель Пётр",
         "2026-01-01 11:00:00"),
    )
    events = _run(build_order_timeline(oid))
    assert action in _actions(events)


def test_timeline_shipped_resolves_actor_name(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    _exec(
        db,
        "UPDATE orders SET status='shipped', shipped_at=?, shipped_by=? WHERE id=?",
        ("2026-01-01 12:00:00", KEEPER, oid),
    )
    events = _run(build_order_timeline(oid))
    shipped = next(e for e in events if e["action"] == "order_shipped")
    assert shipped["actor"] == "Кладовщик Олег"


def test_timeline_payments_sent_confirmed_rejected(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    _exec(
        db,
        "INSERT INTO payments (user_id, full_name, amount_cents, currency, comment, status, "
        "order_id, created_at, confirmed_at) VALUES (?, ?, ?, 'USD', '', 'confirmed', ?, ?, ?)",
        (MGR, "Менеджер Иван", 10000, oid, "2026-01-01 12:10:00", "2026-01-01 13:00:00"),
    )
    _exec(
        db,
        "INSERT INTO payments (user_id, full_name, amount_cents, currency, comment, status, "
        "order_id, created_at, confirmed_at) VALUES (?, ?, ?, 'USD', '', 'rejected', ?, ?, ?)",
        (MGR, "Менеджер Иван", 5000, oid, "2026-01-01 12:20:00", "2026-01-01 13:10:00"),
    )
    events = _run(build_order_timeline(oid))
    assert _actions(events).count("payment_sent") == 2
    assert "payment_confirmed" in _actions(events)
    assert "payment_rejected" in _actions(events)


def test_timeline_fully_paid_uses_denormalized_name(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    _exec(
        db,
        "UPDATE orders SET paid_confirmed_at=?, paid_confirmed_by=?, "
        "paid_confirmed_by_name=? WHERE id=?",
        ("2026-01-01 14:00:00", BOSS, "Руководитель Пётр", oid),
    )
    events = _run(build_order_timeline(oid))
    fully_paid = next(e for e in events if e["action"] == "order_fully_paid")
    assert fully_paid["actor"] == "Руководитель Пётр"


def test_timeline_cash_deposit_created_and_confirmed(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO cash_deposits (manager_id, amount_cents, confirmed_by, "
                "confirmed_at, status, created_at) VALUES (?, ?, ?, ?, 'confirmed', ?)"
            ),
            (MGR, 10000, BOSS, "2026-01-01 15:00:00", "2026-01-01 14:30:00"),
        )
        deposit_id = cur.lastrowid
        conn.commit()
    _exec(
        db,
        "INSERT INTO cash_deposit_orders (deposit_id, order_id, amount_allocated_cents, "
        "is_manual) VALUES (?, ?, ?, 0)",
        (deposit_id, oid, 10000),
    )
    events = _run(build_order_timeline(oid))
    assert "cash_deposit_created" in _actions(events)
    confirmed = next(e for e in events if e["action"] == "cash_deposit_confirmed")
    assert confirmed["actor"] == "Руководитель Пётр"
    created = next(e for e in events if e["action"] == "cash_deposit_created")
    assert created["actor"] == "Менеджер Иван"


def test_timeline_return_confirmed_and_rejected(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    _exec(
        db,
        "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
        "refund_method, created_by, confirmed_by, status, created_at, confirmed_at) "
        "VALUES (?, 'partial', 'брак', 5000, 'cash', ?, ?, 'confirmed', ?, ?)",
        (oid, KEEPER, BOSS, "2026-01-02 09:00:00", "2026-01-02 10:00:00"),
    )
    events = _run(build_order_timeline(oid))
    assert "return_created" in _actions(events)
    confirmed = next(e for e in events if e["action"] == "return_confirmed")
    assert confirmed["actor"] == "Руководитель Пётр"


def test_timeline_cancelled_includes_reason(db):
    from services.order_timeline import build_order_timeline

    oid = _order(db)
    _exec(
        db,
        "UPDATE orders SET status='cancelled', cancelled_at=?, cancelled_by=?, "
        "cancellation_reason=? WHERE id=?",
        ("2026-01-03 09:00:00", BOSS, "клиент отказался", oid),
    )
    events = _run(build_order_timeline(oid))
    cancelled = next(e for e in events if e["action"] == "order_cancelled")
    assert cancelled["actor"] == "Руководитель Пётр"
    assert "клиент отказался" in cancelled["text"]


def test_timeline_full_lifecycle_is_chronological(db):
    """Все узлы сразу — сортировка по времени, а не по порядку вставки/типу."""
    from services.order_timeline import build_order_timeline

    oid = _order(db, created_at="2026-01-01 09:00:00")
    _exec(db, "UPDATE orders SET submitted_at=? WHERE id=?", ("2026-01-01 09:05:00", oid))
    _exec(
        db,
        "INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, "
        "created_at, approved_by, approved_by_name, approved_at) "
        "VALUES (?, ?, ?, 'approved', '', ?, ?, ?, ?)",
        (oid, MGR, "Менеджер Иван", "2026-01-01 09:05:00", BOSS, "Руководитель Пётр",
         "2026-01-01 10:00:00"),
    )
    _exec(
        db,
        "UPDATE orders SET status='shipped', shipped_at=?, shipped_by=? WHERE id=?",
        ("2026-01-01 11:00:00", KEEPER, oid),
    )
    _exec(
        db,
        "UPDATE orders SET paid_confirmed_at=?, paid_confirmed_by=?, "
        "paid_confirmed_by_name=? WHERE id=?",
        ("2026-01-01 12:00:00", BOSS, "Руководитель Пётр", oid),
    )
    events = _run(build_order_timeline(oid))
    assert len(events) == 6
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    assert _actions(events) == [
        "order_created", "order_submitted", "shipment_requested", "shipment_approved",
        "order_shipped", "order_fully_paid",
    ]
