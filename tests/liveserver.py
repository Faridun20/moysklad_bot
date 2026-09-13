"""Живой uvicorn в том же процессе + перехваченный Telegram — общий каркас.

Им пользуются два слоя: E2E (`tests/e2e`, сверху Chromium) и нагрузочные
сценарии (`tests/perf`, сверху httpx). Сервер поднимается ПОТОКОМ, а не
subprocess'ом: иначе monkeypatch границ (подпись initData, исходящие в
Telegram) не достал бы до него.

Мокается только граница с внешним миром (конвенция CLAUDE.md):
* `verify_init_data` — initData = user_id строкой, роли настоящие;
* `get_notify_bot` / `tg_send_message` / `aget_notify_recipients` — в память.
Всё остальное — БД, роли, лимитер, warehouse, PDF — настоящее.
"""

from __future__ import annotations

import asyncio
import importlib
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any


def run_async(coro):
    """Выполнить корутину из синхронного теста/фикстуры.

    НЕ `asyncio.run` напрямую: sync-API Playwright держит в главном потоке
    работающий event loop всё время, пока открыт браузер, и `asyncio.run` там
    падает с «cannot be called from a running event loop». Отдельный поток со
    своим loop'ом работает и под браузером, и без него.
    """
    box: dict = {}

    def _target():
        try:
            box["v"] = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001 — пробрасываем как есть
            box["e"] = e

    t = threading.Thread(target=_target, name="live-async")
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box.get("v")


def free_port() -> int:
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


class LiveServer:
    """Что получает тест: адрес сервера, БД, id ролей, перехваченный Telegram."""

    def __init__(self, base_url: str, db, ids: dict, bot: FakeBot, pushes: list):
        self.base_url = base_url
        self.db = db
        self.ids = ids
        self.bot = bot
        self.pushes = pushes

    def rows(self, sql: str, params=()) -> list[dict]:
        with self.db.get_conn() as conn:
            cur = self.db.get_cursor(conn)
            cur.execute(self.db.q(sql), params)
            return [dict(r) for r in cur.fetchall()]

    def exec(self, sql: str, params=()) -> None:
        with self.db.get_conn() as conn:
            cur = self.db.get_cursor(conn)
            cur.execute(self.db.q(sql), params)
            conn.commit()

    def run(self, coro):
        return run_async(coro)


ROLE_IDS = {"admin": 1, "boss": 100, "mgr": 200, "keeper": 400, "book": 500}


def seed_basic(db) -> dict[str, Any]:
    """Роли, один товар на складе (20 шт.) и один клиент — минимум для продажи."""
    from services import container_receipt, warehouse

    ids: dict[str, Any] = dict(ROLE_IDS)
    db.set_role(ids["admin"], "admin_user", "Admin", "admin")
    db.set_role(ids["boss"], "boss_user", "Boss", "boss")
    db.set_role(ids["mgr"], "mgr_user", "Manager", "manager")
    db.set_role(ids["keeper"], "keeper_user", "Keeper", "warehouse_keeper")
    db.set_role(ids["book"], "book_user", "Book", "bookkeeper")

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
    return ids


@contextmanager
def live_server(isolated_db, monkeypatch, *, seed: bool = True):
    """Контекст: сервер на свободном порту, засеянная БД, перехваченный Telegram."""
    import services.notifier as notifier
    import services.rate_limit as rate_limit
    import services.roles as roles
    import services.warehouse as warehouse
    import uvicorn
    import webapp.server as server

    importlib.reload(roles)
    importlib.reload(warehouse)
    rate_limit.reset()

    db = isolated_db
    ids = seed_basic(db) if seed else dict(ROLE_IDS)

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

    port = free_port()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, name="live-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("live server: uvicorn не поднялся за 15 с")
        time.sleep(0.05)

    try:
        yield LiveServer(f"http://127.0.0.1:{port}", db, ids, bot, pushes)
    finally:
        srv.should_exit = True
        thread.join(timeout=10)
