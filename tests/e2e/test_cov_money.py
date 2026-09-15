"""E2E, покрытие раздела «Деньги»: каждая кнопка — с проверкой последствия.

Вкладки: «Подтвердить» (оплаты, сдачи, возвраты), «Долги» (фильтры, отметка
оплаты, рассрочки техники, карточка покупателя), «Касса» (сдача наличных,
ручной платёж, возврат) и «Отчёт» (поступления, пересчёт в базовую валюту,
«где деньги», лента движения). Мало увидеть кнопку — после нажатия проверяем,
что легло в БД (копейки, статусы), что ушло в Telegram и что нарисовал экран.

`test_sales_money.py` уже держит базовые пути (отметка 150 → подтверждение,
оплата paid-заказа, полная сдача, полный возврат, UZS-строка платежа) — здесь
их не повторяем, а идём дальше: арифметика частичных оплат, FIFO-сдача по
двум заказам, отказы, частичный возврат с выдачей из кассы, валюты и курс,
сквозная сверка «долг ↔ отчёт» и роли.

Найденные расхождения в деньгах закреплены `xfail(strict=True)` с пометкой
BUG: тест описывает правильное поведение и начнёт «неожиданно проходить»,
когда баг починят.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pytest

from tests.e2e.conftest import go, pay_form, pay_order, tab

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")

# ─── Хелперы ─────────────────────────────────────────────────────────────────

CP_NAME = "ООО Ромашка"
MGR2 = 201


def _norm(s: str | None) -> str:
    """Текст без пробелов: formatMoney ставит неразрывные, opsAmount — обычные."""
    return re.sub(r"\s+", "", s or "")


def _idle(page) -> None:
    """Раздел отрисован: ни спиннера, ни скелетона в #content."""
    page.wait_for_function(
        "() => { const c = document.querySelector('#content');"
        " return !!c && c.children.length > 0"
        " && !c.querySelector('.spinner-wrap, .sk-card, .sk-hero, .sk-label, .sk-action'); }"
    )


def _alert(page, text: str) -> None:
    """Ответ приложения: диалог Telegram (ошибка формы) или тост (успех и отказ
    сервера ушли в неблокирующие тосты — модалка прерывала работу)."""
    page.wait_for_function(
        "(t) => window.__tgAlerts.some(a => a.includes(t))"
        " || [...document.querySelectorAll('.toast-msg')].some(e => e.textContent.includes(t))",
        arg=text,
    )


def _api(page, path: str, body: dict | None = None) -> dict:
    """Запрос из браузера под той же подписью, что шлёт app.js."""
    return page.evaluate(
        """async ([path, body]) => {
            const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({initData: window.Telegram.WebApp.initData, ...body})});
            let data = {};
            try { data = await r.json(); } catch (e) { /* не JSON */ }
            return {status: r.status, body: data};
        }""",
        [path, body or {}],
    )


def _text(page, selector: str) -> str:
    return page.locator(selector).first.text_content() or ""


def _texts(page, selector: str) -> list[str]:
    return page.eval_on_selector_all(selector, "els => els.map(e => e.textContent)")


def _order(e2e, *, qty: float = 1, price: float = 100.0, payment_type: str = "credit",
           due: str | None = "2030-01-15", currency: str | None = None, owner: int | None = None,
           approve: bool = True, ship: bool = False, pay: str | None = "card") -> int:
    """Заказ через сервисы: сабмит → одобрение боссом → (отгрузка)."""
    from services.database import mark_order_shipped
    from services.order_workflow import approve_shipment_request, submit_order

    db, ids = e2e.db, e2e.ids
    uid = owner or ids["mgr"]
    name = "Manager2" if uid == MGR2 else "Manager"
    cp = e2e.rows("SELECT id FROM counterparties ORDER BY id LIMIT 1")[0]["id"]
    oid = db.create_order(uid, name, "")
    db.update_order_agent(oid, str(cp), CP_NAME)
    db.add_order_item(oid, "Кабель ВВГ 3x2.5", "", qty, "м", price, product_id=ids["product"])
    if currency:
        db.update_order_currency(oid, currency)
    res = e2e.run(submit_order(oid, uid, name, payment_type=payment_type,
                               due_date=due if payment_type == "credit" else None))
    assert res.get("ok"), res
    if approve:
        ap = e2e.run(approve_shipment_request(res["req_id"], ids["boss"], "Boss", e2e.bot, override=True))
        assert ap.get("ok"), ap
        if payment_type == "paid" and pay:
            # «Оплата сразу»: менеджер перед отгрузкой вносит, как получил деньги
            # (по умолчанию картой — платёж ждёт подтверждения руководителя).
            pay_order(e2e, oid, [(pay, qty * price)], uid=uid)
    if ship:
        assert e2e.run(mark_order_shipped(oid, ids["keeper"], "Keeper")).get("ok")
    return oid


def _confirm_order_payments(e2e, oid: int) -> None:
    from services.database import confirm_all_pending_payments_for_order

    assert e2e.run(confirm_all_pending_payments_for_order(oid, e2e.ids["boss"], "Boss")) >= 1


def _standalone_payment(e2e, amount: float, currency: str, status: str = "confirmed") -> int:
    from services.database import add_payment, confirm_payment, reject_payment

    pid = add_payment(e2e.ids["mgr"], "@mgr_user", "Manager", amount, currency, "Касса")
    if status == "confirmed":
        assert e2e.run(confirm_payment(pid, e2e.ids["boss"], "Boss"))
    elif status == "rejected":
        assert e2e.run(reject_payment(pid, e2e.ids["boss"], "Boss"))
    return pid


def _deposit(e2e, amount: float, *, confirm: bool = False) -> int:
    from services.database import confirm_cash_deposit, create_cash_deposit

    res = e2e.run(create_cash_deposit(e2e.ids["mgr"], amount))
    assert res.get("ok"), res
    if confirm:
        assert e2e.run(confirm_cash_deposit(res["deposit_id"], e2e.ids["boss"], "Boss")).get("ok")
    return res["deposit_id"]


def _set_rate(e2e, code: str, rate: float | None) -> None:
    """Курс к базовой (USD). None — удалить курс, как будто его не задавали."""
    if rate is None:
        e2e.exec("DELETE FROM currency_rates WHERE currency_code = ?", (code,))
        e2e.db._invalidate_currency_rates_cache()
        return
    ok, err = e2e.db.set_currency_rate(code, rate, e2e.ids["boss"])
    assert ok, err


def _credit_machine(e2e, *, buyer: str = "Азиз Рахимов", price_cents: int = 2_000_000,
                    down_cents: int = 500_000, months: int = 3, vin: str = "JCB3CX-001") -> int:
    from services import machines

    m = e2e.run(machines.create_machine(vin=vin, name="JCB 3CX", created_by=e2e.ids["boss"],
                                        price_cents=price_cents, status="in_stock"))
    assert m["ok"], m
    deal = e2e.run(machines.create_deal(m["machine_id"], kind="credit", price_cents=price_cents,
                                        buyer_name=buyer, created_by=e2e.ids["boss"],
                                        down_payment_cents=down_cents, months=months))
    assert deal["ok"], deal
    return deal["deal_id"]


def _money(page, key: str) -> None:
    """Открыть «Деньги» и вкладку `key`, дождаться отрисовки. «Подтвердить»
    руководства — экран «Решения»."""
    if key == "confirm" and page.locator('#bottom-nav .nav-item[data-screen="decisions"]').count():
        go(page, "decisions")
        _idle(page)
        return
    go(page, "money")
    _idle(page)
    if page.locator(f'.seg-item[data-sect="{key}"]').count():
        tab(page, key)
    _idle(page)


def _redraw_debts(page) -> None:
    """Перечитать «Долги» с сервера — нажать уже выбранный фильтр."""
    active = page.get_attribute(".seg-item.active[data-f]", "data-f") or "all"
    page.click(f'.seg-item[data-f="{active}"]')
    _idle(page)


def _add_manager2(e2e) -> None:
    e2e.db.set_role(MGR2, "mgr2_user", "Manager2", "manager")


# ─── Роли: вкладки и отказ сервера ───────────────────────────────────────────


@pytest.mark.parametrize(("who", "tabs", "active"), [
    # Подтверждения руководства — в «Решениях» (test_boss_ui.py); «Касса» —
    # с «Рабочими действиями» (boss_work_actions этого модуля). «Сверка» —
    # ежедневный пересчёт наличных: она у всех, кому отвечает
    # /api/cash/reconcile (admin/boss/manager), и от «Рабочих действий» не
    # зависит — руководителю это контроль, а не работа склада.
    ("boss", ["debts", "ops", "reconcile", "report"], "debts"),
    ("admin", ["debts", "ops", "reconcile", "report"], "debts"),
    # «Подтвердить» у менеджера — пока он замещает кладовщика и бухгалтера
    # (services.roles.ROLE_ALSO_ACTS_AS); при откате — ["debts", "ops",
    # "reconcile"].
    ("mgr", ["confirm", "debts", "ops", "reconcile"], "confirm"),
    ("keeper", [], None),
    ("book", [], None),
])
def test_money_tabs_match_role(open_app, e2e, who, tabs, active):
    """Набор вкладок «Денег» — по матрице ручек: ни одна не отвечает 403."""
    page = open_app(e2e.ids[who])
    go(page, "money")
    _idle(page)
    got = page.eval_on_selector_all(".seg-item[data-sect]", "els => els.map(e => e.dataset.sect)")
    assert got == tabs
    if active:
        assert page.get_attribute(".seg-item.active[data-sect]", "data-sect") == active
    # Каждая доступная вкладка открывается без ошибки и с честным пустым
    # состоянием (данных ещё нет). У кладовщика и бухгалтера вкладка одна —
    # «Подтвердить», и переключатель из одного пункта не рисуется.
    # На «Сверке» пустого списка нет: содержимое вкладки — сама форма пересчёта,
    # и она появляется только после ответа /api/cash/reconcile/context, то есть
    # проверяет ровно то же — что ручка роли отвечает.
    empty = {"confirm": "Нет записей на подтверждении", "debts": "Долгов и платежей пока нет",
             "report": "За период поступлений нет", "ops": "Оформить возврат",
             "reconcile": "Сверка кассы за"}
    for key in tabs or ["confirm"]:
        if tabs:
            tab(page, key)
        _idle(page)
        body = page.locator("#content").inner_text()
        assert "Нет доступа" not in body and "Не удалось загрузить" not in body, (key, body)
        assert empty[key].lower() in body.lower(), (key, body)


FORBIDDEN = {
    "mgr": [
        ("/api/money/summary", {"period": "month"}), ("/api/cash/history", {}),
        ("/api/money/forecast", {}), ("/api/money/discipline", {}),
        # Сдачи, приёмка возврата и подтверждение карты/перечисления менеджеру
        # пока открыты — он замещает бухгалтера и кладовщика (ROLE_ALSO_ACTS_AS,
        # tests/e2e/test_manager_acts_as.py, services/order_payments.py).
        # Подтверждение возврата остаётся руководству.
        ("/api/returns/confirm", {"return_id": 1}),
    ],
    "keeper": [
        ("/api/debts", {}), ("/api/orders/mark_paid", {"order_id": "CREDIT", "amount": 10}),
        ("/api/deposits/create", {"amount": 50}), ("/api/deposits/my", {}),
        ("/api/deposits/pending", {}), ("/api/deposits/confirm", {"deposit_id": "DEP"}),
        ("/api/payments/send", {"amount": 10, "currency": "USD", "comment": "x"}),
        ("/api/money/summary", {}), ("/api/money/receivables", {}),
        ("/api/orders/confirm_payment", {"order_id": "PAID"}),
    ],
    "book": [
        ("/api/debts", {}), ("/api/orders/mark_paid", {"order_id": "CREDIT", "amount": 10}),
        ("/api/returns/pending", {}),
        ("/api/returns/goods_received", {"return_id": 1}), ("/api/returns/confirm", {"return_id": 1}),
        ("/api/deposits/create", {"amount": 50}),
        ("/api/payments/send", {"amount": 10, "currency": "USD", "comment": "x"}),
        ("/api/money/summary", {}), ("/api/cash/history", {}),
    ],
    "boss": [
        # Босс подтверждает платежи, а не отправляет их сам себе.
        ("/api/payments/send", {"amount": 10, "currency": "USD", "comment": "x"}),
    ],
}


@pytest.mark.parametrize("who", list(FORBIDDEN))
def test_forbidden_money_actions_are_refused_and_change_nothing(open_app, e2e, who):
    """Спрятать кнопку мало: ручка обязана отказать, и деньги не должны сдвинуться."""
    paid = _order(e2e, payment_type="paid", price=200.0)       # ждущий платёж 200
    credit = _order(e2e, price=100.0)                           # долг 100
    dep = _deposit(e2e, 50.0)                                   # ждущая сдача 50
    subst = {"PAID": paid, "CREDIT": credit, "DEP": dep}

    page = open_app(e2e.ids[who])
    for path, body in FORBIDDEN[who]:
        body = {k: subst.get(v, v) if isinstance(v, str) else v for k, v in body.items()}
        res = _api(page, path, body)
        assert res["status"] == 403, (who, path, res)

    assert e2e.rows("SELECT order_id, amount_cents, status FROM payments ORDER BY id") == [
        {"order_id": paid, "amount_cents": 20000, "status": "pending"},
    ]
    assert e2e.rows("SELECT amount_cents, status FROM cash_deposits") == [
        {"amount_cents": 5000, "status": "pending"},
    ]
    assert e2e.rows("SELECT COUNT(*) AS n FROM returns")[0]["n"] == 0


def test_manager_sees_only_own_debts_boss_sees_all_with_owner(open_app, e2e):
    _add_manager2(e2e)
    mine = _order(e2e, price=100.0)
    foreign = _order(e2e, price=250.0, owner=MGR2)

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "debts")
    assert mgr.locator(".debt-card").count() == 1
    assert f"#{mine}" in _text(mgr, ".debt-card")
    assert mgr.locator(f'.btn-pay-debt[data-id="{foreign}"]').count() == 0
    assert mgr.locator("text=Нам должны").count() == 0, "итог «нам должны» — только руководству"
    # Чужой долг не отметить и в обход экрана.
    res = _api(mgr, "/api/orders/mark_paid", {"order_id": foreign, "amount": 10})
    assert res["status"] == 403
    assert e2e.rows("SELECT COUNT(*) AS n FROM payments")[0]["n"] == 0
    # «Мои долги»: сводка остатка — только по своему заказу.
    assert "100USD" in _norm(_text(mgr, ".money-base-total"))

    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    assert boss.locator(".debt-card").count() == 2
    foreign_card = boss.locator(f'.debt-card:has(.btn-pay-debt[data-id="{foreign}"])')
    assert "Manager2" in foreign_card.locator(".debt-owner").text_content()
    assert boss.locator(f'.debt-card:has(.btn-pay-debt[data-id="{mine}"]) .debt-owner').count() == 1
    assert "350USD" in _norm(_text(boss, ".money-base-total"))


def test_boss_ops_tab_has_return_form_but_no_cash_or_payment_forms(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    _money(boss, "ops")
    assert boss.locator("#ret-create").count() == 1
    assert boss.locator("#dep-create").count() == 0, "босс наличные не сдаёт — он их принимает"
    assert boss.locator("#pay-submit").count() == 0

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    for sel in ("#dep-create", "#pay-submit", "#ret-create"):
        assert mgr.locator(sel).count() == 1, sel
    # У менеджера нет «Подтвердить» — подтверждать ему нечего.
    assert mgr.locator(".dep-confirm, .pay-confirm, .ret-confirm").count() == 0


# ─── «Подтвердить» ───────────────────────────────────────────────────────────


def test_confirm_badge_counts_payment_deposit_and_return(open_app, e2e):
    from services.database import create_return

    _order(e2e, payment_type="paid", price=200.0)                 # ждёт оплата
    _deposit(e2e, 30.0)                                           # ждёт сдача
    returned = _order(e2e, payment_type="paid", price=90.0)
    _confirm_order_payments(e2e, returned)
    item = e2e.rows("SELECT id FROM order_items WHERE order_id = ?", (returned,))[0]["id"]
    assert e2e.run(create_return(returned, "full", "Брак", [(item, 1, 90.0)], "no_refund",
                                 e2e.ids["mgr"])).get("ok")  # оплаченный заказ: «в счёт долга» вычитать не из чего (_debt_reduction_refusal)

    boss = open_app(e2e.ids["boss"])
    _money(boss, "confirm")
    # Руководство: общий бейдж «Решений» на панели, секции по видам.
    badge = "() => document.querySelector('#bottom-nav [data-decisions-badge]')?.textContent"
    boss.wait_for_function(f"{badge} === '3'")
    labels = _norm(" ".join(_texts(boss, "#content .section-label")))
    assert "Оплатыкартойиперечислением(1)" in labels
    assert "Сдачиналичных(1)" in labels
    assert "Возвраты(1)" in labels

    boss.click(".dep-confirm")
    _alert(boss, "Сдача подтверждена")
    _idle(boss)
    boss.wait_for_function(f"{badge} === '2'")


def test_boss_rejects_paid_order_payment_from_confirm_tab(open_app, e2e):
    oid = _order(e2e, payment_type="paid", qty=2, price=100.0)
    boss = open_app(e2e.ids["boss"])
    _money(boss, "confirm")
    card = boss.locator(f'.debt-card[data-pay="{oid}"]')
    card.wait_for()
    assert "200USD" in _norm(card.locator(".debt-amount").text_content())
    assert f"Заказ #{oid}" in card.text_content() and "Manager" in card.text_content()
    assert "из200USD" in _norm(card.text_content())

    boss.click(f'.pay-reject[data-id="{oid}"]')
    boss.wait_for_selector(f'.pay-reject[data-id="{oid}"]', state="detached")
    assert f"confirm:Отклонить оплату по заказу #{oid}?" in boss.evaluate("window.__tgAlerts")
    _idle(boss)
    assert "Решений не ждёт" in boss.locator("#content").inner_text()

    assert e2e.rows("SELECT amount_cents, status FROM payments WHERE order_id = ?", (oid,)) == [
        {"amount_cents": 20000, "status": "rejected"},
    ]
    assert e2e.rows("SELECT paid_confirmed_at FROM orders WHERE id = ?", (oid,))[0]["paid_confirmed_at"] is None
    to_mgr = [p["text"] for p in e2e.pushes if p["uid"] == e2e.ids["mgr"]]
    assert any("Платёж отклонён" in t and "200 USD" in t for t in to_mgr), to_mgr
    # Отклонённое не попадает в поступления.
    summary = _api(boss, "/api/money/summary", {"period": "month"})["body"]
    assert summary["payments"] == [] and summary["base_total"] is None


def test_deposit_reject_needs_reason_notifies_manager_and_frees_orders(open_app, e2e):
    oid = _order(e2e, price=80.0, ship=True)
    dep = _deposit(e2e, 80.0)
    assert e2e.rows("SELECT order_id, amount_allocated_cents FROM cash_deposit_orders") == [
        {"order_id": oid, "amount_allocated_cents": 8000},
    ]

    boss = open_app(e2e.ids["boss"])
    _money(boss, "confirm")
    card = f'.debt-card[data-dep="{dep}"]'
    boss.wait_for_selector(card)
    assert _norm(f"#{oid} — 80 USD") in _norm(_text(boss, f"{card} .debt-meta"))
    box = boss.locator(f"{card} .dep-reject-box")
    assert box.is_hidden()
    boss.click(f"{card} .dep-reject")
    assert box.is_visible()
    boss.click(f"{card} .dep-reject")
    assert box.is_hidden(), "повторное нажатие прячет поле причины"
    boss.click(f"{card} .dep-reject")

    boss.fill(f"{card} .dep-reason", "ok")
    boss.click(f"{card} .dep-reject-send")
    _alert(boss, "❌ Укажите причину")
    assert e2e.rows("SELECT status FROM cash_deposits")[0]["status"] == "pending"

    boss.fill(f"{card} .dep-reason", "Не хватает 20 долларов")
    boss.click(f"{card} .dep-reject-send")
    _alert(boss, "Сдача отклонена")
    boss.wait_for_selector(card, state="detached")
    assert e2e.rows("SELECT status, reject_reason, confirmed_by FROM cash_deposits") == [
        {"status": "rejected", "reject_reason": "Не хватает 20 долларов", "confirmed_by": e2e.ids["boss"]},
    ]
    to_mgr = [m["text"] for m in e2e.bot.messages if m["chat_id"] == e2e.ids["mgr"]]
    assert any(f"сдача #{dep} отклонена" in t and "Не хватает 20 долларов" in t for t in to_mgr), to_mgr
    assert e2e.rows("SELECT status, payment_confirmed FROM orders WHERE id = ?", (oid,))[0] == {
        "status": "shipped", "payment_confirmed": 0,
    }

    # Менеджер видит причину в «Моих сдачах» и сдаёт заново: отклонённая сдача
    # больше не держит остаток заказа — новая ложится на него целиком.
    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    assert "Не хватает 20 долларов" in _text(mgr, f".stock-row:has-text('#{dep}')")
    mgr.fill("#dep-amount", "80")
    mgr.click("#dep-create")
    _alert(mgr, "отправлена на подтверждение")
    new = e2e.rows("SELECT id FROM cash_deposits WHERE status = 'pending'")[0]["id"]
    assert e2e.rows("SELECT order_id, amount_allocated_cents FROM cash_deposit_orders WHERE deposit_id = ?",
                    (new,)) == [{"order_id": oid, "amount_allocated_cents": 8000}]
    deps = _api(boss, "/api/money/summary", {"period": "month"})["body"]["deposits"]
    assert (deps["total_cents"], deps["count"], deps["by_currency"]) == (0, 0, [])


def test_deposit_validation_rejects_non_positive_amounts(open_app, e2e):
    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    for raw in ("", "0", "-5"):
        mgr.evaluate("window.__tgAlerts.length = 0")
        mgr.fill("#dep-amount", raw)
        mgr.click("#dep-create")
        _alert(mgr, "Введите положительную сумму")
    assert e2e.rows("SELECT COUNT(*) AS n FROM cash_deposits")[0]["n"] == 0
    # Центы не теряются: 80.55 → 8055 копеек. Открытых заказов нет — сдача без
    # распределения, но в кассу она всё равно идёт.
    mgr.fill("#dep-amount", "80.55")
    mgr.click("#dep-create")
    _alert(mgr, "отправлена на подтверждение")
    assert e2e.rows("SELECT amount_cents, status FROM cash_deposits") == [{"amount_cents": 8055, "status": "pending"}]
    assert e2e.rows("SELECT COUNT(*) AS n FROM cash_deposit_orders")[0]["n"] == 0
    _idle(mgr)
    # Копейки не округляются: 80.55 — это не «81 USD».
    assert _norm("— 80,55 USD") in _norm(_text(mgr, ".stock-row .stock-name"))


# ─── Сдача наличных: FIFO по двум заказам → подтверждение → касса в отчёте ───


def test_cash_deposit_fifo_across_orders_confirmed_lands_in_cash_report(open_app, e2e):
    first = _order(e2e, price=50.0, ship=True)
    second = _order(e2e, price=80.0, ship=True)
    # FIFO по времени создания: делаем порядок явным, а не «в ту же секунду».
    e2e.exec("UPDATE orders SET created_at = '2026-01-01 09:00:00' WHERE id = ?", (first,))
    e2e.exec("UPDATE orders SET created_at = '2026-01-02 09:00:00' WHERE id = ?", (second,))

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    mgr.fill("#dep-amount", "100")
    mgr.click("#dep-create")
    _alert(mgr, "отправлена на подтверждение")
    dep = e2e.rows("SELECT id, amount_cents, status FROM cash_deposits")[0]
    assert dep["amount_cents"] == 10000 and dep["status"] == "pending"
    alloc = e2e.rows("SELECT order_id, amount_allocated_cents FROM cash_deposit_orders ORDER BY order_id")
    assert alloc == [{"order_id": first, "amount_allocated_cents": 5000},
                     {"order_id": second, "amount_allocated_cents": 5000}], "первый закрыт, второй — остатком"
    # Карточка сдачи ушла всем, кто подтверждает: босс, админ, бухгалтер.
    cards = [m for m in e2e.bot.messages if f"Сдача наличных #{dep['id']}" in m["text"]]
    assert {m["chat_id"] for m in cards} == {e2e.ids["boss"], e2e.ids["admin"], e2e.ids["book"]}
    assert f"заказ #{first} — 50.00 USD" in cards[0]["text"] and f"заказ #{second} — 50.00 USD" in cards[0]["text"]
    _idle(mgr)
    assert _norm(f"#{dep['id']} — 100 USD") in _norm(_text(mgr, ".stock-row .stock-name"))

    boss = open_app(e2e.ids["boss"])
    _money(boss, "confirm")
    card = f'.debt-card[data-dep="{dep["id"]}"]'
    boss.wait_for_selector(card)
    assert "100USD" in _norm(_text(boss, f"{card} .debt-amount"))
    assert _norm(f"#{first} — 50 USD, #{second} — 50 USD") in _norm(_text(boss, f"{card} .debt-meta"))
    boss.click(f"{card} .dep-confirm")
    _alert(boss, "Сдача подтверждена")

    orders = {r["id"]: r for r in e2e.rows("SELECT id, status, payment_confirmed FROM orders")}
    assert orders[first] == {"id": first, "status": "paid", "payment_confirmed": 1}
    assert orders[second] == {"id": second, "status": "shipped", "payment_confirmed": 0}
    assert any(m["chat_id"] == e2e.ids["mgr"] and f"Закрыты заказы: #{first}." in m["text"]
               for m in e2e.bot.messages)

    # «Долги»: первый ушёл, по второму осталось 80 − 50 = 30.
    _money(boss, "debts")
    assert boss.locator(f'.btn-pay-debt[data-id="{first}"]').count() == 0
    button = _text(boss, f'.btn-pay-debt[data-id="{second}"]')
    assert "ост.30USD" in _norm(button), button
    assert "30USD" in _norm(_text(boss, ".money-base-total"))
    debt = [d for d in _api(boss, "/api/debts")["body"]["debts"] if d["id"] == second][0]
    assert (debt["total"], debt["confirmed"], debt["remaining"]) == (80.0, 0.0, 30.0)

    # «Отчёт»: наличные в кассе — 100, итог ≈ 100 USD, в ленте — принятая сдача.
    tab(boss, "report")
    _idle(boss)
    assert _norm("Наличные (сдачи) · 100 USD") in _norm(" ".join(_texts(boss, ".stock-name")))
    assert "1сдача" in _norm(" ".join(_texts(boss, ".stock-folder")))
    assert _norm(_text(boss, ".money-total")) == "≈100USD"
    ribbon = _norm(" ".join(_texts(boss, "#money-body .c-row:has(.card-row-icon)")))
    assert "Сдача·100USD" in ribbon and "принят" in ribbon


# ─── Ручной платёж (не по заказу) ────────────────────────────────────────────


def test_manual_payment_rows_validate_and_send_one_payment_per_currency(open_app, e2e):
    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    rows = mgr.locator("#pay-rows .pay-row")
    assert rows.count() == 1
    mgr.click(".pay-row-del")
    assert rows.count() == 1, "последнюю строку не удалить"

    mgr.click("#pay-submit")
    assert "Введите положительную сумму" in _text(mgr, "#pay-status")
    mgr.fill(".pay-row-amount", "250000")
    mgr.click("#pay-submit")
    assert "Укажите комментарий" in _text(mgr, "#pay-status")
    assert e2e.rows("SELECT COUNT(*) AS n FROM payments")[0]["n"] == 0

    # Новая строка наследует валюту последней — удобно вводить серию.
    mgr.click(".pay-row:nth-child(1) [data-cur-opt='UZS']")
    mgr.click("#pay-add-row")
    assert rows.count() == 2
    assert rows.nth(1).locator(".pay-row-cur").get_attribute("data-cur") == "UZS"
    rows.nth(1).locator("[data-cur-opt='USD']").click()
    assert rows.nth(1).locator("[data-cur-opt='USD']").get_attribute("aria-pressed") == "true"
    mgr.click("#pay-add-row")
    assert rows.count() == 3 and rows.nth(2).locator(".pay-row-cur").get_attribute("data-cur") == "USD"
    rows.nth(2).locator(".pay-row-del").click()
    assert rows.count() == 2

    rows.nth(1).locator(".pay-row-amount").fill("75.5")
    mgr.fill("#pay-comment", "Аренда склада")
    mgr.click("#pay-submit")
    # Успех перерисовывает кассу: форма снова из одной пустой строки.
    mgr.wait_for_function("() => document.querySelectorAll('#pay-rows .pay-row').length === 1"
                          " && document.querySelector('#pay-comment').value === ''")

    pays = e2e.rows("SELECT id, amount_cents, currency, status, order_id, comment FROM payments ORDER BY id")
    assert [(p["amount_cents"], p["currency"], p["status"], p["order_id"], p["comment"]) for p in pays] == [
        (25_000_000, "UZS", "pending", None, "Аренда склада"),
        (7550, "USD", "pending", None, "Аренда склада"),
    ]
    # Одно уведомление боссу — с кнопкой на каждый платёж.
    notes = [p for p in e2e.pushes if "Новые платежи" in p["text"]]
    assert len(notes) == 1 and notes[0]["uid"] == e2e.ids["boss"]
    buttons = [row[0]["callback_data"] for row in notes[0]["reply_markup"]["inline_keyboard"]]
    assert buttons == [f"pay_ok:{pays[0]['id']}", f"pay_ok:{pays[1]['id']}"]


# ─── Возврат: частичный по позициям, деньги наличными из кассы ───────────────


def test_partial_return_with_cash_refund_reduces_cash_in_report(open_app, e2e):
    oid = _order(e2e, payment_type="paid", qty=3, price=100.0)
    _confirm_order_payments(e2e, oid)

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    mgr.click("#ret-load")
    _alert(mgr, "Сначала укажите номер заказа")
    mgr.fill("#ret-order", str(oid))
    mgr.click("#ret-load")
    mgr.wait_for_selector("#ret-positions:not([hidden]) .ret-pos")
    assert mgr.locator(".ret-pos").count() == 1
    assert mgr.input_value(".ret-qty") == "3"
    mgr.fill(".ret-qty", "1")
    mgr.click('[data-refund="cash"]')
    assert mgr.get_attribute('[data-refund="cash"]', "aria-pressed") == "true"
    assert mgr.get_attribute('[data-refund="debt_reduction"]', "aria-pressed") == "false"
    mgr.fill("#ret-reason", "Бр")
    mgr.click("#ret-create")
    _alert(mgr, "Опишите причину")
    mgr.fill(".ret-qty", "0")
    mgr.fill("#ret-reason", "Брак одной бухты")
    mgr.click("#ret-create")
    _alert(mgr, "Укажите количество хотя бы по одной позиции")
    assert e2e.rows("SELECT COUNT(*) AS n FROM returns")[0]["n"] == 0
    mgr.fill(".ret-qty", "1")
    mgr.click("#ret-create")
    _alert(mgr, "Возврат #")

    ret = e2e.rows("SELECT id, return_type, total_amount_cents, refund_method, status FROM returns")[0]
    assert (ret["return_type"], ret["total_amount_cents"], ret["refund_method"], ret["status"]) == (
        "partial", 10000, "cash", "pending")
    assert e2e.rows("SELECT qty, amount_cents FROM return_items") == [{"qty": 1, "amount_cents": 10000}]

    boss = open_app(e2e.ids["boss"])
    _money(boss, "confirm")
    card = f'.debt-card[data-ret="{ret["id"]}"]'
    boss.wait_for_selector(card)
    assert "100USD" in _norm(_text(boss, f"{card} .debt-amount"))
    assert "Брак одной бухты" in _text(boss, card)
    boss.click(f"{card} .ret-goods")
    _alert(boss, "Товар отмечен как принятый")
    boss.wait_for_selector(f"{card} .ret-confirm:not([disabled])")
    boss.click(f"{card} .ret-confirm")
    _alert(boss, "Возврат подтверждён")

    assert e2e.rows("SELECT status, return_status FROM orders WHERE id = ?", (oid,))[0] == {
        "status": "partially_returned", "return_status": "partial",
    }
    assert e2e.rows("SELECT quantity, returned_qty FROM order_items WHERE order_id = ?", (oid,)) == [
        {"quantity": 3, "returned_qty": 1},
    ]
    # Деньги отдали из кассы — отрицательная подтверждённая сдача на 100.
    assert e2e.rows("SELECT amount_cents, status FROM cash_deposits") == [
        {"amount_cents": -10000, "status": "confirmed"},
    ]

    _money(boss, "report")
    _idle(boss)
    names = _norm(" ".join(_texts(boss, ".stock-name")))
    assert "USD·300" in names and "Наличные(сдачи)·-100USD" in names
    assert _norm(_text(boss, ".money-total")) == "≈200USD", "300 оплаты − 100 выдано из кассы"
    ribbon = _norm(" ".join(_texts(boss, "#money-body .c-row:has(.card-row-icon)")))
    assert "Возврат·100USD" in ribbon and f"заказ#{oid}" in ribbon
    summary = _api(boss, "/api/money/summary", {"period": "month"})["body"]
    assert summary["base_total"] == 200.0
    assert (summary["deposits"]["total_cents"], summary["deposits"]["count"]) == (-10000, 1)


def test_return_refused_for_unshipped_and_foreign_order(open_app, e2e):
    _add_manager2(e2e)
    oid = _order(e2e, price=100.0)  # одобрен, но не отгружен

    mgr2 = open_app(MGR2)
    _money(mgr2, "ops")
    mgr2.fill("#ret-order", str(oid))
    mgr2.fill("#ret-reason", "Клиент передумал")
    mgr2.click("#ret-create")
    _alert(mgr2, "Возврат доступен только для отгруженных/оплаченных")

    from services.database import mark_order_shipped

    assert e2e.run(mark_order_shipped(oid, e2e.ids["keeper"], "Keeper")).get("ok")
    mgr2.wait_for_selector("#ret-create:not([disabled])")
    mgr2.click("#ret-create")
    _alert(mgr2, "Возврат только по своим заказам")
    mgr2.click("#ret-load")
    # Отказ «Оформить» ушёл в тост (_alert выше его поймал), отказ «Выбрать
    # позиции» — по-прежнему диалог.
    mgr2.wait_for_function("() => window.__tgAlerts.some(a => a.includes('только по своим'))")
    assert e2e.rows("SELECT COUNT(*) AS n FROM returns")[0]["n"] == 0


def test_cash_refund_for_uzs_order_without_rate_is_not_booked_as_usd(open_app, e2e):
    """Возврат 1 250 000 сумов наличными при незаданном курсе UZS.

    confirm_return конвертирует выдачу в базовую валюту, а при отсутствии курса
    «пишет как есть» — из кассы уходит 1 250 000 ДОЛЛАРОВ. Ожидаем, что сумма
    в сумах не превращается в доллары один к одному.
    """
    from services.database import create_return

    oid = _order(e2e, payment_type="paid", price=1_250_000.0, currency="UZS")
    _confirm_order_payments(e2e, oid)
    item = e2e.rows("SELECT id FROM order_items WHERE order_id = ?", (oid,))[0]["id"]
    assert e2e.run(create_return(oid, "full", "Брак", [(item, 1, 1_250_000.0)], "cash",
                                 e2e.ids["mgr"])).get("ok")

    boss = open_app(e2e.ids["boss"])
    _money(boss, "confirm")
    boss.click(".ret-goods")
    _alert(boss, "Товар отмечен как принятый")
    boss.wait_for_selector(".ret-confirm:not([disabled])")
    boss.click(".ret-confirm")
    boss.wait_for_function(
        "() => window.__tgAlerts.some(a => a.includes('Возврат подтверждён') || a.startsWith('❌'))"
        " || [...document.querySelectorAll('.toast')].some(t => /Возврат подтверждён/.test(t.textContent)"
        " || t.classList.contains('toast--error'))"
    )
    cash = e2e.rows("SELECT amount_cents FROM cash_deposits")
    assert cash != [{"amount_cents": -125_000_000}], "касса «выдала» 1 250 000 USD"


# ─── «Долги»: сроки, фильтр, отметка оплаты, отказ ───────────────────────────


def test_debts_buckets_sums_and_due_now_filter(open_app, e2e):
    overdue = _order(e2e, price=100.0)
    today_ = _order(e2e, price=200.0)
    future = _order(e2e, price=300.0)
    e2e.exec("UPDATE orders SET due_date = '2020-01-01' WHERE id = ?", (overdue,))
    e2e.exec("UPDATE orders SET due_date = ? WHERE id = ?", (date.today().isoformat(), today_))

    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    stats = {k: (_text(boss, f".debt-stat-{k} .debt-stat-num"), _norm(_text(boss, f".debt-stat-{k} .debt-stat-sum")))
             for k in ("overdue", "today", "upcoming")}
    assert stats == {"overdue": ("1", "100USD"), "today": ("1", "200USD"), "upcoming": ("1", "300USD")}
    status = {boss.locator(".debt-card").nth(i).get_attribute("data-status") for i in range(3)}
    assert status == {"overdue", "due_today", "upcoming"}
    assert "600USD" in _norm(_text(boss, ".money-base-total"))
    assert "Просрочен" in _text(boss, '.debt-card[data-status="overdue"] .debt-state')

    boss.click('.seg-item[data-f="today"]')
    _idle(boss)
    assert boss.get_attribute('.seg-item[data-f="today"]', "aria-pressed") == "true"
    ids = sorted(int(x) for x in boss.eval_on_selector_all(".btn-pay-debt", "els => els.map(e => e.dataset.id)"))
    assert ids == sorted([overdue, today_]), "будущий срок в «К оплате сейчас» не попадает"
    assert boss.locator(".debt-stat-upcoming").count() == 0
    assert "300USD" in _norm(_text(boss, ".money-base-total"))
    assert _api(boss, "/api/debts", {"mode": "today"})["body"]["remaining_by_currency"] == [
        {"currency": "USD", "total": 300.0},
    ]
    boss.click('.seg-item[data-f="all"]')
    _idle(boss)
    assert boss.locator(".btn-pay-debt").count() == 3
    assert future in [int(x) for x in boss.eval_on_selector_all(".btn-pay-debt", "els => els.map(e => e.dataset.id)")]


def test_debt_payment_form_validates_amounts_and_refuses_overpayment(open_app, e2e):
    """Оплата долга — форма «как получены деньги»: ноль и минус не отправить,
    переплату не срезаем молча, а показываем и не пускаем (сервер — тоже)."""
    oid = _order(e2e, qty=2, price=100.0)  # 200 USD
    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "debts")
    btn = f'.btn-pay-debt[data-id="{oid}"]'
    assert "ост.200USD" in _norm(_text(mgr, btn))
    pay_form(mgr, btn, [("card", "0")], submit=False)
    assert mgr.locator(".c-overlay #ms-submit").is_disabled()
    mgr.fill(".c-overlay .pay-part-amount", "-5")
    assert mgr.locator(".c-overlay #ms-submit").is_disabled()
    mgr.fill(".c-overlay .pay-part-amount", "999")
    assert "Большенужногона799USD" in _norm(_text(mgr, ".c-overlay .pay-total"))
    assert mgr.locator(".c-overlay #ms-submit").is_disabled()
    assert e2e.rows("SELECT COUNT(*) AS n FROM payments")[0]["n"] == 0
    card_id = e2e.rows("SELECT id FROM acc_accounts WHERE kind = 'card'")[0]["id"]
    res = _api(mgr, "/api/orders/payment", {"order_id": oid, "parts": [
        {"method": "card", "currency": "USD", "amount": "999"}]})
    assert res["status"] == 400 and res["body"]["code"] == "account_required", "карта без «куда» — отказ"
    res = _api(mgr, "/api/orders/payment", {"order_id": oid, "parts": [
        {"method": "card", "currency": "USD", "amount": "999", "account_id": card_id}]})
    assert res["status"] == 400 and res["body"]["code"] == "over"

    mgr.fill(".c-overlay .pay-part-amount", "200")
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".toast:has-text('записана')")
    assert e2e.rows("SELECT amount_cents, currency, status FROM payments") == [
        {"amount_cents": 20000, "currency": "USD", "status": "pending"},
    ]
    push = [p for p in e2e.pushes if "Требуется подтверждение оплаты" in p["text"]]
    assert push and "закрывает долг полностью" in push[0]["text"]
    assert "на карту •••• 1234 (Фаридун М.) · 200 USD" in push[0]["text"]
    # Больше вносить нечего: остаток уже заявлен.
    res = _api(mgr, "/api/orders/payment", {"order_id": oid, "parts": [
        {"method": "cash", "currency": "USD", "amount": "1"}]})
    assert res["status"] == 400 and "нечего вносить" in res["body"]["detail"]
    _idle(mgr)
    assert mgr.locator(btn).count() == 0
    text = _norm(_text(mgr, ".debt-awaiting"))
    assert _norm("Оплата 200 USD ждёт подтверждения · после подтверждения долг: 0 USD") in text
    assert "подтвердит" in text, "руководитель в системе есть — менеджеру кнопки нет, сказано, кто"


def test_boss_rejects_marked_payment_in_debts_and_debt_returns(open_app, e2e):
    from services.database import mark_order_paid

    oid = _order(e2e, qty=2, price=100.0)
    ok, pid = e2e.run(mark_order_paid(oid, e2e.ids["mgr"], "Manager", amount=80.0))
    assert ok

    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    awaiting = boss.locator(".debt-awaiting")
    assert _norm("Оплата 80 USD ждёт подтверждения · после подтверждения долг: 120 USD") in _norm(
        awaiting.text_content())
    assert "80USD" in _norm(_text(boss, ".money-pending .money-value"))
    boss.click(f'.debt-awaiting .btn-reject-pay[data-id="{oid}"]')
    boss.wait_for_selector(".debt-awaiting", state="detached")
    assert any(a.startswith("confirm:Отклонить ожидающие платежи?") for a in boss.evaluate("window.__tgAlerts"))
    _idle(boss)

    assert e2e.rows("SELECT id, status FROM payments") == [{"id": pid, "status": "rejected"}]
    card = boss.locator(f'.debt-card:has(.btn-pay-debt[data-id="{oid}"])')
    assert card.get_attribute("data-status") == "upcoming"
    assert "ост.200USD" in _norm(_text(boss, f'.btn-pay-debt[data-id="{oid}"]'))
    assert "пусто" in _text(boss, ".money-pending .money-value")
    to_mgr = [p["text"] for p in e2e.pushes if p["uid"] == e2e.ids["mgr"]]
    assert any("Платёж отклонён" in t and "80 USD" in t for t in to_mgr), to_mgr


def test_debts_convert_currencies_to_base_only_with_rate(open_app, e2e):
    _order(e2e, qty=2, price=100.0)                               # 200 USD
    _order(e2e, price=1_250_000.0, currency="UZS")                # 1 250 000 UZS
    _set_rate(e2e, "UZS", None)

    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    total = _norm(_text(boss, ".money-base-total"))
    assert "1250000UZS" in total and "200USD" in total
    assert "≈200USD(частьбезкурса)" in total, total
    assert _norm(boss.locator(".c-row:has-text('Всего') .card-row-value").first.text_content()) == \
        "≈200USD(частьбезкурса)"
    # Суммы разных валют в «Просрочено/Сегодня/Будущие» не складываются.
    assert _norm(_text(boss, ".debt-stat-upcoming .debt-stat-sum")) == "1250000UZS200USD"
    body = _api(boss, "/api/debts")["body"]
    assert (body["remaining_base_total"], body["remaining_base_partial"]) == (200.0, True)

    _set_rate(e2e, "UZS", 0.00008)  # 1 USD = 12 500 сум
    _redraw_debts(boss)
    total = _norm(_text(boss, ".money-base-total"))
    assert "≈300USD" in total and "частьбезкурса" not in total, total
    assert _norm(boss.locator(".c-row:has-text('Всего') .card-row-value").first.text_content()) == "≈300USD"
    body = _api(boss, "/api/debts")["body"]
    assert (body["remaining_base_total"], body["remaining_base_partial"]) == (300.0, False)


def test_debt_reduction_return_shrinks_debt_and_full_remaining_payment(open_app, e2e):
    """Возврат «в счёт долга»: касса не трогается, остаток по заказу тает на сумму возврата."""
    from services.database import mark_order_paid

    oid = _order(e2e, qty=3, price=100.0, ship=True)  # 300 USD
    ok, _pid = e2e.run(mark_order_paid(oid, e2e.ids["mgr"], "Manager", amount=120.0))
    assert ok
    _confirm_order_payments(e2e, oid)

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    mgr.fill("#ret-order", str(oid))
    mgr.click("#ret-load")
    mgr.wait_for_selector(".ret-pos")
    mgr.fill(".ret-qty", "1")
    assert mgr.get_attribute('[data-refund="debt_reduction"]', "aria-pressed") == "true", "по умолчанию — в счёт долга"
    mgr.fill("#ret-reason", "Лишняя бухта")
    mgr.click("#ret-create")
    _alert(mgr, "Возврат #")
    rid = e2e.rows("SELECT id, refund_method FROM returns")[0]
    assert rid["refund_method"] == "debt_reduction"

    boss = open_app(e2e.ids["boss"])
    _money(boss, "confirm")
    boss.click(f'.debt-card[data-ret="{rid["id"]}"] .ret-goods')
    _alert(boss, "Товар отмечен как принятый")
    boss.wait_for_selector(".ret-confirm:not([disabled])")
    boss.click(".ret-confirm")
    _alert(boss, "Возврат подтверждён")
    assert e2e.rows("SELECT COUNT(*) AS n FROM cash_deposits")[0]["n"] == 0, "касса не выдаёт денег"

    _money(boss, "debts")
    _idle(boss)
    # 300 − 120 оплачено − 100 возвращено = 80.
    card = boss.locator('.debt-card[data-status="partial"]')
    assert _norm("Остаток: 80 USD") in _norm(card.text_content())
    debt = _api(boss, "/api/debts")["body"]["debts"][0]
    assert (debt["confirmed"], debt["remaining"]) == (120.0, 80.0)

    _money(mgr, "debts")
    # Форма предзаполнена всем остатком (80) — способ и «Записать».
    pay_form(mgr, f'.btn-pay-debt[data-id="{oid}"]', [("bank", "80")])
    mgr.wait_for_selector(".toast:has-text('записана')")
    assert e2e.rows("SELECT amount_cents FROM payments WHERE status = 'pending'") == [{"amount_cents": 8000}]
    _redraw_debts(boss)
    boss.click(f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]')
    boss.wait_for_selector(".debt-awaiting", state="detached")
    assert e2e.rows("SELECT paid_confirmed_at FROM orders WHERE id = ?", (oid,))[0]["paid_confirmed_at"]


def test_uzs_credit_order_is_paid_in_its_currency_and_converted_in_report(open_app, e2e):
    oid = _order(e2e, price=1_250_000.0, currency="UZS")
    _set_rate(e2e, "UZS", 0.00008)

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "debts")
    assert "ост.1250000UZS" in _norm(_text(mgr, f'.btn-pay-debt[data-id="{oid}"]'))
    pay_form(mgr, f'.btn-pay-debt[data-id="{oid}"]', [("card", "500000")])
    mgr.wait_for_selector(".toast:has-text('записана')")
    assert e2e.rows("SELECT amount_cents, currency FROM payments") == [{"amount_cents": 50_000_000, "currency": "UZS"}]
    push = [p for p in e2e.pushes if "Требуется подтверждение оплаты" in p["text"]][-1]
    assert "500 000 UZS" in push["text"] and "≈ <b>100 USD</b>" in push["text"], push["text"]

    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    boss.click(f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]')
    boss.wait_for_selector(".debt-awaiting", state="detached")
    _idle(boss)
    card = _norm(boss.locator('.debt-card[data-status="partial"]').text_content())
    assert "Оплачено:500000UZS" in card and "Остаток:750000UZS" in card, card
    # Один остаток в одной валюте — без «≈»: переводить не во что.
    assert _norm(_text(boss, ".money-base-total")) == _norm("Осталось получить: 750 000 UZS")
    assert "500000UZS" in _norm(_text(boss, ".money-received .money-value"))

    tab(boss, "report")
    _idle(boss)
    assert _norm(_text(boss, ".money-total")) == "≈40USD", "500 000 × 0.00008"
    assert _norm(_text(boss, "#money-body .stock-row .stock-name")) == "UZS·500000"


def test_money_report_laggard_row_opens_buyer_card(open_app, e2e):
    deal = _credit_machine(e2e)
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).date()
    # Платёж с плановой датой в начале недели получен днём позже — опоздание.
    e2e.exec(
        "UPDATE machine_deal_payments SET due_date = ?, paid_at = ?, paid_by = ? WHERE deal_id = ? AND seq = 1",
        (week_start.isoformat(), f"{week_start + timedelta(days=1)} 10:00:00", e2e.ids["boss"], deal),
    )
    boss = open_app(e2e.ids["boss"])
    _money(boss, "report")
    boss.wait_for_selector("#money-insights [data-buyer]")
    assert "0из1·0%" in _norm(_text(boss, "#money-insights .c-row:has-text('Платежей в срок')"))
    row = boss.locator('#money-insights [data-buyer="Азиз Рахимов"]')
    assert _norm(row.locator(".card-row-value").text_content()) == "1из1"
    row.click()
    boss.wait_for_selector(".editor-title:has-text('Азиз Рахимов')")
    assert boss.locator("[data-payment]").count() == 3


# ─── Рассрочки по технике в «Долгах» и карточка покупателя ───────────────────


def test_machine_installment_row_opens_buyer_card_and_payments_close_deal(open_app, e2e):
    _credit_machine(e2e)  # 20 000: взнос 5 000 + 3 × 5 000

    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    row = '[data-buyer="Азиз Рахимов"]'
    boss.wait_for_selector(row)
    assert _norm(_text(boss, f"{row} .card-row-value")) == "15000USD"
    assert _norm("Следующий: 5 000 USD до") in _norm(_text(boss, f"{row} .card-row-sub"))
    assert _norm("По технике") in _norm(_text(boss, ".c-surface"))
    assert "15000USD" in _norm(boss.locator(".c-row:has-text('Всего') .card-row-value").first.text_content())

    boss.click(row)
    boss.wait_for_selector(".editor-title:has-text('Азиз Рахимов')")
    outstanding = ".c-row:has-text('Всего по рассрочкам')"
    assert _norm(_text(boss, f"{outstanding} .card-row-value")) == "15000USD"
    assert "3платежа" in _norm(_text(boss, f"{outstanding} .card-row-sub"))
    assert boss.locator('[data-payment][data-paid="0"]').count() == 3

    boss.click('[data-payment][data-paid="0"]')
    boss.wait_for_function("() => document.querySelectorAll('[data-payment][data-paid=\"1\"]').length === 1")
    assert e2e.rows("SELECT amount_cents FROM machine_payment_receipts") == [{"amount_cents": 500_000}]
    assert _norm(_text(boss, f"{outstanding} .card-row-value")) == "10000USD"

    # Снять отметку — деньги уходят обратно в долг.
    boss.click('[data-payment][data-paid="1"]')
    boss.wait_for_function("() => document.querySelectorAll('[data-payment][data-paid=\"1\"]').length === 0")
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_payment_receipts")[0]["n"] == 0
    assert _norm(_text(boss, f"{outstanding} .card-row-value")) == "15000USD"

    for left in (2, 1):
        boss.click('[data-payment][data-paid="0"]')
        boss.wait_for_function("(n) => document.querySelectorAll('[data-payment][data-paid=\"0\"]').length === n",
                               arg=left)
    boss.click('[data-payment][data-paid="0"]')
    boss.wait_for_selector(".toast:has-text('Рассрочка закрыта')")
    deal = e2e.rows("SELECT closed_at FROM machine_deals")[0]
    assert deal["closed_at"], "последний платёж закрывает сделку"
    assert e2e.rows("SELECT status FROM machines")[0]["status"] == "sold"
    assert e2e.rows("SELECT SUM(amount_cents) AS s FROM machine_payment_receipts")[0]["s"] == 1_500_000

    _money(boss, "debts")
    assert boss.locator("[data-buyer]").count() == 0
    assert boss.locator("text=По технике").count() == 0


def test_buyer_card_progress_counts_down_payment_and_paid_installments(open_app, e2e):
    _credit_machine(e2e)
    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    boss.click('[data-buyer="Азиз Рахимов"]')
    boss.wait_for_selector('[data-payment][data-paid="0"]')
    boss.click('[data-payment][data-paid="0"]')
    boss.wait_for_function("() => document.querySelectorAll('[data-payment][data-paid=\"1\"]').length === 1")
    # Взнос 5 000 + первый платёж 5 000 = 10 000 из 20 000; «осталось» обязано
    # совпасть с «Всего по рассрочкам» строкой выше.
    progress = _norm(_text(boss, ".schedule-total"))
    assert "Получено10000USDиз20000USD" in progress, progress
    assert "осталось10000USD" in progress, progress


def test_buyer_card_add_receipt_button_opens_form(open_app, e2e):
    _credit_machine(e2e)
    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    boss.click('[data-buyer="Азиз Рахимов"]')
    boss.wait_for_selector("[data-receipt-add]")
    boss.click("[data-receipt-add]")
    boss.wait_for_selector(".c-overlay #ms-f-amount", timeout=3000)


def test_partial_machine_receipt_reduces_debt_row(open_app, e2e):
    from services import machines

    deal = _credit_machine(e2e)
    assert e2e.run(machines.add_receipt(deal, 150_000, user_id=e2e.ids["boss"])).get("ok")  # 1 500 из 5 000
    boss = open_app(e2e.ids["boss"])
    _money(boss, "debts")
    boss.wait_for_selector("[data-buyer]")
    # График: 15 000 к получению, клиент уже внёс 1 500 → должен 13 500.
    assert _norm(_text(boss, "[data-buyer] .card-row-value")) == "13500USD"


# ─── «Отчёт»: поступления, курс, период, лента, «где деньги» ─────────────────


def test_money_report_totals_by_currency_convert_with_rate_and_warn_without(open_app, e2e):
    _standalone_payment(e2e, 150.0, "USD")
    _standalone_payment(e2e, 1_250_000.0, "UZS")
    _standalone_payment(e2e, 999.0, "USD", status="pending")       # не поступление
    _standalone_payment(e2e, 5_000_000.0, "UZS", status="rejected")  # не поступление
    _deposit(e2e, 80.0, confirm=True)
    _set_rate(e2e, "UZS", 0.00008)

    boss = open_app(e2e.ids["boss"])
    _money(boss, "report")
    assert "Поступления·Месяц" in _norm(" ".join(_texts(boss, "#money-body .section-label")))
    assert _norm(_text(boss, ".money-total")) == "≈330USD", "150 + 1 250 000 × 0.00008 + 80"
    names = [_norm(t) for t in _texts(boss, "#money-body .stock-row .stock-name")]
    assert names == ["UZS·1250000", "USD·150", "Наличные(сдачи)·80USD"]
    folders = [_norm(t) for t in _texts(boss, "#money-body .stock-row .stock-folder")]
    assert folders == ["1платёж", "1платёж", "1сдача"]
    assert boss.locator(".money-total-note").count() == 0
    s = _api(boss, "/api/money/summary", {"period": "month"})["body"]
    assert {p["currency"]: (p["total_cents"], p["count"]) for p in s["payments"]} == {
        "USD": (15000, 1), "UZS": (125_000_000, 1)}
    assert (s["base_total"], s["base_partial"], s["missing_rates"]) == (330.0, False, [])

    ribbon = [_norm(t) for t in _texts(boss, "#money-body .c-row:has(.card-row-icon)")]
    joined = " ".join(ribbon)
    for piece in ("Платёж·150USD", "Платёж·1250000UZS", "Сдача·80USD"):
        assert any(piece in r and "принят" in r for r in ribbon), (piece, joined)
    assert any("Платёж·999USD" in r and "ожидает" in r for r in ribbon), joined
    assert any("Платёж·5000000UZS" in r and "отклонён" in r for r in ribbon), joined

    # Курс сняли — сумы не пропадают молча: итог помечен, недостающее названо.
    _set_rate(e2e, "UZS", None)
    boss.click('[data-period="month"]')
    _idle(boss)
    assert _norm(_text(boss, ".money-total")) == "≈230USD(неполный)"
    assert _norm("Без курса не учтено: UZS 1 250 000 — задайте курс валют.") in _norm(
        " ".join(_texts(boss, ".money-total-note")))
    s = _api(boss, "/api/money/summary", {"period": "month"})["body"]
    assert (s["base_total"], s["base_partial"], s["missing_rates"]) == (
        230.0, True, [{"currency": "UZS", "amount": 1_250_000.0}])


def test_money_report_period_counts_by_confirmation_date(open_app, e2e):
    pid = _standalone_payment(e2e, 40.0, "USD")
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    before_week = (week_start - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    e2e.exec("UPDATE payments SET created_at = ?, confirmed_at = ? WHERE id = ?", (before_week, before_week, pid))
    in_year = before_week[:4] == str(now.year)

    boss = open_app(e2e.ids["boss"])
    _money(boss, "report")
    boss.click('[data-period="week"]')
    _idle(boss)
    assert boss.get_attribute('[data-period="week"]', "aria-pressed") == "true"
    assert "Поступления·Неделя" in _norm(" ".join(_texts(boss, "#money-body .section-label")))
    assert "За период поступлений нет" in boss.locator("#money-body").inner_text()
    assert "Движений пока нет" in boss.locator("#money-body").inner_text()

    boss.click('[data-period="year"]')
    _idle(boss)
    body = boss.locator("#money-body").inner_text()
    if in_year:
        assert _norm(_text(boss, ".money-total")) == "≈40USD"
        assert "Платёж·40USD" in _norm(body)
    else:  # первая неделя января: вчерашний платёж — в прошлом году
        assert "За период поступлений нет" in body

    boss.click('[data-period="custom"]')
    boss.wait_for_selector("#money-body .cal-host")
    assert "Выберите даты периода на календаре выше." in boss.locator("#money-body").inner_text()


def test_money_report_where_money_aging_top_debtors_forecast_discipline(open_app, e2e):
    from services import machines

    oid = _order(e2e, qty=2, price=100.0)
    e2e.exec("UPDATE orders SET due_date = ? WHERE id = ?", ((date.today() - timedelta(days=40)).isoformat(), oid))
    deal = _credit_machine(e2e)  # 3 × 5 000 впереди
    # Первый платёж графика — сегодня: он «ожидался в периоде», пока не оплачен.
    first = e2e.rows("SELECT id FROM machine_deal_payments WHERE deal_id = ? AND seq = 1", (deal,))[0]["id"]
    e2e.exec("UPDATE machine_deal_payments SET due_date = ? WHERE id = ?", (date.today().isoformat(), first))

    boss = open_app(e2e.ids["boss"])
    _money(boss, "report")
    boss.wait_for_selector("#money-insights .aging-row")
    insights = "#money-insights"
    total = boss.locator(f"{insights} .c-row:has-text('Всего') .card-row-value").first
    assert _norm(total.text_content()) == "15200USD"
    assert "200USD" in _norm(_text(boss, f"{insights} .c-row:has-text('По заказам')"))
    assert "15000USD" in _norm(_text(boss, f"{insights} .c-row:has-text('По технике')"))

    aging = {r.get_attribute("data-status"): (_norm(r.locator(".aging-sum").text_content()),
                                               _norm(r.locator(".aging-count").text_content()))
             for r in boss.locator(f"{insights} .aging-row[data-status]").all()}
    assert aging["overdue_30"] == ("200USD", "1документ")
    assert aging["not_due"] == ("15000USD", "3документа")
    assert aging["overdue_90"] == ("—", "0документов")

    top = boss.locator(f"{insights} .c-row:has(.card-row-sub:text-matches('техника|заказы'))")
    assert [_norm(top.nth(i).locator(".card-row-title").text_content()) for i in range(top.count())] == [
        "АзизРахимов", _norm(CP_NAME)]
    assert _norm(top.nth(0).locator(".card-row-value").text_content()) == "15000USD"

    fc = boss.locator(f"{insights} .aging-row:not([data-status])")
    assert fc.count() == 6, "прогноз на полгода вперёд, пустые месяцы не выбрасываются"
    assert sum(1 for i in range(6) if "5000USD" in _norm(fc.nth(i).locator(".aging-sum").text_content())) == 3
    assert "200USD" not in _norm(" ".join(fc.all_text_contents())), "просрочка — не будущее поступление"

    disc = _norm(_text(boss, f"{insights} .c-row:has-text('Собрано из ожидаемого')"))
    assert "—из5000USD" in disc, disc

    # Платёж пришёл в срок — дисциплина пересчитывается.
    assert e2e.run(machines.pay_installment(first, user_id=e2e.ids["boss"])).get("ok")
    boss.click('[data-period="month"]')
    _idle(boss)
    boss.wait_for_selector("#money-insights .aging-row")
    disc = _norm(_text(boss, f"{insights} .c-row:has-text('Собрано из ожидаемого')"))
    assert "5000USDиз5000USD" in disc, disc
    assert "1из1·100%" in _norm(_text(boss, f"{insights} .c-row:has-text('Платежей в срок')"))
    assert _norm(boss.locator(f"{insights} .c-row:has-text('Всего') .card-row-value").first.text_content()) \
        == "10200USD"


def test_money_report_uses_rate_frozen_at_confirmation(open_app, e2e):
    _set_rate(e2e, "UZS", 0.00008)   # 12 500 сум за доллар в день платежа
    pid = _standalone_payment(e2e, 1_250_000.0, "UZS")
    assert e2e.rows("SELECT fx_rate_to_base FROM payments WHERE id = ?", (pid,))[0]["fx_rate_to_base"] == 0.00008
    _set_rate(e2e, "UZS", 0.0001)    # сум укрепился: 10 000 за доллар

    boss = open_app(e2e.ids["boss"])
    _money(boss, "report")
    # Деньги пришли, когда 1 250 000 сум стоили 100 USD; задним числом их не
    # должно стать 125.
    assert _norm(_text(boss, ".money-total")) == "≈100USD"


# ─── Сквозняк: заказ в долг → частичные оплаты → долг тает → отчёт сходится ──


def test_end_to_end_credit_order_payments_shrink_debt_and_match_report(open_app, e2e):
    oid = _order(e2e, qty=3, price=100.0)  # 300 USD в долг
    mgr = open_app(e2e.ids["mgr"])
    boss = open_app(e2e.ids["boss"])

    # 1) Менеджер вносит 120 переводом на карту.
    _money(mgr, "debts")
    pay_form(mgr, f'.btn-pay-debt[data-id="{oid}"]', [("card", "120")])
    mgr.wait_for_selector(".toast:has-text('записана')")
    _idle(mgr)
    card = _norm(_text(mgr, ".debt-awaiting"))
    assert _norm("Оплата 120 USD ждёт подтверждения · после подтверждения долг: 180 USD") in card
    assert "накарту••••1234(ФаридунМ.)·120USD—ждётпроверкибанка" in card and "подтвердит" in card
    push = [p for p in e2e.pushes if "Требуется подтверждение оплаты" in p["text"]][-1]
    assert "120 USD" in push["text"] and "Останется к получению: <b>180 USD</b>" in push["text"]
    p1 = e2e.rows("SELECT id FROM payments")[0]["id"]
    assert push["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"pay_ok:{p1}"

    # 2) Босс подтверждает в «Долгах».
    _money(boss, "debts")
    boss.click(f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]')
    boss.wait_for_selector(".debt-awaiting", state="detached")
    _idle(boss)
    assert e2e.rows("SELECT amount_cents, status, confirmed_at IS NOT NULL AS c FROM payments") == [
        {"amount_cents": 12000, "status": "confirmed", "c": 1}]
    assert any(p["uid"] == e2e.ids["mgr"] and "Платёж принят" in p["text"] for p in e2e.pushes)
    card = boss.locator('.debt-card[data-status="partial"]')
    assert _norm("Оплачено: 120 USD") in _norm(card.text_content())
    assert _norm("Остаток: 180 USD") in _norm(card.text_content())
    assert "120USD" in _norm(_text(boss, ".money-received .money-value"))
    debt = _api(boss, "/api/debts")["body"]["debts"][0]
    assert (debt["total"], debt["confirmed"], debt["pending"], debt["remaining"], debt["state"]) == (
        300.0, 120.0, 0.0, 180.0, "partial")

    # 3) Ещё 100 — видно разложение «подтверждено / ждёт / останется».
    _redraw_debts(mgr)
    assert "ост.180USD" in _norm(_text(mgr, f'.btn-pay-debt[data-id="{oid}"]'))
    pay_form(mgr, f'.btn-pay-debt[data-id="{oid}"]', [("bank", "100")])
    mgr.wait_for_function("() => document.querySelector('.debt-awaiting')")
    _idle(mgr)
    card = _norm(_text(mgr, ".debt-awaiting"))
    assert "Ужеподтверждено:120USD" in card
    assert _norm("Оплата 100 USD ждёт подтверждения · после подтверждения долг: 80 USD") in card
    _redraw_debts(boss)
    boss.click(f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]')
    boss.wait_for_selector(".debt-awaiting", state="detached")
    _idle(boss)

    # 4) Остаток — «Внести ещё оплату» на карточке ждущей оплаты: форма
    # предзаполнена ровно 80, не больше.
    _redraw_debts(mgr)
    pay_form(mgr, f'.btn-pay-debt[data-id="{oid}"]', [("card", "80")], submit=False)
    assert mgr.locator(".c-overlay #ms-submit").is_enabled()
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".toast:has-text('Оплата 80 USD записана')")
    assert e2e.rows("SELECT amount_cents FROM payments ORDER BY id DESC LIMIT 1")[0]["amount_cents"] == 8000
    _redraw_debts(boss)
    boss.click(f'.debt-awaiting .btn-confirm-pay[data-id="{oid}"]')
    boss.wait_for_selector(".debt-awaiting", state="detached")
    _idle(boss)

    order = e2e.rows("SELECT paid_confirmed_at, paid_confirmed_by FROM orders WHERE id = ?", (oid,))[0]
    assert order["paid_confirmed_at"] and order["paid_confirmed_by"] == e2e.ids["boss"]
    assert e2e.rows("SELECT SUM(amount_cents) AS s FROM payments WHERE status = 'confirmed'")[0]["s"] == 30000
    assert boss.locator(".debt-card").count() == 0
    assert "Открытых долгов нет" in boss.locator("#content").inner_text()
    assert "300USD" in _norm(_text(boss, ".money-received .money-value"))

    # 5) Отчёт: ровно сумма заказа, три платежа, лента по заказу.
    tab(boss, "report")
    _idle(boss)
    assert _norm(_text(boss, ".money-total")) == "≈300USD"
    assert [_norm(t) for t in _texts(boss, "#money-body .stock-row .stock-name")] == [
        "USD·300", "Наличные(сдачи)·0USD"]
    assert "3платежа" in _norm(_text(boss, "#money-body .stock-row .stock-folder"))
    ribbon = [_norm(t) for t in _texts(boss, "#money-body .c-row:has(.card-row-icon)")]
    amounts = sorted(re.match(r"Платёж·(\d+USD)", r).group(1) for r in ribbon if r.startswith("Платёж"))
    assert amounts == ["100USD", "120USD", "80USD"], ribbon
    assert all(f"заказ#{oid}" in r and "принят" in r for r in ribbon if r.startswith("Платёж"))
    s = _api(boss, "/api/money/summary", {"period": "month"})["body"]
    assert s["payments"] == [{"currency": "USD", "total_cents": 30000, "count": 3}] and s["base_total"] == 300.0


# ─── Расхождения в деньгах: сдача ↔ отметка оплаты ───────────────────────────


def test_mark_full_remaining_after_partial_deposit_charges_only_rest(open_app, e2e):
    oid = _order(e2e, price=100.0, ship=True)
    _deposit(e2e, 60.0, confirm=True)

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "debts")
    btn = f'.btn-pay-debt[data-id="{oid}"]'
    assert "ост.40USD" in _norm(_text(mgr, btn)), "экран честно показывает остаток 40"
    mgr.click(btn)
    mgr.wait_for_selector(".c-overlay .pay-part")
    assert mgr.input_value(".c-overlay .pay-part-amount") == "40", "форма предзаполнена остатком"
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".toast:has-text('записана')")
    assert e2e.rows("SELECT amount_cents FROM payments WHERE order_id = ?", (oid,)) == [{"amount_cents": 4000}]


def test_deposit_does_not_cover_amount_already_marked_paid(open_app, e2e):
    from services.database import mark_order_paid

    oid = _order(e2e, qty=2, price=100.0, ship=True)
    ok, _pid = e2e.run(mark_order_paid(oid, e2e.ids["mgr"], "Manager", amount=150.0))
    assert ok

    mgr = open_app(e2e.ids["mgr"])
    _money(mgr, "ops")
    mgr.fill("#dep-amount", "200")
    mgr.click("#dep-create")
    _alert(mgr, "отправлена на подтверждение")
    alloc = e2e.rows("SELECT amount_allocated_cents FROM cash_deposit_orders WHERE order_id = ?", (oid,))
    # Под ожидающую оплату 150 уже «занято»: на сдачу остаётся не больше 50.
    assert sum(a["amount_allocated_cents"] for a in alloc) <= 5000, alloc
