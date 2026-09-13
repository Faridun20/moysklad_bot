"""T2.1 — остаток по заказу считается ОДНИМ кодом во всех трёх местах.

Критерий готовности из плана:
  • заказ 1000, подтверждённый платёж 800 → все три места дают 200;
  • заказ закрыт сдачей налички → все три места дают 0;
  • деактивированный босс не в списке получателей.

«Три места» — это `/api/debts` (webapp), `get_order_payment_summary` (карточка
заказа и пуш боссу) и утреннее напоминание `tasks/run_debts_notify`. До T2.1
каждое считало по-своему: напоминание вообще игнорировало оплаты (§2.9),
а summary забывал сдачи наличных (§2.10).
"""

import asyncio

import pytest

from services.debts import calc_order_balance, calc_order_balances, calc_remaining_cents


# ─── Фикстуры сценариев ──────────────────────────────────────────────────────


def _order_1000(db, uid=1, due_date=None):
    """credit-заказ на 1000 в статусе shipped (по нему можно платить).

    due_date задаём явно: напоминание о долгах раскладывает заказы по
    «просрочено»/«сегодня» именно по нему, заказ без срока в сообщение
    не попадает вовсе.
    """
    from datetime import date

    due = due_date or date.today().isoformat()
    db.set_role(uid, "mgr", "Manager", "manager")
    oid = db.create_order(uid, "Manager", "")
    db.add_order_item(oid, "Товар", "href", 1, "шт", 1000.0)
    db.update_order_agent(oid, "A-1", "Клиент")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type='credit', currency='USD', due_date=? WHERE id=?"),
            (due, oid),
        )
        conn.commit()
    db.update_order_status(oid, "shipped")
    return oid


def _confirm_payment_of(db, oid, uid, amount):
    ok, pid = asyncio.run(db.mark_order_paid(oid, uid, "Manager", amount=amount))
    assert ok, "mark_order_paid не создал платёж"
    asyncio.run(db.confirm_payment(pid, uid, "Boss"))
    return pid


def _confirmed_deposit_for(db, oid, uid, amount):
    """Сдача наличных, распределённая на заказ, затем подтверждённая."""
    res = asyncio.run(db.create_cash_deposit(uid, amount, allocations=[(oid, amount)]))
    assert res.get("ok"), res
    asyncio.run(db.confirm_cash_deposit(res["deposit_id"], uid, "Boss"))
    return res["deposit_id"]


# ─── Сценарий 1: заказ 1000, подтверждённый платёж 800 → остаток 200 ─────────


def test_partial_payment_leaves_200(isolated_db):
    db = isolated_db
    oid = _order_1000(db)
    _confirm_payment_of(db, oid, 1, 800.0)

    # Место №1 — общий расчёт (его же зовут /api/debts и напоминание).
    assert asyncio.run(calc_remaining_cents(oid)) == 20000

    # Место №2 — карточка заказа / пуш боссу.
    summary = asyncio.run(db.get_order_payment_summary(oid))
    assert summary["remaining_cents"] == 20000
    assert summary["remaining"] == 200.0
    assert summary["confirmed_cents"] == 80000

    # Место №3 — батч, которым пользуются /api/debts и run_debts_notify.
    batch = asyncio.run(calc_order_balances([oid]))
    assert batch[oid].remaining_cents == 20000


# ─── Сценарий 2: заказ закрыт сдачей налички → остаток 0 ────────────────────


def test_order_closed_by_cash_deposit_shows_zero(isolated_db):
    """§2.10: раньше get_order_payment_summary не знал про сдачи, и заказ,
    закрытый наличкой, показывал долг в полную сумму."""
    db = isolated_db
    oid = _order_1000(db)
    _confirmed_deposit_for(db, oid, 1, 1000.0)

    assert asyncio.run(calc_remaining_cents(oid)) == 0

    summary = asyncio.run(db.get_order_payment_summary(oid))
    assert summary["remaining_cents"] == 0
    assert summary["deposits_cents"] == 100000

    batch = asyncio.run(calc_order_balances([oid]))
    assert batch[oid].remaining_cents == 0


def test_deposit_and_payment_together(isolated_db):
    """Половина сдачей, половина платежом — заказ покрыт полностью."""
    db = isolated_db
    oid = _order_1000(db)
    _confirm_payment_of(db, oid, 1, 400.0)
    _confirmed_deposit_for(db, oid, 1, 600.0)

    bal = asyncio.run(calc_order_balance(oid))
    assert bal.confirmed_cents == 40000
    assert bal.deposits_cents == 60000
    assert bal.remaining_cents == 0


# ─── Формула: границы ────────────────────────────────────────────────────────


def test_pending_payment_does_not_reduce_remaining(isolated_db):
    """Неподтверждённый платёж — ещё не деньги: остаток прежний."""
    db = isolated_db
    oid = _order_1000(db)
    ok, _pid = asyncio.run(db.mark_order_paid(oid, 1, "Manager", amount=800.0))
    assert ok

    bal = asyncio.run(calc_order_balance(oid))
    assert bal.pending_cents == 80000
    assert bal.confirmed_cents == 0
    assert bal.remaining_cents == 100000


def test_overpayment_does_not_go_negative(isolated_db):
    db = isolated_db
    oid = _order_1000(db)
    _confirm_payment_of(db, oid, 1, 1000.0)
    assert asyncio.run(calc_remaining_cents(oid)) == 0


def test_unknown_order_is_zero_not_crash(isolated_db):
    assert asyncio.run(calc_remaining_cents(999_999)) == 0
    assert asyncio.run(calc_order_balances([999_999])) == {}


def test_batch_matches_per_order(isolated_db):
    """Батч и одиночный расчёт не должны расходиться — иначе «один источник»
    снова превращается в два."""
    db = isolated_db
    o1 = _order_1000(db, uid=1)
    o2 = _order_1000(db, uid=1)
    _confirm_payment_of(db, o1, 1, 250.0)
    _confirmed_deposit_for(db, o2, 1, 400.0)

    batch = asyncio.run(calc_order_balances([o1, o2]))
    for oid in (o1, o2):
        assert batch[oid].remaining_cents == asyncio.run(calc_remaining_cents(oid))
    assert batch[o1].remaining_cents == 75000
    assert batch[o2].remaining_cents == 60000


def test_cross_currency_payment_does_not_cover_order(isolated_db):
    """WP-04: платёж в чужой валюте не должен закрывать заказ. В копейках
    «5000 UZS» больше «1000 USD», и без фильтра по валюте заказ ложно
    оказывался оплаченным."""
    db = isolated_db
    oid = _order_1000(db)  # currency='USD'
    db.add_payment(1, "u", "U", 5000.0, "UZS", "чужая валюта", order_id=oid)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status='confirmed' WHERE order_id=?"), (oid,))
        conn.commit()

    bal = asyncio.run(calc_order_balance(oid))
    assert bal.confirmed_cents == 0, "платёж в UZS не должен зачитываться в USD-заказ"
    assert bal.remaining_cents == 100000


# ─── Формула существует ровно в одном месте ─────────────────────────────────


def test_close_order_delegates_to_shared_formula():
    """`_maybe_close_order_after_payment` считает остаток тем же кодом.

    Это была четвёртая копия формулы: свой инлайновый SQL внутри
    FOR UPDATE-транзакции. Формула совпадала, но следующая правка развела бы
    их снова — а именно эта функция решает, закрывать ли заказ.
    """
    import inspect

    from services.database import _maybe_close_order_after_payment

    src = inspect.getsource(_maybe_close_order_after_payment)
    assert "calc_order_balance(order_id, conn=txn)" in src
    # Никаких собственных SUM'ов по денежным таблицам.
    for fragment in ("FROM payments", "FROM order_items", "FROM returns", "cash_deposit_orders"):
        assert fragment not in src, f"вернулась инлайновая сумма: {fragment}"


def test_closing_uses_deposits_and_currency_filter(isolated_db):
    """Поведенческая проверка того же: заказ закрывается сдачей + платежом,
    а платёж в чужой валюте его не закрывает."""
    db = isolated_db

    # Сдача 600 + платёж 400 = 1000 → заказ закрыт.
    # Путь сдачи помечает закрытие через payment_confirmed/status='paid'
    # (paid_confirmed_at заполняет только путь платежа).
    o1 = _order_1000(db)
    _confirm_payment_of(db, o1, 1, 400.0)
    _confirmed_deposit_for(db, o1, 1, 600.0)
    closed = asyncio.run(db.get_order(o1))
    assert closed["status"] == "paid" and closed["payment_confirmed"] == 1
    assert asyncio.run(calc_remaining_cents(o1)) == 0

    # Платёж в UZS по USD-заказу заказ не закрывает.
    o2 = _order_1000(db)
    db.add_payment(1, "u", "U", 5000.0, "UZS", "чужая валюта", order_id=o2)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status='confirmed' WHERE order_id=?"), (o2,))
        conn.commit()
    asyncio.run(db._maybe_close_order_after_payment(o2, 1, "Boss"))
    assert asyncio.run(db.get_order(o2))["paid_confirmed_at"] is None


# ─── Получатели напоминания ─────────────────────────────────────────────────


def test_deactivated_boss_is_not_a_recipient(isolated_db):
    """Уволенный руководитель не должен продолжать получать сводку долгов."""
    db = isolated_db
    db.set_role(501, "b1", "Активный Босс", "boss")
    db.set_role(502, "b2", "Уволенный Босс", "boss")
    asyncio.run(db.deactivate_user(502, by=1))

    users = db.get_all_users()
    bosses = [
        u for u in users if u["role"] in ("admin", "boss") and not u.get("deactivated_at")
    ]
    ids = {u["user_id"] for u in bosses}
    assert 501 in ids
    assert 502 not in ids, "деактивированный босс попал в получателей"


@pytest.mark.parametrize("is_boss_view", [False, True])
def test_notify_message_shows_remaining_not_total(isolated_db, is_boss_view):
    """§2.9: в напоминании стоит остаток, а не полная сумма заказа."""
    db = isolated_db
    from tasks.run_debts_notify import _format_message

    oid = _order_1000(db)
    _confirm_payment_of(db, oid, 1, 800.0)

    from datetime import date

    today = date.today().isoformat()
    debts = asyncio.run(db.get_open_debts(due_through=today))
    assert debts, "заказ должен быть в списке открытых долгов"
    balances = asyncio.run(calc_order_balances([d["id"] for d in debts]))

    text = _format_message(debts, balances, today, is_boss_view=is_boss_view)

    assert "200" in text, f"нет остатка 200 в сообщении: {text}"
    assert "1 000" not in text and "1000" not in text, f"показана полная сумма: {text}"
