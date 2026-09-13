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
    """Нажать раздел в нижней панели и дождаться его отрисовки."""
    page.click(f'#bottom-nav .nav-item[data-screen="{screen}"]')
    page.wait_for_function(
        "(s) => document.querySelector('#bottom-nav .nav-item.active')?.dataset.screen === s", arg=screen
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


def seed_order(e2e: E2E, *, payment_type: str = "credit", due_date: str | None = "2030-01-15",
               qty: float = 2, price: float = 100.0, approve: bool = True) -> dict:
    """Заказ менеджера на «Ромашку» через сервисы (не через браузер).

    Нужен сценариям, которые начинаются ПОСЛЕ продажи: оплата, сдача, возврат,
    долги. Путь через редактор уже покрыт своим тестом, повторять его в каждом
    из них — оплачивать полминуты браузера за то, что и так проверено.
    Возвращает {order_id, req_id, counterparty_id}.
    """
    from services.order_workflow import approve_shipment_request, submit_order

    db, ids = e2e.db, e2e.ids
    cp = e2e.rows("SELECT id FROM counterparties ORDER BY id LIMIT 1")[0]["id"]
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, str(cp), "ООО Ромашка")
    db.add_order_item(oid, "Кабель ВВГ 3x2.5", "", qty, "м", price, product_id=ids["product"])
    res = e2e.run(submit_order(oid, ids["mgr"], "Manager", payment_type=payment_type, due_date=due_date))
    assert res.get("ok"), res
    if approve:
        ap = e2e.run(approve_shipment_request(res["req_id"], ids["boss"], "Boss", e2e.bot))
        assert ap.get("ok"), ap
    return {"order_id": oid, "req_id": res["req_id"], "counterparty_id": cp}
