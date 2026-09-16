"""E2E: шторка «Меню» — все разделы роли и их вкладки.

Нижняя панель держит четыре раздела, пятый слот — «Меню»: шторка справа
деревом «раздел → вкладки». Проверяем то, чего не видит jsdom: шторка
реально выезжает в видимую область и уезжает за край, фон под ней недоступен
(inert), переход ведёт в тот же раздел и вкладку, что ряд вкладок, и все
способы закрыть — фон, Esc, «Назад» Telegram, свайп — действительно закрывают.
"""

from __future__ import annotations

from tests.e2e.conftest import go, settled

VIEW_W = 390


def _open_menu(page) -> None:
    page.click('#bottom-nav .nav-item[data-action="menu"]')
    _wait_open(page)


def _wait_open(page) -> None:
    page.wait_for_selector("#nav-drawer.is-open .nav-drawer-panel")
    # Выезд доиграл: правый край панели у правого края экрана.
    page.wait_for_function(
        "(w) => { const r = document.querySelector('#nav-drawer .nav-drawer-panel')"
        ".getBoundingClientRect(); return Math.abs(r.right - w) < 1; }",
        arg=VIEW_W,
    )


def _is_closed(page) -> bool:
    return page.evaluate(
        "() => !document.getElementById('nav-drawer').classList.contains('is-open')"
        " && document.getElementById('nav-drawer').hasAttribute('inert')"
        " && !document.querySelector('.app').hasAttribute('inert')"
    )


def _wait_offscreen(page) -> None:
    page.wait_for_function(
        "(w) => document.querySelector('#nav-drawer .nav-drawer-panel').getBoundingClientRect().left >= w",
        arg=VIEW_W,
    )


def test_manager_opens_menu_and_goes_to_stock_invoices(open_app, e2e):
    mgr = open_app(e2e.ids["mgr"])
    settled(mgr)
    assert mgr.locator("#bottom-nav .nav-item[data-screen]").count() == 4

    _open_menu(mgr)
    menu = mgr.locator('#bottom-nav [data-action="menu"]')
    assert menu.get_attribute("aria-expanded") == "true"
    # Фокус внутри диалога, фон недоступен.
    assert mgr.evaluate("() => document.querySelector('#nav-drawer .nav-drawer-panel').contains(document.activeElement)")
    assert mgr.evaluate("() => document.querySelector('.app').hasAttribute('inert')")
    assert mgr.evaluate("() => window.Telegram.WebApp.BackButton.isVisible") is True
    # Текущий раздел («Сегодня», без вкладок) подсвечен сам.
    assert mgr.get_attribute('#nav-drawer [aria-current="page"]', "data-screen") == "today"

    # Вкладки по роли: менеджеру «Движения» есть, «Воронки» и «Лимитов» нет
    # (ручки ответят 403 — дверь, которая не открывается, не рисуется).
    drawer = mgr.locator("#nav-drawer")
    assert drawer.locator('[data-screen="stock"][data-tab="invoices"]').count() == 1
    assert drawer.locator('[data-tab="funnel"]').count() == 0
    assert drawer.locator('[data-tab="limits"]').count() == 0

    mgr.click('#nav-drawer .nav-link[data-screen="stock"][data-tab="invoices"]')
    mgr.wait_for_selector('.seg-item.active[data-sect="invoices"]')
    settled(mgr)
    assert _is_closed(mgr)
    _wait_offscreen(mgr)
    assert mgr.evaluate("() => document.getElementById('bottom-nav').dataset.current") == "stock"
    assert mgr.get_attribute("#bottom-nav .nav-item.active", "data-screen") == "stock"
    assert menu.get_attribute("aria-expanded") == "false"
    assert "Движения" in mgr.inner_text("#greeting")
    # Раздел — корневой экран: «Назад» после перехода спрятана.
    assert mgr.evaluate("() => window.Telegram.WebApp.BackButton.isVisible") is False

    # Повторное открытие подсвечивает именно вкладку.
    _open_menu(mgr)
    current = mgr.locator('#nav-drawer [aria-current="page"]')
    assert current.count() == 1
    assert current.get_attribute("data-tab") == "invoices"


def test_boss_reaches_section_outside_the_bar(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    _open_menu(boss)
    # Руководство: семь разделов, в панели — «Сегодня · Решения · Деньги · Продажи».
    assert boss.locator("#nav-drawer .nav-link--section").count() == 7
    boss.click('#nav-drawer .nav-link[data-screen="clients"][data-tab="limits"]')
    boss.wait_for_selector('.seg-item.active[data-sect="limits"]')
    # «Клиентов» в панели нет — подсвечена «Меню», чтобы было видно, где ты.
    assert boss.locator('#bottom-nav [data-action="menu"].active').count() == 1
    assert boss.locator("#bottom-nav .nav-item[data-screen].active").count() == 0
    # Ряд вкладок раздела на месте и работает как раньше.
    boss.click('.seg-item[data-sect="funnel"]')
    boss.wait_for_selector('.seg-item.active[data-sect="funnel"]')


def test_drawer_closes_by_scrim_escape_back_and_swipe(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    # Заглушка BackButton запоминает обработчик — «Назад» нажимаем из теста.
    boss.evaluate(
        "() => { const b = window.Telegram.WebApp.BackButton;"
        " b.onClick = (f) => { window.__back = f; }; b.offClick = () => { window.__back = null; }; }"
    )

    # Тап по фону слева от панели.
    _open_menu(boss)
    boss.mouse.click(24, 400)
    boss.wait_for_function("() => !document.getElementById('nav-drawer').classList.contains('is-open')")
    assert _is_closed(boss)
    _wait_offscreen(boss)
    # Фокус вернулся на «Меню».
    assert boss.evaluate("() => document.activeElement?.dataset.action") == "menu"

    # Esc.
    _open_menu(boss)
    boss.keyboard.press("Escape")
    assert _is_closed(boss)

    # Системная «Назад» Telegram.
    _open_menu(boss)
    boss.evaluate("() => window.__back()")
    assert _is_closed(boss)
    assert boss.evaluate("() => window.Telegram.WebApp.BackButton.isVisible") is False

    # Свайп вправо по панели. Короткий сдвиг — шторка возвращается на место.
    _open_menu(boss)
    box = boss.locator("#nav-drawer .nav-drawer-panel").bounding_box()
    y = box["y"] + box["height"] / 2
    x0 = box["x"] + 40
    boss.mouse.move(x0, y)
    boss.mouse.down()
    boss.mouse.move(x0 + 20, y, steps=4)
    boss.wait_for_timeout(200)
    boss.mouse.move(x0 + 30, y, steps=4)
    boss.wait_for_timeout(200)
    boss.mouse.up()
    assert boss.evaluate("() => document.getElementById('nav-drawer').classList.contains('is-open')")
    _wait_open(boss)  # доехала обратно
    # Длинный — закрывает, и отпущенный над пунктом палец никуда не ведёт.
    screen_before = boss.evaluate("() => document.getElementById('bottom-nav').dataset.current")
    boss.mouse.move(x0, y)
    boss.mouse.down()
    boss.mouse.move(x0 + box["width"] * 0.6, y, steps=10)
    boss.mouse.up()
    boss.wait_for_function("() => !document.getElementById('nav-drawer').classList.contains('is-open')")
    assert _is_closed(boss)
    _wait_offscreen(boss)
    assert boss.evaluate("() => document.getElementById('bottom-nav').dataset.current") == screen_before


def test_keeper_has_no_menu_button(open_app, e2e):
    """У кладовщика три раздела без вкладок: шторка повторила бы панель."""
    keeper = open_app(e2e.ids["keeper"])
    assert keeper.locator('#bottom-nav [data-action="menu"]').count() == 0
    go(keeper, "money")
