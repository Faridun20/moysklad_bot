"""Техника от «в пути» до закрытой рассрочки и границы ролей через ручки.

Роли — настоящие, из `user_roles`. Менеджер временно замещает кладовщика и
бухгалтера (`services.roles.ROLE_ALSO_ACTS_AS`), но не руководство.
"""

from __future__ import annotations

import pytest

from tests.scenarios import flows as f
from tests.scenarios.invariants import expect_audit


def test_machine_in_transit_to_closed_installment(world):
    """Менеджер заводит машину в пути → босс «Прибыла» → рассрочка 12 000
    (взнос 2 000, 4 месяца) → поступления частями → последнее закрывает сделку,
    машина «Продана». Себестоимость и паспорт покупателя менеджеру не видны."""
    w = world
    mid = f.create_machine(w, f.MGR, "Экскаватор Hitachi ZX200", "hit-zx200 0001", price=12500)
    card = f.machine_card(w, f.MGR, mid)
    assert card["machine"]["status"] == "in_transit" and not card["can_manage"]

    # Себестоимость задаёт руководство; менеджер её не видит и в форме не задаёт.
    w.call(f.BOSS, "/api/machines/update", machine_id=mid, fields={"cost": 9000})
    assert "cost_cents" not in f.machine_card(w, f.MGR, mid)["machine"]
    assert f.machine_card(w, f.BOSS, mid)["machine"]["cost_cents"] == 900000

    assert w.status(f.MGR, "/api/machines/status", machine_id=mid, status="in_stock",
                    expected="in_transit") == 403
    f.machine_arrived(w, f.BOSS, mid)
    assert f.machine_card(w, f.BOSS, mid)["machine"]["status"] == "in_stock"

    # Рассрочку оформляет менеджер — это заявка: машина пока «на складе»,
    # графика и сделки нет, вторую заявку на машину не принять.
    req = f.machine_deal(w, f.MGR, mid, kind="credit", price=12000, buyer="Иванов И.И.",
                         down_payment=2000, months=4)
    assert req["pending"] and req["deal_id"] is None
    card = f.machine_card(w, f.MGR, mid)
    assert card["machine"]["status"] == "in_stock" and card["request"]["status"] == "pending"
    assert card["deals"] == [] and card["can_request"] == []
    assert not card["can_decide"], "при живом руководителе менеджер своё не одобряет"
    f.approve_machine_deal(w, f.MGR, req["request_id"], expect=403)
    f.machine_deal(w, f.BOSS, mid, kind="sale", price=13000, buyer="Другой", expect=409)
    assert w.rows("SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0

    pending = f.machine_requests(w, f.BOSS)["requests"]
    assert [r["id"] for r in pending] == [req["request_id"]]
    assert pending[0]["buyer_passport"] == "AA1234567" and pending[0]["schedule_preview"]["months"] == 4
    assert pending[0]["discount_pct"] == 4.0  # 12 000 против прайса 12 500
    deal = f.approve_machine_deal(w, f.BOSS, req["request_id"])
    assert deal["status"] == "on_credit" and deal["payments"] == 4 and deal["approval_mode"] == "boss"
    deal_id = deal["deal_id"]
    f.approve_machine_deal(w, f.BOSS, req["request_id"], expect=409)

    boss_card = f.machine_card(w, f.BOSS, mid)
    d = boss_card["deals"][0]
    assert d["progress"]["planned_cents"] == 1_200_000 and d["progress"]["left_cents"] == 1_000_000
    assert "buyer_passport" not in f.machine_card(w, f.MGR, mid)["deals"][0]

    # Клиент платит не «платёж №N», а деньги: 3 000 закрывает один и часть второго.
    # Вносит их менеджер — рассрочку ведёт он; способ пишется, как у заказа.
    f.machine_receipt(w, f.MGR, deal_id, 3000, method="card")
    assert f.machine_card(w, f.MGR, mid)["deals"][0]["receipts"][0]["method"] == "card"
    d = f.machine_card(w, f.BOSS, mid)["deals"][0]
    assert d["progress"]["left_cents"] == 700_000
    assert sum(1 for p in d["payments"] if p.get("paid_at")) == 2  # взнос + первый
    assert f.machine_card(w, f.BOSS, mid)["machine"]["status"] == "on_credit"

    f.machine_receipt(w, f.MGR, deal_id, 7000)
    card = f.machine_card(w, f.BOSS, mid)
    assert card["machine"]["status"] == "sold"
    assert card["deals"][0]["closed_at"], "последнее поступление закрывает сделку"
    f.machine_receipt(w, f.BOSS, deal_id, 100, expect=409)
    expect_audit(w.db, "machine_deal_created")
    expect_audit(w.db, "machine_deal_approved")


def test_manager_sale_rework_resubmit_and_reject_restores_nothing(world):
    """Продажа менеджера → на доработку (скидка велика) → правка цены → одобрение
    → «Продана». Бронь другой машины → отклонение: статус «На складе» как был."""
    w = world
    mid = f.create_machine(w, f.BOSS, "Погрузчик SDLG 956", "sdlg-956-01", price=40000,
                           status="in_stock")
    req = f.machine_deal(w, f.MGR, mid, kind="sale", price=30000, buyer="ООО Карьер")
    f.rework_machine_deal(w, f.BOSS, req["request_id"], "скидка 25% — много, максимум 10%")
    card = f.machine_card(w, f.MGR, mid)
    assert card["request"]["status"] == "rework" and card["machine"]["status"] == "in_stock"
    assert "максимум 10%" in card["request"]["decision_note"]
    # Чужой менеджер доработать не может; одобрить заявку на доработке нельзя.
    f.resubmit_machine_deal(w, f.MGR2, req["request_id"], price=36000, expect=403)
    f.approve_machine_deal(w, f.BOSS, req["request_id"], expect=409)
    f.resubmit_machine_deal(w, f.MGR, req["request_id"], price=36000)
    sold = f.approve_machine_deal(w, f.BOSS, req["request_id"])
    assert sold["status"] == "sold"
    assert w.rows("SELECT price_cents, created_by FROM machine_deals WHERE machine_id = ?",
                  (mid,)) == [{"price_cents": 3_600_000, "created_by": f.MGR}]

    other = f.create_machine(w, f.BOSS, "Каток XCMG XS143", "xcmg-xs143", price=20000,
                             status="in_stock")
    booking = f.machine_deal(w, f.MGR, other, kind="reserve", price=None, buyer="ИП Каримов")
    assert f.machine_card(w, f.BOSS, other)["request"]["kind"] == "reserve"
    f.reject_machine_deal(w, f.BOSS, booking["request_id"], "клиент не подтвердил")
    card = f.machine_card(w, f.MGR, other)
    assert card["machine"]["status"] == "in_stock" and card["request"] is None
    assert "reserve" in card["can_request"]
    again = f.machine_deal(w, f.MGR, other, kind="reserve", price=None, buyer="ИП Каримов",
                           approve_by=f.BOSS)
    assert again["status"] == "reserved"
    # Клиент передумал: свою бронь менеджер снимает сам, чужую — нет.
    f.unreserve_machine(w, f.MGR2, other, expect=403)
    assert f.unreserve_machine(w, f.MGR, other)["mode"] == "own"
    assert f.machine_card(w, f.MGR, other)["machine"]["status"] == "in_stock"
    expect_audit(w.db, "machine_deal_rejected")
    expect_audit(w.db, "machine_deal_returned")


def test_machine_cash_sale_and_no_double_sale(world):
    w = world
    mid = f.create_machine(w, f.BOSS, "Погрузчик XCMG LW300", "xcmg-lw300-77", status="in_stock")
    sale = f.machine_deal(w, f.BOSS, mid, kind="sale", price=30000, buyer="ООО Стройка")
    assert sale["status"] == "sold"
    f.machine_deal(w, f.BOSS, mid, kind="sale", price=31000, buyer="Другой", expect=409)
    assert len(f.machine_card(w, f.BOSS, mid)["deals"]) == 1


# ─── Роли ────────────────────────────────────────────────────────────────────

_ANY_BODY = {"order_id": 1, "req_id": 1, "deposit_id": 1, "return_id": 1, "machine_id": 1,
             "container_id": 1, "invoice_id": 1, "reason": "потому что", "amount": 10,
             "idempotency_key": "k", "type": "incoming",
             "items": [{"product_id": 1, "quantity": 1, "price_cents": None}]}

# Ручки, которые гость (и уволенный, и незнакомец) не открывает вообще.
_WORK_ENDPOINTS = [
    "/api/home", "/api/stock", "/api/wh/stock", "/api/wh/invoices/create", "/api/orders/create",
    "/api/orders/requests", "/api/requests/approve", "/api/orders/ship", "/api/orders/cancel",
    "/api/orders/mark_paid", "/api/orders/confirm_payment", "/api/debts", "/api/deposits/create",
    "/api/deposits/confirm", "/api/returns/create", "/api/returns/confirm", "/api/containers/list",
    "/api/containers/create", "/api/machines/list", "/api/machines/create", "/api/machines/deal",
    "/api/money/summary", "/api/analytics",
]

# Только руководство (менеджеру — 403, совмещение ролей этого не даёт).
# `/api/orders/confirm_payment` и `reject_payment` здесь нет: карту/перечисление
# сверяет бухгалтер, а менеджер пока бухгалтер (ROLE_ALSO_ACTS_AS,
# services/order_payments.py) — см. test_manager_confirms_card_only_as_acting_bookkeeper.
_BOSS_ONLY = [
    "/api/orders/requests", "/api/requests/approve", "/api/requests/reject", "/api/orders/cancel",
    "/api/returns/confirm",
    "/api/machines/status", "/api/machines/receipt_delete", "/api/settings/delete_requires_boss",
    "/api/containers/delete", "/api/credit/set",
]


@pytest.mark.parametrize("who", ["guest", "nobody", "fired"])
def test_guest_and_fired_get_403_everywhere(world, who):
    w = world
    if who == "fired":
        w.call(f.ADMIN, "/api/users/deactivate", user_id=f.FIRED, action="deactivate")
    uid = {"guest": f.GUEST, "nobody": f.NOBODY, "fired": f.FIRED}[who]
    codes = {path: w.status(uid, path, **_ANY_BODY) for path in _WORK_ENDPOINTS}
    assert {p: c for p, c in codes.items() if c != 403} == {}, "гостю что-то ответили не отказом"


def test_manager_is_refused_boss_actions(world):
    w = world
    codes = {path: w.status(f.MGR, path, **_ANY_BODY) for path in _BOSS_ONLY}
    assert {p: c for p, c in codes.items() if c != 403} == {}
    # Расход со склада — тоже руководство, даже с корректным телом.
    pid = f.create_product(w, "Товар")
    f.incoming_invoice(w, f.MGR, [(pid, 5, None)])
    cp = f.create_counterparty(w, f.MGR, "Клиент")
    assert w.status(f.MGR, "/api/wh/invoices/create", type="outgoing", counterparty_id=cp,
                    idempotency_key=f.key(),
                    items=[{"product_id": pid, "quantity": 1, "price_cents": 100}]) == 403
    assert f.stock(w, pid) == 5


def test_manager_confirms_card_only_as_acting_bookkeeper(world):
    """Кладовщик и гость оплату по заказу не подтверждают; менеджер — да (он
    пока бухгалтер), но наличные этой кнопкой не подтверждаются никем."""
    w = world
    pid = f.create_product(w, "Кабель")
    f.incoming_invoice(w, f.MGR, [(pid, 10, None)])
    cp = f.create_counterparty(w, f.MGR, "Клиент")
    order = f.create_order(w, f.MGR, cp, "Клиент", [(pid, "Кабель", 2, 50)])
    oid = order["order_id"]
    f.approve(w, f.BOSS, order["req_id"])
    f.ship_order(w, f.MGR, oid)
    f.record_payment(w, f.MGR, oid, {"card": 60, "cash": 40})
    for uid in (f.KEEPER, f.GUEST):
        assert w.status(uid, "/api/orders/confirm_payment", order_id=oid, idempotency_key=f.key()) == 403
    res = f.confirm_payments(w, f.MGR, oid)
    assert res["confirmed_count"] == 1 and res["skipped_cash"] == 1
    assert w.rows("SELECT p.status, pp.method FROM payments p JOIN payment_parts pp ON pp.payment_id = p.id "
                  "ORDER BY pp.id") == [{"status": "confirmed", "method": "card"},
                                         {"status": "pending", "method": "cash"}]


def test_keeper_and_bookkeeper_stay_in_their_lane(world):
    """Кладовщик отгружает и принимает возврат, но не продаёт; бухгалтер
    подтверждает сдачу, но не отгружает; менеджер замещает обоих."""
    w = world
    pid = f.create_product(w, "Кабель")
    f.incoming_invoice(w, f.MGR, [(pid, 10, None)])
    cp = f.create_counterparty(w, f.MGR, "Клиент")

    assert w.status(f.KEEPER, "/api/orders/create") == 403
    assert w.status(f.KEEPER, "/api/deposits/create", amount=10, idempotency_key=f.key()) == 403
    assert w.status(f.BOOK, "/api/orders/ship", order_id=1, idempotency_key=f.key()) == 403

    order = f.create_order(w, f.MGR, cp, "Клиент", [(pid, "Кабель", 2, 50)])
    f.approve(w, f.BOSS, order["req_id"])
    # Менеджер отгружает сам (замещает кладовщика).
    f.ship_order(w, f.MGR, order["order_id"])
    dep = f.hand_over_cash(w, f.MGR, 100)
    # Бухгалтер подтверждает сдачу.
    f.confirm_deposit(w, f.BOOK, dep["deposit_id"])
    assert f.order_status(w, order["order_id"]) == "paid"
