"""
Деньги хранятся ТОЛЬКО в копейках (T1.3): REAL-колонок денег в схеме нет,
писатели пишут *_cents, читатели считают в целых копейках.

Тесты про backfill float→копейки и про COALESCE-фолбэк читателей удалены
вместе с самим механизмом: заполнять *_cents стало неоткуда (исходных
REAL-колонок нет), а фолбэк читателя нечем накормить.
"""

import asyncio

import pytest


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(sql, params)
        conn.commit()


def _one(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(sql, params)
        return dict(cur.fetchone())


# ─── Схема: REAL-колонок денег больше нет ────────────────────────────────────

MONEY_REAL_COLUMNS = [
    ("payments", "amount"),
    ("order_items", "price"),
    ("credit_limits", "limit_amount"),
    ("cash_deposits", "amount"),
    ("cash_deposit_orders", "amount_allocated"),
    ("returns", "total_amount"),
    ("return_items", "amount"),
    ("product_prices", "sale_price"),
    ("product_prices", "cost_price"),
]


@pytest.mark.parametrize("table,column", MONEY_REAL_COLUMNS)
def test_money_real_columns_are_gone(isolated_db, table, column):
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(f"PRAGMA table_info({table})")
        cols = {r[1] for r in cur.fetchall()}
    assert cols, f"таблица {table} не создана"
    assert column not in cols, (
        f"{table}.{column} — REAL-колонка денег, должна быть только {column}_cents"
    )
    assert any(c.endswith("_cents") for c in cols), f"{table}: нет ни одной *_cents колонки"


# ─── Round-trip: точность не теряется ────────────────────────────────────────


def test_price_roundtrip_is_exact_through_db(isolated_db):
    """1234.56 переживает запись/чтение без потери копейки.

    На Postgres REAL — это float4: 1234.56 хранился как 1234.5599975585938.
    В копейках (BIGINT) значение точное.
    """
    db = isolated_db
    oid = db.create_order(1, "U")
    item_id = db.add_order_item(oid, "P", "href", 1, "шт", 1234.56)

    assert _one(db, "SELECT price_cents FROM order_items WHERE id = ?", (item_id,))[
        "price_cents"
    ] == 123456

    items = asyncio.run(db.get_order_items(oid))
    assert items[0]["price_cents"] == 123456
    assert items[0]["price"] == 1234.56  # мажорный ключ считается из копеек


def test_payment_amount_roundtrip_is_exact(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "U")
    pid = db.add_payment(1, "u", "U", 1234.56, "USD", "", order_id=oid)
    assert _one(db, "SELECT amount_cents FROM payments WHERE id = ?", (pid,))[
        "amount_cents"
    ] == 123456
    payment = asyncio.run(db.get_payment(pid))
    assert payment["amount_cents"] == 123456
    assert payment["amount"] == 1234.56


def test_order_total_sums_exactly_in_cents(isolated_db):
    """Три позиции по 1234.56 → ровно 3703.68, без float-дрейфа."""
    db = isolated_db
    oid = db.create_order(1, "U")
    for _ in range(3):
        db.add_order_item(oid, "P", "href", 1, "шт", 1234.56)

    s = asyncio.run(db.get_order_payment_summary(oid))
    assert s["total_cents"] == 370368
    assert s["total"] == 3703.68


# ─── Писатели/читатели в копейках ────────────────────────────────────────────


def test_order_payment_summary_in_cents(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "U")
    db.add_order_item(oid, "P", "href", 2, "шт", 49.99)  # 2 × 49.99 = 99.98
    db.add_payment(1, "u", "U", 49.99, "USD", "частичная", order_id=oid)

    s = asyncio.run(db.get_order_payment_summary(oid))
    assert s["total_cents"] == 9998
    assert s["pending_cents"] == 4999
    assert s["confirmed_cents"] == 0
    assert s["remaining_cents"] == 9998
    # float-поля выводятся точно из копеек.
    assert s["total"] == 99.98
    assert s["pending"] == 49.99


def test_writers_store_cents(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "U")
    item_id = db.add_order_item(oid, "P", "href", 1, "шт", 10.10)
    pid = db.add_payment(1, "u", "U", 200.0, "USD", "", order_id=oid)
    assert _one(db, "SELECT price_cents FROM order_items WHERE id = ?", (item_id,))["price_cents"] == 1010
    assert _one(db, "SELECT amount_cents FROM payments WHERE id = ?", (pid,))["amount_cents"] == 20000


def test_mark_order_paid_writes_cents(isolated_db):
    # mark_order_paid — второй путь вставки платежа; тоже пишет копейки.
    db = isolated_db
    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")
    db.add_order_item(oid, "P", "href", 1, "шт", 100.0)
    _exec(db, "UPDATE orders SET payment_type='credit' WHERE id=?", (oid,))
    db.update_order_status(oid, "shipped")  # mark_paid только по активному заказу (WP-09)

    ok, pid = asyncio.run(db.mark_order_paid(oid, 1, "Manager", amount=40.0))
    assert ok
    assert _one(db, "SELECT amount_cents FROM payments WHERE id=?", (pid,))["amount_cents"] == 4000


def test_mark_order_paid_full_uses_remaining_cents(isolated_db):
    db = isolated_db
    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")
    db.add_order_item(oid, "P", "href", 2, "шт", 49.99)  # 99.98 → 9998 коп.
    _exec(db, "UPDATE orders SET payment_type='credit' WHERE id=?", (oid,))
    db.update_order_status(oid, "shipped")  # mark_paid только по активному заказу (WP-09)

    ok, pid = asyncio.run(db.mark_order_paid(oid, 1, "Manager"))  # amount=None → весь остаток
    assert ok
    assert _one(db, "SELECT amount_cents FROM payments WHERE id=?", (pid,))["amount_cents"] == 9998
