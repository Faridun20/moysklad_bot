"""E2E первой волны: WebApp в настоящем Chromium против живого сервера и БД.

Сценарии выбраны по цене ошибки: денежный контур (заказ → заявка → одобрение
→ списание), и то, что только что чинили по аудиту — экранирование в экране
босса и вкладки, которые вели к 403. jsdom-smoke это тоже покрывает, но с
заглушкой `api()`; здесь ответы приходят от настоящего сервера.
"""

from __future__ import annotations

from tests.e2e.conftest import go


def _nav_screens(page) -> list[str]:
    return page.eval_on_selector_all("#bottom-nav .nav-item", "els => els.map(e => e.dataset.screen)")


# ─── Навигация по ролям ──────────────────────────────────────────────────────


def test_nav_sections_follow_role(open_app, e2e):
    """Пять разделов у босса; у кладовщика нет «Склада» — ручки ему не отвечают."""
    boss = open_app(e2e.ids["boss"])
    assert _nav_screens(boss) == ["today", "sales", "stock", "money", "clients"]

    keeper = open_app(e2e.ids["keeper"])
    screens = _nav_screens(keeper)
    assert "stock" not in screens
    assert {"today", "sales", "money"} <= set(screens)


def test_guest_sees_no_access_screen(open_app, e2e):
    """Новый пользователь — guest с нулевыми правами: навигации нет, есть отказ."""
    page = open_app(777_000)  # роли в user_roles нет → guest
    assert page.locator("#bottom-nav .nav-item").count() == 0
    assert "доступ" in page.locator("#content").inner_text().lower()


# ─── Денежный контур: заказ → заявка → одобрение → списание ──────────────────


def test_manager_order_to_boss_approval_moves_stock(open_app, e2e):
    """Полный путь продажи через два браузера и одну базу.

    Менеджер собирает заказ в редакторе (клиент, товар, количество, цена) и
    отправляет заявку; босс видит её в «Заявках» и одобряет. Проверяем не
    экран, а последствия в БД: заявка approved, у заказа есть накладная,
    остаток на складе уменьшился ровно на количество из заявки.
    """
    ids = e2e.ids
    mgr = open_app(ids["mgr"])
    go(mgr, "sales")
    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#choose-agent")

    # Клиент — из справочника контрагентов (/api/agents → counterparties).
    mgr.click("#choose-agent")
    mgr.wait_for_selector(".agent-row")
    mgr.click('.agent-row:has-text("Ромашка")')
    mgr.wait_for_selector("#change-agent")  # редактор перерисован с выбранным клиентом

    # Товар — из каталога остатков (/api/stock), количество/цена — в диалоге,
    # подтверждение — MainButton Telegram.
    mgr.click("#btn-add-product")
    mgr.wait_for_selector(".prod-row")
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")
    mgr.fill("#qty-input", "2")
    mgr.fill("#price-input", "100")
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    mgr.click("#btn-submit")
    mgr.wait_for_function("() => window.__tgAlerts.some(a => a.includes('отправлена'))")

    reqs = e2e.rows("SELECT id, status, order_id FROM shipment_requests")
    assert len(reqs) == 1 and reqs[0]["status"] == "pending"
    order_id = reqs[0]["order_id"]
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"] == "pending"
    # Пуш руководству о новой заявке ушёл (граница перехвачена).
    assert any("заявка" in p["text"].lower() for p in e2e.pushes)

    boss = open_app(ids["boss"])
    go(boss, "sales")
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-approve")
    card = boss.locator(".order-card").first
    assert "Ромашка" in card.inner_text()
    assert "Кабель" in card.inner_text()
    boss.click(".btn-approve")
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('одобрена'))")

    assert e2e.rows("SELECT status FROM shipment_requests")[0]["status"] == "approved"
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (order_id,))[0]["status"] == "approved"
    ship = e2e.rows("SELECT invoice_id, failed_at FROM order_shipment WHERE order_id = ?", (order_id,))
    assert ship and ship[0]["invoice_id"] and ship[0]["failed_at"] is None
    inv = e2e.rows("SELECT type, total_amount_cents FROM invoices WHERE id = ?", (ship[0]["invoice_id"],))[0]
    assert inv["type"] == "outgoing" and inv["total_amount_cents"] == 20000
    stock = e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (ids["product"],))[0]["quantity"]
    assert stock == 18, "20 на приходе минус 2 в заявке"


# ─── Аудит п.1 через настоящий браузер ───────────────────────────────────────


def test_xss_in_request_does_not_run_in_boss_session(open_app, e2e):
    """Менеджер назвал клиента и позицию `<img onerror>`; у босса это текст.

    jsdom-тест проверяет рендер с заглушкой api(); здесь строка проходит весь
    путь — БД → /api/orders/requests → Chromium — и обязана остаться текстом.
    """
    from services.order_workflow import submit_order

    ids = e2e.ids
    db = e2e.db
    payload = '<img src=x onerror="window.__pwned=1">'
    oid = db.create_order(ids["mgr"], "Mgr", "")
    cp = e2e.rows("SELECT id FROM counterparties")[0]["id"]
    db.update_order_agent(oid, str(cp), "ООО " + payload)
    db.add_order_item(oid, "Труба " + payload, "", 1, "шт", 10.0, product_id=ids["product"])
    res = e2e.run(submit_order(oid, ids["mgr"], "Mgr", payment_type="paid", due_date=None))
    assert res["ok"], res

    boss = open_app(ids["boss"])
    go(boss, "sales")
    boss.click("#show-requests")
    boss.wait_for_selector(".order-card")

    assert boss.evaluate("window.__pwned") is None
    assert boss.locator(".order-card img").count() == 0
    text = boss.locator(".order-card").first.inner_text()
    assert "<img src=x" in text, "текст экранирован, а не вырезан"


# ─── Вкладки не ведут к 403 ──────────────────────────────────────────────────


def test_manager_clients_section_opens_leads_without_error(open_app, e2e):
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "clients")
    mgr.wait_for_function("() => !document.querySelector('#clients-body .sk-card')")
    body = mgr.locator("#content").inner_text()
    assert "Нет доступа" not in body and "Ошибка" not in body
    assert mgr.locator('[data-sect="funnel"]').count() == 0


def test_keeper_money_section_has_no_cash_tab(open_app, e2e):
    keeper = open_app(e2e.ids["keeper"])
    go(keeper, "money")
    keeper.wait_for_function("() => !document.querySelector('#content .sk-card')")
    assert keeper.locator('[data-sect="ops"]').count() == 0
    body = keeper.locator("#content").inner_text()
    assert "Нет доступа" not in body and "Ошибка" not in body


# ─── Аудит п.5: расход только руководству ────────────────────────────────────


def _open_invoice_form(page):
    go(page, "stock")
    page.click('[data-wh-go="whinvoices"]')
    page.wait_for_selector("#wh-new")
    page.click("#wh-new")
    page.wait_for_selector("#wh-cp")


def test_manager_invoice_form_has_no_outgoing(open_app, e2e):
    mgr = open_app(e2e.ids["mgr"])
    _open_invoice_form(mgr)
    assert mgr.locator('[data-whtype="outgoing"]').count() == 0
    # inner_text отдаёт текст с учётом CSS text-transform (заголовки — капсом).
    assert "приход на склад" in mgr.locator("#content").inner_text().lower()


def test_boss_invoice_form_has_both_types(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    _open_invoice_form(boss)
    assert boss.locator('[data-whtype="outgoing"]').count() == 1
    assert boss.locator('[data-whtype="incoming"]').count() == 1


# ─── Гонка рендеров: ушёл с экрана раньше, чем он загрузился ─────────────────


def test_navigating_away_during_load_does_not_bring_old_screen_back(open_app, e2e, monkeypatch):
    """«Сегодня» грузится полторы секунды; человек уже нажал «Деньги».

    Старый рендер, закончивший после перехода, дописывал свой экран поверх
    нового: нажал «Деньги» — увидел главную. Ловилось только под нагрузкой
    (E2E-прогон целиком), здесь задержка сделана явной.
    """
    import asyncio

    from services import async_db, database

    orig = database.get_user_orders

    async def slow(*a, **kw):
        await asyncio.sleep(1.5)
        return await orig(*a, **kw)

    monkeypatch.setattr(async_db, "get_user_orders", slow, raising=False)

    boss = open_app(e2e.ids["boss"])  # стартует на «Сегодня», /api/home ещё в полёте
    go(boss, "money")
    boss.wait_for_selector('.seg-item[data-sect="confirm"]')
    boss.wait_for_timeout(2500)  # даём старому рендеру шанс «вернуться»
    assert boss.locator('.seg-item[data-sect="confirm"]').count() == 1
    assert boss.locator("#content .hero, #content .greeting").count() == 0
    assert boss.evaluate("document.querySelector('#bottom-nav .nav-item.active')?.dataset.screen") == "money"


def test_switching_tab_during_orders_load_keeps_report(open_app, e2e, monkeypatch):
    """Список заказов ещё грузится, а человек уже открыл «Отчёт» — отчёт остаётся."""
    import asyncio

    from services import async_db, database

    orig = database.get_all_orders

    async def slow(*a, **kw):
        await asyncio.sleep(1.5)
        return await orig(*a, **kw)

    monkeypatch.setattr(async_db, "get_all_orders", slow, raising=False)

    boss = open_app(e2e.ids["boss"])
    go(boss, "sales")
    boss.click('.seg-item[data-sect="report"]')
    boss.wait_for_selector("[data-period]")
    boss.wait_for_timeout(2500)
    assert boss.locator("[data-period]").count() > 0, "заказы, догрузившись, затёрли отчёт"
    assert boss.locator("#btn-new-order, #show-requests").count() == 0
