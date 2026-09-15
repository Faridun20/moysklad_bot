"""Роль при промахе кэша читается В ПОТОКЕ, а не в event loop.

`_authorize`/`get_role`/`is_boss` синхронные и зовутся из async-кода. При
промахе кэша (раз в 30 с на пользователя) SELECT шёл прямо в потоке loop'а, а
при исчерпанном пуле Postgres `_pool_getconn` ещё и спал до 10 с — весь процесс
(WebApp и бот) стоял. Проверяем по факту: в каком потоке исполняется чтение
роли, — а не по устройству кода.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _where() -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "thread"
    return "loop"


@pytest.fixture
def spy_roles(isolated_db, monkeypatch):
    import services.roles as roles

    importlib.reload(roles)
    calls: list[str] = []

    def fake_read(uid):
        calls.append(_where())
        return ("boss", False)

    monkeypatch.setattr(roles, "_db_role_and_deactivation", fake_read)
    return roles, calls


def test_webapp_cache_miss_reads_role_off_the_loop(spy_roles, monkeypatch):
    roles, calls = spy_roles
    import webapp.server as server

    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)

    resp = client.post("/api/currency/rates", json={"initData": "777"})

    assert resp.status_code == 200, resp.text
    assert calls, "роль не читалась вовсе — тест ничего не проверил"
    assert calls == ["thread"], f"чтение роли в event loop: {calls}"


def test_webapp_invalid_signature_does_not_touch_cache(spy_roles, monkeypatch):
    roles, calls = spy_roles
    import webapp.server as server

    monkeypatch.setattr(server, "verify_init_data", lambda init_data: None)
    resp = TestClient(server.app).post("/api/currency/rates", json={"initData": "bad"})
    assert resp.status_code == 401
    assert calls == [] and roles._auth_cache == {}


def test_webapp_body_reaches_handler_intact(spy_roles, monkeypatch):
    """Middleware читает тело сам — ручка обязана получить его целиком."""
    roles, calls = spy_roles
    import webapp.server as server

    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    resp = TestClient(server.app).post(
        "/api/currency/rates/set",
        json={"initData": "777", "currency_code": "UZS", "rate_to_base": 1 / 12600},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["currency_code"] == "UZS"


def test_entry_close_to_expiry_is_refreshed_ahead(spy_roles):
    roles, calls = spy_roles
    roles._auth_cache[5] = (time.monotonic() - (roles._AUTH_TTL - 1), "manager", False)
    asyncio.run(roles.warm_auth_cache(5))
    assert calls == ["thread"]
    assert roles.cached_role(5) == "boss"
    assert calls == ["thread"], "после прогрева синхронный путь не должен читать БД"


def test_bot_middleware_warms_cache_before_handler(spy_roles):
    roles, calls = spy_roles
    from bot import RateLimitMiddleware

    seen: list[str] = []

    async def handler(event, data):
        seen.append(roles.cached_role(data["event_from_user"].id))

    async def go():
        await RateLimitMiddleware(max_calls=100)(
            handler, SimpleNamespace(), {"event_from_user": SimpleNamespace(id=4242)}
        )

    asyncio.run(go())
    assert seen == ["boss"]
    assert calls == ["thread"], f"чтение роли в event loop: {calls}"


# ─── Пул Postgres: из потока loop'а не спим ──────────────────────────────────


class _ExhaustedPool:
    def __init__(self):
        self.attempts = 0

    def getconn(self):
        from psycopg2 import pool

        self.attempts += 1
        raise pool.PoolError("connection pool exhausted")


def test_exhausted_pool_in_event_loop_fails_fast_without_sleep(isolated_db, monkeypatch):
    from psycopg2 import pool as pg_pool

    db = isolated_db

    def no_sleep(_s):
        raise AssertionError("time.sleep в потоке event loop")

    monkeypatch.setattr(db.time, "sleep", no_sleep)
    fake = _ExhaustedPool()

    async def go():
        db._acquire_pooled_conn(fake)

    with pytest.raises(pg_pool.PoolError):
        asyncio.run(go())
    assert fake.attempts == 1


def test_exhausted_pool_in_worker_thread_still_waits(isolated_db, monkeypatch):
    from psycopg2 import pool as pg_pool

    db = isolated_db
    monkeypatch.setattr(db, "_PG_POOL_ACQUIRE_TIMEOUT", 0.12)
    monkeypatch.setattr(db, "_PG_POOL_ACQUIRE_INTERVAL", 0.03)
    fake = _ExhaustedPool()

    async def go():
        await asyncio.to_thread(db._acquire_pooled_conn, fake)

    with pytest.raises(pg_pool.PoolError):
        asyncio.run(go())
    assert fake.attempts >= 3, "в worker-потоке ожидание коннекта сохранено"
