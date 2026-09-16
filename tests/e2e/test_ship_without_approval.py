"""E2E: отгрузка без одобрения — карточка заказа менеджера до и после.

Решение владельца (сентябрь 2026): «одобрение отгрузки не нужно — менеджер сам
отмечает, что отгрузил, руководителю приходит уведомление». Здесь — то, что
видит менеджер на карточке:

* заявка, отправленная «на одобрение» до выката (решение по ней не нужно), не
  застревает: «Одобрение не нужно — можно отгружать» и кнопка «Отгрузить»;
* заявка со скидкой выше порога ждёт руководителя: причина строкой, кнопки
  отгрузки нет;
* после «Отгрузить» — статус «Отгружен», остаток списан один раз, руководителю
  ушла карточка «Заказ отгружен» без кнопок решения.

Снимки экрана (390px) — в SHIP_SHOTS_DIR, если он задан.
"""

from __future__ import annotations

import os

import pytest

from tests.e2e.conftest import go, seed_order, settled

pytestmark = pytest.mark.e2e


def _shot(page, name: str) -> None:
    folder = os.environ.get("SHIP_SHOTS_DIR")
    if folder:
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(folder, f"{name}.png"))


def _stock(e2e) -> float:
    return float(e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (e2e.ids["product"],))[0]["quantity"])


def test_legacy_request_without_decision_is_shipped_by_manager(open_app, e2e):
    ids = e2e.ids
    seeded = seed_order(e2e, approve=False, qty=3, price=100.0)  # «в долг», заявка без решения
    oid = seeded["order_id"]

    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    card = mgr.locator(f'.order-card[data-id="{oid}"]')
    card.wait_for()
    settled(mgr)
    text = card.inner_text()
    assert "Одобрение не нужно — можно отгружать" in text
    assert card.locator(".btn-ship-order").count() == 1
    card.scroll_into_view_if_needed()
    _shot(mgr, "manager-card-before-shipping")

    card.locator(".btn-ship-order").click()   # подтверждение — заглушка отвечает «да»
    mgr.wait_for_function(f"() => window.__tgAlerts.some(a => a.includes('Заказ #{oid} отгружен'))")
    mgr.wait_for_selector(f'.order-card[data-id="{oid}"][data-status="shipped"]')
    settled(mgr)
    card = mgr.locator(f'.order-card[data-id="{oid}"]')
    assert card.locator(".btn-ship-order, .btn-pay-order").count() == 0
    assert "Отгружен" in card.inner_text()
    card.scroll_into_view_if_needed()
    _shot(mgr, "manager-card-after-shipping")

    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "shipped"
    assert _stock(e2e) == 17
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices WHERE type = 'outgoing'")[0]["n"] == 1
    e2e.wait_for(lambda: any(
        m["chat_id"] == ids["boss"] and f"Заказ #{oid} отгружен" in m["text"] for m in e2e.bot.messages
    ))
    note = next(m for m in e2e.bot.messages if m["chat_id"] == ids["boss"] and f"Заказ #{oid} отгружен" in m["text"])
    assert "3 м × 100 USD = 300 USD" in note["text"] and "Итого: 300 USD" in note["text"]
    assert not note.get("reply_markup")

    # В «Решениях» у руководителя по этой заявке ничего не висит.
    boss = open_app(ids["boss"])
    go(boss, "decisions")
    settled(boss)
    assert boss.locator(f'.order-card[data-request="{seeded["req_id"]}"]').count() == 0


def test_request_with_discount_waits_for_boss_and_card_says_why(open_app, e2e):
    ids = e2e.ids
    seeded = seed_order(e2e, approve=False, qty=1, price=100.0, needs_decision=True)
    oid = seeded["order_id"]

    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    card = mgr.locator(f'.order-card[data-id="{oid}"]')
    card.wait_for()
    settled(mgr)
    assert "Ждёт решения руководителя: скидка 50% при пороге 15%" in card.inner_text()
    assert card.locator(".btn-ship-order, .btn-pay-order").count() == 0
    card.scroll_into_view_if_needed()
    _shot(mgr, "manager-card-waits-for-discount-decision")

    # Мимо кнопки — сервер тоже не отгрузит.
    res = mgr.evaluate(
        "async (id) => { const r = await apiResult('/api/orders/ship', {order_id: id}); "
        "return {status: r.status, body: r.body}; }",
        oid,
    )
    assert res["status"] == 409 and res["body"]["code"] == "decision_required"
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "pending"
    assert _stock(e2e) == 20
