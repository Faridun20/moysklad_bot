"""
Аналитика для босса: касса (поступления за период) + дебиторка (открытые
долги), и произвольный период через парсер дат.

БД настоящая (isolated_db). Деньги — в копейках. Парсер и клавиатуру тестируем
без сети/бота. Покрывает то, что просил пользователь: «сколько получил» и «за
какие заказы ещё не отдали», плюс выбор произвольного диапазона.
"""

import asyncio
from datetime import datetime


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


def _credit_order(db, agent_id, agent_name, total, due="2099-12-31", mgr=900):
    db.set_role(mgr, "mgr", "Manager", "manager")
    oid = db.create_order(mgr, "Manager", "")
    db.update_order_agent(oid, agent_id, agent_name)
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    db.update_order_status(oid, "approved")
    asyncio.run(db.set_order_payment(oid, "credit", due))
    return oid


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


# ─── дебиторка (открытые долги) ───────────────────────────────────────────────


def test_receivables_open_credit_debts(isolated_db):
    db = isolated_db
    from handlers.analytics import _receivables_from_local

    _credit_order(db, "A", "Client A", 300.0)  # remaining 30000, не просрочен
    o2 = _credit_order(db, "B", "Client B", 200.0)
    # частичная оплата 100 → remaining 10000
    pid = db.add_payment(900, "m", "M", 100.0, "USD", "", order_id=o2)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status = 'confirmed' WHERE id = ?"), (pid,))
        conn.commit()

    # paid-заказ (не credit) → не дебиторка
    paid = db.create_order(900, "Manager", "")
    db.add_order_item(paid, "Товар", "", 1, "шт", 500.0)
    db.update_order_status(paid, "approved")  # payment_type по умолчанию 'paid'

    # credit полностью оплачен → remaining 0 → не в списке
    o4 = _credit_order(db, "D", "Client D", 100.0)
    p4 = db.add_payment(900, "m", "M", 100.0, "USD", "", order_id=o4)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status = 'confirmed' WHERE id = ?"), (p4,))
        conn.commit()

    # просроченный долг (due в прошлом)
    _credit_order(db, "C", "Client C", 80.0, due="2020-01-01")  # overdue, 8000

    r = asyncio.run(_receivables_from_local(None))

    assert r["total_cents"] == 48000  # 30000 + 10000 + 8000
    assert r["count"] == 3  # A, B, C (D с нулевым остатком отброшен)
    assert r["overdue_count"] == 1
    assert r["overdue_cents"] == 8000
    # сортировка по убыванию остатка
    assert r["debtors"][0]["agent_name"] == "Client A"
    assert r["debtors"][0]["remaining_cents"] == 30000
    overdue = [d for d in r["debtors"] if d["overdue"]]
    assert len(overdue) == 1 and overdue[0]["agent_name"] == "Client C"


def test_receivables_empty(isolated_db):
    from handlers.analytics import _receivables_from_local

    r = asyncio.run(_receivables_from_local(None))
    assert r == {
        "total_cents": 0,
        "count": 0,
        "overdue_count": 0,
        "overdue_cents": 0,
        "debtors": [],
    }


# ─── парсер произвольного периода ─────────────────────────────────────────────


def test_parse_period_input_valid_space():
    from handlers.analytics import _parse_period_input

    res = _parse_period_input("01.01.2026 31.03.2026")
    assert res is not None
    since, until = res
    assert since == datetime(2026, 1, 1, 0, 0, 0)
    assert until == datetime(2026, 3, 31, 23, 59, 59)


def test_parse_period_input_dash_and_short_year():
    from handlers.analytics import _parse_period_input

    res = _parse_period_input("1.1.26-5.5.26")
    assert res is not None
    since, until = res
    assert since.year == 2026 and since.month == 1 and since.day == 1
    assert until.year == 2026 and until.month == 5 and until.day == 5


def test_parse_period_input_rejects_bad():
    from handlers.analytics import _parse_period_input

    assert _parse_period_input("привет") is None
    assert _parse_period_input("01.01.2026") is None  # только одна дата
    assert _parse_period_input("31.03.2026 01.01.2026") is None  # from > to
    assert _parse_period_input("99.99.2026 01.01.2026") is None  # невалидная дата


# ─── клавиатура аналитики ─────────────────────────────────────────────────────


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_analytics_keyboard_boss_has_cashbox_and_custom():
    from utils.keyboards import analytics_keyboard

    cbs = _callbacks(analytics_keyboard("sales", "week", is_boss=True))
    assert "an:sales:week" in cbs
    assert "anp:sales" in cbs  # «Свой период»
    assert "an:cash:week" in cbs  # переключатель «Касса»
    assert "menu" in cbs


def test_analytics_keyboard_manager_no_cashbox():
    from utils.keyboards import analytics_keyboard

    cbs = _callbacks(analytics_keyboard("sales", "month", is_boss=False))
    assert not any(c.startswith("an:cash") for c in cbs)
    assert "anp:sales" in cbs


def test_analytics_keyboard_cash_view_toggles_back_to_sales():
    from utils.keyboards import analytics_keyboard

    cbs = _callbacks(analytics_keyboard("cash", "custom", is_boss=True))
    # custom → переключатель откатывается на month, но кликабелен
    assert "an:sales:month" in cbs
    assert "anp:cash" in cbs
