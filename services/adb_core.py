"""
Async DB core — фундамент миграции `async_db → asyncpg` (задача #21, Stage 0).

Единый async-интерфейс поверх ДВУХ бэкендов:
  • asyncpg  — нативный async-драйвер Postgres (прод), пул коннектов;
  • aiosqlite — async SQLite (тесты/локалка), чтобы ТОТ ЖЕ async-код-путь
    покрывался существующими тестами без реального Postgres.

Конвенция запросов: пишем в asyncpg-нотации позиционных плейсхолдеров
`$1, $2, …`. Для SQLite транслируем `$N → ?` с раскрытием повторов
(asyncpg допускает повтор `$1`; sqlite использует последовательные `?`).
Любая строка результата нормализуется в обычный dict.

Почему двухбэкендная абстракция, а не «просто asyncpg»: asyncpg говорит
только с Postgres, а весь локал-дев и 500+ тестов работают на SQLite.
aiosqlite даёт async-SQLite, поэтому один и тот же async-API тестируется
на SQLite и работает на Postgres в проде.

Stage 0 НЕ трогает services.database (sync-путь жив) и ничего не
импортирует этот модуль в рантайме — это фундамент + конвенция + тесты.
Следующие стадии мигрируют функции батчами и подключают lifecycle
(init_pool/close_pool) в bot.py и webapp.

ВНИМАНИЕ: env (DATABASE_URL/DB_PATH) читаем НА ВЫЗОВЕ, а не на импорте —
тесты подменяют их через monkeypatch (isolated_db).
"""

from __future__ import annotations

import os
import re
import tempfile
from contextlib import asynccontextmanager
from typing import Any

_PARAM_RE = re.compile(r"\$(\d+)")

# asyncpg-пул (ленивая инициализация). Размеры — как у psycopg2-пула.
_pg_pool: Any = None


def _use_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL", ""))


def _db_path() -> str:
    return os.environ.get("DB_PATH", os.path.join(tempfile.gettempdir(), "payments.db"))


def _to_sqlite(query: str, args: tuple) -> tuple[str, list]:
    """`$N` (asyncpg) → `?` (sqlite) с раскрытием повторных параметров.

    Каждое вхождение `$N` даёт свой `?` и подставляет args[N-1] в правильном
    порядке (повтор `$1` корректно раскрывается в два `?` с одним значением).
    """
    out_params: list = []

    def _repl(m: re.Match) -> str:
        out_params.append(args[int(m.group(1)) - 1])
        return "?"

    return _PARAM_RE.sub(_repl, query), out_params


def _rowcount_from_status(status: str) -> int:
    """asyncpg `execute` возвращает строку статуса ('UPDATE 3', 'INSERT 0 1',
    'DELETE 2') — вытаскиваем число затронутых строк (последний токен)."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError, AttributeError):
        return -1


# ─── lifecycle ────────────────────────────────────────────────────────────────


async def init_pool() -> Any:
    """Создать asyncpg-пул (идемпотентно). Для SQLite — no-op (None)."""
    global _pg_pool
    if not _use_postgres():
        return None
    if _pg_pool is None:
        import asyncpg

        _pg_pool = await asyncpg.create_pool(
            os.environ["DATABASE_URL"],
            min_size=int(os.environ.get("PG_POOL_MIN", "1")),
            max_size=int(os.environ.get("PG_POOL_MAX", "10")),
        )
    return _pg_pool


async def close_pool() -> None:
    global _pg_pool
    if _pg_pool is not None:
        await _pg_pool.close()
        _pg_pool = None


# ─── read/exec примитивы ────────────────────────────────────────────────────────


async def fetch(query: str, *args: Any) -> list[dict]:
    """SELECT → список dict'ов (возможно пустой)."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]

    import aiosqlite

    sql, params = _to_sqlite(query, args)
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetchrow(query: str, *args: Any) -> dict | None:
    """Первая строка как dict или None."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
        return dict(row) if row is not None else None

    import aiosqlite

    sql, params = _to_sqlite(query, args)
    async with aiosqlite.connect(_db_path()) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(sql, params) as cur:
            row = await cur.fetchone()
    return dict(row) if row is not None else None


async def fetchval(query: str, *args: Any) -> Any:
    """Первый столбец первой строки (или None)."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    import aiosqlite

    sql, params = _to_sqlite(query, args)
    async with aiosqlite.connect(_db_path()) as conn, conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def execute(query: str, *args: Any) -> int:
    """INSERT/UPDATE/DELETE/DDL. Возвращает число затронутых строк (rowcount;
    для asyncpg — распарсенное из строки статуса, -1 если не число)."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(query, *args)
        return _rowcount_from_status(status)

    import aiosqlite

    sql, params = _to_sqlite(query, args)
    async with aiosqlite.connect(_db_path()) as conn:
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur.rowcount


# ─── транзакция (для будущих write-стадий) ──────────────────────────────────────


@asynccontextmanager
async def transaction():
    """Async-транзакция с единым conn-объектом, у которого есть
    fetch/fetchrow/fetchval/execute. Коммит на успехе, rollback на исключении.

    Для asyncpg — `pool.acquire()` + `conn.transaction()`. Для SQLite —
    одна aiosqlite-сессия с commit/rollback. Объект-обёртка даёт одинаковый
    API на обоих бэкендах.
    """
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn, conn.transaction():
            yield _PgTxn(conn)
        return

    import aiosqlite

    conn = await aiosqlite.connect(_db_path())
    conn.row_factory = aiosqlite.Row
    try:
        yield _SqliteTxn(conn)
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()


class _PgTxn:
    __slots__ = ("_c",)

    def __init__(self, conn):
        self._c = conn

    async def fetch(self, q: str, *a: Any) -> list[dict]:
        return [dict(r) for r in await self._c.fetch(q, *a)]

    async def fetchrow(self, q: str, *a: Any) -> dict | None:
        r = await self._c.fetchrow(q, *a)
        return dict(r) if r is not None else None

    async def fetchval(self, q: str, *a: Any) -> Any:
        return await self._c.fetchval(q, *a)

    async def execute(self, q: str, *a: Any) -> int:
        return _rowcount_from_status(await self._c.execute(q, *a))


class _SqliteTxn:
    __slots__ = ("_c",)

    def __init__(self, conn):
        self._c = conn

    async def fetch(self, q: str, *a: Any) -> list[dict]:
        sql, params = _to_sqlite(q, a)
        async with self._c.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def fetchrow(self, q: str, *a: Any) -> dict | None:
        sql, params = _to_sqlite(q, a)
        async with self._c.execute(sql, params) as cur:
            r = await cur.fetchone()
        return dict(r) if r is not None else None

    async def fetchval(self, q: str, *a: Any) -> Any:
        sql, params = _to_sqlite(q, a)
        async with self._c.execute(sql, params) as cur:
            r = await cur.fetchone()
        return r[0] if r else None

    async def execute(self, q: str, *a: Any) -> int:
        sql, params = _to_sqlite(q, a)
        cur = await self._c.execute(sql, params)
        return cur.rowcount
