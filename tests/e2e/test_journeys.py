"""Сквозные дни в браузере: несколько ролей, одна база, один товар.

Отдельные E2E проверяют СВОЙ шаг и заводят остальное сервисами (`seed_order`).
Здесь наоборот — вся цепочка нажатиями, как её проходят люди за день, а в
конце те же общие инварианты, что у API-сценариев (`tests/scenarios/
invariants.py`): склад сходится с накладными, деньги по заказу — с долгом,
отгруженное списано. Сценарии API-уровня гоняют бизнес-логику подробно;
этих двух хватает, чтобы убедиться, что экраны складываются в рабочий день.
"""

from __future__ import annotations

from tests.e2e.conftest import go, settled, tab
from tests.scenarios import invariants


def _invariants(e2e) -> None:
    invariants.stock_never_negative(e2e.db)
    invariants.stock_matches_invoices(e2e.db)
    invariants.money_is_consistent(e2e.db)
    invariants.orders_follow_their_status(e2e.db)


def _stock(e2e) -> float:
    return float(e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (e2e.ids["product"],))[0]["quantity"])


def _manager_builds_credit_order(e2e, mgr, qty: str, price: str) -> int:
    go(mgr, "sales")
    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#choose-agent")
    mgr.click("#choose-agent")
    mgr.wait_for_selector(".agent-row")
    mgr.click('.agent-row:has-text("Ромашка")')
    mgr.wait_for_selector("#change-agent")
    mgr.click("#btn-add-product")
    mgr.wait_for_selector(".prod-row")
    mgr.click(f'.prod-row[data-product="{e2e.ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")
    mgr.fill("#qty-input", qty)
    mgr.fill("#price-input", price)
    mgr.evaluate("window.__tgMainClick()")
    mgr.click('[data-pay="credit"]')
    mgr.wait_for_selector("#due-date-wrap:not(.hidden)")
    mgr.fill("#due-date-input", "2030-01-15")
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    mgr.click("#btn-submit")
    mgr.wait_for_function("() => window.__tgAlerts.some(a => a.includes('отправлена'))")
    return int(e2e.rows("SELECT order_id FROM shipment_requests ORDER BY id DESC LIMIT 1")[0]["order_id"])


def _boss_approves(boss) -> None:
    go(boss, "sales")
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-approve")
    boss.click(".btn-approve")
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('одобрена'))")


def test_credit_day_from_order_to_closed_debt(open_app, e2e):
    """Менеджер продаёт в кредит → босс одобряет → кладовщик отгружает →
    менеджер отмечает частичную оплату → босс подтверждает → менеджер сдаёт
    остаток наличными → босс подтверждает сдачу → долга нет."""
    ids = e2e.ids
    mgr = open_app(ids["mgr"])
    oid = _manager_builds_credit_order(e2e, mgr, "2", "100")

    boss = open_app(ids["boss"])
    _boss_approves(boss)
    assert _stock(e2e) == 18

    keeper = open_app(ids["keeper"])
    go(keeper, "sales")
    keeper.wait_for_selector(f'.btn-ship-order[data-id="{oid}"]')
    keeper.click(f'.btn-ship-order[data-id="{oid}"]')
    keeper.wait_for_function("() => window.__tgAlerts.some(a => a.startsWith('🚚'))")
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "shipped"

    mgr = open_app(ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    mgr.wait_for_selector(f'.btn-mark-paid[data-id="{oid}"]')
    mgr.fill(f'.pay-amount-input[data-id="{oid}"]', "150")
    mgr.click(f'.btn-mark-paid[data-id="{oid}"]')
    mgr.wait_for_selector(".toast:has-text('ждёт подтверждения')")

    boss = open_app(ids["boss"])
    go(boss, "money")
    tab(boss, "debts")
    sel = f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]'
    boss.wait_for_selector(sel)
    boss.click(sel)
    boss.wait_for_function("(s) => !document.querySelector(s)", arg=sel)

    tab(mgr, "ops")
    mgr.wait_for_selector("#dep-amount")
    mgr.fill("#dep-amount", "50")
    mgr.click("#dep-create")
    mgr.wait_for_selector(".toast:has-text('Сдача #')")
    dep = e2e.rows("SELECT id FROM cash_deposits")[0]["id"]

    tab(boss, "confirm")
    boss.wait_for_selector(f'.debt-card[data-dep="{dep}"] .dep-confirm')
    boss.click(f'.debt-card[data-dep="{dep}"] .dep-confirm')
    boss.wait_for_selector(".toast:has-text('Сдача подтверждена')")

    order = e2e.rows("SELECT status, payment_confirmed FROM orders WHERE id = ?", (oid,))[0]
    assert order == {"status": "paid", "payment_confirmed": 1}
    tab(boss, "debts")
    settled(boss)
    assert boss.locator(f".debt-card:has-text('#{oid}')").count() == 0
    _invariants(e2e)


def test_boss_cancels_approved_order_and_stock_comes_back(open_app, e2e):
    ids = e2e.ids
    mgr = open_app(ids["mgr"])
    oid = _manager_builds_credit_order(e2e, mgr, "5", "40")
    boss = open_app(ids["boss"])
    _boss_approves(boss)
    assert _stock(e2e) == 15

    boss = open_app(ids["boss"])  # босс вернулся к заказам позже, с новым экраном
    go(boss, "sales")
    boss.wait_for_selector(f'.btn-cancel-order[data-id="{oid}"]')
    boss.click(f'.btn-cancel-order[data-id="{oid}"]')
    boss.fill(f'.cancel-box[data-id="{oid}"] .cancel-reason', "Клиент передумал")
    boss.click(f'.cancel-send[data-id="{oid}"]')
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.startsWith('🚫'))")

    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "cancelled"
    assert _stock(e2e) == 20
    # Менеджер видит отмену у себя, а кнопки отгрузки у отменённого нет.
    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    settled(mgr)
    assert mgr.locator(f'.btn-ship-order[data-id="{oid}"]').count() == 0
    _invariants(e2e)
