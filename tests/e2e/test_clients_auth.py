"""E2E, вторая волна: клиенты, справочники, авторизация.

Лиды и звонки, привязка контрагента, кредитные лимиты, курсы, канал, глобальный
поиск; отдельно — что делает WebApp с плохой подписью и деактивированным
пользователем, и что видит бухгалтер.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import go, seed_order, settled, sheet_fill, tab

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")


def _seed_lead(e2e, *, tg_user_id: int = 555_001, name: str = "Азиз Р.") -> int:
    from services import leads

    r = e2e.run(leads.record_message(
        tg_user_id=tg_user_id, manager_id=e2e.ids["mgr"], inbound=True,
        username="aziz", display_name=name,
    ))
    return int(r["lead_id"])


# ─── Лиды ────────────────────────────────────────────────────────────────────


def test_manager_marks_lead_won(open_app, e2e):
    lead_id = _seed_lead(e2e)
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "leads")
    mgr.wait_for_selector(f'[data-lead="{lead_id}"]')
    row = mgr.locator(f'[data-lead="{lead_id}"]')
    assert "Азиз" in row.inner_text()
    row.click()
    mgr.wait_for_selector('[data-lead-status="won"]')
    assert "не отвечали" in mgr.locator("#content").inner_text()
    mgr.click('[data-lead-status="won"]')
    mgr.wait_for_selector(".toast:has-text('Отмечено')")
    assert e2e.rows("SELECT status FROM leads WHERE id = ?", (lead_id,))[0]["status"] == "won"


def test_lead_lost_reason_is_optional(open_app, e2e):
    lead_id = _seed_lead(e2e)
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "leads")
    mgr.click(f'[data-lead="{lead_id}"]')
    mgr.click('[data-lead-status="lost"]')
    mgr.wait_for_selector(".c-overlay [data-reason]")
    mgr.click('.c-overlay [data-reason="no_stock"]')
    mgr.click("#ms-submit")
    mgr.wait_for_function("(id) => !document.querySelector('.c-overlay')", arg=lead_id)
    assert e2e.rows("SELECT status FROM leads WHERE id = ?", (lead_id,))[0]["status"] == "lost"
    assert e2e.rows("SELECT reason FROM lead_lost WHERE lead_id = ?", (lead_id,))[0]["reason"] == "no_stock"


def test_lead_gets_counterparty_created_by_button(open_app, e2e):
    """Контрагента заводит ЧЕЛОВЕК кнопкой, тёзка — привязывается, а не дублируется."""
    lead_id = _seed_lead(e2e, name="ООО Ромашка")  # тёзка существующего контрагента
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "leads")
    mgr.click(f'[data-lead="{lead_id}"]')
    mgr.click("#lead-agent")
    mgr.wait_for_selector("#ms-f-search")
    mgr.click('.c-overlay button:has-text("Завести нового клиента")')  # confirmDialog → «да»
    mgr.wait_for_selector(".toast:has-text('уже есть')")
    assert e2e.rows("SELECT COUNT(*) AS n FROM counterparties")[0]["n"] == 1, "тёзку не завели"
    cp = e2e.rows("SELECT id FROM counterparties")[0]["id"]
    assert e2e.rows("SELECT agent_ms_id FROM leads WHERE id = ?", (lead_id,))[0]["agent_ms_id"] == str(cp)
    mgr.wait_for_selector("#lead-agent")
    assert "Ромашка" in mgr.locator("#content").inner_text()


def test_call_is_recorded_without_lead(open_app, e2e):
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "leads")
    mgr.wait_for_selector("#call-new")
    mgr.click("#call-new")
    sheet_fill(mgr, {"display_name": "Бахтиёр", "phone": "+998 (90) 111-22-33",
                     "interest": "Кабель 3x2.5"})
    mgr.click('.c-overlay [data-src="referral"]')
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Звонок записан')")
    call = e2e.rows("SELECT lead_id, phone, phone_key, direction, source, display_name FROM lead_calls")[0]
    assert call["lead_id"] is None, "звонок без Telegram — самостоятельная запись"
    assert call["phone_key"] == "901112233" and call["phone"] == "+998 (90) 111-22-33"
    assert call["direction"] == "in" and call["source"] == "referral"
    mgr.wait_for_selector("[data-call-link]")
    assert "Бахтиёр" in mgr.locator("#content").inner_text()


def test_boss_funnel_counts_seeded_lead(open_app, e2e):
    _seed_lead(e2e)
    boss = open_app(e2e.ids["boss"])
    go(boss, "leads")
    tab(boss, "funnel")
    settled(boss)
    text = boss.locator("#content").inner_text()
    assert "Ошибка" not in text and "Нет доступа" not in text
    # Свежий лид ещё не «ждёт ответа» (порог по времени), но в воронку вошёл.
    assert "Обратились" in text and "без ответа вовсе: 1" in text


# ─── Лимиты и курсы ──────────────────────────────────────────────────────────


def test_boss_sets_credit_limit_from_client_card(open_app, e2e):
    seeded = seed_order(e2e)
    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "limits")
    boss.wait_for_selector(f'[data-agent="{seeded["counterparty_id"]}"]')
    boss.click(f'[data-agent="{seeded["counterparty_id"]}"]')
    boss.wait_for_selector("#cl-edit")
    boss.click("#cl-edit")
    boss.fill("#cl-input", "500")
    boss.click("#cl-save")
    boss.wait_for_function(
        "(id) => /500/.test(document.querySelector('#content').textContent)", arg=0
    )
    lim = e2e.rows("SELECT agent_id, limit_amount_cents FROM credit_limits")
    assert lim == [{"agent_id": str(seeded["counterparty_id"]), "limit_amount_cents": 50000}]


def test_boss_updates_currency_rate(open_app, e2e):
    seed_order(e2e)  # без клиентов экран лимитов пуст, а курсы открываются с него
    from services.database import set_currency_rate

    # Курс хранится как «1 UZS = X USD»; форма показывает обратное — сум за доллар.
    ok, err = set_currency_rate("UZS", 1 / 12500, e2e.ids["admin"])
    assert ok, err
    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "limits")
    boss.wait_for_selector("#open-rates")
    boss.click("#open-rates")
    boss.wait_for_selector(".rate-save")
    boss.fill(".rate-input", "12700")
    boss.click(".rate-save")
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('Курс'))")
    code = boss.locator(".rate-save").first.get_attribute("data-code")
    rate = e2e.rows("SELECT rate_to_base FROM currency_rates WHERE currency_code = ?", (code,))[0]
    assert float(rate["rate_to_base"]) == pytest.approx(1 / 12700)


# ─── Канал ───────────────────────────────────────────────────────────────────


def test_channel_history_renders_and_manager_has_no_tab(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "leads")
    tab(boss, "channel")
    settled(boss)
    text = boss.locator("#content").inner_text()
    assert "Ошибка" not in text and "Нет доступа" not in text

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "leads")
    assert mgr.locator('.seg-item[data-sect="channel"]').count() == 0


def test_channel_draft_from_price_card_hides_stock(open_app, e2e):
    """Черновик собирает сервер, и количество в него не попадает."""
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.wait_for_selector("[data-price-idx]")
    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-post")
    boss.click("#pe-post")
    boss.wait_for_selector("#ms-f-manager_username")
    boss.fill("#ms-f-manager_username", "manager_tg")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-f-text")
    draft = boss.input_value("#ms-f-text")
    assert "Кабель" in draft
    assert "20" not in draft, "остаток наружу не уходит"
    assert "manager_tg" in draft


# ─── Глобальный поиск ────────────────────────────────────────────────────────


def test_global_search_finds_counterparty_and_order(open_app, e2e):
    seeded = seed_order(e2e)
    boss = open_app(e2e.ids["boss"])
    boss.click("#search-btn")
    boss.fill("#search-input", "Ромашка")
    boss.wait_for_selector(".search-item[data-agent]")
    assert "Ромашка" in boss.locator(".search-item[data-agent]").first.inner_text()

    boss.fill("#search-input", f"#{seeded['order_id']}")
    boss.wait_for_selector(f".search-item:has-text('#{seeded['order_id']}')")


# ─── Авторизация ─────────────────────────────────────────────────────────────


def test_bad_init_data_shows_error_not_blank_screen(open_app, e2e):
    page = open_app(e2e.ids["boss"], init_data="tampered")
    page.wait_for_selector(".error-card")
    assert page.locator("#bottom-nav .nav-item").count() == 0
    text = page.locator("#content").inner_text().lower()
    assert "telegram" in text or "авториз" in text or "доступ" in text


def test_deactivated_manager_loses_access(open_app, e2e):
    from services.database import deactivate_user

    e2e.run(deactivate_user(e2e.ids["mgr"], e2e.ids["admin"]))
    page = open_app(e2e.ids["mgr"])
    assert page.locator("#bottom-nav .nav-item").count() == 0
    text = page.locator("#content").inner_text().lower()
    # Не «нет связи» с кнопкой «Повторить», а прямой ответ.
    assert "доступ" in text and "отключ" in text
    assert "нет связи" not in text


def test_api_without_init_data_is_rejected(e2e):
    """Прямой POST без подписи — 401, а не 500 и не пустой ответ."""
    import json
    import urllib.request

    req = urllib.request.Request(
        e2e.base_url + "/api/orders", data=json.dumps({}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as err:
        assert err.code == 401, err.code
    else:
        raise AssertionError("запрос без initData прошёл")


# ─── Бухгалтер ───────────────────────────────────────────────────────────────


def test_bookkeeper_confirms_deposit_but_has_no_cashbox(open_app, e2e):
    seed_order(e2e, qty=1, price=30.0)
    from services.database import create_cash_deposit

    r = e2e.run(create_cash_deposit(e2e.ids["mgr"], 30.0))
    assert r.get("ok"), r

    book = open_app(e2e.ids["book"])
    go(book, "money")
    # У бухгалтера в «Деньгах» одна вкладка — «Подтвердить», и переключатель
    # из одного пункта не рисуется вовсе.
    book.wait_for_selector(".dep-confirm")
    assert book.locator('.seg-item[data-sect]').count() == 0
    # Бухгалтер не подтверждает оплаты по заказам — только сдачи и возвраты.
    assert book.locator(".pay-confirm").count() == 0
    book.click(".dep-confirm")
    book.wait_for_selector(".toast:has-text('Сдача в кассу подтверждена')")
    assert e2e.rows("SELECT status, confirmed_by FROM cash_deposits")[0] == {
        "status": "confirmed", "confirmed_by": e2e.ids["book"],
    }


def test_warehouse_keeper_confirms_goods_received_only(open_app, e2e):
    seeded = seed_order(e2e, payment_type="paid", due_date=None)
    from services.database import confirm_all_pending_payments_for_order, create_return

    e2e.run(confirm_all_pending_payments_for_order(seeded["order_id"], e2e.ids["boss"], "Boss"))
    item = e2e.rows("SELECT id FROM order_items WHERE order_id = ?", (seeded["order_id"],))[0]["id"]
    r = e2e.run(create_return(seeded["order_id"], "full", "Брак", [(item, 2, 200.0)],
                              "no_refund", e2e.ids["mgr"]))  # оплаченный заказ: «в счёт долга» вычитать не из чего (_debt_reduction_refusal)
    assert r.get("ok"), r

    keeper = open_app(e2e.ids["keeper"])
    go(keeper, "money")  # единственная вкладка — «Подтвердить», переключателя нет
    keeper.wait_for_selector(".ret-goods")
    keeper.click(".ret-goods")
    keeper.wait_for_selector(".toast:has-text('принят')")
    assert e2e.rows("SELECT goods_received, status FROM returns")[0] == {"goods_received": 1, "status": "pending"}
    # Подтвердить возврат кладовщик не может: ручка отвечает только руководству.
    # Кнопку, которая гарантированно ответит 403, не рисуем — вместо неё
    # подпись, кто подтверждает.
    keeper.wait_for_selector(".debt-card[data-ret] .debt-meta:has-text('подтверждает руководитель')")
    assert keeper.locator(".ret-confirm").count() == 0
    assert e2e.rows("SELECT status FROM returns")[0]["status"] == "pending"
