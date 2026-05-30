"""
IMPLEMENTATION.md Фаза 5: возвраты + FEFO/партии, сервисный слой.
На настоящей SQLite (isolated_db). MS reverse-demand — отдельной фазой.
"""

import asyncio

import pytest


@pytest.fixture
def shipped_order(isolated_db):
    """Менеджер + отгруженный заказ с двумя позициями (2×100 и 1×50)."""
    db = isolated_db
    mgr = 200
    db.set_role(mgr, "mgr", "Manager", "manager")
    oid = db.create_order(mgr, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар A", "", 2, "шт", 100.0)
    db.add_order_item(oid, "Товар B", "", 1, "шт", 50.0)
    db.update_order_status(oid, "shipped")
    items = asyncio.run(db.get_order_items(oid))
    return db, mgr, oid, items


# ─── Возвраты ────────────────────────────────────────────────────────────────


def test_partial_return_sets_partially_returned(shipped_order):
    db, mgr, oid, items = shipped_order
    item_a = items[0]
    r = asyncio.run(
        db.create_return(
            oid,
            "partial",
            "брак",
            [(item_a["id"], 1, 100.0)],
            refund_method="debt_reduction",
            created_by=mgr,
        )
    )
    assert r["ok"] is True
    conf = asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))
    assert conf["ok"] is True
    assert conf["order_status"] == "partially_returned"
    assert asyncio.run(db.get_order(oid))["return_status"] == "partial"
    # returned_qty обновился у позиции A.
    a = next(i for i in asyncio.run(db.get_order_items(oid)) if i["id"] == item_a["id"])
    assert float(a["returned_qty"]) == 1.0


def test_full_return_sets_returned(shipped_order):
    db, mgr, oid, items = shipped_order
    full = [(items[0]["id"], 2, 200.0), (items[1]["id"], 1, 50.0)]
    r = asyncio.run(db.create_return(oid, "full", "отказ", full, refund_method="cash", created_by=mgr))
    conf = asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))
    assert conf["order_status"] == "returned"
    assert asyncio.run(db.get_order(oid))["status"] == "returned"


def test_cash_refund_creates_negative_deposit(shipped_order):
    db, mgr, oid, items = shipped_order
    r = asyncio.run(
        db.create_return(
            oid, "partial", "брак", [(items[1]["id"], 1, 50.0)], refund_method="cash", created_by=mgr
        )
    )
    asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT amount, status FROM cash_deposits WHERE amount < 0"))
        row = cur.fetchone()
    assert row is not None
    assert float(row["amount"] if db.USE_POSTGRES else row[0]) == -50.0


def test_return_confirm_is_idempotent(shipped_order):
    db, mgr, oid, items = shipped_order
    r = asyncio.run(
        db.create_return(
            oid, "partial", "x", [(items[0]["id"], 1, 100.0)], refund_method="no_refund", created_by=mgr
        )
    )
    assert asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))["ok"] is True
    assert asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))["ok"] is False


def test_return_blocked_for_draft(isolated_db):
    db = isolated_db
    mgr = 200
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "T", "", 1, "шт", 10.0)
    items = asyncio.run(db.get_order_items(oid))
    r = asyncio.run(
        db.create_return(
            oid, "partial", "x", [(items[0]["id"], 1, 10.0)], refund_method="no_refund", created_by=mgr
        )
    )
    assert r["ok"] is False


def test_confirmed_return_reduces_agent_debt(shipped_order):
    db, mgr, oid, items = shipped_order
    # Долг до возврата = 250 (2×100 + 1×50, без оплат).
    assert asyncio.run(db.get_agent_current_debt("A-1")) == 250.0
    r = asyncio.run(
        db.create_return(
            oid,
            "partial",
            "брак",
            [(items[0]["id"], 1, 100.0)],
            refund_method="debt_reduction",
            created_by=mgr,
        )
    )
    asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))
    # Возврат на 100 → долг 150.
    assert asyncio.run(db.get_agent_current_debt("A-1")) == 150.0


# ─── FEFO / партии ───────────────────────────────────────────────────────────


def test_batch_upsert_and_fefo_order(isolated_db):
    db = isolated_db
    db.upsert_product_batch("P1", "B-LATE", "late", "2026-12-31", 5)
    db.upsert_product_batch("P1", "B-SOON", "soon", "2026-06-01", 3)
    db.upsert_product_batch("P1", "B-NONE", "nodate", None, 10)
    # Нужно 6 → берём сначала ближайший (3), потом следующий по дате (3 из late).
    alloc = db.select_batches_fefo("P1", 6)
    takes = [a["take"] for a in alloc]
    assert takes[0] == 3.0  # B-SOON первым (раньше истекает)
    assert sum(takes) == 6.0


def test_batch_upsert_updates_existing(isolated_db):
    db = isolated_db
    bid1 = db.upsert_product_batch("P2", "B-1", "c", "2026-06-01", 5)
    bid2 = db.upsert_product_batch("P2", "B-1", "c", "2026-06-01", 8)  # тот же moysklad_batch_id
    assert bid1 == bid2
    alloc = db.select_batches_fefo("P2", 100)  # возьмём всё доступное
    assert alloc[0]["take"] == 8.0  # обновилось, не задвоилось


def test_batches_expiring_within(isolated_db):
    db = isolated_db
    from datetime import datetime, timedelta

    soon = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    far = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    db.upsert_product_batch("P3", "B-soon", "s", soon, 4)
    db.upsert_product_batch("P3", "B-far", "f", far, 4)
    codes = {
        b["batch_code"] for b in asyncio.run(db.get_batches_expiring_within(days=7))
    }  # async после asyncpg Stage 8
    assert "s" in codes and "f" not in codes
