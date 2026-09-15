"""E2E: аудит фронта — то, что видно только в настоящем браузере с живым сервером.

* подпись Telegram истекла посреди работы — экран «Сессия истекла», а
  набранная накладная возвращается после переоткрытия (localStorage);
* запрос висит — через срок «Нет подключения» с «Повторить», а не вечный
  спиннер (настоящий AbortController Chromium);
* сумма оплаты «150,5» доезжает до БД копейками и показывается с валютой;
* «Показать ещё» в заказах листает страницы сервера.
"""

from __future__ import annotations

from tests.e2e.conftest import go, seed_order, settled, tab, pay_form

import pytest

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")


def _add_position(page, product_id: int) -> None:
    page.click("#wh-add")
    page.wait_for_selector(f'.picker-list [data-pick="{product_id}"]')
    page.click(f'.picker-list [data-pick="{product_id}"]')
    page.click("#ms-submit")
    page.wait_for_selector(".c-overlay", state="detached")
    page.wait_for_selector('.wh-pos [data-f="quantity"]')


def test_session_expiry_shows_screen_and_invoice_draft_survives_reopen(open_app, e2e):
    # Сид уже содержит приходную накладную на стартовый остаток.
    before = e2e.rows("SELECT COUNT(*) AS n FROM invoices")[0]["n"]
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    boss.click("#wh-new")
    boss.wait_for_selector('[data-whtype="incoming"]')
    boss.click('[data-whtype="incoming"]')
    boss.wait_for_selector('[data-whtype="incoming"].active')
    _add_position(boss, e2e.ids["product"])
    boss.fill('.wh-pos [data-f="quantity"]', "7")
    boss.fill("#wh-comment", "Довоз после обеда")

    # Час прошёл: сервер перестал принимать подпись (заглушка verify_init_data
    # отвечает None на нечисловой initData — ровно как на просроченный).
    boss.evaluate("() => { _initData = 'expired'; }")
    boss.wait_for_function("() => !document.querySelector('#wh-save').disabled")
    boss.click("#wh-save")
    boss.wait_for_selector(".session-expired:has-text('Сессия истекла')")
    assert "Invalid" not in boss.inner_text(".session-expired")
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices")[0]["n"] == before

    # Переоткрыли приложение (свежий initData от Telegram) — черновик на месте.
    boss.reload()
    boss.wait_for_selector("#bottom-nav .nav-item")
    go(boss, "stock")
    tab(boss, "invoices")
    boss.wait_for_selector("#wh-new:has-text('Продолжить черновик')")
    boss.click("#wh-new")
    boss.wait_for_selector('.wh-pos [data-f="quantity"]')
    assert boss.input_value('.wh-pos [data-f="quantity"]') == "7"
    assert boss.input_value("#wh-comment") == "Довоз после обеда"
    boss.click("#wh-save")
    boss.wait_for_selector(".toast:has-text('проведена')")
    rows = e2e.rows("SELECT comment FROM invoices WHERE comment = ?", ("Довоз после обеда",))
    assert len(rows) == 1, "ровно одна накладная — повтор не задвоил"
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices")[0]["n"] == before + 1
    boss.wait_for_selector("#wh-new:has-text('Новая накладная')")


def test_hanging_request_ends_with_retry_instead_of_endless_spinner(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    hung = []
    # Ручка «повисла»: запрос ушёл, ответа нет. Срок сокращаем, чтобы не ждать
    # 20 секунд, — механизм тот же (AbortController + гонка со сроком).
    boss.route("**/api/stock", lambda route: hung.append(route))
    boss.evaluate(
        "() => { _netInst = createNet({ fetch: (p, i) => fetch(p, i),"
        " getInitData: () => _initData, onSessionExpired: showSessionExpired, timeoutMs: 800 }); }"
    )
    go(boss, "stock")
    boss.wait_for_selector("#content .error-card:has-text('Нет подключения')")
    assert hung, "запрос действительно висел"

    # Связь вернулась — «Повторить» загружает каталог.
    boss.unroute("**/api/stock")
    boss.click("#content .error-card button")
    boss.wait_for_selector(".stock-row")


def test_partial_payment_with_comma_reaches_db_in_cents(open_app, e2e):
    oid = seed_order(e2e)["order_id"]  # 200 USD в долг
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    btn = f'.btn-pay-debt[data-id="{oid}"]'
    mgr.wait_for_selector(btn)
    pay_form(mgr, btn, [("cash", "150,5")])
    mgr.wait_for_selector(".toast:has-text('записана')")
    toast = mgr.locator(".toast").last.text_content().replace("\u00a0", " ").replace("\u202f", " ")
    assert "150,50 USD" in toast
    assert e2e.rows("SELECT amount_cents, currency FROM payments WHERE order_id = ?", (oid,)) == [
        {"amount_cents": 15050, "currency": "USD"},
    ], "частичная оплата не превратилась в «весь остаток»"
    assert e2e.rows("SELECT method, amount_cents FROM payment_parts WHERE order_id = ?", (oid,)) == [
        {"method": "cash", "amount_cents": 15050},
    ]


def test_orders_show_more_loads_next_page_from_server(open_app, e2e):
    db, ids = e2e.db, e2e.ids
    for _ in range(53):
        oid = db.create_order(ids["mgr"], "Manager", "")
        db.update_order_agent(oid, "1", "ООО Ромашка")
        db.update_order_status(oid, "approved")

    boss = open_app(ids["boss"])
    go(boss, "sales")
    boss.wait_for_selector('.seg-item[data-filter="all"]')
    settled(boss)
    boss.wait_for_selector("#orders-more:has-text('Показать ещё (3)')")
    assert boss.locator(".order-card").count() == 50
    boss.click("#orders-more")
    boss.wait_for_function("() => document.querySelectorAll('.order-card').length === 53")
    assert boss.locator("#orders-more").count() == 0

    # Фильтр уходит на сервер: отгруженных нет — пусто, и «Показать ещё» нет.
    boss.click('.seg-item[data-filter="shipped"]')
    boss.wait_for_selector(".empty-state-title:has-text('Нет заказов')")
    assert boss.locator("#orders-more").count() == 0
