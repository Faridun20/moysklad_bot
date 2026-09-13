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

import asyncio
import importlib
import os
import socket
import threading
import time
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


def run_async(coro):
    """Выполнить корутину из теста/фикстуры.

    НЕ `asyncio.run` напрямую: sync-API Playwright держит в главном потоке
    работающий event loop (greenlet поверх asyncio) всё время, пока открыт
    браузер, и `asyncio.run` там падает с «cannot be called from a running
    event loop». Отдельный поток со своим loop'ом — единственный способ
    сосуществовать с ним, не переписывая тесты на async.
    """
    box: dict = {}

    def _target():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 — пробрасываем как есть
            box["e"] = e

    t = threading.Thread(target=_target, name="e2e-async")
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeBot:
    """Граница с Telegram: всё исходящее — в память."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.documents: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        self.messages.append({"chat_id": chat_id, "text": text, **kw})

    async def send_document(self, chat_id, document, **kw):
        self.documents.append({"chat_id": chat_id, **kw})


class E2E:
    """Что получает тест: адрес сервера, БД, id ролей, перехваченный Telegram."""

    def __init__(self, base_url: str, db, ids: dict, bot: FakeBot, pushes: list):
        self.base_url = base_url
        self.db = db
        self.ids = ids
        self.bot = bot
        self.pushes = pushes

    # ── чтение состояния БД в сценариях ──
    def rows(self, sql: str, params=()):
        with self.db.get_conn() as conn:
            cur = self.db.get_cursor(conn)
            cur.execute(self.db.q(sql), params)
            return [dict(r) for r in cur.fetchall()]

    def run(self, coro):
        return run_async(coro)


@pytest.fixture
def e2e(isolated_db, monkeypatch) -> E2E:
    """Живой сервер на свободном порту + засеянные роли, товар, склад, клиент."""
    import services.rate_limit as rate_limit
    import services.roles as roles
    import services.notifier as notifier
    import services.warehouse as warehouse
    import uvicorn
    import webapp.server as server

    importlib.reload(roles)
    importlib.reload(warehouse)
    rate_limit.reset()

    db = isolated_db
    ids = {"admin": 1, "boss": 100, "mgr": 200, "keeper": 400, "book": 500}
    db.set_role(ids["admin"], "admin_user", "Admin", "admin")
    db.set_role(ids["boss"], "boss_user", "Boss", "boss")
    db.set_role(ids["mgr"], "mgr_user", "Manager", "manager")
    db.set_role(ids["keeper"], "keeper_user", "Keeper", "warehouse_keeper")
    db.set_role(ids["book"], "book_user", "Book", "bookkeeper")

    # Товар на складе и клиент — минимум, с которым можно оформить продажу.
    from services import container_receipt

    pid = run_async(container_receipt.create_product("Кабель ВВГ 3x2.5"))["product_id"]
    wid = run_async(warehouse.default_warehouse_id())
    run_async(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 20, "price_cents": None}],
    ))
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"),
            ("ООО Ромашка", "customer", "+998901234567", db.now_str()),
        )
        conn.commit()
    ids["product"] = pid
    ids["warehouse"] = wid

    # ── границы с Telegram ──
    bot = FakeBot()
    pushes: list[dict] = []

    async def _get_bot():
        return bot

    async def _send(uid, text, **kw):
        pushes.append({"uid": uid, "text": text, **kw})
        return True

    async def _recipients():
        return [ids["boss"]]

    monkeypatch.setattr(server, "get_notify_bot", _get_bot)
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"}
        if str(init_data).isdigit() else None,
    )
    monkeypatch.setattr(notifier, "tg_send_message", _send)
    monkeypatch.setattr(notifier, "aget_notify_recipients", _recipients)

    # ── сервер в потоке ──
    port = _free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, name="e2e-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("E2E: uvicorn не поднялся за 15 с")
        time.sleep(0.05)

    yield E2E(f"http://127.0.0.1:{port}", db, ids, bot, pushes)

    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def open_app(browser, e2e):
    """`open_app(user_id)` → страница WebApp, залогиненная этим пользователем."""
    contexts = []

    def _open(user_id: int) -> Page:
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        contexts.append(ctx)
        page = ctx.new_page()
        page.set_default_timeout(E2E_TIMEOUT_MS)
        stub = _TG_STUB % {"init_data": repr(str(user_id))}
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
