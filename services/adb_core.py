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

import asyncio
import os
import re
import tempfile
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

_PARAM_RE = re.compile(r"\$(\d+)")


def _pg_args(args: tuple) -> tuple:
    """float-параметры → Decimal по КРАТЧАЙШЕЙ записи числа (`repr`).

    asyncpg кладёт Python float в NUMERIC-колонку двоичным значением целиком:
    2.3 уезжает как 2.29999999999999982236431605997495353221893310546875.
    Количества склада и заказов ради того и переведены в NUMERIC, чтобы не
    копить хвосты float, — а драйвер приносил их обратно на входе, и сумма
    дробных отгрузок снова не сходилась с «возвращено полностью». psycopg2
    (синхронный слой) пишет float литералом `2.3`, поэтому расходился только
    асинхронный путь. Decimal принимают и NUMERIC, и REAL/DOUBLE-параметры
    (через __float__), так что замена безопасна для любого типа параметра.
    `type(a) is float`, а не isinstance: bool и прочие подклассы не трогаем.
    """
    if not any(type(a) is float for a in args):
        return args
    return tuple(Decimal(repr(a)) if type(a) is float else a for a in args)


# ─── Сортировка и поиск по названию ────────────────────────────────────────────

# ICU-правило сравнения для русского. Postgres в образе на Alpine (musl) не
# умеет libc-локалей: `ORDER BY name` там сортирует по кодам символов —
# «Zeta, alfa, Ёлка, Абрикос». ICU-коллации в образ входят (initdb заводит их в
# pg_collation), и `ru-RU-x-icu` даёт «Абрикос, Ёлка, alfa, Zeta» без смены
# локали всей базы. Наличие коллации сверяет `startup_checks`.
NAME_COLLATION = "ru-RU-x-icu"


def order_by_name(column: str) -> str:
    """`column COLLATE "ru-RU-x-icu"` на Postgres, голая колонка на SQLite.

    SQLite ICU не несёт и неизвестный COLLATE отвергает запросом целиком,
    поэтому там сортировка остаётся бинарной — порядок проверяется на
    Postgres (TEST_PG_URL). Имя колонки приходит из кода, не от пользователя.
    """
    if _use_postgres():
        return f'{column} COLLATE "{NAME_COLLATION}"'
    return column


def name_search_sql(column: str) -> str:
    """Выражение для LIKE-поиска по названию: нижний регистр и ё→е.

    «Ёлка» в справочнике и «елка» в поиске (или наоборот) — одно слово: на
    телефоне «ё» набирают долгим нажатием и чаще не набирают вовсе.
    Нормализуются ОБЕ стороны — колонка здесь, ввод в `name_search_param`.
    `lower()` на SQLite переопределён Unicode-версией в обоих слоях,
    `replace()` встроенный в обеих базах.
    """
    return f"replace(lower({column}), 'ё', 'е')"


def normalize_search(text: str | None) -> str:
    """Пользовательский ввод к виду `name_search_sql`: нижний регистр, ё→е."""
    return (text or "").strip().lower().replace("ё", "е")


def name_search_param(text: str | None) -> str:
    """`%ввод%` для LIKE против `name_search_sql`."""
    return f"%{normalize_search(text)}%"

# asyncpg-пул (ленивая инициализация). Размеры — как у psycopg2-пула.
# Пул привязан к event loop'у, на котором создан. Храним этот loop, чтобы
# пересоздавать пул при смене loop'а (cron делает несколько asyncio.run;
# worker-тред поднимает свой loop) — иначе вызов пула с чужого/закрытого
# loop'а падает. См. init_pool.
_pg_pool: Any = None
_pg_pool_loop: asyncio.AbstractEventLoop | None = None


def _use_postgres() -> bool:
    return bool(os.environ.get("DATABASE_URL", ""))


# Сколько секунд ждать чужую пишущую транзакцию SQLite. По умолчанию у
# sqlite3 это 5 с; под параллельной нагрузкой (tests/perf: восемь одобрений
# разом, каждое с PDF внутри транзакции) очередь писателей длиннее, и
# одиночные запросы падали «database is locked». На Postgres не влияет.
_SQLITE_TIMEOUT = 30


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


# Таймауты сессии asyncpg-пула. Без них один зависший запрос (или транзакция,
# ждущая чужой FOR UPDATE / advisory-lock) держал соединение пула бесконечно, и
# после десятка таких WebApp вставал целиком: новые запросы ждали свободного
# соединения, которого не будет. statement_timeout — на ОДИН запрос, а не на
# транзакцию: длинная пачка коротких INSERT (перенос истории) его не задевает.
# Самые тяжёлые запросы пула — агрегаты аналитики и отчёта — укладываются в
# секунды. Схему и backfill'ы (`tasks.migrate`) гоняет синхронный слой
# (psycopg2), таймауты пула их не касаются. Разовому скрипту, которому нужно
# больше, — env: PG_STATEMENT_TIMEOUT_MS=0 снимает ограничение.
_DEFAULT_STATEMENT_TIMEOUT_MS = 30_000
_DEFAULT_LOCK_TIMEOUT_MS = 10_000


def _env_ms(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(0, value)


def pg_server_settings() -> dict[str, str]:
    """server_settings для asyncpg: таймауты запроса и ожидания блокировки."""
    return {
        "statement_timeout": str(_env_ms("PG_STATEMENT_TIMEOUT_MS", _DEFAULT_STATEMENT_TIMEOUT_MS)),
        # lock_timeout ограничивает и ожидание pg_advisory_xact_lock: очередь
        # на один заказ дольше 10 с — это зависание, а не нагрузка.
        "lock_timeout": str(_env_ms("PG_LOCK_TIMEOUT_MS", _DEFAULT_LOCK_TIMEOUT_MS)),
    }


async def init_pool() -> Any:
    """Создать/вернуть asyncpg-пул для ТЕКУЩЕГО event loop'а. Для SQLite — None.

    Loop-aware: если закэшированный пул привязан к другому loop'у (cron сделал
    новый asyncio.run; worker-тред поднял свой loop), старый пул рвём
    terminate() (синхронно, без await — его loop обычно уже закрыт) и создаём
    свежий на текущем loop'е. В долгоживущих bot/webapp loop один — пул
    создаётся однажды и переиспользуется.
    """
    global _pg_pool, _pg_pool_loop
    if not _use_postgres():
        return None

    loop = asyncio.get_running_loop()
    if _pg_pool is not None:
        if _pg_pool_loop is loop:
            return _pg_pool
        # Пул с другого loop'а — нельзя graceful-close без его loop'а; рвём.
        try:
            _pg_pool.terminate()
        except Exception:
            pass
        _pg_pool = None
        _pg_pool_loop = None

    import asyncpg

    _pg_pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"],
        min_size=int(os.environ.get("PG_POOL_MIN", "1")),
        max_size=int(os.environ.get("PG_POOL_MAX", "10")),
        server_settings=pg_server_settings(),
    )
    _pg_pool_loop = loop
    return _pg_pool


async def close_pool() -> None:
    global _pg_pool, _pg_pool_loop
    if _pg_pool is not None:
        try:
            await _pg_pool.close()
        except Exception:
            try:
                _pg_pool.terminate()
            except Exception:
                pass
        _pg_pool = None
        _pg_pool_loop = None


# ─── read/exec примитивы ────────────────────────────────────────────────────────


async def _register_sqlite_functions(conn) -> None:
    """Довесить aiosqlite-соединению те же функции, что есть у синхронного.

    Встроенный SQLite `LOWER()` — ASCII-only: «Иванов» так и остаётся «Иванов».
    В Postgres он Unicode-aware, поэтому LIKE-поиск по кириллице (имена
    клиентов, заметки к контейнерам) вёл себя на проде и в тестах ПО-РАЗНОМУ:
    там находил, здесь нет. `services.database.get_conn` эту функцию давно
    переопределяет — на асинхронной стороне её просто забыли, и расхождение
    всплывало как «поиск не работает» ровно там, где его писали по образцу.

    Соединение передаётся УЖЕ открытым: `Connection.__aenter__` делает
    `await self`, и повторное ожидание того же объекта вешает поток намертво.
    """
    await conn.create_function(
        "lower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True
    )


@asynccontextmanager
async def _sqlite_conn():
    """Открытое aiosqlite-соединение, чей поток гарантированно гаснет.

    У aiosqlite 0.20 каждое соединение — отдельный НЕ-daemon поток, который
    ждёт в очереди следующую команду, пока его не остановят. `connect()` гасит
    поток только на `Exception`, а `CancelledError` — `BaseException`: задачу
    сняли на середине открытия (фоновое уведомление или печатная форма, когда
    `asyncio.run` в тесте или портал TestClient закрывает loop), и поток
    остаётся ждать вечно. Интерпретатор на выходе ждёт такие потоки
    (`threading._shutdown`) — pytest печатал «N passed» и не завершался: шард
    локальной CI висел до таймаута. Поэтому: при любом прерывании открытия
    поток останавливаем сами, а сам поток — daemon, чтобы соединение, которое
    кто-то всё же не закрыл, не держало выход процесса.
    """
    import aiosqlite

    conn = aiosqlite.connect(_db_path(), timeout=_SQLITE_TIMEOUT)
    conn.daemon = True
    try:
        await conn  # старт потока + открытие; `__aenter__` делает то же самое
    except BaseException:
        conn._stop_running()  # как делает сам aiosqlite на Exception
        raise
    try:
        await _register_sqlite_functions(conn)
        conn.row_factory = aiosqlite.Row
        yield conn
    finally:
        # close() останавливает поток в finally — даже если его самого снимут.
        await conn.close()


async def fetch(query: str, *args: Any) -> list[dict]:
    """SELECT → список dict'ов (возможно пустой)."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *_pg_args(args))
        return [dict(r) for r in rows]

    sql, params = _to_sqlite(query, args)
    async with _sqlite_conn() as conn, conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetchrow(query: str, *args: Any) -> dict | None:
    """Первая строка как dict или None."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *_pg_args(args))
        return dict(row) if row is not None else None

    sql, params = _to_sqlite(query, args)
    async with _sqlite_conn() as conn, conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def fetchval(query: str, *args: Any) -> Any:
    """Первый столбец первой строки (или None)."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *_pg_args(args))

    sql, params = _to_sqlite(query, args)
    async with _sqlite_conn() as conn, conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def execute(query: str, *args: Any) -> int:
    """INSERT/UPDATE/DELETE/DDL. Возвращает число затронутых строк (rowcount;
    для asyncpg — распарсенное из строки статуса, -1 если не число)."""
    if _use_postgres():
        pool = await init_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(query, *_pg_args(args))
        return _rowcount_from_status(status)

    sql, params = _to_sqlite(query, args)
    async with _sqlite_conn() as conn:
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

    # BEGIN IMMEDIATE, а не отложенная транзакция по умолчанию. Транзакция
    # здесь почти всегда «прочитать остаток → записать»: с DEFERRED чтение
    # берёт SHARED, запись просит RESERVED, и при втором таком же соседе SQLite
    # отвечает «database is locked» СРАЗУ, не дожидаясь timeout (апгрейд
    # блокировки под busy-handler не попадает). А два читателя, прошедшие
    # проверку остатка одновременно, списывали его дважды — на Postgres от
    # этого держит FOR UPDATE, на SQLite его нет. IMMEDIATE берёт пишущую
    # блокировку на входе: писатели выстраиваются в очередь, как на проде.
    # Нашли нагрузочные тесты (tests/perf/test_load.py).
    # Соединение — через `_sqlite_conn`: снятая на BEGIN (ждёт чужую пишущую
    # блокировку до timeout) задача раньше оставляла поток aiosqlite жить.
    async with _sqlite_conn() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield _SqliteTxn(conn)
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise


class _PgTxn:
    __slots__ = ("_c",)

    def __init__(self, conn):
        self._c = conn

    async def fetch(self, q: str, *a: Any) -> list[dict]:
        return [dict(r) for r in await self._c.fetch(q, *_pg_args(a))]

    async def fetchrow(self, q: str, *a: Any) -> dict | None:
        r = await self._c.fetchrow(q, *_pg_args(a))
        return dict(r) if r is not None else None

    async def fetchval(self, q: str, *a: Any) -> Any:
        return await self._c.fetchval(q, *_pg_args(a))

    async def execute(self, q: str, *a: Any) -> int:
        return _rowcount_from_status(await self._c.execute(q, *_pg_args(a)))

    async def executemany(self, q: str, rows: list[tuple]) -> int:
        """Батч-INSERT/UPDATE одним prepared-statement'ом (asyncpg.executemany).
        Каждая строка биндится отдельно → нет лимита параметров на один
        statement. Возвращает число переданных строк."""
        if not rows:
            return 0
        await self._c.executemany(q, [_pg_args(tuple(r)) for r in rows])
        return len(rows)


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

    async def executemany(self, q: str, rows: list[tuple]) -> int:
        """Батч одним sqlite3.executemany. `$N` → `?` транслируем один раз;
        порядок плейсхолдеров применяем к каждой строке (повтор/перестановка
        $N поддержаны, как в _to_sqlite). Возвращает число строк."""
        if not rows:
            return 0
        order = [int(m.group(1)) - 1 for m in _PARAM_RE.finditer(q)]
        sql = _PARAM_RE.sub("?", q)
        seq = [tuple(r[i] for i in order) for r in rows]
        await self._c.executemany(sql, seq)
        return len(rows)
