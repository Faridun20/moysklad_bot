"""
Stage 2 миграции денег: backfill float→копейки, COALESCE-фолбэк читателей
и счёт сумм заказа в копейках на реальной SQLite-схеме (isolated_db).
"""


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


def test_backfill_fills_cents_from_legacy_float(isolated_db):
    db = isolated_db
    # Legacy-строки: *_cents оставляем NULL (как на старой проде до миграции).
    _exec(
        db,
        "INSERT INTO payments (user_id, username, full_name, amount, currency, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (1, "u", "U", 1500.0, "USD", db.now_str()),
    )
    _exec(
        db,
        "INSERT INTO order_items (order_id, product_name, product_href, quantity, unit, price) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (1, "P", "href", 2, "шт", 49.99),
    )

    db.run_backfills()

    assert _one(db, "SELECT amount_cents FROM payments")["amount_cents"] == 150000
    assert _one(db, "SELECT price_cents FROM order_items")["price_cents"] == 4999


def test_backfill_idempotent(isolated_db):
    db = isolated_db
    _exec(
        db,
        "INSERT INTO payments (user_id, username, full_name, amount, currency, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (1, "u", "U", 33.33, "USD", db.now_str()),
    )
    db.run_backfills()
    first = _one(db, "SELECT amount_cents FROM payments")["amount_cents"]
    db.run_backfills()  # второй прогон не должен ничего менять
    second = _one(db, "SELECT amount_cents FROM payments")["amount_cents"]
    assert first == second == 3333


def test_reader_fallback_when_cents_null(isolated_db):
    db = isolated_db
    # Без backfill: _cents == NULL → читатели берут из legacy REAL.
    assert db._amount_cents({"amount": 12.34, "amount_cents": None}) == 1234
    assert db._amount_cents({"amount": 0, "amount_cents": 7777}) == 7777
    assert db._price_cents({"price": 49.99, "price_cents": None}) == 4999
    assert db._price_cents({"price": 0, "price_cents": 100}) == 100


def test_order_payment_summary_in_cents(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "U")
    db.add_order_item(oid, "P", "href", 2, "шт", 49.99)  # 2 × 49.99 = 99.98
    db.add_payment(1, "u", "U", 49.99, "USD", "частичная", order_id=oid)

    s = db.get_order_payment_summary(oid)
    assert s["total_cents"] == 9998
    assert s["pending_cents"] == 4999
    assert s["confirmed_cents"] == 0
    assert s["remaining_cents"] == 9998
    # float-поля выводятся точно из копеек.
    assert s["total"] == 99.98
    assert s["pending"] == 49.99


def test_dual_write_populates_cents(isolated_db):
    db = isolated_db
    oid = db.create_order(1, "U")
    item_id = db.add_order_item(oid, "P", "href", 1, "шт", 10.10)
    pid = db.add_payment(1, "u", "U", 200.0, "USD", "", order_id=oid)
    assert _one(db, "SELECT price_cents FROM order_items WHERE id = ?", (item_id,))["price_cents"] == 1010
    assert _one(db, "SELECT amount_cents FROM payments WHERE id = ?", (pid,))["amount_cents"] == 20000
