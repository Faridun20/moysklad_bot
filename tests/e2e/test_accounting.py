"""E2E: бухгалтерия — от выключателя до закрытия дня.

Сценарий владельца целиком, в настоящем браузере: руководитель включает
бухгалтерию и заводит счета, менеджер получает оплату долга частью наличными
долларами и частью на карту сумами по СВОЕМУ курсу, руководитель подтверждает —
долг закрыт; «Деньги сейчас» показывают остатки в валюте счетов, пересчёт кассы
фиксирует расхождение сверкой. И отдельно — что при выключенной бухгалтерии
экраны прежние.

Продажа заводится сервисами (`seed_order`), как в остальных сценариях денег.
"""

from __future__ import annotations

import pytest

from tests.e2e import test_cov_click_everything as _crawler
from tests.e2e.conftest import go, seed_order, settled, tab

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")

# Фикстуры обходчика (принтер-заглушка, лимитер выключен) — присваиванием, а не
# импортом имён: pytest находит фикстуру по имени в модуле, а импорт того же
# имени, затенённый параметром теста, линтер считает переопределением.
printer = _crawler.printer
no_rate_limit = _crawler.no_rate_limit

CBU_UZS = 12650.5


def _sheet(page):
    """Верхняя открытая шторка (формы открываются поверх друг друга)."""
    return page.locator(".c-overlay").last


def _submit(page):
    _sheet(page).locator("#ms-submit").click()


def _add_account(boss, name, kind, currency, **fields):
    boss.click("#acc-add-account")
    sheet = _sheet(boss)
    sheet.locator("#ms-f-name").fill(name)
    sheet.locator(f'.seg-item[data-opt="{kind}"]').click()
    sheet.locator(f'.seg-item[data-opt="{currency}"]').click()
    for key, value in fields.items():
        sheet.locator(f"#ms-f-{key}").fill(str(value))
    _submit(boss)
    boss.wait_for_selector(f'[data-acc-edit]:has-text("{name}")')


def test_accounting_flow_receipt_two_currencies_then_close_day(open_app, e2e):
    from services import accounting as acc

    ids = e2e.ids
    e2e.db.set_currency_rate("UZS", 1 / CBU_UZS, ids["boss"])
    e2e.db.set_currency_rate_daily("UZS", acc.today_str(), 1 / CBU_UZS, "cbu")
    oid = seed_order(e2e)["order_id"]  # кредит 2 × 100 = 200 USD

    # ── Руководитель: включить и завести счета ──
    boss = open_app(ids["boss"])
    go(boss, "money")
    tab(boss, "ops")
    boss.wait_for_selector("#acc-enable")
    boss.click("#acc-enable")
    _submit(boss)
    boss.wait_for_selector("#acc-add-account")
    assert e2e.rows("SELECT value FROM app_settings WHERE key = 'accounting_enabled'")[0]["value"] == "true"
    _add_account(boss, "Касса USD", "cash", "USD")
    _add_account(boss, "Humo Али", "card", "UZS", bank="Humo", card_last4="1234", holder="Али")
    accounts = e2e.rows("SELECT name, kind, currency, card_last4, holder FROM acc_accounts ORDER BY id")
    assert accounts == [
        {"name": "Касса USD", "kind": "cash", "currency": "USD", "card_last4": None, "holder": None},
        {"name": "Humo Али", "kind": "card", "currency": "UZS", "card_last4": "1234", "holder": "Али"},
    ]

    # ── Менеджер: «Получил деньги» в «Долгах» ──
    mgr = open_app(ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    mgr.wait_for_selector(f'.acc-pay[data-acc-order="{oid}"]')
    assert mgr.locator(".btn-pay-debt").count() == 0, "кнопка разбивки заменена «Получил деньги»"
    mgr.click(f'.acc-pay[data-acc-order="{oid}"]')
    sheet = _sheet(mgr)
    sheet.locator(".acc-line").first.wait_for()
    assert "Касса USD" in sheet.locator(".acc-line").first.inner_text()
    sheet.locator(".acc-line-amount").first.fill("80")
    sheet.locator("#acc-add-line").click()
    assert "Humo Али" in sheet.locator(".acc-line").nth(1).inner_text()
    sheet.locator(".acc-line-amount").nth(1).fill("1 524 000")
    # Курс по умолчанию — ЦБ; менеджер ставит свой.
    assert sheet.locator("#acc-rate-UZS").input_value() == "12650.5"
    sheet.locator("#acc-rate-UZS").fill("12700")
    mgr.wait_for_function(
        "() => /200\\s*USD/.test(document.querySelector('.c-overlay:last-of-type #acc-total')?.textContent || '')"
    )
    assert "Останется: 0" in sheet.locator("#acc-total").inner_text().replace("\u00a0", " ")
    _submit(mgr)
    mgr.wait_for_selector(".toast:has-text('ждёт подтверждения')")

    pays = e2e.rows("SELECT amount_cents, currency, status FROM payments WHERE order_id = ?", (oid,))
    assert pays == [{"amount_cents": 20000, "currency": "USD", "status": "pending"}]
    entries = e2e.rows(
        "SELECT a.name, e.amount_cents, e.currency, e.rate, e.rate_source, e.cbu_rate "
        "FROM acc_entries e JOIN acc_accounts a ON a.id = e.account_id ORDER BY e.id"
    )
    assert entries == [
        {"name": "Касса USD", "amount_cents": 8000, "currency": "USD", "rate": "1",
         "rate_source": "base", "cbu_rate": "1"},
        {"name": "Humo Али", "amount_cents": 152400000, "currency": "UZS", "rate": "12700",
         "rate_source": "manual", "cbu_rate": "12650.5"},
    ]

    # ── Руководитель подтверждает в «Долгах» — долг закрыт ──
    boss.reload()
    boss.wait_for_selector("#bottom-nav .nav-item")
    go(boss, "money")
    tab(boss, "debts")
    sel = f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]'
    boss.wait_for_selector(sel)
    boss.click(sel)
    boss.wait_for_function("(s) => !document.querySelector(s)", arg=sel)
    order = e2e.rows("SELECT paid_confirmed_at FROM orders WHERE id = ?", (oid,))[0]
    assert order["paid_confirmed_at"], "долг закрыт подтверждением"
    settled(boss)
    assert boss.locator(f'.acc-pay[data-acc-order="{oid}"]').count() == 0

    # ── «Деньги сейчас» ──
    tab(boss, "ops")
    boss.wait_for_selector("[data-acc-account]")
    rows = {
        boss.locator(f'[data-acc-account] .card-row-title >> nth={i}').inner_text():
            boss.locator(f'[data-acc-account] .card-row-value >> nth={i}').inner_text().replace("\u202f", " ").replace("\u00a0", " ")
        for i in range(boss.locator("[data-acc-account]").count())
    }
    assert rows == {"Касса USD": "80 USD", "Humo Али": "1 524 000 UZS"}
    total = boss.locator("#acc-total").inner_text()
    assert "USD" in total

    # ── Закрыть день: в кассе 75 вместо 80 ──
    boss.click("#acc-close")
    sheet = _sheet(boss)
    assert "Касса USD" in sheet.locator(".acc-account-pick").inner_text()
    sheet.locator("#ms-f-counted").fill("75")
    assert "Разница" in sheet.locator("#acc-close-info").inner_text()
    _submit(boss)
    # Без причины расхождение не принимается — ошибка внутри формы.
    boss.wait_for_selector(".c-overlay #ms-error:not([hidden])")
    assert "причину" in sheet.locator("#ms-error").inner_text()
    sheet.locator("#ms-f-note").fill("Сдачу дали из кассы")
    _submit(boss)
    boss.wait_for_selector(".toast:has-text('расхождение')")
    closes = e2e.rows("SELECT expected_cents, counted_cents, diff_cents FROM acc_day_closes")
    assert closes == [{"expected_cents": 8000, "counted_cents": 7500, "diff_cents": -500}]
    boss.wait_for_selector('[data-acc-account]:has-text("75 USD")')
    assert "день закрыт" in boss.locator('[data-acc-account]:has-text("Касса USD")').inner_text()

    # ── Журнал: поступление и сверка видны, фильтр по типу работает ──
    boss.click('[data-acc-view="journal"]')
    boss.wait_for_selector("[data-acc-doc]")
    text = boss.locator("#content").inner_text()
    assert "Получили" in text and "Закрытие дня" in text
    boss.click('[data-acc-kind="reconcile"]')
    boss.wait_for_function(
        "() => document.querySelector('[data-acc-kind=\"reconcile\"]')?.classList.contains('active')"
    )
    settled(boss)
    assert "Получили" not in boss.locator("#content").inner_text()


def test_accounting_off_keeps_old_screens(open_app, e2e):
    """Выключатель выключен: «Внести оплату» (разбивка) в долгах, старая «Касса»,
    кнопка включения — только руководителю."""
    oid = seed_order(e2e)["order_id"]
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    mgr.wait_for_selector(f'.btn-pay-debt[data-id="{oid}"]')
    assert mgr.locator(".acc-pay").count() == 0
    tab(mgr, "ops")
    mgr.wait_for_selector("#dep-amount")
    assert mgr.locator("#acc-enable, [data-acc-view]").count() == 0

    boss = open_app(e2e.ids["boss"])
    go(boss, "money")
    tab(boss, "ops")
    boss.wait_for_selector("#acc-enable")
    assert boss.locator("[data-acc-view]").count() == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM acc_docs")[0]["n"] == 0


# ─── Обходчик при включённой бухгалтерии ─────────────────────────────────────


def _seed_accounting(e2e, debt_order_id: int) -> None:
    """Счета и по документу каждого вида — чтобы обходчику было что нажимать."""
    from services import accounting as acc

    ids = e2e.ids
    boss = acc.Actor(ids["boss"], "Boss", "boss")
    mgr = acc.Actor(ids["mgr"], "Manager", "manager")
    e2e.db.set_currency_rate_daily("UZS", acc.today_str(), 1 / CBU_UZS, "cbu")
    e2e.run(acc.set_enabled(boss, True))
    cash = e2e.run(acc.save_account(boss, {"name": "Касса USD", "kind": "cash", "currency": "USD",
                                           "opening": "500"}))["id"]
    card = e2e.run(acc.save_account(boss, {"name": "Humo", "kind": "card", "currency": "UZS",
                                           "card_last4": "1234"}))["id"]
    e2e.run(acc.record_receipt(mgr, {"order_id": debt_order_id, "idempotency_key": "crawl-r",
                                     "lines": [{"account_id": card, "amount": "127000"}],
                                     "rates": {"UZS": "12700"}}))
    e2e.run(acc.record_expense(mgr, {"account_id": cash, "amount": "5", "note": "вода",
                                     "idempotency_key": "crawl-e"}))
    e2e.run(acc.record_transfer(boss, {"from_account_id": cash, "to_account_id": card, "amount": "10",
                                       "amount_in": "127000", "idempotency_key": "crawl-x"}))
    e2e.run(acc.close_day(boss, {"account_id": cash, "counted": "485", "idempotency_key": "crawl-c"}))


@pytest.mark.parametrize("role", ["boss", "mgr"])
def test_click_everything_with_accounting(open_app, e2e, role, tmp_path, printer, no_rate_limit, capsys):
    """Тот же обходчик, что `test_click_everything`, но бухгалтерия включена:
    новые экраны и формы не роняют приложение ни у руководителя, ни у менеджера."""
    seeded = _crawler._seed_rich(e2e, tmp_path)
    _seed_accounting(e2e, seeded["debt"])
    page = open_app(e2e.ids[role])
    crawl = _crawler.Crawl(page, f"{role}+acc")
    crawl.listen()
    try:
        crawl.run()
    finally:
        _crawler._report(crawl, capsys)
    assert crawl.screens > 0 and crawl.clicks > 0
    assert not crawl.failures, "Падения при обходе:\n" + "\n".join(crawl.failures)
