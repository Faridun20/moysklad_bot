"""
Регресс (аудит #2): подтверждённые возвраты уменьшают «к оплате» по заказу.
Без этого возвращённый-и-доплаченный credit-заказ не закрывался (остаток
оставался завышенным на сумму возврата) — фантомный «недоплаченный» заказ.

Покрываем: get_order_payment_summary (остаток net возвратов), mark_order_paid
(дефолт доплаты = net), авто-закрытие из confirm_payment и из confirm_return,
закрытие через сдачу наличных (confirm_cash_deposit).
"""

import asyncio


def _credit_shipped(db, total_per_unit, qty, mgr=2):
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    iid = db.add_order_item(oid, "Товар", "", qty, "шт", total_per_unit)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type='credit', due_date='2099-01-01' WHERE id=?"),
            (oid,),
        )
        conn.commit()
    return oid, iid


def _confirm_return(db, oid, iid, qty, amount, method="debt_reduction"):
    r = asyncio.run(db.create_return(oid, "partial", "брак", [(iid, qty, amount)], method, 1))
    assert r["ok"], r
    res = asyncio.run(db.confirm_return(r["return_id"], 1, "Boss"))
    assert res["ok"], res
    return r["return_id"]


def test_confirm_return_cash_records_negative_deposit_from_cents(isolated_db):
    """WP-08: cash-возврат пишет отрицательную подтверждённую сдачу из
    total_amount_cents в одной транзакции с подтверждением (касса не завышается,
    нет состояния «возврат принят, выплата не записана»)."""
    db = isolated_db
    oid, iid = _credit_shipped(db, 100.0, 5)  # total 500
    rid = _confirm_return(db, oid, iid, 2, 200.0, method="cash")  # 200 наличными
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT amount_cents, status FROM cash_deposits WHERE notes = ?"),
            (f"refund возврат #{rid}",),
        )
        row = cur.fetchone()
    assert row is not None, "cash-выплата не записана"
    d = dict(row)
    assert d["status"] == "confirmed"
    assert d["amount_cents"] == -20000  # −200.00, из cents


def test_summary_remaining_net_of_returns(isolated_db):
    db = isolated_db
    oid, iid = _credit_shipped(db, 100.0, 10)  # total 1000
    _confirm_return(db, oid, iid, 4, 400.0)     # возврат 400

    summ = asyncio.run(db.get_order_payment_summary(oid))
    assert summ["returns_cents"] == 40000
    assert summ["remaining_cents"] == 60000  # 1000 − 400
    assert summ["remaining"] == 600.0


def test_pay_remaining_after_return_closes_order(isolated_db):
    """Возврат, затем доплата остатка → mark_paid предлагает net (600), confirm
    закрывает заказ (раньше confirmed 600 < total 1000 → не закрывался)."""
    db = isolated_db
    oid, iid = _credit_shipped(db, 100.0, 10)
    _confirm_return(db, oid, iid, 4, 400.0)

    ok, pid = asyncio.run(db.mark_order_paid(oid, 2, "Mgr"))  # amount=None → остаток
    assert ok and pid
    summ = asyncio.run(db.get_order_payment_summary(oid))
    assert summ["pending_cents"] == 60000  # предложено ровно 600, не 1000
    assert asyncio.run(db.confirm_payment(pid, 1, "Boss")) is True

    o = asyncio.run(db.get_order(oid))
    assert o["paid_confirmed_at"] is not None  # заказ закрыт
    assert asyncio.run(db.get_order_payment_summary(oid))["remaining_cents"] == 0
    assert asyncio.run(db.get_agent_current_debt(o["agent_id"])) == 0


def test_return_after_payment_closes_order(isolated_db):
    """Платёж покрывает будущий net, затем возврат → confirm_return добивает
    закрытие (site 5). Платёж 600 при долге 1000 не закрыл; возврат 400 → net
    600 == confirmed 600 → закрыт."""
    db = isolated_db
    oid, iid = _credit_shipped(db, 100.0, 10)

    ok, pid = asyncio.run(db.mark_order_paid(oid, 2, "Mgr", amount=600.0))
    assert ok
    assert asyncio.run(db.confirm_payment(pid, 1, "Boss")) is True
    assert asyncio.run(db.get_order(oid))["paid_confirmed_at"] is None  # ещё не закрыт

    _confirm_return(db, oid, iid, 4, 400.0)  # возврат добивает закрытие

    assert asyncio.run(db.get_order(oid))["paid_confirmed_at"] is not None


def test_full_return_does_not_mark_paid(isolated_db):
    """Полный возврат (status='returned') — терминальный, его НЕ помечаем
    оплаченным через net<=0 (нет платежей)."""
    db = isolated_db
    oid, iid = _credit_shipped(db, 100.0, 10)
    _confirm_return(db, oid, iid, 10, 1000.0)  # весь заказ

    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "returned"
    assert o["paid_confirmed_at"] is None  # не «оплачен»


def test_partial_cash_return_does_not_close_order(isolated_db):
    """CASH-возврат НЕ уменьшает «к оплате» (деньги уже отданы из кассы отдельной
    отрицательной сдачей). Заказ, оплаченный частично, с cash-возвратом остатка
    НЕ закрывается — иначе клиента кредитуют дважды (товар назад + деньги назад)."""
    db = isolated_db
    oid, iid = _credit_shipped(db, 100.0, 10)  # 1000
    ok, pid = asyncio.run(db.mark_order_paid(oid, 2, "Mgr", amount=700.0))
    assert ok
    assert asyncio.run(db.confirm_payment(pid, 1, "Boss")) is True

    _confirm_return(db, oid, iid, 3, 300.0, method="cash")  # cash, не debt_reduction

    o = asyncio.run(db.get_order(oid))
    assert o["paid_confirmed_at"] is None  # НЕ закрыт
    summ = asyncio.run(db.get_order_payment_summary(oid))
    assert summ["returns_cents"] == 0          # cash-возврат не зачитывается в «к оплате»
    assert summ["remaining_cents"] == 30000    # всё ещё должен 300


def test_deposit_closes_order_net_of_returns(isolated_db):
    """Сдача распределена пока заказ 'shipped' (FIFO берёт только shipped); затем
    возврат; при подтверждении сдачи покрытие считается против net (1000−400=600)
    → заказ закрывается. Раньше сравнивалось с полной суммой 1000 → не закрывался."""
    db = isolated_db
    oid, iid = _credit_shipped(db, 100.0, 10)  # 1000, shipped

    res = asyncio.run(db.create_cash_deposit(2, 600.0))  # аллокация 600 на shipped-заказ
    assert res["ok"], res
    _confirm_return(db, oid, iid, 4, 400.0)              # теперь net = 600

    conf = asyncio.run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))
    assert oid in conf["closed_orders"]
    assert asyncio.run(db.get_order(oid))["payment_confirmed"] == 1
