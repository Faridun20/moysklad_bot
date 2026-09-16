"""E2E: интерфейс руководителя — «смотреть, решать, контролировать».

Решение владельца: в меню руководителя только то, что ему нужно; работа
менеджера (накладные, отгрузка, касса, документы, лиды, канал, моточасы,
приёмка контейнера) — за выключателем «Рабочие действия» в «Меню», на день,
когда менеджер заболел. Выключатель — предпочтение ВИДА (сервер, user_prefs),
права ручек от него не зависят. Всё, что ждёт решения, — один экран «Решения»
с общим бейджем; из уведомлений его открывает `?startapp=decisions`.

Менеджер при этом не меняется ни на кнопку — это отдельный тест.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.e2e.conftest import go, nav_screens, seed_order, settled, tab, toast_text

SHOTS = os.environ.get("E2E_SHOTS_DIR")


def _shot(page, name: str) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)


def _bar(page) -> list[str]:
    return page.eval_on_selector_all(
        "#bottom-nav .nav-item[data-screen]", "els => els.map(e => e.dataset.screen)"
    )


def _seg_tabs(page) -> list[str]:
    return page.eval_on_selector_all("#content .seg-item[data-sect]", "els => els.map(e => e.dataset.sect)")


def _open_menu(page) -> None:
    page.click('#bottom-nav [data-action="menu"]')
    page.wait_for_selector("#nav-drawer.is-open .nav-drawer-panel")


def _drawer_tabs(page, section: str) -> list[str]:
    return page.eval_on_selector_all(
        f'#nav-drawer .nav-link--tab[data-screen="{section}"]', "els => els.map(e => e.dataset.tab)"
    )


def _seed_pending_everything(e2e) -> dict:
    """По одному решению каждого вида: заявка, оплата картой, сдача, возврат."""
    from services.database import (
        confirm_all_pending_payments_for_order,
        create_cash_deposit,
        create_return,
        mark_return_goods_received,
    )

    request = seed_order(e2e, approve=False, qty=1, price=120.0)              # заявка ждёт
    paid = seed_order(e2e, payment_type="paid", due_date=None, price=50.0)     # карта ждёт
    seed_order(e2e, qty=1, price=30.0)
    dep = e2e.run(create_cash_deposit(e2e.ids["mgr"], 30.0))                   # сдача ждёт
    assert dep.get("ok"), dep
    returned = seed_order(e2e, payment_type="paid", due_date=None, price=90.0)
    e2e.run(confirm_all_pending_payments_for_order(returned["order_id"], e2e.ids["boss"], "Boss"))
    item = e2e.rows("SELECT id FROM order_items WHERE order_id = ?", (returned["order_id"],))[0]["id"]
    ret = e2e.run(create_return(returned["order_id"], "full", "Брак", [(item, 2, 90.0)],
                                "no_refund", e2e.ids["mgr"]))
    assert ret.get("ok"), ret
    # Приёмку товара отмечает склад — у руководителя кнопки нет (работа склада).
    assert e2e.run(mark_return_goods_received(ret["return_id"], e2e.ids["keeper"])).get("ok")
    return {"req": request, "paid": paid, "deposit_id": dep["deposit_id"], "return_id": ret["return_id"]}


# ─── Панель, шторка, вкладки ────────────────────────────────────────────────


def test_boss_sees_exactly_the_new_bar_drawer_and_no_worker_buttons(open_app, e2e):
    seeded = seed_order(e2e, qty=1, price=40.0)            # одобрен, «Отгрузить» у склада
    boss = open_app(e2e.ids["boss"])
    settled(boss)
    assert _bar(boss) == ["today", "decisions", "money", "sales", "clients"]
    assert boss.locator('#bottom-nav [data-action="menu"]').count() == 1
    assert nav_screens(boss) == ["today", "decisions", "money", "sales", "clients",
                                 "stock", "leads", "settings"]
    _shot(boss, "boss-today")

    _open_menu(boss)
    # «Сверка» остаётся и без «Рабочих действий»: ежедневный пересчёт кассы для
    # руководителя — контроль, а не работа склада (helpers.js moneyTabs).
    assert _drawer_tabs(boss, "money") == ["debts", "reconcile", "report"]
    assert _drawer_tabs(boss, "sales") == ["orders", "report"]
    assert _drawer_tabs(boss, "stock") == ["catalog", "containers", "machines"]
    assert _drawer_tabs(boss, "clients") == ["buyers", "limits"]
    # «Обращения» без «Рабочих действий» — одна «Воронка», подпунктов нет.
    assert _drawer_tabs(boss, "leads") == []
    switch = boss.locator("#nav-drawer [data-work-switch]")
    assert switch.get_attribute("aria-checked") == "false"
    boss.wait_for_timeout(350)
    _shot(boss, "boss-drawer")
    boss.click("#nav-drawer .nav-drawer-close")

    go(boss, "sales")
    tab(boss, "orders")
    settled(boss)
    card = boss.locator(f'.order-card[data-id="{seeded["order_id"]}"]')
    card.wait_for()
    assert card.locator(".btn-ship-order, .btn-pay-order").count() == 0
    assert card.locator(".btn-cancel-order").count() == 1
    assert _seg_tabs(boss) == ["orders", "report"]
    _shot(boss, "boss-sales-orders")

    go(boss, "money")
    settled(boss)
    assert _seg_tabs(boss) == ["debts", "reconcile", "report"]
    assert boss.locator("#content .btn-pay-debt, #content .btn-confirm-pay").count() == 0
    _shot(boss, "boss-money-debts")

    go(boss, "stock")
    settled(boss)
    assert _seg_tabs(boss) == ["catalog", "containers", "machines"]
    tab(boss, "machines")
    settled(boss)
    assert boss.locator("#machine-new").count() == 0
    tab(boss, "containers")
    settled(boss)
    assert boss.locator("#container-new").count() == 0

    go(boss, "clients")
    settled(boss)
    assert _seg_tabs(boss) == ["buyers", "limits"]

    go(boss, "settings")
    settled(boss)
    assert boss.locator("#content [data-work-switch]").count() == 1
    assert boss.locator("#content #set-rates").count() == 1
    _shot(boss, "boss-settings")


def test_switch_on_shows_worker_actions_and_survives_reopen(open_app, e2e):
    seeded = seed_order(e2e, qty=1, price=40.0)
    boss = open_app(e2e.ids["boss"])
    settled(boss)
    _open_menu(boss)
    boss.click("#nav-drawer [data-work-switch]")
    boss.wait_for_function(
        "() => document.querySelector('#nav-drawer [data-work-switch]')?.getAttribute('aria-checked') === 'true'"
    )
    # Вкладки в шторке сразу поменялись.
    boss.wait_for_selector('#nav-drawer .nav-link--tab[data-screen="stock"][data-tab="invoices"]')
    assert _drawer_tabs(boss, "money") == ["debts", "ops", "reconcile", "report"]
    assert _drawer_tabs(boss, "clients") == ["buyers", "limits"]
    assert _drawer_tabs(boss, "leads") == ["funnel", "list", "channel"]
    assert _drawer_tabs(boss, "sales") == ["orders", "report", "docs"]
    boss.wait_for_timeout(350)
    _shot(boss, "boss-drawer-work-on")
    # Сервер запомнил и записал в аудит.
    from services import user_prefs

    prefs = user_prefs.get_prefs(e2e.ids["boss"])
    assert prefs["work_actions"] is True
    # «Сегодня» до переключения показывала подсказку D2 (work_actions было
    # выключено) — счётчик её показов рос независимо, `work_actions` его не
    # трогает и в аудит счётчик не попадает (см. test_user_prefs.py).
    assert prefs["work_actions_hint_shown"] >= 1
    assert e2e.rows("SELECT action, details FROM audit_log WHERE action = 'pref_set'") == [
        {"action": "pref_set", "details": "work_actions=on"},
    ]
    boss.click('#nav-drawer .nav-link[data-screen="sales"][data-tab="orders"]')
    settled(boss)
    card = boss.locator(f'.order-card[data-id="{seeded["order_id"]}"]')
    card.wait_for()
    card.locator(".btn-ship-order").wait_for()

    # Новая сессия WebView (localStorage не при чём) — выключатель на месте.
    again = open_app(e2e.ids["boss"])
    settled(again)
    go(again, "money")
    settled(again)
    assert _seg_tabs(again) == ["debts", "ops", "reconcile", "report"]

    # Выключил — работа менеджера снова спрятана.
    _open_menu(again)
    again.click("#nav-drawer [data-work-switch]")
    again.wait_for_function(
        "() => document.querySelector('#nav-drawer [data-work-switch]')?.getAttribute('aria-checked') === 'false'"
    )
    again.click("#nav-drawer .nav-drawer-close")
    # Экран под шторкой перерисован без «Кассы».
    again.wait_for_function(
        "() => ![...document.querySelectorAll('#content .seg-item[data-sect]')].some(e => e.dataset.sect === 'ops')"
    )
    assert user_prefs.get_prefs(e2e.ids["boss"])["work_actions"] is False


# ─── «Решения» ──────────────────────────────────────────────────────────────


def test_decisions_lists_every_kind_and_each_decision_works(open_app, e2e):
    seeded = _seed_pending_everything(e2e)
    boss = open_app(e2e.ids["boss"])
    settled(boss)
    # «Сегодня» уже знает, сколько решений ждёт, — бейдж на панели.
    badge = boss.locator('#bottom-nav .nav-item[data-screen="decisions"] [data-decisions-badge]')
    boss.wait_for_function(
        "() => document.querySelector('#bottom-nav [data-decisions-badge]')?.textContent === '4'"
    )
    assert badge.is_visible()
    # Строка очереди ведёт в «Решения».
    boss.locator('[data-queue="decisions"]').first.click()
    boss.wait_for_function("() => document.getElementById('bottom-nav').dataset.current === 'decisions'")
    settled(boss)
    groups = boss.eval_on_selector_all("[data-decision-group]", "els => els.map(e => e.dataset.decisionGroup)")
    assert groups == ["requests", "payments", "deposits", "returns"]
    _shot(boss, "boss-decisions")

    # Заявка: цена и тип оплаты видны, одобрение проводит заказ.
    req_card = boss.locator(f'.order-card[data-request="{seeded["req"]["req_id"]}"]')
    assert "120" in req_card.inner_text() and "В долг" in req_card.inner_text()
    req_card.locator(".btn-approve").click()
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('Заявка одобрена'))")
    boss.wait_for_function(
        "() => !document.querySelector('[data-decision-group=\"requests\"]')"
    )
    assert e2e.rows("SELECT status FROM orders WHERE id = ?", (seeded["req"]["order_id"],))[0]["status"] == "approved"

    boss.click(f'.pay-confirm[data-id="{seeded["paid"]["order_id"]}"]')
    boss.wait_for_function("() => !document.querySelector('[data-decision-group=\"payments\"]')")
    assert e2e.rows(
        "SELECT COUNT(*) AS n FROM payments WHERE order_id = ? AND status = 'pending'", (seeded["paid"]["order_id"],)
    )[0]["n"] == 0

    boss.click(f'.debt-card[data-dep="{seeded["deposit_id"]}"] .dep-confirm')
    boss.wait_for_function("() => !document.querySelector('[data-decision-group=\"deposits\"]')")
    assert e2e.rows("SELECT status FROM cash_deposits WHERE id = ?", (seeded["deposit_id"],))[0]["status"] == "confirmed"

    ret = boss.locator(f'.debt-card[data-ret="{seeded["return_id"]}"]')
    assert ret.locator(".ret-goods").count() == 0          # приёмка — работа склада
    ret.locator(".ret-confirm").click()
    boss.wait_for_selector("text=Ничего не ждёт решения")
    assert e2e.rows("SELECT status FROM returns WHERE id = ?", (seeded["return_id"],))[0]["status"] == "confirmed"
    boss.wait_for_function("() => document.querySelector('#bottom-nav [data-decisions-badge]')?.hidden === true")
    assert "Возврат подтверждён" in toast_text(boss)


def test_deep_link_opens_decisions(open_app, e2e):
    seed_order(e2e, approve=False)
    boss = open_app(e2e.ids["boss"])
    boss.goto(e2e.base_url + "/?startapp=decisions")
    boss.wait_for_function("() => document.getElementById('bottom-nav')?.dataset.current === 'decisions'")
    boss.wait_for_selector(".btn-approve")
    # Менеджеру тот же адрес — его «Подтвердить», а не чужой экран.
    mgr = open_app(e2e.ids["mgr"])
    mgr.goto(e2e.base_url + "/?startapp=decisions")
    mgr.wait_for_function("() => document.getElementById('bottom-nav')?.dataset.current === 'money'")
    mgr.wait_for_selector('.seg-item.active[data-sect="confirm"]')


def test_boss_machine_and_container_cards_are_view_plus_delete(open_app, e2e):
    from services import containers, machines

    mres = e2e.run(machines.create_machine(name="JCB 3CX", vin="JCB1", price_cents=2_500_000,
                                           cost_cents=2_000_000, currency="USD", created_by=e2e.ids["boss"]))
    assert mres.get("ok"), mres
    mid = mres["machine_id"]
    cres = e2e.run(containers.create_container(number="MSKU-77", created_by=e2e.ids["boss"]))
    cid = cres["container_id"]
    e2e.run(containers.add_item(cid, name="Кабель", expected_qty=10))
    e2e.run(containers.mark_arrived(cid, user_id=e2e.ids["boss"]))

    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "machines")
    settled(boss)
    boss.click(f'[data-machine="{mid}"]')
    boss.wait_for_selector('#content [data-mact="delete"]')
    for act in ("hours", "edit", "sale", "credit"):
        assert boss.locator(f'#content [data-mact="{act}"]').count() == 0, act
    text = boss.inner_text("#content")
    assert "Себестоимость" in text
    _shot(boss, "boss-machine-card")

    go(boss, "stock")
    tab(boss, "containers")
    settled(boss)
    boss.click(f'[data-container="{cid}"]')
    boss.wait_for_selector("#content #cont-del")
    for sel in ("#cont-edit", "#cont-item-add", "#cont-save", "#cont-supply", "#cont-post", ".qty-input"):
        assert boss.locator(f"#content {sel}").count() == 0, sel
    _shot(boss, "boss-container-card")


# ─── Менеджер не меняется ───────────────────────────────────────────────────


def test_manager_ui_is_unchanged(open_app, e2e):
    seeded = seed_order(e2e, qty=1, price=40.0)
    mgr = open_app(e2e.ids["mgr"])
    settled(mgr)
    # «Клиенты» — пятой кнопкой панели (решение владельца: список покупателей
    # в одно касание); «Обращения» — в шторке.
    assert _bar(mgr) == ["today", "sales", "stock", "money", "clients"]
    assert nav_screens(mgr) == ["today", "sales", "stock", "money", "clients", "leads"]
    _open_menu(mgr)
    assert mgr.locator("#nav-drawer [data-work-switch]").count() == 0
    assert _drawer_tabs(mgr, "money") == ["confirm", "debts", "ops", "reconcile"]
    assert _drawer_tabs(mgr, "stock") == ["catalog", "containers", "machines", "invoices"]
    assert _drawer_tabs(mgr, "sales") == ["orders", "report", "docs"]
    mgr.click("#nav-drawer .nav-drawer-close")
    go(mgr, "sales")
    tab(mgr, "orders")
    settled(mgr)
    card = mgr.locator(f'.order-card[data-id="{seeded["order_id"]}"]')
    card.locator(".btn-ship-order").wait_for()
    assert card.locator(".btn-cancel-order").count() == 0
    assert mgr.locator("#btn-new-order").count() == 1
