"""
Инварианты денег и состояний — на настоящей БД (isolated_db).

Самая дорогая цена бага здесь — деньги/склад. Проверяем не «функция
вернула 200», а свойства, которые ДОЛЖНЫ держаться всегда:
  - подтверждение платежа идемпотентно (повтор не удваивает сумму);
  - долг = total − confirmed, никогда не отрицательный (клампится);
  - переплата не «уносит» remaining в минус;
  - отклонённый платёж не считается и его нельзя подтвердить;
  - заказ закрывается ровно когда confirmed ≥ total.

Переходы статусов (TRANSITIONS / ролевые права) покрыты отдельно в
tests/test_order_workflow.py — здесь только денежная часть.
"""

import asyncio


def _order_with_total(db, total_price: float, qty: int = 1):
    """Создать approved-заказ с одной позицией на сумму total_price."""
    mgr_id = 200
    db.set_role(mgr_id, "mgr", "Manager", "manager")
    order_id = db.create_order(mgr_id, "Manager", "")
    db.add_order_item(order_id, "Товар", "", qty, "шт", total_price / qty)
    db.update_order_status(order_id, "approved")
    return order_id, mgr_id


# ─── Round 6: amount validation (S3) ──────────────────────────────────────────


def test_create_cash_deposit_rejects_nan_inf_and_too_large(isolated_db):
    """`amount > 0` пропускает 1e308 / NaN / inf — без верхней границы FIFO-
    математика отравляется (inf*0 = NaN), а UI боссу показывает 'nan USD'."""
    db = isolated_db
    db.set_role(300, "mgr", "Manager", "manager")
    for bad in (float("nan"), float("inf"), -1.0, 0.0, 10_000_001.0, 1e308):
        res = asyncio.run(db.create_cash_deposit(300, bad))
        assert res["ok"] is False, f"должен быть отклонён: {bad!r}"
    # Граничные валидные — проходят.
    ok = asyncio.run(db.create_cash_deposit(300, 9_999_999.99))
    assert ok["ok"] is True


def test_confirm_payment_is_idempotent(isolated_db):
    db = isolated_db
    order_id, mgr = _order_with_total(db, 400.0, qty=4)
    pid = db.add_payment(mgr, "@m", "Manager", 100.0, "USD", "ч1", order_id=order_id)

    assert asyncio.run(db.confirm_payment(pid, mgr, "Boss")) is True
    # Повторное подтверждение того же платежа — no-op (status уже confirmed).
    assert asyncio.run(db.confirm_payment(pid, mgr, "Boss")) is False

    summary = db.get_order_payment_summary(order_id)
    assert summary["confirmed"] == 100.0  # не 200 — повтор не удвоил


def test_debt_equals_total_minus_confirmed(isolated_db):
    db = isolated_db
    order_id, mgr = _order_with_total(db, 400.0, qty=4)
    p1 = db.add_payment(mgr, "@m", "Manager", 100.0, "USD", "ч1", order_id=order_id)
    db.add_payment(mgr, "@m", "Manager", 300.0, "USD", "ч2", order_id=order_id)

    # Пока ничего не подтверждено: весь долг открыт, оба — pending.
    s0 = db.get_order_payment_summary(order_id)
    assert s0["total"] == 400.0
    assert s0["confirmed"] == 0.0
    assert s0["pending"] == 400.0
    assert s0["remaining"] == 400.0

    asyncio.run(db.confirm_payment(p1, mgr, "Boss"))
    s1 = db.get_order_payment_summary(order_id)
    assert s1["confirmed"] == 100.0
    assert s1["remaining"] == 300.0  # total − confirmed
    assert s1["pending"] == 300.0  # второй ещё ждёт


def test_overpayment_does_not_drive_remaining_negative(isolated_db):
    db = isolated_db
    order_id, mgr = _order_with_total(db, 400.0, qty=4)
    pid = db.add_payment(mgr, "@m", "Manager", 500.0, "USD", "много", order_id=order_id)
    asyncio.run(db.confirm_payment(pid, mgr, "Boss"))

    s = db.get_order_payment_summary(order_id)
    assert s["confirmed"] == 500.0
    assert s["remaining"] == 0.0  # max(0, total−confirmed), не −100


def test_rejected_payment_is_not_counted_and_cannot_be_confirmed(isolated_db):
    db = isolated_db
    order_id, mgr = _order_with_total(db, 400.0, qty=4)
    pid = db.add_payment(mgr, "@m", "Manager", 100.0, "USD", "ч1", order_id=order_id)

    assert asyncio.run(db.reject_payment(pid, mgr, "Boss")) is True
    # Отклонённый не идёт ни в confirmed, ни в pending.
    s = db.get_order_payment_summary(order_id)
    assert s["confirmed"] == 0.0
    assert s["pending"] == 0.0
    # И подтвердить его уже нельзя (status != pending).
    assert asyncio.run(db.confirm_payment(pid, mgr, "Boss")) is False


def test_order_closes_exactly_when_confirmed_reaches_total(isolated_db):
    db = isolated_db
    order_id, mgr = _order_with_total(db, 400.0, qty=4)
    p1 = db.add_payment(mgr, "@m", "Manager", 100.0, "USD", "ч1", order_id=order_id)
    p2 = db.add_payment(mgr, "@m", "Manager", 300.0, "USD", "ч2", order_id=order_id)

    asyncio.run(db.confirm_payment(p1, mgr, "Boss"))
    # Частично оплачен — заказ ещё не закрыт.
    assert not db.get_order(order_id).get("paid_confirmed_at")

    asyncio.run(db.confirm_payment(p2, mgr, "Boss"))
    # confirmed (400) >= total (400) → заказ помечен оплаченным.
    assert db.get_order(order_id).get("paid_confirmed_at")
    assert db.get_order_payment_summary(order_id)["remaining"] == 0.0
