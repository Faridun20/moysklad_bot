"""Продажи за день: заказ → одобрение → отгрузка → деньги → сдача → долги.

Каждый сценарий начинается с пустой базы (своя БД на Postgres), товар
приходуется накладной, продажа идёт ручками WebApp под настоящими ролями.
После сценария `conftest.world` проверяет общие инварианты.
"""

from __future__ import annotations

import pytest

from tests.scenarios import flows as f
from tests.scenarios.invariants import expect_audit

CLIENT = "ООО Ромашка"


def _shop(w, qty: float = 50) -> dict:
    """Утро: на складе есть кабель, в справочнике — клиент."""
    pid = f.create_product(w, "Кабель ВВГ 3x2.5", unit="м")
    f.incoming_invoice(w, f.MGR, [(pid, qty, 60.0)])
    cp = f.create_counterparty(w, f.MGR, CLIENT)
    return {"product": pid, "client": cp}


def _sell(w, shop, *, qty: float, price: float, payment_type: str = "credit", uid: int = f.MGR) -> dict:
    return f.create_order(
        w, uid, shop["client"], CLIENT, [(shop["product"], "Кабель ВВГ 3x2.5", qty, price)],
        payment_type=payment_type,
    )


def test_credit_order_full_cycle_to_zero_debt(world):
    """Кредит 5 × 100: одобрение списывает склад, отгрузка, частичная оплата,
    сдача остатка наличными, подтверждение — долг ноль, заказ «оплачен»."""
    w = world
    shop = _shop(w)
    order = _sell(w, shop, qty=5, price=100)
    oid = order["order_id"]
    assert f.order_status(w, oid) == "pending"
    assert f.stock(w, shop["product"]) == 50, "заявка ещё не двигает склад"

    # Менеджер не одобряет свою заявку — только руководство.
    assert w.status(f.MGR, "/api/requests/approve", req_id=order["req_id"], idempotency_key=f.key()) == 403
    f.approve(w, f.BOSS, order["req_id"])
    assert f.order_status(w, oid) == "approved"
    assert f.stock(w, shop["product"]) == 45, "одобрение проводит расходную накладную"
    assert f.api_stock(w, f.MGR, shop["product"]) == 45

    f.ship_order(w, f.KEEPER, oid)
    assert f.order_status(w, oid) == "shipped"
    debt = f.debt_of(w, f.MGR, oid)
    assert debt and debt["remaining"] == 500

    f.mark_paid(w, f.MGR, oid, 200)
    assert f.debt_of(w, f.MGR, oid)["state"] == "awaiting_confirmation"
    f.confirm_payments(w, f.BOSS, oid)
    assert f.debt_of(w, f.MGR, oid)["remaining"] == 300

    dep = f.hand_over_cash(w, f.MGR, 300)
    assert w.rows("SELECT order_id, amount_allocated_cents FROM cash_deposit_orders WHERE deposit_id = ?",
                  (dep["deposit_id"],)) == [{"order_id": oid, "amount_allocated_cents": 30000}]
    closed = f.confirm_deposit(w, f.BOSS, dep["deposit_id"])
    assert closed["closed_orders"] == [oid]

    assert f.order_status(w, oid) == "paid"
    assert f.debt_of(w, f.BOSS, oid) is None
    summary = f.debts(w, f.BOSS)
    assert summary["money_received"] == [{"currency": "USD", "total": 200.0}]
    expect_audit(w.db, "shipment_request_sent", "order_shipped", "cash_deposit_confirmed")


def test_paid_order_flow_breakdown_then_handover_and_card_confirmation(world):
    """«Оплата сразу» 3 × 80: одобрение денег не заявляет; перед отгрузкой
    менеджер вносит 140 наличными и 100 картой; руководитель подтверждает
    карту, наличные закрывает сдача — заказ оплачен, в «Долгах» его нет."""
    w = world
    shop = _shop(w)
    order = _sell(w, shop, qty=3, price=80, payment_type="paid")
    oid = order["order_id"]
    f.approve(w, f.BOSS, order["req_id"])
    assert w.rows("SELECT 1 FROM payments WHERE order_id = ?", (oid,)) == [], "автоплатежа больше нет"

    f.ship_order(w, f.KEEPER, oid, payments={"cash": 140, "card": 100})
    parts = w.rows("SELECT pp.method, pp.amount_cents, p.status FROM payment_parts pp "
                   "JOIN payments p ON p.id = pp.payment_id WHERE pp.order_id = ? ORDER BY pp.id", (oid,))
    assert parts == [{"method": "cash", "amount_cents": 14000, "status": "pending"},
                     {"method": "card", "amount_cents": 10000, "status": "pending"}]
    assert f.confirm_payments(w, f.BOSS, oid)["confirmed_count"] == 1, "кнопка подтверждает только карту"
    assert f.debt_of(w, f.BOSS, oid)["remaining"] == 140

    dep = f.hand_over_cash(w, f.MGR, 140)
    assert w.rows("SELECT order_id, amount_cents FROM cash_deposit_parts WHERE deposit_id = ?",
                  (dep["deposit_id"],)) == [{"order_id": oid, "amount_cents": 14000}]
    assert f.confirm_deposit(w, f.BOSS, dep["deposit_id"])["closed_orders"] == [oid]
    row = w.one("SELECT status, paid_confirmed_at FROM orders WHERE id = ?", (oid,))
    assert row["paid_confirmed_at"], "оплата зафиксирована на заказе"
    assert f.debt_of(w, f.BOSS, oid) is None
    assert f.stock(w, shop["product"]) == 47
    expect_audit(w.db, "order_payment_recorded", "cash_deposit_confirmed")


def test_paid_order_cannot_be_shipped_without_payment_breakdown(world):
    w = world
    shop = _shop(w)
    order = _sell(w, shop, qty=1, price=100, payment_type="paid")
    f.approve(w, f.BOSS, order["req_id"])
    code = w.status(f.KEEPER, "/api/orders/ship", order_id=order["order_id"], idempotency_key=f.key())
    assert code in (400, 409), "отгрузка оплаченного заказа без способа оплаты должна отказать"
    assert f.order_status(w, order["order_id"]) == "approved"
    # Не хватает — тоже отказ: «оплата сразу» вносится на всю сумму.
    f.record_payment(w, f.MGR, order["order_id"], {"cash": 60}, expect=400)
    f.ship_order(w, f.KEEPER, order["order_id"], payments={"cash": 60, "bank": 40})
    assert f.order_status(w, order["order_id"]) == "shipped"


def test_cancelled_order_returns_stock(world):
    """Отмена одобренного заказа возвращает товар; менеджер отменить не может;
    отгруженный — только через возврат."""
    w = world
    shop = _shop(w, qty=20)
    first = _sell(w, shop, qty=7, price=50)
    f.approve(w, f.BOSS, first["req_id"])
    assert f.stock(w, shop["product"]) == 13

    assert w.status(f.MGR, "/api/orders/cancel", order_id=first["order_id"], reason="передумал") == 403
    f.cancel_order(w, f.BOSS, first["order_id"])
    assert f.order_status(w, first["order_id"]) == "cancelled"
    assert f.stock(w, shop["product"]) == 20, "товар вернулся на склад той же транзакцией"
    assert f.debt_of(w, f.BOSS, first["order_id"]) is None

    second = _sell(w, shop, qty=2, price=50)
    f.approve(w, f.BOSS, second["req_id"])
    f.ship_order(w, f.KEEPER, second["order_id"])
    f.cancel_order(w, f.BOSS, second["order_id"], expect=409)
    assert f.stock(w, shop["product"]) == 18
    expect_audit(w.db, "order_cancelled")


def test_partial_return_restores_stock_and_debt(world):
    """Отгрузили 5 × 100 в кредит, вернули 2 «в счёт долга»: склад +2 после
    подтверждения, долг 300, заказ «частично возвращён»; второй раз больше
    доступного не вернуть."""
    w = world
    shop = _shop(w, qty=10)
    order = _sell(w, shop, qty=5, price=100)
    oid = order["order_id"]
    f.approve(w, f.BOSS, order["req_id"])
    f.ship_order(w, f.KEEPER, oid)
    assert f.stock(w, shop["product"]) == 5

    ret = f.create_return(w, f.MGR, oid, [(order["item_ids"][0], 2)])
    assert ret["total_amount"] == 200
    # Пока товар не принят, подтверждать нечего; и подтверждает только руководство.
    f.confirm_return(w, f.BOSS, ret["return_id"], expect=409)
    assert f.stock(w, shop["product"]) == 5
    f.return_goods_received(w, f.KEEPER, ret["return_id"])
    assert w.status(f.MGR, "/api/returns/confirm", return_id=ret["return_id"], idempotency_key=f.key()) == 403
    f.confirm_return(w, f.BOSS, ret["return_id"])

    assert f.stock(w, shop["product"]) == 7
    assert f.order_status(w, oid) == "partially_returned"
    assert f.debt_of(w, f.BOSS, oid)["remaining"] == 300

    f.create_return(w, f.MGR, oid, [(order["item_ids"][0], 4)], expect=400)
    f.mark_paid(w, f.MGR, oid, 300)
    f.confirm_payments(w, f.BOSS, oid)
    assert f.debt_of(w, f.BOSS, oid) is None


def test_cash_handover_is_allocated_fifo_across_orders(world):
    """Сдача 400 при двух отгруженных кредитах 300 и 200: первый закрыт, по
    второму остаток 100 — после подтверждения сдачи."""
    w = world
    shop = _shop(w)
    older = _sell(w, shop, qty=3, price=100)
    newer = _sell(w, shop, qty=2, price=100)
    for o in (older, newer):
        f.approve(w, f.BOSS, o["req_id"])
        f.ship_order(w, f.KEEPER, o["order_id"])

    dep = f.hand_over_cash(w, f.MGR, 400)
    alloc = {r["order_id"]: r["amount_allocated_cents"] for r in w.rows(
        "SELECT order_id, amount_allocated_cents FROM cash_deposit_orders WHERE deposit_id = ?",
        (dep["deposit_id"],))}
    assert alloc == {older["order_id"]: 30000, newer["order_id"]: 10000}

    f.confirm_deposit(w, f.BOSS, dep["deposit_id"])
    assert f.order_status(w, older["order_id"]) == "paid"
    assert f.debt_of(w, f.MGR, newer["order_id"])["remaining"] == 100


@pytest.mark.parametrize("shipped", [False, True], ids=["approved", "shipped"])
def test_cash_handover_for_paid_order_settles_it(world, shipped):
    """Клиент заплатил наличными при заказе «оплата сразу», менеджер сдал их в
    кассу: сдача обязана лечь на этот заказ и после подтверждения закрыть его."""
    w = world
    shop = _shop(w)
    order = _sell(w, shop, qty=2, price=150, payment_type="paid")
    oid = order["order_id"]
    f.approve(w, f.BOSS, order["req_id"])
    if shipped:
        f.ship_order(w, f.KEEPER, oid, payments={"cash": 300})
    else:
        # Деньги получены до отгрузки — внесены, товар ещё на складе.
        f.record_payment(w, f.MGR, oid, {"cash": 300})

    dep = f.hand_over_cash(w, f.MGR, 300)
    alloc = w.rows("SELECT order_id, amount_cents FROM cash_deposit_parts WHERE deposit_id = ?",
                   (dep["deposit_id"],))
    assert alloc == [{"order_id": oid, "amount_cents": 30000}], "сдача не привязана к заказу"
    assert w.rows("SELECT 1 FROM cash_deposit_orders WHERE deposit_id = ?", (dep["deposit_id"],)) == []
    f.confirm_deposit(w, f.BOSS, dep["deposit_id"])
    row = w.one("SELECT payment_confirmed, paid_confirmed_at FROM orders WHERE id = ?", (oid,))
    assert row["payment_confirmed"] or row["paid_confirmed_at"]


def test_two_managers_debts_are_scoped_and_totals_agree(world):
    """Два менеджера, по заказу каждому: менеджер видит только свои долги,
    босс — оба; итоги «к получению» у босса равны сумме строк."""
    w = world
    shop = _shop(w)
    mine = _sell(w, shop, qty=2, price=100)
    theirs = _sell(w, shop, qty=1, price=250, uid=f.MGR2)
    for o in (mine, theirs):
        f.approve(w, f.BOSS, o["req_id"])
        f.ship_order(w, f.KEEPER, o["order_id"])

    assert {d["id"] for d in f.debts(w, f.MGR)["debts"]} == {mine["order_id"]}
    assert {d["id"] for d in f.debts(w, f.MGR2)["debts"]} == {theirs["order_id"]}
    boss = f.debts(w, f.BOSS)
    assert {d["id"] for d in boss["debts"]} == {mine["order_id"], theirs["order_id"]}
    assert boss["remaining_by_currency"] == [{"currency": "USD", "total": 450.0}]
    # Чужой заказ менеджер не отмечает оплаченным и не возвращает.
    assert w.status(f.MGR, "/api/orders/mark_paid", order_id=theirs["order_id"], amount=10,
                    idempotency_key=f.key()) == 403
    f.create_return(w, f.MGR, theirs["order_id"], None, expect=403)


def test_rejected_handover_can_be_handed_over_again(world):
    """Босс отклонил сдачу («деньги не дошли») — заказ снова ждёт денег, и
    повторная сдача ложится на него же и закрывает."""
    w = world
    shop = _shop(w)
    order = _sell(w, shop, qty=3, price=100)
    f.approve(w, f.BOSS, order["req_id"])
    f.ship_order(w, f.KEEPER, order["order_id"])

    first = f.hand_over_cash(w, f.MGR, 300)
    w.call(f.BOSS, "/api/deposits/reject", deposit_id=first["deposit_id"], reason="В кассе нет")
    assert f.debt_of(w, f.MGR, order["order_id"])["remaining"] == 300

    again = f.hand_over_cash(w, f.MGR, 300)
    assert w.rows("SELECT order_id, amount_allocated_cents FROM cash_deposit_orders WHERE deposit_id = ?",
                  (again["deposit_id"],)) == [{"order_id": order["order_id"], "amount_allocated_cents": 30000}]
    f.confirm_deposit(w, f.BOSS, again["deposit_id"])
    assert f.order_status(w, order["order_id"]) == "paid"
    expect_audit(w.db, "cash_deposit_rejected", "cash_deposit_confirmed")


def test_full_return_of_unpaid_credit_clears_debt(world):
    w = world
    shop = _shop(w, qty=10)
    order = _sell(w, shop, qty=3, price=100)
    f.approve(w, f.BOSS, order["req_id"])
    f.ship_order(w, f.KEEPER, order["order_id"])
    ret = f.create_return(w, f.MGR, order["order_id"], None)
    f.return_goods_received(w, f.KEEPER, ret["return_id"])
    f.confirm_return(w, f.BOSS, ret["return_id"])
    assert f.order_status(w, order["order_id"]) == "returned"
    assert f.stock(w, shop["product"]) == 10
    assert f.debt_of(w, f.BOSS, order["order_id"]) is None


# ─── Найдено сценариями: реальные дыры, закреплённые как ожидаемое поведение ──


def test_cancelling_paid_order_voids_its_pending_payment(world):
    w = world
    shop = _shop(w)
    order = _sell(w, shop, qty=2, price=100, payment_type="paid")
    oid = order["order_id"]
    f.approve(w, f.BOSS, order["req_id"])
    f.cancel_order(w, f.BOSS, oid)

    live = w.rows("SELECT status FROM payments WHERE order_id = ? AND status IN ('pending', 'confirmed')", (oid,))
    assert live == [], "платёж отменённого заказа должен сниматься вместе с заказом"
    f.confirm_payments(w, f.BOSS, oid, expect=(200, 409))
    assert w.rows("SELECT 1 FROM payments WHERE order_id = ? AND status = 'confirmed'", (oid,)) == []


def test_order_whose_write_off_failed_cannot_be_shipped(world):
    w = world
    shop = _shop(w, qty=10)
    first = _sell(w, shop, qty=7, price=10)
    second = _sell(w, shop, qty=7, price=10)
    f.approve(w, f.BOSS, first["req_id"])
    f.approve(w, f.BOSS, second["req_id"])  # одобрение не откатывается — так задумано
    failed = w.one("SELECT invoice_id, failed_at FROM order_shipment WHERE order_id = ?", (second["order_id"],))
    assert failed["invoice_id"] is None and failed["failed_at"]

    code = w.status(f.KEEPER, "/api/orders/ship", order_id=second["order_id"], idempotency_key=f.key())
    assert code == 409, "отгрузить заказ, по которому склад не списан, нельзя"
    assert f.order_status(w, second["order_id"]) == "approved"


def test_debt_reduction_return_on_fully_paid_order_is_refused(world):
    w = world
    shop = _shop(w, qty=10)
    order = _sell(w, shop, qty=5, price=100)
    oid = order["order_id"]
    f.approve(w, f.BOSS, order["req_id"])
    f.ship_order(w, f.KEEPER, oid)
    f.mark_paid(w, f.MGR, oid, 500)
    f.confirm_payments(w, f.BOSS, oid)

    code = w.status(f.MGR, "/api/returns/create", order_id=oid, reason="Брак", refund_method="debt_reduction",
                    items=[{"item_id": order["item_ids"][0], "quantity": 2}], idempotency_key=f.key())
    assert code in (400, 409), "долга нет — уменьшать нечего"
