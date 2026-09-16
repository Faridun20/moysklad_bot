"""E2E: журнал действий боссу (C1) и история заказа на карточке (C3).

Настоящий Chromium против живого uvicorn — ровно то, что «сборка правильных
кусков» юнитами не ловит: экран открывается из «Настройки», фильтр периода
реально уходит на сервер и список меняется, «История» на карточке заказа
разворачивается и показывает реальные события, записанные сервисами при
`seed_order` (заявка подана/одобрена).
"""

from __future__ import annotations

from tests.e2e.conftest import go, seed_order, settled, tab


def test_boss_opens_audit_log_and_filters_by_date(open_app, e2e):
    seed_order(e2e)  # submit_order + approve_shipment_request — настоящие audit_log записи

    page = open_app(e2e.ids["boss"])
    go(page, "settings")
    page.click("#set-audit-log")
    page.wait_for_selector(".audit-row")
    assert page.locator(".audit-row").count() > 0
    # Заявка одобрена руководителем — запись должна быть видна боссу.
    assert "Заявка на отгрузку одобрена" in page.locator(".audit-row").first.inner_text() \
        or page.locator("text=Заявка на отгрузку одобрена").count() > 0

    # Фильтр «Сегодня» — реальный запрос на сервер, список не пустеет
    # (всё засеяно только что).
    page.click('[data-alperiod="today"]')
    settled(page)
    page.wait_for_selector(".audit-row")
    assert page.locator(".audit-row").count() > 0


def test_manager_has_no_audit_log_entry_point(open_app, e2e):
    """У менеджера «Настройки» — только «Реквизиты компании»: журнала там нет —
    сторож на UI-слое; сервер уже проверен юнитами (tests/test_audit_log_api.py)."""
    page = open_app(e2e.ids["mgr"])
    assert page.locator('#bottom-nav .nav-item[data-screen="settings"]').count() == 0
    go(page, "settings")
    page.wait_for_selector("#set-company")
    assert page.locator("#set-audit-log").count() == 0


def test_order_card_shows_timeline_after_actions(open_app, e2e):
    order = seed_order(e2e)

    page = open_app(e2e.ids["boss"])
    go(page, "sales")
    tab(page, "orders")
    settled(page)
    card = page.locator(f'.order-card[data-id="{order["order_id"]}"]')
    card.locator("[data-timeline-toggle]").click()
    box = page.locator(f'#order-timeline-{order["order_id"]}')
    box.wait_for(state="visible")
    page.wait_for_function(
        "(id) => document.getElementById('order-timeline-' + id)"
        ".textContent.includes('Заказ создан')",
        arg=order["order_id"],
    )
    text = box.inner_text()
    assert "Заказ создан" in text
    assert "Заявка на отгрузку" in text

    # Повторный клик сворачивает ленту.
    card.locator("[data-timeline-toggle]").click()
    assert box.is_hidden()
