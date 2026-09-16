"""E2E: «куда поступили деньги» — карта или счёт в оплате (services/pay_accounts.py).

Требование владельца: выбрав «Карта» или «На счёт», менеджер указывает, ЧЬЯ это
карта или ЧЕЙ счёт — последние цифры и владельца, фирму и номер счёта, —
а руководитель сверяет банк по этой строке.

1. «Оплата сразу» 12 130 USD: 5 000 наличными + 7 130 на НОВУЮ карту
   «Фаридун М. •••• 1234», заведённую прямо из формы (полный номер отвергнут) →
   отгрузка → руководитель в «Решениях» видит «на карту •••• 1234 (Фаридун М.)»
   и подтверждает.
2. Долг перечислением на новый счёт «ООО Farid Impeks» (20 цифр): карточка
   «Долгов» называет счёт, следующая оплата предлагает его сама.
3. Руководитель в «Настройках → Карты и счета»: новая карта, правка, архив —
   архивная не предлагается менеджеру, но на старом платеже остаётся.

Снимки экрана (390px) — в PAY_ACCOUNTS_SHOTS_DIR, если он задан.
"""

from __future__ import annotations

import os

from tests.e2e.conftest import go, open_confirmations, seed_order, settled, tab

CARD_LABEL = "на карту •••• 1234 (Фаридун М.)"
BANK_LABEL = "на счёт ООО Farid Impeks (…6789)"


def _shot(page, name: str) -> None:
    folder = os.environ.get("PAY_ACCOUNTS_SHOTS_DIR")
    if folder:
        os.makedirs(folder, exist_ok=True)
        page.wait_for_timeout(400)  # экран въезжает с анимацией — снимаем после
        page.screenshot(path=os.path.join(folder, f"{name}.png"))


def _norm(s: str | None) -> str:
    return " ".join(str(s or "").replace(" ", " ").replace(" ", " ").split())


def _toast(page, text: str) -> None:
    page.wait_for_function(
        "(t) => [...document.querySelectorAll('.toast')].some(e => e.textContent.replace(/\\s+/g, ' ').includes(t))",
        arg=text,
    )


def _top(page, selector: str):
    return page.locator(selector).last


def test_manager_pays_cash_and_new_card_boss_checks_it_in_decisions(open_app, e2e):
    ids = e2e.ids
    oid = seed_order(e2e, payment_type="paid", due_date=None, qty=1, price=12130.0, pay=None)["order_id"]

    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    btn = f'.btn-pay-order[data-id="{oid}"]'
    mgr.wait_for_selector(btn)
    mgr.click(btn)
    mgr.wait_for_selector(".c-overlay .pay-part")
    mgr.locator(".c-overlay .pay-part-amount").first.fill("5000")
    assert mgr.locator(".c-overlay .pay-part-account").count() == 0, "у наличных «куда» не спрашивают"
    mgr.click(".c-overlay .pay-add-part")
    card_row = mgr.locator(".c-overlay .pay-part").nth(1)
    assert card_row.locator('[data-pay-method="card"]').get_attribute("aria-pressed") == "true"
    assert card_row.locator(".pay-part-amount").input_value() == "7130"
    assert "Строка 2: выберите, на какую карту пришли деньги" in _norm(mgr.locator(".c-overlay .pay-total").inner_text())
    assert mgr.locator(".c-overlay #ms-submit").is_disabled()
    _shot(mgr, "01-manager-form-card-needs-account")

    # Лист выбора пуст — новая карта заводится тут же, не уходя из оплаты.
    card_row.locator(".pay-part-account").click()
    picker = _top(mgr, ".c-overlay.pay-account-picker")
    picker.wait_for()
    assert "Карт пока нет" in _norm(picker.inner_text())
    _shot(mgr, "02-manager-card-picker-empty")
    picker.locator(".picker-add").click()
    form = _top(mgr, ".c-overlay.pay-account-form")
    form.wait_for()
    form.locator("#ms-f-holder").fill("Фаридун М.")
    form.locator("#ms-f-card_last4").fill("8600 1234 5678 1234")  # вставили номер целиком
    form.locator("#ms-submit").click()
    form.locator("#ms-error:has-text('полный номер карты не храним')").wait_for()
    form.locator("#ms-f-card_last4").fill("1234")
    form.locator("#ms-f-bank").fill("Kapitalbank")
    _shot(mgr, "03-manager-new-card-form")
    form.locator("#ms-submit").click()
    _toast(mgr, "Карта добавлена")
    mgr.wait_for_selector(".c-overlay .pay-part-account:has-text('•••• 1234 · Фаридун М.')")
    assert "Сумма сходится" in mgr.locator(".c-overlay .pay-total").inner_text()
    _shot(mgr, "04-manager-form-cash-and-card")
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_function("(id) => window.__tgAlerts.some(a => a.includes('Заказ #' + id + ' отгружен'))", arg=str(oid))

    accounts = e2e.rows("SELECT kind, card_last4, holder, bank FROM acc_accounts")
    assert accounts == [{"kind": "card", "card_last4": "1234", "holder": "Фаридун М.", "bank": "Kapitalbank"}]
    assert "8600" not in str(e2e.rows("SELECT * FROM acc_accounts")), "полный номер карты не сохранился нигде"
    assert e2e.rows(
        "SELECT pp.method, a.card_last4 FROM payment_parts pp LEFT JOIN payment_part_accounts ppa ON ppa.part_id = pp.id "
        "LEFT JOIN acc_accounts a ON a.id = ppa.account_id ORDER BY pp.id") == [
        {"method": "cash", "card_last4": None}, {"method": "card", "card_last4": "1234"}]
    mgr.click(f'.order-card[data-id="{oid}"] [data-details-toggle]')
    mgr.wait_for_selector(f'.order-card[data-id="{oid}"] .order-parts')
    parts = _norm(mgr.locator(f'.order-card[data-id="{oid}"] .order-parts').inner_text())
    assert f"{CARD_LABEL} · 7 130 USD — ждёт проверки банка" in parts
    _shot(mgr, "05-manager-order-card-breakdown")

    # Руководитель сверяет банк по карте — в «Решениях», и подтверждает.
    boss = open_app(ids["boss"])
    open_confirmations(boss)
    pay_card = boss.locator(f'.debt-card[data-pay="{oid}"]')
    pay_card.wait_for()
    text = _norm(pay_card.inner_text())
    assert f"{CARD_LABEL} · 7 130 USD — ждёт проверки банка" in text, text
    assert "Подтвердить 7 130 USD" in _norm(pay_card.locator(".pay-confirm").inner_text())
    pay_card.scroll_into_view_if_needed()
    _shot(boss, "06-boss-decisions-card-shows-whose-card")
    boss.click(f'.pay-confirm[data-id="{oid}"]')
    _toast(boss, f"Оплата по заказу #{oid} подтверждена")
    assert e2e.rows("SELECT p.status FROM payments p JOIN payment_parts pp ON pp.payment_id = p.id "
                    "WHERE pp.method = 'card'") == [{"status": "confirmed"}]
    pushed = [p["text"] for p in e2e.pushes if "Требуется подтверждение оплаты" in p["text"]]
    assert pushed and f"{CARD_LABEL} · 7 130 USD" in pushed[0], "пуш руководителю называет карту"


def test_bank_transfer_to_company_account_and_it_is_offered_next_time(open_app, e2e):
    from services.database import mark_order_shipped

    ids = e2e.ids
    oid = seed_order(e2e, qty=2, price=100.0)["order_id"]  # 200 USD в долг
    assert e2e.run(mark_order_shipped(oid, ids["keeper"], "Keeper")).get("ok")

    mgr = open_app(ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    btn = f'.btn-pay-debt[data-id="{oid}"]'
    mgr.wait_for_selector(btn)
    mgr.click(btn)
    mgr.wait_for_selector(".c-overlay .pay-part")
    row = mgr.locator(".c-overlay .pay-part").first
    row.locator('[data-pay-method="bank"]').click()
    mgr.locator(".c-overlay .pay-part-amount").first.fill("120")
    assert "Выберите, на какой счёт пришли деньги" in _norm(mgr.locator(".c-overlay .pay-total").inner_text())
    mgr.locator(".c-overlay .pay-part-account").click()
    picker = _top(mgr, ".c-overlay.pay-account-picker")
    picker.wait_for()
    picker.locator(".picker-add").click()
    form = _top(mgr, ".c-overlay.pay-account-form")
    form.wait_for()
    form.locator("#ms-f-holder").fill("ООО Farid Impeks")
    form.locator("#ms-f-account_number").fill("2020884090011223678")
    form.locator("#ms-submit").click()
    form.locator("#ms-error:has-text('20 цифр')").wait_for()
    form.locator("#ms-f-account_number").fill("20208840900112236789")
    form.locator("#ms-f-bank").fill("Kapitalbank")
    form.locator("#ms-f-mfo").fill("01158")
    form.locator("#ms-f-company_tin").fill("301234567")
    _shot(mgr, "07-manager-new-bank-account-form")
    form.locator("#ms-submit").click()
    _toast(mgr, "Счёт добавлен")
    mgr.wait_for_selector(".c-overlay .pay-part-account:has-text('ООО Farid Impeks · …6789')")
    mgr.click(".c-overlay #ms-submit")
    _toast(mgr, "записана")
    assert e2e.rows("SELECT a.kind, a.currency, d.account_number, d.mfo, d.company_tin FROM acc_accounts a "
                    "JOIN acc_account_details d ON d.account_id = a.id") == [
        {"kind": "bank", "currency": "USD", "account_number": "20208840900112236789", "mfo": "01158",
         "company_tin": "301234567"}]

    settled(mgr)
    awaiting = _norm(mgr.locator(".debt-awaiting").inner_text())
    assert f"{BANK_LABEL} · 120 USD — ждёт проверки банка" in awaiting, awaiting
    _shot(mgr, "08-manager-debt-card-bank")

    # Следующая оплата перечислением сама предлагает этот счёт.
    mgr.click(btn)
    mgr.wait_for_selector(".c-overlay .pay-part")
    mgr.locator(".c-overlay .pay-part").first.locator('[data-pay-method="bank"]').click()
    mgr.wait_for_selector(".c-overlay .pay-part-account:has-text('ООО Farid Impeks · …6789')")
    _shot(mgr, "09-manager-last-account-offered")
    mgr.keyboard.press("Escape")

    boss = open_app(ids["boss"])
    go(boss, "money")
    tab(boss, "debts")
    boss.wait_for_selector(".debt-awaiting")
    assert f"{BANK_LABEL} · 120 USD" in _norm(boss.locator(".debt-awaiting").inner_text())
    from services.database import get_cash_history

    history = e2e.run(get_cash_history(10))
    assert [h["account_label"] for h in history if h["kind"] == "payment"] == [BANK_LABEL]


def test_boss_manages_cards_in_settings_archived_card_is_not_offered(open_app, e2e):
    ids = e2e.ids
    old = seed_order(e2e, payment_type="paid", due_date=None, qty=1, price=300.0)  # оплачен тестовой картой
    debt = seed_order(e2e, qty=1, price=50.0)
    from services.database import mark_order_shipped

    assert e2e.run(mark_order_shipped(debt["order_id"], ids["keeper"], "Keeper")).get("ok")

    boss = open_app(ids["boss"])
    go(boss, "settings")
    boss.wait_for_selector("#set-pay-accounts")
    boss.click("#set-pay-accounts")
    boss.wait_for_selector("#content [data-pay-account]")
    assert "•••• 1234 · Фаридун М." in _norm(boss.locator("#content").inner_text())
    boss.click('#content [data-pay-account-add="card"]')
    form = _top(boss, ".c-overlay.pay-account-form")
    form.wait_for()
    form.locator("#ms-f-holder").fill("Али Валиев")
    form.locator("#ms-f-card_last4").fill("5678")
    form.locator("#ms-f-bank").fill("Humo")
    form.locator("#ms-submit").click()
    _toast(boss, "Карта добавлена")
    boss.wait_for_selector("#content [data-pay-account]:has-text('•••• 5678 · Али Валиев')")
    _shot(boss, "10-boss-settings-cards-and-accounts")

    # Правка владельца — и в архив.
    boss.click("#content [data-pay-account]:has-text('•••• 1234')")
    form = _top(boss, ".c-overlay.pay-account-form")
    form.wait_for()
    form.locator("#ms-f-holder").fill("Фаридун Масуджанов")
    form.locator("#ms-submit").click()
    _toast(boss, "Сохранено")
    boss.wait_for_selector("#content [data-pay-account]:has-text('Фаридун Масуджанов')")
    boss.click("#content [data-pay-account]:has-text('•••• 1234')")
    form = _top(boss, ".c-overlay.pay-account-form")
    form.wait_for()
    _shot(boss, "11-boss-edit-card")
    form.locator(".pay-account-archive").click()
    _toast(boss, "Убрано в архив")
    boss.wait_for_selector("#content [data-pay-account]:has-text('•••• 1234')", state="detached")
    boss.click("#content [data-pay-accounts-archived]")
    boss.wait_for_selector("#content [data-pay-account]:has-text('в архиве')")
    _shot(boss, "12-boss-settings-archive-shown")

    # Менеджер: архивной карты в выборе нет, на старом платеже она осталась.
    mgr = open_app(ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    btn = f'.btn-pay-debt[data-id="{debt["order_id"]}"]'
    mgr.wait_for_selector(btn)
    mgr.click(btn)
    mgr.wait_for_selector(".c-overlay .pay-part")
    mgr.locator(".c-overlay .pay-part").first.locator('[data-pay-method="card"]').click()
    account = mgr.locator(".c-overlay .pay-part-account")
    assert account.get_attribute("data-account-id") == "", "последняя карта в архиве — не предлагается"
    account.click()
    picker = _top(mgr, ".c-overlay.pay-account-picker")
    picker.wait_for()
    names = _norm(picker.locator(".picker-list").inner_text())
    assert "•••• 5678 · Али Валиев" in names and "1234" not in names
    assert picker.locator(".pay-accounts-manage").count() == 0, "править справочник менеджер при руководителе не может"
    _shot(mgr, "13-manager-picker-without-archived")
    picker.locator("[data-pick]").first.click()
    picker.locator("#ms-submit").click()
    mgr.wait_for_selector(".c-overlay .pay-part-account:has-text('Али Валиев')")
    mgr.keyboard.press("Escape")

    go(mgr, "sales")
    card = f'.order-card[data-id="{old["order_id"]}"] .order-parts'
    mgr.click(f'.order-card[data-id="{old["order_id"]}"] [data-details-toggle]')
    mgr.wait_for_selector(card)
    assert "на карту •••• 1234 (Фаридун Масуджанов) · 300 USD" in _norm(mgr.locator(card).inner_text())
