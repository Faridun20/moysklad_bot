"""Perf-слой: счётчик SQL, живой сервер без браузера, бенчмарки.

Три вещи, которых нет в остальных тестах:

* `query_counter` — считает SQL-запросы В ОБОИХ слоях: синхронном
  (`database._TimedCursor.execute`) и асинхронном (`aiosqlite.Connection.execute`,
  через него идёт всё в `adb_core`). Главный потребитель — тесты на N+1: не
  «сколько запросов», а «растёт ли число запросов с числом строк».
* `live` — тот же uvicorn-в-потоке, что у E2E (`tests/liveserver.py`), но
  сверху httpx, а не Chromium: нагрузочным сценариям браузер не нужен, а
  параллельность в нём и не получить.
* `benchmark` — pytest-benchmark. Порогов по времени НЕТ намеренно: на CI-раннере
  они шумят и валят сборку из-за соседа по машине. Бенчмарки дают цифры в
  отчёт, регрессии сложности ловят счётчики запросов и тесты масштабирования.

Запуск: `pytest tests/perf -m perf`. В основной pytest-гейт слой не входит.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/perf" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.perf)


class QueryCounter:
    def __init__(self) -> None:
        self.sync = 0
        self.async_ = 0
        self.connects = 0
        self.statements: list[str] = []

    @property
    def total(self) -> int:
        return self.sync + self.async_

    def reset(self) -> None:
        self.sync = self.async_ = self.connects = 0
        self.statements.clear()

    def __repr__(self) -> str:
        return f"QueryCounter(total={self.total}, sync={self.sync}, async={self.async_}, connects={self.connects})"


@pytest.fixture
def query_counter(monkeypatch) -> QueryCounter:
    import aiosqlite

    from services import database

    qc = QueryCounter()

    orig_sync = database._TimedCursor.execute

    def sync_execute(self, query, params=None):
        qc.sync += 1
        qc.statements.append(" ".join(str(query).split())[:80])
        return orig_sync(self, query, params)

    monkeypatch.setattr(database._TimedCursor, "execute", sync_execute)

    orig_async = aiosqlite.Connection.execute

    def async_execute(self, sql, parameters=None):
        qc.async_ += 1
        qc.statements.append(" ".join(str(sql).split())[:80])
        return orig_async(self, sql, parameters)

    monkeypatch.setattr(aiosqlite.Connection, "execute", async_execute)

    orig_connect = aiosqlite.connect

    def connect(*a, **kw):
        qc.connects += 1
        return orig_connect(*a, **kw)

    monkeypatch.setattr(aiosqlite, "connect", connect)
    return qc


@pytest.fixture
def live(isolated_db, monkeypatch):
    """Живой сервер (см. tests/liveserver.py) без браузера."""
    from tests.liveserver import live_server

    with live_server(isolated_db, monkeypatch) as srv:
        yield srv


@pytest.fixture
def api_env(isolated_db, monkeypatch):
    """FastAPI TestClient + засеянная БД — для счётчиков запросов и размеров ответа."""
    import importlib

    from fastapi.testclient import TestClient

    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server
    from tests.liveserver import seed_basic

    importlib.reload(roles)
    rate_limit.reset()
    ids = seed_basic(isolated_db)
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"} if str(s).isdigit() else None,
    )

    async def _no_bot():
        return None

    monkeypatch.setattr(server, "get_notify_bot", _no_bot)
    client = TestClient(server.app)
    return client, isolated_db, ids
