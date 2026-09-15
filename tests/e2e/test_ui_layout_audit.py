"""E2E: геометрический аудит вёрстки на всех экранах — «ничего ни на что не налезает».

Жалобы с площадки (Android Telegram, ~360–412 css px, тёмная тема): нижняя
панель садилась на форму при открытой клавиатуре; карточка клиента наезжала на
поле поиска; вкладка «Подтвердить» читалась «одтвердить»; фильтр долгов и верх
карточки контейнера сидели под шапкой; последние карточки уходили под панель.
Каждая из них — геометрия, которую jsdom не считает. Поэтому здесь настоящий
Chromium проходит все разделы и вкладки под руководителем и менеджером, на
засеянных данных (длинные имена, крупные суммы, много карточек), и на каждом
экране `layout_audit.js` меряет:

* горизонтальный вылет страницы и элементов за край экрана;
* пересечение соседних блоков и «склейку» скруглённых карточек без зазора;
* вылет содержимого из карточки/кнопки и обрезанные подписи вкладок и кнопок;
* активную вкладку ряда — видна целиком;
* каждый интерактивный элемент, докрученный в зону видимости, не закрыт нижней
  панелью или шапкой (`elementFromPoint` в его верхней и нижней четверти);
* шапка непрозрачна.

Отдельно — клавиатура (окно сжато, поле в фокусе: панель спрятана, поле и
соседние контролы открыты) и сброс прокрутки при смене вида.

`E2E_SHOTS_DIR=/путь` — сохранить скриншот каждого проверенного экрана.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.e2e.conftest import (
    _TG_STUB,
    E2E_TIMEOUT_MS,
    go,
    nav_screens,
    seed_order,
    settled,
    tab,
    work_actions,
)
from tests.e2e.test_cov_click_everything import _INFLIGHT_JS, _seed_rich

AUDIT_JS = (Path(__file__).parent / "layout_audit.js").read_text(encoding="utf-8")
SHOTS = os.environ.get("E2E_SHOTS_DIR")

# Тема Telegram так, как её отдаёт клиент: SDK кладёт themeParams в
# CSS-переменные `--tg-theme-*` на <html>. Цвета — Android «Тёмная» / «Дневная».
_THEMES = {
    "dark": {
        "bg_color": "#212121", "secondary_bg_color": "#181818", "text_color": "#ffffff",
        "hint_color": "#aaaaaa", "link_color": "#8774e1", "button_color": "#8774e1",
        "button_text_color": "#ffffff",
    },
    "light": {
        "bg_color": "#ffffff", "secondary_bg_color": "#f0f0f0", "text_color": "#000000",
        "hint_color": "#999999", "link_color": "#2481cc", "button_color": "#2481cc",
        "button_text_color": "#ffffff",
    },
}

# Дополнение к заглушке SDK из conftest: тема, высота окна и события
# (`viewportChanged` приходит от клиента, когда клавиатура сжимает окно).
_TG_EXTRA = """
(function () {
  const w = window.Telegram.WebApp;
  const handlers = {};
  const theme = %(theme)s;
  w.colorScheme = %(scheme)s;
  w.themeParams = theme;
  for (const [k, v] of Object.entries(theme)) {
    document.documentElement.style.setProperty('--tg-theme-' + k.replace(/_/g, '-'), v);
  }
  // Клиент сообщает высоту с запасом над окном WebView (%(extra)s px) — так
  // бывает на Android, и каркас не должен от этого становиться выше экрана.
  Object.defineProperty(w, 'viewportHeight', { get: () => innerHeight + %(extra)s, configurable: true });
  Object.defineProperty(w, 'viewportStableHeight', { get: () => innerHeight + %(extra)s, configurable: true });
  w.onEvent = function (e, f) { (handlers[e] = handlers[e] || []).push(f); };
  w.offEvent = function (e, f) { handlers[e] = (handlers[e] || []).filter(x => x !== f); };
  window.__tgEmit = function (e, arg) { (handlers[e] || []).forEach(f => f.call(w, arg)); };
})();
"""


@pytest.fixture
def phone(browser, e2e):
    """`phone(user_id, theme=, width=, height=)` → страница WebApp в теме Telegram."""
    contexts = []

    def _open(user_id: int, *, theme: str = "dark", width: int = 390, height: int = 844,
              tg_extra_height: int = 0) -> Page:
        # reduced_motion: появление экрана (fadeUp) сдвигает блоки на 7px, и
        # замер посреди анимации видел бы пересечения, которых на экране нет.
        ctx = browser.new_context(viewport={"width": width, "height": height},
                                  color_scheme=theme, reduced_motion="reduce")
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(E2E_TIMEOUT_MS)
        stub = _TG_STUB % {"init_data": repr(str(user_id))} + _TG_EXTRA % {
            "theme": json.dumps(_THEMES[theme]), "scheme": json.dumps(theme), "extra": tg_extra_height,
        }
        page.route(
            "https://telegram.org/js/telegram-web-app.js",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=stub),
        )
        page.goto(e2e.base_url + "/")
        page.wait_for_selector("#bottom-nav .nav-item", state="attached")
        page.evaluate(_INFLIGHT_JS)
        return page

    yield _open
    for ctx in contexts:
        ctx.close()


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Обход открывает десятки экранов подряд — лимитер отвечал бы 429."""
    import webapp.server as server

    monkeypatch.setattr(server, "rate_limit_acquire", lambda *a, **kw: True)


def _shot(page: Page, name: str, *, full_page: bool = False) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=full_page)


# ─── Засев ───────────────────────────────────────────────────────────────────


def _seed_layout(e2e, tmp_path: Path, *, accounting: bool = False) -> None:
    """Всё, что есть у обходчика, плюс то, на чём вёрстка ломалась на площадке.

    Длинные названия клиентов (пикер «Выбор клиента» и карточки), долг с
    внесённой разбивкой оплаты (наличные на руках + карта ждёт банка — длинные
    строки состояния), «оплата сразу» без оплаты («Внесите оплату · …» на
    карточке заказа), крупная сумма в сумах, много карточек — чтобы нижние
    доходили до панели.
    """
    from services.database import mark_order_shipped
    from tests.e2e.conftest import pay_order

    seeded = _seed_rich(e2e, tmp_path)
    db = e2e.db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for i in range(12):
            cur.execute(
                db.q("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"),
                (f"ООО «Торгово-строительная компания Самарканд Инвест Групп-{i}»", "customer",
                 f"+99890{i:07d}", db.now_str()),
            )
        conn.commit()
    # Лимит «Ромашки» из засева обходчика не должен мешать крупным заказам.
    e2e.exec("UPDATE credit_limits SET limit_amount_cents = 99999999900")
    big = seed_order(e2e, qty=1, price=12130.0)
    assert e2e.run(mark_order_shipped(big["order_id"], e2e.ids["keeper"], "Keeper")).get("ok")
    # Разбивка: наличные остаются у менеджера (форма сдачи со списком заказов),
    # карта ждёт подтверждения (карточки «Подтвердить» и «Долги»).
    pay_order(e2e, big["order_id"], [("cash", 5000), ("card", 7130)])
    # «Оплата сразу» одобрена, оплату не вносили — карточка «Внесите оплату».
    seed_order(e2e, payment_type="paid", due_date=None, qty=3, price=4043.33, pay=None)
    uzs = seed_order(e2e, qty=1000, price=12130.0)
    e2e.exec("UPDATE orders SET currency = 'UZS' WHERE id = ?", (uzs["order_id"],))
    # Рассрочка менеджера ждёт одобрения: группа «Сделки по технике» в «Решениях»
    # (длинные условия: цена против прайса, скидка, график), «Ждёт одобрения» в
    # «Технике» и карточка заявки в карточке машины.
    from services import machine_deal_requests as mdr
    from services import machines

    m = e2e.run(machines.create_machine(
        vin="LAYOUT-MDR", name="Hitachi ZX200-5G с гидромолотом и ковшом 1,2 м³",
        created_by=e2e.ids["boss"], status="in_stock", price_cents=2_500_000))
    req = e2e.run(mdr.submit(
        m["machine_id"], kind="credit", actor_id=e2e.ids["mgr"], actor_name="Manager",
        actor_role="manager", price_cents=2_400_000, buyer_name="ООО «Самарканд Инвест Групп»",
        buyer_phone="+998901112233", buyer_passport="AA1234567", buyer_note="Самовывоз со склада",
        down_payment_cents=400_000, months=12, notify=False))
    assert req.get("ok") and req.get("pending"), req
    if accounting:
        from tests.e2e.test_accounting import _seed_accounting

        _seed_accounting(e2e, seeded["debt"])


# ─── Обход ───────────────────────────────────────────────────────────────────


class Audit:
    def __init__(self, page: Page, label: str):
        self.page = page
        self.label = label
        self.issues: list[str] = []
        self.screens = 0

    def idle(self) -> None:
        p = self.page
        p.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        p.wait_for_function("() => window.__e2eInflight === 0")
        settled(p)
        # Докрутка рядов и подсказки прокрутки — в MutationObserver и таймере 60 мс.
        p.wait_for_timeout(120)

    def check(self, name: str, root: str = "#content") -> None:
        self.idle()
        self.screens += 1
        for issue in self.page.evaluate(AUDIT_JS, root):
            self.issues.append(f"[{self.label}] {name}: {issue}")
        _shot(self.page, f"{self.label}-{name}", full_page=root == "#content")

    def sections(self) -> None:
        # Все разделы роли: панель и «Меню» (у руководителя — ещё «Решения» и
        # «Настройки», склад и клиенты — в шторке).
        p = self.page
        for screen in nav_screens(p):
            go(p, screen)
            self.idle()
            tabs = p.eval_on_selector_all("#content .seg-item[data-sect]", "els => els.map(e => e.dataset.sect)")
            for key in tabs or [""]:
                if key:
                    tab(p, key)
                self.check(f"{screen}-{key or 'main'}")

    def open_cards(self, list_tab: str, row: str, name: str) -> None:
        p = self.page
        go(p, "stock")
        tab(p, list_tab)
        self.idle()
        for i in range(p.locator(f"#content {row}").count()):
            if i:
                go(p, "stock")
                tab(p, list_tab)
                self.idle()
            p.locator(f"#content {row}").nth(i).click()
            self.check(f"{name}{i}")

    def overlay(self, opener: str, name: str) -> None:
        """Форма поверх экрана: открыть, проверить, закрыть не отправляя."""
        p = self.page
        if not p.locator(opener).count():
            return
        p.locator(opener).first.click()
        p.wait_for_selector(".c-overlay")
        self.check(name, ".c-overlay")
        while p.locator(".c-overlay").count():
            p.keyboard.press("Escape")
            p.wait_for_timeout(50)

    def payment_form(self, opener: str, name: str, *, accounts: bool = False) -> None:
        """Форма «Как получены деньги»: две строки, вторая — картой в сумах с
        курсом и строкой «Куда поступили». `accounts` — ещё лист выбора карты
        и форма новой карты/счёта поверх оплаты."""
        p = self.page
        if not p.locator(opener).count():
            return
        p.locator(opener).first.click()
        p.wait_for_selector(".c-overlay .pay-part")
        p.click(".c-overlay .pay-add-part")
        p.locator(".c-overlay .pay-part").nth(1).locator('[data-pay-cur="UZS"]').click()
        p.wait_for_selector(".c-overlay .pay-part-rate")
        p.wait_for_selector(".c-overlay .pay-part-account")
        self.check(name, ".c-overlay")
        if accounts:
            p.locator(".c-overlay .pay-part-account").last.click()
            p.wait_for_selector(".c-overlay.pay-account-picker")
            self.check(f"{name}-card-picker", ".c-overlay.pay-account-picker")
            p.click(".c-overlay.pay-account-picker .picker-add")
            p.wait_for_selector(".c-overlay.pay-account-form")
            self.check(f"{name}-new-card", ".c-overlay.pay-account-form")
            while p.locator(".c-overlay").count():
                p.keyboard.press("Escape")
                p.wait_for_timeout(50)
            p.locator(opener).first.click()
            p.wait_for_selector(".c-overlay .pay-part")
            p.locator(".c-overlay .pay-part").first.locator('[data-pay-method="bank"]').click()
            p.locator(".c-overlay .pay-part-account").first.click()
            p.wait_for_selector(".c-overlay.pay-account-picker")
            p.click(".c-overlay.pay-account-picker .picker-add")
            p.wait_for_selector(".c-overlay.pay-account-form #ms-f-account_number")
            self.check(f"{name}-new-bank-account", ".c-overlay.pay-account-form")
        while p.locator(".c-overlay").count():
            p.keyboard.press("Escape")
            p.wait_for_timeout(50)

    def order_editor(self, product_id: int) -> None:
        p = self.page
        go(p, "sales")
        tab(p, "orders")
        self.idle()
        p.click("#btn-new-order")
        p.wait_for_selector("#choose-agent")
        self.check("order-editor")
        p.click("#choose-agent")
        p.wait_for_selector(".agent-row >> nth=5")
        self.check("client-picker")
        p.click('.agent-row:has-text("Ромашка")')
        p.wait_for_selector("#btn-add-product")
        p.click("#btn-add-product")
        p.wait_for_selector(".prod-row")
        self.check("product-picker")
        p.click(f'.prod-row[data-product="{product_id}"]')
        p.wait_for_selector("#qty-input")
        self.check("qty")


@pytest.mark.parametrize(
    ("role", "theme", "width", "accounting", "work"),
    [
        # Руководитель как есть: «Сегодня · Решения · Деньги · Продажи · Меню»,
        # без работы менеджера.
        ("boss", "dark", 390, False, False),
        ("mgr", "dark", 360, False, True),
        # Бухгалтерия включена: «Касса» — счета и журнал, в долгах «Получил деньги».
        # Руководитель с «Рабочими действиями» — экраны менеджера под ним.
        ("boss", "light", 360, True, True),
        ("mgr", "light", 412, True, True),
    ],
    ids=["boss-dark-390", "mgr-dark-360", "boss-light-360-acc-work", "mgr-light-412-acc"],
)
def test_no_overlaps_on_any_screen(phone, e2e, tmp_path, no_rate_limit, role, theme, width, accounting, work):
    _seed_layout(e2e, tmp_path, accounting=accounting)
    if role == "boss" and work:
        work_actions(e2e)
    page = phone(e2e.ids[role], theme=theme, width=width, height=800)
    audit = Audit(page, f"{role}-{theme}-{width}{'-acc' if accounting else ''}{'-work' if role == 'boss' and work else ''}")

    audit.sections()
    if role == "boss":
        # «Решения»: сделка по технике — своей группой с кнопками решения;
        # «Настройки»: «Удаление — только руководитель» рядом с «Рабочими действиями».
        go(page, "decisions")
        page.wait_for_selector('#content [data-decision-group="machine_deals"] [data-mreq-approve]')
        audit.check("decisions-machine-deal")
        go(page, "settings")
        page.wait_for_selector("#content [data-delete-switch]")
        assert page.locator("#content [data-work-switch]").count() == 1
        audit.check("settings-switches")
    # Деньги → Долги с фильтром «К оплате сейчас».
    go(page, "money")
    tab(page, "debts")
    audit.idle()
    page.click('.seg-item[data-f="today"]')
    audit.check("money-debts-today")
    page.click('.seg-item[data-f="all"]')
    audit.idle()
    if not accounting and work:
        # Оплата долга — форма разбивки (при бухгалтерии её заменяет «Получил деньги»).
        audit.payment_form("#content .btn-pay-debt", "debt-payment-form", accounts=True)
        # «Оплата сразу» перед отгрузкой: карточка «Внесите оплату» и форма.
        go(page, "sales")
        tab(page, "orders")
        audit.idle()
        page.wait_for_selector("#content .btn-pay-order")
        audit.check("sales-orders-needs-payment")
        audit.payment_form("#content .btn-pay-order", "order-payment-form")
        go(page, "money")
        tab(page, "debts")
        audit.idle()
    if role == "mgr" and not accounting:
        # «Сдать наличные»: на руках по заказам; переключение на сумы.
        go(page, "money")
        tab(page, "ops")
        audit.idle()
        page.wait_for_selector("#content .dep-order")
        audit.check("money-ops-handover")
        page.click('#content [data-dep-cur="UZS"]')
        audit.check("money-ops-handover-uzs")
        go(page, "money")
        tab(page, "debts")
        audit.idle()
    if page.locator("#content [data-buyer]").count():
        page.locator("#content [data-buyer]").first.click()
        audit.check("buyer-card")
    if accounting:
        go(page, "money")
        tab(page, "ops")
        audit.idle()
        for view in page.eval_on_selector_all("[data-acc-view]", "els => els.map(e => e.dataset.accView)"):
            page.click(f'[data-acc-view="{view}"]')
            audit.check(f"acc-{view}")
        go(page, "money")
        tab(page, "debts")
        audit.idle()
        audit.overlay(".acc-pay", "acc-receipt-form")

    # Отчёт за произвольный период — календарь.
    go(page, "sales")
    if page.locator('.seg-item[data-sect="report"]').count():
        tab(page, "report")
        audit.idle()
        page.click('.seg-item[data-period="custom"]')
        page.wait_for_selector("#content .cal")
        audit.check("sales-report-calendar")

    audit.open_cards("containers", "[data-container]", "container-card")
    audit.open_cards("machines", "[data-machine]", "machine-card")
    go(page, "stock")
    tab(page, "machines")
    audit.idle()
    audit.overlay("#machine-new", "machine-form")
    tab(page, "containers")
    audit.idle()
    audit.overlay("#container-new", "container-form")
    if page.locator('.seg-item[data-sect="invoices"]').count():
        tab(page, "invoices")
        audit.idle()
        page.click("#wh-new")
        audit.check("invoice-form")
        audit.overlay("#wh-cp", "counterparty-picker")

    go(page, "stock")
    tab(page, "machines")
    audit.idle()
    page.locator("#content [data-machine]").first.click()
    page.wait_for_selector("#content .section-label")
    audit.idle()
    audit.overlay('[data-mact="hours"]', "machine-hours-form")

    go(page, "sales")
    if page.locator('.seg-item[data-sect="docs"]').count():
        tab(page, "docs")
        audit.idle()
        audit.overlay("#doc-new", "doc-form")

    if role == "boss":
        # «Настройки»: реквизиты компании и курсы.
        go(page, "settings")
        audit.idle()
        audit.overlay("#set-company", "company-form")
        go(page, "settings")
        audit.idle()
        page.click("#set-rates")
        page.wait_for_selector(".rate-input")
        audit.check("settings-rates")
        # «Карты и счета»: список, новая запись, правка с архивом.
        go(page, "settings")
        audit.idle()
        page.click("#set-pay-accounts")
        page.wait_for_selector("#content [data-pay-account]")
        audit.check("settings-pay-accounts")
        audit.overlay('#content [data-pay-account-add="bank"]', "settings-pay-account-new-bank")
        audit.overlay("#content [data-pay-account]", "settings-pay-account-edit")
        go(page, "clients")
        tab(page, "limits")
        audit.idle()
        page.click("#open-rates")
        page.wait_for_selector(".rate-input")
        audit.check("rates")
        go(page, "clients")
        tab(page, "limits")
        audit.idle()
        if page.locator("#content [data-agent]").count():
            page.locator("#content [data-agent]").first.click()
            audit.check("limit-edit")

    go(page, "clients")
    audit.idle()
    if page.locator('.seg-item[data-sect="list"]').count():
        tab(page, "list")
        audit.idle()
    if page.locator("#content [data-lead]").count():
        page.locator("#content [data-lead]").first.click()
        audit.check("lead-card")

    if role == "mgr":
        audit.order_editor(e2e.ids["product"])

    page.click("#search-btn")
    page.fill("#search-input", "Ромашка")
    page.wait_for_selector("#content .search-item")
    audit.check("search")

    # Шторка «Меню»: пункты не наезжают, последний докручивается.
    go(page, "money")
    page.click('#bottom-nav [data-action="menu"]')
    page.wait_for_selector("#nav-drawer.is-open")
    page.wait_for_timeout(350)  # выезд шторки
    audit.check("nav-drawer", "#nav-drawer .nav-drawer-body")
    page.click("#nav-drawer .nav-drawer-close")

    assert audit.screens >= 20, f"обход прошёл подозрительно мало экранов: {audit.screens}"
    assert not audit.issues, "Вёрстка:\n" + "\n".join(audit.issues)


# ─── D3 (продуктовый аудит): бейдж счётчика не режет подпись вкладки ─────────

_TAB_BADGE_CHECK_JS = """
() => {
  const out = [];
  let sawBadge = false;
  for (const el of document.querySelectorAll('#content .seg-item[data-sect]')) {
    if (el.scrollWidth > el.clientWidth + 1) {
      out.push(`clipped: ${el.dataset.sect} (${el.scrollWidth} > ${el.clientWidth})`);
    }
    const badge = el.querySelector('.stock-badge');
    if (badge) {
      sawBadge = true;
      const b = badge.getBoundingClientRect();
      const t = el.getBoundingClientRect();
      if (b.left < t.left - 1 || b.right > t.right + 1) {
        out.push(`badge-overflow: ${el.dataset.sect}`);
      }
    }
  }
  return { out, sawBadge };
}
"""


@pytest.mark.parametrize("width", [360, 390], ids=["360", "390"])
def test_confirm_badge_does_not_clip_tab_label(phone, e2e, tmp_path, no_rate_limit, width):
    """Регресс со снимков площадки (payments-shots/05,07,09): сегмент «Деньги →
    Подтвердить N» с бейджем счётчика читался «одтвердить» — `.seg-item` со
    старым `min-width: 0` делил ряд на равные трети и резал подпись с обеих
    сторон, пока бейдж стоял рядом. Фикс — `.seg-item { min-width:
    max-content }` (style.css, комментарий там же прямо ссылается на этот
    баг) — сделан отдельным UI-визуальным проходом ДО этой задачи.

    `test_no_overlaps_on_any_screen` в этом же файле ловит то же самое как
    часть общего геометрического аудита (проходит все экраны и роли), но не
    называет конкретно эту вкладку и не требует, чтобы бейдж реально был
    показан. Здесь — узкая, целевая регресс-проверка именно по описанию бага:
    на 360/390px, с РЕАЛЬНО показанным бейджем счётчика («Подтвердить N»), у
    КАЖДОГО ряда вкладок «Деньги»/«Продажи»/«Склад»/«Клиенты» подпись активной
    (и любой другой видимой) вкладки не обрезана, и бейдж не вылезает за
    рамку вкладки (не наезжает на подпись/соседей).
    """
    _seed_layout(e2e, tmp_path)  # заводит платёж картой, ждущий подтверждения
    page = phone(e2e.ids["mgr"], theme="dark", width=width, height=800)

    saw_badge = False
    issues: list[str] = []
    for screen in ("money", "sales", "stock", "clients"):
        go(page, screen)
        settled(page)
        tabs = page.eval_on_selector_all(
            "#content .seg-item[data-sect]", "els => els.map(e => e.dataset.sect)"
        )
        for key in tabs:
            tab(page, key)
            # Бейдж «Подтвердить» дописывается в DOM асинхронно (см. setConfirmBadge
            # в app.js) — короткая пауза, чтобы застать его на месте.
            page.wait_for_timeout(150)
            res = page.evaluate(_TAB_BADGE_CHECK_JS)
            issues += [f"{screen}/{key}: {i}" for i in res["out"]]
            saw_badge = saw_badge or res["sawBadge"]

    assert not issues, "Вкладки:\n" + "\n".join(issues)
    assert saw_badge, "бейдж счётчика ни разу не показался — проверка не была бы полезной без него"


# ─── Клавиатура ──────────────────────────────────────────────────────────────


def _nav_visibility(page: Page) -> str:
    return page.evaluate("() => getComputedStyle(document.getElementById('bottom-nav')).visibility")


def _covered(page: Page, sel: str) -> str | None:
    """Кто закрывает элемент в его текущем положении (None — никто)."""
    return page.eval_on_selector(
        sel,
        """el => {
          const r = el.getBoundingClientRect();
          if (r.bottom > innerHeight || r.top < 0) return 'за краем окна';
          for (const fy of [0.25, 0.75]) {
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height * fy);
            if (!hit || !(hit === el || el.contains(hit))) {
              return hit ? (hit.closest('#bottom-nav, .topbar') || hit).className : 'пусто';
            }
          }
          return null;
        }""",
    )


def test_keyboard_hides_bottom_nav_and_keeps_qty_form_open(phone, e2e):
    """«Добавить в заявку»: клавиатура сжала окно — панель не садится на валюту."""
    page = phone(e2e.ids["mgr"], width=390, height=844)
    go(page, "sales")
    page.click("#btn-new-order")
    page.click("#btn-add-product")
    page.click(f'.prod-row[data-product="{e2e.ids["product"]}"]')
    page.wait_for_selector("#qty-input")
    page.wait_for_timeout(400)
    assert _nav_visibility(page) == "visible", "фокус без клавиатуры панель не прячет"

    # Клавиатура: Android WebView сжимает окно, клиент шлёт viewportChanged.
    page.focus("#qty-input")
    page.set_viewport_size({"width": 390, "height": 470})
    page.evaluate("() => window.__tgEmit('viewportChanged', { isStateStable: true })")
    page.wait_for_function("() => getComputedStyle(document.getElementById('bottom-nav')).visibility === 'hidden'")
    page.wait_for_timeout(400)  # докрутка поля после анимации клавиатуры
    _shot(page, "keyboard-qty")
    assert _covered(page, "#qty-input") is None
    for sel in ('.cur-btn[data-cur="USD"]', '.cur-btn[data-cur="UZS"]'):
        page.eval_on_selector(sel, "el => el.scrollIntoView({ block: 'nearest' })")
        assert _covered(page, sel) is None, f"{sel} закрыт при открытой клавиатуре"

    # Поле ниже в форме доезжает в видимую часть само.
    page.evaluate("() => window.scrollTo(0, 0)")
    page.focus("#price-input")
    page.wait_for_timeout(500)
    assert _covered(page, "#price-input") is None

    # Клавиатура закрылась — панель вернулась.
    page.evaluate("() => document.activeElement.blur()")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function("() => getComputedStyle(document.getElementById('bottom-nav')).visibility === 'visible'")


def test_autofocus_without_keyboard_keeps_bottom_nav(phone, e2e):
    """Поиск ставит фокус сам; окно не сжалось (клавиатуры нет) — панель на месте."""
    page = phone(e2e.ids["boss"])
    page.click("#search-btn")
    page.wait_for_selector("#search-input")
    page.focus("#search-input")
    page.wait_for_timeout(400)
    assert _nav_visibility(page) == "visible", "без клавиатуры уйти с экрана поиска было бы некуда"
    go(page, "money")


# ─── Прокрутка при смене вида ────────────────────────────────────────────────


def test_new_view_starts_at_top_not_under_header(phone, e2e, tmp_path, no_rate_limit):
    """Прокрутка прежнего списка не переезжает на вкладку и карточку.

    С площадки: «Подтвердить» пролистали, нажали «Долги» — от ряда вкладок под
    шапкой осталась одна нижняя кромка; из пролистанного списка открыли
    карточку — её верх наполовину под шапкой. Клиент сообщает высоту чуть
    больше окна (tg_extra_height): тогда страница не бывает короче окна, и
    прокрутка сама в ноль не сбрасывается.
    """
    _seed_layout(e2e, tmp_path)
    # Менеджер: у него «Подтвердить» — вкладка «Денег» (у руководства — «Решения»).
    page = phone(e2e.ids["mgr"], height=520, tg_extra_height=80)
    go(page, "money")
    tab(page, "confirm")
    settled(page)
    page.wait_for_selector("#money-body .debts-list")
    page.wait_for_function("() => document.documentElement.scrollHeight > innerHeight + 200")
    page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_function("() => window.scrollY > 100")
    tab(page, "debts")
    page.wait_for_selector(".debts-header .seg")
    settled(page)
    seg_top, bar_bottom = page.evaluate(
        "() => [document.querySelector('#content .seg-row .seg').getBoundingClientRect().top,"
        " document.querySelector('.topbar').getBoundingClientRect().bottom]"
    )
    assert seg_top >= bar_bottom, f"вкладки раздела под шапкой: {seg_top} < {bar_bottom}"

    go(page, "stock")
    tab(page, "machines")
    settled(page)
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    page.locator("#content [data-machine]").last.click()
    page.wait_for_selector("#content [data-mact]")
    settled(page)
    assert page.evaluate("() => window.scrollY") == 0
