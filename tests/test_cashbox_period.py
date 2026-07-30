"""
Касса: поступления за период (`get_cashbox_stats`) в копейках.

БД настоящая (isolated_db). T3.3: дебиторка, парсер произвольного периода и
клавиатура аналитики жили в вырезанном handlers/analytics — их тесты ушли
вместе с модулем; WebApp считает то же в webapp/server.py.
"""

# ─── helpers ──────────────────────────────────────────────────────────────────


def _payment(db, amount, currency="USD", status="confirmed", created_at=None, order_id=None):
    pid = db.add_payment(1, "m", "M", amount, currency, "", order_id=order_id)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        if created_at:
            cur.execute(
                db.q("UPDATE payments SET status = ?, created_at = ? WHERE id = ?"),
                (status, created_at, pid),
            )
        else:
            cur.execute(
                db.q("UPDATE payments SET status = ? WHERE id = ?"), (status, pid)
            )
        conn.commit()
    return pid


# ─── касса (поступления) ────────────────────────────────────────────────────


def test_cashbox_stats_sums_confirmed_in_period(isolated_db):
    db = isolated_db
    _payment(db, 100.0, "USD")  # confirmed, сейчас → учитывается (10000 коп.)
    _payment(db, 50.0, "EUR")  # confirmed, сейчас → учитывается (5000 коп.)
    _payment(db, 999.0, "USD", status="pending")  # pending → НЕ учитывается
    _payment(db, 70.0, "USD", created_at="2020-06-01 00:00:00")  # вне периода

    stats = db.get_cashbox_stats("2025-01-01 00:00:00", "2099-12-31 23:59:59")

    assert stats["total_cents"] == 15000  # 10000 + 5000
    assert stats["count"] == 2
    assert stats["by_currency"] == {"USD": 10000, "EUR": 5000}


def test_cashbox_stats_empty(isolated_db):
    db = isolated_db
    stats = db.get_cashbox_stats("2025-01-01 00:00:00", "2099-12-31 23:59:59")
    assert stats == {"total_cents": 0, "count": 0, "by_currency": {}}


# ─── клавиатура аналитики ─────────────────────────────────────────────────────


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]
