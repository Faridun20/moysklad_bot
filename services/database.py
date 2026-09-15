"""
База данных — SQLite (локально) и PostgreSQL (продакшен).
"""

import asyncio
import os
import time
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from contextlib import contextmanager
from typing import Any, NamedTuple

from services import money  # канонические деньги (копейки); leaf-модуль, без циклов
from services import adb_core  # async DB-слой (asyncpg/aiosqlite); leaf-модуль, без циклов

# SQL-фрагменты денежных сумм и формула остатка живут в services.debts —
# единственный источник истины (T2.1). Импортируем под старыми именами,
# чтобы не трогать десяток call-сайтов. debts зависит только от adb_core,
# цикла нет.
from services.debts import (
    RETURN_OWED_FILTER as _RETURN_OWED_FILTER,
    SUM_ALLOC_CENTS as _SUM_ALLOC_CENTS,
    SUM_ORDER_TOTAL_CENTS as _SUM_ORDER_TOTAL_CENTS,
    SUM_PAYMENTS_CENTS as _SUM_PAYMENTS_CENTS,
    SUM_RETURNS_CENTS as _SUM_RETURNS_CENTS,
)

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
import tempfile

_default_db = os.path.join(tempfile.gettempdir(), "payments.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)
USE_POSTGRES = bool(DATABASE_URL)

# Логировать запросы дольше N мс (предупреждение).
# 0 — выключено. Управляется переменной окружения SQL_SLOW_MS.
SQL_SLOW_MS = float(os.environ.get("SQL_SLOW_MS", "200"))

# Размер пула. Минимум 1 коннект всегда держим открытым, максимум
# PG_POOL_MAX — это потолок одновременно открытых коннектов от этого
# процесса. Railway Postgres даёт ~50-100 коннектов на инстанс; 10
# достаточно для бота на сотни юзеров и оставляет запас другим
# сервисам (webapp как отдельный процесс, миграции и т.п.).
_PG_POOL_MIN = int(os.environ.get("PG_POOL_MIN", "1"))
_PG_POOL_MAX = int(os.environ.get("PG_POOL_MAX", "10"))
# Сколько ждать свободный коннект при временно исчерпанном пуле, прежде чем
# сдаться. asyncio.to_thread (через который идут все adb.* вызовы) может
# запустить больше DB-потоков, чем коннектов в пуле — размер дефолтного
# executor'а зависит от числа CPU хоста и обычно > PG_POOL_MAX. При всплеске
# параллельных запросов с фронта getconn() моментально кидал PoolError → 500.
# Теперь ждём освобождения (запросы выстраиваются в очередь к пулу).
_PG_POOL_ACQUIRE_TIMEOUT = float(os.environ.get("PG_POOL_ACQUIRE_TIMEOUT", "10"))
_PG_POOL_ACQUIRE_INTERVAL = 0.05

# Сколько синхронное соединение может простоять ВНУТРИ открытой транзакции, пока
# сервер его не оборвёт. psycopg2 открывает транзакцию первым же SELECT'ом, и
# `with get_conn()`, внутри которого случился сетевой вызов или зависший поток,
# держал бы снимок и блокировки часами: VACUUM не чистит, `FOR UPDATE` соседа
# ждёт, пул теряет соединение. 5 минут — на порядок больше любой рабочей
# транзакции. Активно работающую сессию таймаут не трогает (он про простой
# между командами), поэтому длинные прогоны `tasks.migrate` и разовых скриптов
# ему не мешают; скрипт, которому нужно думать между запросами дольше,
# снимает ограничение: PG_IDLE_IN_TX_TIMEOUT_MS=0. asyncpg-пул это не
# касается — у него свои таймауты (`adb_core.pg_server_settings`).
_DEFAULT_IDLE_IN_TX_TIMEOUT_MS = 300_000


def pg_session_options() -> dict[str, str]:
    """kwargs для psycopg2.connect: `options=-c idle_in_transaction_session_timeout=…`.

    Env читается при создании пула, а не при импорте: скрипт может выставить
    значение до первого обращения к базе. 0 — ограничение снято (опции нет).
    """
    try:
        ms = max(0, int(os.environ.get("PG_IDLE_IN_TX_TIMEOUT_MS", _DEFAULT_IDLE_IN_TX_TIMEOUT_MS)))
    except (TypeError, ValueError):
        ms = _DEFAULT_IDLE_IN_TX_TIMEOUT_MS
    if ms == 0 or "options=" in DATABASE_URL:
        # Свои `options` в URL уже заданы — kwargs их ПЕРЕЗАПИСАЛИ бы целиком
        # (psycopg2.extensions.make_dsn), и чужая настройка молча пропала бы.
        return {}
    return {"options": f"-c idle_in_transaction_session_timeout={ms}"}

if USE_POSTGRES:
    from psycopg2 import pool as _pg_pool
    from psycopg2.extras import RealDictCursor

    logger.info("Используется PostgreSQL")

    _pg_connection_pool: _pg_pool.ThreadedConnectionPool | None = None

    def _get_pool() -> _pg_pool.ThreadedConnectionPool:
        """Ленивая инициализация пула — позволяет импортировать модуль
        в окружениях без DATABASE_URL (тесты, миграции) без падения."""
        global _pg_connection_pool
        if _pg_connection_pool is None:
            kwargs = pg_session_options()
            _pg_connection_pool = _pg_pool.ThreadedConnectionPool(
                _PG_POOL_MIN, _PG_POOL_MAX, DATABASE_URL, **kwargs
            )
            logger.info(
                "Postgres pool создан: min=%d, max=%d",
                _PG_POOL_MIN,
                _PG_POOL_MAX,
            )
        return _pg_connection_pool

    def _pool_getconn():
        return _acquire_pooled_conn(_get_pool())

else:
    import sqlite3

    logger.info("Используется SQLite: %s", DB_PATH)


def _in_event_loop_thread() -> bool:
    """Идёт ли вызов в потоке, где крутится asyncio-loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _acquire_pooled_conn(pool):
    """getconn с ожиданием: при исчерпании пула ждём до
    _PG_POOL_ACQUIRE_TIMEOUT сек, опрашивая раз в _PG_POOL_ACQUIRE_INTERVAL,
    вместо мгновенного PoolError → 500. По истечении — пробрасываем PoolError.

    Ждать (`time.sleep`) можно только в worker-потоке (`asyncio.to_thread`).
    Синхронный вызов БД прямо из event loop (кэш роли при промахе, забытый
    `to_thread`) при исчерпанном пуле усыплял бы ВЕСЬ процесс на секунды:
    все запросы WebApp и апдейты бота стоят, пока один ждёт коннект. Поэтому
    из потока loop'а — одна попытка и громкий отказ: одна 500-я лучше
    замороженного сервиса, а лог показывает место, которое надо увести в поток.
    """
    from psycopg2 import pool as _pg_pool_mod

    if _in_event_loop_thread():
        try:
            return pool.getconn()
        except _pg_pool_mod.PoolError:
            logger.error(
                "Postgres pool исчерпан, а вызов идёт прямо из event loop — не ждём "
                "(time.sleep заморозил бы весь процесс). Уведите вызов в asyncio.to_thread.",
                stack_info=True,
            )
            raise
    deadline = time.monotonic() + _PG_POOL_ACQUIRE_TIMEOUT
    waited = False
    while True:
        try:
            return pool.getconn()
        except _pg_pool_mod.PoolError:
            if time.monotonic() >= deadline:
                logger.error(
                    "Postgres pool исчерпан: ждали %.1fs (max=%d) — сдаёмся",
                    _PG_POOL_ACQUIRE_TIMEOUT,
                    _PG_POOL_MAX,
                )
                raise
            if not waited:
                waited = True
                logger.warning(
                    "Postgres pool исчерпан (max=%d) — ждём свободный коннект…",
                    _PG_POOL_MAX,
                )
            time.sleep(_PG_POOL_ACQUIRE_INTERVAL)


class _TimedCursor:
    """Прозрачная обёртка над курсором: засекает время execute()
    и логирует запросы дольше SQL_SLOW_MS."""

    __slots__ = ("_cur",)

    def __init__(self, cur):
        self._cur = cur

    def execute(self, query, params=None):
        if SQL_SLOW_MS <= 0:
            return (
                self._cur.execute(query, params) if params is not None else self._cur.execute(query)
            )
        start = time.perf_counter()
        try:
            if params is not None:
                return self._cur.execute(query, params)
            return self._cur.execute(query)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms >= SQL_SLOW_MS:
                short = " ".join(query.split())[:120]
                logger.warning("SQL slow %.0f ms: %s", elapsed_ms, short)

    def executemany(self, query, seq):
        return self._cur.executemany(query, seq)

    def __getattr__(self, name):
        # fetchone / fetchall / lastrowid / rowcount / close / __iter__ и т.д.
        return getattr(self._cur, name)


@contextmanager
def get_conn():
    """Контекстный менеджер для коннекта к БД.

    Postgres: берём из ThreadedConnectionPool и возвращаем обратно
    (а не close — close уничтожает коннект и пул его пересоздаёт, что
    убивает весь смысл пула). При исключении делаем rollback, чтобы
    не вернуть в пул коннект с «грязной» транзакцией.

    SQLite: по-старому — отдельное соединение на каждый вызов.
    """
    if USE_POSTGRES:
        pool = _get_pool()
        conn = _pool_getconn()
        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            pool.putconn(conn)
    else:
        # timeout — ждать чужую пишущую транзакцию (по умолчанию 5 с; под
        # параллельной нагрузкой tests/perf не хватало).
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        # SQLite встроенный LOWER() — ASCII-only: 'Иванов'→'Иванов' (кириллица
        # не лоуэркейсится). Postgres LOWER() — Unicode-aware. Чтобы LIKE-поиск
        # вёл себя одинаково в обеих БД (важно для кириллических имён клиентов/
        # менеджеров), переопределяем LOWER на Python str.lower() (Unicode).
        conn.create_function(
            "lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True
        )
        try:
            yield conn
        finally:
            conn.close()


def get_cursor(conn):
    if USE_POSTGRES:
        raw = conn.cursor(cursor_factory=RealDictCursor)
    else:
        raw = conn.cursor()
    return _TimedCursor(raw)


def q(query: str) -> str:
    if USE_POSTGRES:
        return query.replace("?", "%s")
    return query


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _invalidate_role_cache(user_id: int) -> None:
    """Сбрасываем кэш ролей И флага деактивации. Лениво импортируем
    services.roles, иначе круговой импорт (roles уже зависит от database)."""
    try:
        from services.roles import invalidate_deactivated, invalidate_role

        invalidate_role(user_id)
        invalidate_deactivated(user_id)
    except Exception:
        # Кэш — мягкий, рассинхрон протухнет через TTL, поэтому write-операцию
        # из-за него не валим. Но и молчать нельзя: при поломке импорта роль
        # остаётся закэшированной, а значит повышение/понижение НЕ применяется —
        # и раньше об этом не было ни строчки в логах (§2.16).
        logger.warning(
            "Не удалось сбросить кэш роли user_id=%s — роль может остаться "
            "прежней до истечения TTL", user_id, exc_info=True,
        )


# ─── Инициализация ────────────────────────────────────────────────────────────


def init_db():
    """Гарантирует что схема существует. Безопасно вызывать из любого
    процесса при старте — все DDL идут через `CREATE TABLE IF NOT EXISTS`.

    Инкрементальных миграций в проекте нет: каждая таблица объявлена
    в `_create_tables` ОДИН раз, сразу с финальным набором колонок,
    типов и ограничений. Добавляешь колонку — правишь определение
    таблицы, а не пишешь ALTER.

    Backfill'ы и сидинг настроек — не часть init_db (они пишут данные,
    не должны бежать при каждом старте сервиса), они в `tasks/migrate.py`.

    Для прод-старта используй: `python -m tasks.migrate` перед
    `python bot.py`. На Railway: `tasks/migrate.py` в pre-start
    команде сервиса, или отдельный Cron Job «one-shot».
    """
    if not USE_POSTGRES:
        _enable_sqlite_wal()
    _create_tables()
    _create_indexes()
    _seed_currency_rates()
    _load_predefined_users()
    logger.info("База данных инициализирована (CREATE TABLE only)")


def _enable_sqlite_wal() -> None:
    """SQLite: журнал WAL — читатели не ждут писателя, писатель — читателей.

    В режиме по умолчанию (rollback journal) коммит берёт EXCLUSIVE и на это
    время останавливает все чтения, а читающая транзакция не даёт писателю
    закоммитить. Под параллельной нагрузкой (tests/perf: восемь одобрений
    разом) одиночные SELECT'ы отваливались «database is locked», хотя ждать
    им было всего сотни миллисекунд. WAL убирает оба ожидания. Настройка
    хранится В ФАЙЛЕ базы — достаточно выставить один раз при старте.
    Postgres это не касается: там свой MVCC.
    """
    try:
        with get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # Файловая система без поддержки WAL (сетевой диск) — остаёмся на
        # rollback journal, это не повод не стартовать.
        logger.warning("SQLite: не удалось включить WAL, остаёмся на rollback journal")


def _seed_currency_rates():
    """Сид BASE_CURRENCY с rate=1.0 — гарантирует что convert_to_base
    работает «из коробки» хотя бы для одной валюты. Остальные ставит
    админ через /api/currency/rates. Идемпотентно через UNIQUE PK."""
    from config import BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    with get_conn() as conn:
        cur = get_cursor(conn)
        try:
            if USE_POSTGRES:
                cur.execute(
                    q(
                        "INSERT INTO currency_rates (currency_code, rate_to_base, updated_at) "
                        "VALUES (?, 1.0, ?) ON CONFLICT (currency_code) DO NOTHING"
                    ),
                    (base, now_str()),
                )
            else:
                cur.execute(
                    q(
                        "INSERT OR IGNORE INTO currency_rates "
                        "(currency_code, rate_to_base, updated_at) VALUES (?, 1.0, ?)"
                    ),
                    (base, now_str()),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.debug("seed currency_rates skipped (likely table not yet created)")


def _table_ddls() -> list[str]:
    """Определения всех таблиц — ОДИН источник и для `_create_tables`, и для
    сверки схемы при старте (`services.schema_check`): ожидаемые колонки
    берутся отсюда же, иначе список для сверки разъехался бы с определением."""
    id_type = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    # ВСЕ количества (склад, позиции заказа и возврата, состав контейнера): на
    # Postgres NUMERIC — точная десятичная арифметика; на SQLite REAL (NUMERIC
    # там всё равно сводится к REAL-аффинности). REAL на Postgres — это float4:
    # 2.3 хранится как 2.2999999523, дробные возвраты не сходились с
    # количеством, и заказ не становился «возвращён полностью». Базы, где
    # колонки уже созданы REAL, догоняет разовый scripts/apply_constraints.
    # Читающий код получает Decimal (asyncpg/psycopg2) и приводит к float на
    # границе — как у stock/invoice_items.
    qty_type = "NUMERIC" if USE_POSTGRES else "REAL"

    # Элементы списка оставлены с прежним отступом (внутри скобок он не
    # значим): определения таблиц правят параллельные ветки, и сдвиг всего
    # списка превратил бы каждую их правку в конфликт слияния.
    tables = [
            # deactivated_at/_by — «увольнение»: get_role отдаёт guest, пока стоит.
            """CREATE TABLE IF NOT EXISTS user_roles (
                user_id              BIGINT PRIMARY KEY,
                username             TEXT,
                full_name            TEXT,
                role                 TEXT NOT NULL DEFAULT 'manager',
                moysklad_employee_id TEXT,
                ms_sync_status       TEXT DEFAULT 'pending',
                deactivated_at       TEXT,
                deactivated_by       BIGINT,
                created_at           TEXT
            )""",
            # ms_sync_claimed_at — время claim'а для MS-синка (WP-10). Reaper
            # orphan'ов судит устаревание по нему, а не по confirmed_at: иначе
            # платёж, подтверждённый >30 мин назад, мог быть сброшен reaper'ом
            # ПРЯМО во время in-flight POST → второй paymentin в МС (дубль).
            f"""CREATE TABLE IF NOT EXISTS payments (
                id                 {id_type},
                user_id            BIGINT NOT NULL,
                username           TEXT,
                full_name          TEXT,
                amount_cents       BIGINT NOT NULL,
                currency           TEXT NOT NULL DEFAULT 'USD',
                comment            TEXT,
                status             TEXT NOT NULL DEFAULT 'pending',
                order_id           BIGINT,
                ms_paymentin_id    TEXT,
                ms_sync_status     TEXT,
                ms_sync_error      TEXT,
                ms_sync_claimed_at TEXT,
                -- Курс валюты платежа к BASE_CURRENCY, замороженный в момент
                -- подтверждения (confirm_payment). Пересчёт «в долларах»
                -- обязан опираться на курс того дня, а не сегодняшний: иначе
                -- выручка прошлого месяца меняется от движения курса.
                fx_rate_to_base    REAL,
                created_at         TEXT NOT NULL,
                confirmed_at       TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS audit_log (
                id         {id_type},
                user_id    BIGINT NOT NULL,
                full_name  TEXT,
                role       TEXT,
                action     TEXT NOT NULL,
                details    TEXT,
                created_at TEXT NOT NULL
            )""",
            # payment_type: 'paid' (оплачено сразу) | 'credit' (в долг).
            # due_date заполняется только для 'credit'.
            # Двухступенчатое подтверждение оплаты:
            #   paid_at          — менеджер отметил «деньги получил»
            #   paid_confirmed_* — босс/админ подтвердил «да, в кассе»
            # ms_cancel_synced_at    — отмена отражена в МС (реверс customerorder)
            # ms_deleted_at          — документ обнаружен удалённым в МС
            # ms_drift_at            — сумма разошлась с документом в МС (сигнал, не автоправка)
            # ms_transition_blocked_at — МС прислал нелегальный для нашей FSM статус
            # ms_demand_failed_at    — customerorder создан, demand упал (нужна доделка)
            f"""CREATE TABLE IF NOT EXISTS orders (
                id                       {id_type},
                user_id                  BIGINT NOT NULL,
                full_name                TEXT,
                status                   TEXT NOT NULL DEFAULT 'draft',
                comment                  TEXT,
                agent_id                 TEXT,
                agent_name               TEXT,
                currency                 TEXT,
                payment_type             TEXT NOT NULL DEFAULT 'paid',
                due_date                 TEXT,
                paid_at                  TEXT,
                paid_confirmed_at        TEXT,
                paid_confirmed_by        BIGINT,
                paid_confirmed_by_name   TEXT,
                payment_confirmed        INTEGER NOT NULL DEFAULT 0,
                payment_confirmed_at     TEXT,
                rejection_comment        TEXT,
                rejection_count          INTEGER NOT NULL DEFAULT 0,
                frozen                   INTEGER NOT NULL DEFAULT 0,
                credit_limit_override    INTEGER NOT NULL DEFAULT 0,
                credit_limit_override_by BIGINT,
                return_status            TEXT,
                submitted_at             TEXT,
                shipped_at               TEXT,
                shipped_by               BIGINT,
                cancelled_at             TEXT,
                cancelled_by             BIGINT,
                cancellation_reason      TEXT,
                ms_demand_id             TEXT,
                ms_customerorder_id      TEXT,
                ms_cancel_synced_at      TEXT,
                ms_deleted_at            TEXT,
                ms_drift_at              TEXT,
                ms_transition_blocked_at TEXT,
                ms_demand_failed_at      TEXT,
                -- Курс валюты заказа к BASE_CURRENCY, замороженный при первом
                -- «реализующем» переходе (approved/shipped, _snapshot_order_fx).
                fx_rate_to_base          REAL,
                created_at               TEXT NOT NULL,
                updated_at               TEXT NOT NULL
            )""",
            # price — цена за единицу в валюте заказа (как ввёл пользователь).
            # price_cents — она же в копейках (канон, см. services/money.py).
            f"""CREATE TABLE IF NOT EXISTS order_items (
                id                   {id_type},
                order_id             BIGINT NOT NULL,
                product_name         TEXT NOT NULL,
                product_href         TEXT,
                quantity             {qty_type} NOT NULL DEFAULT 1,
                unit                 TEXT DEFAULT 'шт',
                price_cents          BIGINT NOT NULL DEFAULT 0,
                returned_qty         {qty_type} NOT NULL DEFAULT 0,
                note                 TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS shipment_requests (
                id               {id_type},
                order_id         BIGINT NOT NULL,
                user_id          BIGINT NOT NULL,
                full_name        TEXT,
                status           TEXT NOT NULL DEFAULT 'pending',
                comment          TEXT,
                approved_by      BIGINT,
                approved_by_name TEXT,
                created_at       TEXT NOT NULL,
                approved_at      TEXT
            )""",
            # Round 6 RACE-4: idempotency-guard для ops_monitor cron.
            # PRIMARY KEY (run_date) + INSERT-if-absent через `claim_ops_monitor_run`
            # — параллельный/повторный запуск за тот же день делает noop.
            """CREATE TABLE IF NOT EXISTS ops_monitor_runs (
                run_date   TEXT PRIMARY KEY,
                started_at TEXT
            )""",
            # ─── IMPLEMENTATION.md Фаза 1–2 (адаптировано под dual-DB) ──────────
            # Конвенции проекта: TEXT для JSON/UUID/timestamp, BIGINT-копейки
            # для денег (количества — qty_type, см. выше; REAL — курсы),
            # INTEGER 0/1 для boolean, BIGINT — telegram user_id, без FK
            # (как и остальные таблицы здесь). Postgres-специфику (JSONB,
            # gen_random_uuid, NUMERIC) НЕ используем — иначе ломается SQLite.
            # Кредитный лимит контрагента. agent_id — UUID контрагента МойСклад.
            """CREATE TABLE IF NOT EXISTS credit_limits (
                agent_id     TEXT PRIMARY KEY,
                agent_name   TEXT NOT NULL,
                limit_amount_cents BIGINT NOT NULL DEFAULT 200000,
                set_by       BIGINT,
                notes        TEXT,
                updated_at   TEXT,
                created_at   TEXT
            )""",
            # Сдача наличных в кассу (manager → касса). status: pending|confirmed|rejected.
            f"""CREATE TABLE IF NOT EXISTS cash_deposits (
                id           {id_type},
                manager_id   BIGINT NOT NULL,
                amount_cents BIGINT NOT NULL,
                deposited_at TEXT,
                confirmed_by BIGINT,
                confirmed_at TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT,
                notes        TEXT,
                created_at   TEXT
            )""",
            # Распределение одной сдачи по заказам (composite PK).
            """CREATE TABLE IF NOT EXISTS cash_deposit_orders (
                deposit_id       BIGINT NOT NULL,
                order_id         BIGINT NOT NULL,
                amount_allocated_cents BIGINT NOT NULL,
                is_manual        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (deposit_id, order_id)
            )""",
            # Возвраты товара. return_type: partial|full. status: pending|confirmed|rejected.
            f"""CREATE TABLE IF NOT EXISTS returns (
                id           {id_type},
                order_id     BIGINT NOT NULL,
                return_type  TEXT NOT NULL,
                reason       TEXT NOT NULL,
                total_amount_cents BIGINT NOT NULL,
                refund_method TEXT,
                moysklad_return_id TEXT,
                created_by   BIGINT NOT NULL,
                confirmed_by BIGINT,
                status       TEXT NOT NULL DEFAULT 'pending',
                goods_received INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT,
                confirmed_at TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS return_items (
                id            {id_type},
                return_id     BIGINT NOT NULL,
                order_item_id BIGINT NOT NULL,
                qty           {qty_type} NOT NULL,
                amount_cents  BIGINT NOT NULL
            )""",
            # Журнал изменений заказа (before/after/summary как JSON-текст).
            f"""CREATE TABLE IF NOT EXISTS order_change_log (
                id              {id_type},
                order_id        BIGINT NOT NULL,
                changed_by      BIGINT NOT NULL,
                change_type     TEXT NOT NULL,
                before_snapshot TEXT,
                after_snapshot  TEXT,
                summary         TEXT,
                created_at      TEXT
            )""",
            # Ключи идемпотентности для мутаций (result как JSON-текст).
            """CREATE TABLE IF NOT EXISTS idempotency_keys (
                key        TEXT PRIMARY KEY,
                operation  TEXT NOT NULL,
                user_id    BIGINT NOT NULL,
                result     TEXT,
                created_at TEXT,
                expires_at TEXT
            )""",
            # Настройки приложения (value как JSON-текст). Источник «магических чисел».
            """CREATE TABLE IF NOT EXISTS app_settings (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                description TEXT,
                updated_by  BIGINT,
                updated_at  TEXT
            )""",
            # Журнал запусков cron-задач.
            f"""CREATE TABLE IF NOT EXISTS cron_runs (
                id          {id_type},
                task_name   TEXT NOT NULL,
                started_at  TEXT,
                finished_at TEXT,
                status      TEXT NOT NULL DEFAULT 'running',
                error_message TEXT,
                metadata    TEXT
            )""",
            # Курсы валют → к BASE_CURRENCY (USD по умолчанию). «Текущий»
            # рыночный курс: обновляется авто-задачей tasks/run_fx_sync (ЦБ РУз)
            # или вручную боссом через /api/currency/rates. Используется для
            # UI-сводок и как fallback, когда у строки нет снимка курса.
            """CREATE TABLE IF NOT EXISTS currency_rates (
                currency_code TEXT PRIMARY KEY,
                rate_to_base  REAL NOT NULL,
                updated_at    TEXT,
                updated_by    BIGINT
            )""",
            # Дневной архив курсов (история). Пишется tasks/run_fx_sync из CBU
            # (source='cbu') либо вручную (source='manual'). Нужен для:
            #   - точки-во-времени конвертации (get_currency_rate_asof) и
            #     бэкфилла снимков прошлым заказам/платежам;
            #   - аудита движения курса.
            # PK (currency_code, rate_date) — один курс на валюту в день.
            """CREATE TABLE IF NOT EXISTS currency_rate_daily (
                currency_code TEXT NOT NULL,
                rate_date     TEXT NOT NULL,
                rate_to_base  REAL NOT NULL,
                source        TEXT,
                created_at    TEXT NOT NULL,
                PRIMARY KEY (currency_code, rate_date)
            )""",
            # Per-user overrides прав (PR #44 / tech debt #3c).
            # Существующий enum-based system (admin/boss/manager/...) остаётся
            # как «дефолт по роли» — см. services.roles.ROLE_DEFAULTS. Эта
            # таблица позволяет админу точечно выдать или отозвать право
            # конкретному юзеру, не создавая новую роль. has_permission()
            # сначала смотрит сюда, потом falls back к ROLE_DEFAULTS.
            # granted: 1 = explicit grant (даже если роль не имеет),
            #          0 = explicit revoke (даже если роль имеет по дефолту).
            """CREATE TABLE IF NOT EXISTS user_permissions (
                user_id         BIGINT NOT NULL,
                permission_code TEXT NOT NULL,
                granted         INTEGER NOT NULL DEFAULT 1,
                updated_at      TEXT,
                updated_by      BIGINT,
                PRIMARY KEY (user_id, permission_code)
            )""",
            # Личные настройки ИНТЕРФЕЙСА (services/user_prefs.py): например,
            # «Рабочие действия» у руководителя. Это предпочтение вида, а НЕ
            # права — ручки их не читают. В БД, а не в localStorage: WebView
            # Telegram хранилище теряет. value — JSON-текст, как у app_settings.
            """CREATE TABLE IF NOT EXISTS user_prefs (
                user_id    BIGINT NOT NULL,
                pref_key   TEXT NOT NULL,
                value      TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (user_id, pref_key)
            )""",
            # Цены товаров, выставленные руководством (PR C).
            # sale_price — минимальная цена продажи: при добавлении товара
            #   в заказ менеджер может поднять, но не опустить ниже.
            # cost_price — себестоимость: видна ТОЛЬКО boss/admin, нужна
            #   для расчёта прибыли. Может быть NULL (не задана).
            # Источник истины — руководство: задаётся через
            #   /api/products/prices/set.
            # `ms_id` — исторически UUID МойСклад, после перехода там лежит id
            # НАШЕЙ карточки строкой (`backfill_local_identifiers`).
            # Переименовать колонку нечем: инкрементальных миграций в проекте
            # нет, а заводить таблицу-двойник ради имени поля — хуже.
            """CREATE TABLE IF NOT EXISTS product_prices (
                ms_id         TEXT PRIMARY KEY,
                product_name  TEXT,
                sale_price_cents BIGINT,
                cost_price_cents BIGINT,
                currency      TEXT,
                updated_by    BIGINT,
                updated_at    TEXT
            )""",
            # ═══════════════════════════════════════════════════════════════
            # Локальный складской учёт — замена МойСклад.
            #
            # Идентичность: id — SERIAL (pg) / AUTOINCREMENT (sqlite).
            # legacy_ms_id хранит UUID из МойСклад только для миграции и
            # последующей сверки; на него нет ни одного рантайм-пути — новый
            # код ходит исключительно по числовому id.
            #
            # Деньги — BIGINT в минорных единицах (копейки/центы), как везде
            # в проекте (services/money.py). Схема из ТЗ предлагала NUMERIC;
            # отклонились сознательно, чтобы в одной БД не оказалось двух
            # денежных конвенций и конвертации на каждом стыке
            # накладная↔заказ↔платёж.
            #
            # Количества — NUMERIC на Postgres (точная арифметика, остаток не
            # накапливает дрейф при дробных отгрузках) / REAL на SQLite,
            # где NUMERIC всё равно имеет REAL-аффинность.
            #
            # Время — TEXT в локальной TZ через now_str(), как остальные
            # таблицы. TIMESTAMPTZ DEFAULT now() из ТЗ не берём: он пишет UTC,
            # а весь проект сравнивает с local-TZ строками (CLAUDE.md про
            # silent-false при сравнении TZ'ов).
            # ═══════════════════════════════════════════════════════════════
            f"""CREATE TABLE IF NOT EXISTS counterparties (
                id           {id_type},
                name         TEXT NOT NULL,
                type         TEXT NOT NULL DEFAULT 'customer'
                             CHECK (type IN ('supplier', 'customer')),
                phone        TEXT,
                telegram_id  BIGINT,
                notes        TEXT,
                legacy_ms_id TEXT,
                created_at   TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS products (
                id           {id_type},
                name         TEXT NOT NULL,
                category     TEXT,
                sku          TEXT,
                unit         TEXT NOT NULL DEFAULT 'шт',
                legacy_ms_id TEXT,
                created_at   TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS warehouses (
                id   {id_type},
                name TEXT NOT NULL
            )""",
            # Остаток по (товар, склад). Отрицательным не бывает: движение,
            # уводящее в минус, откатывает всю накладную целиком.
            f"""CREATE TABLE IF NOT EXISTS stock (
                product_id   BIGINT NOT NULL,
                warehouse_id BIGINT NOT NULL,
                quantity     {qty_type} NOT NULL DEFAULT 0,
                PRIMARY KEY (product_id, warehouse_id)
            )""",
            # Накладная. Промежуточного draft нет — сразу confirmed, остатки
            # двигаются в той же транзакции, что и вставка строк.
            f"""CREATE TABLE IF NOT EXISTS invoices (
                id                 {id_type},
                type               TEXT NOT NULL
                                   CHECK (type IN ('incoming', 'outgoing')),
                counterparty_id    BIGINT,
                warehouse_id       BIGINT NOT NULL,
                invoice_number     TEXT NOT NULL,
                invoice_date       TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'confirmed'
                                   CHECK (status IN ('confirmed', 'cancelled')),
                currency           TEXT NOT NULL DEFAULT 'USD',
                total_amount_cents BIGINT NOT NULL DEFAULT 0,
                comment            TEXT,
                created_by         BIGINT,
                created_at         TEXT NOT NULL,
                cancelled_by       BIGINT,
                cancelled_at       TEXT,
                telegram_sent      INTEGER NOT NULL DEFAULT 0,
                telegram_sent_at   TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS invoice_items (
                id          {id_type},
                invoice_id  BIGINT NOT NULL,
                product_id  BIGINT NOT NULL,
                quantity    {qty_type} NOT NULL,
                price_cents BIGINT
            )""",
            # Платежи ПОСТАВЩИКАМ (исходящие). Отдельная таблица, а не строка
            # в `payments`: там лежат деньги ОТ клиентов, и на них считается
            # вся дебиторка (`services/debts`, `receivables`). Одна запись
            # исходящего платежа в `payments` уменьшила бы долг клиента на
            # сумму, которую мы заплатили поставщику, — и расхождение всплыло
            # бы не в отчёте, а в разговоре с клиентом.
            #
            # Заполняется переносом истории из МойСклад
            # (`scripts/migrate_history_from_moysklad.py`, entity/paymentout).
            # Долг ПЕРЕД поставщиком = приходные накладные минус эти платежи.
            f"""CREATE TABLE IF NOT EXISTS supplier_payments (
                id               {id_type},
                counterparty_id  BIGINT,
                supplier_name    TEXT,
                amount_cents     BIGINT NOT NULL,
                currency         TEXT NOT NULL DEFAULT 'USD',
                comment          TEXT,
                invoice_id       BIGINT,
                ms_paymentout_id TEXT,
                fx_rate_to_base  REAL,
                paid_at          TEXT,
                created_at       TEXT NOT NULL
            )""",
            # Счётчик номеров накладных. Инкремент — атомарный UPSERT
            # ... ON CONFLICT DO UPDATE ... RETURNING в транзакции накладной.
            """CREATE TABLE IF NOT EXISTS invoice_counters (
                type        TEXT NOT NULL,
                year        INTEGER NOT NULL,
                last_number INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (type, year)
            )""",
            # ─── Генерация юридических документов (docxtpl + LibreOffice) ───
            f"""CREATE TABLE IF NOT EXISTS document_templates (
                id         {id_type},
                type       TEXT NOT NULL,
                file_path  TEXT NOT NULL,
                is_active  INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS generated_documents (
                id                 {id_type},
                template_id        BIGINT,
                counterparty_id    BIGINT,
                client_name        TEXT,
                passport_data      TEXT,
                product_name       TEXT NOT NULL,
                total_amount_cents BIGINT NOT NULL,
                currency           TEXT NOT NULL DEFAULT 'USD',
                start_date         TEXT NOT NULL,
                term_months        INTEGER NOT NULL,
                payment_type       TEXT NOT NULL
                                   CHECK (payment_type IN ('single', 'installment')),
                installments_count INTEGER,
                file_path          TEXT,
                created_by         BIGINT,
                created_at         TEXT NOT NULL
            )""",
            # Соответствие «UUID МойСклад → локальный id». Артефакт миграции,
            # а не рантайм-путь: заполняется scripts/migrate_from_moysklad.py
            # и читается сверкой + будущим переводом orders/order_items/
            # credit_limits/product_prices на числовые ключи (шаг 4 плана).
            #
            # Почему отдельной таблицей, а не колонками counterparty_id/
            # product_id в существующих таблицах: добавить колонку в уже
            # развёрнутую боевую БД можно только ALTER'ом, а он в проекте
            # запрещён (T1.1, tests/test_schema_single_pass.py) — схема
            # создаётся одним проходом CREATE TABLE. Таблица соответствий
            # новая, поэтому создаётся штатно и на шаге 1 переключения не
            # трогает денежные таблицы вообще.
            # Соответствие «UUID МойСклад → наш id». Рабочий артефакт миграции
            # (`scripts/migrate_from_moysklad.py`): по нему сверялись данные и
            # по нему `backfill_local_identifiers` перевёл старые ссылки на
            # числовые ключи. Держим, пока живёт аккаунт МС — это единственное,
            # чем можно доказать, что и откуда приехало.
            """CREATE TABLE IF NOT EXISTS ms_id_map (
                entity_type TEXT NOT NULL,
                ms_id       TEXT NOT NULL,
                local_id    BIGINT NOT NULL,
                migrated_at TEXT NOT NULL,
                PRIMARY KEY (entity_type, ms_id)
            )""",
            # ── Учёт экскаваторов (волна 4, T4.1) ────────────────────────────
            #
            # Отличие от остальной схемы: здесь есть FOREIGN KEY. Связи строго
            # иерархические (часы/фото/сделки не существуют без машины), каскад
            # избавляет от ручной уборки. На SQLite (тесты) FK по умолчанию не
            # энфорсятся, поэтому сервис всё равно чистит детей явно — иначе
            # поведение разъезжалось бы между тестами и продом.
            #
            # cost_cents (себестоимость) видит только boss/admin — срез делает
            # слой сервиса, не фронт.
            f"""CREATE TABLE IF NOT EXISTS machines (
                id               {id_type},
                vin              TEXT NOT NULL UNIQUE,
                name             TEXT NOT NULL,
                brand            TEXT,
                model            TEXT,
                year             INTEGER,
                hours            INTEGER,
                hours_updated_at TEXT,
                price_cents      BIGINT,
                cost_cents       BIGINT,
                currency         TEXT NOT NULL DEFAULT 'USD',
                status           TEXT NOT NULL DEFAULT 'in_transit',
                eta_date         TEXT,
                container_no     TEXT,
                location         TEXT,
                notes            TEXT,
                ms_product_id    TEXT,
                created_by       BIGINT NOT NULL,
                created_at       TEXT,
                updated_at       TEXT,
                CONSTRAINT machines_status_chk CHECK (status IN
                    ('in_transit','in_stock','reserved','sold','on_credit','archived'))
            )""",
            # Каждое показание моточасов — отдельной строкой (видна динамика и
            # ловятся опечатки); в machines.hours дублируется последнее.
            f"""CREATE TABLE IF NOT EXISTS machine_hours (
                id          {id_type},
                machine_id  INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                hours       INTEGER NOT NULL CHECK (hours >= 0),
                recorded_by BIGINT NOT NULL,
                recorded_at TEXT
            )""",
            # Файлы не скачиваем — на Railway эфемерная ФС; храним tg_file_id.
            # file_unique_id обязателен (волна 7): tg_file_id привязан к паре
            # «бот + сервер Bot API», и переезд на локальный Bot API server его
            # обнулит. file_unique_id переживает переезд и показывает, какие
            # записи осиротели, — поэтому NOT NULL, а не опционально.
            f"""CREATE TABLE IF NOT EXISTS machine_photos (
                id             {id_type},
                machine_id     INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                tg_file_id     TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                caption        TEXT,
                sort_order     INTEGER NOT NULL DEFAULT 0,
                uploaded_by    BIGINT NOT NULL,
                uploaded_at    TEXT,
                UNIQUE (machine_id, file_unique_id)
            )""",
            # Сделка по машине. order_id/agent_ms_id — необязательная связь с
            # заказом и контрагентом МС: продажа техники может идти и мимо них.
            f"""CREATE TABLE IF NOT EXISTS machine_deals (
                id             {id_type},
                machine_id     INTEGER NOT NULL REFERENCES machines(id),
                kind           TEXT NOT NULL CHECK (kind IN ('sale','credit')),
                price_cents    BIGINT NOT NULL,
                currency       TEXT NOT NULL DEFAULT 'USD',
                buyer_name     TEXT NOT NULL,
                buyer_phone    TEXT,
                buyer_passport TEXT,
                buyer_note     TEXT,
                order_id       BIGINT,
                agent_ms_id    TEXT,
                sold_at        TEXT,
                due_date       TEXT,
                closed_at      TEXT,
                created_by     BIGINT NOT NULL
            )""",
            # График рассрочки. Первоначальный взнос — та же таблица, `seq = 0`
            # и `paid_at` сразу: деньги уже получены, и отдельная колонка в
            # `machine_deals` описывала бы ровно то же самое вторым способом
            # (а заодно не доехала бы до существующей таблицы на проде —
            # инкрементальных миграций в проекте нет).
            f"""CREATE TABLE IF NOT EXISTS machine_deal_payments (
                id            {id_type},
                deal_id       INTEGER NOT NULL REFERENCES machine_deals(id),
                seq           INTEGER NOT NULL,
                due_date      TEXT NOT NULL,
                amount_cents  BIGINT NOT NULL,
                paid_at       TEXT,
                paid_by       BIGINT,
                notified_at   TEXT,
                created_at    TEXT,
                UNIQUE (deal_id, seq)
            )""",
            # Фактические поступления по рассрочке. Отдельно от графика, потому
            # что клиент платит не «платёж №3», а деньги: в один месяц больше,
            # в другой меньше. График — план, поступления — факт, и один к
            # одному они не ложатся. Покрытие графика считается распределением
            # поступлений по порядку (`services.machines.allocate_receipts`).
            f"""CREATE TABLE IF NOT EXISTS machine_payment_receipts (
                id            {id_type},
                deal_id       INTEGER NOT NULL REFERENCES machine_deals(id),
                amount_cents  BIGINT NOT NULL,
                received_at   TEXT NOT NULL,
                received_by   BIGINT,
                note          TEXT,
                created_at    TEXT
            )""",
            # Заявка на сделку по машине: бронь, продажа или рассрочка, которую
            # оформил менеджер и которая ждёт решения руководителя. Отдельная
            # таблица, а не статус в `machine_deals`: сделка — денежный факт, на
            # неё смотрят дебиторка, напоминания, бухгалтерия и архив, и каждый
            # из них пришлось бы учить пропускать «ещё не одобренные». Строка в
            # `machine_deals` появляется только при одобрении (`deal_id`), поэтому
            # все старые сделки — одобренные по построению. Машина на время
            # заявки статуса НЕ меняет (CHECK `machines_status_chk` на проде без
            # ALTER не расширить): «ждёт одобрения» выводится из живой заявки, а
            # вторую заявку на ту же машину держит частичный UNIQUE
            # `idx_machine_deal_requests_active`. CHECK на kind/status/
            # approval_mode ставит `scripts/apply_constraints` (значения — из
            # `services.machine_deal_requests`), FK — здесь, как у всей техники.
            f"""CREATE TABLE IF NOT EXISTS machine_deal_requests (
                id                 {id_type},
                machine_id         INTEGER NOT NULL REFERENCES machines(id),
                kind               TEXT NOT NULL,
                status             TEXT NOT NULL DEFAULT 'pending',
                price_cents        BIGINT,
                list_price_cents   BIGINT,
                currency           TEXT NOT NULL DEFAULT 'USD',
                down_payment_cents BIGINT NOT NULL DEFAULT 0,
                months             INTEGER NOT NULL DEFAULT 0,
                buyer_name         TEXT NOT NULL,
                buyer_phone        TEXT,
                buyer_passport     TEXT,
                buyer_note         TEXT,
                agent_ms_id        TEXT,
                machine_status     TEXT NOT NULL,
                attempts           INTEGER NOT NULL DEFAULT 1,
                created_by         BIGINT NOT NULL,
                creator_name       TEXT,
                created_at         TEXT NOT NULL,
                submitted_at       TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                decided_by         BIGINT,
                decider_name       TEXT,
                decided_at         TEXT,
                decision_note      TEXT,
                approval_mode      TEXT,
                deal_id            INTEGER REFERENCES machine_deals(id)
            )""",
            # Способ, которым получено поступление по рассрочке (наличные /
            # карта / перечисление — те же слова, что у разбивки оплаты заказа,
            # `order_payments.METHODS`). Sidecar, а не колонка: таблица
            # поступлений уже на проде. Нет строки — способ не указан (старые
            # поступления, кнопка «оплачен» без формы, бухгалтерия — там счёт).
            """CREATE TABLE IF NOT EXISTS machine_receipt_methods (
                receipt_id INTEGER PRIMARY KEY REFERENCES machine_payment_receipts(id),
                method     TEXT NOT NULL,
                created_at TEXT
            )""",
            # Куда поступили деньги по рассрочке «на карту»/«на счёт» — запись
            # справочника `acc_accounts` (как у разбивки оплаты заказа,
            # `payment_part_accounts`). FK на поступление — здесь, как у всей
            # техники; на счёт — `scripts/apply_constraints`.
            """CREATE TABLE IF NOT EXISTS machine_receipt_accounts (
                receipt_id INTEGER PRIMARY KEY REFERENCES machine_payment_receipts(id),
                account_id BIGINT NOT NULL,
                created_at TEXT
            )""",
            # История публикаций в канал. Нужна, чтобы один и тот же контейнер
            # не ушёл в канал дважды — второй раз обычно потому, что первый
            # забыли.
            f"""CREATE TABLE IF NOT EXISTS channel_posts (
                id         {id_type},
                kind       TEXT NOT NULL,
                ref        TEXT,
                message_id BIGINT,
                posted_by  BIGINT,
                posted_at  TEXT,
                created_at TEXT
            )""",
            # Фотографии товаров каталога. Как у техники: храним только
            # идентификаторы Telegram, файл живёт там. `file_unique_id`
            # обязателен — он переживает смену сервера Bot API.
            f"""CREATE TABLE IF NOT EXISTS product_photos (
                id             {id_type},
                ms_id          TEXT NOT NULL,
                tg_file_id     TEXT NOT NULL,
                file_unique_id TEXT NOT NULL,
                caption        TEXT,
                uploaded_by    BIGINT,
                uploaded_at    TEXT,
                UNIQUE (ms_id, file_unique_id)
            )""",
            # Подключение бота к личному аккаунту менеджера (Telegram Business).
            # Нужно, чтобы по `business_connection_id` из апдейта понять, чей
            # это чат: сам апдейт менеджера не называет.
            """CREATE TABLE IF NOT EXISTS business_connections (
                connection_id TEXT PRIMARY KEY,
                manager_id    BIGINT NOT NULL,
                user_chat_id  BIGINT,
                is_enabled    INTEGER NOT NULL DEFAULT 1,
                can_read      INTEGER NOT NULL DEFAULT 0,
                connected_at  TEXT,
                updated_at    TEXT
            )""",
            # Клиент, написавший менеджеру. Один ряд на человека, а не на
            # переписку: вопрос «сколько клиентов написали» иначе двоился бы,
            # если тот же человек написал двум менеджерам.
            #
            # ТЕКСТОВ СООБЩЕНИЙ ЗДЕСЬ НЕТ И НЕ ДОЛЖНО БЫТЬ. Для воронки нужны
            # только «кто, когда, в какую сторону»; хранить переписку клиентов —
            # ответственность без выгоды.
            f"""CREATE TABLE IF NOT EXISTS leads (
                id              {id_type},
                tg_user_id      BIGINT NOT NULL UNIQUE,
                manager_id      BIGINT,
                username        TEXT,
                display_name    TEXT,
                status          TEXT NOT NULL DEFAULT 'new'
                                CHECK (status IN ('new','won','lost')),
                agent_ms_id     TEXT,
                first_seen_at   TEXT,
                last_inbound_at TEXT,
                last_outbound_at TEXT,
                first_reply_at  TEXT,
                created_at      TEXT,
                updated_at      TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS lead_events (
                id         {id_type},
                lead_id    INTEGER NOT NULL REFERENCES leads(id),
                kind       TEXT NOT NULL,
                manager_id BIGINT,
                at         TEXT NOT NULL,
                created_at TEXT
            )""",
            # Звонки. Отдельная таблица, а не колонки в `leads`: у позвонившего
            # клиента нет `tg_user_id`, а он там NOT NULL UNIQUE — и ослабить
            # его на проде нельзя (инкрементальных миграций нет).
            #
            # `lead_id` НЕОБЯЗАТЕЛЕН — это и есть решение. Звонок от человека,
            # которого нет в Telegram, — законное самостоятельное состояние:
            # он живёт в списке «перезвонить», а не притворяется перепиской.
            # Привязка ставится руками, когда клиент напишет.
            #
            # `note` здесь ЗАКОНЕН, в отличие от `leads`: это заметка менеджера
            # о собственном звонке, а не сохранённое чужое сообщение. Разница
            # принципиальная — не переносить это послабление на переписку.
            f"""CREATE TABLE IF NOT EXISTS lead_calls (
                id           {id_type},
                lead_id      INTEGER REFERENCES leads(id),
                phone        TEXT,
                phone_key    TEXT,
                display_name TEXT,
                direction    TEXT NOT NULL DEFAULT 'in'
                             CHECK (direction IN ('in','out')),
                source       TEXT,
                interest     TEXT,
                manager_id   BIGINT,
                at           TEXT NOT NULL,
                note         TEXT,
                created_at   TEXT
            )""",
            # Причина отказа. Sidecar по образцу `container_supply`: в
            # `lead_events` колонки под текст нет и появиться не может.
            # Причина НЕОБЯЗАТЕЛЬНА — кнопку «Не купил» и так нажимают редко,
            # и обязательное поле привело бы к тому, что её перестанут нажимать
            # вовсе. Потерять сам факт отказа хуже, чем отказ без причины.
            """CREATE TABLE IF NOT EXISTS lead_lost (
                lead_id  INTEGER PRIMARY KEY REFERENCES leads(id),
                reason   TEXT NOT NULL
                         CHECK (reason IN ('price','no_stock','competitor',
                                           'postponed','wrong_fit','no_answer','other')),
                note     TEXT,
                at       TEXT,
                set_by   BIGINT
            )""",
            # Контейнеры в пути. Состав заводят при отправке («ожидалось»), при
            # прибытии проставляют факт — расхождение видно сразу, а не после
            # ручной сверки с накладной.
            f"""CREATE TABLE IF NOT EXISTS containers (
                id           {id_type},
                number       TEXT NOT NULL UNIQUE,
                status       TEXT NOT NULL DEFAULT 'in_transit'
                             CHECK (status IN ('in_transit','arrived')),
                eta_date     TEXT,
                arrived_at   TEXT,
                notes        TEXT,
                created_by   BIGINT NOT NULL,
                created_at   TEXT,
                updated_at   TEXT
            )""",
            # Связь контейнера с МойСклад: поставщик (нужен «Приёмке») и id
            # созданного документа. Отдельной таблицей, а не колонками в
            # `containers`: инкрементальных миграций в проекте нет, и новая
            # колонка не доехала бы до уже существующей таблицы на проде.
            """CREATE TABLE IF NOT EXISTS container_supply (
                container_id   INTEGER PRIMARY KEY REFERENCES containers(id),
                supplier_ms_id TEXT,
                supplier_name  TEXT,
                ms_supply_id   TEXT,
                synced_at      TEXT,
                unmatched      TEXT,
                updated_at     TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS container_items (
                id            {id_type},
                container_id  INTEGER NOT NULL REFERENCES containers(id),
                name          TEXT NOT NULL,
                unit          TEXT NOT NULL DEFAULT 'шт',
                expected_qty  {qty_type} NOT NULL DEFAULT 0,
                arrived_qty   {qty_type},
                note          TEXT,
                created_at    TEXT
            )""",
            # Позиция приёмки ↔ карточка номенклатуры МойСклад. Пока связи не
            # было, оприходование угадывало товар по названию — и «Кабель PV
            # 0.6» из накладной не находил «Кабель PV 0,6» из каталога.
            # Отдельная таблица, а не колонка в `container_items`:
            # инкрементальных миграций в проекте нет, и колонка в уже
            # существующую на проде таблицу просто не доехала бы.
            # `container_id` дублируется намеренно — по нему состав чистится
            # одним DELETE и его же видит сторож сирот (containers.CHILD_TABLES).
            """CREATE TABLE IF NOT EXISTS container_item_links (
                item_id      INTEGER PRIMARY KEY REFERENCES container_items(id),
                container_id INTEGER NOT NULL REFERENCES containers(id),
                ms_id        TEXT NOT NULL,
                ms_name      TEXT,
                created_at   TEXT
            )""",
            # Приёмка контейнера в ЛОКАЛЬНЫЙ склад. Отдельная таблица, а не
            # новые колонки в `container_supply`: там лежат идентификаторы
            # МойСклад (TEXT-uuid), а здесь — id наших `counterparties` и
            # `invoices` (BIGINT). Класть целое в колонку с именем
            # `supplier_ms_id` значит завести поле, смысл которого зависит от
            # эпохи записи; такие поля разъезжаются молча.
            """CREATE TABLE IF NOT EXISTS container_receipt (
                container_id  INTEGER PRIMARY KEY REFERENCES containers(id),
                supplier_id   BIGINT,
                supplier_name TEXT,
                invoice_id    BIGINT,
                received_at   TEXT,
                unmatched     TEXT,
                updated_at    TEXT
            )""",
            # Позиция приёмки ↔ карточка локальной номенклатуры. Замена
            # `container_item_links` (там `ms_id` — uuid МойСклад): та таблица
            # остаётся только источником для backfill'а, новые связи пишутся
            # сюда. `container_id` дублируется намеренно — по нему состав
            # чистится одним DELETE, и его же видит сторож сирот.
            """CREATE TABLE IF NOT EXISTS container_item_products (
                item_id      INTEGER PRIMARY KEY REFERENCES container_items(id),
                container_id INTEGER NOT NULL REFERENCES containers(id),
                product_id   BIGINT NOT NULL,
                created_at   TEXT
            )""",
            # ─── Себестоимость (services/costing.py) ─────────────────────
            # Всё за выключателем app_settings.accounting_enabled. Новые
            # таблицы, а не колонки в `containers`/`invoice_items`: колонка в
            # существующую на проде таблицу не доехала бы (ALTER запрещён).
            #
            # Курсы — TEXT (Decimal строкой), а не REAL: REAL на Postgres это
            # float4, и курс сума к доллару (0.000079…) терял бы знаки ровно
            # там, где из него считается себестоимость партии.
            #
            # Шапка закупки контейнера: валюта и курс НА ДАТУ ПРИБЫТИЯ. Курс
            # хранится так, как его знают на площадке, — «сум за 1 USD» и
            # «сум за 1 единицу валюты закупки»; курс к базовой выводится.
            """CREATE TABLE IF NOT EXISTS container_costing (
                container_id INTEGER PRIMARY KEY REFERENCES containers(id),
                currency     TEXT NOT NULL,
                uzs_per_usd  TEXT NOT NULL,
                uzs_per_unit TEXT NOT NULL,
                rate_source  TEXT,
                rate_date    TEXT,
                updated_by   BIGINT,
                updated_at   TEXT
            )""",
            # Цена закупки за единицу по позиции контейнера, в валюте шапки.
            """CREATE TABLE IF NOT EXISTS container_item_costs (
                item_id          INTEGER PRIMARY KEY REFERENCES container_items(id),
                container_id     INTEGER NOT NULL REFERENCES containers(id),
                unit_price_cents BIGINT NOT NULL,
                updated_by       BIGINT,
                updated_at       TEXT
            )""",
            # Партия = строка приходной накладной с ценой и курсом. FIFO берёт
            # из партий по порядку; `total_cost_base_cents` NULL — цену ещё не
            # вписали (партия всё равно занимает место в очереди). Остаток
            # партии не хранится — выводится из `sale_costs`, иначе отмена
            # отгрузки должна была бы помнить, что вернуть.
            f"""CREATE TABLE IF NOT EXISTS cost_batches (
                id                    {id_type},
                invoice_id            BIGINT NOT NULL,
                product_id            BIGINT NOT NULL,
                container_id          BIGINT,
                batch_date            TEXT NOT NULL,
                quantity              {qty_type} NOT NULL,
                unit_price_cents      BIGINT,
                currency              TEXT NOT NULL,
                rate_to_base          TEXT,
                total_cost_base_cents BIGINT,
                ref_rates             TEXT,
                created_at            TEXT NOT NULL
            )""",
            # Себестоимость, ЗАФИКСИРОВАННАЯ при отгрузке: сколько из какой
            # партии ушло и почём. Прибыль прошлых продаж читается отсюда и не
            # «плывёт» от новых закупок. batch_id NULL — товар старше учёта
            # (остаток до включения): себестоимость ручная или неизвестна.
            f"""CREATE TABLE IF NOT EXISTS sale_costs (
                id                {id_type},
                invoice_id        BIGINT NOT NULL,
                product_id        BIGINT NOT NULL,
                batch_id          BIGINT,
                quantity          {qty_type} NOT NULL,
                cost_base_cents   BIGINT,
                cost_source       TEXT NOT NULL,
                sale_price_cents  BIGINT NOT NULL,
                currency          TEXT NOT NULL,
                sale_rate_to_base TEXT,
                ref_rate_to_base  TEXT,
                created_at        TEXT NOT NULL
            )""",
            # Позиция заказа ↔ карточка локальной номенклатуры. Отдельная
            # таблица, а не значение в `order_items.product_href`: там лежит
            # ССЫЛКА на документ МойСклад, и класть в колонку с таким именем
            # целочисленный id значит завести поле, смысл которого зависит от
            # эпохи записи. Заполняется при добавлении позиции; у строк,
            # заведённых до перехода, — backfill'ом через `products.legacy_ms_id`.
            """CREATE TABLE IF NOT EXISTS order_item_products (
                item_id    INTEGER PRIMARY KEY REFERENCES order_items(id),
                order_id   BIGINT NOT NULL,
                product_id BIGINT NOT NULL,
                created_at TEXT
            )""",
            # Отгрузка заказа = расходная накладная локального склада. Раньше
            # это был demand в МойСклад, и его id лежал в `orders.ms_demand_id`;
            # держать там теперь номер нашей накладной значило бы оставить в
            # схеме колонку, название которой врёт про содержимое.
            #
            # `failed_at`/`error` — замена флагу `ms_demand_failed_at`: заявка
            # одобрена, а списать остаток не вышло (не хватило товара, позиции
            # не сопоставлены). Такой заказ обязан попасть в дайджест «нужна
            # доделка», иначе он выглядит отгруженным, а склад с ним не сошёлся.
            """CREATE TABLE IF NOT EXISTS order_shipment (
                order_id   BIGINT PRIMARY KEY,
                invoice_id BIGINT,
                shipped_at TEXT,
                failed_at  TEXT,
                error      TEXT
            )""",
            # Возврат товара = приходная накладная. Раньше остаток на склад
            # возвращал МойСклад документом «Возврат покупателя»; после его
            # удаления подтверждённый возврат менял только деньги и returned_qty,
            # а товар на складе не появлялся. Отдельная таблица, а не колонка в
            # `returns` (та уже на проде). PRIMARY KEY по return_id — второй
            # рубеж идемпотентности после CAS статуса: один возврат — одна
            # накладная. `invoice_id IS NULL` + `skipped_reason` — возврат
            # подтверждён, но склад сознательно не двигали (см. confirm_return).
            """CREATE TABLE IF NOT EXISTS return_receipt (
                return_id      BIGINT PRIMARY KEY,
                order_id       BIGINT NOT NULL,
                invoice_id     BIGINT,
                skipped_reason TEXT,
                unmatched      TEXT,
                created_at     TEXT
            )""",
        ]
    # Бухгалтерия (счета, журнал денег) — схема в leaf-модуле: сервис
    # импортирует database, и объявление здесь дало бы цикл импортов.
    from services.accounting_schema import tables as _accounting_tables

    tables.extend(_accounting_tables(id_type))
    # «Как получены деньги» (разбивка оплаты, валюта и строки сдачи) — тоже leaf.
    from services.order_payments_schema import tables as _order_payments_tables

    tables.extend(_order_payments_tables(id_type))
    # «Долги поставщикам» (условия оплаты прихода, «с чего заплатили») — тоже leaf.
    from services.supplier_debts_schema import tables as _supplier_debts_tables

    tables.extend(_supplier_debts_tables(id_type))

    return tables


def _create_tables():
    """Только CREATE TABLE IF NOT EXISTS. Idempotent, безопасен
    при concurrent старте."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        # Создаём каждую таблицу в отдельной транзакции
        for sql in _table_ddls():
            try:
                cur.execute(sql)
                conn.commit()
            except Exception:
                conn.rollback()
                # Раньше ЛЮБАЯ ошибка логировалась как «таблица уже существует».
                # Нет прав, кривой тип, опечатка в DDL — всё выглядело безобидно,
                # а узнавали об этом по 500-й в рантайме (§2.16). CREATE TABLE
                # IF NOT EXISTS на существующей таблице не бросает вовсе, значит
                # исключение здесь — всегда НАСТОЯЩАЯ проблема.
                logger.exception("CREATE TABLE не выполнен — схема неполная")


def _index_ddls() -> list[str]:
    """Определения всех индексов — ОДИН источник и для `_create_indexes`, и
    для сверки при старте (`startup_checks.check_indexes`): индекс, который
    молча не создался, виден только так."""
    # Элементы списка оставлены с прежним отступом — см. _table_ddls.
    snapshot_indexes = [
            # ─── Локальный складской учёт ────────────────────────────
            # Номер накладной — бизнес-ключ. UNIQUE ловит гонку двух
            # параллельных создающих транзакций: счётчик инкрементится
            # атомарно, но индекс — последний рубеж, если кто-то вставит
            # номер в обход счётчика (импорт, ручной фикс).
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_number "
            "ON invoices(invoice_number)",
            # Партиальные UNIQUE по legacy_ms_id: сверка миграции требует
            # ровно одну локальную строку на UUID МойСклад. NULL (товары,
            # заведённые уже после перехода) не конфликтуют.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_legacy_ms "
            "ON products(legacy_ms_id) WHERE legacy_ms_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_counterparties_legacy_ms "
            "ON counterparties(legacy_ms_id) WHERE legacy_ms_id IS NOT NULL",
            # SKU уникален, но только среди заполненных — в МойСклад код
            # товара необязателен, и после миграции часть строк будет с NULL.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_products_sku "
            "ON products(sku) WHERE sku IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice "
            "ON invoice_items(invoice_id)",
            # Идемпотентность переноса истории: повторный прогон находит
            # платёж по родному id МойСклад и обновляет, а не создаёт второй.
            # Партиальный — платежи, заведённые не переносом, не конфликтуют.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_payments_ms_unique "
            "ON supplier_payments(ms_paymentout_id) WHERE ms_paymentout_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_supplier_payments_cp "
            "ON supplier_payments(counterparty_id)",
            "CREATE INDEX IF NOT EXISTS idx_invoices_counterparty "
            "ON invoices(counterparty_id)",
            # Список накладных в WebApp: сортировка по дате, фильтр по типу.
            "CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date)",
            # Отгрузки за период (warehouse.list_shipments → отчёт продаж,
            # аналитика): type = 'outgoing' AND status = 'confirmed' AND
            # invoice_date в диапазоне ORDER BY invoice_date DESC, id DESC —
            # индекс отдаёт строки уже в нужном порядке.
            "CREATE INDEX IF NOT EXISTS idx_invoices_type_status_date "
            "ON invoices(type, status, invoice_date, id)",
            # Обратный поиск «локальный id → ms_id» при сверке миграции.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ms_id_map_local "
            "ON ms_id_map(entity_type, local_id)",
            # Уникальность paymentin'ов в МойСклад. Спасает от race condition
            # между cron-retry и confirm-hook: если оба попробуют создать
            # paymentin для одного платежа, второй INSERT упадёт на UNIQUE
            # constraint, а не наплодит дубликаты в МойСклад. Partial index —
            # чтобы NULL'ы (ещё не синхронизированные) не конфликтовали.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_ms_paymentin_unique "
            "ON payments(ms_paymentin_id) WHERE ms_paymentin_id IS NOT NULL",
            # Фильтры по статусу: get_paid_orders_awaiting_confirmation (orders)
            # и get_pending_requests (shipment_requests) сканируют по status.
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
            "CREATE INDEX IF NOT EXISTS idx_shipment_requests_status ON shipment_requests(status)",
            # IMPLEMENTATION.md Фаза 1–2: индексы новых таблиц.
            "CREATE INDEX IF NOT EXISTS idx_cash_deposits_manager_status ON cash_deposits(manager_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_cash_deposits_pending ON cash_deposits(status, deposited_at)",
            "CREATE INDEX IF NOT EXISTS idx_returns_order ON returns(order_id)",
            "CREATE INDEX IF NOT EXISTS idx_returns_status ON returns(status)",
            "CREATE INDEX IF NOT EXISTS idx_change_log_order ON order_change_log(order_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_keys(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_task_started ON cron_runs(task_name, started_at)",
            # Долг агента (get_agent_current_debt) и check_credit_limit (на каждом
            # одобрении после энфорса #29) фильтруют orders по agent_id — без
            # индекса full scan по orders.
            "CREATE INDEX IF NOT EXISTS idx_orders_agent_id ON orders(agent_id)",
            # Заказы менеджера (get_user_orders, аналитика) — фильтр+сортировка.
            "CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at)",
            # Все заказы свежими вперёд (get_all_orders → /api/orders босса и
            # кладовщика; страница `limit`): ORDER BY created_at DESC без
            # фильтра по менеджеру. id — развязка одинаковых секунд, иначе
            # страницы на границе теряют или дублируют строку.
            "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at, id)",
            # Аудит: get_audit_log сортирует по created_at, prune_audit_log
            # фильтрует по нему.
            "CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)",
            # WP-26: лента «Движение денег» (get_cash_history) и касса/отчёты
            # делают ORDER BY created_at DESC LIMIT / range по created_at на этих
            # трёх таблицах — без индекса full scan + sort, дорожает с ростом БД.
            "CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at)",
            # «Мои платежи» менеджера (/api/payments/my): WHERE user_id ORDER BY
            # created_at DESC LIMIT 50 — без пары колонок это скан всех платежей.
            "CREATE INDEX IF NOT EXISTS idx_payments_user_created ON payments(user_id, created_at)",
            # Итог «Деньги» и лента движения денег (get_money_totals,
            # get_cash_history) режут период по COALESCE(confirmed_at,
            # created_at) среди подтверждённых. Индекс по выражению, частичный:
            # pending/rejected в итог не входят. SQLite умеет и то и другое
            # (3.9+), поэтому определение общее.
            "CREATE INDEX IF NOT EXISTS idx_payments_confirmed_period "
            "ON payments((COALESCE(confirmed_at, created_at))) WHERE status = 'confirmed'",
            "CREATE INDEX IF NOT EXISTS idx_cash_deposits_created ON cash_deposits(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_returns_created ON returns(created_at)",
            # ── Уникальность (§2.1, §3.4) ────────────────────────────────────
            # Одна pending-заявка на заказ. Закрывает двойной сабмит на уровне
            # БД: второй INSERT падает на constraint, а не плодит две заявки
            # (и, следом, два customerorder+demand в МС с двойным списанием).
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_shipment_requests_one_pending "
            "ON shipment_requests(order_id) WHERE status = 'pending'",
            # Один pending-возврат на заказ. Раньше защищал только
            # advisory-lock, и только на Postgres.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_returns_one_pending "
            "ON returns(order_id) WHERE status = 'pending'",
            # Однозначность обратного поиска по документам МС:
            # find_order_by_ms_customerorder_id брал LIMIT 1 из потенциально
            # нескольких строк.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_ms_customerorder "
            "ON orders(ms_customerorder_id) WHERE ms_customerorder_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_ms_demand "
            "ON orders(ms_demand_id) WHERE ms_demand_id IS NOT NULL",
            # ── Под реальные фильтры (§3.8) ──────────────────────────────────
            # PK (deposit_id, order_id) не работает для поиска по order_id, а по
            # нему идут _order_confirmed_deposit_cents / _order_allocated_deposit_cents
            # / get_confirmed_deposit_cents_for_orders.
            "CREATE INDEX IF NOT EXISTS idx_cash_deposit_orders_order "
            "ON cash_deposit_orders(order_id)",
            # get_payments_for_order(s) + confirm_all_pending_* фильтруют по паре.
            # Он же покрывает «все платежи по заказу» (order_id — префикс), поэтому
            # отдельный idx_payments_order_id не нужен: лишний индекс только
            # удорожал бы каждую вставку платежа.
            "CREATE INDEX IF NOT EXISTS idx_payments_order_status "
            "ON payments(order_id, status)",
            # get_open_debts: payment_type='credit' + status IN (...) +
            # paid_confirmed_at IS NULL. Прежний idx_orders_credit_due был
            # (payment_type, paid_at, due_date) — запрос им не покрывался.
            "CREATE INDEX IF NOT EXISTS idx_orders_debt_lookup "
            "ON orders(payment_type, status, paid_confirmed_at)",
            # ── Машины (T4.1) ────────────────────────────────────────────────
            # Список машин всегда фильтруется по статусу (витрина «в наличии»,
            # «в пути», архив).
            "CREATE INDEX IF NOT EXISTS idx_machines_status ON machines(status)",
            # История моточасов и сделок читается «последние сверху» по машине.
            "CREATE INDEX IF NOT EXISTS idx_machine_hours_machine "
            "ON machine_hours(machine_id, recorded_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_machine_deals_machine "
            "ON machine_deals(machine_id, sold_at DESC)",
            # График читается по сделке (карточка — её покрывает UNIQUE
            # (deal_id, seq) из определения таблицы) и по сроку (ежедневный
            # обход неоплаченных платежей в напоминалке).
            "CREATE INDEX IF NOT EXISTS idx_machine_deal_payments_due "
            "ON machine_deal_payments(due_date)",
            "CREATE INDEX IF NOT EXISTS idx_machine_receipts_deal "
            "ON machine_payment_receipts(deal_id, received_at)",
            "CREATE INDEX IF NOT EXISTS idx_machine_receipt_accounts_account "
            "ON machine_receipt_accounts(account_id)",
            # Одна живая заявка на машину — инвариант, а не оптимизация: две
            # одновременные «продажи» одной машины от двух менеджеров иначе обе
            # дошли бы до руководителя. Сервис проверяет это под блокировкой
            # машины, индекс — последний рубеж.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_machine_deal_requests_active "
            "ON machine_deal_requests(machine_id) WHERE status IN ('pending', 'rework')",
            # Очередь решений руководителя — по статусу, старые сверху.
            "CREATE INDEX IF NOT EXISTS idx_machine_deal_requests_status "
            "ON machine_deal_requests(status, submitted_at)",
            # ── Контейнеры ───────────────────────────────────────────────────
            # Воронка: список фильтруется по менеджеру и по последней
            # активности, события читаются по лиду.
            "CREATE INDEX IF NOT EXISTS idx_channel_posts_ref ON channel_posts(kind, ref)",
            "CREATE INDEX IF NOT EXISTS idx_leads_manager ON leads(manager_id)",
            "CREATE INDEX IF NOT EXISTS idx_leads_last_inbound ON leads(last_inbound_at)",
            "CREATE INDEX IF NOT EXISTS idx_lead_events_lead ON lead_events(lead_id, at)",
            "CREATE INDEX IF NOT EXISTS idx_lead_events_at ON lead_events(at)",
            # Непривязанные звонки — самый частый запрос экрана («кому
            # перезвонить»), поэтому индекс именно по нему, а не по дате.
            "CREATE INDEX IF NOT EXISTS idx_lead_calls_lead ON lead_calls(lead_id, at)",
            "CREATE INDEX IF NOT EXISTS idx_lead_calls_at ON lead_calls(at)",
            "CREATE INDEX IF NOT EXISTS idx_containers_status ON containers(status)",
            "CREATE INDEX IF NOT EXISTS idx_container_items_container "
            "ON container_items(container_id, id)",
            # Связки чистятся и читаются по контейнеру (удаление состава,
            # сторож сирот), а PK стоит на item_id — без этого индекса каждая
            # такая операция читала бы таблицу целиком.
            "CREATE INDEX IF NOT EXISTS idx_container_item_products_container "
            "ON container_item_products(container_id)",
            # Себестоимость: одна партия на (накладная, товар) — повторный
            # хук не заведёт вторую; FIFO читает партии по товару, отчёт —
            # фиксации по накладной и по партии.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_batches_invoice_product "
            "ON cost_batches(invoice_id, product_id)",
            "CREATE INDEX IF NOT EXISTS idx_cost_batches_product "
            "ON cost_batches(product_id, batch_date)",
            "CREATE INDEX IF NOT EXISTS idx_cost_batches_container "
            "ON cost_batches(container_id)",
            "CREATE INDEX IF NOT EXISTS idx_container_item_costs_container "
            "ON container_item_costs(container_id)",
            "CREATE INDEX IF NOT EXISTS idx_sale_costs_invoice ON sale_costs(invoice_id)",
            "CREATE INDEX IF NOT EXISTS idx_sale_costs_batch ON sale_costs(batch_id)",
            "CREATE INDEX IF NOT EXISTS idx_sale_costs_product ON sale_costs(product_id)",
            "CREATE INDEX IF NOT EXISTS idx_order_item_products_order "
            "ON order_item_products(order_id)",
            "CREATE INDEX IF NOT EXISTS idx_order_item_products_product "
            "ON order_item_products(product_id)",
            "CREATE INDEX IF NOT EXISTS idx_order_shipment_failed "
            "ON order_shipment(failed_at) WHERE failed_at IS NOT NULL",
            # ── Одна накладная — один владелец ───────────────────────────────
            # Отгрузка заказа, приход возврата и приёмка контейнера ссылаются на
            # накладную, и каждая такая накладная принадлежит ровно одному
            # документу: отмена отгрузки одного заказа иначе отменила бы
            # списание другого, а сверка «остаток не списан» считала бы товар
            # дважды. Код дублей не пишет (повторная приёмка ОТМЕНЯЕТ старую
            # накладную и заводит новую, отмена отгрузки обнуляет ссылку), так
            # что индекс — последний рубеж от ручной правки и импорта.
            # Частичные: пустая ссылка (отгрузка не прошла, склад не двигали,
            # контейнер оприходован ещё в МС) законна у любого числа строк.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_order_shipment_invoice "
            "ON order_shipment(invoice_id) WHERE invoice_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_return_receipt_invoice "
            "ON return_receipt(invoice_id) WHERE invoice_id IS NOT NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_container_receipt_invoice "
            "ON container_receipt(invoice_id) WHERE invoice_id IS NOT NULL",
            # Позиции заказа и возврата читаются ТОЛЬКО по родителю: карточка
            # заказа, долг агента (батч по order_id), отгрузка, возврат. PK стоит
            # на id, и без этих индексов каждый такой запрос читал таблицу
            # целиком — самую длинную в схеме.
            "CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id)",
            "CREATE INDEX IF NOT EXISTS idx_return_items_return ON return_items(return_id)",
            # Одна позиция заказа — одна строка в возврате. Две строки на одну
            # позицию проходят проверку «не больше доступного» каждая по
            # отдельности, и подтверждение возврата упиралось в overshoot уже
            # после того, как деньги посчитаны. Ручка отвергает повтор позиции
            # раньше (400), индекс — рубеж для любого другого пути.
            # idx_return_items_return выше НЕ убираем, хотя он префикс этого:
            # не создайся UNIQUE из-за дублей в данных — чтение позиций по
            # возврату осталось бы без индекса вовсе.
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_return_items_return_item "
            "ON return_items(return_id, order_item_id)",
        ]
    from services.accounting_schema import INDEXES as _accounting_indexes

    snapshot_indexes.extend(_accounting_indexes)
    from services.order_payments_schema import INDEXES as _order_payments_indexes

    snapshot_indexes.extend(_order_payments_indexes)
    from services.supplier_debts_schema import INDEXES as _supplier_debts_indexes

    snapshot_indexes.extend(_supplier_debts_indexes)
    return snapshot_indexes


# Индексы, убранные из `_index_ddls` как лишние. Удаление строки из списка
# индекс на существующей базе НЕ удаляет — это делает разовый
# `scripts/apply_constraints --apply` (DROP INDEX IF EXISTS). Список здесь, а не
# в скрипте, чтобы сторож (`tests/test_db_constraints.py`) видел, что убранный
# индекс не вернулся в определения.
DROPPED_INDEXES: dict[str, str] = {
    "idx_machine_deal_payments_deal": "дубль UNIQUE (deal_id, seq) из определения таблицы",
    "idx_product_photos_ms": "префикс UNIQUE (ms_id, file_unique_id)",
    "idx_credit_limits_updated_at": "ни один запрос не фильтрует и не сортирует по updated_at",
    "idx_payments_pending": (
        "платежи по статусу ищутся только вместе с order_id — это idx_payments_order_status"
    ),
    "idx_user_roles_role": "таблица на десятки строк; get_all_users сортирует её целиком",
    "idx_lead_calls_phone": "по phone_key нет ни одного запроса",
}


def _create_indexes() -> list[str]:
    """CREATE INDEX IF NOT EXISTS — idempotent, гоняется при каждом старте.

    Возвращает список индексов, которые создать НЕ удалось. Раньше отказ
    уходил в DEBUG, и индекс, не созданный из-за дублей в данных (UNIQUE) или
    опечатки, было не увидеть ничем, кроме медленного запроса. Теперь ERROR в
    лог; алерт админам поднимает `startup_checks` — он сверяет имена индексов
    живой базы с `_index_ddls` (здесь async-алертам взяться неоткуда: init_db
    синхронный и зовётся и из cron-CLI).
    """
    failed: list[str] = []
    with get_conn() as conn:
        cur = get_cursor(conn)
        for sql in _index_ddls():
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as e:
                conn.rollback()
                failed.append(sql)
                logger.error("Индекс не создан: %s — %s: %s", sql, type(e).__name__, e)
    return failed


def seed_warehouses() -> int:
    """Засеять склад по умолчанию. Идемпотентно — сеем только в пустую
    таблицу, чтобы переименованный вручную склад не продублировался."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("SELECT COUNT(*) AS c FROM warehouses")
        row = cur.fetchone()
        count = (row["c"] if isinstance(row, dict) else row[0]) or 0
        if count:
            return 0
        cur.execute(q("INSERT INTO warehouses (name) VALUES (?)"), ("Основной склад",))
        conn.commit()
        logger.info("Засеян склад по умолчанию «Основной склад»")
        return 1


def backfill_container_receipts() -> dict:
    """Перенести связки приёмки контейнеров со старых MS-таблиц на локальные.

    Что переносим:
      • `container_item_links.ms_id` (uuid карточки МойСклад) → `product_id`
        нашей номенклатуры, через `products.legacy_ms_id`, который проставила
        миграция каталога;
      • `container_supply` (поставщик + отметка синхронизации) →
        `container_receipt` с локальным `supplier_id`.

    **`invoice_id` намеренно остаётся пустым.** Приход по таким контейнерам
    делал МойСклад, и его результат приехал к нам остатками — миграцией
    каталога и склада. Завести здесь локальную накладную значило бы прибавить
    тот же товар второй раз. Поэтому `received_at` переносится (контейнер
    считается оприходованным и кнопка не предлагается), а `invoice_id` пуст;
    `container_receipt.receive` такое сочетание распознаёт и отказывает явным
    текстом, а не заводит дубль.

    Идемпотентно: строки, которые уже есть, не трогаем.
    """
    stamp = now_str()
    stats = {"items": 0, "containers": 0, "errors": 0}
    with get_conn() as conn:
        cur = get_cursor(conn)
        try:
            cur.execute(
                q(
                    "INSERT INTO container_item_products "
                    "    (item_id, container_id, product_id, created_at) "
                    "SELECT l.item_id, l.container_id, p.id, ? "
                    "FROM container_item_links l "
                    "JOIN products p ON p.legacy_ms_id = l.ms_id "
                    "WHERE NOT EXISTS ("
                    "    SELECT 1 FROM container_item_products cp WHERE cp.item_id = l.item_id"
                    ")"
                ),
                (stamp,),
            )
            stats["items"] = max(cur.rowcount, 0)
            cur.execute(
                q(
                    "INSERT INTO container_receipt "
                    "    (container_id, supplier_id, supplier_name, received_at, "
                    "     unmatched, updated_at) "
                    "SELECT s.container_id, c.id, s.supplier_name, s.synced_at, "
                    "       s.unmatched, ? "
                    "FROM container_supply s "
                    "LEFT JOIN counterparties c ON c.legacy_ms_id = s.supplier_ms_id "
                    "WHERE NOT EXISTS ("
                    "    SELECT 1 FROM container_receipt r WHERE r.container_id = s.container_id"
                    ")"
                ),
                (stamp,),
            )
            stats["containers"] = max(cur.rowcount, 0)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("Backfill приёмки контейнеров: %s", e)
            return {"items": 0, "containers": 0, "errors": 1}
    if stats["items"] or stats["containers"]:
        logger.info(
            "Backfill приёмки контейнеров: позиций %d, контейнеров %d",
            stats["items"], stats["containers"],
        )
    return stats


def backfill_local_identifiers() -> dict:
    """Переписать ссылки на МойСклад на наши собственные id.

    После переноса каталога и справочника контрагентов (`legacy_ms_id` у
    `products`/`counterparties`) в старых строках всё ещё лежат UUID МойСклад:
    `orders.agent_id`, `credit_limits.agent_id`, `machine_deals.agent_ms_id`,
    `leads.agent_ms_id`, `product_photos.ms_id`, `order_items.product_href`.
    Пока они там, один и тот же клиент существует под двумя ключами — старые
    заказы под UUID, новые под нашим id, — и долг по нему считается дважды по
    половинке.

    Колонки НЕ переименовываем и не добавляем: инкрементальных миграций в
    проекте нет. `agent_id` — имя нейтральное («идентификатор контрагента»), и
    после этого прогона в нём лежит наш id строкой. Там, где имя колонки
    говорит про МойСклад по существу (`order_items.product_href` — это ссылка
    на документ), связь уезжает в отдельную таблицу `order_item_products`.

    Идемпотентно: строка, у которой уже стоит локальный id, под условие
    `legacy_ms_id = <значение>` больше не попадает.
    """
    stats: dict[str, int] = {}
    errors = 0
    # (метка, SQL). Каждый шаг — своей транзакцией: упавший не должен уносить
    # остальные, а частично переписанные ссылки чинятся повторным прогоном.
    steps = [
        (
            "orders",
            "UPDATE orders SET agent_id = ("
            "    SELECT CAST(c.id AS TEXT) FROM counterparties c "
            "    WHERE c.legacy_ms_id = orders.agent_id) "
            "WHERE agent_id IS NOT NULL AND EXISTS ("
            "    SELECT 1 FROM counterparties c WHERE c.legacy_ms_id = orders.agent_id)",
        ),
        (
            "credit_limits",
            "UPDATE credit_limits SET agent_id = ("
            "    SELECT CAST(c.id AS TEXT) FROM counterparties c "
            "    WHERE c.legacy_ms_id = credit_limits.agent_id) "
            "WHERE EXISTS ("
            "    SELECT 1 FROM counterparties c WHERE c.legacy_ms_id = credit_limits.agent_id)",
        ),
        (
            "machine_deals",
            "UPDATE machine_deals SET agent_ms_id = ("
            "    SELECT CAST(c.id AS TEXT) FROM counterparties c "
            "    WHERE c.legacy_ms_id = machine_deals.agent_ms_id) "
            "WHERE agent_ms_id IS NOT NULL AND EXISTS ("
            "    SELECT 1 FROM counterparties c WHERE c.legacy_ms_id = machine_deals.agent_ms_id)",
        ),
        (
            "leads",
            "UPDATE leads SET agent_ms_id = ("
            "    SELECT CAST(c.id AS TEXT) FROM counterparties c "
            "    WHERE c.legacy_ms_id = leads.agent_ms_id) "
            "WHERE agent_ms_id IS NOT NULL AND EXISTS ("
            "    SELECT 1 FROM counterparties c WHERE c.legacy_ms_id = leads.agent_ms_id)",
        ),
        (
            "product_prices",
            "UPDATE product_prices SET ms_id = ("
            "    SELECT CAST(p.id AS TEXT) FROM products p "
            "    WHERE p.legacy_ms_id = product_prices.ms_id) "
            "WHERE EXISTS ("
            "    SELECT 1 FROM products p WHERE p.legacy_ms_id = product_prices.ms_id)",
        ),
        (
            "product_photos",
            "UPDATE product_photos SET ms_id = ("
            "    SELECT CAST(p.id AS TEXT) FROM products p "
            "    WHERE p.legacy_ms_id = product_photos.ms_id) "
            "WHERE EXISTS ("
            "    SELECT 1 FROM products p WHERE p.legacy_ms_id = product_photos.ms_id)",
        ),
    ]
    with get_conn() as conn:
        cur = get_cursor(conn)
        for label, sql in steps:
            try:
                cur.execute(sql)
                stats[label] = max(cur.rowcount, 0)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("Backfill ссылок (%s): %s", label, e)
                stats[label] = 0
                errors += 1

        # Позиции заказов: в `product_href` лежит ССЫЛКА, id из неё надо
        # выкусить — в SQL это делается по-разному на двух движках, поэтому
        # разбираем в Python. Строк немного (только те, что ещё не связаны).
        try:
            cur.execute(
                "SELECT oi.id, oi.order_id, oi.product_href FROM order_items oi "
                "WHERE oi.product_href IS NOT NULL AND oi.product_href <> '' "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM order_item_products op WHERE op.item_id = oi.id)"
            )
            pending = [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.warning("Backfill позиций заказов (чтение): %s", e)
            pending = []
            errors += 1

        linked = 0
        if pending:
            from utils.helpers import extract_id_from_href

            cur.execute(
                "SELECT id, legacy_ms_id FROM products WHERE legacy_ms_id IS NOT NULL"
            )
            by_ms = {
                str(r["legacy_ms_id"] if isinstance(r, dict) else r[1]): int(
                    r["id"] if isinstance(r, dict) else r[0]
                )
                for r in cur.fetchall()
            }
            stamp = now_str()
            for row in pending:
                ms_id = extract_id_from_href(str(row["product_href"] or ""))
                product_id = by_ms.get(ms_id)
                if not product_id:
                    continue
                try:
                    cur.execute(
                        q(
                            "INSERT INTO order_item_products "
                            "(item_id, order_id, product_id, created_at) VALUES (?, ?, ?, ?)"
                        ),
                        (int(row["id"]), int(row["order_id"]), product_id, stamp),
                    )
                    linked += 1
                except Exception as e:
                    conn.rollback()
                    logger.warning("Backfill позиции заказа #%s: %s", row["id"], e)
                    errors += 1
            conn.commit()
        stats["order_items"] = linked

    if any(stats.values()):
        logger.info("Backfill локальных id: %s", stats)
    stats["errors"] = errors
    return stats


def seed_document_templates() -> int:
    """Засеять шаблоны юридических документов. Идемпотентно по типу.

    file_path кладём ОТНОСИТЕЛЬНЫЙ (templates/legal/…): шаблон лежит в
    репозитории и едет с образом. Заменить его своим можно, прописав в этой
    строке абсолютный путь в томе /app/data — `legal_docs.template_path`
    предпочитает значение из БД, когда оно задано.
    """
    from services.legal_docs import TEMPLATES

    inserted = 0
    with get_conn() as conn:
        cur = get_cursor(conn)
        for doc_type, (filename, _parts) in TEMPLATES.items():
            cur.execute(
                q("SELECT id FROM document_templates WHERE type = ?"), (doc_type,)
            )
            if cur.fetchone():
                continue
            cur.execute(
                q(
                    "INSERT INTO document_templates (type, file_path, is_active, created_at) "
                    "VALUES (?, ?, 1, ?)"
                ),
                (doc_type, f"templates/legal/{filename}", now_str()),
            )
            inserted += 1
        conn.commit()
    if inserted:
        logger.info("Засеяно шаблонов документов: %d", inserted)
    return inserted


def backfill_legacy_paid_confirmed() -> dict:
    """Закрыть legacy-долги (paid_at стоит, платежей нет — эпоха до частичных
    оплат): paid_confirmed_at = paid_at. РАЗОВАЯ миграция, см. run_backfills."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        try:
            cur.execute(
                "UPDATE orders "
                "SET paid_confirmed_at = paid_at, "
                "    paid_confirmed_by = user_id, "
                "    paid_confirmed_by_name = COALESCE(full_name, '') "
                "WHERE paid_at IS NOT NULL AND paid_confirmed_at IS NULL "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM payments WHERE order_id = orders.id"
                "  )"
            )
            rows = max(cur.rowcount, 0)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning("Backfill paid_confirmed: %s", e)
            return {"orders": 0, "errors": 1}
    if rows > 0:
        logger.info("Backfill legacy: %d закрытых долгов автоподтверждены", rows)
    return {"orders": rows, "errors": 0}


# Разовые data-миграции: (имя, функция). Каждая выполняется ОДИН раз на базу —
# отметка `backfill_done:<имя>` в app_settings. Раньше `run_backfills` гонял их
# на КАЖДОМ `docker compose up` (tasks.migrate перед стартом сервисов), и на
# живых деньгах это была скрытая мутация при каждом деплое: legacy-UPDATE
# закрывал долг любому заказу, у которого стоит paid_at и нет строк payments
# (так выглядит, например, заказ, перенесённый из истории МС без платежа), а
# переписывание идентификаторов срабатывало на любой новой строке со старым
# UUID. Сидинг справочников (настройки, склад, шаблоны) остаётся ежедневным: он
# только вставляет отсутствующее и ничего не меняет.
ONE_TIME_BACKFILLS: tuple[tuple[str, Any], ...] = (
    ("legacy_paid_confirmed", lambda: backfill_legacy_paid_confirmed()),
    ("container_receipts", lambda: backfill_container_receipts()),
    ("local_identifiers", lambda: backfill_local_identifiers()),
)


def _backfill_flag(name: str) -> str:
    return f"backfill_done:{name}"


def _backfill_done_at(name: str) -> str | None:
    """Когда разовый backfill отработал (None — ещё не выполнялся). Мимо TTL-кэша
    настроек: решение «гонять или нет» должно видеть базу, а не память."""
    import json as _json

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT value FROM app_settings WHERE key = ?"), (_backfill_flag(name),))
        row = cur.fetchone()
    if not row:
        return None
    raw = row["value"] if hasattr(row, "keys") else row[0]
    try:
        return str(_json.loads(raw))
    except (TypeError, ValueError):
        return str(raw)


def run_backfills(rerun: tuple[str, ...] | list[str] = ()) -> dict:
    """Сидинг настроек/справочников + разовые data-миграции.

    Сидинг (`seed_app_settings`, `seed_warehouses`, `seed_document_templates`)
    идемпотентен и безвреден — выполняется каждый раз.

    Разовые миграции (`ONE_TIME_BACKFILLS`) выполняются, только пока у базы нет
    отметки `backfill_done:<имя>` в app_settings; отметка ставится, если шаг
    прошёл без ошибок. Повторить осознанно (например, после повторного переноса
    справочников из МойСклад) — `rerun=("local_identifiers",)` или «all»; из
    консоли: `python -m tasks.migrate --rerun-backfill local_identifiers`.

    Recovery-backfill (сброс paid_confirmed_at по сравнению SUM(amount)
    с SUM(quantity*price)) удалён в T1.3: он лечил данные, испорченные
    старым backfill-багом, и читал REAL-колонки денег, которых больше нет.

    Запускается из `tasks/migrate.py`. НЕ из init_db. Возвращает
    {имя: "skipped" | статистика шага} — для лога и тестов.
    """
    # ── Сидинг (идемпотентно, только вставка отсутствующего) ─────────
    seed_app_settings()
    seed_warehouses()
    seed_document_templates()

    forced = set(rerun or ())
    report: dict = {}
    for name, step in ONE_TIME_BACKFILLS:
        done_at = _backfill_done_at(name)
        if done_at and name not in forced and "all" not in forced:
            logger.info("Backfill %s уже выполнен (%s) — пропускаем", name, done_at)
            report[name] = "skipped"
            continue
        stats = step() or {}
        report[name] = stats
        if int(stats.get("errors") or 0) == 0:
            set_setting(_backfill_flag(name), now_str())
        else:
            logger.warning("Backfill %s прошёл с ошибками — повторится при следующем запуске", name)
    return report


# ─── Настройки приложения (app_settings) ──────────────────────────────────────
#
# Источник «магических чисел» (IMPLEMENTATION.md §3.13/§19). value хранится
# как JSON-текст (dual-DB: ни JSONB, ни native-типов). get_setting парсит JSON.

_DEFAULT_SETTINGS: dict[str, tuple] = {
    # key: (value, description)
    "credit_limit_default": (2000.0, "Дефолтный кредитный лимит для новых клиентов (USD)"),
    "cancellation_window_hours": (4, "Окно отмены одобренного заказа (часов)"),
    "cash_deposit_reminder_time": ("18:00", "Время напоминания о сдаче налички (Asia/Tashkent)"),
    "cash_deposit_escalation_days": (2, "Через сколько дней без сдачи — алерт боссам"),
    "stale_pending_hours": (48, "Через сколько часов pending-заявка считается зависшей"),
    "stale_pending_escalation_days": (5, "Через сколько дней — алерт-эскалация админу"),
    "reject_max_cycles": (3, "Максимум циклов reject→resubmit перед freeze"),
    "price_check_threshold_percent": (15, "Цена ниже прайса на X% → warning боссу"),
    "paid_order_confirmation_threshold": (
        500.0,
        "Сумма для двухступенчатого подтверждения paid-заказов",
    ),
    "audit_log_retention_months": (6, "Сколько месяцев аудита держим в БД"),
    "moysklad_retry_max_attempts": (3, "Макс попыток для МойСклад API"),
    "moysklad_circuit_breaker_threshold": (10, "Сколько фейлов за 5 мин → пауза"),
    "client_notifications_enabled": (True, "Глобальный switch уведомлений клиентам"),
    "return_deadline_days": (90, "Лимит на оформление возврата (дней с отгрузки)"),
    "auto_create_demand_on_approve": (True, "Создавать demand в МойСклад при approve"),
    "auto_ship_on_approve": (True, "Авто-переход в shipped сразу после approve"),
    "machines_archive_days": (90, "Через сколько дней проданная техника уходит в архив"),
    "boss_instant_threshold_usd": (
        5000.0,
        "Денежное событие ≥ этой суммы (USD) — боссу пуш сразу, не в дайджест",
    ),
    "boss_digest_time": (
        "19:00",
        "Время вечернего дайджеста решений боссу (Asia/Tashkent)",
    ),
    # Решение владельца: пока менеджер один, удалять технику, товары и
    # накладные может и он; включённый флаг оставляет это руководству.
    "delete_requires_boss": (False, "Удаление техники, товаров и накладных — только руководитель"),
    # Свой курс в оплате/документе бухгалтерии — не дальше X% от курса ЦБ
    # (services.accounting.manual_rate_refusal).
    "manual_rate_max_deviation_pct": (10, "Свой курс валюты — не дальше стольких % от курса ЦБ"),
}


def seed_app_settings() -> int:
    """Засеять дефолтные настройки, не перетирая уже изменённые. Возвращает
    число вставленных ключей."""
    import json as _json

    inserted = 0
    with get_conn() as conn:
        cur = get_cursor(conn)
        for key, (value, desc) in _DEFAULT_SETTINGS.items():
            try:
                if USE_POSTGRES:
                    cur.execute(
                        "INSERT INTO app_settings (key, value, description, updated_at) "
                        "VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                        (key, _json.dumps(value), desc, now_str()),
                    )
                else:
                    cur.execute(
                        "INSERT OR IGNORE INTO app_settings (key, value, description, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (key, _json.dumps(value), desc, now_str()),
                    )
                inserted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.debug("seed_app_settings %s: %s", key, e)
    return inserted


# TTL-кэш настроек: app_settings меняются редко, а читаются на горячих путях
# (кредитный дефолт, окна, пороги — иногда несколько раз за запрос). Ключ →
# (monotonic_ts, value). Инвалидация — в set_setting; в тестах кэш обнуляется
# reload'ом модуля (см. фикстуру isolated_db в conftest).
_SETTINGS_TTL = 120.0
_settings_cache: dict[str, tuple[float, Any]] = {}


def get_setting(key: str, default=None):
    """Прочитать настройку (JSON-десериализация value). Падать не должна —
    при любой проблеме возвращает default. Значение из БД/дефолтов кэшируется
    на _SETTINGS_TTL сек; переданный вызывающим default НЕ кэшируется."""
    import json as _json

    entry = _settings_cache.get(key)
    if entry is not None and time.monotonic() - entry[0] < _SETTINGS_TTL:
        return entry[1]
    try:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(q("SELECT value FROM app_settings WHERE key = ?"), (key,))
            row = cur.fetchone()
        if not row:
            # Не засеяно — берём из дефолтов, если есть (их тоже кэшируем).
            if key in _DEFAULT_SETTINGS:
                val = _DEFAULT_SETTINGS[key][0]
                _settings_cache[key] = (time.monotonic(), val)
                return val
            return default  # неизвестный ключ — не кэшируем чужой default
        raw = row["value"] if USE_POSTGRES else row[0]
        val = _json.loads(raw)
        _settings_cache[key] = (time.monotonic(), val)
        return val
    except Exception as e:
        logger.warning("get_setting %s failed: %s", key, e)
        return default


def set_setting(key: str, value, updated_by: int | None = None) -> None:
    """Записать настройку (value сериализуется в JSON). Создаёт ключ при отсутствии."""
    import json as _json

    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO app_settings (key, value, updated_by, updated_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at",
                (key, _json.dumps(value), updated_by, now_str()),
            )
        else:
            cur.execute(
                "INSERT INTO app_settings (key, value, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (key, _json.dumps(value), updated_by, now_str()),
            )
        conn.commit()
    _settings_cache.pop(key, None)  # инвалидация TTL-кэша


# ─── IMPLEMENTATION.md Фаза 3: кредитные лимиты ───────────────────────────────
#
# agent_id — UUID контрагента МойСклад. Дефолтный лимит — из app_settings
# (credit_limit_default). current_debt считаем из существующей платёжной
# модели (remaining по заказу) минус подтверждённые возвраты — без отдельного
# «долгового» поля, чтобы не плодить параллельную истину.


async def get_credit_limit(agent_id: str) -> float:
    """Лимит контрагента; если строки нет — дефолт из app_settings.

    asyncpg Stage 18 (#21): native async через adb_core; get_setting (sync,
    TTL-кэш) — мост через to_thread."""
    if not agent_id:
        return float(await asyncio.to_thread(get_setting, "credit_limit_default", 2000.0))
    val = await adb_core.fetchval(
        "SELECT limit_amount_cents FROM credit_limits WHERE agent_id = $1", agent_id
    )
    if val is not None:
        return float(money.from_cents(val))
    return float(await asyncio.to_thread(get_setting, "credit_limit_default", 2000.0))


async def ensure_credit_limit(agent_id: str, agent_name: str) -> None:
    """Завести строку лимита для нового клиента (set_by=NULL → «авто»).

    asyncpg Stage 18 (#21): native async через adb_core (INSERT ... DO NOTHING /
    INSERT OR IGNORE); get_setting — мост через to_thread."""
    if not agent_id:
        return
    default_cents = money.to_cents(
        await asyncio.to_thread(get_setting, "credit_limit_default", 2000.0)
    )
    if USE_POSTGRES:
        await adb_core.execute(
            "INSERT INTO credit_limits (agent_id, agent_name, limit_amount_cents, set_by, created_at, updated_at) "
            "VALUES ($1, $2, $3, NULL, $4, $5) ON CONFLICT (agent_id) DO NOTHING",
            agent_id, agent_name, default_cents, now_str(), now_str(),
        )
    else:
        await adb_core.execute(
            "INSERT OR IGNORE INTO credit_limits (agent_id, agent_name, limit_amount_cents, set_by, created_at, updated_at) "
            "VALUES ($1, $2, $3, NULL, $4, $5)",
            agent_id, agent_name, default_cents, now_str(), now_str(),
        )


async def set_credit_limit(
    agent_id: str,
    agent_name: str,
    limit_amount: float,
    set_by: int | None = None,
    notes: str | None = None,
) -> None:
    """Установить/изменить лимит + запись в audit_log.

    asyncpg Stage 18 (#21): native async через adb_core (UPSERT ON CONFLICT);
    add_audit_log/get_role (sync) — мост через to_thread."""
    limit_cents = money.to_cents(limit_amount or 0)
    if USE_POSTGRES:
        await adb_core.execute(
            "INSERT INTO credit_limits (agent_id, agent_name, limit_amount_cents, set_by, notes, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (agent_id) DO UPDATE SET "
            "limit_amount_cents = EXCLUDED.limit_amount_cents, "
            "set_by = EXCLUDED.set_by, notes = EXCLUDED.notes, updated_at = EXCLUDED.updated_at",
            agent_id, agent_name, limit_cents, set_by, notes, now_str(), now_str(),
        )
    else:
        await adb_core.execute(
            "INSERT INTO credit_limits (agent_id, agent_name, limit_amount_cents, set_by, notes, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT(agent_id) DO UPDATE SET "
            "limit_amount_cents = excluded.limit_amount_cents, "
            "set_by = excluded.set_by, notes = excluded.notes, updated_at = excluded.updated_at",
            agent_id, agent_name, limit_cents, set_by, notes, now_str(), now_str(),
        )
    if set_by:
        await asyncio.to_thread(
            add_audit_log,
            set_by,
            "",
            await asyncio.to_thread(get_role, set_by),
            "credit_limit_changed",
            f"{agent_name}: лимит → {limit_amount:.0f} USD" + (f" ({notes})" if notes else ""),
        )


async def get_confirmed_returns_cents_for_orders(order_ids: list[int]) -> dict[int, int]:
    """Подтверждённые (не-cash) возвраты по заказам, в копейках, батчем.
    Единый источник для долга — get_agent_current_debt и /api/debts (WP-06)."""
    if not order_ids:
        return {}
    ph = ", ".join(f"${i + 1}" for i in range(len(order_ids)))
    rows = await adb_core.fetch(
        f"SELECT order_id, {_SUM_RETURNS_CENTS} AS rc FROM returns "
        f"WHERE order_id IN ({ph}) AND {_RETURN_OWED_FILTER} GROUP BY order_id",
        *order_ids,
    )
    return {r["order_id"]: int(r["rc"] or 0) for r in rows}


async def get_confirmed_deposit_cents_for_orders(order_ids: list[int]) -> dict[int, int]:
    """Подтверждённые сдачи, распределённые на заказы, в копейках, батчем (WP-06)."""
    if not order_ids:
        return {}
    ph = ", ".join(f"${i + 1}" for i in range(len(order_ids)))
    rows = await adb_core.fetch(
        f"SELECT cdo.order_id AS oid, {_SUM_ALLOC_CENTS} AS dc "
        "FROM cash_deposit_orders cdo JOIN cash_deposits d ON d.id = cdo.deposit_id "
        f"WHERE cdo.order_id IN ({ph}) AND d.status = 'confirmed' "
        "GROUP BY cdo.order_id",
        *order_ids,
    )
    return {r["oid"]: int(r["dc"] or 0) for r in rows}


async def get_allocated_deposit_cents_for_orders(
    order_ids: list[int], conn=None
) -> dict[int, int]:
    """Распределено на заказы сдачами pending+confirmed, в копейках, батчем.

    В отличие от get_confirmed_deposit_cents_for_orders учитывает и pending:
    неподтверждённая сдача уже «застолбила» остаток, второй раз распределять
    его нельзя. `conn` — чтобы считать внутри транзакции под advisory-lock'ом.
    Определение — в services.debts (одно на сдачу и на отметку оплаты).
    """
    from services.debts import calc_allocated_deposit_cents

    return await calc_allocated_deposit_cents(order_ids, conn=conn)


async def deposit_remaining_cents_for_orders(
    order_ids: list[int], conn=None
) -> dict[int, int]:
    """Сколько ещё можно покрыть сдачей по каждому заказу, в копейках.

    total − возвраты − платежи (confirmed + PENDING) − распределённое сдачами
    (pending+confirmed). Отличается от services.debts.calc_remaining_cents тем,
    что видит и заявленное, но не подтверждённое: ожидающая отметка оплаты уже
    обещает эти деньги, и сдача поверх неё собирала по заказу больше его суммы
    (заказ 200, отмечено 150, сдача 200 → «получено» 350).

    Формула — services.debts.calc_claimable_cents, та же, что у mark_order_paid.
    `conn` — для расчёта внутри транзакции.
    """
    from services.debts import calc_claimable_cents

    return await calc_claimable_cents(order_ids, conn=conn)


async def get_agents_current_debt(agent_ids: list[str]) -> dict[str, float]:
    """Текущий долг КАЖДОГО из контрагентов одним проходом: {agent_id: долг}.

    Долг — сумма непогашенных остатков по открытым заказам минус подтверждённые
    возвраты, в базовой валюте. Открытые = не draft/rejected/cancelled/paid/
    returned. Заказы всех агентов читаются одним SELECT, позиции/платежи/
    возвраты/сдачи — батчем по всем заказам, остаток считается в Python (та же
    формула, что в get_order_payment_summary).

    Ради чего батч: список заявок босса звал долг по каждой заявке отдельно —
    по 6 запросов на строку, 60 заявок = 360 запросов (нашёл
    tests/perf/test_query_counts.py::test_pending_requests_are_batched).
    Контрагенты без открытых заказов в результат не попадают — вызывающий
    трактует отсутствие как 0.0.
    """
    ids = sorted({str(a) for a in agent_ids if a})
    if not ids:
        return {}
    from config import BASE_CURRENCY

    placeholders = ", ".join(f"${i + 1}" for i in range(len(ids)))
    rows = await adb_core.fetch(
        f"SELECT id, currency, agent_id FROM orders WHERE agent_id IN ({placeholders}) "
        "AND status NOT IN ('draft', 'rejected', 'cancelled', 'paid', 'returned') "
        "AND payment_confirmed = 0 AND (ms_deleted_at IS NULL)",
        *ids,
    )
    order_ids = [r["id"] for r in rows]
    if not order_ids:
        return {}
    base_cur = (BASE_CURRENCY or "USD").upper()
    currency_by_order = {r["id"]: (r["currency"] or base_cur) for r in rows}
    agent_by_order = {r["id"]: str(r["agent_id"]) for r in rows}

    items_by_order = await get_order_items_by_ids(order_ids)
    payments_by_order = await get_payments_for_orders(order_ids)
    returns_by_order = await get_confirmed_returns_cents_for_orders(order_ids)
    deposits_by_order = await get_confirmed_deposit_cents_for_orders(order_ids)

    debt_base: dict[str, float] = {}
    for oid in order_ids:
        total = sum(
            money.mul_qty(_price_cents(it), it.get("quantity", 0) or 0)
            for it in items_by_order.get(oid, [])
        )
        confirmed = sum(
            _amount_cents(p)
            for p in payments_by_order.get(oid, [])
            if p["status"] == "confirmed"
        )
        paid = confirmed + deposits_by_order.get(oid, 0)
        net_cents = max(0, max(0, total - paid) - returns_by_order.get(oid, 0))
        net_major = float(money.from_cents(net_cents))
        base = convert_to_base(net_major, currency_by_order[oid])
        agent = agent_by_order[oid]
        debt_base[agent] = debt_base.get(agent, 0.0) + (base if base is not None else net_major)
    return {a: round(v, 2) for a, v in debt_base.items()}


async def get_agent_current_debt(agent_id: str) -> float:
    """Текущий долг контрагента в базовой валюте. Обёртка над батчем
    `get_agents_current_debt` — формула долга живёт в одном месте."""
    if not agent_id:
        return 0.0
    return (await get_agents_current_debt([str(agent_id)])).get(str(agent_id), 0.0)


async def get_credit_limits(agent_ids: list[str]) -> dict[str, float]:
    """Лимиты по списку контрагентов одним SELECT; без строки — дефолт из настроек."""
    ids = sorted({str(a) for a in agent_ids if a})
    default = float(await asyncio.to_thread(get_setting, "credit_limit_default", 2000.0))
    if not ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(ids)))
    rows = await adb_core.fetch(
        f"SELECT agent_id, limit_amount_cents FROM credit_limits WHERE agent_id IN ({placeholders})",
        *ids,
    )
    found = {str(r["agent_id"]): float(money.from_cents(r["limit_amount_cents"])) for r in rows}
    return {a: found.get(a, default) for a in ids}


async def check_credit_limit(agent_id: str, order_total: float, currency: str | None = None) -> dict:
    """Проверка лимита для нового заказа. НЕ блокирует — даёт данные для
    решения боса (over_limit + цифры). Всё в БАЗОВОЙ валюте: долг агента уже в
    базовой (get_agent_current_debt), сумму нового заказа конвертируем по его
    валюте (None/без курса → как есть). Лимит — в базовой.

    asyncpg Stage 18 (#21): native async; get_agent_current_debt/get_credit_limit
    теперь async (await)."""
    debt = await get_agent_current_debt(agent_id)
    limit = await get_credit_limit(agent_id)
    order_total_base = order_total
    if currency:
        conv = convert_to_base(order_total, currency)
        if conv is not None:
            order_total_base = conv
    projected = debt + order_total_base
    return {
        "current_debt": debt,
        "limit": limit,
        "order_total_base": order_total_base,
        "projected": projected,
        "over_limit": projected > limit,
    }


async def agent_has_order(agent_id: str) -> bool:
    """Есть ли у контрагента хоть один заказ. Гейт для
    установки кредит-лимита: лимит можно задать только тому, на кого реально
    создавали заказ, а не любому контрагенту из справочника МС (иначе плодятся
    лимиты-сироты, которых нет в overview). Native async через adb_core."""
    if not agent_id:
        return False
    row = await adb_core.fetchrow(
        "SELECT 1 FROM orders WHERE agent_id = $1 LIMIT 1",
        agent_id,
    )
    return row is not None


async def get_credit_overview() -> list[dict]:
    """Сводка по контрагентам для боса: лимит + текущий долг. Объединяет
    строки credit_limits и контрагентов из активных заказов (даже без явной
    строки лимита — у них дефолтный лимит). Сортировка: сначала те, кто ближе
    к лимиту/превысил.

    Батч-версия (без N+1): раньше на каждого агента звался get_agent_current_debt,
    а тот — get_order_payment_summary на КАЖДЫЙ заказ (≈ A×(1+4N) запросов). Теперь
    долг считается из 4 групповых выборок, клампинг — в Python (логика тождественна
    get_agent_current_debt; держим её тут, чтобы не плодить кросс-БД GREATEST/MAX).

    asyncpg Stage 12 (#21): native async через adb_core. Три батч-функции долга
    тоже async (await); get_setting остаётся sync (money-core, TTL-кэш) — зовём
    через to_thread, чтобы не блокировать loop на cache-miss."""
    agents: dict[str, str] = {}
    limits_map: dict[str, float] = {}
    for r in await adb_core.fetch(
        "SELECT agent_id, agent_name, limit_amount_cents FROM credit_limits"
    ):
        aid = r["agent_id"]
        if not aid:
            continue
        agents[aid] = r["agent_name"] or aid
        limits_map[aid] = float(money.from_cents(r["limit_amount_cents"] or 0))
    for r in await adb_core.fetch(
        "SELECT DISTINCT agent_id, agent_name FROM orders "
        "WHERE agent_id IS NOT NULL AND agent_id != '' "
        "AND status NOT IN ('draft', 'rejected', 'cancelled') "
        "AND (ms_deleted_at IS NULL)"
    ):
        aid = r["agent_id"]
        if aid and aid not in agents:
            agents[aid] = r["agent_name"] or aid

    from config import BASE_CURRENCY

    # Открытые заказы (фильтр идентичен get_agent_current_debt) — один запрос
    # на всех агентов; долг по ним разбираем в Python.
    open_rows = [
        (r["id"], r["agent_id"], r["currency"])
        for r in await adb_core.fetch(
            "SELECT id, agent_id, currency FROM orders "
            "WHERE status NOT IN ('draft', 'rejected', 'cancelled', 'paid', 'returned') "
            "AND payment_confirmed = 0 AND (ms_deleted_at IS NULL)"
        )
    ]

    open_ids = [oid for oid, _, _ in open_rows]
    items_by_order = await get_order_items_by_ids(open_ids)
    payments_by_order = await get_payments_for_orders(open_ids)
    returns_cents_by_order: dict[int, int] = {}
    if open_ids:
        ph = ", ".join(f"${i + 1}" for i in range(len(open_ids)))
        for r in await adb_core.fetch(
            f"SELECT order_id, {_SUM_RETURNS_CENTS} AS rc FROM returns "
            f"WHERE order_id IN ({ph}) AND {_RETURN_OWED_FILTER} "
            f"GROUP BY order_id",
            *open_ids,
        ):
            returns_cents_by_order[r["order_id"]] = int(r["rc"] or 0)
    default_limit = float(await asyncio.to_thread(get_setting, "credit_limit_default", 2000.0))
    base_cur = (BASE_CURRENCY or "USD").upper()

    # debt[aid] = Σ по открытым заказам net = max(0, max(0, total−confirmed) − returns),
    # в КОПЕЙКАХ и сконвертированный в БАЗОВУЮ валюту — бит-в-бит как
    # get_agent_current_debt (мульти-валютный долг к единому лимиту).
    debt_by_agent: dict[str, float] = {}
    # Долг РАЗДЕЛЬНО по валютам (для отображения — не складываем UZS+USD+EUR).
    # debt (base) ниже остаётся для over_limit (лимит — в базовой валюте).
    debt_cur_by_agent: dict[str, dict[str, float]] = {}
    for oid, aid, cur in open_rows:
        items = items_by_order.get(oid, [])
        total = sum(
            money.mul_qty(_price_cents(it), it.get("quantity", 0) or 0) for it in items
        )
        confirmed = sum(
            _amount_cents(p)
            for p in payments_by_order.get(oid, [])
            if p["status"] == "confirmed"
        )
        net_cents = max(0, max(0, total - confirmed) - returns_cents_by_order.get(oid, 0))
        if not net_cents:
            continue
        net_major = float(money.from_cents(net_cents))
        ocur = (cur or base_cur).upper()
        dc = debt_cur_by_agent.setdefault(aid, {})
        dc[ocur] = dc.get(ocur, 0.0) + net_major
        base = convert_to_base(net_major, cur or base_cur)
        debt_by_agent[aid] = debt_by_agent.get(aid, 0.0) + (
            base if base is not None else net_major
        )

    out: list[dict[str, Any]] = []
    for aid, name in agents.items():
        # round(…, 2) бит-в-бит как get_agent_current_debt — иначе float-дрейф
        # суммы по нескольким заказам мог дать over_limit=True здесь при False там.
        debt = round(debt_by_agent.get(aid, 0.0), 2)
        limit = limits_map.get(aid, default_limit)
        dc = debt_cur_by_agent.get(aid, {})
        debt_by_currency = [
            {"currency": c, "amount": round(v, 2)}
            for c, v in sorted(dc.items(), key=lambda kv: kv[1], reverse=True)
        ]
        out.append(
            {
                "agent_id": aid,
                "agent_name": name,
                "limit": limit,
                "debt": debt,
                "debt_by_currency": debt_by_currency,
                "free": limit - debt,
                "over_limit": debt > limit,
            }
        )
    out.sort(key=lambda a: float(a["free"]))
    return out


async def get_orders_by_agent(agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Заказы контрагента (для карточки клиента): свежие сверху, без
    фантомных (ms_deleted_at). Сумма заказа считается из позиций (в копейках).
    Индекс idx_orders_agent_id уже есть."""
    if not agent_id:
        return []
    rows = await adb_core.fetch(
        "SELECT id, status, currency, created_at, payment_type, due_date "
        "FROM orders WHERE agent_id = $1 AND (ms_deleted_at IS NULL) "
        "ORDER BY created_at DESC LIMIT $2",
        agent_id, limit,
    )
    ids = [r["id"] for r in rows]
    items_by_order = await get_order_items_by_ids(ids)
    out: list[dict[str, Any]] = []
    for r in rows:
        items = items_by_order.get(r["id"], [])
        total_cents = sum(
            money.mul_qty(_price_cents(it), it.get("quantity", 0) or 0) for it in items
        )
        out.append(
            {
                "id": r["id"],
                "status": r["status"],
                "currency": r["currency"],
                "created_at": r["created_at"],
                "total_cents": int(total_cents),
                # Состав заказа. Позиции уже загружены ради суммы — отдать их
                # даром дешевле, чем заводить отдельную ручку: карточка клиента
                # показывала «заказ на 25 000», но не ЧТО в нём, и ответить на
                # «что он у нас берёт» было нечем.
                "items": [
                    {
                        "name": it.get("product_name") or "—",
                        "quantity": float(it.get("quantity", 0) or 0),
                        "unit": it.get("unit") or "шт",
                        "price_cents": _price_cents(it),
                    }
                    for it in items
                ],
            }
        )
    return out


async def get_agent_money_history(agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Движение денег ПО КОНКРЕТНОМУ клиенту: платежи, сдачи и возвраты.

    Формат строки намеренно совпадает с `get_cash_history` (kind/amount/
    currency/status/who/order_id/note/created_at) — карточка клиента рисует
    ленту тем же фронтовым кодом, что и экран «Деньги», и добавление нового
    вида движения не придётся делать дважды.

    Что считается «платежом клиента»:
      • payments по его заказам;
      • сдачи наличных — в той части, что распределена на его заказы
        (`cash_deposit_orders`): сдача может закрывать заказы разных клиентов,
        и показывать её полную сумму в карточке одного было бы враньём;
      • возвраты по его заказам — деньги, ушедшие обратно.

    Заказы-фантомы (удалённые в МойСклад) исключены, как и в общей ленте:
    иначе платёж виден в истории клиента, но не входит ни в один итог.
    """
    if not agent_id:
        return []

    pays = await adb_core.fetch(
        "SELECT p.id, p.user_id, p.amount_cents, p.currency, p.status, p.comment, "
        "p.order_id, p.created_at "
        "FROM payments p JOIN orders o ON o.id = p.order_id "
        "WHERE o.agent_id = $1 AND (o.ms_deleted_at IS NULL) "
        "ORDER BY p.created_at DESC LIMIT $2",
        agent_id, limit,
    )
    # Сумма — ровно та часть сдачи, что пришлась на заказы этого клиента.
    deps = await adb_core.fetch(
        "SELECT d.id, d.manager_id, d.status, d.reject_reason, d.created_at, "
        "SUM(cdo.amount_allocated_cents) AS amount_cents "
        "FROM cash_deposits d "
        "JOIN cash_deposit_orders cdo ON cdo.deposit_id = d.id "
        "JOIN orders o ON o.id = cdo.order_id "
        "WHERE o.agent_id = $1 AND (o.ms_deleted_at IS NULL) "
        "GROUP BY d.id, d.manager_id, d.status, d.reject_reason, d.created_at "
        "ORDER BY d.created_at DESC LIMIT $2",
        agent_id, limit,
    )
    rets = await adb_core.fetch(
        "SELECT r.id, r.order_id, r.created_by, r.total_amount_cents, r.status, "
        "r.reason, r.created_at, o.currency AS order_currency "
        "FROM returns r JOIN orders o ON o.id = r.order_id "
        "WHERE o.agent_id = $1 AND (o.ms_deleted_at IS NULL) "
        "ORDER BY r.created_at DESC LIMIT $2",
        agent_id, limit,
    )

    # get_all_users синхронный — через to_thread, чтобы не блокировать loop
    # (тот же урок, что в get_cash_history, WP-25).
    users = await asyncio.to_thread(get_all_users)
    names = {u["user_id"]: u.get("full_name") or str(u["user_id"]) for u in users}

    from config import BASE_CURRENCY

    base_cur = (BASE_CURRENCY or "USD").upper()

    rows: list[dict[str, Any]] = []
    for p in pays:
        rows.append({
            "kind": "payment", "id": p["id"],
            "amount": float(money.from_cents(int(p["amount_cents"] or 0))),
            "currency": p.get("currency") or base_cur, "status": p["status"],
            "who": names.get(p["user_id"], str(p["user_id"])),
            "order_id": p.get("order_id"), "note": p.get("comment") or "",
            "created_at": (p.get("created_at") or "")[:16],
        })
    for d in deps:
        rows.append({
            "kind": "deposit", "id": d["id"],
            "amount": float(money.from_cents(int(d["amount_cents"] or 0))),
            "currency": base_cur, "status": d["status"],
            "who": names.get(d["manager_id"], str(d["manager_id"])),
            "order_id": None, "note": d.get("reject_reason") or "",
            "created_at": (d.get("created_at") or "")[:16],
        })
    for r in rets:
        rows.append({
            "kind": "return", "id": r["id"],
            "amount": float(money.from_cents(int(r["total_amount_cents"] or 0))),
            "currency": (r.get("order_currency") or base_cur), "status": r["status"],
            "who": names.get(r["created_by"], str(r["created_by"])),
            "order_id": r.get("order_id"), "note": r.get("reason") or "",
            "created_at": (r.get("created_at") or "")[:16],
        })
    rows.sort(key=lambda x: x["created_at"], reverse=True)
    return rows[:limit]


async def get_clients_overview() -> list[dict[str, Any]]:
    """Список «Клиенты»: кредит-overview (долг/лимит/over_limit) + телефон.

    Раньше сюда подмешивался баланс взаиморасчётов МойСклад и добавлялись
    контрагенты, у которых баланс есть, а заказов нет. Баланса больше нет:
    «сколько должен» считается по нашим же заказам (`get_credit_overview`),
    и второй ответ на тот же вопрос с ним бы расходился. Клиенты без заказов
    из списка «Долги» выпадают — их и не за что там показывать; весь
    справочник смотрят через `/api/agents`.
    """
    rows = await get_credit_overview()
    cp = await adb_core.fetch("SELECT id, name, phone FROM counterparties")
    cp_by_id = {str(c["id"]): c for c in cp}
    for r in rows:
        c = cp_by_id.get(str(r["agent_id"]))
        r["phone"] = (c or {}).get("phone") or ""
    return rows


# ─── IMPLEMENTATION.md Фаза 3: журнал изменений заказа ────────────────────────


def log_order_change(
    order_id: int,
    changed_by: int,
    change_type: str,
    before: dict | None = None,
    after: dict | None = None,
    summary: dict | None = None,
) -> None:
    """Записать изменение заказа в order_change_log (snapshots как JSON-текст)."""
    import json as _json

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "INSERT INTO order_change_log "
                "(order_id, changed_by, change_type, before_snapshot, after_snapshot, summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                order_id,
                changed_by,
                change_type,
                _json.dumps(before) if before is not None else None,
                _json.dumps(after) if after is not None else None,
                _json.dumps(summary) if summary is not None else None,
                now_str(),
            ),
        )
        conn.commit()


# ─── IMPLEMENTATION.md Фаза 3: reject→draft + freeze, cancel, stale ───────────


class _DraftAbort(Exception):
    """Откат reject→draft с текстом для босса."""


async def reject_order_to_draft(
    order_id: int,
    rejected_by: int,
    rejected_name: str,
    comment: str,
    *,
    req_id: int | None = None,
) -> dict:
    """Reject заявки по модели IMPLEMENTATION.md §6.4: заказ возвращается в
    draft с комментарием, счётчик отклонений растёт, после reject_max_cycles
    заказ замораживается (frozen=1, resubmit запрещён до разморозки админом).

    `req_id` — заявка, которую этим решением снимаем с очереди (`returned`).
    Заказ, заявка и снимок состава для diff при переотправке пишутся ОДНОЙ
    транзакцией под FOR UPDATE заказа. Раньше заказ уходил в draft одним
    коммитом (asyncpg), а заявка помечалась другим драйвером (psycopg2): сбой
    между ними оставлял pending-заявку при заказе-черновике, и переотправка
    упиралась в уникальный индекс «одна pending-заявка на заказ» — заказ
    вставал без кнопки, которая бы его сдвинула.

    Возвращает {ok, error, frozen, rejection_count}.
    """
    import json as _json

    max_cycles = int(await asyncio.to_thread(get_setting, "reject_max_cycles", 3))
    lock = " FOR UPDATE" if USE_POSTGRES else ""
    try:
        async with adb_core.transaction() as txn:
            order = await txn.fetchrow(
                f"SELECT status, rejection_count FROM orders WHERE id = $1{lock}", order_id
            )
            if not order:
                raise _DraftAbort("Заказ не найден")
            if order["status"] != "pending":
                raise _DraftAbort("Заказ не в статусе pending")
            rc = int(order.get("rejection_count") or 0) + 1
            frozen = 1 if rc >= max_cycles else 0
            stamp = now_str()
            await txn.execute(
                "UPDATE orders SET status = 'draft', rejection_comment = $1, "
                "rejection_count = $2, frozen = $3, updated_at = $4 "
                "WHERE id = $5 AND status = 'pending'",
                comment, rc, frozen, stamp, order_id,
            )
            if req_id is not None:
                moved = await txn.execute(
                    "UPDATE shipment_requests SET status = 'returned', approved_by = $1, "
                    "approved_by_name = $2, approved_at = $3 "
                    "WHERE id = $4 AND order_id = $5 AND status = 'pending'",
                    rejected_by, rejected_name, stamp, req_id, order_id,
                )
                if not moved:
                    raise _DraftAbort("Заявка уже обработана")
            # Снапшот состояния на момент reject — для diff при переотправке (#30).
            snap_items = await txn.fetch(
                "SELECT product_href, product_name, quantity, price_cents "
                "FROM order_items WHERE order_id = $1",
                order_id,
            )
            snap_rows = [
                {
                    "product_href": it.get("product_href") or "",
                    "product_name": it.get("product_name") or "",
                    "quantity": float(it.get("quantity", 0) or 0),
                    "price": float(money.from_cents(int(it.get("price_cents") or 0))),
                }
                for it in snap_items
            ]
            before_snapshot = {
                "items": snap_rows,
                "total": sum(float(r["quantity"]) * float(r["price"]) for r in snap_rows),
            }
            await txn.execute(
                "INSERT INTO order_change_log (order_id, changed_by, change_type, "
                "before_snapshot, after_snapshot, summary, created_at) "
                "VALUES ($1, $2, 'reject', $3, NULL, $4, $5)",
                order_id, rejected_by, _json.dumps(before_snapshot),
                _json.dumps({"rejection_count": rc}), stamp,
            )
    except _DraftAbort as e:
        return {"ok": False, "error": str(e)}

    await asyncio.to_thread(
        add_audit_log,
        rejected_by,
        rejected_name,
        await asyncio.to_thread(get_role, rejected_by),
        "order_rejected",
        f"Заказ #{order_id} → draft (попытка {rc}/{max_cycles})"
        + (" — ЗАМОРОЖЕН" if frozen else ""),
    )
    if req_id is not None:
        await asyncio.to_thread(
            add_audit_log,
            rejected_by,
            rejected_name,
            await asyncio.to_thread(get_role, rejected_by),
            "shipment_returned",
            f"Заявка #{req_id} возвращена на доработку (заказ #{order_id})",
        )
    return {"ok": True, "error": None, "frozen": bool(frozen), "rejection_count": rc}


async def get_last_reject_snapshot(order_id: int) -> dict | None:
    """Последний снапшот состояния заказа на момент reject→draft (#30). None если
    заказ не реджектился. Используется для diff «что изменилось» при переотправке."""
    import json as _json

    row = await adb_core.fetchrow(
        "SELECT before_snapshot FROM order_change_log "
        "WHERE order_id = $1 AND change_type = 'reject' AND before_snapshot IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        order_id,
    )
    if not row or not row.get("before_snapshot"):
        return None
    try:
        return _json.loads(row["before_snapshot"])
    except (ValueError, TypeError):
        return None


async def unfreeze_order(order_id: int, unfrozen_by: int, unfrozen_name: str) -> dict:
    """Разморозить заказ (admin): frozen=0 + сброс rejection_count, чтобы цикл
    reject→resubmit начался заново. Возвращает {ok, error}.

    Native async через adb_core; add_audit_log/get_role — мост через to_thread
    (sync, категория get_role — см. asyncpg #21)."""
    order = await get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    if not order.get("frozen"):
        return {"ok": False, "error": "Заказ не заморожен"}

    await adb_core.execute(
        "UPDATE orders SET frozen = 0, rejection_count = 0, updated_at = $1 WHERE id = $2",
        now_str(), order_id,
    )
    await asyncio.to_thread(
        add_audit_log,
        unfrozen_by,
        unfrozen_name,
        await asyncio.to_thread(get_role, unfrozen_by),
        "order_unfrozen",
        f"Заказ #{order_id} разморожен (счётчик отклонений сброшен)",
    )
    return {"ok": True, "error": None}


async def get_frozen_orders() -> list[dict]:
    """Замороженные заказы (frozen=1, не удалённые) — для админ-списка/разморозки.
    Native async через adb_core."""
    return await adb_core.fetch(
        "SELECT * FROM orders WHERE frozen = 1 "
        "ORDER BY updated_at DESC"
    )


async def set_order_shipped_meta(order_id: int, shipped_by: int) -> bool:
    """Идемпотентно проставить shipped_at/shipped_by (только если ещё не стоят).

    Для отгрузки, пришедшей СИГНАЛОМ МС (вебхук stateType=Successful): там
    cas_order_status меняет только статус, а инварианты shipped-заказа —
    дедлайн возвратов (create_return: `if shipped_at and not force`) и
    stale-детект — опираются на shipped_at. Без этого webhook-отгрузка
    оставляла shipped_at=NULL → дедлайн молча не срабатывал (WP-13).
    shipped_by=0 — системный маркер «МойСклад»."""
    return (
        await adb_core.execute(
            "UPDATE orders SET shipped_at = COALESCE(shipped_at, $1), "
            "shipped_by = COALESCE(shipped_by, $2), updated_at = $3 WHERE id = $4",
            now_str(), shipped_by, now_str(), order_id,
        )
        > 0
    )


async def mark_order_shipped(order_id: int, shipped_by: int, shipped_name: str) -> dict:
    """Отметить заказ отгруженным (approved → shipped), DB-часть. Альтернатива
    МС-вебхуку (stateType=Successful) — для аккаунтов без статуса типа
    «Успешный». Выставляет shipped_at/shipped_by. Возвращает {ok, error}.

    Round 6 (L_R6): если у заказа уже есть `ms_demand_id`, значит МС-сторона
    отгрузила раньше (через webhook или manual API). Audit'им как
    'sync', а не как 'ручную отгрузку' — иначе менеджер видит спам.

    asyncpg Stage 16 (#21): native async. get_order/add_audit_log/get_role
    (sync money-core) — мост через to_thread; атомарный UPDATE — adb_core.execute.
    """
    order = await get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    if order.get("status") != "approved":
        return {"ok": False, "error": "Отгрузить можно только одобренный заказ"}

    from services import order_payments, order_shipment
    from services.debts import lock_orders

    if (order.get("payment_type") or "paid") == "paid":
        # Ранний отказ ДО повторного списания: без оплаты склад не трогаем.
        early_gap = (await order_payments.payment_gap_cents([order_id])).get(order_id, 0)
        if early_gap > 0:
            cur = (order.get("currency") or "").upper()
            return {
                "ok": False, "code": "payment_required", "gap_cents": early_gap,
                "error": (
                    f"Заказ #{order_id} «оплата сразу»: сначала введите, как клиент "
                    f"заплатил (наличные, карта, перечисление). Не внесено: "
                    f"{order_payments.fmt_cents(early_gap, cur)}"
                ),
            }
    # Списание при одобрении не прошло (не хватило остатка, позиции без карточек —
    # order_shipment.failed_at): «отгружен» поставил бы товар в дорогу, а остаток
    # остался бы на полке. Пробуем списать ещё раз (остаток могли довезти) и без
    # накладной не отгружаем. Заказы без строки order_shipment (эпоха МойСклад,
    # ручные статусы) — как раньше.
    shipment = await order_shipment.get_shipment(order_id)
    if shipment is not None and not shipment.get("invoice_id"):
        retry = await order_shipment.ship_order(order, await get_order_items(order_id), user_id=shipped_by)
        if not retry.get("ok"):
            return {
                "ok": False,
                "code": "stock_not_written_off",
                "error": (
                    f"Склад по заказу #{order_id} не списан: {retry.get('reason') or 'ошибка склада'}. "
                    "Отгрузить нельзя — товар уехал бы, а остаток остался на полке. Оформите "
                    "приход недостающего или поправьте позиции и нажмите «Отгрузить» ещё раз."
                ),
            }

    async with adb_core.transaction() as txn:
        # «Оплата сразу» не уезжает, пока не введено, как получены деньги
        # (требование владельца: «до отгрузки, чтобы потом не возникало
        # вопросов»). Проверка — под замком заказа, тем же, что у записи
        # разбивки: отклонённая между проверкой и отгрузкой оплата не проскочит.
        await lock_orders(txn, [order_id])
        if (order.get("payment_type") or "paid") == "paid":
            gap = (await order_payments.payment_gap_cents([order_id], conn=txn)).get(order_id, 0)
            if gap > 0:
                cur = (order.get("currency") or "").upper()
                return {
                    "ok": False,
                    "code": "payment_required",
                    "gap_cents": gap,
                    "error": (
                        f"Заказ #{order_id} «оплата сразу»: сначала введите, как клиент "
                        f"заплатил (наличные, карта, перечисление). Не внесено: "
                        f"{order_payments.fmt_cents(gap, cur)}"
                    ),
                }
        updated = (
            await txn.execute(
                "UPDATE orders SET status = 'shipped', shipped_at = $1, shipped_by = $2, "
                "updated_at = $3 WHERE id = $4 AND status = 'approved'",
                now_str(), shipped_by, now_str(), order_id,
            )
            > 0
        )
    if not updated:
        return {"ok": False, "error": "Заказ уже обработан"}

    # Заморозить курс заказа на момент отгрузки (best-effort, sync money-core).
    await asyncio.to_thread(_snapshot_order_fx, order_id)

    already_in_ms = bool(order.get("ms_demand_id"))
    await asyncio.to_thread(
        add_audit_log,
        shipped_by,
        shipped_name,
        await asyncio.to_thread(get_role, shipped_by),
        "order_shipped",
        (
            f"Заказ #{order_id} sync (demand уже в МС, локальный статус догнан)"
            if already_in_ms
            else f"Заказ #{order_id} отмечен отгруженным"
        ),
    )
    return {"ok": True, "error": None, "already_in_ms": already_in_ms}


async def cancel_order(order_id: int, cancelled_by: int, cancelled_name: str, reason: str) -> dict:
    """Отмена одобренного заказа (IMPLEMENTATION.md §6.7): статус И возврат
    списанного товара — ОДНОЙ транзакцией. Shipped по спеке требует возврата на
    100% — здесь не пропускаем (нужен return-флоу).

    Раньше статус коммитился первым, а накладная отменялась потом, отдельной
    транзакцией «best-effort»: сбой между ними оставлял заказ отменённым, а
    товар — списанным навсегда. Повторить было нечем: второй вызов отвечал
    «доступна только для approved». Теперь либо заказ отменён и остаток
    вернулся, либо не изменилось ничего и оператор видит причину.

    Замок — тот же, что у списания (`order_shipment._lock_order_for_shipment`:
    advisory по заказу + FOR UPDATE строки), поэтому «одобрить против
    отменить» по-прежнему сериализуются. Возвращает {ok, error, stock_reverse}.
    """
    from services import order_shipment

    async with adb_core.transaction() as txn:
        status = await order_shipment._lock_order_for_shipment(txn, order_id)
        if status is None:
            return {"ok": False, "error": "Заказ не найден"}
        if status != "approved":
            if status == "cancelled":
                return {"ok": False, "error": "Заказ уже обработан"}
            return {
                "ok": False,
                "error": "Отмена доступна только для approved (shipped → через возврат)",
            }
        # Оплата, внесённая разбивкой до отгрузки. Ожидающая — отклоняется
        # вместе с отменой (деньги вернули клиенту). Подтверждённая или уже
        # сданная в кассу — отказ: молча отменить заказ с принятыми деньгами
        # значит потерять их след.
        from services import order_payments

        parts = (await order_payments.parts_for_orders([order_id], conn=txn)).get(order_id, [])
        confirmed_c = int(await txn.fetchval(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE order_id = $1 "
            "AND status = 'confirmed'", order_id,
        ) or 0)
        in_deposit = [p for p in parts if p["state"] == "in_deposit"]
        if confirmed_c > 0 or in_deposit:
            cur = (await txn.fetchval("SELECT currency FROM orders WHERE id = $1", order_id)) or ""
            what = (
                f"подтверждена оплата {order_payments.fmt_cents(confirmed_c, cur)}"
                if confirmed_c else "наличные уже сданы в кассу (сдача ждёт подтверждения)"
            )
            return {
                "ok": False,
                "code": "money_received",
                "error": (
                    f"По заказу #{order_id} {what} — отмена потеряла бы эти деньги. "
                    + ("Отклоните сдачу, потом отменяйте заказ."
                       if not confirmed_c else
                       "Отгрузите заказ и оформите возврат «Наличными» — деньги выдадут из кассы с записью.")
                ),
            }
        # Сначала склад: его отказ случается ДО первой записи, и тогда
        # транзакция не пишет ничего — заказ остаётся одобренным.
        rev = await order_shipment.cancel_shipment_locked(txn, order_id, user_id=cancelled_by)
        if not rev.get("ok"):
            return {
                "ok": False,
                "error": f"Товар не вернуть на склад: {rev.get('reason') or rev.get('code')}",
                "stock_reverse": rev,
            }
        updated = (
            await txn.execute(
                "UPDATE orders SET status = 'cancelled', cancelled_at = $1, "
                "cancelled_by = $2, cancellation_reason = $3, updated_at = $4 "
                "WHERE id = $5 AND status = 'approved'",
                now_str(), cancelled_by, reason, now_str(), order_id,
            )
            > 0
        )
        if not updated:  # под замком недостижимо; страховка на SQLite-ручные правки
            raise RuntimeError(f"cancel_order: заказ #{order_id} ушёл из approved под замком")
        # Все ожидающие платежи отменённого заказа снимаются вместе с ним: и
        # строки разбивки, и старый автоплатёж одобрения без способа. Иначе
        # «Подтвердить» по отменённому заказу засчитывал деньги за продажу,
        # которой нет (сценарий test_cancelling_paid_order_voids_its_pending_payment).
        voided = await txn.fetch(
            "SELECT id FROM payments WHERE order_id = $1 AND status = 'pending'", order_id
        )
        await txn.execute(
            "UPDATE payments SET status = 'rejected' WHERE order_id = $1 AND status = 'pending'",
            order_id,
        )

    await asyncio.to_thread(
        add_audit_log,
        cancelled_by,
        cancelled_name,
        await asyncio.to_thread(get_role, cancelled_by),
        "order_cancelled",
        f"Заказ #{order_id} отменён: {reason[:200]}"
        + (f"; сняты ожидающие платежи #{', #'.join(str(r['id']) for r in voided)}" if voided else ""),
    )
    if rev.get("invoice_id"):
        logger.info("Заказ #%s отменён, отгрузка откачена, остаток возвращён", order_id)
    return {"ok": True, "error": None, "stock_reverse": rev}


async def get_stale_pending_orders(hours: int = 48) -> list[dict]:
    """Заявки, висящие в pending дольше `hours` (для stale-мониторинга, §13).
    Берём COALESCE(submitted_at, created_at). asyncpg Stage 8 (#21): native
    async через adb_core; cutoff в Python (local TZ), параметром (CLAUDE.md)."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return await adb_core.fetch(
        "SELECT * FROM orders WHERE status = 'pending' "
        "AND COALESCE(submitted_at, created_at) < $1 ORDER BY created_at ASC",
        cutoff,
    )


# ─── IMPLEMENTATION.md Фаза 4: сдача наличных (cash deposits) ─────────────────
#
# Менеджер сдаёт собранные деньги в кассу; босс/бухгалтер подтверждает.
# Закрывает дыру «собрал у клиента, но не сдал в офис». Используем новую
# модель: при покрытии заказа подтверждёнными сдачами он переходит в 'paid'
# (payment_confirmed=1). order total берём из get_order_payment_summary.


async def _order_total_cents(order_id: int) -> int:
    """Сумма заказа в копейках (точно, из price_cents).

    asyncpg Stage 17 (#21): native async; get_order_payment_summary пока sync
    (общий read, флип в Stage 19) — мост через to_thread."""
    summary = await get_order_payment_summary(order_id)
    return int(summary["total_cents"])


async def _order_confirmed_deposit_cents(order_id: int) -> int:
    """Распределено на заказ ПОДТВЕРЖДЁННЫМИ сдачами, в копейках.

    asyncpg Stage 17 (#21): native async через adb_core (fetchval)."""
    return int(
        await adb_core.fetchval(
            f"SELECT {_SUM_ALLOC_CENTS} FROM cash_deposit_orders cdo "
            "JOIN cash_deposits d ON d.id = cdo.deposit_id "
            "WHERE cdo.order_id = $1 AND d.status = 'confirmed'",
            order_id,
        )
        or 0
    )


async def _order_confirmed_returns_cents(order_id: int) -> int:
    """Сумма подтверждённых (не удалённых) возвратов по заказу, в копейках.

    Возвраты уменьшают «к оплате» по заказу (так же, как в get_agent_current_debt):
    заказ считается закрытым, когда подтверждённые платежи/сдачи покрывают
    total − returns. Без этого возвращённый-и-доплаченный заказ не закрывался
    (остаток оставался завышенным на сумму возврата). _SUM_RETURNS_CENTS
    резолвится в рантайме (определён ниже по файлу)."""
    return int(
        await adb_core.fetchval(
            f"SELECT {_SUM_RETURNS_CENTS} FROM returns "
            f"WHERE order_id = $1 AND {_RETURN_OWED_FILTER}",
            order_id,
        )
        or 0
    )


async def _order_confirmed_payment_cents(order_id: int) -> int:
    """Подтверждённые платежи по заказу в ВАЛЮТЕ ЗАКАЗА, в копейках. Платежи к
    заказу — в его валюте (link/close это гарантируют, WP-04); NULL-валюту
    платежа трактуем как валюту заказа."""
    from config import BASE_CURRENCY

    cur = await adb_core.fetchval("SELECT currency FROM orders WHERE id = $1", order_id)
    order_cur = (cur or BASE_CURRENCY or "USD").upper()
    return int(
        await adb_core.fetchval(
            f"SELECT {_SUM_PAYMENTS_CENTS} FROM payments "
            "WHERE order_id = $1 AND status = 'confirmed' "
            "AND COALESCE(UPPER(currency), $2) = $2",
            order_id,
            order_cur,
        )
        or 0
    )


def _is_base_currency(currency: str | None) -> bool:
    """Заказ в базовой валюте (или валюта не задана = база). Сдачи (cash_deposits)
    хранятся в базовой валюте без поля currency, поэтому распределять их можно
    ТОЛЬКО на заказы в базовой валюте — иначе FIFO сравнивает копейки разных
    валют без конверсии (WP-05). Заказы в иной валюте сдачей не закрываем."""
    from config import BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    return (currency or base).upper() == base


async def _calc_balances(order_ids: list[int], conn=None):
    from services.debts import calc_order_balances

    return await calc_order_balances(order_ids, conn=conn)


async def get_manager_open_orders_for_deposit(manager_id: int, conn=None) -> list[dict]:
    """Отгруженные неоплаченные заказы менеджера В БАЗОВОЙ ВАЛЮТЕ (для распределения
    сдачи). Возвращает [{id, total, covered, remaining}] по возрастанию created_at.
    remaining = total − возвраты − подтверждённые платежи − распределённое
    pending+confirmed-сдачами: сдача не должна перекрывать уже оплаченное
    платежами или возвращённое (WP-05).

    Заказы в не-базовой валюте исключаем: сдача в базовой валюте без конверсии
    их не покрывает.

    Остаток считаем в копейках (без float-эпсилона 0.01) — заказ попадает
    в список, только если непокрытый остаток ≥ 1 копейки."""
    db = conn if conn is not None else adb_core
    rows = await db.fetch(
        "SELECT id, currency FROM orders WHERE user_id = $1 AND status = 'shipped' "
        "AND payment_confirmed = 0 ORDER BY created_at ASC",
        manager_id,
    )
    # Заказы в не-базовой валюте отсеиваем сразу: сдача в базовой их не покрывает.
    ordered_ids = [r["id"] for r in rows if _is_base_currency(r["currency"])]
    if not ordered_ids:
        return []

    # T2.11: раньше здесь было 4 запроса НА КАЖДЫЙ заказ, и каждый брал свой
    # коннект из пула — при вызове изнутри транзакции это выедало пул и
    # вставало намертво. Теперь два запроса на любое число заказов.
    remaining_by_id = await deposit_remaining_cents_for_orders(ordered_ids, conn=conn)
    balances = await _calc_balances(ordered_ids, conn=conn)

    out = []
    for oid in ordered_ids:  # порядок FIFO — по created_at, как в SELECT выше
        remaining_cents = remaining_by_id.get(oid, 0)
        if remaining_cents <= 0:
            continue
        bal = balances.get(oid)
        total_cents = bal.total_cents if bal else 0
        out.append(
            {
                "id": oid,
                "total": float(money.from_cents(total_cents)),
                "covered": float(money.from_cents(total_cents - remaining_cents)),
                "remaining": float(money.from_cents(remaining_cents)),
                "remaining_cents": remaining_cents,
            }
        )
    return out


# Round 6: верхняя граница финансовых сумм (нижняя `> 0` уже была).
# Защищает от inf/NaN и от случайного `1e308`, который "проходит" сравнение
# `amount > 0`, но потом отравляет FIFO-математику и `/api/credit/overview`
# (показывает `nan USD` боссу).
_AMOUNT_MAX = 10_000_000.0


def _validate_amount(amount: float | None) -> tuple[bool, str | None]:
    """True если amount — конечное положительное число в разумных пределах.
    Возвращает (ok, error_message)."""
    import math

    if amount is None:
        return False, "Сумма не задана"
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, "Сумма должна быть числом"
    if math.isnan(amount) or math.isinf(amount):
        return False, "Сумма должна быть числом"
    if amount <= 0:
        return False, "Сумма должна быть > 0"
    if amount > _AMOUNT_MAX:
        return False, f"Сумма превышает лимит ({_AMOUNT_MAX:.0f})"
    return True, None


def current_rate_to_base(currency: str | None) -> float | None:
    """Текущий курс валюты к базовой (потолок суммы, пересчёт выручки).
    Базовая — 1.0 без обращения к БД (строки курса для неё может не быть);
    неизвестная — None."""
    from config import BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    code = (currency or base).upper()
    if code == base:
        return 1.0
    return get_currency_rate(code)


def validate_amount_in_currency(
    amount: float | None, currency: str | None
) -> tuple[bool, str | None]:
    """Сумма денег в валюте `currency`: конечная, > 0 и не выше потолка в
    ЭКВИВАЛЕНТЕ базовой валюты (money.MAX_BASE_CENTS).

    `_validate_amount` с его «10 000 000 в любой валюте» для сумов означал
    потолок ≈ $800 — ни технику, ни крупный заказ в UZS одним платежом не
    провести. Курс берём текущий (кэш 5 мин): это сторож от опечаток, а не
    учёт. Без курса действует технический потолок money.HARD_MAX_CENTS —
    отказать в платеже из-за незаданного курса нельзя.
    """
    import math

    if amount is None:
        return False, "Сумма не задана"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return False, "Сумма должна быть числом"
    if math.isnan(value) or math.isinf(value):
        return False, "Сумма должна быть числом"
    if value <= 0:
        return False, "Сумма должна быть > 0"
    try:
        cents = money.to_cents(amount)
    except (ArithmeticError, ValueError):
        return False, "Сумма должна быть числом"
    ok, err = money.validate_cents(cents, current_rate_to_base(currency))
    return ok, (err or None)


# ─── Currency rates (PR #42: tech debt #3a) ──────────────────────────────────
#
# Простая модель: один курс на валюту относительно BASE_CURRENCY (USD).
# Не храним историю — для UI-сводок («сколько денег у нас всего в USD»)
# достаточно «текущего» курса. Историческая точность (rate-at-payment)
# отдельная задача с другой моделью данных.
#
# Кэш: rates меняются редко (ручной admin-update раз в день максимум),
# TTL 5 минут даёт мгновенный hit для всех queries без stale-данных
# больше чем на 5 мин.

import threading as _threading

_CURRENCY_RATES_CACHE: dict[str, tuple[float, float]] = {}  # code → (loaded_at_monotonic, rate)
_CURRENCY_RATES_CACHE_TTL = 300.0  # 5 минут
_currency_rates_lock = _threading.Lock()


def get_currency_rate(currency_code: str) -> float | None:
    """Получить rate валюты к BASE_CURRENCY. None если валюта неизвестна.

    Кэшируется на 5 мин — TTL вне lock'а, sync-friendly. Если код упал
    в БД-вызове, возвращаем None (caller решает: использовать 1.0
    fallback или явный warning)."""
    if not currency_code:
        return None
    code = currency_code.upper()
    now = time.monotonic()
    with _currency_rates_lock:
        cached = _CURRENCY_RATES_CACHE.get(code)
        if cached is not None and (now - cached[0]) < _CURRENCY_RATES_CACHE_TTL:
            return cached[1]
    # Cache miss / stale → читаем БД
    try:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q("SELECT rate_to_base FROM currency_rates WHERE currency_code = ?"),
                (code,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            rate = float(row["rate_to_base"]) if hasattr(row, "keys") else float(row[0])
    except Exception:
        logger.exception("get_currency_rate(%s) failed", code)
        return None
    with _currency_rates_lock:
        _CURRENCY_RATES_CACHE[code] = (now, rate)
    return rate


# Коридор курса сума к доллару для ТЕКУЩЕГО курса. Не прогноз, а сторож от
# опечаток: поле формы показывает «сум за 1 USD», а в базу уходит обратное
# число, и лишний/пропущенный ноль или перевёрнутый курс (12 600 вместо
# 1/12 600) молча пересчитывал все сводки «в долларах» в тысячи раз. За
# 2017–2026 сум ходил в пределах ~8 000–13 000 — у коридора запас в разы в обе
# стороны, при этом ошибка на порядок в него уже не попадает. Дневной архив
# коридором НЕ режется: история за годы назад законно бывает вне его.
UZS_PER_USD_MIN = 5_000.0
UZS_PER_USD_MAX = 50_000.0


def _fmt_rate_num(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").removesuffix(".00")


def validate_rate_to_base(code: str, rate: float) -> tuple[bool, str | None]:
    """Курс `1 code = rate BASE` в разумных границах. (ok, понятная ошибка).

    Базовая валюта — строго 1 (иначе все пересчёты «в базовую» перекошены).
    Пара сум/доллар — коридор UZS_PER_USD_*. Прочие пары (если список валют
    расширят) — только общая проверка `_validate_amount` у вызывающего.
    """
    from config import BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    value = float(rate)
    if code == base:
        if abs(value - 1.0) > 1e-12:
            return False, f"Курс базовой валюты {base} всегда 1 — менять его нельзя"
        return True, None
    if {code, base} == {"UZS", "USD"}:
        uzs_per_usd = 1.0 / value if code == "UZS" else value
        if not (UZS_PER_USD_MIN <= uzs_per_usd <= UZS_PER_USD_MAX):
            return False, (
                f"Курс вне разумных границ: получилось 1 USD = "
                f"{_fmt_rate_num(uzs_per_usd)} сум. Ожидается от "
                f"{_fmt_rate_num(UZS_PER_USD_MIN)} до {_fmt_rate_num(UZS_PER_USD_MAX)} "
                "сум за доллар — проверьте нули и направление курса."
            )
    return True, None


def set_currency_rate(currency_code: str, rate: float, updated_by: int) -> tuple[bool, str | None]:
    """Установить/обновить rate. UPSERT с автоинвалидацией кэша.

    Возвращает (ok, error_msg). Валидирует rate как amount (> 0, конечное) и
    по коридору пары (`validate_rate_to_base`) — это касается и ЦБ-синка:
    аномальный ответ источника лучше громкого отказа, чем молчаливой записи.
    `currency_code` — нормализуется UPPER, должен быть в ALLOWED_CURRENCIES."""
    code, err = _check_rate_input(currency_code, rate)
    if err:
        return False, err
    with get_conn() as conn:
        cur = get_cursor(conn)
        _upsert_current_rate(cur, code, rate, updated_by)
        conn.commit()
    # Инвалидируем кэш именно этой валюты, остальные не трогаем.
    with _currency_rates_lock:
        _CURRENCY_RATES_CACHE.pop(code, None)
    return True, None


def _check_rate_input(currency_code: str, rate: float) -> tuple[str, str | None]:
    """Нормализованный код и ошибка (None — всё в порядке)."""
    from config import ALLOWED_CURRENCIES

    code = (currency_code or "").upper().strip()
    if not code:
        return code, "currency_code пустой"
    if code not in ALLOWED_CURRENCIES:
        return code, f"currency_code должен быть из {list(ALLOWED_CURRENCIES)}"
    ok, err = _validate_amount(rate)
    if not ok:
        return code, f"rate: {err}"
    ok, err = validate_rate_to_base(code, rate)
    if not ok:
        return code, err
    return code, None


def set_currency_rate_manual(
    currency_code: str, rate: float, updated_by: int
) -> tuple[bool, str | None]:
    """Ручная правка курса из WebApp: текущий курс + дневной архив source='manual'.

    Обе записи — одним коммитом. Метка 'manual' в архиве за СЕГОДНЯ — то, по
    чему ночной `run_fx_sync` понимает, что курс этого дня поправил человек, и
    не перезаписывает его ни в `currency_rates`, ни в архиве. Раньше правка
    держалась до ближайшего прогона синка и исчезала без следа, а снимки
    `fx_rate_to_base` операций того дня уезжали по курсу ЦБ, который босс
    сознательно исправил. Следующий день синк пишет как обычно.
    """
    code, err = _check_rate_input(currency_code, rate)
    if err:
        return False, err
    day = now_str()[:10]  # бизнес-дата — в кадре процесса, как created_at
    with get_conn() as conn:
        cur = get_cursor(conn)
        _upsert_current_rate(cur, code, rate, updated_by)
        _upsert_daily_rate(cur, code, day, rate, "manual")
        conn.commit()
    with _currency_rates_lock:
        _CURRENCY_RATES_CACHE.pop(code, None)
    logger.info("Курс %s задан вручную (user_id=%s): %s на %s", code, updated_by, rate, day)
    return True, None


def get_currency_rate_daily_source(currency_code: str, rate_date: str) -> str | None:
    """Источник курса в дневном архиве за день ('cbu' | 'manual' | None — записи нет)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT source FROM currency_rate_daily "
                "WHERE currency_code = ? AND rate_date = ?"
            ),
            ((currency_code or "").upper(), (rate_date or "")[:10]),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row["source"] if hasattr(row, "keys") else row[0]


def _upsert_current_rate(cur, code: str, rate: float, updated_by: int) -> None:
    if USE_POSTGRES:
        cur.execute(
            q(
                "INSERT INTO currency_rates "
                "(currency_code, rate_to_base, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT (currency_code) DO UPDATE SET "
                "rate_to_base = EXCLUDED.rate_to_base, "
                "updated_at = EXCLUDED.updated_at, "
                "updated_by = EXCLUDED.updated_by"
            ),
            (code, float(rate), now_str(), updated_by),
        )
    else:
        cur.execute(
            q(
                "INSERT OR REPLACE INTO currency_rates "
                "(currency_code, rate_to_base, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?)"
            ),
            (code, float(rate), now_str(), updated_by),
        )


def _upsert_daily_rate(cur, code: str, day: str, rate: float, source: str) -> None:
    """UPSERT дневного архива. Ручную запись дня перезаписывает только ручная.

    Условие — в самом UPSERT (одинаково на Postgres и SQLite ≥ 3.24), а не
    отдельной проверкой: синк и правка не разойдутся между SELECT и записью.
    """
    cur.execute(
        q(
            "INSERT INTO currency_rate_daily "
            "(currency_code, rate_date, rate_to_base, source, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (currency_code, rate_date) DO UPDATE SET "
            "rate_to_base = excluded.rate_to_base, source = excluded.source "
            "WHERE COALESCE(currency_rate_daily.source, '') <> 'manual' "
            "OR excluded.source = 'manual'"
        ),
        (code, day, float(rate), source, now_str()),
    )


async def get_all_currency_rates() -> list[dict]:
    """Все курсы. Для admin-UI и /api/currency/rates. asyncpg Stage 8 (#21)."""
    return await adb_core.fetch(
        "SELECT currency_code, rate_to_base, updated_at, updated_by "
        "FROM currency_rates ORDER BY currency_code"
    )


def convert_to_base(amount: float, from_currency: str | None) -> float | None:
    """Перевести amount из from_currency в BASE_CURRENCY.

    Возвращает None если валюта неизвестна (rate не задан админом).
    Это намеренно: caller должен явно обработать «не могу посчитать»,
    а не молча умножить на 1.0 и выдать неверный итог.
    """
    from config import BASE_CURRENCY

    if amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    code = (from_currency or BASE_CURRENCY).upper()
    base = (BASE_CURRENCY or "USD").upper()
    if code == base:
        return amount
    rate = get_currency_rate(code)
    if rate is None or rate <= 0:
        return None
    # Конвертация в копейках с единичным округлением — без float-дрейфа.
    return float(money.from_cents(money.convert_cents(money.to_cents(amount), rate)))


def _invalidate_currency_rates_cache() -> None:
    """Сбросить весь кэш (для тестов и admin-debug endpoint'а)."""
    with _currency_rates_lock:
        _CURRENCY_RATES_CACHE.clear()


def convert_to_base_at(
    amount: float, from_currency: str | None, rate_to_base: float | None
) -> float | None:
    """Перевести amount в BASE_CURRENCY по ПЕРЕДАННОМУ курсу (снимок).

    В отличие от convert_to_base (берёт ТЕКУЩИЙ курс из БД), считает по
    `rate_to_base`, замороженному на момент операции. Если валюта = базовой
    (или None) — возвращает amount как есть. Если курс не передан/невалиден —
    None (caller решает: fallback на convert_to_base или показать «—»).
    """
    from config import BASE_CURRENCY

    if amount is None:
        return None
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    code = (from_currency or BASE_CURRENCY).upper()
    base = (BASE_CURRENCY or "USD").upper()
    if code == base:
        return amount
    if rate_to_base is None:
        return None
    try:
        rate = float(rate_to_base)
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    return float(money.from_cents(money.convert_cents(money.to_cents(amount), rate)))


def set_currency_rate_daily(
    currency_code: str, rate_date: str, rate_to_base: float, source: str = "cbu"
) -> tuple[bool, str | None]:
    """UPSERT курса в дневной архив (currency_rate_daily). Один курс на день;
    запись дня с source='manual' перезаписывает только другая ручная.

    `rate_date` — 'YYYY-MM-DD'. Возвращает (ok, error_msg). Валидирует rate
    как amount (> 0, конечное). currency_code нормализуется UPPER.
    """
    code = (currency_code or "").upper().strip()
    if not code:
        return False, "currency_code пустой"
    if not rate_date:
        return False, "rate_date пустой"
    ok, err = _validate_amount(rate_to_base)
    if not ok:
        return False, f"rate_to_base: {err}"
    with get_conn() as conn:
        cur = get_cursor(conn)
        # Ручную запись дня (source='manual') автоматический источник не
        # затирает — см. set_currency_rate_manual.
        _upsert_daily_rate(cur, code, rate_date, rate_to_base, source)
        conn.commit()
    return True, None


def get_currency_rate_asof(currency_code: str, date_str: str) -> float | None:
    """Курс валюты к BASE_CURRENCY на дату `date_str` (самый свежий день ≤ date).

    Выходные/пропуски в архиве → берём ближайший ранний день. Базовая валюта
    → 1.0 без обращения к БД. None если в архиве нет ни одной подходящей записи.
    `date_str` сравнивается лексикографически — формат строго 'YYYY-MM-DD'
    (или с временем; берётся первые 10 символов).
    """
    from config import BASE_CURRENCY

    if not currency_code:
        return None
    code = currency_code.upper()
    base = (BASE_CURRENCY or "USD").upper()
    if code == base:
        return 1.0
    day = (date_str or "")[:10]
    if not day:
        return None
    try:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q(
                    "SELECT rate_to_base FROM currency_rate_daily "
                    "WHERE currency_code = ? AND rate_date <= ? "
                    "ORDER BY rate_date DESC LIMIT 1"
                ),
                (code, day),
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("get_currency_rate_asof(%s, %s) failed", code, day)
        return None
    if row is None:
        return None
    return float(row["rate_to_base"]) if hasattr(row, "keys") else float(row[0])


def backfill_fx_rate_snapshots() -> dict:
    """Проставить fx_rate_to_base прошлым orders/payments без снимка — по дате
    операции через дневной архив (get_currency_rate_asof). Идемпотентно
    (UPDATE ... WHERE fx_rate_to_base IS NULL). Возвращает {orders, payments}.

    Дата операции: orders → shipped_at/approved_at/created_at (что раньше есть),
    payments → confirmed_at/created_at. Если архив на ту дату пуст → строку
    пропускаем (останется NULL → пересчёт fallback'нет на текущий курс).
    """
    o_count = 0
    p_count = 0

    def _rows(sql: str) -> list:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(q(sql))
            return cur.fetchall()

    def _val(row, key, idx):
        return row[key] if hasattr(row, "keys") else row[idx]

    order_rows = _rows(
        "SELECT id, currency, "
        "COALESCE(shipped_at, approved_at, created_at) AS op_date "
        "FROM orders WHERE fx_rate_to_base IS NULL AND currency IS NOT NULL "
        "AND status IN ('approved','shipped','paid','partially_returned','returned')"
    )
    for row in order_rows:
        rate = get_currency_rate_asof(_val(row, "currency", 1), _val(row, "op_date", 2) or "")
        if rate is None:
            continue
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q("UPDATE orders SET fx_rate_to_base = ? WHERE id = ? AND fx_rate_to_base IS NULL"),
                (float(rate), _val(row, "id", 0)),
            )
            conn.commit()
        o_count += 1

    payment_rows = _rows(
        "SELECT id, currency, COALESCE(confirmed_at, created_at) AS op_date "
        "FROM payments WHERE fx_rate_to_base IS NULL AND currency IS NOT NULL "
        "AND status = 'confirmed'"
    )
    for row in payment_rows:
        rate = get_currency_rate_asof(_val(row, "currency", 1), _val(row, "op_date", 2) or "")
        if rate is None:
            continue
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q("UPDATE payments SET fx_rate_to_base = ? WHERE id = ? AND fx_rate_to_base IS NULL"),
                (float(rate), _val(row, "id", 0)),
            )
            conn.commit()
        p_count += 1

    return {"orders": o_count, "payments": p_count}


# ─── Product prices (PR C: управление ценами руководством) ───────────────────
#
# Руководство (boss/admin) задаёт на товар:
#   sale_price — минимальная цена продажи (менеджер может поднять, не ниже)
#   cost_price — себестоимость (видна только boss/admin → расчёт прибыли)
# Источник истины — руководство, не МС. Кэш по ms_id (TTL 5 мин) как у
# currency rates — цены меняются редко, читаются часто (каждый add_item).

_PRODUCT_PRICE_CACHE: dict[str, tuple[float, dict]] = {}  # ms_id → (loaded_at, row)
_PRODUCT_PRICE_CACHE_TTL = 300.0
_product_price_lock = _threading.Lock()


def set_product_price(
    ms_id: str,
    product_name: str,
    sale_price: float | None,
    cost_price: float | None,
    currency: str | None,
    updated_by: int,
) -> tuple[bool, str | None]:
    """UPSERT цены товара. Возвращает (ok, error_msg).

    sale_price и cost_price — опциональны (None = не задано), но если
    заданы — валидируются через `validate_amount_in_currency` (>0, конечные,
    потолок в эквиваленте базовой валюты).
    currency дефолтится в BASE_CURRENCY.
    """
    from config import BASE_CURRENCY

    ms_id = (ms_id or "").strip()
    if not ms_id:
        return False, "ms_id обязателен"
    for label, val in (("sale_price", sale_price), ("cost_price", cost_price)):
        if val is not None:
            ok, err = validate_amount_in_currency(val, currency)
            if not ok:
                return False, f"{label}: {err}"
    cur_code = (currency or BASE_CURRENCY or "USD").upper()
    sale_c = money.to_cents(sale_price) if sale_price is not None else None
    cost_c = money.to_cents(cost_price) if cost_price is not None else None
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                q(
                    "INSERT INTO product_prices "
                    "(ms_id, product_name, sale_price_cents, cost_price_cents, currency, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (ms_id) DO UPDATE SET "
                    "product_name = EXCLUDED.product_name, "
                    "sale_price_cents = EXCLUDED.sale_price_cents, "
                    "cost_price_cents = EXCLUDED.cost_price_cents, "
                    "currency = EXCLUDED.currency, "
                    "updated_by = EXCLUDED.updated_by, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                (ms_id, product_name or "", sale_c, cost_c, cur_code, updated_by, now_str()),
            )
        else:
            cur.execute(
                q(
                    "INSERT OR REPLACE INTO product_prices "
                    "(ms_id, product_name, sale_price_cents, cost_price_cents, currency, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                (ms_id, product_name or "", sale_c, cost_c, cur_code, updated_by, now_str()),
            )
        conn.commit()
    with _product_price_lock:
        _PRODUCT_PRICE_CACHE.pop(ms_id, None)
    return True, None


def _with_major(row, *pairs: tuple[str, str]):
    """Дописать к строке мажорные ключи, посчитанные из копеек.

    Деньги хранятся ТОЛЬКО в копейках (T1.3), но потребители строк —
    бот-экраны, JSON-ответы WebApp и фронт — исторически читают мажорные
    ключи (`amount`, `total_amount`, `amount_allocated`). Контракт API
    менять не в этой задаче, поэтому конвертация живёт здесь, на границе
    чтения: единственный float в цепочке и только для отображения.

    Денежные РАСЧЁТЫ используют *_cents напрямую и сюда не заглядывают.
    """
    if row is None:
        return None
    if isinstance(row, list):
        return [_with_major(r, *pairs) for r in row]
    d = dict(row)
    for major, cents in pairs:
        c = d.get(cents)
        d[major] = float(money.from_cents(int(c))) if c is not None else 0.0
    return d


def _price_row_major(row: dict | None) -> dict:
    """Строка product_prices → dict, где рядом с *_cents лежат мажорные
    sale_price/cost_price. Хранение — только копейки (T1.3), но потребители
    (бот-экраны, /api/stock, расчёт прибыли) исторически читают мажорные
    ключи, и контракт JSON-ответов на этом держится. Конвертация — здесь,
    на границе чтения, а не в схеме."""
    if not row:
        return {}
    d = dict(row)
    for major, cents in (("sale_price", "sale_price_cents"), ("cost_price", "cost_price_cents")):
        c = d.get(cents)
        d[major] = float(money.from_cents(int(c))) if c is not None else None
    return d


def get_product_price(ms_id: str) -> dict | None:
    """Цена товара по ms_id (с кэшем). None если не задана.

    Возвращает полный dict (sale_price/cost_price/currency). Caller
    обязан НЕ отдавать cost_price менеджеру (фильтровать на API-edge).
    """
    ms_id = (ms_id or "").strip()
    if not ms_id:
        return None
    now = time.monotonic()
    with _product_price_lock:
        cached = _PRODUCT_PRICE_CACHE.get(ms_id)
        if cached is not None and (now - cached[0]) < _PRODUCT_PRICE_CACHE_TTL:
            return cached[1] or None
    try:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q(
                    "SELECT ms_id, product_name, sale_price_cents, cost_price_cents, "
                    "currency, updated_at FROM product_prices WHERE ms_id = ?"
                ),
                (ms_id,),
            )
            row = cur.fetchone()
            result = _price_row_major(row)
    except Exception:
        logger.exception("get_product_price(%s) failed", ms_id)
        return None
    with _product_price_lock:
        _PRODUCT_PRICE_CACHE[ms_id] = (now, result)
    return result or None


async def get_product_prices_by_ids(ms_ids: list[str]) -> dict[str, dict]:
    """Батч-выборка цен по списку ms_id (без N+1). Возвращает {ms_id: row}.

    Пропускает кэш (батч обычно для разовых расчётов прибыли по заказу).
    asyncpg Stage 9 (#21): native async через adb_core; IN-список — $1..$N.
    """
    ids = [str(x).strip() for x in (ms_ids or []) if str(x).strip()]
    if not ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(ids)))
    rows = await adb_core.fetch(
        "SELECT ms_id, product_name, sale_price_cents, cost_price_cents, currency "
        f"FROM product_prices WHERE ms_id IN ({placeholders})",
        *ids,
    )
    return {r["ms_id"]: _price_row_major(r) for r in rows}




async def get_all_product_prices() -> list[dict]:
    """Все заданные цены. Для admin-UI экрана «Цены». asyncpg Stage 9 (#21)."""
    rows = await adb_core.fetch(
        "SELECT ms_id, product_name, sale_price_cents, cost_price_cents, currency, updated_at "
        "FROM product_prices ORDER BY product_name"
    )
    return [_price_row_major(r) for r in rows]


def _invalidate_product_price_cache() -> None:
    with _product_price_lock:
        _PRODUCT_PRICE_CACHE.clear()


async def create_cash_deposit(
    manager_id: int,
    amount: float,
    allocations: list[tuple] | None = None,
    idem_key: str | None = None,
    *,
    currency: str | None = None,
    order_ids: list[int] | None = None,
) -> dict:
    """Создать сдачу (status=pending) + распределение по заказам.

    `idem_key` — результат пишется в ключ идемпотентности той же транзакцией.

    Куда идут деньги (авто, `allocations is None`):
      1. **Наличные строки разбивки на руках** у этого менеджера в валюте сдачи
         (`order_payments.cash_on_hand`), старые первыми. Это и есть «деньги
         по неподтверждённым заказам»: менеджер записал «клиент отдал 5 000
         наличными», и сдача 5 000 закрывает именно их. Строка берётся целиком;
         денег меньше — строка делится. Привязка — `cash_deposit_parts`.
      2. Остаток сдачи в БАЗОВОЙ валюте — прежний FIFO по долгам без разбивки
         (`get_manager_open_orders_for_deposit`, `cash_deposit_orders`): старые
         заказы, по которым деньги ещё никак не заявлены.
      3. Что не легло никуда — `unallocated_cents` (сдача всё равно создаётся:
         наличные в кассу бывают и не по заказам; экран это показывает).
    `order_ids` — ручной выбор: распределять только по этим заказам.

    Почему прод-сдачи были «Заказы: —»: автоплатёж одобрения «оплата сразу»
    заявлял ВСЮ сумму заказа ожидающим платежом, и FIFO по долгам видел остаток
    0 — распределять было не на что. Разбивка этот автоплатёж заменяет, а
    наличная её часть стала целью сдачи.

    allocations: список (order_id, amount) для ручного режима по долгам —
    прежний контракт, без разбивки.

    Валюта сдачи — `cash_deposit_currency` (у `cash_deposits` колонки нет);
    без строки сдача в базовой валюте.

    Замок — строки заказов (services.debts.lock_orders), общий с отметкой
    оплаты, разбивкой и «Получил деньги».
    """
    from services import order_payments

    from config import ALLOWED_CURRENCIES, BASE_CURRENCY

    base_cur = (BASE_CURRENCY or "USD").upper()
    dep_cur = (currency or base_cur).upper()

    if dep_cur not in {c.upper() for c in ALLOWED_CURRENCIES}:
        return {"ok": False, "error": f"Валюта {dep_cur} не поддерживается"}
    ok, err = validate_amount_in_currency(amount, dep_cur)
    if not ok:
        return {"ok": False, "error": err}

    is_manual = allocations is not None
    allocs: list[tuple] = list(allocations) if allocations is not None else []

    # T2.11: FIFO-расчёт — ДО захвата коннекта под транзакцию (внутренние
    # запросы с отдельных коннектов пула при PG_POOL_MAX одновременных сдачах
    # вешали процесс, §2.13). Под транзакцией — только перепроверка.
    candidates: list[dict] = []
    part_rows: list[dict] = []
    if not is_manual:
        part_rows = await order_payments.cash_on_hand(manager_id, dep_cur)
        if dep_cur == base_cur:
            candidates = await get_manager_open_orders_for_deposit(manager_id)
        if order_ids:
            wanted = {int(o) for o in order_ids}
            part_rows = [r for r in part_rows if int(r["order_id"]) in wanted]
            candidates = [o for o in candidates if int(o["id"]) in wanted]

    from services.debts import lock_orders

    amount_cents = money.to_cents(amount)
    parts_taken: list[dict] = []
    left_cents = amount_cents
    async with adb_core.transaction() as txn:
        target_ids = (
            [o["id"] for o in candidates] + [int(r["order_id"]) for r in part_rows]
            if not is_manual else [a[0] for a in allocs]
        )
        await lock_orders(txn, target_ids)
        if not is_manual:
            # Сначала наличные строки разбивки (перечитываются под замком).
            parts_taken, left_cents = await order_payments.allocate_deposit_to_parts_locked(
                txn, manager_id, dep_cur, amount_cents,
                sorted({int(r["order_id"]) for r in part_rows}) if part_rows else [],
            ) if part_rows else ([], amount_cents)
            # Остаток — прежний FIFO по долгам без разбивки. Перепроверка ПОД
            # ЗАМКОМ тем же коннектом (conn=txn): между расчётом и этим
            # моментом другой вызов мог заявить часть остатка.
            fresh = await deposit_remaining_cents_for_orders([o["id"] for o in candidates], conn=txn)
            for o in candidates:
                if left_cents <= 0:
                    break
                available = min(int(o.get("remaining_cents") or 0), fresh.get(o["id"], 0))
                take_cents = min(available, left_cents)
                if take_cents > 0:
                    allocs.append((o["id"], float(money.from_cents(take_cents))))
                    left_cents -= take_cents
        else:
            fresh = await deposit_remaining_cents_for_orders(target_ids, conn=txn)
            # Ручное распределение проверяется той же формулой: иначе оно было
            # третьим путём заявить уже заявленные деньги.
            wanted_c: dict[int, int] = {}
            for order_id, alloc in allocs:
                wanted_c[int(order_id)] = wanted_c.get(int(order_id), 0) + money.to_cents(alloc)
            for order_id, cents in wanted_c.items():
                if cents > fresh.get(order_id, 0):
                    return {
                        "ok": False,
                        "error": f"По заказу #{order_id} столько заявить нельзя: "
                        f"доступно {money.format_cents(fresh.get(order_id, 0), decimals=2)}",
                    }
            left_cents = amount_cents - sum(wanted_c.values())

        if USE_POSTGRES:
            deposit_id = await txn.fetchval(
                "INSERT INTO cash_deposits (manager_id, amount_cents, deposited_at, status, created_at) "
                "VALUES ($1, $2, $3, 'pending', $4) RETURNING id",
                manager_id, amount_cents, now_str(), now_str(),
            )
        else:
            await txn.execute(
                "INSERT INTO cash_deposits (manager_id, amount_cents, deposited_at, status, created_at) "
                "VALUES ($1, $2, $3, 'pending', $4)",
                manager_id, amount_cents, now_str(), now_str(),
            )
            deposit_id = await txn.fetchval("SELECT last_insert_rowid()")
        await txn.execute(
            "INSERT INTO cash_deposit_currency (deposit_id, currency) VALUES ($1, $2)",
            deposit_id, dep_cur,
        )
        for p in parts_taken:
            await txn.execute(
                "INSERT INTO cash_deposit_parts (deposit_id, part_id, order_id, amount_cents) "
                "VALUES ($1, $2, $3, $4)",
                deposit_id, p["part_id"], p["order_id"], p["amount_cents"],
            )
        for order_id, alloc in allocs:
            await txn.execute(
                "INSERT INTO cash_deposit_orders (deposit_id, order_id, amount_allocated_cents, is_manual) "
                "VALUES ($1, $2, $3, $4)",
                deposit_id, order_id, money.to_cents(alloc), 1 if is_manual else 0,
            )
        await idem_store_in(txn, idem_key, {"ok": True, "deposit_id": deposit_id})
    return {
        "ok": True,
        "deposit_id": deposit_id,
        "currency": dep_cur,
        "allocations": allocs,
        "parts": parts_taken,
        "unallocated_cents": max(0, left_cents),
    }


async def confirm_cash_deposit(deposit_id: int, confirmed_by: int, confirmed_name: str = "") -> dict:
    """Подтвердить сдачу и закрыть покрытые ею заказы — ОДНОЙ транзакцией.

    Возвращает {ok, closed_orders, self_confirmed, self_note, approval_mode}.
    Отказ по правам (`order_payments.confirm_rights`) — {ok: False, error,
    code: 'confirm_forbidden', status: 403}.

    Раньше статус сдачи коммитился первым, а заказы закрывались потом, каждый
    своей транзакцией. Падение между ними оставляло сдачу `confirmed` с
    незакрытыми заказами — и без пути повтора. Теперь либо всё, либо ничего.

    Что подтверждается вместе со сдачей:
      * наличные строки разбивки (`cash_deposit_parts`) — их ожидающие платежи
        становятся confirmed; заказ закрывается штатным
        `_close_order_if_covered_locked` (как подтверждение платежа);
      * прежнее распределение по долгам (`cash_deposit_orders`) — покрытый
        заказ получает `payment_confirmed`/`paid`, как раньше.

    Покрытие считает `services.debts.calc_order_balances(conn=txn)` — внутри
    той же транзакции она видит только что подтверждённые платежи и сдачу.

    Замки: advisory по сдаче (два одновременных подтверждения одной), затем
    строки заказов по возрастанию id (`lock_orders` — «заказ → платёж»), затем
    CAS статуса сдачи.
    """
    from services import order_payments

    dep_head = await adb_core.fetchrow("SELECT manager_id FROM cash_deposits WHERE id = $1", deposit_id)
    # Кто вправе подтвердить — сервисный рубеж (HTTP и кнопка dep_ok): менеджер
    # не подтверждает сдачи, пока в системе есть руководитель/бухгалтер.
    rights = await order_payments.confirm_rights(
        confirmed_by, [dep_head["manager_id"]] if dep_head else []
    )
    if not rights["allowed"]:
        return {"ok": False, "error": rights["error"], "code": order_payments.CONFIRM_FORBIDDEN,
                "status": 403}
    part_orders, part_currencies = await order_payments.deposit_part_orders(deposit_id)
    # Курс для снимка fx_rate_to_base — ДО транзакции (sync-чтение на SQLite
    # ждало бы нашу же пишущую транзакцию), как в confirm_payment.
    rates = {c: await asyncio.to_thread(get_currency_rate, c) for c in part_currencies if c}

    closed: list[int] = []
    try:
        return await _confirm_cash_deposit_txn(
            deposit_id, confirmed_by, confirmed_name, rights, part_orders, rates, closed
        )
    except order_payments.PaymentError as e:
        # Строка сдачи уже не ждёт (confirm_deposit_parts_locked) — транзакция
        # откатилась целиком, сдача осталась pending.
        return {"ok": False, "error": e.message, "code": e.code, "status": e.status}


async def _confirm_cash_deposit_txn(
    deposit_id: int, confirmed_by: int, confirmed_name: str, rights: dict,
    part_orders: list[int], rates: dict, closed: list[int],
) -> dict:
    from services import order_payments
    from services.debts import calc_order_balances, lock_orders

    async with adb_core.transaction() as txn:
        if USE_POSTGRES:
            await txn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"cash_deposit:confirm:{deposit_id}"
            )
        legacy_rows = await txn.fetch(
            "SELECT order_id FROM cash_deposit_orders WHERE deposit_id = $1", deposit_id
        )
        legacy_ids = sorted({int(r["order_id"]) for r in legacy_rows})
        order_ids = sorted(set(legacy_ids) | set(part_orders))
        await lock_orders(txn, order_ids)
        rc = await txn.execute(
            "UPDATE cash_deposits SET status = 'confirmed', confirmed_by = $1, confirmed_at = $2 "
            "WHERE id = $3 AND status = 'pending'",
            confirmed_by, now_str(), deposit_id,
        )
        if rc <= 0:
            return {"ok": False, "error": "Сдача уже обработана"}

        paid_by_parts = await order_payments.confirm_deposit_parts_locked(txn, deposit_id, rates)
        for oid in paid_by_parts:
            # Строки разбивки — это платежи: заказ закрывается ровно как при
            # подтверждении платежа (paid_confirmed_at), какой бы из путей —
            # сдача или кнопка «Подтвердить» по карте — ни оказался последним.
            done, _cents = await _close_order_if_covered_locked(txn, oid, confirmed_by, confirmed_name)
            if done:
                closed.append(oid)

        balances = await calc_order_balances(legacy_ids, conn=txn)
        for oid in legacy_ids:
            bal = balances.get(oid)
            if bal is None or oid in closed:
                continue
            # net <= 0 (полный возврат) — это не «оплачено», статус ведёт
            # confirm_return.
            net_owed_cents = bal.total_cents - bal.returns_cents
            if net_owed_cents <= 0 or bal.remaining_cents > 0:
                continue
            rc = await txn.execute(
                "UPDATE orders SET payment_confirmed = 1, payment_confirmed_at = $1, "
                "status = 'paid', updated_at = $2 WHERE id = $3 AND payment_confirmed = 0",
                now_str(), now_str(), oid,
            )
            if rc > 0:
                closed.append(oid)

    role = await asyncio.to_thread(get_role, confirmed_by)
    note = rights["note"]
    await asyncio.to_thread(
        add_audit_log,
        confirmed_by,
        confirmed_name,
        role,
        "cash_deposit_confirmed",
        f"Сдача #{deposit_id} подтверждена; наличные по заказам: "
        f"{', '.join('#' + str(o) for o in part_orders) or '—'}; закрыты заказы: {closed or '—'}"
        + (f" ({note})" if note else ""),
    )
    return {"ok": True, "closed_orders": sorted(closed), "self_confirmed": bool(rights["own"]),
            "self_note": note, "approval_mode": rights["mode"]}


async def reject_cash_deposit(
    deposit_id: int, rejected_by: int, rejected_name: str, reason: str
) -> dict:
    """asyncpg Stage 17 (#21): native async через adb_core.execute; add_audit_log/
    get_role (sync) — мост через to_thread."""
    # Round 6 (L_R8): clip reason — DB-колонка TEXT (unbounded), а сообщение
    # потом шлётся менеджеру через bot.send_message (Telegram-лимит 4096).
    # UI-валидация в webapp/server.py есть, но прямой бот-FSM вызов её обходит.
    reason = (reason or "").strip()[:500]
    updated = (
        await adb_core.execute(
            "UPDATE cash_deposits SET status = 'rejected', reject_reason = $1, "
            "confirmed_by = $2, confirmed_at = $3 WHERE id = $4 AND status = 'pending'",
            reason, rejected_by, now_str(), deposit_id,
        )
        > 0
    )
    if not updated:
        return {"ok": False, "error": "Сдача уже обработана"}
    await asyncio.to_thread(
        add_audit_log,
        rejected_by,
        rejected_name,
        await asyncio.to_thread(get_role, rejected_by),
        "cash_deposit_rejected",
        f"Сдача #{deposit_id} отклонена: {reason[:200]}",
    )
    return {"ok": True}


async def get_cash_deposit(deposit_id: int) -> dict | None:
    """asyncpg Stage 9 (#21): native async через adb_core."""
    row = await adb_core.fetchrow("SELECT * FROM cash_deposits WHERE id = $1", deposit_id)
    return _with_major(row, ("amount", "amount_cents"))


async def get_cash_deposit_orders(deposit_id: int) -> list[dict]:
    """asyncpg Stage 9 (#21)."""
    rows = await adb_core.fetch(
        "SELECT order_id, amount_allocated_cents FROM cash_deposit_orders WHERE deposit_id = $1",
        deposit_id,
    )
    return _with_major(rows, ("amount_allocated", "amount_allocated_cents"))


async def get_cash_deposit_orders_batch(deposit_ids: list[int]) -> dict[int, list[dict]]:
    """Батч-версия get_cash_deposit_orders: {deposit_id: [{order_id, amount_allocated}]}.
    Один SQL вместо N (был N+1 в /api/deposits/pending). Депозиты без привязанных
    заказов в результат не попадают — caller использует .get(id, []).
    asyncpg Stage 9 (#21): IN-список — $1..$N."""
    if not deposit_ids:
        return {}
    unique_ids = list(set(deposit_ids))
    placeholders = ",".join(f"${i + 1}" for i in range(len(unique_ids)))
    rows = await adb_core.fetch(
        f"SELECT deposit_id, order_id, amount_allocated_cents FROM cash_deposit_orders "
        f"WHERE deposit_id IN ({placeholders})",
        *unique_ids,
    )
    grouped: dict[int, list[dict]] = {}
    for d in rows:
        # Форма элемента — как у get_cash_deposit_orders (без deposit_id).
        grouped.setdefault(d["deposit_id"], []).append(
            _with_major(
                {"order_id": d["order_id"], "amount_allocated_cents": d["amount_allocated_cents"]},
                ("amount_allocated", "amount_allocated_cents"),
            )
        )
    return grouped


async def get_manager_cash_deposits(manager_id: int, limit: int = 20) -> list[dict]:
    """asyncpg Stage 9 (#21)."""
    rows = await adb_core.fetch(
        "SELECT * FROM cash_deposits WHERE manager_id = $1 "
        "ORDER BY created_at DESC LIMIT $2",
        manager_id,
        limit,
    )
    return _with_major(rows, ("amount", "amount_cents"))


def get_deposit_confirmers() -> list[int]:
    """user_id ролей, которые подтверждают сдачи: admin/boss/bookkeeper.

    Пока бухгалтера нет, карточку получают и менеджеры — они замещают его
    (`services.roles.ROLE_ALSO_ACTS_AS`). Импорт внутри: roles импортирует
    database на уровне модуля."""
    from services.roles import notify_recipients

    try:
        users = get_all_users()
    except Exception:
        return []
    return notify_recipients(users, ("admin", "boss", "bookkeeper"))


async def get_pending_cash_deposits() -> list[dict]:
    """Сдачи, ждущие подтверждения (для боса/бухгалтера).

    asyncpg-миграция Stage 5 (задача #21): нативный async через adb_core.
    Вызовы: handlers/webapp (`await adb.…`), async-cron run_ops_monitor
    (`await`), тесты (`asyncio.run`). Loop-aware пул (Stage 4) делает
    cron-вызов безопасным.
    """
    rows = await adb_core.fetch(
        "SELECT * FROM cash_deposits WHERE status = 'pending' "
        "ORDER BY deposited_at ASC"
    )
    return _with_major(rows, ("amount", "amount_cents"))


async def get_pending_payments() -> list[dict]:
    """Платежи, ждущие подтверждения (карта/перечисление — руководитель или
    бухгалтер; наличные тоже приходят сюда со статусом pending, хоть и
    подтверждаются только сдачей — способ смотри в `order_payments.
    parts_by_payment`, «нет строки» = старый платёж без разбивки).

    Источник для `services.notify_policy`/`services.boss_digest`: то, что не
    ушло боссу немедленным пушем (сумма ниже `boss_instant_threshold_usd»),
    должно быть видно в вечернем дайджесте — без отдельной очереди, прямо из
    текущего состояния `payments`. Живой заказ (WP-11) — платёж по удалённому
    заказу не в счёт, как и в `get_cash_history`."""
    rows = await adb_core.fetch(
        f"SELECT p.* FROM payments p {_LIVE_ORDER_PAYMENT_JOIN.format(p='p')} "
        f"WHERE p.status = 'pending' AND {_LIVE_ORDER_PAYMENT_FILTER} "
        "ORDER BY p.created_at ASC"
    )
    return _with_major(rows, ("amount", "amount_cents"))


async def get_confirmed_payments_since(since_iso: str) -> list[dict]:
    """Платежи, подтверждённые с момента `since_iso` (для вечернего дайджеста
    боссу — раздел «мелкие поступления»: то, что ушло без немедленного пуша,
    но уже реально пришло). Граница — по `COALESCE(confirmed_at, created_at)`,
    как в `get_cash_history`/итоге «Деньги» — платёж без confirmed_at
    (мигрированная история) считается по дате записи."""
    rows = await adb_core.fetch(
        f"SELECT p.* FROM payments p {_LIVE_ORDER_PAYMENT_JOIN.format(p='p')} "
        f"WHERE p.status = 'confirmed' AND {_LIVE_ORDER_PAYMENT_FILTER} "
        "AND COALESCE(p.confirmed_at, p.created_at) >= $1 "
        "ORDER BY p.created_at ASC",
        since_iso,
    )
    return _with_major(rows, ("amount", "amount_cents"))


async def get_overdue_undeposited_orders(days: int = 2) -> list[dict]:
    """Отгруженные неоплаченные заказы старше `days` (cash-эскалация, §7.6).

    asyncpg-миграция Stage 7 (задача #21): нативный async через adb_core.
    Порог cutoff считаем в Python (local TZ) и передаём параметром — НЕ
    сравниваем с SQL NOW() (см. CLAUDE.md). Вызовы: async-cron (`await`),
    тест (`asyncio.run`)."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return await adb_core.fetch(
        "SELECT * FROM orders WHERE status = 'shipped' AND payment_confirmed = 0 "
        # Закрытый платежами (разбивка/карта) заказ сдавать уже нечего.
        "AND paid_confirmed_at IS NULL "
        "AND COALESCE(shipped_at, created_at) < $1 "
        "ORDER BY user_id, created_at",
        cutoff,
    )


class _TxnAbort(Exception):
    """Внутренний сигнал отката adb_core.transaction() с user-facing сообщением.

    adb_core.transaction() откатывает транзакцию на ЛЮБОМ исключении — поднимаем
    это внутри `async with`, ловим снаружи и возвращаем {ok: False, error: message}.
    Используется money-функциями (Stage 14+), где нужен откат уже сделанных
    внутри критической секции записей (а не просто early-return = commit)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _is_returnable(order: dict) -> bool:
    """Заказ можно вернуть, если он отгружен/оплачен/частично-возвращён ИЛИ
    фактически оплачен по легаси-схеме (paid_confirmed_at заполнен — оплата
    через /pay подтверждена, но ярлык status мог остаться 'approved')."""
    if order.get("status") in ("shipped", "paid", "partially_returned"):
        return True
    return bool(order.get("paid_confirmed_at"))


async def create_return(
    order_id: int,
    return_type: str,
    reason: str,
    items: list[tuple],
    refund_method: str | None,
    created_by: int,
    force: bool = False,
    idem_key: str | None = None,
) -> dict:
    """Создать возврат (status=pending) + позиции. items = [(order_item_id, qty, amount)].
    `idem_key` — результат пишется в ключ идемпотентности той же транзакцией.
    return_type: 'partial'|'full'. refund_method: 'cash'|'debt_reduction'|'no_refund'.
    Доступно для shipped/paid/partially_returned. Дедлайн (return_deadline_days)
    блокирует, если не force (вызывающий решает по роли). Возвращает {ok, return_id}.

    asyncpg #21: native async. get_order/get_order_items — async; get_setting
    (sync, TTL-кэш) — мост через to_thread; критическая секция (advisory-lock +
    dup-check + INSERT) — adb_core.transaction() (advisory-xact-lock держится до
    конца tx, как раньше).
    """
    order = await get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    if not _is_returnable(order):
        return {"ok": False, "error": "Возврат доступен только для отгруженных/оплаченных"}
    if not items:
        return {"ok": False, "error": "Не указаны позиции возврата"}

    # Дедлайн (read-only, до блокировки).
    deadline_days = int(await asyncio.to_thread(get_setting, "return_deadline_days", 90))
    shipped_at = order.get("shipped_at")
    if shipped_at and not force:
        limit = (datetime.now() - timedelta(days=deadline_days)).strftime("%Y-%m-%d %H:%M:%S")
        if shipped_at < limit:
            return {
                "ok": False,
                "error": f"Возврат позже {deadline_days} дней — нужно подтверждение",
            }

    # H1: режем количество по доступному остатку (quantity - returned_qty) и
    # считаем сумму строки из price_cents (источник истины), а не из float price
    # и не доверяя переданному amount. Это read-only прикидка; финальную защиту
    # от overshoot даёт атомарный guard в confirm_return.
    oitems = {it["id"]: it for it in await get_order_items(order_id)}
    clamped: list[tuple] = []  # (order_item_id, take_qty, line_cents)
    for oitem_id, qty, _amount in items:
        oi = oitems.get(oitem_id)
        if not oi:
            continue
        # Десятичной арифметикой, а не float: 1.1 − 0.9 во float даёт
        # 0.20000000000000007, и такой «остаток» при подтверждении не пролезал
        # в `returned_qty + qty <= quantity` на NUMERIC-колонке (overshoot).
        available = Decimal(str(oi.get("quantity", 0) or 0)) - Decimal(
            str(oi.get("returned_qty", 0) or 0)
        )
        take = float(min(Decimal(str(qty)), available))
        if take <= 0:
            continue
        clamped.append((oitem_id, take, money.mul_qty(_price_cents(oi), take)))
    if not clamped:
        return {"ok": False, "error": "Нет позиций, доступных к возврату"}

    total_cents = sum(c for _, _, c in clamped)
    total_amount = float(money.from_cents(total_cents))

    # H1 + Round 6 RACE-2: не плодим параллельные возвраты по одному заказу.
    # Advisory-lock держится до конца транзакции и покрывает И dup-check, И INSERT
    # (иначе TOCTOU пропускал два возврата, и каждый confirm_return наращивал
    # returned_qty → overflow > quantity). Dup-check возвращает {ok: False} без
    # записей — раннему return соответствует commit пустой tx (lock освобождён).
    async with adb_core.transaction() as txn:
        if USE_POSTGRES:
            await txn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"return:order:{order_id}")
        cnt = await txn.fetchval(
            "SELECT COUNT(*) FROM returns WHERE order_id = $1 "
            "AND status = 'pending'",
            order_id,
        )
        if int(cnt or 0) > 0:
            return {"ok": False, "error": "По заказу уже есть возврат на рассмотрении"}
        refusal = await _debt_reduction_refusal(txn, order_id, refund_method, total_cents)
        if refusal:
            return {"ok": False, "error": refusal, "code": "no_debt_to_reduce"}

        if USE_POSTGRES:
            return_id = await txn.fetchval(
                "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
                "refund_method, created_by, status, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7) RETURNING id",
                order_id, return_type, reason, total_cents, refund_method, created_by, now_str(),
            )
        else:
            await txn.execute(
                "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
                "refund_method, created_by, status, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)",
                order_id, return_type, reason, total_cents, refund_method, created_by, now_str(),
            )
            return_id = await txn.fetchval("SELECT last_insert_rowid()")
        for oitem_id, qty, line_cents in clamped:
            await txn.execute(
                "INSERT INTO return_items (return_id, order_item_id, qty, amount_cents) "
                "VALUES ($1, $2, $3, $4)",
                return_id, oitem_id, qty, line_cents,
            )
        await idem_store_in(
            txn, idem_key, {"ok": True, "return_id": return_id, "total_amount": total_amount}
        )
    return {"ok": True, "return_id": return_id, "total_amount": total_amount}


async def _debt_reduction_refusal(txn, order_id: int, refund_method: str | None,
                                  amount_cents: int) -> str | None:
    """Возврат «в счёт долга» больше долга — отказ текстом, иначе None.

    Возврат «в счёт долга» уменьшает остаток к оплате. Если клиент уже заплатил
    (долга нет или он меньше суммы возврата), вычитать не из чего: сумма
    возврата — его переплата, и она молча исчезала бы — ни в долгах, ни в
    выдаче из кассы. Долг = то, что по заказу ещё можно заявить
    (`calc_claimable_cents`): ожидающие оплаты и сдачи считаются уже
    заплаченными — после их подтверждения переплата была бы та же. Замок
    заказа берётся здесь же (`lock_orders`), как у всех «заявлено по заказу».
    """
    if refund_method != "debt_reduction":
        return None
    from services.debts import calc_claimable_cents, lock_orders

    await lock_orders(txn, [order_id])
    debt = (await calc_claimable_cents([order_id], conn=txn)).get(order_id, 0)
    if amount_cents <= debt:
        return None
    from services.order_payments import fmt_cents

    cur = (await txn.fetchval("SELECT currency FROM orders WHERE id = $1", order_id)) or ""
    head = (
        "Долга по заказу нет — клиент уже заплатил"
        if debt <= 0
        else f"Долг по заказу {fmt_cents(debt, cur)} меньше возврата"
    )
    return (
        f"{head}: вернуть {fmt_cents(amount_cents, cur)} «в счёт долга» "
        "нельзя — переплата клиента пропала бы. Выберите «Наличными» (деньги выдадут из "
        "кассы) или «Без возврата»."
    )


async def mark_return_goods_received(return_id: int, by: int) -> dict:
    """Кладовщик отметил «товар получен» (флаг, статус остаётся pending).

    asyncpg Stage 14 (#21): native async через adb_core."""
    rc = await adb_core.execute(
        "UPDATE returns SET goods_received = 1 WHERE id = $1 AND status = 'pending'",
        return_id,
    )
    return {"ok": rc > 0}


async def _plan_return_stock(return_id: int, order_id: int) -> dict:
    """Что приходовать на склад по возврату. Только чтение, ДО транзакции.

    Возвращает {positions, unmatched, skipped_reason}. `positions` — строки
    приходной накладной (только ВОЗВРАЩЁННЫЕ позиции и их количества из
    return_items, а не весь заказ).

    Приходуем, только если товар по заказу действительно СПИСЫВАЛСЯ со склада:
    есть расходная накладная отгрузки (`order_shipment.invoice_id`) или заказ
    эпохи МойСклад (списан там, и снимок остатков уже это учёл — так же судит
    `order_shipment.list_failed`). Локальный заказ, по которому накладная не
    провелась (failed_at, позиции без карточек), остаток не уменьшал — приход
    по его возврату прибавил бы товар, которого склад не терял.

    Сопоставление с номенклатурой — тем же `_resolve_products`, что у отгрузки:
    возврат обязан попасть на ту же карточку, с которой товар списали.
    """
    from services import order_shipment

    order = await adb_core.fetchrow(
        "SELECT o.ms_demand_id, o.ms_customerorder_id, s.invoice_id AS ship_invoice_id "
        "FROM orders o LEFT JOIN order_shipment s ON s.order_id = o.id WHERE o.id = $1",
        order_id,
    )
    written_off = bool(
        order
        and (
            order.get("ship_invoice_id")
            or str(order.get("ms_demand_id") or "").strip()
            or str(order.get("ms_customerorder_id") or "").strip()
        )
    )
    if not written_off:
        return {
            "positions": [],
            "unmatched": [],
            "skipped_reason": "по заказу не было расходной накладной — остаток не списывался",
        }
    rows = await adb_core.fetch(
        "SELECT ri.qty AS quantity, oi.product_name, op.product_id "
        "FROM return_items ri JOIN order_items oi ON oi.id = ri.order_item_id "
        "LEFT JOIN order_item_products op ON op.item_id = oi.id "
        "WHERE ri.return_id = $1 ORDER BY ri.id",
        return_id,
    )
    positions, unmatched = await order_shipment._resolve_products(rows)
    # Цену продажи в приход НЕ пишем: цена в приходной накладной читается как
    # закупочная, и цена продажи исказила бы себестоимость. Но если учёт
    # себестоимости включён, отгрузка уже зафиксировала, почём товар ушёл
    # (sale_costs, в базовой валюте) — возвращаем его по ТОЙ ЖЕ себестоимости,
    # иначе возвращённая партия ляжет «без себестоимости» и следующая продажа
    # выпадет из прибыли. Хоть одна строка отгрузки без себестоимости — цену
    # не угадываем. Учёт выключен — sale_costs пуст, поведение прежнее.
    unit_cost: dict[int, int] = {}
    ship_invoice_id = order.get("ship_invoice_id") if order else None
    if ship_invoice_id:
        for r in await adb_core.fetch(
            "SELECT product_id, SUM(quantity) AS qty, SUM(cost_base_cents) AS cost, "
            "COUNT(*) AS n, COUNT(cost_base_cents) AS n_cost "
            "FROM sale_costs WHERE invoice_id = $1 GROUP BY product_id",
            int(ship_invoice_id),
        ):
            qty = float(r["qty"] or 0)
            if qty > 0 and r["n"] == r["n_cost"]:
                unit_cost[int(r["product_id"])] = round(int(r["cost"]) / qty)
    for p in positions:
        p["price_cents"] = unit_cost.get(int(p["product_id"]))
    reason = None if positions else "ни одна позиция возврата не сопоставлена с номенклатурой"
    return {
        "positions": positions,
        "unmatched": unmatched,
        "skipped_reason": reason,
        # Себестоимость — в базовой валюте: приход по ней оформляется в базовой.
        "priced_in_base": any(p["price_cents"] is not None for p in positions),
    }


async def confirm_return(return_id: int, confirmed_by: int, confirmed_name: str = "") -> dict:
    """Подтвердить возврат: returned_qty += по позициям, статус заказа
    (returned|partially_returned), обработка refund (cash → отрицательная
    сдача; debt_reduction/no_refund — учёт в долге), ПРИХОД товара на склад
    приходной накладной «Возврат по заказу #N» — всё одной транзакцией.
    Возвращает {ok, order_status, invoice_id, invoice_number, stock_skipped}.

    asyncpg Stage 14 (#21): native async. Транзакционные границы сохранены как в
    sync-версии — критическая секция (confirm + overshoot-guard) одна транзакция
    (откат через _TxnAbort), батч-восстановление/статус/refund — отдельные
    операции. Sync money-core хелперы (get_order/get_order_items/_adjust_batch_qty/
    add_audit_log/get_role) мостим через to_thread."""
    ret = await adb_core.fetchrow("SELECT * FROM returns WHERE id = $1", return_id)
    if not ret:
        return {"ok": False, "error": "Возврат не найден"}
    if ret.get("status") != "pending":
        return {"ok": False, "error": "Возврат уже обработан"}

    # Критическая атомарная секция: подтверждение + overshoot-guard.
    # Round 6 (L_R2): атомарная проверка returned_qty + delta <= quantity.
    # Без неё concurrent confirm двух разных returns по одному заказу мог
    # наращивать returned_qty за пределы quantity (overshoot → лишний MS-doc).
    from config import BASE_CURRENCY

    order_id = ret["order_id"]
    ritems: list[dict] = []
    new_status = "partially_returned"
    return_status = "partial"

    # Выдача наличными: касса ведётся в БАЗОВОЙ валюте (у cash_deposits нет
    # колонки валюты), поэтому сумму возврата надо перевести по курсу. Считаем
    # ДО транзакции: без курса возврат не проводим вовсе. Раньше при
    # незаданном курсе сумма писалась «как есть» — возврат 1 250 000 сум
    # выдавал из кассы 1 250 000 ДОЛЛАРОВ, и касса уходила в минус на
    # несуществующие деньги. Понятный отказ лучше неверной суммы: курс задают
    # за минуту, а испорченную кассу потом не сверить.
    refund_base_cents: int | None = None
    if ret.get("refund_method") == "cash":
        order_cur = (
            await adb_core.fetchval("SELECT currency FROM orders WHERE id = $1", order_id)
            or BASE_CURRENCY
            or "USD"
        ).upper()
        refund_major = float(money.from_cents(int(ret.get("total_amount_cents") or 0)))
        refund_base = await asyncio.to_thread(convert_to_base, refund_major, order_cur)
        if refund_base is None:
            return {
                "ok": False,
                "error": (
                    f"Нет курса {order_cur} к {(BASE_CURRENCY or 'USD').upper()}: "
                    "выдачу из кассы не пересчитать. Задайте курс валют и "
                    "подтвердите возврат снова."
                ),
            }
        refund_base_cents = money.to_cents(refund_base)

    # План прихода — до транзакции: сопоставление читает справочник через
    # adb_core напрямую, а на SQLite чтение мимо открытой BEGIN IMMEDIATE
    # транзакции ждало бы её же. Позиции возврата после создания не меняются,
    # так что план не устаревает к моменту записи.
    from services import warehouse

    stock_plan = await _plan_return_stock(return_id, order_id)
    order_head = await adb_core.fetchrow(
        "SELECT agent_id, currency FROM orders WHERE id = $1", order_id
    ) or {}
    stock_warehouse_id = (
        await warehouse.default_warehouse_id() if stock_plan["positions"] else None
    )
    receipt: dict | None = None
    # Атомарная секция (WP-08): подтверждение возврата + overshoot-guard + СТАТУС
    # ЗАКАЗА + денежный refund — в ОДНОЙ транзакции. Раньше статус и cash-выплата
    # писались ПОСЛЕ коммита подтверждения → крах между ними оставлял возврат
    # «confirmed» (товар оприходован), а выплату из кассы незаписанной → касса
    # завышалась без возможности reconcile. Теперь либо всё, либо ничего.
    from services.debts import lock_orders

    closed_by_return = False
    closed_cents = 0
    try:
        async with adb_core.transaction() as txn:
            # Подтверждённый возврат уменьшает «можно заявить» — замок заказа
            # тот же, что у отметки оплаты и сдачи (services.debts.lock_orders):
            # иначе параллельная отметка оплаты считала остаток ещё без возврата.
            await lock_orders(txn, [order_id])
            # Между оформлением и подтверждением клиент мог доплатить: «в счёт
            # долга» сверх долга не подтверждаем по той же причине, что и не
            # оформляем (_debt_reduction_refusal).
            refusal = await _debt_reduction_refusal(
                txn, order_id, ret.get("refund_method"), int(ret.get("total_amount_cents") or 0)
            )
            if refusal:
                raise _TxnAbort(refusal)
            # T2.8: подтвердить возврат можно только если товар ПРИНЯТ.
            # goods_received писался (mark_return_goods_received), но никогда не
            # проверялся: босс подтверждал возврат → returned_qty рос, заказ
            # уходил в returned, а при refund_method='cash' деньги выдавались из
            # кассы (отрицательная сдача) — за товар, который физически мог не
            # приехать (§2.12).
            #
            # Проверка — частью того же CAS-UPDATE, а не отдельным SELECT'ом:
            # иначе между проверкой и записью флаг мог смениться.
            rc = await txn.execute(
                "UPDATE returns SET status = 'confirmed', confirmed_by = $1, confirmed_at = $2 "
                "WHERE id = $3 AND status = 'pending' AND goods_received = 1",
                confirmed_by, now_str(), return_id,
            )
            if rc == 0:
                # Различаем причины: «уже обработан» и «товар не принят» — иначе
                # кладовщик получит непонятное сообщение и пойдёт к админу.
                row = await txn.fetchrow(
                    "SELECT status, goods_received FROM returns WHERE id = $1", return_id
                )
                if row and row["status"] == "pending" and not row["goods_received"]:
                    raise _TxnAbort(
                        "Сначала отметьте, что товар принят "
                        "(кнопка «Товар получен» в карточке возврата)"
                    )
                raise _TxnAbort("Возврат уже обработан")
            ritems = await txn.fetch(
                "SELECT order_item_id, qty FROM return_items WHERE return_id = $1", return_id
            )
            for ri in ritems:
                rc2 = await txn.execute(
                    "UPDATE order_items SET returned_qty = returned_qty + $1 "
                    "WHERE id = $2 AND returned_qty + $1 <= quantity",
                    ri["qty"], ri["order_item_id"],
                )
                if rc2 == 0:
                    # Overshoot — откатываем confirm целиком, заявка остаётся pending.
                    raise _TxnAbort(
                        "Превышен доступный остаток к возврату (другой возврат "
                        "уже учтён). Перепроверьте и создайте новый."
                    )

            # Товар — обратно на склад, в ЭТОЙ ЖЕ транзакции: подтверждённый
            # возврат без прихода — это деньги клиенту за товар, которого в
            # остатках нет, и его уже не продать. Повторное подтверждение сюда
            # не доходит (CAS статуса выше), а PK return_receipt — второй рубеж
            # на случай, если статус вернут в pending руками.
            if await txn.fetchval(
                "SELECT return_id FROM return_receipt WHERE return_id = $1", return_id
            ) is not None:
                raise _TxnAbort("Возврат уже оприходован")
            invoice_id = None
            if stock_plan["positions"]:
                counterparty_id: int | None
                try:
                    counterparty_id = int(str(order_head.get("agent_id") or "").strip())
                except (TypeError, ValueError):
                    counterparty_id = None
                if counterparty_id is not None and await txn.fetchval(
                    "SELECT id FROM counterparties WHERE id = $1", counterparty_id
                ) is None:
                    # Устаревший id в заказе не повод держать возврат: приход
                    # проводим без контрагента, как и отгрузку legacy-заказа.
                    counterparty_id = None
                try:
                    receipt = await warehouse.create_invoice_in(
                        txn,
                        invoice_type="incoming",
                        warehouse_id=int(stock_warehouse_id or 1),
                        items=stock_plan["positions"],
                        counterparty_id=counterparty_id,
                        currency=(
                            str(BASE_CURRENCY or "USD")
                            if stock_plan.get("priced_in_base")
                            else str(order_head.get("currency") or BASE_CURRENCY or "USD")
                        ),
                        comment=f"Возврат по заказу #{order_id} (возврат #{return_id})",
                        created_by=confirmed_by,
                    )
                except warehouse.InvoiceError as e:
                    # Приход не прошёл — не подтверждаем и деньги: иначе
                    # возврат закрыт, а товара на складе нет (ровно исходный баг).
                    raise _TxnAbort(f"Товар не оприходован: {e.message}")
                invoice_id = int(receipt["invoice_id"])
            await txn.execute(
                "INSERT INTO return_receipt (return_id, order_id, invoice_id, "
                "skipped_reason, unmatched, created_at) VALUES ($1, $2, $3, $4, $5, $6)",
                return_id, order_id, invoice_id, stock_plan["skipped_reason"],
                ", ".join(stock_plan["unmatched"]) or None, now_str(),
            )

            # Полностью ли возвращён заказ? (returned_qty уже обновлён в этой txn.)
            items = await txn.fetch(
                "SELECT quantity, returned_qty FROM order_items WHERE order_id = $1", order_id
            )
            fully = (
                all(
                    float(it["returned_qty"] or 0) + 1e-9 >= float(it["quantity"] or 0)
                    for it in items
                )
                if items
                else False
            )
            new_status = "returned" if fully else "partially_returned"
            return_status = "full" if fully else "partial"
            await txn.execute(
                "UPDATE orders SET status = $1, return_status = $2, updated_at = $3 WHERE id = $4",
                new_status, return_status, now_str(), order_id,
            )

            # Refund: cash → отрицательная подтверждённая сдача (выдача из кассы).
            if refund_base_cents is not None:
                order = await txn.fetchrow(
                    "SELECT user_id FROM orders WHERE id = $1", order_id
                )
                # Сумма уже в базовой валюте (пересчитана до транзакции).
                await txn.execute(
                    "INSERT INTO cash_deposits (manager_id, amount_cents, deposited_at, "
                    "status, confirmed_by, confirmed_at, notes, created_at) "
                    "VALUES ($1, $2, $3, 'confirmed', $4, $5, $6, $7)",
                    (order or {}).get("user_id") or confirmed_by,
                    -refund_base_cents,  # выдача из кассы — отрицательная сдача
                    now_str(), confirmed_by, now_str(),
                    f"refund возврат #{return_id}", now_str(),
                )
            # debt_reduction / no_refund — отдельной записи не требуют (долг учитывает
            # подтверждённые возвраты в get_agent_current_debt).

            # Частичный возврат мог обнулить остаток к оплате (платежи уже
            # покрыли total − возврат) → закрываем заказ ТОЙ ЖЕ транзакцией:
            # отдельным коммитом сбой между ними оставлял покрытый заказ
            # открытым без пути повтора. Полный возврат (status='returned') —
            # терминальный, «оплатой» его не закрываем.
            if new_status == "partially_returned":
                closed_by_return, closed_cents = await _close_order_if_covered_locked(
                    txn, order_id, confirmed_by, confirmed_name
                )
    except _TxnAbort as e:
        return {"ok": False, "error": e.message}

    if closed_by_return:
        await _audit_order_fully_paid(order_id, confirmed_by, confirmed_name, closed_cents)

    role = await asyncio.to_thread(get_role, confirmed_by)
    await asyncio.to_thread(
        add_audit_log,
        confirmed_by,
        confirmed_name,
        role,
        "return_confirmed",
        f"Возврат #{return_id} по заказу #{order_id} ({return_status}, "
        f"{money.format_cents(int(ret.get('total_amount_cents') or 0))} USD, "
        f"{ret.get('refund_method')})",
    )
    stock_skipped = stock_plan["skipped_reason"]
    if receipt is not None:
        logger.info(
            "Возврат #%s по заказу #%s оприходован накладной %s",
            return_id, order_id, receipt["invoice_number"],
        )
    else:
        # Не ошибка, но расхождение склада, о котором надо знать сразу.
        logger.warning(
            "Возврат #%s по заказу #%s подтверждён БЕЗ прихода на склад: %s",
            return_id, order_id, stock_skipped,
        )
    if stock_plan["unmatched"]:
        logger.warning(
            "Возврат #%s: позиции без карточки номенклатуры не оприходованы: %s",
            return_id, stock_plan["unmatched"],
        )
    return {
        "ok": True,
        "order_status": new_status,
        "invoice_id": int(receipt["invoice_id"]) if receipt else None,
        "invoice_number": receipt["invoice_number"] if receipt else None,
        "stock_skipped": stock_skipped,
        "unmatched": list(stock_plan["unmatched"]),
    }


async def get_pending_returns() -> list[dict]:
    """Возвраты, ждущие подтверждения. asyncpg-миграция Stage 5 (задача #21):
    нативный async через adb_core. Вызовы: handlers/webapp (`await adb.…`),
    async-cron run_ops_monitor (`await`), тесты (`asyncio.run`)."""
    rows = await adb_core.fetch(
        "SELECT * FROM returns WHERE status = 'pending' "
        "ORDER BY created_at ASC"
    )
    return _with_major(rows, ("total_amount", "total_amount_cents"))


async def get_return(return_id: int) -> dict | None:
    """asyncpg Stage 10 (#21): native async через adb_core."""
    row = await adb_core.fetchrow("SELECT * FROM returns WHERE id = $1", return_id)
    return _with_major(row, ("total_amount", "total_amount_cents"))


async def get_return_positions_for_ms(return_id: int) -> list[dict]:
    """Позиции возврата с product_href и ценой (из order_items) — для сборки
    документа «Возврат покупателя» в МойСклад. amount берём из return_items.
    asyncpg Stage 10 (#21)."""
    return await adb_core.fetch(
        "SELECT oi.product_href AS product_href, oi.product_name AS product_name, "
        "ri.qty AS qty, oi.price_cents AS price_cents "
        "FROM return_items ri JOIN order_items oi ON oi.id = ri.order_item_id "
        "WHERE ri.return_id = $1",
        return_id,
    )


def claim_ops_monitor_run(run_date: str) -> bool:
    """Round 6 RACE-4: idempotency-guard для ops_monitor.

    Railway Cron при сетевом hiccup'е может ретраить запуск, или ручной запуск
    может пересечься с плановым — без guard'а дайджест разойдётся всем 2 раза.

    Возвращает True если этот вызов «застолбил» дату (первый за сегодня),
    False если уже запускался. `tasks/run_ops_monitor.main()` должен exit 0
    при False.

    Атомарный INSERT-if-absent по PRIMARY KEY. CREATE TABLE в init_db; run_date —
    'YYYY-MM-DD' строка (по local TZ через now_str()).
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        try:
            if USE_POSTGRES:
                cur.execute(
                    "INSERT INTO ops_monitor_runs (run_date, started_at) "
                    "VALUES (%s, %s) ON CONFLICT (run_date) DO NOTHING",
                    (run_date, now_str()),
                )
            else:
                cur.execute(
                    "INSERT OR IGNORE INTO ops_monitor_runs (run_date, started_at) "
                    "VALUES (?, ?)",
                    (run_date, now_str()),
                )
            claimed = cur.rowcount > 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return claimed




# ─── Роли ────────────────────────────────────────────────────────────────────

# Единый whitelist ролей (SECURITY.md C2 — раньше дублировался в database и
# handlers/users, рассинхрон давал silent-fail при назначении роли).
# IMPLEMENTATION.md §4.1: 6 ролей.
VALID_ROLES = ("admin", "boss", "bookkeeper", "warehouse_keeper", "manager", "guest")


def get_role(user_id: int) -> str:
    """
    Вернуть роль пользователя из БД. Если строки нет — возвращаем 'guest'
    (нулевые права). Это означает, что любая попытка вызвать handler с
    проверкой роли тут же отклонит непривилегированного пользователя.
    Раньше default был 'manager', что фактически открывало бота миру.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return "guest"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT role, deactivated_at FROM user_roles WHERE user_id = ?"), (uid,)
        )
        row = cur.fetchone()
    if not row:
        return "guest"
    deactivated = row["deactivated_at"] if USE_POSTGRES else row[1]
    if deactivated:
        # Деактивированный пользователь теряет все права (#32).
        return "guest"
    return row["role"] if USE_POSTGRES else row[0]


def get_role_and_deactivation(user_id: int) -> tuple[str, bool]:
    """Роль И флаг деактивации одним SELECT'ом — для общего кэша `services.roles`.

    Раньше это были два запроса и два кэша с РАЗНЫМ TTL (роль 60 с,
    деактивация 30 с), и понижение роли из бота доезжало до webapp позже,
    чем деактивация. Один запрос, один TTL — и роль, и блок применяются
    кросс-процессно за одно и то же окно.

    Возвращает ('guest', False) для отсутствующей строки: нет записи — нет
    прав, а деактивировать нечего.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return "guest", False
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT role, deactivated_at FROM user_roles WHERE user_id = ?"), (uid,)
        )
        row = cur.fetchone()
    if not row:
        return "guest", False
    role = row["role"] if USE_POSTGRES else row[0]
    deactivated = bool(row["deactivated_at"] if USE_POSTGRES else row[1])
    return (str(role or "guest"), deactivated)


def is_user_deactivated(user_id: int) -> bool:
    """Деактивирован ли пользователь — ПРЯМОЙ read из БД, минуя кэш ролей.

    R1: cached_role (TTL 60с) живёт в памяти каждого процесса отдельно — bot и
    webapp не делят кэш. Деактивация через бот не видна webapp до истечения TTL.
    Этот lookup по PK дешёвый и всегда свежий → зовётся в _authorize, чтобы
    деактивация применялась мгновенно во всех процессах. False при отсутствии
    строки (нет записи — нечего деактивировать; роль отдельно решит guest)."""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT deactivated_at FROM user_roles WHERE user_id = ?"), (uid,)
        )
        row = cur.fetchone()
    if not row:
        return False
    deactivated = row["deactivated_at"] if USE_POSTGRES else row[0]
    return bool(deactivated)


# Через сколько секунд застолблённый ключ БЕЗ результата считается брошенным.
# Только для операций, которые пишут результат ключа В СВОЕЙ транзакции
# (`idem_store_in`): у них «ключ есть, результата нет» после этого срока значит
# «транзакция не закоммитилась» — таймауты сессии пула (30 с на запрос, 10 с на
# ожидание замка) не дают ей жить так долго. У остальных результат пишется
# после коммита, и пустой ключ может скрывать уже проведённую операцию —
# их не переиспользуем никогда.
IDEM_RECLAIM_AFTER_S = 600


async def idem_claim(
    key: str, operation: str, user_id: int, *, reclaim_after_s: int | None = None
) -> dict | None:
    """DB-уровневая идемпотентность для денежных create-эндпоинтов (R2).

    Атомарно «застолбить» ключ: INSERT-if-absent. Возвращает:
      - None  → ключ НАШ (вставили), вызывающий выполняет операцию и затем
                зовёт idem_store(key, result);
      - dict  → ключ уже был: ранее сохранённый result (или {} если операция
                ещё в полёте/без результата) — вызывающий отдаёт его как ответ,
                операцию НЕ повторяет.

    In-memory _idem_cache в webapp не переживает рестарт и не делится между
    воркерами — отсюда дубль deposit/return. Этот claim в общей БД закрывает
    окно между ретраями клиента. Идемпотентно по PK (key)."""
    import json

    expires = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    rc = await adb_core.execute(
        "INSERT INTO idempotency_keys (key, operation, user_id, result, created_at, expires_at) "
        "VALUES ($1, $2, $3, NULL, $4, $5) ON CONFLICT (key) DO NOTHING",
        key, operation, user_id, now_str(), expires,
    )
    if rc > 0:
        return None  # ключ наш — выполняем операцию
    if reclaim_after_s is not None:
        # Ключ без результата старше срока — транзакция операции так и не
        # закоммитилась (результат пишется в ней же). Раньше такой ключ сутки
        # отвечал «уже обрабатывается», и повторить сдачу/оплату было нельзя.
        # CAS по created_at: из двух одновременных ретраев ключ получит один.
        cutoff = (datetime.now() - timedelta(seconds=reclaim_after_s)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        taken = await adb_core.execute(
            "UPDATE idempotency_keys SET created_at = $1, expires_at = $2 "
            "WHERE key = $3 AND result IS NULL AND created_at < $4",
            now_str(), expires, key, cutoff,
        )
        if taken > 0:
            return None
    row = await adb_core.fetchrow(
        "SELECT result FROM idempotency_keys WHERE key = $1", key
    )
    if row and row["result"]:
        try:
            return json.loads(row["result"])
        except (ValueError, TypeError):
            return {}
    return {}  # ключ занят, но результата ещё нет (операция в полёте)


async def idem_store(key: str, result: dict) -> None:
    """Сохранить результат операции под ранее застолблённым idem-ключом."""
    import json

    await adb_core.execute(
        "UPDATE idempotency_keys SET result = $1 WHERE key = $2",
        json.dumps(result), key,
    )


async def idem_store_in(txn, key: str | None, result: dict) -> None:
    """Записать результат под ключом ВНУТРИ транзакции самой операции.

    `idem_store` после коммита оставлял окно: операция проведена, а процесс
    умер до записи результата — ключ оставался пустым, и ретрай сутки получал
    «уже обрабатывается», не узнав, что деньги уже записаны. Результат в той же
    транзакции появляется ровно тогда же, когда и сама запись."""
    if not key:
        return
    import json

    await txn.execute(
        "UPDATE idempotency_keys SET result = $1 WHERE key = $2", json.dumps(result), key
    )


async def idem_release(key: str) -> None:
    """Освободить застолблённый ключ, если операция упала ДО idem_store —
    чтобы легитимный ретрай не получал 409 навсегда (до expiry). Удаляем только
    строки без результата (result IS NULL): успешный ключ не трогаем."""
    await adb_core.execute(
        "DELETE FROM idempotency_keys WHERE key = $1 AND result IS NULL", key
    )


def set_role(user_id: int, username: str, full_name: str, role: str) -> bool:
    valid_roles = VALID_ROLES
    if role not in valid_roles:
        return False
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO user_roles (user_id, username, full_name, role, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                -- T2.13 (§2.8): NULLIF+COALESCE. /addrole зовёт set_role с
                -- пустыми username/full_name (у админа их нет), и безусловный
                -- EXCLUDED затирал реальное имя — /users показывал голый ID
                -- до следующего /start пользователя.
                    username = COALESCE(NULLIF(EXCLUDED.username, ''), user_roles.username),
                    full_name = COALESCE(NULLIF(EXCLUDED.full_name, ''), user_roles.full_name),
                    role = EXCLUDED.role
            """,
                (user_id, username, full_name, role, now_str()),
            )
        else:
            cur.execute(
                """
                INSERT INTO user_roles (user_id, username, full_name, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                -- T2.13 (§2.8): NULLIF+COALESCE. /addrole зовёт set_role с
                -- пустыми username/full_name (у админа их нет), и безусловный
                -- EXCLUDED затирал реальное имя — /users показывал голый ID
                -- до следующего /start пользователя.
                    username = COALESCE(NULLIF(excluded.username, ''), user_roles.username),
                    full_name = COALESCE(NULLIF(excluded.full_name, ''), user_roles.full_name),
                    role = excluded.role
            """,
                (user_id, username, full_name, role, now_str()),
            )
        conn.commit()
    _invalidate_role_cache(user_id)
    return True


def get_all_users() -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("SELECT * FROM user_roles ORDER BY role, full_name")
        rows = cur.fetchall()
    return [dict(r) for r in rows]


async def deactivate_user(user_id: int, by: int) -> bool:
    """Деактивировать пользователя (#32): теряет все права (get_role → guest) и
    уведомления. Идемпотентно (повторно — False). Роль в user_roles.role
    сохраняется, чтобы восстановить при reactivate."""
    rc = await adb_core.execute(
        "UPDATE user_roles SET deactivated_at = $1, deactivated_by = $2 "
        "WHERE user_id = $3 AND deactivated_at IS NULL",
        now_str(), by, user_id,
    )
    _invalidate_role_cache(user_id)
    return rc > 0


async def reactivate_user(user_id: int, by: int) -> bool:
    """Снять деактивацию — роль восстанавливается из сохранённого user_roles.role."""
    rc = await adb_core.execute(
        "UPDATE user_roles SET deactivated_at = NULL, deactivated_by = NULL "
        "WHERE user_id = $1 AND deactivated_at IS NOT NULL",
        user_id,
    )
    _invalidate_role_cache(user_id)
    return rc > 0


async def get_user(user_id: int) -> dict | None:
    """asyncpg Stage 11 (#21): native async. Leaf — внутри database.py не
    вызывается (роль читается через services.roles.cached_role/get_role)."""
    return await adb_core.fetchrow("SELECT * FROM user_roles WHERE user_id = $1", user_id)


def ensure_user(user_id: int, username: str, full_name: str, admin_ids: list[int]):
    """
    Создать запись о пользователе если её нет; обновить имя/username если есть.

    Правила выбора роли для НОВЫХ пользователей:
      - В ADMIN_IDS  → admin
      - В BOSS_IDS   → boss
      - В ALLOWED_USERS (если этот env задан) → manager
      - Иначе → guest (нулевые права, админ повышает через /addrole)

    Опасный legacy-режим: LEGACY_OPEN_BOT=1 + ALLOWED_USERS пуст →
    любой новичок получает manager. Это эквивалент «открытый бот»
    и существует только для обратной совместимости со старыми
    развёртками. На продакшене НЕ включать.
    """
    from config import BOSS_IDS, ALLOWED_USERS, LEGACY_OPEN_BOT

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT role FROM user_roles WHERE user_id = ?"), (user_id,))
        row = cur.fetchone()

        if row:
            cur.execute(
                q("UPDATE user_roles SET username = ?, full_name = ? WHERE user_id = ?"),
                (username, full_name, user_id),
            )
            conn.commit()
            return

        if user_id in admin_ids:
            role = "admin"
        elif user_id in BOSS_IDS:
            role = "boss"
        elif ALLOWED_USERS and user_id in ALLOWED_USERS:
            role = "manager"
        elif LEGACY_OPEN_BOT and not ALLOWED_USERS:
            # Эта ветка только для legacy-развёрток. Громко логируем,
            # чтобы оператор увидел в Railway logs что бот открыт всему миру.
            logger.warning(
                "LEGACY_OPEN_BOT=1 + ALLOWED_USERS пуст: user_id=%s "
                "получил роль 'manager' автоматически. На проде смените "
                "поведение: убрать LEGACY_OPEN_BOT и/или заполнить ALLOWED_USERS.",
                user_id,
            )
            role = "manager"
        else:
            role = "guest"

        cur.execute(
            q(
                "INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, ?, ?, ?)"
            ),
            (user_id, username, full_name, role, now_str()),
        )
        conn.commit()
    _invalidate_role_cache(user_id)






# ─── Платежи ─────────────────────────────────────────────────────────────────


def add_payment(
    user_id: int,
    username: str,
    full_name: str,
    amount: float,
    currency: str,
    comment: str,
    order_id: int | None = None,
) -> int:
    """Создать запись о платеже. Если задан order_id — это «оплата по
    конкретному заказу» (частичная или полная); тогда после approve
    босса автоматически проверяется, не закрыт ли заказ полностью.
    Без order_id — самостоятельный платёж в кассу (legacy /pay flow)."""
    amount_cents = money.to_cents(amount)
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO payments
                    (user_id, username, full_name, amount_cents, currency, comment, status, order_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id
            """,
                (user_id, username, full_name, amount_cents, currency, comment, order_id, now_str()),
            )
            payment_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO payments
                    (user_id, username, full_name, amount_cents, currency, comment, status, order_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
                (user_id, username, full_name, amount_cents, currency, comment, order_id, now_str()),
            )
            payment_id = cur.lastrowid
        conn.commit()
    return payment_id


async def get_payments_for_order(order_id: int) -> list[dict]:
    """Все платежи привязанные к заказу (включая pending/rejected/archived).

    asyncpg Stage 19 (#21): native async (fetch)."""
    rows = await adb_core.fetch(
        "SELECT * FROM payments WHERE order_id = $1 ORDER BY created_at ASC", order_id
    )
    return _with_major(rows, ("amount", "amount_cents"))


async def get_payments_for_orders(order_ids: list[int]) -> dict[int, list[dict]]:
    """Батч-версия: {order_id: [payments...]}. Заказы без платежей в
    результат не попадают; вызывающий должен использовать .get(oid, []).

    Дедуплицируем order_ids — если caller передал список с повторами,
    placeholders разрастаются впустую и план запроса страдает.

    asyncpg Stage 12 (#21): native async; IN-список — $1..$N.
    """
    if not order_ids:
        return {}
    unique_ids = list(set(order_ids))
    placeholders = ", ".join(f"${i + 1}" for i in range(len(unique_ids)))
    rows = await adb_core.fetch(
        f"SELECT * FROM payments WHERE order_id IN ({placeholders}) ORDER BY created_at ASC",
        *unique_ids,
    )
    grouped: dict[int, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["order_id"], []).append(
            _with_major(r, ("amount", "amount_cents"))
        )
    return grouped


def _price_cents(item: dict) -> int:
    """Цена позиции заказа в копейках. Деньги хранятся только в копейках,
    поэтому это просто чтение колонки (0 для NULL)."""
    return int(item.get("price_cents") or 0)


def _amount_cents(payment: dict) -> int:
    """Сумма платежа в копейках."""
    return int(payment.get("amount_cents") or 0)


async def get_manager_performance(since_iso: str, until_iso: str) -> list[dict]:
    """Аналитика по менеджерам (boss) из ЛОКАЛЬНЫХ orders, GROUP BY user_id за
    период [since_iso, until_iso] по created_at. Надёжный источник (в отличие от
    МС-аналитики, где нет привязки к менеджеру). Без N+1 — батч items/payments/
    returns. Деньги — в мажорных единицах. Сортировка по выручке убыв.

    На менеджера: orders_count (создано), approved/shipped, revenue (по статусам
    shipped/paid/partially_returned/returned), debt (остаток по неоплаченным
    shipped/partially_returned), returns_count. Имя/роль — из user_roles."""
    # Запросы — диапазоном по orders.created_at и JOIN'ами к нему, а не
    # `SELECT *` + три `IN (все id периода)`. Главная босса зовёт это на каждое
    # открытие: полные строки заказов и списки id на тысячи параметров за
    # «год» упирались в память и в предел asyncpg (32 767 параметров), а
    # условие `created_at >= $1 AND created_at <= $2` ложится на индекс
    # orders(created_at, id). Нужные колонки — явно.
    period = "o.created_at >= $1 AND o.created_at <= $2 AND (o.ms_deleted_at IS NULL)"
    orders = await adb_core.fetch(
        "SELECT o.id, o.user_id, o.full_name, o.status, o.currency, o.fx_rate_to_base, "
        f"o.payment_confirmed FROM orders o WHERE {period}",
        since_iso, until_iso,
    )
    if not orders:
        return []
    items_by_order: dict[int, list[dict]] = {}
    for it in await adb_core.fetch(
        "SELECT oi.order_id, oi.quantity, oi.price_cents FROM order_items oi "
        f"JOIN orders o ON o.id = oi.order_id WHERE {period}",
        since_iso, until_iso,
    ):
        items_by_order.setdefault(int(it["order_id"]), []).append(it)
    # Подтверждённые платежи — суммой на заказ; учитываются только у долговых
    # статусов, поэтому фильтр статуса заказа — сразу в SQL.
    confirmed_by_order = {
        int(r["order_id"]): int(r["c"] or 0)
        for r in await adb_core.fetch(
            "SELECT p.order_id, COALESCE(SUM(p.amount_cents), 0) AS c FROM payments p "
            f"JOIN orders o ON o.id = p.order_id WHERE {period} "
            "AND o.status IN ('shipped', 'partially_returned') AND p.status = 'confirmed' "
            "GROUP BY p.order_id",
            since_iso, until_iso,
        )
    }
    returns_by_order = {
        int(r["order_id"]): int(r["c"])
        for r in await adb_core.fetch(
            "SELECT r.order_id, COUNT(*) AS c FROM returns r "
            f"JOIN orders o ON o.id = r.order_id WHERE {period} AND r.status = 'confirmed' "
            "GROUP BY r.order_id",
            since_iso, until_iso,
        )
    }

    revenue_statuses = {"shipped", "paid", "partially_returned", "returned"}
    debt_statuses = {"shipped", "partially_returned"}

    from config import BASE_CURRENCY

    base_cur = (BASE_CURRENCY or "USD").upper()

    agg: dict[int, dict] = {}
    for o in orders:
        uid = o["user_id"]
        m = agg.setdefault(
            uid,
            {
                "user_id": uid,
                "full_name": o.get("full_name") or str(uid),
                "orders_count": 0,
                "approved": 0,
                "shipped": 0,
                "revenue_cents": 0,
                "debt_cents": 0,
                # Выручка/долг РАЗДЕЛЬНО по валютам (не складываем разные валюты).
                "revenue_cents_cur": {},
                "debt_cents_cur": {},
                # Сводный итог в BASE_CURRENCY (по снимку курса заказа; fallback —
                # текущий курс). *_any: хоть что-то сконвертировано; *_missing:
                # часть валют без курса (итог приблизительный).
                "revenue_base_sum": 0.0,
                "revenue_base_any": False,
                "revenue_base_missing": False,
                "debt_base_sum": 0.0,
                "debt_base_any": False,
                "debt_base_missing": False,
                "returns_count": 0,
            },
        )
        status = o.get("status")
        # «Создано» — продуктивные заказы: отменённые/отклонённые не считаем
        # (иначе счётчик завышен на отменённые боссом/МС и отклонённые заявки).
        if status not in ("cancelled", "rejected"):
            m["orders_count"] += 1
        if status == "approved":
            m["approved"] += 1
        elif status == "shipped":
            m["shipped"] += 1
        ocur = (o.get("currency") or base_cur).upper()
        snap = o.get("fx_rate_to_base")
        items = items_by_order.get(o["id"], [])
        total_cents = sum(
            money.mul_qty(_price_cents(it), it.get("quantity", 0) or 0) for it in items
        )

        # m/ocur/snap связываем аргументами по умолчанию: замыкание внутри
        # цикла иначе смотрит на переменные ПОСЛЕДНЕЙ итерации (ruff B023).
        # Сейчас функция вызывается тут же, в своей итерации, и поведение
        # совпадает — но первый же отложенный вызов считал бы чужой заказ.
        def _accum_base(major: float, kind: str, *, m=m, ocur=ocur, snap=snap) -> None:
            # Снимок курса заказа, иначе текущий курс. None → валюта без курса.
            base_val = convert_to_base_at(major, ocur, snap)
            if base_val is None:
                base_val = convert_to_base(major, ocur)
            if base_val is None:
                m[f"{kind}_base_missing"] = True
            else:
                m[f"{kind}_base_sum"] += base_val
                m[f"{kind}_base_any"] = True

        if status in revenue_statuses:
            m["revenue_cents"] += total_cents
            m["revenue_cents_cur"][ocur] = m["revenue_cents_cur"].get(ocur, 0) + total_cents
            _accum_base(float(money.from_cents(total_cents)), "revenue")
        if status in debt_statuses and not o.get("payment_confirmed"):
            net = max(0, total_cents - confirmed_by_order.get(int(o["id"]), 0))
            m["debt_cents"] += net
            m["debt_cents_cur"][ocur] = m["debt_cents_cur"].get(ocur, 0) + net
            if net > 0:
                _accum_base(float(money.from_cents(net)), "debt")
        m["returns_count"] += returns_by_order.get(o["id"], 0)

    users = {u["user_id"]: u for u in await asyncio.to_thread(get_all_users)}
    result = []
    for uid, m in agg.items():
        u = users.get(uid)
        result.append(
            {
                "user_id": uid,
                "full_name": (u.get("full_name") if u else None) or m["full_name"],
                "role": (u.get("role") if u else None) or "—",
                "orders_count": m["orders_count"],
                "approved": m["approved"],
                "shipped": m["shipped"],
                "revenue": float(money.from_cents(m["revenue_cents"])),
                "debt": float(money.from_cents(m["debt_cents"])),
                "revenue_by_currency": [
                    {"currency": c, "amount": float(money.from_cents(v))}
                    for c, v in sorted(
                        m["revenue_cents_cur"].items(), key=lambda kv: kv[1], reverse=True
                    )
                ],
                "debt_by_currency": [
                    {"currency": c, "amount": float(money.from_cents(v))}
                    for c, v in sorted(
                        m["debt_cents_cur"].items(), key=lambda kv: kv[1], reverse=True
                    )
                ],
                "returns_count": m["returns_count"],
            }
        )
    result.sort(key=lambda x: x["revenue"], reverse=True)
    return result


# «Живой заказ» для денежных агрегатов: платёж учитываем, только если его заказа
# нет (standalone, order_id IS NULL) ИЛИ заказ не удалён ни в МС, ни локально.
# ЕДИНЫЙ источник для ВСЕХ денежных поверхностей (итог «Деньги», лента, касса,
# отчёты) — иначе бот и WebApp расходятся в сумме (платёж удалённого заказа
# виден в одной поверхности и скрыт в другой). Требует JOIN orders o ON o.id =
# <payments-alias>.order_id и алиас payments-таблицы.
_LIVE_ORDER_PAYMENT_JOIN = "LEFT JOIN orders o ON o.id = {p}.order_id"
_LIVE_ORDER_PAYMENT_FILTER = (
    "(o.id IS NULL OR o.ms_deleted_at IS NULL)"
)



async def get_order_payment_summary(order_id: int) -> dict:
    """Сумма по заказу: total / confirmed / pending / remaining.

    Используется для отображения «оплачено X из Y, остаток Z» и для
    решения, закрыт ли заказ (remaining == 0).

    Дополнительно (PR #42): `*_base` поля — пересчёт в BASE_CURRENCY через
    `convert_to_base`. None если курс валюты заказа не задан в админке;
    UI может в этом случае показать «—» вместо враного «$0.00».

    asyncpg Stage 19 (#21): native async; get_order/get_order_items/
    get_payments_for_order теперь async (await). convert_to_base — кэшированный
    read-хелпер (категория get_setting), зовём прямым sync-вызовом.
    """
    from config import BASE_CURRENCY

    from services.debts import calc_order_balance

    order = await get_order(order_id)
    if not order:
        return {"total": 0.0, "confirmed": 0.0, "pending": 0.0, "remaining": 0.0}
    # T2.1: остаток считает services.debts — единственный источник истины.
    # Раньше здесь была своя формула БЕЗ сдач наличных, поэтому заказ, закрытый
    # сдачей, показывал долг в карточке и в пуше боссу (§2.10).
    bal = await calc_order_balance(order_id)
    total_cents = bal.total_cents
    confirmed_cents = bal.confirmed_cents
    pending_cents = bal.pending_cents
    returns_cents = bal.returns_cents
    deposits_cents = bal.deposits_cents
    remaining_cents = bal.remaining_cents

    total = float(money.from_cents(total_cents))
    confirmed = float(money.from_cents(confirmed_cents))
    pending = float(money.from_cents(pending_cents))
    remaining = float(money.from_cents(remaining_cents))
    returns_amount = float(money.from_cents(returns_cents))

    currency = (order.get("currency") or BASE_CURRENCY).upper()
    return {
        "total": total,
        "confirmed": confirmed,
        "pending": pending,
        "remaining": remaining,
        "returns": returns_amount,
        "deposits": float(money.from_cents(deposits_cents)),
        "total_cents": total_cents,
        "confirmed_cents": confirmed_cents,
        "pending_cents": pending_cents,
        "remaining_cents": remaining_cents,
        "returns_cents": returns_cents,
        "deposits_cents": deposits_cents,
        "currency": currency,
        # *_base — None если convert_to_base не смог (курс не задан админом).
        "total_base": convert_to_base(total, currency),
        "confirmed_base": convert_to_base(confirmed, currency),
        "pending_base": convert_to_base(pending, currency),
        "remaining_base": convert_to_base(remaining, currency),
        "base_currency": (BASE_CURRENCY or "USD").upper(),
    }








def prune_idempotency_keys() -> int:
    """Удалить протухшие ключи идемпотентности (expires_at < сейчас).

    T2.13 (§3.5): expires_at писался, но НИКОГДА не читался — таблица росла
    вечно. Порог считаем в Python и передаём параметром: created_at/expires_at
    пишутся в локальной TZ через now_str(), а SQL-функции текущего времени
    (NOW() / datetime('now')) отдают UTC — лексикографическое сравнение строк
    в разных TZ молча всегда даёт False (CLAUDE.md).
    """
    # У таблицы PK — `key` (TEXT), поэтому _batched_delete (он ходит по `id`)
    # тут не подходит; порции режем по самому ключу.
    cutoff = now_str()
    total = 0
    with get_conn() as conn:
        cur = get_cursor(conn)
        while True:
            cur.execute(
                q(
                    "DELETE FROM idempotency_keys WHERE key IN ("
                    "SELECT key FROM idempotency_keys "
                    "WHERE expires_at IS NOT NULL AND expires_at < ? "
                    "ORDER BY key LIMIT ?)"
                ),
                (cutoff, 5000),
            )
            n = cur.rowcount or 0
            conn.commit()
            total += n
            if n <= 0:
                break
    return total


def prune_audit_log(retention_months: int = 6) -> int:
    """Удалить записи аудита старше retention_months (janitor, §13). Месяц ≈ 30
    дней (для janitor-задачи достаточно). Возвращает число удалённых строк."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(days=retention_months * 30)).strftime("%Y-%m-%d %H:%M:%S")
    return _batched_delete("audit_log", "created_at < ?", (cutoff,))


def _batched_delete(table: str, where: str, params: tuple, batch: int = 5000) -> int:
    """Удалять строки порциями (L2): один большой DELETE держит длинный лок на
    проде. Работает в SQLite и Postgres через DELETE ... WHERE id IN (SELECT ...
    LIMIT). Коммит после каждой порции. Возвращает число удалённых."""
    total = 0
    with get_conn() as conn:
        cur = get_cursor(conn)
        while True:
            cur.execute(
                q(
                    f"DELETE FROM {table} WHERE id IN ("
                    f"SELECT id FROM {table} WHERE {where} ORDER BY id LIMIT ?)"
                ),
                (*params, batch),
            )
            n = cur.rowcount or 0
            conn.commit()
            total += n
            if n <= 0:
                break
    return total


















async def get_order_total_cents(order_id: int) -> int:
    """Сумма заказа в минорных единицах (копейках) из локальных order_items.
    Точный аналог `total_cents` из get_order_payment_summary — для сверки с
    документом МойСклад (поле `sum`)."""
    val = await adb_core.fetchval(
        f"SELECT {_SUM_ORDER_TOTAL_CENTS} FROM order_items WHERE order_id = $1",
        order_id,
    )
    return int(val or 0)


async def set_order_credit_override(order_id: int, by: int) -> bool:
    """Отметить, что заказ одобрен боссом с превышением кредитного лимита
    (override). Снимает повторную проверку при будущих одобрениях этого заказа."""
    return (
        await adb_core.execute(
            "UPDATE orders SET credit_limit_override = 1, credit_limit_override_by = $1, "
            "updated_at = $2 WHERE id = $3",
            by, now_str(), order_id,
        )
        > 0
    )








def get_pool_stats() -> dict:
    """Снимок состояния Postgres-пула: used/free/min/max/util_pct.

    Возвращает пустой dict если используется SQLite или пул не
    инициализирован. Лезет в private-атрибуты psycopg2.pool —
    публичного API нет, но интерфейс стабилен между версиями 2.x.

    Используется shipment_notifier loop'ом для периодического
    логирования; если util_pct устойчиво > 80% — pg max'ы пора
    поднимать или искать утечку коннектов.
    """
    if not USE_POSTGRES:
        return {}
    if _pg_connection_pool is None:
        return {"used": 0, "free": 0, "min": _PG_POOL_MIN, "max": _PG_POOL_MAX, "util_pct": 0.0}
    pool = _pg_connection_pool
    # ThreadedConnectionPool._used — dict(key→conn) для checked-out,
    # ._pool — list для свободных. .minconn / .maxconn — public.
    try:
        used = len(getattr(pool, "_used", {}))
        free = len(getattr(pool, "_pool", []))
        maxconn = int(getattr(pool, "maxconn", _PG_POOL_MAX))
        minconn = int(getattr(pool, "minconn", _PG_POOL_MIN))
        util = (used / maxconn * 100.0) if maxconn > 0 else 0.0
        return {
            "used": used,
            "free": free,
            "min": minconn,
            "max": maxconn,
            "util_pct": round(util, 1),
        }
    except Exception:
        return {}


def record_cron_run(
    task_name: str,
    status: str,
    started_at: str,
    finished_at: str,
    duration_ms: int,
    error_message: str | None = None,
) -> None:
    """Записать факт запуска cron-задачи в `cron_runs`.

    Вызывается обёрткой `run_cron_main` из tasks/run_*.py — каждая
    cron-CLI пишет финальный статус ровно один раз. INSERT-only:
    история запусков (cleanup отдельным cron'ом в будущем).

    `status`: 'ok' | 'failed'. Сейчас не используем 'running' — пишем
    в конце с известным финальным состоянием. Если процесс убит
    SIGTERM до finally — запись не появится, и ops_monitor увидит
    «stale» (этого мы и хотим).
    """
    if not task_name:
        return
    err = (error_message or "")[:2000] if error_message else None
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "INSERT INTO cron_runs "
                "(task_name, started_at, finished_at, status, error_message, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (
                task_name[:100],
                started_at,
                finished_at,
                status[:32],
                err,
                str(duration_ms),  # храним в metadata как строку; отдельной колонки нет
            ),
        )
        conn.commit()


async def get_last_cron_runs() -> list[dict]:
    """Вернуть по одной записи на task — последнюю по started_at.

    Используется в ops_monitor для алерта «cron не запускался».
    SQL: SELECT DISTINCT ON (task_name) для Postgres, GROUP BY + JOIN
    для SQLite (DISTINCT ON только pg-only).

    asyncpg Stage 13 (#21): native async через adb_core.
    """
    if USE_POSTGRES:
        return await adb_core.fetch(
            "SELECT DISTINCT ON (task_name) task_name, started_at, finished_at, "
            "status, error_message, metadata "
            "FROM cron_runs "
            "ORDER BY task_name, started_at DESC"
        )
    # SQLite: max(started_at) per task через GROUP BY с трюком
    # «max-of-tuple» (max(started_at || '|' || id) даёт стабильную
    # выборку даже при совпадении started_at).
    return await adb_core.fetch(
        "SELECT cr.task_name, cr.started_at, cr.finished_at, cr.status, "
        "cr.error_message, cr.metadata "
        "FROM cron_runs cr "
        "INNER JOIN ("
        "  SELECT task_name, MAX(id) AS max_id "
        "  FROM cron_runs GROUP BY task_name"
        ") last ON cr.task_name = last.task_name AND cr.id = last.max_id "
        "ORDER BY cr.task_name"
    )


async def get_stale_crons(thresholds_hours: dict[str, float]) -> list[dict]:
    """Вернуть cron'ы, чей последний УСПЕШНЫЙ запуск старше порога.

    `thresholds_hours`: {'ms_sync_retry': 1.0, 'ops_monitor': 26.0, ...}
    Порог в часах. Если task'и нет в выдаче `get_last_cron_runs` —
    включаем в результат с last_success=None (значит, ни разу не было).

    Возвращаем: [{task_name, last_success_at, hours_ago, threshold_hours,
                  last_status, last_error}]

    asyncpg Stage 13 (#21): native async — get_last_cron_runs тоже async (await).
    """
    now = datetime.now()
    rows = await get_last_cron_runs()
    # last successful run per task: если last = failed, надо искать
    # предыдущий ok'ный. Для простоты: считаем последний run; если он
    # failed — отмечаем `last_status='failed'`, hours_ago = от него
    # (а не от последнего ok). Это даёт алерт и при «давно failed».
    by_task = {r["task_name"]: r for r in rows}
    stale: list[dict] = []
    for task_name, threshold_h in thresholds_hours.items():
        last = by_task.get(task_name)
        if last is None:
            stale.append(
                {
                    "task_name": task_name,
                    "last_success_at": None,
                    "hours_ago": None,
                    "threshold_hours": threshold_h,
                    "last_status": None,
                    "last_error": None,
                }
            )
            continue
        try:
            last_at = datetime.strptime(last["started_at"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        hours_ago = (now - last_at).total_seconds() / 3600
        # Алертим: либо stale (давно), либо последний run failed.
        if hours_ago > threshold_h or last.get("status") != "ok":
            stale.append(
                {
                    "task_name": task_name,
                    "last_success_at": last["started_at"],
                    "hours_ago": round(hours_ago, 1),
                    "threshold_hours": threshold_h,
                    "last_status": last.get("status"),
                    "last_error": last.get("error_message"),
                }
            )
    return stale








def _reconcile_window() -> tuple[str, int]:
    """(порог даты, LIMIT) для cron-реконсиляции МС — из env, с дефолтами."""
    from datetime import timedelta

    try:
        days = int(os.environ.get("MS_RECONCILE_WINDOW_DAYS", "30"))
    except ValueError:
        days = 30
    try:
        limit = int(os.environ.get("MS_RECONCILE_LIMIT", "500"))
    except ValueError:
        limit = 500
    cutoff = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S")
    return cutoff, max(1, limit)
















async def count_boss_attention() -> dict[str, int]:
    """Счётчики для блока «Требует внимания» на главной — одними COUNT(*).

    T2.13 (§3.8): /api/home делал четыре полных `SELECT *`
    (get_pending_cash_deposits, get_pending_returns,
    get_paid_orders_awaiting_confirmation, get_open_debts) ТОЛЬКО ради len().
    Строки сериализовались из БД и выбрасывались; при rate-limit 120/мин на
    эндпоинте это заметная нагрузка, растущая с размером базы.
    """
    deposits = await adb_core.fetchval(
        "SELECT COUNT(*) FROM cash_deposits WHERE status = 'pending'"
    )
    returns_ = await adb_core.fetchval(
        "SELECT COUNT(*) FROM returns WHERE status = 'pending'"
    )
    # Зеркалит get_paid_orders_awaiting_confirmation: ТОЛЬКО payment_type='paid'
    # в approved/shipped (credit-долги считаются отдельно, в debts) и без
    # фантомов. Расхождение фильтров здесь давало бы боссу счётчик, не
    # совпадающий с содержимым таба «Платежи».
    payments = await adb_core.fetchval(
        "SELECT COUNT(*) FROM orders o "
        "WHERE o.payment_type = 'paid' "
        "AND o.status IN ('approved', 'shipped') "
        "AND (o.ms_deleted_at IS NULL) "
        # Ждёт решения только то, что подтверждающий может проверить: карта,
        # перечисление или старый платёж без способа. Наличные на руках у
        # менеджера подтверждаются сдачей в кассу — кнопки под ними нет.
        "AND EXISTS (SELECT 1 FROM payments p "
        "            WHERE p.order_id = o.id AND p.status = 'pending' "
        "            AND NOT EXISTS (SELECT 1 FROM payment_parts pp "
        "                            WHERE pp.payment_id = p.id AND pp.method = 'cash'))"
    )
    # Зеркалит get_open_debts — фильтр один (_OPEN_DEBT_FILTER).
    debts = await adb_core.fetchval(f"SELECT COUNT(*) FROM orders WHERE {_OPEN_DEBT_FILTER}")
    return {
        "deposits": int(deposits or 0),
        "returns": int(returns_ or 0),
        "payments": int(payments or 0),
        "debts": int(debts or 0),
    }






async def delete_order(order_id: int, requested_by: int) -> bool:
    """Удалить заказ-черновик. Только status='draft' можно удалять, и
    только владелец (даже boss/admin не удаляет чужие — нужно жёстче
    через audit). Каскадно сносим order_items.

    Возвращает True если что-то удалили, False если заказ не найден,
    не draft, или не принадлежит requested_by.

    asyncpg Stage 16 (#21): native async. Pre-check + каскадный DELETE — в одной
    adb_core.transaction() (как раньше один conn); ранний return — до удалений.
    add_audit_log/get_role (sync) — мост через to_thread.
    """
    deleted = False
    async with adb_core.transaction() as txn:
        # Проверяем, что заказ существует, draft и принадлежит юзеру — ПОД
        # замком строки. Без него проверка и удаление расходились: сабмит
        # (draft→pending, тоже FOR UPDATE) успевал между ними, и удалялся уже
        # отправленный заказ вместе с позициями, а pending-заявка оставалась
        # сиротой в очереди босса.
        lock = " FOR UPDATE" if USE_POSTGRES else ""
        row = await txn.fetchrow(f"SELECT user_id, status FROM orders WHERE id = $1{lock}", order_id)
        if not row:
            return False
        if row["user_id"] != requested_by or row["status"] != "draft":
            return False
        # Не удаляем заказ, по которому есть платежи (WP-09): payments не входят в
        # каскад (в SQLite FK выкл.), и orphan-платёж потом подтвердился бы в кассу/
        # МС против несуществующего заказа. Такой draft нужно разобрать вручную.
        if await txn.fetchval("SELECT 1 FROM payments WHERE order_id = $1 LIMIT 1", order_id):
            return False

        # Каскад вручную, чтобы работало и в SQLite (FK off by default).
        # Связь с номенклатурой ссылается на позицию — её первой, иначе на
        # Postgres DELETE по order_items отвергнется живым FK.
        await txn.execute("DELETE FROM order_item_products WHERE order_id = $1", order_id)
        await txn.execute("DELETE FROM order_items WHERE order_id = $1", order_id)
        # Условие статуса повторено в самом DELETE — последний рубеж.
        deleted = (
            await txn.execute(
                "DELETE FROM orders WHERE id = $1 AND status = 'draft' AND user_id = $2",
                order_id, requested_by,
            )
            > 0
        )
    if deleted:
        await asyncio.to_thread(
            add_audit_log,
            requested_by,
            "",
            await asyncio.to_thread(get_role, requested_by),
            "order_deleted",
            f"Удалён черновик заказа #{order_id}",
        )
    return deleted


async def confirm_payment(
    payment_id: int, confirmed_by: int | None = None, confirmed_name: str = ""
) -> bool:
    """Подтвердить платёж. Если платёж привязан к заказу (order_id) —
    проверяем суммарно, не закрыли ли мы тем самым заказ полностью, и если да —
    проставляем order.paid_confirmed_at.

    ВСЁ — одной транзакцией: CAS статуса платежа, снимок курса и закрытие
    заказа. Раньше это были три коммита: платёж становился `confirmed`, а сбой
    до закрытия оставлял покрытый заказ открытым НАВСЕГДА — повторное
    подтверждение отвечало «уже подтверждён» (CAS не проходил), и другого пути
    закрыть заказ не было. Снимок курса тем же манером мог не записаться, и
    итог «в долларах» по такому платежу плыл с курсом.

    Замки: сначала строка заказа (services.debts.lock_orders — общий замок на
    «заявлено по заказу», в том же порядке «заказ → платёж», что и сторно
    в accounting), потом CAS платежа. Аудит — после коммита: его пишет
    синхронный слой, и на SQLite он ждал бы нашу же пишущую транзакцию.
    """
    from services.debts import lock_orders

    head = await adb_core.fetchrow(
        "SELECT order_id, currency, fx_rate_to_base, user_id FROM payments WHERE id = $1", payment_id
    )
    if head is None:
        return False
    # Наличная строка разбивки подтверждается ТОЛЬКО сдачей в кассу
    # (confirm_cash_deposit): «деньги у менеджера на руках» ещё не в кассе, и
    # кнопка «Принять» под ними закрыла бы заказ, а наличные так и не дошли бы.
    from services import order_payments

    if await order_payments.payment_method(payment_id) == "cash":
        return False
    # Сервисный рубеж прав (HTTP, pay_ok, пачка по заказу): свои деньги и деньги
    # при живом руководителе менеджер не подтверждает → PaymentError(403).
    rights = None
    if confirmed_by is not None:
        rights = await order_payments.require_confirm_rights(confirmed_by, [head.get("user_id")])
    # Курс — ДО транзакции: get_currency_rate синхронный (кэш + чтение БД), и
    # внутри пишущей транзакции SQLite он ждал бы её же.
    rate = None
    if head.get("fx_rate_to_base") is None and head.get("currency"):
        rate = await asyncio.to_thread(get_currency_rate, head["currency"])

    closed_order: int | None = None
    confirmed_cents = 0
    async with adb_core.transaction() as txn:
        order_id = head.get("order_id")
        if order_id:
            await lock_orders(txn, [int(order_id)])
            # Деньги по отменённой/отклонённой продаже не засчитываются: платёж
            # такого заказа — ошибка, а не поступление (cancel_order их снимает;
            # здесь — рубеж для старых строк и гонки «отменить против подтвердить»).
            st = await txn.fetchval("SELECT status FROM orders WHERE id = $1", int(order_id))
            if st in ("cancelled", "rejected", "draft"):
                return False
        rc = await txn.execute(
            "UPDATE payments SET status = 'confirmed', confirmed_at = $1 "
            "WHERE id = $2 AND status = 'pending'",
            now_str(), payment_id,
        )
        if rc <= 0:
            return False
        if rate is not None:
            await txn.execute(
                "UPDATE payments SET fx_rate_to_base = $1 WHERE id = $2 AND fx_rate_to_base IS NULL",
                float(rate), payment_id,
            )
        # Привязку могли поменять между чтением и замком (link_payment_to_order
        # ставит order_id платежу без заказа) — берём фактическую.
        actual = await txn.fetchval("SELECT order_id FROM payments WHERE id = $1", payment_id)
        if actual and actual != order_id:
            await lock_orders(txn, [int(actual)])
        if actual:
            closed, confirmed_cents = await _close_order_if_covered_locked(
                txn, int(actual), confirmed_by, confirmed_name
            )
            closed_order = int(actual) if closed else None

    payment = await get_payment(payment_id)
    if confirmed_by and payment:
        await asyncio.to_thread(
            add_audit_log,
            confirmed_by,
            confirmed_name,
            await asyncio.to_thread(get_role, confirmed_by),
            "payment_confirmed",
            f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}"
            + (f" ({rights['note']})" if rights and rights.get("note") else ""),
        )
    if closed_order is not None:
        await _audit_order_fully_paid(closed_order, confirmed_by, confirmed_name, confirmed_cents)
    return True


async def link_payment_to_order(
    payment_id: int,
    order_id: int,
    linked_by: int,
    linked_name: str = "",
) -> dict:
    """Ретроспективно привязать стендалон-платёж к заказу.

    Use case: бухгалтер/босс видит /pay-платёж в кассе без order_id
    («бытовой» по умолчанию), но понимает что это была частичная оплата
    конкретного заказа. До этой функции — только удалить и пересоздать
    с order_id, теряя audit-trail.

    Семантика:
      * атомарный UPDATE WHERE order_id IS NULL — два параллельных линка
        к разным заказам: только первый выигрывает, второй вернёт ok=False
      * заказ должен существовать, иначе FK-семантика ломается
      * если платёж уже confirmed — после link триггерим _maybe_close_order
        (мог сразу закрыть заказ) + _trigger_ms_paymentin_sync (создать
        paymentin в МС, который не создавался без order'а)
      * audit-log запись о ретроспективной привязке

    Возвращает {ok, error?, ms_sync_triggered?, order_closed?}.

    asyncpg Stage 15 (#21): native async. Pre-check reads (get_payment/get_order)
    и audit/get_role (sync money-core) — мост через to_thread; атомарный
    UPDATE-WHERE-NULL — adb_core.execute; _maybe_close теперь async (await).
    """
    if not payment_id or not order_id:
        return {"ok": False, "error": "payment_id и order_id обязательны"}

    payment = await get_payment(payment_id)
    if not payment:
        return {"ok": False, "error": "Платёж не найден"}
    if payment.get("order_id"):
        return {
            "ok": False,
            "error": f"Платёж уже привязан к заказу #{payment['order_id']}",
        }
    order = await get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    # Валюта платежа должна совпадать с валютой заказа: закрытие заказа считает
    # копейки в валюте заказа, кросс-валютная привязка без конверсии ложно
    # закрывала заказ (напр. UZS-платёж к USD-заказу). Конверсии при закрытии
    # нет — поэтому требуем совпадение валют (WP-04).
    from config import BASE_CURRENCY

    pay_cur = (payment.get("currency") or BASE_CURRENCY or "USD").upper()
    ord_cur = (order.get("currency") or BASE_CURRENCY or "USD").upper()
    if pay_cur != ord_cur:
        return {
            "ok": False,
            "error": f"Валюта платежа ({pay_cur}) не совпадает с валютой заказа ({ord_cur})",
        }

    # Атомарно линкуем — защита от race: два параллельных линка к РАЗНЫМ
    # заказам, оба прошли проверки выше; UPDATE-WHERE-NULL выиграет только
    # один. WHERE id = ? обязательно дополняет, иначе при concurrent
    # link'е разных платежей к разным заказам мы случайно обновим не тот.
    #
    # Привязка меняет «заявлено по заказу» — поэтому под общим замком строк
    # заказа (`debts.lock_orders`) и со сверкой `calc_claimable_cents` в той же
    # транзакции: иначе платёж ложился поверх уже заявленных разбивкой/сдачей
    # денег, и одни деньги засчитывались по заказу дважды.
    from services.debts import calc_claimable_cents, lock_orders

    refusal: str | None = None
    async with adb_core.transaction() as txn:
        await lock_orders(txn, [int(order_id)])
        st = await txn.fetchval("SELECT status FROM orders WHERE id = $1", int(order_id))
        fresh_pay = await txn.fetchrow(
            "SELECT status, amount_cents, order_id FROM payments WHERE id = $1", payment_id
        )
        rc = 0
        if st in ("cancelled", "rejected"):
            refusal = f"Заказ #{order_id} отменён — деньги к нему не привязываются"
        elif fresh_pay is not None and fresh_pay["order_id"] is None and fresh_pay["status"] in (
            "pending", "confirmed"
        ):
            claimable = (await calc_claimable_cents([int(order_id)], conn=txn)).get(int(order_id), 0)
            if int(fresh_pay["amount_cents"] or 0) > claimable:
                refusal = (
                    f"По заказу #{order_id} можно привязать не больше "
                    f"{money.format_cents(claimable)} {ord_cur}: остальное уже оплачено "
                    "или ждёт подтверждения"
                )
        if refusal is None:
            rc = await txn.execute(
                "UPDATE payments SET order_id = $1 WHERE id = $2 AND order_id IS NULL",
                order_id,
                payment_id,
            )
    if refusal is not None:
        return {"ok": False, "error": refusal}
    if rc <= 0:
        # Кто-то между нашими read и UPDATE прилинковал другой заказ.
        # Перечитываем чтобы вернуть оператору актуальную картину.
        fresh = await get_payment(payment_id)
        return {
            "ok": False,
            "error": f"Параллельная привязка: платёж уже принадлежит заказу #{fresh.get('order_id') if fresh else '?'}",
        }

    await asyncio.to_thread(
        add_audit_log,
        linked_by,
        linked_name or "",
        await asyncio.to_thread(get_role, linked_by) or "",
        "payment_linked_to_order",
        f"Платёж #{payment_id} ({payment['amount']:,.2f} {payment.get('currency', 'USD')}) "
        f"привязан к заказу #{order_id} (был стендалон)",
    )

    result: dict = {"ok": True, "ms_sync_triggered": False, "order_closed": False}

    # Если платёж уже был подтверждён до линка — теперь это credit на заказ,
    # надо проверить не закрыли ли мы его и создать paymentin в МС.
    if payment.get("status") == "confirmed":
        await _maybe_close_order_after_payment(order_id, linked_by, linked_name)
        # Проверяем статус заказа после _maybe_close.
        updated_order = await get_order(order_id)
        if updated_order and updated_order.get("paid_confirmed_at"):
            result["order_closed"] = True
        result["ms_sync_triggered"] = True

    return result


async def get_unlinked_payments(limit: int = 100) -> list[dict]:
    """Confirmed-платежи без order_id — кандидаты для ретроспективного link'а.

    Только confirmed: pending-платежи в UI и так видны в общем списке,
    rejected/cancelled — не интересно для link'а.

    Возвращает свежие первыми (для UI «недавно подтверждённое сначала»).

    asyncpg-миграция Stage 2 (задача #21): нативный async через adb_core.
    Вызовы: webapp через `await adb.…`; sync-тесты обновлены на asyncio.run.
    """
    if limit <= 0:
        limit = 100
    rows = await adb_core.fetch(
        "SELECT id, user_id, username, full_name, amount_cents, currency, "
        "comment, confirmed_at, status "
        "FROM payments "
        "WHERE order_id IS NULL AND status = 'confirmed' "
        "ORDER BY id DESC LIMIT $1",
        int(limit),
    )
    return _with_major(rows, ("amount", "amount_cents"))


async def _close_order_if_covered_locked(
    txn, order_id: int, confirmed_by: int | None, confirmed_name: str
) -> tuple[bool, int]:
    """Закрыть заказ, если он покрыт, — ВНУТРИ транзакции под замком заказа.

    Вызывающий обязан уже держать строку заказа (`services.debts.lock_orders`):
    пересчёт и закрытие идут под тем же замком, иначе параллельное
    подтверждение двух платежей одного заказа видело бы `remaining > 0` у обоих
    и оба пропускали закрытие. → (закрыт сейчас, подтверждённые копейки).
    """
    from services.debts import calc_order_balance

    row = await txn.fetchrow("SELECT paid_confirmed_at FROM orders WHERE id = $1", order_id)
    if not row or row["paid_confirmed_at"] is not None:
        return False, 0
    # T2.1: формула — из services.debts, а не своя копия. Она учитывает
    # платежи ТОЛЬКО в валюте заказа (иначе платёж в «дешёвой» валюте вроде
    # UZS в копейках ложно перекрывал USD-заказ, WP-04) и подтверждённые
    # сдачи наличных (иначе заказ, оплаченный наполовину сдачей, не
    # закрывался, WP-05).
    bal = await calc_order_balance(order_id, conn=txn)
    # net <= 0 (напр. полный возврат) — это не «оплата», закрытием здесь не
    # занимаемся: статус ведёт confirm_return.
    if bal.total_cents - bal.returns_cents <= 0 or bal.remaining_cents > 0:
        return False, bal.confirmed_cents
    rc = await txn.execute(
        "UPDATE orders "
        "SET paid_confirmed_at = $1, paid_confirmed_by = $2, "
        "    paid_confirmed_by_name = $3, "
        "    paid_at = COALESCE(paid_at, $4), "
        "    updated_at = $5 "
        "WHERE id = $6 AND paid_confirmed_at IS NULL",
        now_str(),
        confirmed_by or 0,
        confirmed_name or "",
        now_str(),
        now_str(),
        order_id,
    )
    return rc > 0, bal.confirmed_cents


async def _audit_order_fully_paid(
    order_id: int, confirmed_by: int | None, confirmed_name: str, confirmed_cents: int
) -> None:
    role = await asyncio.to_thread(get_role, confirmed_by) if confirmed_by else ""
    await asyncio.to_thread(
        add_audit_log,
        confirmed_by or 0,
        confirmed_name,
        role,
        "order_fully_paid",
        f"Заказ #{order_id} полностью оплачен "
        f"(сумма подтверждённых платежей: {money.format_cents(confirmed_cents)})",
    )


async def _maybe_close_order_after_payment(
    order_id: int,
    confirmed_by: int | None,
    confirmed_name: str,
) -> None:
    """Отдельной транзакцией: взять замок заказа, закрыть его, если покрыт.

    Для путей, где деньги уже зачтены раньше (привязка подтверждённого платежа
    к заказу). Подтверждение платежа закрывает заказ в СВОЕЙ транзакции —
    `confirm_payment`.
    """
    from services.debts import lock_orders

    async with adb_core.transaction() as txn:
        await lock_orders(txn, [order_id])
        closed, confirmed_cents = await _close_order_if_covered_locked(
            txn, order_id, confirmed_by, confirmed_name
        )
    if closed:
        await _audit_order_fully_paid(order_id, confirmed_by, confirmed_name, confirmed_cents)


async def reject_payment(
    payment_id: int, rejected_by: int | None = None, rejected_name: str = ""
) -> bool:
    """Отклонить ожидающий платёж. Замки: строка заказа (`lock_orders`) →
    проверка живой сдачи → CAS платежа, одной транзакцией. Аудит — после коммита."""
    # Наличные, уже лежащие в сдаче, отклоняются вместе со сдачей — иначе
    # подтверждённая потом сдача несла бы деньги отклонённого платежа.
    from services import order_payments
    from services.debts import lock_orders

    head = await adb_core.fetchrow("SELECT order_id FROM payments WHERE id = $1", payment_id)
    if head is None:
        return False
    # Проверка «наличные уже в сдаче» и CAS — ОДНОЙ транзакцией под замком
    # заказа (общий с create_cash_deposit): иначе сдача, созданная между
    # проверкой и UPDATE, уносила деньги отклонённого платежа.
    async with adb_core.transaction() as txn:
        order_id = head.get("order_id")
        if order_id:
            await lock_orders(txn, [int(order_id)])
        actual = await txn.fetchval("SELECT order_id FROM payments WHERE id = $1", payment_id)
        if actual and actual != order_id:
            await lock_orders(txn, [int(actual)])
        if await order_payments.payment_in_active_deposit(payment_id, conn=txn):
            return False
        rc = await txn.execute(
            "UPDATE payments SET status = 'rejected' WHERE id = $1 AND status = 'pending'",
            payment_id,
        )
    updated = rc > 0
    if updated and rejected_by:
        payment = await get_payment(payment_id)
        if payment:
            await asyncio.to_thread(
                add_audit_log,
                rejected_by,
                rejected_name,
                await asyncio.to_thread(get_role, rejected_by),
                "payment_rejected",
                f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}",
            )
    return updated


async def get_payment(payment_id: int) -> dict | None:
    """asyncpg Stage 19 (#21): native async (fetchrow). Денежное ядро (confirm/
    link/reject/archive_payment) уже async и зовёт это через await."""
    row = await adb_core.fetchrow("SELECT * FROM payments WHERE id = $1", payment_id)
    return _with_major(row, ("amount", "amount_cents"))


async def get_cash_history(
    limit: int = 80, since: str | None = None, until: str | None = None
) -> list[dict]:
    """Единая лента движения денег для босса: платежи + сдачи наличных +
    возвраты, в одном списке, новые сверху. Каждая запись: kind
    (payment|deposit|return), сумма, валюта, статус, кто (имя), когда, заказ.

    since/until (WP-11): период по COALESCE(confirmed_at, created_at) — как в
    итоге «Деньги» (get_money_totals), чтобы лента и итог были за один период.

    «Кто» резолвим по user_id через get_all_users (payments.user_id /
    cash_deposits.manager_id / returns.created_by). Деньги форматируются на
    фронте; здесь отдаём amount как есть (float — только для отображения, не
    для расчётов; денежное ядро остаётся на cents в своих функциях).
    """

    def _period(conf: str, created: str, params: list) -> str:
        clause = ""
        if since:
            params.append(since)
            clause += f" AND COALESCE({conf}, {created}) >= ${len(params)}"
        if until:
            params.append(until)
            clause += f" AND COALESCE({conf}, {created}) <= ${len(params)}"
        return clause

    # Платежи по удалённым/фантомным заказам НЕ показываем — они исключены и из
    # итога «Деньги» (get_money_totals), поэтому лента обязана с ним совпадать
    # (иначе платёж «принят» в ленте, но не в сумме — противоречие). standalone-
    # платежи (order_id IS NULL) показываем.
    # С периодом — два запроса по статусу. Подтверждённые фильтруются
    # выражением COALESCE(confirmed_at, created_at) при status = 'confirmed' —
    # ровно форма частичного индекса payments(COALESCE(confirmed_at,
    # created_at)) WHERE status = 'confirmed', и за давний период не
    # просматривается вся свежая история. Остальных статусов мало, их берёт
    # обратный проход по idx_payments_created. Итог тот же, что у одного
    # запроса: обе части режутся тем же LIMIT и сливаются по дате ниже.
    pay_select = (
        "SELECT p.id, p.user_id, p.amount_cents, p.currency, p.status, p.comment, "
        "p.order_id, p.created_at "
        f"FROM payments p {_LIVE_ORDER_PAYMENT_JOIN.format(p='p')} "
        f"WHERE {_LIVE_ORDER_PAYMENT_FILTER}"
    )
    if since or until:
        pays = []
        for status_sql in ("p.status = 'confirmed'", "(p.status IS NULL OR p.status <> 'confirmed')"):
            pay_params: list = []
            pay_clause = _period("p.confirmed_at", "p.created_at", pay_params)
            pay_params.append(limit)
            pays.extend(
                await adb_core.fetch(
                    f"{pay_select} AND {status_sql}{pay_clause} "
                    f"ORDER BY p.created_at DESC LIMIT ${len(pay_params)}",
                    *pay_params,
                )
            )
    else:
        pays = await adb_core.fetch(
            f"{pay_select} ORDER BY p.created_at DESC LIMIT $1", limit
        )
    dep_params: list = []
    dep_clause = _period("confirmed_at", "created_at", dep_params)
    dep_params.append(limit)
    deps = await adb_core.fetch(
        "SELECT id, manager_id, amount_cents, status, reject_reason, created_at "
        f"FROM cash_deposits WHERE 1 = 1{dep_clause} "
        f"ORDER BY created_at DESC LIMIT ${len(dep_params)}",
        *dep_params,
    )
    # Возвраты: сумма в валюте ЗАКАЗА (LEFT JOIN orders.currency, WP-07) + фильтр
    # «живого заказа» (WP-11) — возврат по удалённому заказу не показываем, как и
    # его платежи (иначе в ленте висит «Возврат · заказ #N» по заказу-фантому).
    ret_params: list = []
    ret_clause = _period("r.confirmed_at", "r.created_at", ret_params)
    ret_params.append(limit)
    rets = await adb_core.fetch(
        "SELECT r.id, r.order_id, r.created_by, r.total_amount_cents, "
        "r.refund_method, r.status, r.reason, r.created_at, o.currency AS order_currency "
        "FROM returns r LEFT JOIN orders o ON o.id = r.order_id "
        f"WHERE {_LIVE_ORDER_PAYMENT_FILTER}{ret_clause} "
        f"ORDER BY r.created_at DESC LIMIT ${len(ret_params)}",
        *ret_params,
    )
    # get_all_users — СИНХРОННЫЙ DB-read; внутри async-корутины (она минует
    # to_thread-мост async_db) прямой вызов блокировал event loop FastAPI на
    # запрос (WP-25). Через to_thread — не блокируем цикл.
    users = await asyncio.to_thread(get_all_users)
    names = {u["user_id"]: u.get("full_name") or str(u["user_id"]) for u in users}

    from config import BASE_CURRENCY

    base_cur = (BASE_CURRENCY or "USD").upper()

    from services import order_payments

    parts = await order_payments.parts_by_payment([int(p["id"]) for p in pays])
    dep_curs = await order_payments.deposit_currency([int(d["id"]) for d in deps])
    rows: list[dict] = []
    for p in pays:
        part = parts.get(int(p["id"]))
        rows.append({
            "kind": "payment", "id": p["id"],
            "amount": float(money.from_cents(int(p["amount_cents"] or 0))),
            "currency": p.get("currency") or base_cur, "status": p["status"],
            "who": names.get(p["user_id"], str(p["user_id"])),
            "order_id": p.get("order_id"), "note": p.get("comment") or "",
            "created_at": (p.get("created_at") or "")[:16],
            # Как получены деньги (строка разбивки): способ и сумма в валюте,
            # которую отдал клиент. Нет — старый платёж без способа.
            "method": part["method"] if part else None,
            "method_label": order_payments.METHODS.get(part["method"]) if part else None,
            # Куда пришли деньги (карта/счёт справочника) — «на карту •••• 1234 (…)».
            "account_label": part.get("account_label") if part else None,
            "part_amount": float(money.from_cents(int(part["amount_cents"]))) if part else None,
            "part_currency": part["currency"] if part else None,
        })
    for d in deps:
        rows.append({
            "kind": "deposit", "id": d["id"],
            "amount": float(money.from_cents(int(d["amount_cents"] or 0))),
            "currency": dep_curs.get(int(d["id"]), base_cur), "status": d["status"],
            "who": names.get(d["manager_id"], str(d["manager_id"])),
            "order_id": None, "note": d.get("reject_reason") or "",
            "created_at": (d.get("created_at") or "")[:16],
        })
    for r in rets:
        rows.append({
            "kind": "return", "id": r["id"],
            "amount": float(money.from_cents(int(r["total_amount_cents"] or 0))),
            "currency": (r.get("order_currency") or base_cur), "status": r["status"],
            "who": names.get(r["created_by"], str(r["created_by"])),
            "order_id": r.get("order_id"), "note": r.get("reason") or "",
            "created_at": (r.get("created_at") or "")[:16],
        })
    # Сортировка по дате убыв.; created_at — строка YYYY-MM-DD HH:MM (лексикогр.).
    rows.sort(key=lambda x: x["created_at"], reverse=True)
    return rows[:limit]


async def get_money_totals(since: str | None = None, until: str | None = None) -> dict:
    """Поступления компании за период (раздел «Деньги», boss/admin).

    - payments: подтверждённые платежи по валютам (в копейках). Платежи по
      фантомным заказам (ms_deleted_at) исключаем — как в
      сводке долгов «получено»; standalone-платежи без order_id считаем.
    - deposits: подтверждённые сдачи наличных (USD), суммарно.

    Период считаем по ВРЕМЕНИ ПОДТВЕРЖДЕНИЯ (confirmed_at) — это «поступления»,
    т.е. деньги, ПОЛУЧЕННЫЕ в периоде. Раньше фильтровали по created_at (время
    создания записи) → платёж/сдача, созданные в прошлом периоде, но
    подтверждённые в текущем, не попадали в итог («не считает платежи и сдачу»).
    COALESCE(confirmed_at, created_at) — для легаси-строк без confirmed_at.

    since/until — ISO 'YYYY-MM-DD HH:MM:SS' в локальной TZ (как пишется
    confirmed_at/created_at); порог считаем в Python, передаём параметром
    (CLAUDE.md) — НЕ сравниваем с SQL NOW()/datetime('now') (разные TZ).
    """
    pay_sql = (
        "SELECT p.currency AS currency, COUNT(*) AS cnt, "
        "COALESCE(SUM(p.amount_cents), 0) AS total_cents "
        f"FROM payments p {_LIVE_ORDER_PAYMENT_JOIN.format(p='p')} "
        f"WHERE p.status = 'confirmed' AND {_LIVE_ORDER_PAYMENT_FILTER} "
        # Наличная строка разбивки подтверждается сдачей, и её деньги уже
        # лежат в итоге сдач (в валюте, в которой их сдали). Посчитать ещё и
        # платёж значило бы получить одни наличные дважды.
        "AND NOT EXISTS (SELECT 1 FROM payment_parts pp WHERE pp.payment_id = p.id AND pp.method = 'cash')"
    )
    # Форма условия — `status = 'confirmed' AND COALESCE(confirmed_at,
    # created_at) >= / <=` — совпадает с частичным индексом
    # payments(COALESCE(confirmed_at, created_at)) WHERE status = 'confirmed'.
    # Переписав выражение иначе (другой порядок аргументов COALESCE, DATE(...)
    # поверх), индекс перестанет подходить, и экран «Деньги» снова пойдёт
    # полным проходом по платежам.
    pay_params: list = []
    if since:
        pay_params.append(since)
        pay_sql += f" AND COALESCE(p.confirmed_at, p.created_at) >= ${len(pay_params)}"
    if until:
        pay_params.append(until)
        pay_sql += f" AND COALESCE(p.confirmed_at, p.created_at) <= ${len(pay_params)}"
    # Группа — ещё и по снимку курса: пересчёт в базовую валюту обязан идти по
    # курсу дня подтверждения (payments.fx_rate_to_base), а не по сегодняшнему.
    # Разбивка по валютам собирается из тех же строк в Python.
    pay_sql = pay_sql.replace(
        "SELECT p.currency AS currency,",
        "SELECT p.currency AS currency, p.fx_rate_to_base AS fx_rate_to_base,",
        1,
    )
    pay_sql += " GROUP BY p.currency, p.fx_rate_to_base"
    pay_rows = await adb_core.fetch(pay_sql, *pay_params)
    by_currency: dict[str, dict] = {}
    by_rate: list[dict] = []
    for r in pay_rows:
        cur_code = r.get("currency") or "—"
        cents = int(r["total_cents"] or 0)
        cnt = int(r["cnt"] or 0)
        agg = by_currency.setdefault(cur_code, {"currency": cur_code, "total_cents": 0, "count": 0})
        agg["total_cents"] += cents
        agg["count"] += cnt
        snap = r.get("fx_rate_to_base")
        by_rate.append({
            "currency": cur_code,
            "fx_rate_to_base": float(snap) if snap is not None else None,
            "total_cents": cents,
        })

    # Сдачи — по валюте сдачи (`cash_deposit_currency`, нет строки — базовая):
    # сумы, сданные в кассу, не становятся долларами.
    from config import BASE_CURRENCY

    dep_sql = (
        "SELECT COALESCE(UPPER(c.currency), $1) AS currency, COUNT(*) AS cnt, "
        "COALESCE(SUM(d.amount_cents), 0) AS total_cents "
        "FROM cash_deposits d LEFT JOIN cash_deposit_currency c ON c.deposit_id = d.id "
        "WHERE d.status = 'confirmed'"
    )
    dep_params: list = [(BASE_CURRENCY or "USD").upper()]
    if since:
        dep_params.append(since)
        dep_sql += f" AND COALESCE(d.confirmed_at, d.created_at) >= ${len(dep_params)}"
    if until:
        dep_params.append(until)
        dep_sql += f" AND COALESCE(d.confirmed_at, d.created_at) <= ${len(dep_params)}"
    dep_sql += " GROUP BY COALESCE(UPPER(c.currency), $1)"
    dep_rows = await adb_core.fetch(dep_sql, *dep_params)
    dep_by_cur = [
        {"currency": r["currency"], "total_cents": int(r["total_cents"] or 0), "count": int(r["cnt"] or 0)}
        for r in dep_rows
    ]
    base_dep = next((d for d in dep_by_cur if d["currency"] == dep_params[0]), None)
    dep_row = {
        "total_cents": base_dep["total_cents"] if base_dep else 0,
        "cnt": sum(d["count"] for d in dep_by_cur),
    }

    return {
        "payments": sorted(by_currency.values(), key=lambda p: p["total_cents"], reverse=True),
        # Те же деньги, разложенные по снимку курса — для итога в базовой валюте.
        "payments_by_rate": by_rate,
        "deposits": {
            # total_cents — сдачи в базовой валюте (прежний контракт экрана);
            # все валюты — в by_currency.
            "total_cents": int(dep_row["total_cents"] or 0),
            "count": int(dep_row["cnt"] or 0),
            "by_currency": dep_by_cur,
        },
    }


# ─── Аудит лог ────────────────────────────────────────────────────────────────


def add_audit_log(user_id, full_name, role, action, details=""):
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "INSERT INTO audit_log (user_id, full_name, role, action, details, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (user_id, full_name, role, action, details, now_str()),
        )
        conn.commit()


async def get_audit_log(limit: int = 50, user_id: int | None = None) -> list[dict]:
    """Записи аудита (последние сверху). asyncpg-миграция Stage 6 (задача #21):
    нативный async через adb_core. Вызовы — только async (handlers/audit, log)."""
    if user_id:
        return await adb_core.fetch(
            "SELECT * FROM audit_log WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id,
            limit,
        )
    return await adb_core.fetch(
        "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT $1",
        limit,
    )


# ─── Заказы ───────────────────────────────────────────────────────────────────


async def get_or_create_draft(user_id: int, full_name: str, comment: str = "") -> tuple[int, bool]:
    """Черновик для «создать заказ»: переиспользуем пустой, иначе создаём.

    T3.2: кнопка «Новый заказ» создавала черновик на КАЖДОЕ нажатие. Тапнул
    трижды — три пустых заказа в /myorders, и непонятно, в каком из них ты
    сейчас работаешь. Пустой = без позиций, без клиента и без комментария:
    в такой заказ пользователь ещё ничего не вложил, поэтому вернуть его
    безопасно.

    Возвращает (order_id, created) — created=False, если переиспользовали.
    """
    row = await adb_core.fetchrow(
        "SELECT o.id FROM orders o "
        "WHERE o.user_id = $1 AND o.status = 'draft' "
        "  AND (o.agent_id IS NULL OR o.agent_id = '') "
        "  AND (o.comment IS NULL OR o.comment = '') "
        "  AND NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id) "
        "ORDER BY o.id DESC LIMIT 1",
        user_id,
    )
    if row and not comment:
        return int(row["id"]), False
    return await asyncio.to_thread(create_order, user_id, full_name, comment), True


def create_order(user_id: int, full_name: str, comment: str = "") -> int:
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO orders (user_id, full_name, status, comment, created_at, updated_at)
                VALUES (%s, %s, 'draft', %s, %s, %s) RETURNING id
            """,
                (user_id, full_name, comment, now_str(), now_str()),
            )
            order_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO orders (user_id, full_name, status, comment, created_at, updated_at)
                VALUES (?, ?, 'draft', ?, ?, ?)
            """,
                (user_id, full_name, comment, now_str(), now_str()),
            )
            order_id = cur.lastrowid
        conn.commit()
    return order_id


async def get_order(order_id: int) -> dict | None:
    """asyncpg Stage 19 (#21): native async (fetchrow). Общий read order/payment-
    ядра — все to_thread-мосты к нему сняты в этой стадии."""
    return await adb_core.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)


async def get_orders_by_ids(order_ids: list[int]) -> dict[int, dict]:
    """Батч-загрузка заказов по id → словарь {id: order_dict}.
    order_ids дедуплицируется — placeholder'ы не расходуем впустую.

    asyncpg Stage 13 (#21): native async; IN-список — $1..$N."""
    if not order_ids:
        return {}
    unique_ids = list(set(order_ids))
    placeholders = ", ".join(f"${i + 1}" for i in range(len(unique_ids)))
    rows = await adb_core.fetch(
        f"SELECT * FROM orders WHERE id IN ({placeholders})",
        *unique_ids,
    )
    return {r["id"]: r for r in rows}


async def get_user_orders(
    user_id: int, status: str | None = None, limit: int = 200
) -> list[dict]:
    """Заказы менеджера (опц. фильтр по статусу), свежие первыми.

    T2.13 (§3.8): добавлен LIMIT. Выборка была без границ, а /api/home зовёт её
    на каждое открытие главной (rate-limit 120/мин) — у менеджера с историей
    в тысячи заказов это полный скан и сериализация всего архива ради
    счётчиков и пяти последних.

    asyncpg Stage 12 (#21): native async через adb_core."""
    params: list = [user_id]
    # Прячем заказы, удалённые в МойСклад (ms_deleted_at) —
    # иначе «фантомы» (CO удалён в МС) висят в списке, рассинхрон с аналитикой,
    # которая их уже исключает (get_manager_performance).
    query = "SELECT * FROM orders WHERE user_id = $1 AND (ms_deleted_at IS NULL)"
    if status:
        params.append(status)
        query += f" AND status = ${len(params)}"
    params.append(max(1, int(limit)))
    query += f" ORDER BY created_at DESC LIMIT ${len(params)}"
    return await adb_core.fetch(query, *params)


async def get_all_orders(status: str | None = None) -> list[dict]:
    """Все заказы (опц. фильтр по статусу). asyncpg-миграция Stage 6
    (задача #21): нативный async через adb_core. Вызов — только webapp
    (`await adb.get_all_orders()`)."""
    # Прячем удалённые в МС (ms_deleted_at) заказы — список
    # должен сходиться с аналитикой, которая их уже исключает.
    query = "SELECT * FROM orders WHERE (ms_deleted_at IS NULL)"
    params: list = []
    if status:
        params.append(status)
        query += f" AND status = ${len(params)}"
    query += " ORDER BY created_at DESC"
    return await adb_core.fetch(query, *params)


# Области списка заказов по роли — одно определение на страницу и на счётчики.
ORDER_SCOPES = ("all", "to_ship", "user")


async def get_orders_page(
    *,
    scope: str,
    user_id: int | None = None,
    statuses: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int, int]:
    """Страница списка заказов: фильтры, LIMIT/OFFSET и подсчёты — в SQL.

    → (заказы страницы свежими вперёд, total по фильтрам, pending_count по всей
    области роли без фильтров).

    Раньше `/api/orders` читал ВСЕ заказы роли (`SELECT * FROM orders` без
    LIMIT), фильтровал и резал страницу в Python, а позиции грузил по
    `IN (все id)`. На истории это упирается в память и в предел asyncpg — 32 767
    параметров на запрос — и ручка падает целиком. Здесь в память попадает одна
    страница.

    `scope`: "all" — руководство; "to_ship" — кладовщик (одобренные и
    отгруженные); "user" — свои заказы `user_id`. Даты — YYYY-MM-DD
    включительно, как в `webapp.server._paginate_orders`: день заказа — первые
    10 знаков `created_at`, условие записано диапазоном по самой колонке, чтобы
    работал индекс `orders(created_at, id)`. Порядок `created_at DESC, id DESC`
    — тот же индекс в обратную сторону и стабильные страницы при равных
    отметках времени.
    """
    if scope not in ORDER_SCOPES:
        raise ValueError(f"неизвестная область заказов: {scope!r}")
    base_where = ["(ms_deleted_at IS NULL)"]
    base_args: list = []
    if scope == "to_ship":
        base_where.append("status IN ('approved', 'shipped')")
    elif scope == "user":
        base_args.append(int(user_id or 0))
        base_where.append(f"user_id = ${len(base_args)}")

    where = list(base_where)
    args = list(base_args)
    wanted = [str(x) for x in dict.fromkeys(statuses or []) if x]
    if wanted:
        ph = []
        for st in wanted:
            args.append(st)
            ph.append(f"${len(args)}")
        where.append(f"status IN ({', '.join(ph)})")
    if date_from:
        args.append(date_from[:10])
        where.append(f"created_at >= ${len(args)}")
    if date_to:
        # «По этот день включительно» = строго раньше начала следующего дня.
        upper = (datetime.strptime(date_to[:10], "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
        args.append(upper)
        # created_at > '' — пустая отметка не попадает в период, как в Python-фильтре.
        where.append(f"created_at < ${len(args)} AND created_at > ''")
    where_sql = " AND ".join(where)

    total = int(await adb_core.fetchval(f"SELECT COUNT(*) FROM orders WHERE {where_sql}", *args) or 0)
    pending = int(
        await adb_core.fetchval(
            f"SELECT COUNT(*) FROM orders WHERE {' AND '.join(base_where)} AND status = 'pending'",
            *base_args,
        )
        or 0
    )
    page_args = [*args, max(1, int(limit)), max(0, int(offset))]
    rows = await adb_core.fetch(
        f"SELECT * FROM orders WHERE {where_sql} "
        f"ORDER BY created_at DESC, id DESC LIMIT ${len(page_args) - 1} OFFSET ${len(page_args)}",
        *page_args,
    )
    return rows, total, pending


def _like_escape(s: str) -> str:
    r"""Экранировать LIKE-метасимволы (% _ \) в пользовательском вводе.

    Без этого поиск «50%» или «order_1» интерпретировал бы %/_ как
    wildcard'ы. Экранируем и используем ESCAPE '\' в LIKE-запросе.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_orders(query: str, user_id: int | None = None, limit: int = 20) -> list[dict]:
    """Поиск заказов по тексту: full_name / agent_name / comment, либо по id
    если query — число.

    `user_id` задан → только заказы этого пользователя (скоуп менеджера).
    None → все (boss/admin).
    """
    query = (query or "").strip()
    if not query:
        return []
    like = f"%{_like_escape(query.lower())}%"
    conds = [
        "LOWER(COALESCE(full_name,'')) LIKE ? ESCAPE '\\'",
        "LOWER(COALESCE(agent_name,'')) LIKE ? ESCAPE '\\'",
        "LOWER(COALESCE(comment,'')) LIKE ? ESCAPE '\\'",
    ]
    params: list = [like, like, like]
    if query.isdigit():
        conds.append("id = ?")
        params.append(int(query))
    where = "(" + " OR ".join(conds) + ")"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    params.append(limit)
    sql = f"SELECT * FROM orders WHERE {where} ORDER BY created_at DESC LIMIT ?"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(sql), params)
        return [dict(r) for r in cur.fetchall()]


def search_payments(query: str, user_id: int | None = None, limit: int = 20) -> list[dict]:
    """Поиск платежей: full_name / username / comment, либо по id если число.

    `user_id` задан → только платежи этого пользователя; None → все.
    """
    query = (query or "").strip()
    if not query:
        return []
    like = f"%{_like_escape(query.lower())}%"
    conds = [
        "LOWER(COALESCE(full_name,'')) LIKE ? ESCAPE '\\'",
        "LOWER(COALESCE(username,'')) LIKE ? ESCAPE '\\'",
        "LOWER(COALESCE(comment,'')) LIKE ? ESCAPE '\\'",
    ]
    params: list = [like, like, like]
    if query.isdigit():
        conds.append("id = ?")
        params.append(int(query))
    where = "(" + " OR ".join(conds) + ")"
    if user_id is not None:
        where += " AND user_id = ?"
        params.append(user_id)
    params.append(limit)
    sql = f"SELECT * FROM payments WHERE {where} ORDER BY id DESC LIMIT ?"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(sql), params)
        return [_with_major(r, ("amount", "amount_cents")) for r in cur.fetchall()]


def _draft_locked(cur, order_id: int) -> bool:
    """Заказ — черновик? Под `FOR UPDATE` строки, в транзакции вызывающего.

    Правка состава/клиента/валюты проверялась в ручке ОТДЕЛЬНЫМ чтением, а
    запись шла следующим коммитом. Сабмит (draft→pending под FOR UPDATE)
    успевал между ними, и позиция ложилась в уже отправленный заказ: босс
    одобрял одну сумму, кредит-лимит проверялся по ней, а отгружалась другая.
    Здесь запись ждёт сабмит на замке и видит его результат.
    """
    lock = " FOR UPDATE" if USE_POSTGRES else ""
    cur.execute(q(f"SELECT status FROM orders WHERE id = ?{lock}"), (order_id,))
    row = cur.fetchone()
    if row is None:
        return False
    status = row["status"] if hasattr(row, "keys") else row[0]
    return status == "draft"


def update_order_agent(
    order_id: int, agent_id: str, agent_name: str, *, require_draft: bool = False
) -> bool:
    """`require_draft` — менять только черновик, проверка в той же транзакции."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        if require_draft and not _draft_locked(cur, order_id):
            conn.rollback()
            return False
        cur.execute(
            q("UPDATE orders SET agent_id = ?, agent_name = ?, updated_at = ? WHERE id = ?"),
            (agent_id, agent_name, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


async def set_order_payment(
    order_id: int,
    payment_type: str,
    due_date: str | None = None,
) -> bool:
    """Установить тип оплаты заказа (paid|credit).

    Для credit обязателен due_date (ISO YYYY-MM-DD) — дата к которой
    клиент обязался погасить долг. Для paid due_date игнорируется
    и обнуляется (на случай если заказ переводят из credit обратно).

    Не сбрасывает paid_at — закрытый долг остаётся закрытым.

    asyncpg Stage 16 (#21): native async через adb_core.execute.
    """
    if payment_type not in ("paid", "credit"):
        return False
    if payment_type == "credit" and not due_date:
        return False
    if payment_type == "paid":
        rc = await adb_core.execute(
            "UPDATE orders SET payment_type = $1, due_date = NULL, "
            "updated_at = $2 WHERE id = $3",
            payment_type, now_str(), order_id,
        )
    else:
        rc = await adb_core.execute(
            "UPDATE orders SET payment_type = $1, due_date = $2, updated_at = $3 WHERE id = $4",
            payment_type, due_date, now_str(), order_id,
        )
    return rc > 0


async def mark_order_paid(
    order_id: int,
    marked_by: int,
    marked_by_name: str,
    amount: float | None = None,
    username: str = "",
    idem_key: str | None = None,
) -> tuple[bool, int | None]:
    """Менеджер отмечает поступление денег по заказу.

    `idem_key` — застолблённый ключ идемпотентности: результат пишется в него
    той же транзакцией, что и платёж (`idem_store_in`).

    Поведение:
      1. Создаёт payment-запись в таблице payments с order_id=N и
         статусом 'pending'. Amount = сколько именно получено сейчас
         (для частичной оплаты). Если None — берётся remaining (полная
         доплата до закрытия). 0 / отрицательное — отклоняем.
      2. Ставит order.paid_at = now() если ещё не стоит (легаси-флаг —
         мы используем его в UI как «менеджер хоть раз отметил оплату»).
      3. Возвращает (True, payment_id) при успехе. После approve босса
         через стандартный confirm_payment() сумма зачтётся, и когда
         все payments суммарно покроют order.total — заказ
         автоматически перейдёт в paid_confirmed_at.

    Возвращает (False, None) если order не существует, не credit,
    уже полностью закрыт (paid_confirmed_at стоит), или amount некорректен.

    asyncpg Stage 16 (#21): native async. Вся критическая секция (FOR UPDATE на
    orders → recompute сумм → INSERT payment → UPDATE paid_at) в одной
    adb_core.transaction(); ранние return (False, None) — ДО любых записей
    (commit пустой/lock-only tx, lock освобождён). INSERT-id: RETURNING (pg) /
    last_insert_rowid() (sqlite). add_audit_log/get_role (sync) — мост через
    to_thread после транзакции.
    """
    # Все шаги — внутри одной транзакции с lock'ом на заказ.
    # Раньше без блокировки два менеджера могли одновременно отметить
    # частичные суммы 70+70 на остаток 100 — оба проходили проверку
    # `amount ≤ remaining` (каждый считал «свой» remaining до второго),
    # и в pending копилось 140 при долге 100. Босс потом разбирался.
    # Теперь FOR UPDATE на orders сериализует параллельные mark_paid'ы.
    from config import BASE_CURRENCY

    async with adb_core.transaction() as txn:
        # Lock заказа
        if USE_POSTGRES:
            row = await txn.fetchrow(
                "SELECT payment_type, currency, agent_name, paid_confirmed_at, status "
                "FROM orders WHERE id = $1 FOR UPDATE",
                order_id,
            )
        else:
            row = await txn.fetchrow(
                "SELECT payment_type, currency, agent_name, paid_confirmed_at, status "
                "FROM orders WHERE id = $1",
                order_id,
            )
        if not row:
            return (False, None)
        payment_type = row["payment_type"]
        currency = row["currency"] or BASE_CURRENCY
        agent_name = row["agent_name"]
        already_closed = row["paid_confirmed_at"] is not None

        # «Оплата сразу» тоже принимается: её автоплатёж могли отклонить (денег
        # не было), и заявить оплату повторно было бы нечем — заказ так и висел
        # бы неоплаченным без единой кнопки. Долгом он виден в get_open_debts.
        if payment_type not in ("credit", "paid"):
            return (False, None)
        if already_closed:
            return (False, None)
        # Гард статуса (WP-09): оплату принимаем только по активному кредит-заказу.
        # Без него менеджер мог mark_paid по черновику/отклонённому (напр. кредит-
        # заказ, возвращённый боссом в draft) — pending-платёж против не-одобренного
        # заказа, который потом удалят (orphan-платёж в кассе).
        if row["status"] not in ("approved", "shipped", "partially_returned"):
            return (False, None)

        # Под locком считаем суммы — гарантия что между recompute и
        # INSERT никто другой не добавит payment. Считаем в копейках (точно).
        #
        # Сколько ещё можно заявить — services.debts.calc_claimable_cents:
        # total − возвраты − платежи (confirmed+pending, в валюте заказа) −
        # сдачи (confirmed+pending). Своя копия формулы здесь забывала сдачи:
        # заказ 100, сдача 60 подтверждена, экран честно показывал остаток 40,
        # а «весь остаток» создавал платёж на 100. Та же функция решает, сколько
        # можно распределить сдачей, — два пути заявить одни и те же деньги
        # больше не складываются поверх друг друга.
        from services.debts import calc_claimable_cents

        remaining_cents = (await calc_claimable_cents([order_id], conn=txn)).get(order_id, 0)

        # Если amount не задан — берём остаток (полная доплата)
        if amount is None:
            amount_cents = remaining_cents
        else:
            try:
                amount_cents = money.to_cents(amount)
            except (TypeError, ValueError, ArithmeticError):
                return (False, None)
        if amount_cents <= 0:
            return (False, None)
        # Не даём ввести больше остатка
        if amount_cents > remaining_cents:
            amount_cents = remaining_cents
            if amount_cents <= 0:
                return (False, None)
        amount = float(money.from_cents(amount_cents))

        # INSERT payment в той же транзакции
        comment = f"Оплата по заказу #{order_id}" + (f" ({agent_name})" if agent_name else "")
        if USE_POSTGRES:
            payment_id = await txn.fetchval(
                "INSERT INTO payments "
                "(user_id, username, full_name, amount_cents, currency, comment, "
                " status, created_at, order_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8) "
                "RETURNING id",
                marked_by, username, marked_by_name, amount_cents,
                currency, comment, now_str(), order_id,
            )
        else:
            await txn.execute(
                "INSERT INTO payments "
                "(user_id, username, full_name, amount_cents, currency, comment, "
                " status, created_at, order_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)",
                marked_by, username, marked_by_name, amount_cents,
                currency, comment, now_str(), order_id,
            )
            payment_id = await txn.fetchval("SELECT last_insert_rowid()")

        # paid_at — флаг «менеджер хоть раз отметил». COALESCE сохраняет
        # самое раннее время для последующих частичных платежей.
        await txn.execute(
            "UPDATE orders SET paid_at = COALESCE(paid_at, $1), updated_at = $2 WHERE id = $3",
            now_str(), now_str(), order_id,
        )
        await idem_store_in(txn, idem_key, {"ok": True, "payment_id": payment_id})

    remaining_after = float(money.from_cents(max(0, remaining_cents - amount_cents)))
    await asyncio.to_thread(
        add_audit_log,
        marked_by,
        marked_by_name,
        await asyncio.to_thread(get_role, marked_by),
        "debt_payment_claimed",
        f"Заказ #{order_id}: менеджер отметил {amount:,.0f} {currency} "
        f"(после подтверждения останется: {remaining_after:,.0f})",
    )
    return (True, payment_id)


async def confirm_all_pending_payments_for_order(
    order_id: int,
    confirmed_by: int,
    confirmed_by_name: str,
) -> int:
    """Босс одной кнопкой подтверждает ВСЕ pending платежи по заказу.

    Удобно: при частичных оплатах у заказа могут висеть несколько
    pending payments (менеджер отмечал по очереди). Босс не хочет
    кликать каждый отдельно — этот хелпер закрывает их пачкой.
    Возвращает кол-во подтверждённых.

    Если после серии confirm'ов сумма confirmed достигла order.total —
    заказ автоматически закроется через _maybe_close_order_after_payment.

    asyncpg Stage 15 (#21): native async; confirm_payment теперь async (await);
    get_payments_for_order (sync read) — мост через to_thread.
    """
    from services import order_payments

    payments = await get_payments_for_order(order_id)
    pending = [p for p in payments if p["status"] == "pending"]
    # Права — по всей пачке ДО первого подтверждения: иначе отказ на середине
    # оставил бы заказ подтверждённым наполовину.
    if pending:
        await order_payments.require_confirm_rights(confirmed_by, [p.get("user_id") for p in pending])
    n = 0
    for p in pending:
        if await confirm_payment(p["id"], confirmed_by, confirmed_by_name):
            n += 1
    return n


async def reject_all_pending_payments_for_order(
    order_id: int,
    rejected_by: int,
    rejected_by_name: str,
) -> int:
    """Босс отклоняет ВСЕ pending платежи по заказу. Аналог confirm_all.

    asyncpg Stage 15 (#21): native async; reject_payment теперь async (await);
    get_payments_for_order (sync read) — мост через to_thread."""
    payments = await get_payments_for_order(order_id)
    pending = [p for p in payments if p["status"] == "pending"]
    n = 0
    for p in pending:
        if await reject_payment(p["id"], rejected_by, rejected_by_name):
            n += 1
    return n


# Какие заказы — открытый долг. Одно определение на список долгов и на счётчик
# «Требует внимания» (count_boss_attention): разъехавшись, они показывали бы
# боссу число, не совпадающее со списком.
#
# «Оплата сразу» (paid) — тоже долг, пока деньги не подтверждены. Раньше фильтр
# брал только credit, а очередь «Подтвердить» — только paid с ОЖИДАЮЩИМ
# платежом: заказ, чей автоплатёж отклонили (или он не создался), не попадал
# никуда — ни в «Долги», ни в «Нам должны», ни в очередь. Товар уехал, денег
# нет, и нигде этого не видно.
_OPEN_DEBT_FILTER = (
    "payment_type IN ('credit', 'paid') AND paid_confirmed_at IS NULL "
    # partially_returned тоже несёт остаток долга (частичный возврат не
    # закрыл заказ) — иначе он исчезал из «Долги», но висел в «Клиенты»/
    # кредит-чеке (WP-06, согласовано с get_agent_current_debt).
    "AND status IN ('approved', 'shipped', 'partially_returned') "
    # Заказ удалён в МойСклад (фантом) → долга по нему быть не должно.
    "AND (ms_deleted_at IS NULL)"
)

# Срок оплаты для фильтра «к оплате сейчас». У «оплаты сразу» своего due_date
# нет — деньги причитались в день заказа, поэтому срок = дата создания (первые
# 10 символов локальной строки created_at; SUBSTR одинаков в SQLite и Postgres).
_DEBT_DUE_SQL = (
    "COALESCE(due_date, CASE WHEN payment_type = 'paid' "
    "THEN SUBSTR(created_at, 1, 10) END)"
)


def debt_due_date(order: dict) -> str | None:
    """Срок оплаты долга в Python — зеркало _DEBT_DUE_SQL для раскраски
    просрочки на экране."""
    due = order.get("due_date")
    if due:
        return str(due)[:10]
    if order.get("payment_type") == "paid" and order.get("created_at"):
        return str(order["created_at"])[:10]
    return None


async def get_open_debts(
    user_id: int | None = None,
    due_through: str | None = None,
) -> list[dict]:
    """Список открытых долгов (credit/paid + paid_confirmed_at IS NULL).

    Параметры:
      user_id      — если указан, отдаём только долги этого менеджера;
                     иначе все долги (для boss/admin).
      due_through  — ISO YYYY-MM-DD; вернуть только долги с due_date <=
                     этой даты (т.е. «к оплате на сегодня и просроченные»).
                     None — отдаём все открытые без фильтра по дате.

    Сортировка: сначала просроченные (старая due_date), потом сегодняшние.
    Это удобно и для UI, и для уведомлений.

    Заказ остаётся «открытым» пока paid_confirmed_at IS NULL — то есть
    пока босс не подтвердил поступление. Менеджерский paid_at одного
    недостаточно: до подтверждения деньги формально ещё не получены,
    и заказ всё ещё в списке долгов (но с пометкой `awaiting_confirmation`
    на стороне UI).

    asyncpg Stage 12 (#21): native async через adb_core ($N-плейсхолдеры).
    """
    query = (
        "SELECT * FROM orders "
        f"WHERE {_OPEN_DEBT_FILTER}"
    )
    params: list = []
    if user_id is not None:
        params.append(user_id)
        query += f" AND user_id = ${len(params)}"
    if due_through is not None:
        params.append(due_through)
        query += f" AND {_DEBT_DUE_SQL} IS NOT NULL AND {_DEBT_DUE_SQL} <= ${len(params)}"
    query += (
        " ORDER BY due_date ASC NULLS LAST, id ASC"
        if USE_POSTGRES
        else " ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date ASC, id ASC"
    )
    return await adb_core.fetch(query, *params)


async def get_paid_orders_awaiting_confirmation(user_id: int | None = None) -> list[dict]:
    """Paid-заказы с pending-платежом, ожидающие подтверждения боссом.

    Когда босс одобряет отгрузку по заказу payment_type='paid', авто-
    создаётся pending-платёж (фиксация поступления денег + синк в МойСклад).
    Credit-долги уже видны через get_open_debts; здесь — ТОЛЬКО paid, чтобы
    дать боссу surface для подтверждения в WebApp (таб «Платежи»).

    user_id — если указан, только заказы этого менеджера; иначе все.

    asyncpg-миграция Stage 1 (задача #21): пилотная функция переведена на
    нативный async через services.adb_core (плейсхолдеры $N). Вызывается
    только из webapp через `await adb.get_paid_orders_awaiting_confirmation()`;
    async_db пропускает coroutine-функции без to_thread-обёртки. Sync-вызовов
    у неё нет — поэтому она безопасна как первый пилот.
    """
    query = (
        "SELECT * FROM orders o "
        "WHERE o.payment_type = 'paid' "
        "AND o.status IN ('approved', 'shipped') "
        # Заказ удалён в МойСклад (фантом) → не ждём по нему подтверждения оплаты.
        "AND (o.ms_deleted_at IS NULL) "
        "AND EXISTS (SELECT 1 FROM payments p "
        "            WHERE p.order_id = o.id AND p.status = 'pending')"
    )
    params: list = []
    if user_id is not None:
        params.append(user_id)
        query += f" AND o.user_id = ${len(params)}"
    query += " ORDER BY o.id ASC"
    return await adb_core.fetch(query, *params)


def update_order_currency(order_id: int, currency: str, *, require_draft: bool = False) -> bool:
    """Установить валюту заказа. Применяется ко всем позициям одного
    ордера — менять между позициями не имеет смысла. `require_draft` — только
    у черновика, проверка в той же транзакции (`_draft_locked`)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        if require_draft and not _draft_locked(cur, order_id):
            conn.rollback()
            return False
        cur.execute(
            q("UPDATE orders SET currency = ?, updated_at = ? WHERE id = ?"),
            (currency, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated

def legal_sources_for(target_status: str) -> tuple[str, ...]:
    """Из каких статусов переход в `target_status` легален — по TRANSITIONS.

    Второй список легальных переходов не заводим: машина состояний одна, она
    в services.order_workflow. Импорт ленивый — order_workflow тянет database
    внутри функций, module-level импорт в обе стороны дал бы цикл."""
    from services.order_workflow import TRANSITIONS

    return tuple(src for src, targets in TRANSITIONS.items() if target_status in targets)


def _status_cas_sql(expected_status: str | tuple[str, ...] | None) -> tuple[str, list]:
    """WHERE-хвост и параметры для compare-and-set статуса заказа."""
    if expected_status is None:
        return "", []
    expected = (expected_status,) if isinstance(expected_status, str) else tuple(expected_status)
    if not expected:
        # Пустой набор = переход нелегален ни из какого статуса. Ставим заведомо
        # ложное условие, а не «без guard'а» — иначе опечатка в target_status
        # молча превратилась бы в безусловный UPDATE.
        return " AND 1 = 0", []
    ph = ", ".join("?" for _ in expected)
    return f" AND status IN ({ph})", list(expected)


def _snapshot_order_fx(order_id: int) -> None:
    """Заморозить курс валюты заказа к BASE_CURRENCY (orders.fx_rate_to_base).

    Идемпотентно: ставит ТОЛЬКО если снимка ещё нет и валюта известна. Берёт
    текущий курс (get_currency_rate, кэш TTL) — для базовой валюты это 1.0
    (засеяно). Вызывается при первом «реализующем» переходе заказа (approved/
    shipped), чтобы итог в USD по этому заказу не «плыл» при движении курса.
    """
    try:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q("SELECT currency, fx_rate_to_base FROM orders WHERE id = ?"),
                (order_id,),
            )
            row = cur.fetchone()
        if row is None:
            return
        currency = row["currency"] if hasattr(row, "keys") else row[0]
        existing = row["fx_rate_to_base"] if hasattr(row, "keys") else row[1]
        if existing is not None or not currency:
            return
        rate = get_currency_rate(currency)
        if rate is None:
            return
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q(
                    "UPDATE orders SET fx_rate_to_base = ? "
                    "WHERE id = ? AND fx_rate_to_base IS NULL"
                ),
                (float(rate), order_id),
            )
            conn.commit()
    except Exception:
        # Снимок курса — best-effort: его отсутствие не должно ронять смену
        # статуса (пересчёт упадёт обратно на текущий курс).
        logger.exception("_snapshot_order_fx(%s) failed", order_id)


def update_order_status(
    order_id: int,
    status: str,
    expected_status: str | tuple[str, ...] | None = None,
) -> bool:
    """Перевести заказ в статус. Возвращает True, если строка реально изменилась.

    `expected_status` — compare-and-set: UPDATE применяется, только если текущий
    статус входит в набор. Без него UPDATE безусловный (легаси-вызовы; они
    закрываются в T2.3/T2.7).

    Зачем CAS: МойСклад мог прислать `Unsuccessful` и перевести заказ
    pending→rejected, а заявка при этом осталась `pending`. Босс открывал старое
    сообщение в чате, жал «Одобрить» — и заказ ВОСКРЕСАЛ из rejected в approved
    с новыми документами в МС (§2.2)."""
    tail, extra = _status_cas_sql(expected_status)
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"UPDATE orders SET status = ?, updated_at = ? WHERE id = ?{tail}"),
            (status, now_str(), order_id, *extra),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated and status in ("approved", "shipped"):
        _snapshot_order_fx(order_id)
    return updated


async def cas_order_status(order_id: int, new_status: str, expected_status: str) -> bool:
    """Compare-and-set статуса: применяет new_status ТОЛЬКО если текущий ==
    expected_status. Защита от TOCTOU в системных обработчиках (вебхук МС читает
    статус, потом делает сетевой round-trip к МС, потом пишет — между чтением и
    записью статус мог измениться, напр. confirm_payment перевёл paid). Возвращает
    True, если применено. Native async через adb_core."""
    rc = await adb_core.execute(
        "UPDATE orders SET status = $1, updated_at = $2 WHERE id = $3 AND status = $4",
        new_status, now_str(), order_id, expected_status,
    )
    return rc > 0


def add_order_item(
    order_id: int,
    product_name: str,
    product_href: str,
    quantity: float,
    unit: str,
    price: float = 0.0,
    note: str = "",
    product_id: int | None = None,
    *,
    require_draft: bool = False,
) -> int | None:
    """Добавить позицию заказа.

    `product_id` — карточка нашей номенклатуры; по ней позиция спишется со
    склада при отгрузке. Связь пишется в `order_item_products` (отдельная
    таблица, см. схему), `product_href` остаётся у legacy-строк.

    `require_draft` — только в черновик, проверка статуса в той же транзакции
    (`_draft_locked`); не черновик → None.
    """
    price_cents = money.to_cents(price or 0)
    with get_conn() as conn:
        cur = get_cursor(conn)
        if require_draft and not _draft_locked(cur, order_id):
            conn.rollback()
            return None
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price_cents, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
                (order_id, product_name, product_href, quantity, unit, price_cents, note),
            )
            item_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price_cents, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (order_id, product_name, product_href, quantity, unit, price_cents, note),
            )
            item_id = cur.lastrowid
        if product_id:
            cur.execute(
                q(
                    "INSERT INTO order_item_products (item_id, order_id, product_id, "
                    "created_at) VALUES (?, ?, ?, ?)"
                ),
                (int(item_id), order_id, int(product_id), now_str()),
            )
        conn.commit()
    return item_id


# Размер пачки для `IN (...)`: заметно ниже предела asyncpg (32 767
# параметров на запрос) и предела SQLite (32 766 с версии 3.32).
_IN_CHUNK = 5000

_ORDER_ITEMS_SELECT = (
    "SELECT oi.*, op.product_id AS product_id FROM order_items oi "
    "LEFT JOIN order_item_products op ON op.item_id = oi.id"
)


def _item_row(row):
    """Строка позиции заказа: мажорная цена + количества float'ом.

    `quantity`/`returned_qty` на Postgres — NUMERIC, и драйвер отдаёт Decimal:
    `json.dumps` на нём падает (500 в любой ручке с позициями), а `Decimal *
    float` в расчётах — TypeError. Приводим на границе чтения, как остатки
    склада; точность хранения остаётся за NUMERIC.
    """
    d = _with_major(row, ("price", "price_cents"))
    if d is not None:
        for key in ("quantity", "returned_qty"):
            if d.get(key) is not None:
                d[key] = float(d[key])
    return d


async def get_order_items(order_id: int) -> list[dict]:
    """asyncpg Stage 19 (#21): native async (fetch)."""
    rows = await adb_core.fetch(f"{_ORDER_ITEMS_SELECT} WHERE oi.order_id = $1", order_id)
    return [_item_row(r) for r in rows]


async def get_order_items_by_ids(order_ids: list[int]) -> dict[int, list[dict]]:
    """Батч-загрузка позиций для списка заказов — один SQL вместо N.
    Возвращает {order_id: [items, ...]}. Заказы без позиций отсутствуют
    в результате (вызывающий должен использовать .get(oid, [])).

    asyncpg Stage 12 (#21): native async; IN-список — $1..$N."""
    if not order_ids:
        return {}
    unique_ids = list(set(order_ids))
    rows: list[dict] = []
    # Пачками: у asyncpg предел — 32 767 параметров на запрос, и «все заказы»
    # руководства за несколько лет в один IN не помещаются.
    for start in range(0, len(unique_ids), _IN_CHUNK):
        chunk = unique_ids[start : start + _IN_CHUNK]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(chunk)))
        rows.extend(
            await adb_core.fetch(
                f"{_ORDER_ITEMS_SELECT} WHERE oi.order_id IN ({placeholders})",
                *chunk,
            )
        )
    grouped: dict[int, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["order_id"], []).append(_item_row(r))
    return grouped


async def get_order_item(item_id: int) -> dict | None:
    """asyncpg Stage 11 (#21): native async. Leaf — внутри database.py
    не вызывается (есть get_order_items / get_order_items_by_ids)."""
    row = await adb_core.fetchrow(f"{_ORDER_ITEMS_SELECT} WHERE oi.id = $1", item_id)
    return _item_row(row)


def remove_order_item(item_id: int, *, require_draft: bool = False) -> bool:
    """`require_draft` — удалять только из черновика (`_draft_locked`)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        if require_draft:
            cur.execute(q("SELECT order_id FROM order_items WHERE id = ?"), (item_id,))
            row = cur.fetchone()
            order_id = (row["order_id"] if hasattr(row, "keys") else row[0]) if row else None
            if order_id is None or not _draft_locked(cur, int(order_id)):
                conn.rollback()
                return False
        cur.execute(q("DELETE FROM order_item_products WHERE item_id = ?"), (item_id,))
        cur.execute(q("DELETE FROM order_items WHERE id = ?"), (item_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    return deleted


# ─── Заявки на отгрузку ───────────────────────────────────────────────────────


def create_shipment_request(order_id: int, user_id: int, full_name: str, comment: str = "") -> int:
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, created_at)
                VALUES (%s, %s, %s, 'pending', %s, %s) RETURNING id
            """,
                (order_id, user_id, full_name, comment, now_str()),
            )
            req_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
            """,
                (order_id, user_id, full_name, comment, now_str()),
            )
            req_id = cur.lastrowid
        conn.commit()
    return req_id


def get_shipment_request(req_id: int) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM shipment_requests WHERE id = ?"), (req_id,))
        row = cur.fetchone()
    return dict(row) if row else None


async def get_pending_requests() -> list[dict]:
    """Заявки на отгрузку в статусе pending (для boss/admin).

    asyncpg-миграция Stage 3 (задача #21): нативный async через adb_core.
    Вызовы — async-контексты (handlers + webapp), cron не вызывает; sync-
    обёртка не нужна.
    """
    return await adb_core.fetch(
        "SELECT * FROM shipment_requests WHERE status = 'pending' ORDER BY created_at DESC"
    )


class ShipmentDecision(NamedTuple):
    """Итог решения по заявке. `applied` — заявка И заказ реально переведены.

    `reason` при отказе:
      • 'request_taken' — заявку уже обработал кто-то другой;
      • 'order_moved'   — заявка была pending, но заказ успел уйти в другой
                          статус (напр. МС прислал Unsuccessful → rejected).
    `order_status` — фактический статус заказа на момент отказа, для сообщения.
    """

    applied: bool
    reason: str | None = None
    order_status: str | None = None
    order_id: int | None = None


def _decide_shipment_request(
    req_id: int,
    by: int,
    by_name: str,
    *,
    req_status: str,
    order_status: str,
    audit_action: str,
    audit_text: str,
    credit_override_by: int | None = None,
) -> ShipmentDecision:
    """Атомарно перевести заявку И заказ. Либо оба, либо ни одного.

    `credit_override_by` — одобрение с превышением кредитного лимита: отметка
    `credit_limit_override` ставится ТЕМ ЖЕ UPDATE'ом заказа. Отдельным
    коммитом после одобрения сбой между ними оставлял одобренный сверх лимита
    заказ без отметки, кто и почему это разрешил.

    Раньше это были две отдельные транзакции, и апдейт заказа шёл БЕЗ guard'а:
    заявка становилась approved, а заказ продавливался в approved из любого
    статуса — отклонённый заказ воскресал и получал новые документы в МС (§2.2).
    Теперь заказ переводится compare-and-set в той же транзакции: не прошёл CAS
    — откатывается и заявка.
    """
    allowed = legal_sources_for(order_status)
    tail, extra = _status_cas_sql(allowed)
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("""UPDATE shipment_requests
               SET status = ?, approved_by = ?, approved_by_name = ?, approved_at = ?
               WHERE id = ? AND status = 'pending'"""),
            (req_status, by, by_name, now_str(), req_id),
        )
        if (cur.rowcount or 0) == 0:
            conn.rollback()
            return ShipmentDecision(False, "request_taken")

        cur.execute(q("SELECT order_id FROM shipment_requests WHERE id = ?"), (req_id,))
        row = cur.fetchone()
        order_id = (row["order_id"] if USE_POSTGRES else row[0]) if row else None
        if order_id is None:
            conn.rollback()
            return ShipmentDecision(False, "request_taken")

        override_sql, override_args = "", []
        if credit_override_by is not None:
            override_sql = ", credit_limit_override = 1, credit_limit_override_by = ?"
            override_args = [credit_override_by]
        cur.execute(
            q(f"UPDATE orders SET status = ?, updated_at = ?{override_sql} WHERE id = ?{tail}"),
            (order_status, now_str(), *override_args, order_id, *extra),
        )
        if (cur.rowcount or 0) == 0:
            # Заказ ушёл из допустимого статуса — откатываем И заявку, иначе
            # останется одобренная заявка при отклонённом заказе.
            cur.execute(q("SELECT status FROM orders WHERE id = ?"), (order_id,))
            r = cur.fetchone()
            actual = (r["status"] if USE_POSTGRES else r[0]) if r else None
            conn.rollback()
            return ShipmentDecision(False, "order_moved", actual, order_id)
        conn.commit()

    req = get_shipment_request(req_id)
    if req is not None:
        add_audit_log(by, by_name, get_role(by), audit_action, audit_text.format(req=req))
    return ShipmentDecision(True, None, order_status, order_id)


def approve_shipment_request(
    req_id: int, approved_by: int, approved_name: str, *, credit_override: bool = False
) -> ShipmentDecision:
    return _decide_shipment_request(
        req_id,
        approved_by,
        approved_name,
        credit_override_by=approved_by if credit_override else None,
        req_status="approved",
        order_status="approved",
        audit_action="shipment_approved",
        audit_text=(
            f"Заявка #{req_id} одобрена "
            "(заказ #{req[order_id]} от {req[full_name]})"
        ),
    )


def reject_shipment_request(
    req_id: int, rejected_by: int, rejected_name: str
) -> ShipmentDecision:
    return _decide_shipment_request(
        req_id,
        rejected_by,
        rejected_name,
        req_status="rejected",
        order_status="rejected",
        audit_action="shipment_rejected",
        audit_text=(
            f"Заявка #{req_id} отклонена "
            "(заказ #{req[order_id]} от {req[full_name]})"
        ),
    )


def mark_shipment_request_returned(req_id: int, returned_by: int, returned_name: str) -> bool:
    """Пометить заявку «возвращённой на доработку» (status='returned'), НЕ трогая
    статус заказа — его уже перевёл в 'draft' reject_order_to_draft. Так заявка
    уходит из get_pending_requests, но заказ живёт и переотправится новой заявкой.

    Атомарный UPDATE ... WHERE status='pending' — идемпотентно, защита от гонки.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("""UPDATE shipment_requests
               SET status = 'returned', approved_by = ?, approved_by_name = ?, approved_at = ?
               WHERE id = ? AND status = 'pending'"""),
            (returned_by, returned_name, now_str(), req_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated:
        req = get_shipment_request(req_id)
        if req is not None:
            add_audit_log(
                returned_by,
                returned_name,
                get_role(returned_by),
                "shipment_returned",
                f"Заявка #{req_id} возвращена на доработку "
                f"(заказ #{req['order_id']} от {req['full_name']})",
            )
    return updated


# ─── Загрузка предопределённых пользователей ──────────────────────────────────


def _load_predefined_users():
    try:
        # MANAGER_IDS импортируем наравне с остальными: config.py определяет её
        # в ОБЕИХ ветках (фолбэк `MANAGER_IDS = []` при config_local и
        # _parse_ids("MANAGER_IDS") при env). Отдельный try/__import__/except
        # вокруг неё маскировал бы реальную ошибку импорта конфига под «нет
        # менеджеров».
        from config import ADMIN_IDS, BOSS_IDS, MANAGER_IDS

        with get_conn() as conn:
            cur = get_cursor(conn)
            for uid in ADMIN_IDS:
                cur.execute(
                    q(
                        "INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, 'Admin', 'admin', ?) ON CONFLICT(user_id) DO NOTHING"
                    ),
                    (uid, "", now_str()),
                )
            for uid in BOSS_IDS:
                cur.execute(
                    q(
                        "INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, 'Boss', 'boss', ?) ON CONFLICT(user_id) DO NOTHING"
                    ),
                    (uid, "", now_str()),
                )
            for uid in MANAGER_IDS:
                cur.execute(
                    q(
                        "INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, 'Manager', 'manager', ?) ON CONFLICT(user_id) DO NOTHING"
                    ),
                    (uid, "", now_str()),
                )
            conn.commit()
        logger.info("Предопределённые пользователи загружены")
    except Exception as e:
        logger.warning("Ошибка загрузки пользователей: %s", e)
