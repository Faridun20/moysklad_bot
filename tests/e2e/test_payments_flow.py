"""E2E: «как получены деньги» — поток владельца от заявки до нулевого долга.

1. «Оплата сразу» 12 130 USD: менеджер сам, без одобрения руководителя, жмёт в
   черновике «Внести оплату и отгрузить» → вносит 5 000 наличными + 7 130
   картой → отгрузка (руководителю — уведомление) → сдача 5 000 ложится на
   заказ («Заказы: #N») → руководитель подтверждает сдачу и карту → долг 0.
2. «В долг»: отгрузка без денег → оплата перечислением + наличными сумами по
   курсу → карта подтверждена, наличные ждут сдачи → сдача в UZS закрывает.
3. Руководителя нет (как на проде): карточка долга говорит однозначно, кто и
   что подтверждает, и менеджер подтверждает сам с пометкой.

Снимки экрана (390px) — в PAY_SHOTS_DIR, если он задан.
"""

from __future__ import annotations

import os

from tests.e2e.conftest import go, pay_form, pay_order, seed_order, settled, tab, open_confirmations

import pytest

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")


def _shot(page, name: str) -> None:
    folder = os.environ.get("PAY_SHOTS_DIR")
    if folder:
        page.wait_for_timeout(500)  # экран въезжает с анимацией — снимаем после
        page.screenshot(path=os.path.join(folder, f"{name}.png"))


def _norm(s: str | None) -> str:
    return " ".join(str(s or "").replace(" ", " ").replace(" ", " ").split())


def _api(page, path: str, body: dict | None = None) -> dict:
    return page.evaluate(
        "async ([p, b]) => { const r = await apiResult(p, b); return { status: r.status, body: r.body }; }",
        [path, body or {}],
    )


def _toast(page, text: str) -> None:
    page.wait_for_function(
        "(t) => [...document.querySelectorAll('.toast')].some(e => e.textContent.replace(/\\s+/g, ' ').includes(t))",
        arg=text,
    )


def test_paid_order_split_payment_handover_and_confirmation_close_the_debt(open_app, e2e):
    ids = e2e.ids
    db = e2e.db
    cp = e2e.rows("SELECT id FROM counterparties ORDER BY id LIMIT 1")[0]["id"]
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, str(cp), "ООО Ромашка")
    db.add_order_item(oid, "Кабель ВВГ 3x2.5", "", 1, "м", 12130.0, product_id=ids["product"])
    boss = open_app(ids["boss"])

    # ── Менеджер: черновик «оплата сразу» без оплаты не отгрузить — ни кнопкой,
    # ни ручкой; одобрения руководителя ждать не нужно.
    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    mgr.wait_for_selector(f'.btn-edit-order[data-id="{oid}"]')
    _shot(mgr, "01-draft-card-before-shipping")
    mgr.click(f'.btn-edit-order[data-id="{oid}"]')
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    assert "Внести оплату и отгрузить" in mgr.locator("#btn-submit").inner_text()
    refused = _api(mgr, "/api/orders/ship", {"order_id": oid, "payment_type": "paid"})
    assert refused["status"] == 409 and refused["body"]["code"] == "payment_required"
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "draft"
    assert e2e.rows("SELECT COUNT(*) AS n FROM payments")[0]["n"] == 0
    _shot(mgr, "02-editor-ship-button")

    # ── Разбивка: 5 000 наличными + 7 130 на карту → «Записать и отгрузить».
    pay_form(mgr, "#btn-submit", [("cash", "5000"), ("card", "7130")], submit=False)
    assert "Сумма сходится" in mgr.locator(".c-overlay .pay-total").inner_text()
    assert mgr.locator(".c-overlay #ms-submit").is_enabled()
    _shot(mgr, "03-payment-form-split")
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_function("(id) => window.__tgAlerts.some(a => a.includes('Заказ #' + id + ' отгружен'))", arg=str(oid))
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "shipped"
    assert e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (ids["product"],))[0]["quantity"] == 19
    # Руководителю — уведомление об отгрузке: позиции, итог, как оплачено; без кнопок.
    e2e.wait_for(lambda: any(
        m["chat_id"] == ids["boss"] and f"Заказ #{oid} отгружен" in m["text"] for m in e2e.bot.messages
    ))
    note = next(m for m in e2e.bot.messages if m["chat_id"] == ids["boss"] and f"Заказ #{oid} отгружен" in m["text"])
    assert "Кабель ВВГ 3x2.5 — 1 м × 12 130 USD = 12 130 USD" in note["text"]
    assert "Оплата сразу: наличные 5 000 USD; на карту •••• 1234 (Фаридун М.) · 7 130 USD" in note["text"]
    assert not note.get("reply_markup")
    assert e2e.rows("SELECT method, currency, amount_cents FROM payment_parts ORDER BY id") == [
        {"method": "cash", "currency": "USD", "amount_cents": 500_000},
        {"method": "card", "currency": "USD", "amount_cents": 713_000},
    ]
    # Разбивка оплаты — в подробной сводке карточки (по умолчанию свёрнута).
    mgr.click(f'.order-card[data-id="{oid}"] [data-details-toggle]')
    mgr.wait_for_selector(f'.order-card[data-id="{oid}"] .order-parts')
    _shot(mgr, "03b-order-card-after-shipping")
    parts = _norm(mgr.locator(f'.order-card[data-id="{oid}"] .order-parts').inner_text())
    assert "наличные 5 000 USD — у менеджера, ждут сдачи в кассу" in parts
    assert "на карту •••• 1234 (Фаридун М.) · 7 130 USD — ждёт проверки банка" in parts
    _shot(mgr, "04-order-card-breakdown")

    # ── Сдача наличных: на руках 5 000 по заказу #N.
    go(mgr, "money")
    tab(mgr, "ops")
    mgr.wait_for_selector(f'.dep-order[data-order="{oid}"]')
    assert mgr.input_value("#dep-amount") == "5000"
    _shot(mgr, "05-handover-form")
    mgr.click("#dep-create")
    _toast(mgr, "Сдача в кассу №")
    dep = e2e.rows("SELECT id FROM cash_deposits")[0]["id"]
    assert e2e.rows("SELECT order_id, amount_cents FROM cash_deposit_parts") == [
        {"order_id": oid, "amount_cents": 500_000},
    ]

    # ── Руководитель: «Заказы: #N — 5 000 USD», сдача и карта подтверждены.
    open_confirmations(boss)
    dep_card = f'.debt-card[data-dep="{dep}"]'
    boss.wait_for_selector(dep_card)
    assert f"Заказы: #{oid} — 5 000 USD" in _norm(boss.locator(dep_card).inner_text())
    pay_card = boss.locator(f'.debt-card[data-pay="{oid}"]')
    assert "на карту •••• 1234 (Фаридун М.) · 7 130 USD — ждёт проверки банка" in _norm(pay_card.inner_text())
    assert "Подтвердить 7 130 USD" in _norm(pay_card.locator(".pay-confirm").inner_text())
    _shot(boss, "06-boss-confirm-tab")
    boss.click(f"{dep_card} .dep-confirm")
    _toast(boss, "Сдача в кассу подтверждена")
    boss.wait_for_selector(f'.pay-confirm[data-id="{oid}"]')
    boss.click(f'.pay-confirm[data-id="{oid}"]')
    _toast(boss, f"Оплата по заказу #{oid} подтверждена")

    order = e2e.rows("SELECT status, paid_confirmed_at FROM orders WHERE id = ?", (oid,))[0]
    assert order["paid_confirmed_at"] and order["status"] == "shipped"
    debts = _api(boss, "/api/debts")["body"]["debts"]
    assert oid not in [d["id"] for d in debts], "долг 0 — в «Долгах» заказа нет"
    assert e2e.rows("SELECT status FROM payments ORDER BY id") == [{"status": "confirmed"}] * 2


def test_debt_order_ships_without_money_and_is_paid_by_breakdown(open_app, e2e):
    ids = e2e.ids
    assert e2e.db.set_currency_rate("UZS", 1 / 12700, ids["boss"])[0]
    oid = seed_order(e2e, qty=2, price=100.0)["order_id"]  # 200 USD в долг

    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    ship = f'.btn-ship-order[data-id="{oid}"]'
    mgr.wait_for_selector(ship)
    mgr.click(ship)
    mgr.wait_for_function("() => window.__tgAlerts.some(a => /Заказ #\\d+ отгружен/.test(a))")
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (oid,))[0]["status"] == "shipped"

    go(mgr, "money")
    tab(mgr, "debts")
    pay_form(mgr, f'.btn-pay-debt[data-id="{oid}"]',
             [("bank", "100"), ("cash", "1270000", "UZS")], submit=False)
    rate = mgr.locator(".c-overlay .pay-part").nth(1).locator(".pay-part-rate")
    assert rate.input_value() == "12700", "курс ЦБ подставлен"
    assert "Сумма сходится" in mgr.locator(".c-overlay .pay-total").inner_text()
    _shot(mgr, "07-debt-payment-uzs-rate")
    mgr.click(".c-overlay #ms-submit")
    _toast(mgr, "записана")
    parts = e2e.rows("SELECT method, currency, amount_cents, order_amount_cents, rate_source FROM payment_parts ORDER BY id")
    assert parts == [
        {"method": "bank", "currency": "USD", "amount_cents": 10_000, "order_amount_cents": 10_000, "rate_source": "same"},
        {"method": "cash", "currency": "UZS", "amount_cents": 127_000_000, "order_amount_cents": 10_000, "rate_source": "cbu"},
    ]
    settled(mgr)
    awaiting = _norm(mgr.locator(".debt-awaiting").inner_text())
    assert "Оплата 200 USD ждёт подтверждения · после подтверждения долг: 0 USD" in awaiting
    assert "Ждёт:" not in awaiting and "Останется" not in awaiting

    boss = open_app(ids["boss"])
    go(boss, "money")
    tab(boss, "debts")
    btn = f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]'
    boss.wait_for_selector(btn)
    assert "Подтвердить 100 USD" in _norm(boss.locator(btn).inner_text())
    boss.click(btn)
    boss.wait_for_function(
        "() => [...document.querySelectorAll('.debt-awaiting')].some(c => c.textContent.includes('Наличные подтверждаются сдачей'))"
    )
    card = _norm(boss.locator(".debt-awaiting").inner_text())
    assert "Уже подтверждено: 100 USD" in card
    assert "Оплата 100 USD ждёт подтверждения · после подтверждения долг: 0 USD" in card
    _shot(boss, "08-debt-card-cash-waits-handover")

    # Наличные сумы — отдельной сдачей в UZS.
    go(mgr, "money")
    tab(mgr, "ops")
    mgr.wait_for_selector('[data-dep-cur="UZS"]')
    mgr.click('[data-dep-cur="UZS"]')
    mgr.wait_for_selector(f'.dep-order[data-order="{oid}"]')
    assert mgr.input_value("#dep-amount") == "1270000"
    mgr.click("#dep-create")
    _toast(mgr, "Сдача в кассу №")
    dep = e2e.rows("SELECT d.id, c.currency FROM cash_deposits d JOIN cash_deposit_currency c ON c.deposit_id = d.id")
    assert [r["currency"] for r in dep] == ["UZS"]
    open_confirmations(boss)
    dep_card = f'.debt-card[data-dep="{dep[0]["id"]}"]'
    boss.wait_for_selector(dep_card)
    assert f"#{oid} — 1 270 000 UZS" in _norm(boss.locator(dep_card).inner_text())
    boss.click(f"{dep_card} .dep-confirm")
    _toast(boss, "Сдача в кассу подтверждена")
    assert e2e.rows("SELECT paid_confirmed_at FROM orders WHERE id = ?", (oid,))[0]["paid_confirmed_at"]


def test_debt_card_wording_when_nobody_but_the_manager_can_confirm(open_app, e2e):
    """Прод: «Ждут 12к», «Осталось 0» и «Босс должен подтвердить» при том, что
    босса нет. Теперь — однозначная фраза и честное «подтверждаете вы»."""
    import services.roles as roles

    ids = e2e.ids
    oid = seed_order(e2e, payment_type="paid", due_date=None, qty=1, price=12130.0, pay=None)["order_id"]
    pay_order(e2e, oid, [("cash", 5000), ("card", 7130)])
    e2e.exec("UPDATE user_roles SET role = 'guest' WHERE user_id IN (?, ?, ?)", (ids["boss"], ids["admin"], ids["book"]))
    roles.invalidate_all_roles()

    mgr = open_app(ids["mgr"])
    go(mgr, "money")
    tab(mgr, "debts")
    mgr.wait_for_selector(".debt-awaiting")
    card = _norm(mgr.locator(".debt-awaiting").inner_text())
    assert "Оплата 12 130 USD ждёт подтверждения · после подтверждения долг: 0 USD" in card
    assert "наличные 5 000 USD — у менеджера, ждут сдачи в кассу" in card
    assert "подтверждаете вы — руководителя и бухгалтера в системе нет" in card
    assert "Босс должен подтвердить" not in card
    _shot(mgr, "09-debt-card-no-boss")
    mgr.click(f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]')
    _toast(mgr, "руководителя/бухгалтера в системе нет")
    pays = e2e.rows("SELECT p.status, pp.method FROM payments p JOIN payment_parts pp ON pp.payment_id = p.id ORDER BY p.id")
    assert pays == [{"status": "pending", "method": "cash"}, {"status": "confirmed", "method": "card"}]
