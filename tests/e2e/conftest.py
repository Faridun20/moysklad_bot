"""E2E-каркас: настоящий браузер против живого сервера и настоящей БД.

Чем это отличается от остальных тестов проекта:

* API-тесты (`tests/test_*_api.py`) идут через FastAPI TestClient — HTTP без
  сети, фронта нет вовсе. jsdom-smoke (`webapp/static/__tests__`) исполняет
  app.js в псевдо-браузере с ЗАГЛУШКОЙ `api()` — сервера нет вовсе. Между
  ними дыра: контракт «что фронт шлёт ↔ что сервер ждёт ↔ что фронт рисует из
  ответа» не проверяет никто. Здесь — Chromium грузит index.html с живого
  uvicorn, app.js делает настоящие fetch, сервер пишет в настоящую SQLite.

* Мокается ТОЛЬКО граница с внешним миром (конвенция CLAUDE.md):
  - `verify_init_data` — подпись Telegram: initData = user_id строкой, как в
    API-тестах; роли при этом настоящие, из `user_roles`;
  - `telegram-web-app.js` — SDK Telegram перехватывается на уровне сети
    (`page.route`) и подменяется заглушкой `window.Telegram.WebApp`: в CI
    доступа к telegram.org нет, а тест не должен зависеть от него и в
    принципе;
  - `get_notify_bot` / `tg_send_message` — исходящие в Telegram сообщения и
    документы собираются в память.
  Всё остальное — БД, роли, лимитер, warehouse, PDF — настоящее.

* uvicorn поднимается В ТОМ ЖЕ процессе (поток), а не subprocess'ом: иначе
  monkeypatch границ не достал бы до сервера, и пришлось бы мокать через env
  — а `DEV_AUTH_BYPASS` даёт одного пользователя на процесс, тогда как
  сценарии ходят под разными ролями.

Запуск:  pytest tests/e2e -m e2e
Без Playwright или Chromium пакет пропускается целиком, а не падает;
`E2E_REQUIRED=1` (CI-job e2e) превращает пропуск в падение.
Chromium: `python -m playwright install chromium` (CI) либо готовый бинарь
через `PW_CHROMIUM_PATH` (в облачных окружениях он уже лежит в
/opt/pw-browsers).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright", reason="E2E: пакет playwright не установлен")
from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402

E2E_TIMEOUT_MS = 15_000

# Заглушка Telegram WebApp SDK. Повторяет то, что app.js трогает на верхнем
# уровне и в сценариях: initData, диалоги, MainButton/BackButton, haptic.
# Диалоги подтверждения отвечают «да» и складывают тексты в window.__tgAlerts
# — по ним сценарий проверяет, что пользователь увидел («Заявка отправлена»).
# MainButton настоящая по форме: onClick запоминает обработчик, а
# window.__tgMainClick() его нажимает — ровно так диалог количества и
# добавляет товар в заявку.
_TG_STUB = """
(function () {
  const noop = function () {};
  const alerts = [];
  let mainHandler = null;
  window.__tgAlerts = alerts;
  window.__tgMainClick = function () { if (mainHandler) mainHandler(); };
  window.Telegram = { WebApp: {
    initData: %(init_data)s,
    initDataUnsafe: {},
    platform: 'e2e', version: '8.0',
    colorScheme: 'light', themeParams: {},
    ready: noop, expand: noop, close: noop, onEvent: noop, offEvent: noop,
    enableClosingConfirmation: noop, disableClosingConfirmation: noop,
    setHeaderColor: noop, setBackgroundColor: noop,
    showAlert: function (m, cb) { alerts.push(String(m)); if (cb) cb(); },
    showConfirm: function (m, cb) { alerts.push('confirm:' + m); if (cb) cb(true); },
    showPopup: function (p, cb) { alerts.push('popup'); if (cb) cb(); },
    HapticFeedback: { impactOccurred: noop, notificationOccurred: noop, selectionChanged: noop },
    MainButton: {
      text: '', isVisible: false,
      setText: function (t) { this.text = t; }, show: function () { this.isVisible = true; },
      hide: function () { this.isVisible = false; },
      onClick: function (f) { mainHandler = f; }, offClick: function () { mainHandler = null; },
      showProgress: noop, hideProgress: noop, enable: noop, disable: noop,
    },
    BackButton: {
      isVisible: false, show: function () { this.isVisible = true; },
      hide: function () { this.isVisible = false; }, onClick: noop, offClick: noop,
    },
  } };
})();
"""


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/e2e" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)


def _chromium_executable() -> str | None:
    """Готовый бинарь Chromium, если он есть; иначе — тот, что поставил Playwright."""
    env = os.environ.get("PW_CHROMIUM_PATH")
    if env:
        return env
    default = Path("/opt/pw-browsers/chromium")
    return str(default) if default.exists() else None


@pytest.fixture(scope="session")
def browser() -> Browser:
    with sync_playwright() as pw:
        kwargs = {"headless": True}
        exe = _chromium_executable()
        if exe:
            kwargs["executable_path"] = exe
        try:
            b = pw.chromium.launch(**kwargs)
        except Exception as e:  # pragma: no cover — окружение без браузера
            # Локально без браузера — пропуск. В CI-job'е e2e (E2E_REQUIRED=1)
            # пропуск был бы зелёной галочкой над непрогнанными тестами.
            if os.environ.get("E2E_REQUIRED"):
                raise
            pytest.skip(f"E2E: Chromium не запустился: {e}")
        yield b
        b.close()


# Каркас живого сервера общий с нагрузочными тестами (tests/perf).
from tests.liveserver import FakeBot, LiveServer, live_server, run_async  # noqa: E402,F401

E2E = LiveServer  # прежнее имя в сценариях


@pytest.fixture
def e2e(isolated_db, monkeypatch) -> E2E:
    """Живой сервер на свободном порту + засеянные роли, товар, склад, клиент."""
    with live_server(isolated_db, monkeypatch) as srv:
        yield srv


@pytest.fixture
def open_app(browser, e2e):
    """`open_app(user_id)` → страница WebApp, залогиненная этим пользователем."""
    contexts = []

    def _open(user_id: int, init_data: str | None = None) -> Page:
        """`init_data` — подменить подпись (для сценария «initData не прошла»)."""
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(E2E_TIMEOUT_MS)
        stub = _TG_STUB % {"init_data": repr(str(user_id) if init_data is None else init_data)}
        page.route(
            "https://telegram.org/js/telegram-web-app.js",
            lambda route: route.fulfill(status=200, content_type="application/javascript", body=stub),
        )
        page.goto(e2e.base_url + "/")
        # init() закончился: либо навигация построена, либо экран отказа.
        page.wait_for_selector("#bottom-nav .nav-item, .error-card, .empty-state-title", state="attached")
        return page

    yield _open
    for ctx in contexts:
        ctx.close()


def alerts(page: Page) -> list[str]:
    return page.evaluate("window.__tgAlerts")


def go(page: Page, screen: str) -> None:
    """Открыть раздел и дождаться его отрисовки.

    Раздел в нижней панели — её кнопкой; раздел, не влезший в панель (пятый у
    руководства и менеджера), — через «Меню» → шторку, как это делает человек.
    Текущий раздел читаем из `#bottom-nav[data-current]`: у раздела из шторки
    подсвечена «Меню», а не кнопка раздела.
    """
    item = page.locator(f'#bottom-nav .nav-item[data-screen="{screen}"]')
    if item.count():
        item.click()
    else:
        page.click('#bottom-nav .nav-item[data-action="menu"]')
        page.click(f'#nav-drawer.is-open .nav-link--section[data-screen="{screen}"]')
    page.wait_for_function(
        "(s) => document.getElementById('bottom-nav')?.dataset.current === s", arg=screen
    )


def current_screen(page: Page) -> str:
    """Текущий раздел: `#bottom-nav[data-current]`.

    По активной кнопке панели его уже не узнать: раздел из шторки «Меню»
    подсвечивает саму «Меню», а кнопки раздела в панели нет.
    """
    return page.evaluate("() => document.getElementById('bottom-nav')?.dataset.current || ''")


def nav_screens(page: Page) -> list[str]:
    """Все разделы роли по порядку: кнопки панели и, если есть «Меню», разделы шторки.

    Шторку открываем и закрываем кликом в странице — содержимое её рисуется
    при открытии, до этого разделов вне панели в DOM нет.
    """
    return page.evaluate(
        """() => {
          const bar = [...document.querySelectorAll('#bottom-nav .nav-item[data-screen]')]
            .map(e => e.dataset.screen);
          const menu = document.querySelector('#bottom-nav [data-action="menu"]');
          if (!menu) return bar;
          menu.click();
          const all = [...document.querySelectorAll('#nav-drawer .nav-link--section')]
            .map(e => e.dataset.screen);
          document.querySelector('#nav-drawer .nav-drawer-close').click();
          return all;
        }"""
    )


def tab(page: Page, key: str) -> None:
    """Нажать вкладку раздела (`.seg-item[data-sect]`) и дождаться подсветки."""
    page.click(f'.seg-item[data-sect="{key}"]')
    page.wait_for_function(
        "(k) => document.querySelector('.seg-item.active[data-sect]')?.dataset.sect === k", arg=key
    )


def settled(page: Page) -> None:
    """Дождаться, пока с экрана уйдут скелетоны и индикаторы загрузки."""
    page.wait_for_function(
        "() => !document.querySelector('#content .sk-card, #content .sk-hero, #content .sk-label')"
        " && !Array.from(document.querySelectorAll('#content .loader'))"
        "        .some(el => /загру|ищу|счита/i.test(el.textContent || ''))"
    )


def toast_text(page: Page) -> str:
    return " | ".join(page.eval_on_selector_all(".toast", "els => els.map(e => e.textContent)"))


def sheet_fill(page: Page, values: dict) -> None:
    """Заполнить поля шторки `openMachineSheet` (id = ms-f-<key>)."""
    for key, val in values.items():
        page.fill(f"#ms-f-{key}", str(val))


def pay_order(e2e: E2E, order_id: int, parts: list[tuple] | None = None, *, uid: int | None = None) -> dict:
    """Разбивка «как получены деньги» через сервис (services.order_payments).

    `parts` — [(способ, сумма[, валюта])]; по умолчанию вся сумма к оплате
    картой (ожидающий платёж, который подтверждает руководитель — ровно то, что
    раньше делал автоплатёж одобрения «оплаты сразу»).
    """
    from services import order_payments

    uid = uid or e2e.ids["mgr"]
    order = e2e.rows("SELECT currency, payment_type FROM orders WHERE id = ?", (order_id,))[0]
    cur = order["currency"] or "USD"
    if parts is None:
        gap = e2e.run(order_payments.payment_gap_cents([order_id]))[order_id]
        parts = [("card", gap / 100)]
    rows = [{"method": p[0], "amount": str(p[1]), "currency": p[2] if len(p) > 2 else cur} for p in parts]
    # Карта/перечисление — с тестовой картой/счётом «куда поступили».
    from tests.conftest import with_pay_accounts

    rows = with_pay_accounts(rows, run=e2e.run)
    actor = order_payments.Actor(user_id=uid, name="Manager", role="manager")
    return e2e.run(order_payments.record_payment_parts(order_id, actor, rows))


def pick_account(page: Page, trigger, kind: str, spec: dict | None = None) -> None:
    """Лист «На какую карту / На какой счёт», открытый кнопкой `trigger`: найти
    запись по последним цифрам и владельцу, нет — завести кнопкой «Новая
    карта/счёт» (`spec` — поля формы; по умолчанию тестовые из tests/conftest)."""
    from tests.conftest import TEST_BANK, TEST_CARD

    spec = dict(spec or (TEST_CARD if kind == "card" else TEST_BANK))
    spec.pop("kind", None)
    trigger.click()
    picker = page.locator(".c-overlay.pay-account-picker").last
    picker.wait_for()
    tail = spec.get("card_last4") or str(spec.get("account_number", ""))[-4:]
    picker.locator("#ms-f-search").fill(tail)
    page.wait_for_timeout(200)  # фильтр пикера ждёт паузу в наборе
    holder = spec.get("holder", "")
    match = picker.locator("[data-pick]", has_text=holder) if holder else picker.locator("[data-pick]")
    if match.count():
        match.first.click()
        picker.locator("#ms-submit").click()
    else:
        picker.locator(".picker-add").click()
        form = page.locator(".c-overlay.pay-account-form").last
        form.wait_for()
        for key, value in spec.items():
            field = form.locator(f"#ms-f-{key}")
            if field.count() and field.get_attribute("type") != "hidden":
                field.fill(str(value))
        form.locator("#ms-submit").click()
    page.wait_for_function(
        "() => !document.querySelector('.c-overlay.pay-account-picker, .c-overlay.pay-account-form')"
    )


def choose_pay_account(page: Page, part_index: int, kind: str, spec: dict | None = None) -> None:
    """«Куда поступили» в строке `part_index` формы оплаты."""
    part = page.locator(".c-overlay .pay-part").nth(part_index)
    pick_account(page, part.locator(".pay-part-account"), kind, spec)
    page.wait_for_function(
        "(i) => { const p = document.querySelectorAll('.c-overlay .pay-part')[i];"
        " const a = p && p.querySelector('.pay-part-account'); return !!a && a.dataset.accountId !== ''; }",
        arg=part_index,
    )


def pay_form(page: Page, open_selector: str, rows: list[tuple], *, submit: bool = True,
             accounts: dict[str, dict] | None = None) -> None:
    """Форма «Как получены деньги» в браузере: открыть кнопкой, заполнить строки
    [(способ, сумма[, валюта[, курс]])], отправить. Карта/перечисление без
    предложенной по умолчанию записи получают карту/счёт через лист выбора
    (`accounts` — {способ: поля формы}, иначе тестовые)."""
    page.click(open_selector)
    page.wait_for_selector(".c-overlay .pay-part")
    for i, row in enumerate(rows):
        if i > 0:
            page.click(".c-overlay .pay-add-part")
        part = page.locator(".c-overlay .pay-part").nth(i)
        part.locator(f'[data-pay-method="{row[0]}"]').click()
        if len(row) > 2 and row[2]:
            page.locator(".c-overlay .pay-part").nth(i).locator(f'[data-pay-cur="{row[2]}"]').click()
        part = page.locator(".c-overlay .pay-part").nth(i)
        part.locator(".pay-part-amount").fill(str(row[1]))
        if len(row) > 3 and row[3]:
            part.locator(".pay-part-rate").fill(str(row[3]))
        if row[0] in ("card", "bank"):
            chosen = part.locator(".pay-part-account").get_attribute("data-account-id")
            wanted = (accounts or {}).get(row[0])
            if not chosen or wanted:
                choose_pay_account(page, i, row[0], wanted)
    if submit:
        page.click(".c-overlay #ms-submit")


def require_decision(e2e: E2E, price: float = 100.0) -> None:
    """Заявки на кабель ждут решения руководителя: прайс вдвое выше цены
    заказа — скидка 50% выше порога (15%).

    Одобрение отгрузки не обязательно (`order_workflow.ship_order_now`): в
    «Решениях», очереди «Сегодня» и счётчике «Заявки на рассмотрении» числятся
    только заявки со скидкой выше порога или долгом сверх лимита. Сценарии
    экрана решений заводят заявку именно такой. Прайс — ещё и минимальная цена
    в редакторе (`/api/orders/add_item`): после этого вызова позицию дешевле
    через браузер не добавить."""
    ok, err = e2e.db.set_product_price(
        str(e2e.ids["product"]), "Кабель ВВГ 3x2.5", price * 2, None, "USD", e2e.ids["boss"]
    )
    assert ok, err


def seed_order(e2e: E2E, *, payment_type: str = "credit", due_date: str | None = "2030-01-15",
               qty: float = 2, price: float = 100.0, approve: bool = True,
               pay: str | None = "card", needs_decision: bool = False) -> dict:
    """Заказ менеджера на «Ромашку» через сервисы (не через браузер).

    Нужен сценариям, которые начинаются ПОСЛЕ продажи: оплата, сдача, возврат,
    долги. Путь через редактор уже покрыт своим тестом, повторять его в каждом
    из них — оплачивать полминуты браузера за то, что и так проверено.
    «Оплата сразу» после одобрения получает разбивку `pay` на всю сумму (как
    сделал бы менеджер перед отгрузкой); `pay=None` — без неё (форма в тесте).
    `needs_decision` — заявка со скидкой выше порога (`require_decision`): она
    ждёт руководителя в «Решениях»; без неё заявку отгружает сам менеджер.
    Одобрение (`approve`) такой заявки идёт с подтверждением скидки.
    Возвращает {order_id, req_id, counterparty_id}.
    """
    from services.order_workflow import approve_shipment_request, submit_order

    db, ids = e2e.db, e2e.ids
    if needs_decision:
        require_decision(e2e, price)
    cp = e2e.rows("SELECT id FROM counterparties ORDER BY id LIMIT 1")[0]["id"]
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, str(cp), "ООО Ромашка")
    db.add_order_item(oid, "Кабель ВВГ 3x2.5", "", qty, "м", price, product_id=ids["product"])
    res = e2e.run(submit_order(oid, ids["mgr"], "Manager", payment_type=payment_type, due_date=due_date))
    assert res.get("ok"), res
    if approve:
        # С подтверждением скидки: прайс выше цены мог поставить и другой
        # `seed_order(needs_decision=True)` того же сценария.
        ap = e2e.run(approve_shipment_request(res["req_id"], ids["boss"], "Boss", e2e.bot, discount_ack=True))
        assert ap.get("ok"), ap
        if payment_type == "paid" and pay:
            pay_order(e2e, oid, [(pay, qty * price)])
    return {"order_id": oid, "req_id": res["req_id"], "counterparty_id": cp}


def work_actions(e2e: E2E, user_id: int | None = None, on: bool = True) -> None:
    """Включить руководителю «Рабочие действия» (services.user_prefs) ДО открытия
    страницы. Решение владельца: работа менеджера (накладные, отгрузка, касса,
    лиды, моточасы, приёмка) у руководителя за этим выключателем. Сценарии,
    где руководитель делает работу менеджера, включают его явно — как человек
    включил бы в «Меню»; права ручек от него не зависят."""
    from services import user_prefs

    user_prefs.set_pref(user_id or e2e.ids["boss"], "work_actions", on)


@pytest.fixture
def boss_work_actions(e2e):
    """«Рабочие действия» включены у руководителя и админа.

    Модули со сценариями работы менеджера, которые исторически проходил
    руководитель (накладные, приёмка, касса, техника, канал), подключают это
    через `pytestmark = pytest.mark.usefixtures("boss_work_actions")`: с
    выключателем он делает их как раньше, права ручек те же. Вид руководителя
    по умолчанию (выключено) проверяют test_boss_ui.py и обход вёрстки."""
    work_actions(e2e, e2e.ids["boss"])
    work_actions(e2e, e2e.ids["admin"])


def open_confirmations(page: Page) -> None:
    """Где роль подтверждает оплаты, сдачи и возвраты: у руководства — экран
    «Решения», у остальных — «Деньги → Подтвердить»."""
    if page.locator('#bottom-nav .nav-item[data-screen="decisions"]').count():
        go(page, "decisions")
    else:
        go(page, "money")
        if page.locator('.seg-item[data-sect="confirm"]').count():
            tab(page, "confirm")
    settled(page)
