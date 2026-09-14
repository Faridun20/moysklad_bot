"""E2E-покрытие разделов «Сегодня» и «Клиенты»: каждая кнопка — с последствием.

Первые две волны (`test_webapp_flows`, `test_clients_auth`, `test_sales_money`)
проверили главные пути: лид «купил», причина отказа, звонок без лида, лимит из
карточки, курс с экрана лимитов, очередь дел → долги/заявки. Здесь — всё
остальное, что на этих экранах нажимается: фильтры списка лидов, удаление и
привязка звонков, «Без причины» и «Вернуть в работу», привязка контрагента по
телефону и заведение нового, раскрытие заказов и отгрузок в карточке клиента,
отмена и проверка ввода лимита, перевёрнутый курс сума, поиск по ролям, строки
очереди дел для каждой роли и то, что каждой роли видно ровно положенное.

Проверяется не наличие кнопки, а последствие: строка в БД, перерисованный экран,
тост, текст диалога Telegram. «Назад» у Telegram в заглушке — noop, поэтому
его обработчик вызывается напрямую (`_backHandler` — глобальная привязка app.js).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.e2e.conftest import alerts, go, seed_order, settled, sheet_fill, tab

MGR2 = 201  # второй менеджер — для проверок «видит только своё»


# ─── Хелперы ─────────────────────────────────────────────────────────────────


def _stamp(**delta) -> str:
    """Отметка времени в кадре `now_str()` (локальное время процесса сервера)."""
    return (datetime.now() - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def _add_manager2(e2e) -> int:
    e2e.db.set_role(MGR2, "mgr2_user", "Manager2", "manager")
    return MGR2


def _lead(e2e, tg: int, name: str | None, *, manager: int | None = None,
          inbound_at: str | None = None, reply_at: str | None = None,
          outbound_first: bool = False, username: str | None = "client") -> int:
    """Лид через наблюдатель переписок (без текста — его и не хранят)."""
    from services import leads

    mid = manager or e2e.ids["mgr"]
    r = e2e.run(leads.record_message(
        tg_user_id=tg, manager_id=mid, inbound=not outbound_first,
        username=username, display_name=name, at=inbound_at,
    ))
    if reply_at:
        e2e.run(leads.record_message(
            tg_user_id=tg, manager_id=mid, inbound=False, at=reply_at,
        ))
    return int(r["lead_id"])


def _set_lead_status(e2e, lead_id: int, status: str) -> None:
    from services import leads

    res = e2e.run(leads.set_status(lead_id, status, user_id=e2e.ids["boss"]))
    assert res["ok"], res


def _order_for(e2e, cp_id: int, cp_name: str, *, qty: float, price: float,
               payment_type: str = "credit", due_date: str | None = "2030-01-15") -> int:
    """Одобренный заказ менеджера на указанного контрагента (как seed_order)."""
    from services.order_workflow import approve_shipment_request, submit_order

    db, ids = e2e.db, e2e.ids
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, str(cp_id), cp_name)
    db.add_order_item(oid, "Кабель ВВГ 3x2.5", "", qty, "м", price, product_id=ids["product"])
    res = e2e.run(submit_order(oid, ids["mgr"], "Manager", payment_type=payment_type, due_date=due_date))
    assert res.get("ok"), res
    ap = e2e.run(approve_shipment_request(res["req_id"], ids["boss"], "Boss", e2e.bot))
    assert ap.get("ok"), ap
    return oid


def _text(page, selector: str = "#content") -> str:
    """inner_text с обычными пробелами: ru-RU разделяет разряды неразрывным."""
    return page.inner_text(selector).replace("\xa0", " ").replace(" ", " ")


def _nav(page) -> list[str]:
    return page.eval_on_selector_all("#bottom-nav .nav-item", "els => els.map(e => e.dataset.screen)")


def _sect_tabs(page) -> list[str]:
    return page.eval_on_selector_all(".seg-item[data-sect]", "els => els.map(e => e.dataset.sect)")


def _active_screen(page) -> str | None:
    return page.evaluate("document.querySelector('#bottom-nav .nav-item.active')?.dataset.screen")


def _wait_screen(page, screen: str, sect: str | None = None) -> None:
    page.wait_for_function(
        "(s) => document.querySelector('#bottom-nav .nav-item.active')?.dataset.screen === s", arg=screen
    )
    if sect:
        page.wait_for_function(
            "(k) => document.querySelector('.seg-item.active[data-sect]')?.dataset.sect === k", arg=sect
        )


def _back(page) -> None:
    """Нативная «Назад» Telegram: в заглушке onClick — noop, жмём обработчик сами."""
    assert page.evaluate("typeof _backHandler === 'function'"), "кнопка «Назад» не показана"
    page.evaluate("_backHandler()")


def _lead_ids(page) -> set[int]:
    return set(page.eval_on_selector_all("#clients-body [data-lead]", "els => els.map(e => +e.dataset.lead)"))


def _wait_alert(page, needle: str) -> None:
    page.wait_for_function("(n) => window.__tgAlerts.some(a => a.includes(n))", arg=needle)


def _open_home(page) -> None:
    """«Сегодня» дорисован: очередь дел на месте (у всех ролей)."""
    page.wait_for_selector("#content .section-label:has-text('Требует вас')")
    settled(page)


def _open_lead_card(page, lead_id: int) -> None:
    go(page, "clients")
    if page.locator('.seg-item[data-sect="list"]').count():
        tab(page, "list")
    page.wait_for_selector(f'#clients-body [data-lead="{lead_id}"]')
    page.click(f'#clients-body [data-lead="{lead_id}"]')
    page.wait_for_selector("#lead-agent")


# ─── «Сегодня»: что видит каждая роль ────────────────────────────────────────


def test_role_badge_hero_and_build_line_per_role(open_app, e2e):
    """Бейдж роли, hero выручки, строка сборки и раздел «Клиенты» — по матрице ролей.

    Пустая база: очередь у всех «Всё разобрано» — компактной строкой, а не
    экраном ошибки (у кладовщика и бухгалтера /api/home не отвечает).
    """
    expect = {
        #          бейдж           hero   сборка «Клиенты»
        "boss":   ("Руководитель", True, True, True),
        "admin":  ("Админ", True, True, True),
        "mgr":    ("Менеджер", True, False, True),
        "keeper": ("Кладовщик", False, False, False),
        "book":   ("Бухгалтер", False, False, False),
    }
    for who, (badge, hero, build, clients) in expect.items():
        page = open_app(e2e.ids[who])
        assert _active_screen(page) == "today", who
        _open_home(page)
        assert page.inner_text("#role-badge") == badge, who
        assert page.locator("#content .hero").count() == (1 if hero else 0), who
        assert page.locator("#content .build-version").count() == (1 if build else 0), who
        if build:
            assert page.inner_text("#content .build-version").startswith("сборка "), who
        assert ("clients" in _nav(page)) is clients, who
        assert page.locator("#content .queue-empty:has-text('Всё разобрано')").count() == 1, who
        assert page.locator("[data-queue]").count() == 0, who
        text = page.inner_text("#content")
        assert "Нет доступа" not in text and "Не удалось загрузить" not in text, who
        if not hero:
            # Без сводки нет и строки курса — она живёт под hero.
            assert page.locator("#home-fx").count() == 0, who


def test_boss_hero_counts_company_shipments_and_leaderboard(open_app, e2e):
    seed_order(e2e)  # отгрузка 2 × 100 на «Ромашку», одобрена сегодня
    boss = open_app(e2e.ids["boss"])
    boss.wait_for_selector("#content .hero")
    assert boss.inner_text(".hero-label") == "Выручка компании сегодня"
    assert _text(boss, ".hero-value").startswith("200")
    assert _text(boss, ".hero-delta") == "1 отгрузка · 1 клиент"
    # Лидерборд — только руководству; свой заказ у босса нет → «Мои заказы» нет.
    boss.wait_for_selector(".section-label:has-text('Топ сотрудники')")
    assert "Manager" in boss.inner_text("#content")
    assert boss.locator(".section-label:has-text('Мои заказы')").count() == 0


def test_manager_hero_is_personal_and_recent_order_opens_sales(open_app, e2e):
    seeded = seed_order(e2e)
    oid = seeded["order_id"]
    mgr = open_app(e2e.ids["mgr"])
    mgr.wait_for_selector("#content .hero")
    assert mgr.inner_text(".hero-label") == "Моя выручка сегодня"
    assert _text(mgr, ".hero-value").startswith("200")
    assert mgr.locator(".section-label:has-text('Топ сотрудники')").count() == 0
    # Свод «Мои заказы»: одобрен один, черновиков и ожидающих нет.
    stats = mgr.eval_on_selector_all(
        ".stat", "els => els.map(e => [e.querySelector('.stat-label').textContent.trim(),"
                 " e.querySelector('.stat-value').textContent.trim()])"
    )
    assert dict(stats) == {"Черновики": "0", "Ожидают": "0", "Одобрено": "1"}
    row = mgr.locator(f'[data-order-id="{oid}"]')
    assert "Ромашка" in row.inner_text() and "Одобрено" in row.inner_text()
    row.click()
    _wait_screen(mgr, "sales")
    mgr.wait_for_selector(f'.order-card[data-id="{oid}"]')


# ─── «Сегодня»: очередь дел ──────────────────────────────────────────────────


def _arrived_unchecked_container(e2e, number: str = "MSCU1234567") -> None:
    """Прибывший контейнер с непосчитанной позицией — пункт «Контейнеры не сверены»."""
    from services import containers

    c = e2e.run(containers.create_container(number=number, created_by=e2e.ids["boss"]))
    assert c["ok"], c
    assert e2e.run(containers.add_item(c["container_id"], name="Кабель", expected_qty=5))["ok"]
    assert e2e.run(containers.mark_arrived(c["container_id"], user_id=e2e.ids["boss"]))["ok"]


def test_queue_is_sorted_by_urgency(open_app, e2e):
    """Просроченный долг (crit) выше заявки (warn), несверенный контейнер (info) — последним."""
    overdue = seed_order(e2e)
    e2e.exec("UPDATE orders SET due_date = ? WHERE id = ?", ("2020-01-01", overdue["order_id"]))
    seed_order(e2e, payment_type="paid", due_date=None, approve=False)  # заявка ждёт
    _arrived_unchecked_container(e2e)

    boss = open_app(e2e.ids["boss"])
    boss.wait_for_selector('[data-queue="stock:containers"]')
    order = boss.eval_on_selector_all("[data-queue]", "els => els.map(e => e.dataset.queue)")
    assert order == ["money:debts", "requests", "stock:containers"], order
    counts = boss.eval_on_selector_all("[data-queue] .queue-count", "els => els.map(e => +e.textContent)")
    assert counts == [1, 1, 1]
    assert "требует вас · 3" in _text(boss).lower()  # подписи разделов — капсом (CSS)
    assert "Контейнеры не сверены" in boss.inner_text('[data-queue="stock:containers"]')


def test_queue_container_row_opens_containers_tab(open_app, e2e):
    """wireWorkQueue ставит вкладку и зовёт showScreen('stock'), а showScreen
    разворачивал 'stock' как старый адрес → 'stock:catalog' (LEGACY_SCREENS) и
    затирал выбранную вкладку. Человек видел каталог и искал контейнер руками —
    ровно то, от чего очередь и спасала.
    """
    _arrived_unchecked_container(e2e)
    boss = open_app(e2e.ids["boss"])
    boss.wait_for_selector('[data-queue="stock:containers"]')
    boss.click('[data-queue="stock:containers"]')
    _wait_screen(boss, "stock")
    boss.wait_for_selector(".seg-item.active[data-sect]")
    settled(boss)
    assert boss.evaluate("document.querySelector('.seg-item.active[data-sect]').dataset.sect") == "containers"
    boss.wait_for_selector("#content :text('MSCU1234567')", timeout=3000)


def test_queue_payments_row_opens_confirm_tab_for_boss(open_app, e2e):
    seeded = seed_order(e2e, payment_type="paid", due_date=None)  # платёж ждёт подтверждения
    boss = open_app(e2e.ids["boss"])
    row = boss.locator('[data-queue="money:confirm"]:has-text("Платежи на подтверждение")')
    row.wait_for()
    assert row.locator(".queue-count").inner_text() == "1"
    row.click()
    _wait_screen(boss, "money", "confirm")
    boss.wait_for_selector(f'.pay-confirm[data-id="{seeded["order_id"]}"]')


def test_bookkeeper_and_keeper_see_only_their_confirmations(open_app, e2e):
    """Бухгалтеру — сдачи, кладовщику — возвраты; строка ведёт туда, где закрывается."""
    from services.database import confirm_all_pending_payments_for_order, create_cash_deposit, create_return

    seed_order(e2e, qty=1, price=30.0)
    assert e2e.run(create_cash_deposit(e2e.ids["mgr"], 30.0)).get("ok")
    paid = seed_order(e2e, payment_type="paid", due_date=None)
    e2e.run(confirm_all_pending_payments_for_order(paid["order_id"], e2e.ids["boss"], "Boss"))
    item = e2e.rows("SELECT id FROM order_items WHERE order_id = ?", (paid["order_id"],))[0]["id"]
    r = e2e.run(create_return(paid["order_id"], "full", "Брак", [(item, 2, 200.0)],
                              "debt_reduction", e2e.ids["mgr"]))
    assert r.get("ok"), r

    book = open_app(e2e.ids["book"])
    _open_home(book)
    rows = book.eval_on_selector_all("[data-queue]", "els => els.map(e => e.textContent)")
    assert len(rows) == 1 and "Сдачи наличных" in rows[0], rows
    book.click('[data-queue="money:confirm"]')
    _wait_screen(book, "money")
    book.wait_for_selector(".dep-confirm")
    assert book.locator(".ret-goods, .pay-confirm").count() == 0

    keeper = open_app(e2e.ids["keeper"])
    _open_home(keeper)
    rows = keeper.eval_on_selector_all("[data-queue]", "els => els.map(e => e.textContent)")
    assert len(rows) == 1 and "Возвраты на подтверждение" in rows[0], rows
    keeper.click('[data-queue="money:confirm"]')
    _wait_screen(keeper, "money")
    keeper.wait_for_selector(".ret-goods")


def test_queue_awaiting_clients_boss_to_funnel_manager_to_own_leads(open_app, e2e):
    """«Клиенты ждут ответа»: босс считает всех, менеджер — своих; у менеджера нет воронки."""
    _add_manager2(e2e)
    mine = _lead(e2e, 610_001, "Ждёт Мой", inbound_at=_stamp(hours=13))
    _lead(e2e, 610_002, "Ждёт Чужой", manager=MGR2, inbound_at=_stamp(hours=13))
    _lead(e2e, 610_003, "Свежий")  # в воронку «этой недели» кто-то должен попасть

    boss = open_app(e2e.ids["boss"])
    row = boss.locator('[data-queue="clients:funnel"]')
    row.wait_for()
    assert row.locator(".queue-count").inner_text() == "2"
    row.click()
    _wait_screen(boss, "clients", "funnel")
    boss.wait_for_selector(".section-label:has-text('Ждут ответа · 2')")

    mgr = open_app(e2e.ids["mgr"])
    row = mgr.locator('[data-queue="clients:funnel"]')
    row.wait_for()
    assert row.locator(".queue-count").inner_text() == "1"
    row.click()
    _wait_screen(mgr, "clients")
    mgr.wait_for_selector("#call-new")
    settled(mgr)
    # Вкладку «Воронка» (403 для менеджера) подменили «Лидами», чужой лид не виден.
    assert mgr.locator(".seg-item[data-sect]").count() == 0
    assert mine in _lead_ids(mgr) and len(_lead_ids(mgr)) == 2
    assert "Ждёт Чужой" not in mgr.inner_text("#content")
    assert "ждёт ответа" in mgr.inner_text(f'[data-lead="{mine}"]')


# ─── «Сегодня»: курс и экран курсов ──────────────────────────────────────────


def test_home_fx_for_manager_is_read_only_and_back_returns_home(open_app, e2e):
    from services.database import set_currency_rate

    set_currency_rate("UZS", 1 / 12000, 0)
    mgr = open_app(e2e.ids["mgr"])
    mgr.wait_for_selector("#home-fx [data-open-rates]")
    assert "1 USD = 12 000,00 сум" in _text(mgr, "#home-fx")
    mgr.click("#home-fx [data-open-rates]")
    mgr.wait_for_selector('[data-rate="UZS"]')
    text = _text(mgr)
    assert "Изменять курсы может админ или руководитель" in text
    assert mgr.locator(".rate-input, .rate-save").count() == 0
    assert "базовая" in mgr.inner_text('[data-rate="USD"]')
    _back(mgr)
    _wait_screen(mgr, "today")
    mgr.wait_for_selector("#content .hero")


def test_boss_saves_inverted_rate_and_zero_is_rejected(open_app, e2e):
    """Поле курса сума — «сколько сум за 1 USD»; в базу уходит обратное число."""
    from services.database import set_currency_rate

    set_currency_rate("UZS", 1 / 12000, 0)
    boss = open_app(e2e.ids["boss"])
    boss.wait_for_selector("#home-fx [data-open-rates]")
    boss.click("#home-fx [data-open-rates]")
    boss.wait_for_selector('.rate-input[data-inverted="1"]')
    assert "Сколько сум за 1 USD" in boss.inner_text(".rate-edit")

    boss.fill(".rate-input", "0")
    boss.click('.rate-save[data-code="UZS"]')
    _wait_alert(boss, "Курс должен быть положительным числом")
    rate = e2e.rows("SELECT rate_to_base FROM currency_rates WHERE currency_code = 'UZS'")[0]["rate_to_base"]
    assert rate == pytest.approx(1 / 12000)

    boss.fill(".rate-input", "12500")
    boss.click('.rate-save[data-code="UZS"]')
    _wait_alert(boss, "✅ Курс UZS обновлён")
    rate = e2e.rows("SELECT rate_to_base, updated_by FROM currency_rates WHERE currency_code = 'UZS'")[0]
    assert rate["rate_to_base"] == pytest.approx(1 / 12500)
    assert rate["updated_by"] == e2e.ids["boss"]
    boss.wait_for_function("() => document.querySelector('.rate-input')?.value === '12500'")

    _back(boss)
    _wait_screen(boss, "today")
    boss.wait_for_selector("#home-fx [data-open-rates]")
    assert "1 USD = 12 500,00 сум" in _text(boss, "#home-fx")


# ─── «Сегодня»: поиск в шапке ────────────────────────────────────────────────


def test_boss_search_agent_opens_card_and_back_leads_to_limits(open_app, e2e):
    seed_order(e2e)
    boss = open_app(e2e.ids["boss"])
    boss.click("#search-btn")
    boss.fill("#search-input", "9012345")  # по телефону, а не по названию
    boss.wait_for_selector(".search-item[data-agent]")
    boss.click(".search-item[data-agent]")
    boss.wait_for_selector(".editor-title:has-text('ООО Ромашка')")
    assert "+998901234567" in boss.inner_text(".agent-phone")
    _back(boss)
    _wait_screen(boss, "clients", "limits")
    boss.wait_for_selector("#open-rates")


def test_search_hints_and_order_payment_items_navigate(open_app, e2e):
    seeded = seed_order(e2e, payment_type="paid", due_date=None)  # есть и заказ, и платёж
    boss = open_app(e2e.ids["boss"])
    boss.click("#search-btn")
    boss.fill("#search-input", "Р")
    boss.wait_for_selector("#search-results .empty-hint:has-text('минимум 2 символа')")
    boss.fill("#search-input", "zzqq")
    boss.wait_for_selector("#search-results .empty-hint:has-text('Ничего не найдено')")

    boss.fill("#search-input", "Manager")
    boss.wait_for_selector(".search-group-title:has-text('Платежи')")
    order_item = boss.locator(f".search-item:has-text('#{seeded['order_id']} · ООО Ромашка')")
    assert order_item.count() == 1
    order_item.click()
    _wait_screen(boss, "sales")
    boss.wait_for_selector(f'.order-card[data-id="{seeded["order_id"]}"]')

    boss.click("#search-btn")
    boss.fill("#search-input", "Manager")
    boss.wait_for_selector(".search-group-title:has-text('Платежи')")
    pay = boss.locator(".search-group-title:has-text('Платежи') + .search-item")
    assert "200" in pay.inner_text() and "USD" in pay.inner_text()
    pay.click()
    _wait_screen(boss, "money")


def test_manager_search_sees_only_own_orders_and_no_client_card(open_app, e2e):
    _add_manager2(e2e)
    mine = seed_order(e2e, approve=False, payment_type="paid", due_date=None)["order_id"]
    cp = e2e.rows("SELECT id FROM counterparties")[0]["id"]
    other = e2e.db.create_order(MGR2, "Manager2", "")
    e2e.db.update_order_agent(other, str(cp), "ООО Ромашка")

    mgr = open_app(e2e.ids["mgr"])
    mgr.click("#search-btn")
    mgr.fill("#search-input", "Ромашка")
    mgr.wait_for_selector(".search-group-title:has-text('Клиенты')")
    text = mgr.inner_text("#search-results")
    assert f"#{mine}" in text and f"#{other}" not in text
    # Карточка контрагента — только руководству: строка клиента не кликабельна.
    assert mgr.locator(".search-item[data-agent]").count() == 0
    assert mgr.locator(".search-item:has-text('+998901234567')").count() == 1

    boss = open_app(e2e.ids["boss"])
    boss.click("#search-btn")
    boss.fill("#search-input", "Ромашка")
    boss.wait_for_selector(".search-item[data-agent]")
    text = boss.inner_text("#search-results")
    assert f"#{mine}" in text and f"#{other}" in text


def test_keeper_search_button_is_not_a_dead_end(open_app, e2e):
    """/api/search отвечает только admin/boss/manager, а лупа в шапке была у всех.

    Правило приложения — «таб, который гарантированно ответит 403, это дверь,
    которая не открывается»: либо кнопки нет, либо поиск работает.
    """
    keeper = open_app(e2e.ids["keeper"])
    _open_home(keeper)
    if not keeper.locator("#search-btn").is_visible():
        return
    keeper.click("#search-btn")
    keeper.fill("#search-input", "Ромашка")
    keeper.wait_for_selector("#search-results .search-item, #search-results .empty-hint:has-text('Ничего'),"
                             " #search-results .error-card")
    assert keeper.locator("#search-results .error-card").count() == 0, keeper.inner_text("#search-results")


# ─── «Клиенты»: вкладки по ролям ─────────────────────────────────────────────


def test_clients_tabs_follow_role(open_app, e2e):
    for who in ("boss", "admin"):
        page = open_app(e2e.ids[who])
        go(page, "clients")
        page.wait_for_selector('.seg-item[data-sect="funnel"]')
        assert _sect_tabs(page) == ["funnel", "list", "limits", "channel"], who
        assert page.inner_text("#greeting") == "Клиенты · Воронка", who
        settled(page)
        assert "Обращений пока нет" in page.inner_text("#clients-body"), who
        tab(page, "list")
        page.wait_for_selector("#call-new")
        assert page.inner_text("#greeting") == "Клиенты · Лиды"
        tab(page, "limits")
        page.wait_for_selector("#clients-body :text('Пока нет клиентов')")
        tab(page, "channel")
        page.wait_for_selector("#clients-body :text('В канал ещё ничего не уходило')")
        assert "Канал не настроен" in page.inner_text("#clients-body")
        assert "Нет доступа" not in page.inner_text("#content"), who

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "clients")
    mgr.wait_for_selector("#call-new")
    settled(mgr)
    # Одна вкладка — переключатель не рисуется, воронки/лимитов/канала нет.
    assert mgr.locator(".seg-item[data-sect]").count() == 0
    assert mgr.inner_text("#greeting") == "Клиенты"
    assert mgr.locator("#open-rates").count() == 0
    # Старый адрес «Лимиты» (из бота/закладок) у менеджера ведёт в «Лиды», а не в 403.
    mgr.evaluate("showScreen('limits')")
    mgr.wait_for_selector("#call-new")
    settled(mgr)
    assert "Нет доступа" not in mgr.inner_text("#content")
    assert mgr.locator("#open-rates, [data-sect='limits']").count() == 0

    for who in ("keeper", "book"):
        page = open_app(e2e.ids[who])
        assert "clients" not in _nav(page), who


# ─── «Клиенты» → «Воронка» ───────────────────────────────────────────────────


def test_funnel_shows_first_touch_speed_awaiting_and_managers(open_app, e2e):
    _add_manager2(e2e)
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def ago(minutes: int) -> str:  # не раньше полуночи: воронка «этой недели»
        return max(datetime.now() - timedelta(minutes=minutes), today0).strftime("%Y-%m-%d %H:%M:%S")

    _lead(e2e, 620_001, "Сам Написал", inbound_at=ago(3), reply_at=ago(1))
    _lead(e2e, 620_002, "Мы Первые", outbound_first=True)
    won = _lead(e2e, 620_003, "Купивший", manager=MGR2)
    _set_lead_status(e2e, won, "won")
    waiting = _lead(e2e, 620_004, "Висит Давно", inbound_at=_stamp(hours=13))

    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    boss.wait_for_selector(".section-label:has-text('Воронка обращений')")
    text = _text(boss, "#clients-body").lower()  # подписи разделов — капсом (CSS)
    assert "клиент написал сам" in text and "написали мы первыми" in text
    assert "скорость ответа" in text and "обычно отвечаем за" in text
    assert "по менеджерам" in text and "manager2" in text
    # Второй менеджер: один клиент, купил → 100%.
    row2 = boss.locator(".c-row:has(.card-row-title:text-is('Manager2'))")
    assert row2.locator(".card-row-value").inner_text() == "100%"
    boss.wait_for_selector(".section-label:has-text('Ждут ответа · 1')")
    boss.click(f'#clients-body [data-lead="{waiting}"]')
    boss.wait_for_selector(".editor-title:has-text('Висит Давно')")
    assert "ждёт ответа" in boss.inner_text("#content")
    _back(boss)
    _wait_screen(boss, "clients", "funnel")
    boss.wait_for_selector(".section-label:has-text('Воронка обращений')")


# ─── «Клиенты» → «Лиды» ──────────────────────────────────────────────────────


def test_leads_list_filters_by_outcome_and_state(open_app, e2e):
    fresh = _lead(e2e, 630_001, "Свежий")                                  # в работе, без ответа
    waiting = _lead(e2e, 630_002, "Ждёт", inbound_at=_stamp(hours=13))     # ждёт ответа
    silent = _lead(e2e, 630_003, "Замолчал", inbound_at=_stamp(days=20), reply_at=_stamp(days=15))
    won = _lead(e2e, 630_004, "Купил", inbound_at=_stamp(minutes=5), reply_at=_stamp(minutes=4))
    lost = _lead(e2e, 630_005, "Не купил", inbound_at=_stamp(minutes=5), reply_at=_stamp(minutes=4))
    _set_lead_status(e2e, won, "won")
    _set_lead_status(e2e, lost, "lost")

    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "list")
    boss.wait_for_selector('[data-lfilter="all"].active')
    assert _lead_ids(boss) == {fresh, waiting, silent, won, lost}
    assert "✅ Купил" in boss.inner_text(f'[data-lead="{won}"]')
    assert "замолчал" in boss.inner_text(f'[data-lead="{silent}"]')

    def pick(attr: str, key: str) -> set[int]:
        boss.click(f'[{attr}="{key}"]')
        boss.wait_for_selector(f'[{attr}="{key}"].active')
        return _lead_ids(boss)

    assert pick("data-lfilter", "won") == {won}
    assert pick("data-lfilter", "lost") == {lost}
    assert pick("data-lfilter", "new") == {fresh, waiting, silent}
    assert pick("data-lfilter", "all") == {fresh, waiting, silent, won, lost}
    assert pick("data-lstate", "awaiting_reply") == {waiting}
    assert pick("data-lstate", "never_answered") == {fresh, waiting}
    assert pick("data-lstate", "silent") == {silent}
    # Два отбора складываются: «купил» и «замолчал» одновременно — никого.
    assert pick("data-lfilter", "won") == set()
    assert "По этому отбору никого" in boss.inner_text("#clients-body")
    assert pick("data-lstate", "") == {won}


def test_manager_lead_list_is_scoped_to_own_leads(open_app, e2e):
    _add_manager2(e2e)
    mine = _lead(e2e, 640_001, "Мой Клиент")
    other = _lead(e2e, 640_002, "Чужой Клиент", manager=MGR2)

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "clients")
    mgr.wait_for_selector(f'[data-lead="{mine}"]')
    assert _lead_ids(mgr) == {mine}

    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "list")
    boss.wait_for_selector(f'[data-lead="{other}"]')
    assert _lead_ids(boss) == {mine, other}


def test_unlinked_call_is_deleted(open_app, e2e):
    from services import lead_calls

    keep = e2e.run(lead_calls.add_call(manager_id=e2e.ids["mgr"], display_name="Оставить", phone="901110000"))
    drop = e2e.run(lead_calls.add_call(manager_id=e2e.ids["mgr"], display_name="Ошибся", phone="902220000"))

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "clients")
    mgr.wait_for_selector(".section-label:has-text('Звонили, но не пишут · 2')")
    mgr.click(f'[data-call-del="{drop["call_id"]}"]')
    mgr.wait_for_selector(".section-label:has-text('Звонили, но не пишут · 1')")
    assert "Ошибся" not in mgr.inner_text("#clients-body")
    assert [r["id"] for r in e2e.rows("SELECT id FROM lead_calls")] == [keep["call_id"]]


def test_unlinked_call_is_linked_to_lead(open_app, e2e):
    from services import lead_calls

    lead_id = _lead(e2e, 650_001, "Азиз Р.")
    _lead(e2e, 650_002, "Бобур")
    call = e2e.run(lead_calls.add_call(manager_id=e2e.ids["mgr"], display_name="Азиз",
                                       phone="+998 90 555-44-33", interest="Инвертор"))

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "clients")
    mgr.wait_for_selector(f'[data-call-link="{call["call_id"]}"]')
    assert "Инвертор" in mgr.inner_text("#clients-body")
    mgr.click(f'[data-call-link="{call["call_id"]}"]')
    mgr.wait_for_selector(".c-overlay .c-sheet-title:has-text('Чей это звонок')")
    assert mgr.input_value("#ms-f-search") == "Азиз"
    assert "+998 90 555-44-33" in mgr.inner_text(".c-overlay")
    # Поиск подставлен именем звонившего: в списке только тёзка.
    mgr.wait_for_selector(f'.c-overlay [data-pick-lead="{lead_id}"]')
    assert mgr.locator(".c-overlay [data-pick-lead]").count() == 1

    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:not([hidden]):has-text('Выберите клиента из списка')")
    assert e2e.rows("SELECT lead_id FROM lead_calls")[0]["lead_id"] is None

    mgr.fill("#ms-f-search", "нетакого")
    mgr.wait_for_selector(".c-overlay .loader:has-text('Не найдено')")
    mgr.fill("#ms-f-search", "азиз")
    mgr.click(f'.c-overlay [data-pick-lead="{lead_id}"]')
    mgr.wait_for_selector(f'.c-overlay [data-pick-lead="{lead_id}"].picked')
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Звонок связан')")
    assert mgr.locator(".c-overlay").count() == 0
    assert e2e.rows("SELECT lead_id FROM lead_calls")[0]["lead_id"] == lead_id
    kinds = [r["kind"] for r in e2e.rows("SELECT kind FROM lead_events WHERE lead_id = ?", (lead_id,))]
    assert "call_linked" in kinds
    # Из рабочего списка «перезвонить» звонок ушёл — теперь он в карточке клиента.
    mgr.wait_for_function("() => !document.querySelector('[data-call-link]')")
    mgr.click(f'[data-lead="{lead_id}"]')
    mgr.wait_for_selector(".section-label:has-text('Звонки')")
    card = mgr.inner_text("#content")
    assert "+998 90 555-44-33" in card and "Звонок привязан к переписке" in card


# ─── «Клиенты»: карточка лида ────────────────────────────────────────────────


def test_call_from_lead_card_is_outgoing_and_shows_on_card(open_app, e2e):
    lead_id = _lead(e2e, 660_001, "Шерзод")
    mgr = open_app(e2e.ids["mgr"])
    _open_lead_card(mgr, lead_id)
    mgr.click("#lead-call")
    mgr.wait_for_selector(".c-overlay .c-sheet-title:has-text('Звонок этому клиенту')")
    # Клиент известен — поля «Кто звонил» нет.
    assert mgr.locator("#ms-f-display_name").count() == 0
    mgr.click('.c-overlay [data-dir="out"]')
    mgr.click('.c-overlay [data-src="ads"]')
    mgr.wait_for_selector('.c-overlay [data-src="ads"].active')
    assert mgr.locator('.c-overlay [data-dir="in"].active').count() == 0
    sheet_fill(mgr, {"phone": "90 777 66 55", "interest": "Солнечные панели", "note": "перезвонить в пятницу"})
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Звонок записан')")
    call = e2e.rows("SELECT lead_id, direction, source, phone_key, interest, note, manager_id FROM lead_calls")
    assert call == [{
        "lead_id": lead_id, "direction": "out", "source": "ads", "phone_key": "907776655",
        "interest": "Солнечные панели", "note": "перезвонить в пятницу", "manager_id": e2e.ids["mgr"],
    }]
    mgr.wait_for_selector(".section-label:has-text('Звонки')")
    card = mgr.inner_text("#content")
    assert "Звонили мы · 90 777 66 55" in card and "Реклама" in card and "перезвонить в пятницу" in card
    assert mgr.locator(".card-row-title:text-is('Звонок')").count() == 1  # событие в ленте


def test_lost_without_reason_then_back_to_work_clears_reason(open_app, e2e):
    lead_id = _lead(e2e, 670_001, "Отказник")
    mgr = open_app(e2e.ids["mgr"])
    _open_lead_card(mgr, lead_id)

    # «Сохранить» без причины — подсказка, а не молчаливая отметка.
    mgr.click('[data-lead-status="lost"]')
    mgr.wait_for_selector(".c-overlay [data-reason]")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:not([hidden]):has-text('Выберите причину')")
    assert e2e.rows("SELECT status FROM leads")[0]["status"] == "new"
    # «Без причины» закрывает лид как есть.
    mgr.click('.c-overlay button:has-text("Без причины")')
    mgr.wait_for_selector(".toast:has-text('Отмечено')")
    mgr.wait_for_function("() => !document.querySelector('.c-overlay')")
    assert e2e.rows("SELECT status FROM leads")[0]["status"] == "lost"
    assert e2e.rows("SELECT COUNT(*) AS n FROM lead_lost")[0]["n"] == 0
    mgr.wait_for_selector(".card-row-title:text-is('Отмечен как не купивший')")

    # Уточнили: причина с заметкой.
    mgr.click('[data-lead-status="lost"]')
    mgr.click('.c-overlay [data-reason="price"]')
    mgr.wait_for_selector('.c-overlay [data-reason="price"][aria-pressed="true"]')
    sheet_fill(mgr, {"note": "у соседей дешевле"})
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#content .c-error:has-text('Не купил: Дорого — у соседей дешевле')")
    assert e2e.rows("SELECT reason, note FROM lead_lost") == [{"reason": "price", "note": "у соседей дешевле"}]

    # «Вернуть в работу» — причина больше не факт.
    mgr.click('[data-lead-status="new"]')
    mgr.wait_for_selector("#content .card-row-value:has-text('В работе')")
    assert mgr.locator("#content .c-error").count() == 0
    assert e2e.rows("SELECT status FROM leads")[0]["status"] == "new"
    assert e2e.rows("SELECT COUNT(*) AS n FROM lead_lost")[0]["n"] == 0


def test_link_existing_counterparty_found_by_phone(open_app, e2e):
    lead_id = _lead(e2e, 680_001, "Азиз")
    mgr = open_app(e2e.ids["mgr"])
    _open_lead_card(mgr, lead_id)
    assert "— не привязан" in mgr.inner_text("#content")
    assert mgr.inner_text("#lead-agent").strip() == "Привязать контрагента"
    mgr.click("#lead-agent")
    mgr.wait_for_selector(".c-overlay .loader:has-text('можно завести нового')")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:not([hidden]):has-text('Выберите контрагента из списка')")

    mgr.fill("#ms-f-search", "90 123")  # клиента помнят по номеру
    mgr.wait_for_selector(".c-overlay [data-agent]")
    mgr.click(".c-overlay [data-agent]:has-text('ООО Ромашка')")
    mgr.wait_for_selector(".c-overlay [data-agent].picked")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Контрагент привязан')")
    cp = e2e.rows("SELECT id FROM counterparties")[0]["id"]
    assert e2e.rows("SELECT agent_ms_id FROM leads")[0]["agent_ms_id"] == str(cp)
    assert e2e.rows("SELECT COUNT(*) AS n FROM counterparties")[0]["n"] == 1

    mgr.wait_for_function("() => /Сменить контрагента/.test(document.querySelector('#lead-agent')?.textContent)")
    card = mgr.inner_text("#content")
    assert "ООО Ромашка" in card and "+998901234567" in card and "Привязан контрагент" in card


def test_create_new_counterparty_from_lead_and_empty_name_error(open_app, e2e):
    lead_id = _lead(e2e, 690_001, "Бахтиёр")
    nameless = _lead(e2e, 690_002, None, username=None)
    mgr = open_app(e2e.ids["mgr"])
    _open_lead_card(mgr, lead_id)
    mgr.click("#lead-agent")
    mgr.wait_for_selector("#ms-f-search")
    mgr.fill("#ms-f-search", "ИП Бахтиёр Савдо")
    mgr.click('.c-overlay button:has-text("Завести нового контрагента")')  # confirm → «да»
    mgr.wait_for_selector(".toast:has-text('Контрагент заведён')")
    assert any("Завести контрагента «ИП Бахтиёр Савдо»" in a for a in alerts(mgr))
    cp = e2e.rows("SELECT id, name, telegram_id, type FROM counterparties WHERE name = ?", ("ИП Бахтиёр Савдо",))
    assert cp and cp[0]["telegram_id"] == 690_001 and cp[0]["type"] == "customer"
    assert e2e.rows("SELECT agent_ms_id FROM leads WHERE id = ?", (lead_id,))[0]["agent_ms_id"] == str(cp[0]["id"])
    mgr.wait_for_selector("#content .card-row-value:text-is('ИП Бахтиёр Савдо')")

    # Безымянный собеседник и пустое поле: завести «ничто» нельзя.
    _back(mgr)
    _wait_screen(mgr, "clients")
    mgr.wait_for_selector(f'[data-lead="{nameless}"]')
    mgr.click(f'[data-lead="{nameless}"]')
    mgr.click("#lead-agent")
    mgr.wait_for_selector(".c-overlay [data-agent]")  # пустой поиск — весь справочник
    mgr.click('.c-overlay button:has-text("Завести нового контрагента")')
    mgr.wait_for_selector("#ms-error:not([hidden]):has-text('Впишите название контрагента')")
    assert e2e.rows("SELECT COUNT(*) AS n FROM counterparties")[0]["n"] == 2
    assert e2e.rows("SELECT agent_ms_id FROM leads WHERE id = ?", (nameless,))[0]["agent_ms_id"] is None


def test_back_from_lead_card_opened_from_list_returns_to_list(open_app, e2e):
    """renderLeadCard жёстко ставил clientsTab = 'funnel'.

    Руководитель отобрал «Не купили», открыл клиента, нажал «Назад» — и
    оказывался в воронке: отбор и место в списке терялись.
    """
    lead_id = _lead(e2e, 695_001, "Из списка")
    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "list")
    boss.wait_for_selector(f'[data-lead="{lead_id}"]')
    boss.click(f'[data-lead="{lead_id}"]')
    boss.wait_for_selector("#lead-agent")
    _back(boss)
    _wait_screen(boss, "clients")
    boss.wait_for_selector(".seg-item.active[data-sect]")
    assert boss.evaluate("document.querySelector('.seg-item.active[data-sect]').dataset.sect") == "list"


# ─── «Клиенты» → «Лимиты» и карточка контрагента ─────────────────────────────


def test_limits_sorted_by_debt_with_over_limit_badge_and_rates_back(open_app, e2e):
    from services import async_db as adb

    romashka = e2e.rows("SELECT id FROM counterparties")[0]["id"]
    _order_for(e2e, romashka, "ООО Ромашка", qty=2, price=100.0)  # долг 200
    e2e.exec("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)",
             ("ООО Арслан", "customer", None, e2e.db.now_str()))
    arslan = e2e.rows("SELECT id FROM counterparties WHERE name = 'ООО Арслан'")[0]["id"]
    _order_for(e2e, arslan, "ООО Арслан", qty=3, price=100.0)     # долг 300
    e2e.run(adb.set_credit_limit(str(romashka), "ООО Ромашка", 100, set_by=e2e.ids["boss"]))
    e2e.run(adb.set_credit_limit(str(arslan), "ООО Арслан", 1000, set_by=e2e.ids["boss"]))

    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "limits")
    boss.wait_for_selector("#clients-body [data-agent]")
    assert "клиенты (2)" in _text(boss, "#clients-body").lower()
    order = boss.eval_on_selector_all("#clients-body [data-agent]", "els => els.map(e => e.dataset.agent)")
    assert order == [str(arslan), str(romashka)], "сверху — кто больше должен"
    ars = _text(boss, f'[data-agent="{arslan}"]')
    rom = _text(boss, f'[data-agent="{romashka}"]')
    assert "долг 300 USD · лимит 1 000 USD" in ars and "лимит превышен" not in ars
    assert "долг 200 USD · лимит 100 USD" in rom and "лимит превышен" in rom

    boss.click("#open-rates")
    boss.wait_for_selector(".section-label:has-text('Курсы к USD')")
    assert boss.inner_text("#greeting") == "Курсы валют"
    _back(boss)
    _wait_screen(boss, "clients", "limits")
    boss.wait_for_selector(f'#clients-body [data-agent="{arslan}"]')


def test_rates_entry_available_without_clients(open_app, e2e):
    """renderCreditLimits выходил на пустом списке ДО строки #open-rates.

    На свежей базе (заказов нет, курс сума ещё не синхронизирован) строки курса
    на «Сегодня» тоже нет — руководителю было негде задать курс вовсе.
    """
    boss = open_app(e2e.ids["boss"])
    _open_home(boss)
    assert boss.locator("#home-fx [data-open-rates]").count() == 0
    go(boss, "clients")
    tab(boss, "limits")
    boss.wait_for_selector("#clients-body .empty-state, #open-rates")
    assert boss.locator("#open-rates").count() == 1


def test_agent_card_expands_orders_shipments_and_limit_edit_guards(open_app, e2e):
    credit = seed_order(e2e)                                       # долг 200
    seed_order(e2e, payment_type="paid", due_date=None)            # платёж 200 ждёт
    cp = credit["counterparty_id"]

    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "limits")
    boss.click(f'#clients-body [data-agent="{cp}"]')
    boss.wait_for_selector(".editor-title:has-text('ООО Ромашка')")
    text = _text(boss)
    assert "+998901234567" in text
    # Неподтверждённая оплата долг не гасит; лимит по умолчанию — из настроек.
    assert "Долг по заказам бота: 400 USD · лимит 2 000 · свободно 1 600" in text
    low = text.lower()  # подписи разделов — капсом (CSS)
    assert "покупки · 2 отгрузки · 400 usd" in low
    assert "заказы в боте · 2" in low
    assert "платежи · 1" in low and "Платёж · 200 USD" in text and "ожидает" in text

    # Состав заказа уже в ответе — раскрывается и сворачивается без запроса.
    row = boss.locator(f'[data-order-open="{credit["order_id"]}"]')
    box = boss.locator(f'#agent-order-{credit["order_id"]}')
    assert box.is_hidden()
    row.click()
    box.wait_for(state="visible")
    assert "Кабель ВВГ 3x2.5" in box.inner_text() and row.get_attribute("aria-expanded") == "true"
    row.click()
    box.wait_for(state="hidden")

    # Отгрузка: состав грузится по первому тапу из /api/clients/shipment.
    ship = boss.locator("[data-shipment]").first
    sid = ship.get_attribute("data-shipment")
    ship.click()
    boss.wait_for_selector(f"#shipment-{sid} .items-row")
    items = _text(boss, f"#shipment-{sid}")
    assert "Кабель ВВГ 3x2.5" in items and "200 USD" in items
    assert ship.get_attribute("aria-expanded") == "true"
    ship.click()
    boss.locator(f"#shipment-{sid}").wait_for(state="hidden")

    # «Отмена» прячет редактор; отрицательный лимит не уходит на сервер.
    boss.click("#cl-edit")
    boss.locator("#cl-box").wait_for(state="visible")
    boss.click("#cl-cancel")
    boss.locator("#cl-box").wait_for(state="hidden")
    boss.click("#cl-edit")
    boss.fill("#cl-input", "-5")
    boss.click("#cl-save")
    _wait_alert(boss, "Лимит должен быть неотрицательным числом")
    assert e2e.rows("SELECT COUNT(*) AS n FROM credit_limits")[0]["n"] == 0
    boss.fill("#cl-input", "250")
    boss.click("#cl-save")
    boss.wait_for_selector(".toast:has-text('Лимит обновлён: 250 USD')")
    assert e2e.rows("SELECT limit_amount_cents, set_by FROM credit_limits") == [
        {"limit_amount_cents": 25000, "set_by": e2e.ids["boss"]}
    ]
    boss.wait_for_function(
        "() => /лимит 250 · свободно .?150/.test(document.querySelector('#content').textContent)"
    )

    _back(boss)
    _wait_screen(boss, "clients", "limits")


# ─── «Клиенты» → «Канал» ─────────────────────────────────────────────────────


def test_channel_history_shows_post_and_effect(open_app, e2e):
    from services import channel

    e2e.run(channel.save_post(kind="showcase", ref="Кабель ВВГ 3x2.5", message_id=77,
                              posted_by=e2e.ids["boss"]))
    _lead(e2e, 700_001, "Пришёл после поста")  # first_seen ≥ posted_at

    boss = open_app(e2e.ids["boss"])
    go(boss, "clients")
    tab(boss, "channel")
    boss.wait_for_selector("#clients-body .card-row-title:has-text('Товар')")
    text = _text(boss, "#clients-body")
    assert "🛒 Товар" in text and "Кабель ВВГ 3x2.5" in text
    assert "за 24 ч после поста — 1 обращение · обычно 0/день" in text
    assert "Канал не настроен" in text, "без CHANNEL_ID публикация выключена — и это сказано"
