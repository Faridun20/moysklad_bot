"""
База данных — SQLite (локально) и PostgreSQL (продакшен).
"""

import os
import time
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Any

from services import money  # канонические деньги (копейки); leaf-модуль, без циклов
from services import adb_core  # async DB-слой (asyncpg/aiosqlite); leaf-модуль, без циклов

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
import tempfile

_default_db = os.path.join(tempfile.gettempdir(), "payments.db")
DB_PATH = os.environ.get("DB_PATH", _default_db)
USE_POSTGRES = bool(DATABASE_URL)

# Логировать запросы дольше N мс (предупреждение).
# 0 — выключено. Управляется переменной окружения SQL_SLOW_MS.
SQL_SLOW_MS = float(os.environ.get("SQL_SLOW_MS", "200"))

if USE_POSTGRES:
    from psycopg2 import pool as _pg_pool
    from psycopg2.extras import RealDictCursor

    logger.info("Используется PostgreSQL")

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
    _pg_connection_pool: _pg_pool.ThreadedConnectionPool | None = None

    def _get_pool() -> _pg_pool.ThreadedConnectionPool:
        """Ленивая инициализация пула — позволяет импортировать модуль
        в окружениях без DATABASE_URL (тесты, миграции) без падения."""
        global _pg_connection_pool
        if _pg_connection_pool is None:
            _pg_connection_pool = _pg_pool.ThreadedConnectionPool(
                _PG_POOL_MIN, _PG_POOL_MAX, DATABASE_URL
            )
            logger.info(
                "Postgres pool создан: min=%d, max=%d",
                _PG_POOL_MIN,
                _PG_POOL_MAX,
            )
        return _pg_connection_pool

    def _pool_getconn():
        """getconn с ожиданием: при исчерпании пула ждём до
        _PG_POOL_ACQUIRE_TIMEOUT сек, опрашивая раз в _PG_POOL_ACQUIRE_INTERVAL,
        вместо мгновенного PoolError → 500. Выполняется в worker-потоке
        (asyncio.to_thread), поэтому time.sleep не блокирует event loop.
        По истечении таймаута пробрасываем PoolError."""
        pool = _get_pool()
        deadline = time.monotonic() + _PG_POOL_ACQUIRE_TIMEOUT
        waited = False
        while True:
            try:
                return pool.getconn()
            except _pg_pool.PoolError:
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
else:
    import sqlite3

    logger.info("Используется SQLite: %s", DB_PATH)


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
        conn = sqlite3.connect(DB_PATH)
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
    """Сбрасываем кэш ролей. Лениво импортируем services.roles, иначе
    круговой импорт (roles уже зависит от database)."""
    try:
        from services.roles import invalidate_role

        invalidate_role(user_id)
    except Exception:
        # Кэш — мягкий, рассинхрон протухнет через TTL за 60 сек.
        # Не валим write-операцию из-за проблем с кэшем.
        pass


# ─── Инициализация ────────────────────────────────────────────────────────────


def init_db():
    """Гарантирует что схема существует. Безопасно вызывать из любого
    процесса при старте — все DDL идут через `CREATE TABLE IF NOT EXISTS`.

    ВНИМАНИЕ: миграции (ALTER TABLE ADD COLUMN), backfill'ы и recovery —
    больше НЕ часть init_db. Они вынесены в `tasks/migrate.py` и должны
    запускаться отдельным процессом ПЕРЕД стартом bot/webapp/cron.
    Это закрывает H4: при rolling deploy нескольких процессов ALTER
    TABLE гонялся одновременно и DDL/UPDATE recovery конфликтовали.

    Если ты разрабатываешь локально (свежая SQLite-БД) — init_db
    достаточно: миграции применяются к свежей схеме сразу через
    CREATE TABLE с полным списком колонок.

    Для прод-старта используй: `python -m tasks.migrate` перед
    `python bot.py`. На Railway: `tasks/migrate.py` в pre-start
    команде сервиса, или отдельный Cron Job «one-shot».
    """
    _create_tables()
    _create_indexes()
    _seed_currency_rates()
    _load_predefined_users()
    logger.info("База данных инициализирована (CREATE TABLE only)")


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


def _create_tables():
    """Только CREATE TABLE IF NOT EXISTS. Idempotent, безопасен
    при concurrent старте."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        id_type = "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

        tables = [
            """CREATE TABLE IF NOT EXISTS user_roles (
                user_id              BIGINT PRIMARY KEY,
                username             TEXT,
                full_name            TEXT,
                role                 TEXT NOT NULL DEFAULT 'manager',
                moysklad_employee_id TEXT,
                ms_sync_status       TEXT DEFAULT 'pending',
                created_at           TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS payments (
                id              {id_type},
                user_id         BIGINT NOT NULL,
                username        TEXT,
                full_name       TEXT,
                amount          REAL NOT NULL,
                amount_cents    BIGINT,
                currency        TEXT NOT NULL DEFAULT 'USD',
                comment         TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                order_id        BIGINT,
                ms_paymentin_id TEXT,
                ms_sync_status  TEXT,
                ms_sync_error   TEXT,
                created_at      TEXT NOT NULL,
                confirmed_at    TEXT
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
            f"""CREATE TABLE IF NOT EXISTS orders (
                id                      {id_type},
                user_id                 BIGINT NOT NULL,
                full_name               TEXT,
                status                  TEXT NOT NULL DEFAULT 'draft',
                comment                 TEXT,
                agent_id                TEXT,
                agent_name              TEXT,
                currency                TEXT,
                payment_type            TEXT NOT NULL DEFAULT 'paid',
                due_date                TEXT,
                paid_at                 TEXT,
                paid_confirmed_at       TEXT,
                paid_confirmed_by       BIGINT,
                paid_confirmed_by_name  TEXT,
                ms_demand_id            TEXT,
                ms_customerorder_id     TEXT,
                created_at              TEXT NOT NULL,
                updated_at              TEXT NOT NULL
            )""",
            f"""CREATE TABLE IF NOT EXISTS order_items (
                id           {id_type},
                order_id     BIGINT NOT NULL,
                product_name TEXT NOT NULL,
                product_href TEXT,
                quantity     REAL NOT NULL DEFAULT 1,
                unit         TEXT DEFAULT 'шт',
                price        REAL DEFAULT 0,
                price_cents  BIGINT,
                note         TEXT
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
            # ─── Snapshot МойСклад (локальная копия справочников + остатков) ──
            # Идея: справочники качаем раз в день, остатки — каждые 2 часа
            # как safety-net + точечная инвалидация через вебхуки.
            # Поле ms_id хранит UUID из МойСклад (PRIMARY KEY).
            """CREATE TABLE IF NOT EXISTS ms_products (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                folder_id  TEXT,
                code       TEXT,
                unit       TEXT,
                href       TEXT,
                updated_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS ms_categories (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                parent_id  TEXT,
                href       TEXT,
                updated_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS ms_counterparties (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                phone      TEXT,
                href       TEXT,
                updated_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS ms_employees (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                href       TEXT,
                updated_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS ms_stock (
                ms_id       TEXT PRIMARY KEY,
                name        TEXT,
                folder_id   TEXT,
                folder_name TEXT,
                unit        TEXT,
                stock       REAL DEFAULT 0,
                reserve     REAL DEFAULT 0,
                updated_at  TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS ms_snapshot_meta (
                dataset           TEXT PRIMARY KEY,
                last_refresh      TEXT,
                last_full_refresh TEXT,
                last_webhook_at   TEXT,
                rows_count        INTEGER DEFAULT 0,
                status            TEXT
            )""",
            # Дедуп уведомлений об отгрузках: и MS-вебхук (webapp-процесс), и
            # поллер (bot-процесс) пишут сюда demand_id перед отправкой. PRIMARY
            # KEY + INSERT-if-absent гарантируют ровно одно уведомление на demand
            # независимо от того, какой процесс успел первым.
            """CREATE TABLE IF NOT EXISTS notified_shipments (
                demand_id   TEXT PRIMARY KEY,
                notified_at TEXT
            )""",
            # Round 6 RACE-4: idempotency-guard для ops_monitor cron.
            # PRIMARY KEY (run_date) + INSERT-if-absent через `claim_ops_monitor_run`
            # — параллельный/повторный запуск за тот же день делает noop.
            """CREATE TABLE IF NOT EXISTS ops_monitor_runs (
                run_date   TEXT PRIMARY KEY,
                started_at TEXT
            )""",
            # ─── IMPLEMENTATION.md Фаза 1–2 (адаптировано под dual-DB) ──────────
            # Конвенции проекта: TEXT для JSON/UUID/timestamp, REAL для денег,
            # INTEGER 0/1 для boolean, BIGINT — telegram user_id, без FK
            # (как и остальные таблицы здесь). Postgres-специфику (JSONB,
            # gen_random_uuid, NUMERIC) НЕ используем — иначе ломается SQLite.
            # Кредитный лимит контрагента. agent_id — UUID контрагента МойСклад.
            """CREATE TABLE IF NOT EXISTS credit_limits (
                agent_id     TEXT PRIMARY KEY,
                agent_name   TEXT NOT NULL,
                limit_amount REAL NOT NULL DEFAULT 2000.0,
                limit_amount_cents BIGINT,
                set_by       BIGINT,
                notes        TEXT,
                updated_at   TEXT,
                created_at   TEXT
            )""",
            # Сдача наличных в кассу (manager → касса). status: pending|confirmed|rejected.
            f"""CREATE TABLE IF NOT EXISTS cash_deposits (
                id           {id_type},
                manager_id   BIGINT NOT NULL,
                amount       REAL NOT NULL,
                amount_cents BIGINT,
                deposited_at TEXT,
                confirmed_by BIGINT,
                confirmed_at TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                reject_reason TEXT,
                notes        TEXT,
                deleted_at   TEXT,
                created_at   TEXT
            )""",
            # Распределение одной сдачи по заказам (composite PK).
            """CREATE TABLE IF NOT EXISTS cash_deposit_orders (
                deposit_id       BIGINT NOT NULL,
                order_id         BIGINT NOT NULL,
                amount_allocated REAL NOT NULL,
                amount_allocated_cents BIGINT,
                is_manual        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (deposit_id, order_id)
            )""",
            # Возвраты товара. return_type: partial|full. status: pending|confirmed|rejected.
            f"""CREATE TABLE IF NOT EXISTS returns (
                id           {id_type},
                order_id     BIGINT NOT NULL,
                return_type  TEXT NOT NULL,
                reason       TEXT NOT NULL,
                total_amount REAL NOT NULL,
                total_amount_cents BIGINT,
                refund_method TEXT,
                moysklad_return_id TEXT,
                created_by   BIGINT NOT NULL,
                confirmed_by BIGINT,
                status       TEXT NOT NULL DEFAULT 'pending',
                goods_received INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT,
                confirmed_at TEXT,
                deleted_at   TEXT
            )""",
            f"""CREATE TABLE IF NOT EXISTS return_items (
                id            {id_type},
                return_id     BIGINT NOT NULL,
                order_item_id BIGINT NOT NULL,
                qty           REAL NOT NULL,
                amount        REAL NOT NULL,
                amount_cents  BIGINT
            )""",
            # Партии товара (FEFO). Используется только если МойСклад
            # поддерживает партии для товара; иначе order_items.batch_id NULL.
            """CREATE TABLE IF NOT EXISTS product_batches (
                id                TEXT PRIMARY KEY,
                product_id        TEXT NOT NULL,
                moysklad_batch_id TEXT,
                batch_code        TEXT,
                expiry_date       TEXT,
                qty_remaining     REAL NOT NULL DEFAULT 0,
                received_at       TEXT,
                updated_at        TEXT
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
            # Недоставленные уведомления (для retry-крона). channel: telegram|email|sms.
            f"""CREATE TABLE IF NOT EXISTS failed_notifications (
                id                {id_type},
                user_id           BIGINT NOT NULL,
                notification_type TEXT NOT NULL,
                channel           TEXT NOT NULL,
                payload           TEXT NOT NULL,
                attempts          INTEGER NOT NULL DEFAULT 0,
                last_attempt_at   TEXT,
                last_error        TEXT,
                is_critical       INTEGER NOT NULL DEFAULT 0,
                resolved_at       TEXT,
                resolved_by       BIGINT,
                created_at        TEXT
            )""",
            # Журнал выгрузок audit_log в Google Drive (интеграция — позже).
            f"""CREATE TABLE IF NOT EXISTS audit_archive_exports (
                id              {id_type},
                period_start    TEXT NOT NULL,
                period_end      TEXT NOT NULL,
                file_name       TEXT NOT NULL,
                drive_file_id   TEXT NOT NULL,
                drive_file_url  TEXT NOT NULL,
                records_count   INTEGER NOT NULL,
                file_size_bytes BIGINT NOT NULL,
                exported_at     TEXT,
                exported_by     BIGINT
            )""",
            # Контакты клиента для уведомлений (opt-in).
            """CREATE TABLE IF NOT EXISTS client_contacts (
                agent_id              TEXT PRIMARY KEY,
                telegram_chat_id      BIGINT,
                email                 TEXT,
                phone                 TEXT,
                notifications_opted_in INTEGER NOT NULL DEFAULT 0,
                opted_in_at           TEXT,
                opted_in_by           BIGINT,
                created_at            TEXT,
                updated_at            TEXT
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
            # Курсы валют → к BASE_CURRENCY (USD по умолчанию). Записываются
            # вручную через /api/currency/rates админом. Историю изменений
            # не храним (rate_at_payment-точность — отдельная задача), здесь
            # «текущий рыночный курс» для UI-сводок.
            """CREATE TABLE IF NOT EXISTS currency_rates (
                currency_code TEXT PRIMARY KEY,
                rate_to_base  REAL NOT NULL,
                updated_at    TEXT,
                updated_by    BIGINT
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
            # Цены товаров, выставленные руководством (PR C).
            # sale_price — минимальная цена продажи: при добавлении товара
            #   в заказ менеджер может поднять, но не опустить ниже.
            # cost_price — себестоимость: видна ТОЛЬКО boss/admin, нужна
            #   для расчёта прибыли. Может быть NULL (не задана).
            # Источник истины — руководство (не МС): задаётся через
            #   /api/products/prices/set.
            """CREATE TABLE IF NOT EXISTS product_prices (
                ms_id         TEXT PRIMARY KEY,
                product_name  TEXT,
                sale_price    REAL,
                sale_price_cents BIGINT,
                cost_price    REAL,
                cost_price_cents BIGINT,
                currency      TEXT,
                updated_by    BIGINT,
                updated_at    TEXT
            )""",
        ]

        # Создаём каждую таблицу в отдельной транзакции
        for sql in tables:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("Таблица уже существует или ошибка: %s", e)


def _create_indexes():
    """CREATE INDEX IF NOT EXISTS — idempotent. Можно гонять при каждом
    старте, postgres и sqlite оба корректно работают."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        snapshot_indexes = [
            "CREATE INDEX IF NOT EXISTS idx_ms_products_folder ON ms_products(folder_id)",
            "CREATE INDEX IF NOT EXISTS idx_ms_stock_folder ON ms_stock(folder_id)",
            "CREATE INDEX IF NOT EXISTS idx_ms_categories_parent ON ms_categories(parent_id)",
            # Каждое утреннее уведомление о долгах сканирует все credit-заказы
            # с paid_at IS NULL. Без индекса — full scan по orders, при тысячах
            # записей это секунды. Составной индекс покрывает фильтр и сортировку.
            "CREATE INDEX IF NOT EXISTS idx_orders_credit_due ON orders(payment_type, paid_at, due_date)",
            # Для запросов «все платежи по заказу» — без него полный скан payments.
            "CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id)",
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
            "CREATE INDEX IF NOT EXISTS idx_batches_product_expiry ON product_batches(product_id, expiry_date)",
            "CREATE INDEX IF NOT EXISTS idx_change_log_order ON order_change_log(order_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_failed_notif_unresolved ON failed_notifications(is_critical, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_credit_limits_updated_at ON credit_limits(updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_idempotency_expires ON idempotency_keys(expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_task_started ON cron_runs(task_name, started_at)",
        ]
        for sql in snapshot_indexes:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.debug("Индекс не создан: %s", e)


def run_migrations():
    """ALTER TABLE ADD COLUMN — догоняем старые БД до текущей схемы.

    ВЫНЕСЕНО ИЗ init_db (SECURITY.md H4): раньше эти ALTER гонялись на
    каждом старте каждого процесса. При rolling deploy bot+webapp одно-
    временные DDL вступали в race с UPDATE-запросами от уже работающих
    транзакций. Теперь: вызывается явно из `tasks/migrate.py` перед
    стартом сервисов.

    Идемпотентно: ADD COLUMN если колонка уже есть → SQL ошибка,
    мы её ловим и идём дальше.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        migrations = [
            ("user_roles", "moysklad_employee_id", "TEXT"),
            ("user_roles", "ms_sync_status", "TEXT DEFAULT 'pending'"),
            ("user_roles", "created_at", "TEXT"),
            # Цена за единицу для позиции заказа (в основной валюте,
            # т.е. как пользователь ввёл — например 150.50 USD).
            # При создании demand в МойСклад умножаем на 100 (минорные единицы).
            ("order_items", "price", "REAL DEFAULT 0"),
            # Валюта заказа (USD/UZS/RUB/EUR). По умолчанию BASE_CURRENCY.
            # Хранится на уровне ордера, чтобы все позиции одного заказа
            # были в одной валюте.
            ("orders", "currency", "TEXT"),
            # Тип оплаты: 'paid' (оплачено сразу) или 'credit' (в долг).
            # Default 'paid' — все старые заказы считаем как оплаченные,
            # чтобы миграция была безопасной (не объявить вдруг весь
            # архив должниками).
            ("orders", "payment_type", "TEXT NOT NULL DEFAULT 'paid'"),
            # Дата к которой клиент обязался погасить долг (ISO YYYY-MM-DD).
            # Заполняется только когда payment_type='credit', NULL иначе.
            ("orders", "due_date", "TEXT"),
            # Когда долг был погашен (ISO YYYY-MM-DD HH:MM:SS). NULL пока
            # не погашен. Для 'paid' заказов также NULL — там оплата
            # сразу, отдельный timestamp не нужен (есть created_at).
            ("orders", "paid_at", "TEXT"),
            # Двухступенчатое подтверждение оплаты:
            #  - paid_at:           менеджер отметил «деньги получил»
            #  - paid_confirmed_*:  босс/админ подтвердил «да, в кассе»
            # Заказ считается реально оплаченным ТОЛЬКО когда оба поля
            # заполнены. Если босс отклонил — paid_at обнуляется (см.
            # reject_payment_received), цикл начинается заново.
            ("orders", "paid_confirmed_at", "TEXT"),
            ("orders", "paid_confirmed_by", "BIGINT"),
            ("orders", "paid_confirmed_by_name", "TEXT"),
            # Связь платежа с заказом. Если payment.order_id IS NOT NULL —
            # это «частичная оплата по заказу N», а не самостоятельный платёж
            # в кассу. У одного заказа может быть несколько payments
            # (клиент платит частями). Когда суммa confirmed payments >=
            # order.total, заказ автоматически считается закрытым.
            ("payments", "order_id", "BIGINT"),
            # ID документа Demand в МойСклад, созданного при approve
            # отгрузки. Нужен чтобы paymentin привязывался к конкретной
            # отгрузке (operations field в API МойСклад). NULL если
            # отгрузка ещё не отправлена или create_demand упал.
            # LEGACY: новые заказы используют ms_customerorder_id ниже.
            ("orders", "ms_demand_id", "TEXT"),
            # ID «Заказа покупателя» (customerorder) в МойСклад.
            # Новый workflow — бот создаёт именно customerorder, а не
            # demand. paymentin привязывается сюда через operations
            # вместо ms_demand_id для новых заказов.
            ("orders", "ms_customerorder_id", "TEXT"),
            # ID входящего платежа (paymentin) в МойСклад. Заполняется
            # после успешного create_paymentin. Защищает от дубликатов:
            # повторный confirm не плодит новые paymentin'ы в МойСклад.
            ("payments", "ms_paymentin_id", "TEXT"),
            # Статус синхронизации с МойСклад: NULL (ещё не пробовали),
            # 'synced', 'failed' (с описанием в ms_sync_error).
            ("payments", "ms_sync_status", "TEXT"),
            ("payments", "ms_sync_error", "TEXT"),
            # ─── IMPLEMENTATION.md Фаза 2 (адаптировано: BOOLEAN→INTEGER 0/1,
            #     JSONB→TEXT, NUMERIC→REAL, без FK). Все колонки аддитивны. ──────
            # users → у нас user_roles (telegram-id как PK).
            ("user_roles", "active", "INTEGER NOT NULL DEFAULT 1"),
            ("user_roles", "email", "TEXT"),
            ("user_roles", "phone", "TEXT"),
            ("user_roles", "deactivated_at", "TEXT"),
            ("user_roles", "deactivated_by", "BIGINT"),
            # orders
            ("orders", "deleted_at", "TEXT"),
            ("orders", "rejection_comment", "TEXT"),
            ("orders", "rejection_count", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "frozen", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "cancellation_deadline", "TEXT"),
            ("orders", "cancelled_at", "TEXT"),
            ("orders", "cancelled_by", "BIGINT"),
            ("orders", "cancellation_reason", "TEXT"),
            # Когда отмена была отражена в МойСклад (реверс customerorder).
            # NULL = ещё не синхронизировано; идемпотентность ms_cancel.
            ("orders", "ms_cancel_synced_at", "TEXT"),
            ("orders", "credit_limit_override", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "credit_limit_override_by", "BIGINT"),
            ("orders", "price_check_warnings", "TEXT"),
            ("orders", "payment_confirmed", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "payment_confirmed_at", "TEXT"),
            ("orders", "client_notification_sent", "INTEGER NOT NULL DEFAULT 0"),
            ("orders", "return_status", "TEXT"),
            ("orders", "submitted_at", "TEXT"),
            ("orders", "approved_by", "BIGINT"),
            ("orders", "approved_at", "TEXT"),
            ("orders", "shipped_at", "TEXT"),
            ("orders", "shipped_by", "BIGINT"),
            # order_items
            ("order_items", "stock_snap", "REAL"),
            ("order_items", "price_at_submit", "REAL"),
            ("order_items", "batch_id", "TEXT"),
            ("order_items", "returned_qty", "REAL NOT NULL DEFAULT 0"),
            # ─── Деньги в копейках (минорные единицы) — канон вместо float.
            #     Аддитивные BIGINT-колонки рядом со старыми REAL; backfill
            #     в run_backfills (x_cents = round(x*100)). Старые REAL пока
            #     остаются для безопасного rolling-деплоя. См. services/money.py.
            ("payments", "amount_cents", "BIGINT"),
            ("order_items", "price_cents", "BIGINT"),
            ("order_items", "price_at_submit_cents", "BIGINT"),
            ("credit_limits", "limit_amount_cents", "BIGINT"),
            ("cash_deposits", "amount_cents", "BIGINT"),
            ("cash_deposit_orders", "amount_allocated_cents", "BIGINT"),
            ("returns", "total_amount_cents", "BIGINT"),
            ("return_items", "amount_cents", "BIGINT"),
            ("product_prices", "sale_price_cents", "BIGINT"),
            ("product_prices", "cost_price_cents", "BIGINT"),
        ]
        applied = 0
        for table, column, col_type in migrations:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                conn.commit()
                applied += 1
            except Exception:
                conn.rollback()  # Колонка уже существует — норм
        logger.info("run_migrations: применено %d из %d", applied, len(migrations))


def run_backfills():
    """Одноразовые data-миграции. Идемпотентны.

    1. Закрыть legacy-долги (paid_at стоит, payments записей нет —
       значит это до partial-payments эпохи): paid_confirmed_at = paid_at.
    2. Recovery от старого backfill-бага: если paid_confirmed_at стоит,
       но сумма confirmed payments меньше total, сбросить confirmed_at
       обратно в NULL.

    Запускается из `tasks/migrate.py`. НЕ из init_db — этот код пишет
    данные, не должен бежать при каждом старте сервиса.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        # ── Backfill legacy ──────────────────────────────────────────
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
            rows = cur.rowcount
            conn.commit()
            if rows > 0:
                logger.info("Backfill legacy: %d закрытых долгов автоподтверждены", rows)
        except Exception as e:
            conn.rollback()
            logger.warning("Backfill paid_confirmed: %s", e)

        # ── Recovery ─────────────────────────────────────────────────
        try:
            cur.execute(
                "UPDATE orders "
                "SET paid_confirmed_at = NULL, "
                "    paid_confirmed_by = NULL, "
                "    paid_confirmed_by_name = NULL "
                "WHERE paid_confirmed_at IS NOT NULL "
                "  AND EXISTS (SELECT 1 FROM payments WHERE order_id = orders.id) "
                "  AND ("
                "    (SELECT COALESCE(SUM(amount), 0) FROM payments "
                "     WHERE order_id = orders.id AND status = 'confirmed')"
                "    <"
                "    (SELECT COALESCE(SUM(quantity * price), 0) FROM order_items "
                "     WHERE order_id = orders.id) - 0.01"
                "  )"
            )
            rows = cur.rowcount
            conn.commit()
            if rows > 0:
                logger.warning(
                    "Recovery: %d ошибочно закрытых долгов восстановлены "
                    "(см. историю — backfill закрыл частично оплаченные)",
                    rows,
                )
        except Exception as e:
            conn.rollback()
            logger.warning("Recovery paid_confirmed: %s", e)

        # ── Деньги float → копейки ───────────────────────────────────
        # Заполняем новые *_cents BIGINT из старых REAL: cents = round(x*100).
        # Идемпотентно (WHERE dst IS NULL). round() есть и в SQLite, и в
        # Postgres. Имена таблиц/колонок статические — не пользовательский ввод.
        _money_cols = [
            ("payments", "amount", "amount_cents"),
            ("order_items", "price", "price_cents"),
            ("order_items", "price_at_submit", "price_at_submit_cents"),
            ("credit_limits", "limit_amount", "limit_amount_cents"),
            ("cash_deposits", "amount", "amount_cents"),
            ("cash_deposit_orders", "amount_allocated", "amount_allocated_cents"),
            ("returns", "total_amount", "total_amount_cents"),
            ("return_items", "amount", "amount_cents"),
            ("product_prices", "sale_price", "sale_price_cents"),
            ("product_prices", "cost_price", "cost_price_cents"),
        ]
        for _table, _src, _dst in _money_cols:
            try:
                cur.execute(
                    f"UPDATE {_table} SET {_dst} = CAST(round({_src} * 100) AS INTEGER) "
                    f"WHERE {_dst} IS NULL AND {_src} IS NOT NULL"
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("Backfill cents %s.%s: %s", _table, _dst, e)

    # ── Сидинг app_settings (идемпотентно) ───────────────────────────
    seed_app_settings()


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
    "soft_delete_retention_days": (365, "Через сколько дней soft-deleted удаляется физически"),
    "moysklad_retry_max_attempts": (3, "Макс попыток для МойСклад API"),
    "moysklad_circuit_breaker_threshold": (10, "Сколько фейлов за 5 мин → пауза"),
    "client_notifications_enabled": (True, "Глобальный switch уведомлений клиентам"),
    "return_deadline_days": (90, "Лимит на оформление возврата (дней с отгрузки)"),
    "auto_create_demand_on_approve": (True, "Создавать demand в МойСклад при approve"),
    "auto_ship_on_approve": (True, "Авто-переход в shipped сразу после approve"),
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


def get_credit_limit(agent_id: str) -> float:
    """Лимит контрагента; если строки нет — дефолт из app_settings."""
    if not agent_id:
        return float(get_setting("credit_limit_default", 2000.0))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT limit_amount FROM credit_limits WHERE agent_id = ?"), (agent_id,))
        row = cur.fetchone()
    if row:
        return float(row["limit_amount"] if USE_POSTGRES else row[0])
    return float(get_setting("credit_limit_default", 2000.0))


def ensure_credit_limit(agent_id: str, agent_name: str) -> None:
    """Завести строку лимита для нового клиента (set_by=NULL → «авто»)."""
    if not agent_id:
        return
    default = float(get_setting("credit_limit_default", 2000.0))
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO credit_limits (agent_id, agent_name, limit_amount, set_by, created_at, updated_at) "
                "VALUES (%s, %s, %s, NULL, %s, %s) ON CONFLICT (agent_id) DO NOTHING",
                (agent_id, agent_name, default, now_str(), now_str()),
            )
        else:
            cur.execute(
                "INSERT OR IGNORE INTO credit_limits (agent_id, agent_name, limit_amount, set_by, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (agent_id, agent_name, default, now_str(), now_str()),
            )
        conn.commit()


def set_credit_limit(
    agent_id: str,
    agent_name: str,
    limit_amount: float,
    set_by: int | None = None,
    notes: str | None = None,
) -> None:
    """Установить/изменить лимит + запись в audit_log."""
    limit_cents = money.to_cents(limit_amount or 0)
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO credit_limits (agent_id, agent_name, limit_amount, limit_amount_cents, set_by, notes, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (agent_id) DO UPDATE SET limit_amount = EXCLUDED.limit_amount, "
                "limit_amount_cents = EXCLUDED.limit_amount_cents, "
                "set_by = EXCLUDED.set_by, notes = EXCLUDED.notes, updated_at = EXCLUDED.updated_at",
                (agent_id, agent_name, limit_amount, limit_cents, set_by, notes, now_str(), now_str()),
            )
        else:
            cur.execute(
                "INSERT INTO credit_limits (agent_id, agent_name, limit_amount, limit_amount_cents, set_by, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET limit_amount = excluded.limit_amount, "
                "limit_amount_cents = excluded.limit_amount_cents, "
                "set_by = excluded.set_by, notes = excluded.notes, updated_at = excluded.updated_at",
                (agent_id, agent_name, limit_amount, limit_cents, set_by, notes, now_str(), now_str()),
            )
        conn.commit()
    if set_by:
        add_audit_log(
            set_by,
            "",
            get_role(set_by),
            "credit_limit_changed",
            f"{agent_name}: лимит → {limit_amount:.0f} USD" + (f" ({notes})" if notes else ""),
        )


def get_agent_current_debt(agent_id: str) -> float:
    """Текущий долг контрагента: сумма непогашенных остатков по его открытым
    заказам минус подтверждённые возвраты. Открытые = не draft/rejected/
    cancelled и не soft-deleted."""
    if not agent_id:
        return 0.0
    with get_conn() as conn:
        cur = get_cursor(conn)
        # Исключаем неактуальные: черновики/отклонённые/отменённые, полностью
        # оплаченные (в т.ч. через cash deposit → payment_confirmed=1 /
        # status='paid') и полностью возвращённые.
        cur.execute(
            q(
                "SELECT id FROM orders WHERE agent_id = ? "
                "AND status NOT IN ('draft', 'rejected', 'cancelled', 'paid', 'returned') "
                "AND payment_confirmed = 0 AND (deleted_at IS NULL)"
            ),
            (agent_id,),
        )
        order_ids = [(r["id"] if USE_POSTGRES else r[0]) for r in cur.fetchall()]
    debt_cents = 0
    for oid in order_ids:
        summary = get_order_payment_summary(oid)
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q(
                    f"SELECT {_SUM_RETURNS_CENTS} AS s FROM returns "
                    "WHERE order_id = ? AND status = 'confirmed' AND (deleted_at IS NULL)"
                ),
                (oid,),
            )
            row = cur.fetchone()
        returns_cents = int((row["s"] if USE_POSTGRES else row[0]) or 0)
        debt_cents += max(0, int(summary.get("remaining_cents", 0)) - returns_cents)
    return float(money.from_cents(debt_cents))


def check_credit_limit(agent_id: str, order_total: float) -> dict:
    """Проверка лимита для нового заказа. НЕ блокирует — даёт данные для
    решения боса (over_limit + цифры)."""
    debt = get_agent_current_debt(agent_id)
    limit = get_credit_limit(agent_id)
    projected = debt + order_total
    return {
        "current_debt": debt,
        "limit": limit,
        "projected": projected,
        "over_limit": projected > limit,
    }


def _confirmed_returns_by_order(order_ids: list[int]) -> dict[int, float]:
    """{order_id: сумма подтверждённых (не удалённых) возвратов}. Фильтр идентичен
    тому, что в get_agent_current_debt — используется для батч-расчёта долга."""
    if not order_ids:
        return {}
    unique_ids = list(set(order_ids))
    placeholders = ",".join(["?"] * len(unique_ids))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                f"SELECT order_id, COALESCE(SUM(total_amount), 0) AS s FROM returns "
                f"WHERE order_id IN ({placeholders}) AND status = 'confirmed' "
                f"AND (deleted_at IS NULL) GROUP BY order_id"
            ),
            unique_ids,
        )
        rows = cur.fetchall()
    result: dict[int, float] = {}
    for r in rows:
        oid = r["order_id"] if USE_POSTGRES else r[0]
        result[oid] = float((r["s"] if USE_POSTGRES else r[1]) or 0)
    return result


def get_credit_overview() -> list[dict]:
    """Сводка по контрагентам для боса: лимит + текущий долг. Объединяет
    строки credit_limits и контрагентов из активных заказов (даже без явной
    строки лимита — у них дефолтный лимит). Сортировка: сначала те, кто ближе
    к лимиту/превысил.

    Батч-версия (без N+1): раньше на каждого агента звался get_agent_current_debt,
    а тот — get_order_payment_summary на КАЖДЫЙ заказ (≈ A×(1+4N) запросов). Теперь
    долг считается из 4 групповых выборок, клампинг — в Python (логика тождественна
    get_agent_current_debt; держим её тут, чтобы не плодить кросс-БД GREATEST/MAX)."""
    agents: dict[str, str] = {}
    limits_map: dict[str, float] = {}
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("SELECT agent_id, agent_name, limit_amount FROM credit_limits")
        for r in cur.fetchall():
            aid = r["agent_id"] if USE_POSTGRES else r[0]
            if not aid:
                continue
            agents[aid] = (r["agent_name"] if USE_POSTGRES else r[1]) or aid
            limits_map[aid] = float(r["limit_amount"] if USE_POSTGRES else r[2])
        cur.execute(
            "SELECT DISTINCT agent_id, agent_name FROM orders "
            "WHERE agent_id IS NOT NULL AND agent_id != '' "
            "AND status NOT IN ('draft', 'rejected', 'cancelled') AND (deleted_at IS NULL)"
        )
        for r in cur.fetchall():
            aid = r["agent_id"] if USE_POSTGRES else r[0]
            if aid and aid not in agents:
                agents[aid] = (r["agent_name"] if USE_POSTGRES else r[1]) or aid

        # Открытые заказы (фильтр идентичен get_agent_current_debt) — один запрос
        # на всех агентов; долг по ним разбираем в Python.
        cur.execute(
            "SELECT id, agent_id FROM orders "
            "WHERE status NOT IN ('draft', 'rejected', 'cancelled', 'paid', 'returned') "
            "AND payment_confirmed = 0 AND (deleted_at IS NULL)"
        )
        open_rows = [
            ((r["id"] if USE_POSTGRES else r[0]), (r["agent_id"] if USE_POSTGRES else r[1]))
            for r in cur.fetchall()
        ]

    open_ids = [oid for oid, _ in open_rows]
    items_by_order = get_order_items_by_ids(open_ids)
    payments_by_order = get_payments_for_orders(open_ids)
    returns_by_order = _confirmed_returns_by_order(open_ids)
    default_limit = float(get_setting("credit_limit_default", 2000.0))

    # debt[aid] = Σ по открытым заказам max(0, max(0, total−confirmed) − returns)
    # — бит-в-бит как цикл в get_agent_current_debt.
    debt_by_agent: dict[str, float] = {}
    for oid, aid in open_rows:
        items = items_by_order.get(oid, [])
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        payments = payments_by_order.get(oid, [])
        confirmed = sum(p["amount"] for p in payments if p["status"] == "confirmed")
        returns_sum = returns_by_order.get(oid, 0.0)
        order_debt = max(0.0, max(0.0, total - confirmed) - returns_sum)
        if order_debt:
            debt_by_agent[aid] = debt_by_agent.get(aid, 0.0) + order_debt

    out: list[dict[str, Any]] = []
    for aid, name in agents.items():
        debt = debt_by_agent.get(aid, 0.0)
        limit = limits_map.get(aid, default_limit)
        out.append(
            {
                "agent_id": aid,
                "agent_name": name,
                "limit": limit,
                "debt": debt,
                "free": limit - debt,
                "over_limit": debt > limit,
            }
        )
    out.sort(key=lambda a: float(a["free"]))
    return out


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


def reject_order_to_draft(
    order_id: int,
    rejected_by: int,
    rejected_name: str,
    comment: str,
) -> dict:
    """Reject заявки по модели IMPLEMENTATION.md §6.4: заказ возвращается в
    draft с комментарием, счётчик отклонений растёт, после reject_max_cycles
    заказ замораживается (frozen=1, resubmit запрещён до разморозки админом).

    Атомарный UPDATE ... WHERE status='pending' — защита от гонки.
    Возвращает {ok, error, frozen, rejection_count}.
    """
    order = get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    if order.get("status") != "pending":
        return {"ok": False, "error": "Заказ не в статусе pending"}

    rc = int(order.get("rejection_count") or 0) + 1
    max_cycles = int(get_setting("reject_max_cycles", 3))
    frozen = 1 if rc >= max_cycles else 0

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE orders SET status = 'draft', rejection_comment = ?, "
                "rejection_count = ?, frozen = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'"
            ),
            (comment, rc, frozen, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if not updated:
        return {"ok": False, "error": "Заказ уже обработан"}

    add_audit_log(
        rejected_by,
        rejected_name,
        get_role(rejected_by),
        "order_rejected",
        f"Заказ #{order_id} → draft (попытка {rc}/{max_cycles})"
        + (" — ЗАМОРОЖЕН" if frozen else ""),
    )
    return {"ok": True, "error": None, "frozen": bool(frozen), "rejection_count": rc}


def mark_order_shipped(order_id: int, shipped_by: int, shipped_name: str) -> dict:
    """Отметить заказ отгруженным (approved → shipped), DB-часть. Альтернатива
    МС-вебхуку (stateType=Successful) — для аккаунтов без статуса типа
    «Успешный». Выставляет shipped_at/shipped_by. Возвращает {ok, error}.

    Round 6 (L_R6): если у заказа уже есть `ms_demand_id`, значит МС-сторона
    отгрузила раньше (через webhook или manual API). Audit'им как
    'sync', а не как 'ручную отгрузку' — иначе менеджер видит спам.
    """
    order = get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    if order.get("status") != "approved":
        return {"ok": False, "error": "Отгрузить можно только одобренный заказ"}

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE orders SET status = 'shipped', shipped_at = ?, shipped_by = ?, "
                "updated_at = ? WHERE id = ? AND status = 'approved'"
            ),
            (now_str(), shipped_by, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if not updated:
        return {"ok": False, "error": "Заказ уже обработан"}

    already_in_ms = bool(order.get("ms_demand_id"))
    add_audit_log(
        shipped_by,
        shipped_name,
        get_role(shipped_by),
        "order_shipped",
        (
            f"Заказ #{order_id} sync (demand уже в МС, локальный статус догнан)"
            if already_in_ms
            else f"Заказ #{order_id} отмечен отгруженным"
        ),
    )
    return {"ok": True, "error": None, "already_in_ms": already_in_ms}


def cancel_order(order_id: int, cancelled_by: int, cancelled_name: str, reason: str) -> dict:
    """Отмена заказа (IMPLEMENTATION.md §6.7), DB-часть. Reverse-demand в
    МойСклад — отдельной фазой. Отмена доступна для approved; shipped по спеке
    требует возврата на 100% — здесь не пропускаем (нужен return-флоу).

    M2: окно отмены (cancellation_deadline) убрано — поле нигде не заполнялось,
    проверка была мёртвой и вводила в заблуждение. Если понадобится временно́е
    окно — заполнять deadline при одобрении и вернуть проверку сюда."""
    order = get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    status = order.get("status")
    if status != "approved":
        return {
            "ok": False,
            "error": "Отмена доступна только для approved (shipped → через возврат)",
        }

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE orders SET status = 'cancelled', cancelled_at = ?, "
                "cancelled_by = ?, cancellation_reason = ?, updated_at = ? "
                "WHERE id = ? AND status = 'approved'"
            ),
            (now_str(), cancelled_by, reason, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if not updated:
        return {"ok": False, "error": "Заказ уже обработан"}

    add_audit_log(
        cancelled_by,
        cancelled_name,
        get_role(cancelled_by),
        "order_cancelled",
        f"Заказ #{order_id} отменён: {reason[:200]}",
    )
    return {"ok": True, "error": None}


async def get_stale_pending_orders(hours: int = 48) -> list[dict]:
    """Заявки, висящие в pending дольше `hours` (для stale-мониторинга, §13).
    Берём COALESCE(submitted_at, created_at). asyncpg Stage 8 (#21): native
    async через adb_core; cutoff в Python (local TZ), параметром (CLAUDE.md)."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return await adb_core.fetch(
        "SELECT * FROM orders WHERE status = 'pending' AND (deleted_at IS NULL) "
        "AND COALESCE(submitted_at, created_at) < $1 ORDER BY created_at ASC",
        cutoff,
    )


# ─── IMPLEMENTATION.md Фаза 4: сдача наличных (cash deposits) ─────────────────
#
# Менеджер сдаёт собранные деньги в кассу; босс/бухгалтер подтверждает.
# Закрывает дыру «собрал у клиента, но не сдал в офис». Используем новую
# модель: при покрытии заказа подтверждёнными сдачами он переходит в 'paid'
# (payment_confirmed=1). order total берём из get_order_payment_summary.


def _order_total_cents(order_id: int) -> int:
    """Сумма заказа в копейках (точно, из price_cents)."""
    return int(get_order_payment_summary(order_id)["total_cents"])


# SQL-фрагмент «распределено на заказ в копейках» — берёт amount_allocated_cents
# с фолбэком на legacy REAL (round(amount_allocated*100)). Без placeholder'ов.
_SUM_ALLOC_CENTS = (
    "COALESCE(SUM(COALESCE(cdo.amount_allocated_cents, "
    "CAST(round(cdo.amount_allocated * 100) AS INTEGER))), 0)"
)


def _order_confirmed_deposit_cents(order_id: int) -> int:
    """Распределено на заказ ПОДТВЕРЖДЁННЫМИ сдачами, в копейках."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                f"SELECT {_SUM_ALLOC_CENTS} AS s FROM cash_deposit_orders cdo "
                "JOIN cash_deposits d ON d.id = cdo.deposit_id "
                "WHERE cdo.order_id = ? AND d.status = 'confirmed' AND (d.deleted_at IS NULL)"
            ),
            (order_id,),
        )
        row = cur.fetchone()
    return int((row["s"] if USE_POSTGRES else row[0]) or 0)


def _order_allocated_deposit_cents(order_id: int) -> int:
    """Распределено на заказ pending+confirmed сдачами, в копейках (M3)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                f"SELECT {_SUM_ALLOC_CENTS} AS s FROM cash_deposit_orders cdo "
                "JOIN cash_deposits d ON d.id = cdo.deposit_id "
                "WHERE cdo.order_id = ? AND d.status IN ('pending', 'confirmed') "
                "AND (d.deleted_at IS NULL)"
            ),
            (order_id,),
        )
        row = cur.fetchone()
    return int((row["s"] if USE_POSTGRES else row[0]) or 0)


def get_manager_open_orders_for_deposit(manager_id: int) -> list[dict]:
    """Отгруженные неоплаченные заказы менеджера (для распределения сдачи).
    Возвращает [{id, total, covered, remaining}] по возрастанию created_at.
    covered учитывает уже распределённое pending+confirmed-сдачами (M3).

    Остаток считаем в копейках (без float-эпсилона 0.01) — заказ попадает
    в список, только если непокрытый остаток ≥ 1 копейки."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT id FROM orders WHERE user_id = ? AND status = 'shipped' "
                "AND payment_confirmed = 0 AND (deleted_at IS NULL) ORDER BY created_at ASC"
            ),
            (manager_id,),
        )
        ids = [(r["id"] if USE_POSTGRES else r[0]) for r in cur.fetchall()]
    out = []
    for oid in ids:
        total_cents = _order_total_cents(oid)
        covered_cents = _order_allocated_deposit_cents(oid)
        remaining_cents = total_cents - covered_cents
        if remaining_cents > 0:
            out.append(
                {
                    "id": oid,
                    "total": float(money.from_cents(total_cents)),
                    "covered": float(money.from_cents(covered_cents)),
                    "remaining": float(money.from_cents(remaining_cents)),
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


def set_currency_rate(currency_code: str, rate: float, updated_by: int) -> tuple[bool, str | None]:
    """Установить/обновить rate. UPSERT с автоинвалидацией кэша.

    Возвращает (ok, error_msg). Валидирует rate как amount (> 0, конечное).
    `currency_code` — нормализуется UPPER, должен быть в ALLOWED_CURRENCIES."""
    from config import ALLOWED_CURRENCIES

    code = (currency_code or "").upper().strip()
    if not code:
        return False, "currency_code пустой"
    if code not in ALLOWED_CURRENCIES:
        return False, f"currency_code должен быть из {list(ALLOWED_CURRENCIES)}"
    ok, err = _validate_amount(rate)
    if not ok:
        return False, f"rate: {err}"
    with get_conn() as conn:
        cur = get_cursor(conn)
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
        conn.commit()
    # Инвалидируем кэш именно этой валюты, остальные не трогаем.
    with _currency_rates_lock:
        _CURRENCY_RATES_CACHE.pop(code, None)
    return True, None


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
    заданы — валидируются через `_validate_amount` (>0, конечные, ≤10М).
    currency дефолтится в BASE_CURRENCY.
    """
    from config import BASE_CURRENCY

    ms_id = (ms_id or "").strip()
    if not ms_id:
        return False, "ms_id обязателен"
    for label, val in (("sale_price", sale_price), ("cost_price", cost_price)):
        if val is not None:
            ok, err = _validate_amount(val)
            if not ok:
                return False, f"{label}: {err}"
    cur_code = (currency or BASE_CURRENCY or "USD").upper()
    sale = float(sale_price) if sale_price is not None else None
    cost = float(cost_price) if cost_price is not None else None
    sale_c = money.to_cents(sale) if sale is not None else None
    cost_c = money.to_cents(cost) if cost is not None else None
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                q(
                    "INSERT INTO product_prices "
                    "(ms_id, product_name, sale_price, sale_price_cents, cost_price, cost_price_cents, currency, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (ms_id) DO UPDATE SET "
                    "product_name = EXCLUDED.product_name, "
                    "sale_price = EXCLUDED.sale_price, "
                    "sale_price_cents = EXCLUDED.sale_price_cents, "
                    "cost_price = EXCLUDED.cost_price, "
                    "cost_price_cents = EXCLUDED.cost_price_cents, "
                    "currency = EXCLUDED.currency, "
                    "updated_by = EXCLUDED.updated_by, "
                    "updated_at = EXCLUDED.updated_at"
                ),
                (ms_id, product_name or "", sale, sale_c, cost, cost_c, cur_code, updated_by, now_str()),
            )
        else:
            cur.execute(
                q(
                    "INSERT OR REPLACE INTO product_prices "
                    "(ms_id, product_name, sale_price, sale_price_cents, cost_price, cost_price_cents, currency, updated_by, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (ms_id, product_name or "", sale, sale_c, cost, cost_c, cur_code, updated_by, now_str()),
            )
        conn.commit()
    with _product_price_lock:
        _PRODUCT_PRICE_CACHE.pop(ms_id, None)
    return True, None


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
                    "SELECT ms_id, product_name, sale_price, cost_price, currency, updated_at "
                    "FROM product_prices WHERE ms_id = ?"
                ),
                (ms_id,),
            )
            row = cur.fetchone()
            result = dict(row) if row else {}
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
        "SELECT ms_id, product_name, sale_price, cost_price, currency "
        f"FROM product_prices WHERE ms_id IN ({placeholders})",
        *ids,
    )
    return {r["ms_id"]: r for r in rows}


def get_existing_ms_product_ids(ms_ids: list[str]) -> set[str]:
    """Подмножество ms_ids, которые ещё ЕСТЬ в снапшоте ms_products (т.е. НЕ
    удалены в МойСклад). Аналитика использует это, чтобы прятать из топа
    товары, удалённые в МС (иначе менеджер видит непонятные позиции)."""
    ids = [str(x).strip() for x in (ms_ids or []) if str(x).strip()]
    if not ids:
        return set()
    placeholders = ", ".join(["?"] * len(ids))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"SELECT ms_id FROM ms_products WHERE ms_id IN ({placeholders})"),
            ids,
        )
        return {(r["ms_id"] if USE_POSTGRES else r[0]) for r in cur.fetchall()}


async def get_all_product_prices() -> list[dict]:
    """Все заданные цены. Для admin-UI экрана «Цены». asyncpg Stage 9 (#21)."""
    return await adb_core.fetch(
        "SELECT ms_id, product_name, sale_price, cost_price, currency, updated_at "
        "FROM product_prices ORDER BY product_name"
    )


def _invalidate_product_price_cache() -> None:
    with _product_price_lock:
        _PRODUCT_PRICE_CACHE.clear()


def create_cash_deposit(
    manager_id: int,
    amount: float,
    allocations: list[tuple] | None = None,
) -> dict:
    """Создать сдачу (status=pending) + распределение по заказам.

    allocations: список (order_id, amount) для ручного режима; если None —
    авто-FIFO по открытым заказам менеджера. Возвращает {ok, deposit_id,
    allocations}.

    Конкурентность (Round 6 RACE-1): в Postgres берём advisory-lock на
    (manager_id), чтобы 2 параллельных /deposit от одного менеджера не
    переаллоцировали один и тот же остаток заказа дважды. Без локa
    `get_manager_open_orders_for_deposit` читает pending+confirmed
    распределения вне транзакции — два вызова видят одинаковый `remaining`,
    распределяют сверх лимита, аналитика по cash-flow завышается. На
    SQLite (локалка) advisory-lock'а нет, но там один-процессный сценарий.
    """
    ok, err = _validate_amount(amount)
    if not ok:
        return {"ok": False, "error": err}

    is_manual = allocations is not None
    allocs: list[tuple] = list(allocations) if allocations is not None else []

    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            # Сериализуем FIFO-расчёт + INSERT по manager_id. pg_advisory_xact_lock
            # держится до конца транзакции, второй параллельный вызов ждёт.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"cash_deposit:manager:{manager_id}",),
            )
        if not is_manual:
            left = amount
            for o in get_manager_open_orders_for_deposit(manager_id):
                if left <= 0:
                    break
                take = min(o["remaining"], left)
                if take > 0:
                    allocs.append((o["id"], round(take, 2)))
                    left -= take

        amount_cents = money.to_cents(amount)
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO cash_deposits (manager_id, amount, amount_cents, deposited_at, status, created_at) "
                "VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING id",
                (manager_id, amount, amount_cents, now_str(), now_str()),
            )
            deposit_id = cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO cash_deposits (manager_id, amount, amount_cents, deposited_at, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (manager_id, amount, amount_cents, now_str(), now_str()),
            )
            deposit_id = cur.lastrowid
        for order_id, alloc in allocs:
            cur.execute(
                q(
                    "INSERT INTO cash_deposit_orders (deposit_id, order_id, amount_allocated, amount_allocated_cents, is_manual) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (deposit_id, order_id, alloc, money.to_cents(alloc), 1 if is_manual else 0),
            )
        conn.commit()
    return {"ok": True, "deposit_id": deposit_id, "allocations": allocs}


def confirm_cash_deposit(deposit_id: int, confirmed_by: int, confirmed_name: str = "") -> dict:
    """Подтвердить сдачу (атомарно). Каждый покрытый заказ → 'paid'.
    Возвращает {ok, closed_orders}."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE cash_deposits SET status = 'confirmed', confirmed_by = ?, confirmed_at = ? "
                "WHERE id = ? AND status = 'pending'"
            ),
            (confirmed_by, now_str(), deposit_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
        if not updated:
            return {"ok": False, "error": "Сдача уже обработана"}
        cur.execute(
            q("SELECT order_id FROM cash_deposit_orders WHERE deposit_id = ?"),
            (deposit_id,),
        )
        order_ids = [(r["order_id"] if USE_POSTGRES else r[0]) for r in cur.fetchall()]

    closed = []
    for oid in order_ids:
        # Покрытие считаем в копейках (без float-эпсилона +0.01). Строку заказа
        # блокируем FOR UPDATE и закрываем тем же атомарным UPDATE с guard'ом
        # payment_confirmed=0 — параллельные подтверждения сдач по одному заказу
        # не закроют его дважды (паттерн как в mark_order_paid).
        if _order_confirmed_deposit_cents(oid) >= _order_total_cents(oid):
            with get_conn() as conn:
                cur = get_cursor(conn)
                if USE_POSTGRES:
                    cur.execute(
                        q("SELECT payment_confirmed FROM orders WHERE id = ? FOR UPDATE"),
                        (oid,),
                    )
                cur.execute(
                    q(
                        "UPDATE orders SET payment_confirmed = 1, payment_confirmed_at = ?, "
                        "status = 'paid', updated_at = ? WHERE id = ? AND payment_confirmed = 0"
                    ),
                    (now_str(), now_str(), oid),
                )
                if cur.rowcount > 0:
                    closed.append(oid)
                conn.commit()
    add_audit_log(
        confirmed_by,
        confirmed_name,
        get_role(confirmed_by),
        "cash_deposit_confirmed",
        f"Сдача #{deposit_id} подтверждена; закрыты заказы: {closed or '—'}",
    )
    return {"ok": True, "closed_orders": closed}


def reject_cash_deposit(deposit_id: int, rejected_by: int, rejected_name: str, reason: str) -> dict:
    # Round 6 (L_R8): clip reason — DB-колонка TEXT (unbounded), а сообщение
    # потом шлётся менеджеру через bot.send_message (Telegram-лимит 4096).
    # UI-валидация в webapp/server.py есть, но прямой бот-FSM вызов её обходит.
    reason = (reason or "").strip()[:500]
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE cash_deposits SET status = 'rejected', reject_reason = ?, "
                "confirmed_by = ?, confirmed_at = ? WHERE id = ? AND status = 'pending'"
            ),
            (reason, rejected_by, now_str(), deposit_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if not updated:
        return {"ok": False, "error": "Сдача уже обработана"}
    add_audit_log(
        rejected_by,
        rejected_name,
        get_role(rejected_by),
        "cash_deposit_rejected",
        f"Сдача #{deposit_id} отклонена: {reason[:200]}",
    )
    return {"ok": True}


async def get_cash_deposit(deposit_id: int) -> dict | None:
    """asyncpg Stage 9 (#21): native async через adb_core."""
    return await adb_core.fetchrow("SELECT * FROM cash_deposits WHERE id = $1", deposit_id)


async def get_cash_deposit_orders(deposit_id: int) -> list[dict]:
    """asyncpg Stage 9 (#21)."""
    return await adb_core.fetch(
        "SELECT order_id, amount_allocated FROM cash_deposit_orders WHERE deposit_id = $1",
        deposit_id,
    )


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
        f"SELECT deposit_id, order_id, amount_allocated FROM cash_deposit_orders "
        f"WHERE deposit_id IN ({placeholders})",
        *unique_ids,
    )
    grouped: dict[int, list[dict]] = {}
    for d in rows:
        # Форма элемента — как у get_cash_deposit_orders (без deposit_id).
        grouped.setdefault(d["deposit_id"], []).append(
            {"order_id": d["order_id"], "amount_allocated": d["amount_allocated"]}
        )
    return grouped


async def get_manager_cash_deposits(manager_id: int, limit: int = 20) -> list[dict]:
    """asyncpg Stage 9 (#21)."""
    return await adb_core.fetch(
        "SELECT * FROM cash_deposits WHERE manager_id = $1 AND (deleted_at IS NULL) "
        "ORDER BY created_at DESC LIMIT $2",
        manager_id,
        limit,
    )


def get_deposit_confirmers() -> list[int]:
    """user_id ролей, которые подтверждают сдачи: admin/boss/bookkeeper."""
    try:
        users = get_all_users()
    except Exception:
        return []
    return [u["user_id"] for u in users if u["role"] in ("admin", "boss", "bookkeeper")]


async def get_pending_cash_deposits() -> list[dict]:
    """Сдачи, ждущие подтверждения (для боса/бухгалтера).

    asyncpg-миграция Stage 5 (задача #21): нативный async через adb_core.
    Вызовы: handlers/webapp (`await adb.…`), async-cron run_ops_monitor
    (`await`), тесты (`asyncio.run`). Loop-aware пул (Stage 4) делает
    cron-вызов безопасным.
    """
    return await adb_core.fetch(
        "SELECT * FROM cash_deposits WHERE status = 'pending' AND (deleted_at IS NULL) "
        "ORDER BY deposited_at ASC"
    )


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
        "AND (deleted_at IS NULL) AND COALESCE(shipped_at, created_at) < $1 "
        "ORDER BY user_id, created_at",
        cutoff,
    )


# ─── IMPLEMENTATION.md Фаза 5: возвраты + FEFO/партии ─────────────────────────


def upsert_product_batch(
    product_id: str,
    moysklad_batch_id: str | None,
    batch_code: str,
    expiry_date: str | None,
    qty_remaining: float,
) -> str:
    """UPSERT партии по moysklad_batch_id (натуральный ключ из МС). Возвращает
    id строки (uuid). Используется синком партий (§9.1)."""
    import uuid as _uuid

    with get_conn() as conn:
        cur = get_cursor(conn)
        existing = None
        if moysklad_batch_id:
            cur.execute(
                q("SELECT id FROM product_batches WHERE moysklad_batch_id = ?"),
                (moysklad_batch_id,),
            )
            row = cur.fetchone()
            existing = (row["id"] if USE_POSTGRES else row[0]) if row else None
        if existing:
            cur.execute(
                q(
                    "UPDATE product_batches SET product_id = ?, batch_code = ?, "
                    "expiry_date = ?, qty_remaining = ?, updated_at = ? WHERE id = ?"
                ),
                (product_id, batch_code, expiry_date, qty_remaining, now_str(), existing),
            )
            conn.commit()
            return existing
        bid = _uuid.uuid4().hex
        cur.execute(
            q(
                "INSERT INTO product_batches (id, product_id, moysklad_batch_id, batch_code, "
                "expiry_date, qty_remaining, received_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                bid,
                product_id,
                moysklad_batch_id,
                batch_code,
                expiry_date,
                qty_remaining,
                now_str(),
                now_str(),
            ),
        )
        conn.commit()
        return bid


def select_batches_fefo(product_id: str, qty: float) -> list[dict]:
    """FEFO-резерв: вернуть [{batch_id, take}] из партий с ближайшим expiry
    (§6.2.5). NULL-expiry — в конец (берём после датированных)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT id, expiry_date, qty_remaining FROM product_batches "
                "WHERE product_id = ? AND qty_remaining > 0 "
                "ORDER BY (expiry_date IS NULL), expiry_date ASC"
            ),
            (product_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    out = []
    left = qty
    for r in rows:
        if left <= 0:
            break
        take = min(float(r["qty_remaining"]), left)
        if take > 0:
            out.append({"batch_id": r["id"], "take": round(take, 3)})
            left -= take
    return out


def _adjust_batch_qty(batch_id: str, delta: float) -> None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE product_batches SET qty_remaining = qty_remaining + ?, updated_at = ? "
                "WHERE id = ?"
            ),
            (delta, now_str(), batch_id),
        )
        conn.commit()


async def get_batches_expiring_within(days: int = 7) -> list[dict]:
    """Партии с остатком, истекающие в ближайшие `days` дней (§9.4).
    asyncpg Stage 8 (#21): cutoff в Python, параметром."""
    from datetime import timedelta

    cutoff = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    return await adb_core.fetch(
        "SELECT * FROM product_batches WHERE qty_remaining > 0 "
        "AND expiry_date IS NOT NULL AND expiry_date <= $1 ORDER BY expiry_date ASC",
        cutoff,
    )


def _is_returnable(order: dict) -> bool:
    """Заказ можно вернуть, если он отгружен/оплачен/частично-возвращён ИЛИ
    фактически оплачен по легаси-схеме (paid_confirmed_at заполнен — оплата
    через /pay подтверждена, но ярлык status мог остаться 'approved')."""
    if order.get("status") in ("shipped", "paid", "partially_returned"):
        return True
    return bool(order.get("paid_confirmed_at"))


def is_order_returnable(order_id: int) -> bool:
    """Публичная проверка для бот/webapp-прехеков (та же логика, что в create_return)."""
    order = get_order(order_id)
    if order is None:
        return False
    return _is_returnable(order)


def create_return(
    order_id: int,
    return_type: str,
    reason: str,
    items: list[tuple],
    refund_method: str | None,
    created_by: int,
    force: bool = False,
) -> dict:
    """Создать возврат (status=pending) + позиции. items = [(order_item_id, qty, amount)].
    return_type: 'partial'|'full'. refund_method: 'cash'|'debt_reduction'|'no_refund'.
    Доступно для shipped/paid/partially_returned. Дедлайн (return_deadline_days)
    блокирует, если не force (вызывающий решает по роли). Возвращает {ok, return_id}.
    """
    order = get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    if not _is_returnable(order):
        return {"ok": False, "error": "Возврат доступен только для отгруженных/оплаченных"}
    if not items:
        return {"ok": False, "error": "Не указаны позиции возврата"}

    # Дедлайн (read-only, до блокировки).
    deadline_days = int(get_setting("return_deadline_days", 90))
    shipped_at = order.get("shipped_at")
    if shipped_at and not force:
        from datetime import timedelta

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
    oitems = {it["id"]: it for it in get_order_items(order_id)}
    clamped: list[tuple] = []  # (order_item_id, take_qty, line_cents)
    for oitem_id, qty, _amount in items:
        oi = oitems.get(oitem_id)
        if not oi:
            continue
        available = float(oi.get("quantity", 0) or 0) - float(oi.get("returned_qty", 0) or 0)
        take = min(float(qty), available)
        if take <= 0:
            continue
        clamped.append((oitem_id, take, money.mul_qty(_price_cents(oi), take)))
    if not clamped:
        return {"ok": False, "error": "Нет позиций, доступных к возврату"}

    total_cents = sum(c for _, _, c in clamped)
    total_amount = float(money.from_cents(total_cents))

    # H1 + Round 6 RACE-2: не плодим параллельные возвраты по одному заказу.
    # Advisory-lock держится до конца транзакции и теперь покрывает И dup-check,
    # И INSERT (раньше lock снимался до INSERT — TOCTOU пропускал два возврата,
    # потом каждый confirm_return наращивал returned_qty → overflow > quantity).
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"return:order:{order_id}",),
            )
        cur.execute(
            q(
                "SELECT COUNT(*) AS c FROM returns WHERE order_id = ? "
                "AND status = 'pending' AND (deleted_at IS NULL)"
            ),
            (order_id,),
        )
        row = cur.fetchone()
        if int((row["c"] if USE_POSTGRES else row[0]) or 0) > 0:
            conn.rollback()
            return {"ok": False, "error": "По заказу уже есть возврат на рассмотрении"}

        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, total_amount_cents, "
                "refund_method, created_by, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s) RETURNING id",
                (order_id, return_type, reason, total_amount, total_cents, refund_method, created_by, now_str()),
            )
            return_id = cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, total_amount_cents, "
                "refund_method, created_by, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (order_id, return_type, reason, total_amount, total_cents, refund_method, created_by, now_str()),
            )
            return_id = cur.lastrowid
        for oitem_id, qty, line_cents in clamped:
            cur.execute(
                q(
                    "INSERT INTO return_items (return_id, order_item_id, qty, amount, amount_cents) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (return_id, oitem_id, qty, float(money.from_cents(line_cents)), line_cents),
            )
        conn.commit()
    return {"ok": True, "return_id": return_id, "total_amount": total_amount}


def mark_return_goods_received(return_id: int, by: int) -> dict:
    """Кладовщик отметил «товар получен» (флаг, статус остаётся pending)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE returns SET goods_received = 1 WHERE id = ? AND status = 'pending'"),
            (return_id,),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return {"ok": updated}


def confirm_return(return_id: int, confirmed_by: int, confirmed_name: str = "") -> dict:
    """Подтвердить возврат: returned_qty += по позициям, статус заказа
    (returned|partially_returned), восстановление остатков по партиям FEFO,
    обработка refund (cash → отрицательная сдача; debt_reduction/no_refund —
    учёт в долге). Возвращает {ok, order_status}."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM returns WHERE id = ?"), (return_id,))
        row = cur.fetchone()
        ret = dict(row) if row else None
    if not ret:
        return {"ok": False, "error": "Возврат не найден"}
    if ret.get("status") != "pending":
        return {"ok": False, "error": "Возврат уже обработан"}

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE returns SET status = 'confirmed', confirmed_by = ?, confirmed_at = ? "
                "WHERE id = ? AND status = 'pending'"
            ),
            (confirmed_by, now_str(), return_id),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return {"ok": False, "error": "Возврат уже обработан"}
        cur.execute(
            q("SELECT order_item_id, qty FROM return_items WHERE return_id = ?"),
            (return_id,),
        )
        ritems = [dict(r) for r in cur.fetchall()]
        # Round 6 (L_R2): атомарная проверка returned_qty + delta <= quantity.
        # Без неё concurrent confirm двух разных returns по одному заказу мог
        # наращивать returned_qty за пределы quantity (overshoot → лишний MS-doc).
        for ri in ritems:
            cur.execute(
                q(
                    "UPDATE order_items "
                    "SET returned_qty = returned_qty + ? "
                    "WHERE id = ? AND returned_qty + ? <= quantity"
                ),
                (ri["qty"], ri["order_item_id"], ri["qty"]),
            )
            if cur.rowcount == 0:
                # Overshoot — откатываем confirm целиком, заявка остаётся в pending.
                conn.rollback()
                return {
                    "ok": False,
                    "error": (
                        "Превышен доступный остаток к возврату (другой возврат "
                        "уже учтён). Перепроверьте и создайте новый."
                    ),
                }
        conn.commit()

    # Восстановление остатков по партиям — отдельно, через _adjust_batch_qty.
    for ri in ritems:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(q("SELECT batch_id FROM order_items WHERE id = ?"), (ri["order_item_id"],))
            br = cur.fetchone()
        batch_id = (br["batch_id"] if USE_POSTGRES else br[0]) if br else None
        if batch_id:
            _adjust_batch_qty(batch_id, float(ri["qty"]))

    order_id = ret["order_id"]
    # Полностью ли возвращён заказ?
    items = get_order_items(order_id)
    fully = (
        all(
            float(it.get("returned_qty") or 0) + 1e-9 >= float(it.get("quantity") or 0)
            for it in items
        )
        if items
        else False
    )
    new_status = "returned" if fully else "partially_returned"
    return_status = "full" if fully else "partial"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET status = ?, return_status = ?, updated_at = ? WHERE id = ?"),
            (new_status, return_status, now_str(), order_id),
        )
        conn.commit()

    # Refund: cash → отрицательная подтверждённая сдача (учёт выдачи из кассы).
    if ret.get("refund_method") == "cash":
        order = get_order(order_id)
        with get_conn() as conn:
            cur = get_cursor(conn)
            refund_amount = -float(ret["total_amount"])
            cur.execute(
                q(
                    "INSERT INTO cash_deposits (manager_id, amount, amount_cents, deposited_at, "
                    "status, confirmed_by, confirmed_at, notes, created_at) "
                    "VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?, ?)"
                ),
                (
                    (order or {}).get("user_id") or confirmed_by,
                    refund_amount,
                    -money.to_cents(ret["total_amount"]),  # dual-write копеек (отриц.)
                    now_str(),
                    confirmed_by,
                    now_str(),
                    f"refund возврат #{return_id}",
                    now_str(),
                ),
            )
            conn.commit()
    # debt_reduction / no_refund — отдельной записи не требуют (долг учитывает
    # подтверждённые возвраты в get_agent_current_debt).

    add_audit_log(
        confirmed_by,
        confirmed_name,
        get_role(confirmed_by),
        "return_confirmed",
        f"Возврат #{return_id} по заказу #{order_id} ({return_status}, "
        f"{ret['total_amount']:.0f} USD, {ret.get('refund_method')})",
    )
    return {"ok": True, "order_status": new_status}


async def get_pending_returns() -> list[dict]:
    """Возвраты, ждущие подтверждения. asyncpg-миграция Stage 5 (задача #21):
    нативный async через adb_core. Вызовы: handlers/webapp (`await adb.…`),
    async-cron run_ops_monitor (`await`), тесты (`asyncio.run`)."""
    return await adb_core.fetch(
        "SELECT * FROM returns WHERE status = 'pending' AND (deleted_at IS NULL) "
        "ORDER BY created_at ASC"
    )


def get_return(return_id: int) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM returns WHERE id = ?"), (return_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_return_positions_for_ms(return_id: int) -> list[dict]:
    """Позиции возврата с product_href и ценой (из order_items) — для сборки
    документа «Возврат покупателя» в МойСклад. amount берём из return_items."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT oi.product_href AS product_href, oi.product_name AS product_name, "
                "ri.qty AS qty, oi.price AS price "
                "FROM return_items ri JOIN order_items oi ON oi.id = ri.order_item_id "
                "WHERE ri.return_id = ?"
            ),
            (return_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def claim_ops_monitor_run(run_date: str) -> bool:
    """Round 6 RACE-4: idempotency-guard для ops_monitor.

    Railway Cron при сетевом hiccup'е может ретраить запуск, или ручной запуск
    может пересечься с плановым — без guard'а дайджест разойдётся всем 2 раза.

    Возвращает True если этот вызов «застолбил» дату (первый за сегодня),
    False если уже запускался. `tasks/run_ops_monitor.main()` должен exit 0
    при False.

    Использует ту же таблицу `notified_shipments`-style паттерн с PRIMARY
    KEY-based atomic INSERT-if-absent. CREATE TABLE в init_db; run_date —
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


def set_return_ms_id(return_id: int, ms_id: str) -> bool:
    """Сохранить id документа «Возврат покупателя» из МойСклад (идемпотентность
    повторной отправки).

    Round 6 (RACE-3): conditional UPDATE — если ms_id уже стоит, не
    перезаписываем. Защищает от race'а двух параллельных create_salesreturn:
    оба прочли NULL, оба POST в МС, оба зовут set_return_ms_id — без guard'а
    второй перетёр бы первый id, первый salesreturn в МС становится orphan'ом.
    Возвращаемый bool теперь говорит «выиграл ли я гонку» — caller может,
    если хочет, попытаться удалить только что созданный orphan-doc в МС.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE returns SET moysklad_return_id = ? "
                "WHERE id = ? AND moysklad_return_id IS NULL"
            ),
            (ms_id, return_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


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
        cur.execute(q("SELECT role FROM user_roles WHERE user_id = ?"), (uid,))
        row = cur.fetchone()
    if not row:
        return "guest"
    return row["role"] if USE_POSTGRES else row[0]


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
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
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
                    username = excluded.username,
                    full_name = excluded.full_name,
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


def get_user(user_id: int) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM user_roles WHERE user_id = ?"), (user_id,))
        row = cur.fetchone()
    return dict(row) if row else None


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


def set_moysklad_employee(user_id: int, ms_employee_id: str, status: str = "linked") -> bool:
    """Привязать Telegram пользователя к сотруднику МойСклад."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE user_roles SET moysklad_employee_id = ?, ms_sync_status = ? WHERE user_id = ?"
            ),
            (ms_employee_id, status, user_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def get_moysklad_employee_id(user_id: int) -> str | None:
    """Получить ID сотрудника МойСклад для Telegram пользователя."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT moysklad_employee_id FROM user_roles WHERE user_id = ?"), (user_id,))
        row = cur.fetchone()
    if not row:
        return None
    return row["moysklad_employee_id"] if USE_POSTGRES else row[0]


async def get_unsynced_managers() -> list[dict]:
    """Менеджеры без привязки к МойСклад. asyncpg Stage 8 (#21)."""
    return await adb_core.fetch(
        "SELECT * FROM user_roles WHERE role = 'manager' "
        "AND (moysklad_employee_id IS NULL OR ms_sync_status = 'pending')"
    )


def remove_user(user_id: int, removed_by: int | None = None, removed_name: str = "") -> bool:
    users = get_all_users()
    target = next((u for u in users if u["user_id"] == user_id), None)
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("DELETE FROM user_roles WHERE user_id = ?"), (user_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    if deleted:
        _invalidate_role_cache(user_id)
    if deleted and removed_by and target:
        add_audit_log(
            removed_by,
            removed_name,
            get_role(removed_by),
            "user_removed",
            f"Удалён {target['full_name']} (ID: {user_id}, роль: {target['role']})",
        )
    return deleted


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
                    (user_id, username, full_name, amount, amount_cents, currency, comment, status, order_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id
            """,
                (user_id, username, full_name, amount, amount_cents, currency, comment, order_id, now_str()),
            )
            payment_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO payments
                    (user_id, username, full_name, amount, amount_cents, currency, comment, status, order_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
                (user_id, username, full_name, amount, amount_cents, currency, comment, order_id, now_str()),
            )
            payment_id = cur.lastrowid
        conn.commit()
    return payment_id


def get_payments_for_order(order_id: int) -> list[dict]:
    """Все платежи привязанные к заказу (включая pending/rejected/archived)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT * FROM payments WHERE order_id = ? ORDER BY created_at ASC"),
            (order_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_payments_for_orders(order_ids: list[int]) -> dict[int, list[dict]]:
    """Батч-версия: {order_id: [payments...]}. Заказы без платежей в
    результат не попадают; вызывающий должен использовать .get(oid, []).

    Дедуплицируем order_ids — если caller передал список с повторами,
    placeholders разрастаются впустую и план запроса страдает.
    """
    if not order_ids:
        return {}
    unique_ids = list(set(order_ids))
    placeholders = ",".join(["?"] * len(unique_ids))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"SELECT * FROM payments WHERE order_id IN ({placeholders}) ORDER BY created_at ASC"),
            unique_ids,
        )
        rows = cur.fetchall()
    grouped: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        grouped.setdefault(d["order_id"], []).append(d)
    return grouped


def _price_cents(item: dict) -> int:
    """Цена позиции заказа в копейках: предпочитаем price_cents, иначе из
    legacy REAL price (для строк, ещё не покрытых backfill'ом)."""
    pc = item.get("price_cents")
    if pc is not None:
        return int(pc)
    return money.to_cents(item.get("price", 0) or 0)


def _amount_cents(payment: dict) -> int:
    """Сумма платежа в копейках: предпочитаем amount_cents, иначе legacy REAL."""
    ac = payment.get("amount_cents")
    if ac is not None:
        return int(ac)
    return money.to_cents(payment.get("amount", 0) or 0)


# SQL-фрагменты «сумма денег в копейках» — берут dual-write *_cents с фолбэком
# на legacy REAL (round(x*100)). Без placeholder'ов, безопасно встраивать в f-string.
# Работают и в SQLite, и в Postgres (round + CAST AS INTEGER). Позволяют считать
# суммы/сравнения точно в целых копейках вместо float (раньше нужен был epsilon).
_SUM_PAYMENTS_CENTS = "COALESCE(SUM(COALESCE(amount_cents, CAST(round(amount * 100) AS INTEGER))), 0)"
_SUM_ORDER_TOTAL_CENTS = (
    "COALESCE(SUM(CAST(round(quantity * COALESCE(price_cents, round(price * 100))) AS INTEGER)), 0)"
)
_SUM_RETURNS_CENTS = (
    "COALESCE(SUM(COALESCE(total_amount_cents, CAST(round(total_amount * 100) AS INTEGER))), 0)"
)


def get_order_payment_summary(order_id: int) -> dict:
    """Сумма по заказу: total / confirmed / pending / remaining.

    Используется для отображения «оплачено X из Y, остаток Z» и для
    решения, закрыт ли заказ (remaining == 0).

    Дополнительно (PR #42): `*_base` поля — пересчёт в BASE_CURRENCY через
    `convert_to_base`. None если курс валюты заказа не задан в админке;
    UI может в этом случае показать «—» вместо враного «$0.00».
    """
    from config import BASE_CURRENCY

    order = get_order(order_id)
    if not order:
        return {"total": 0.0, "confirmed": 0.0, "pending": 0.0, "remaining": 0.0}
    items = get_order_items(order_id)
    # Считаем в копейках (точно), с фолбэком на старый REAL для не-забэкфилленных строк.
    total_cents = sum(money.mul_qty(_price_cents(it), it.get("quantity", 0) or 0) for it in items)
    payments = get_payments_for_order(order_id)
    confirmed_cents = sum(_amount_cents(p) for p in payments if p["status"] == "confirmed")
    pending_cents = sum(_amount_cents(p) for p in payments if p["status"] == "pending")
    remaining_cents = max(0, total_cents - confirmed_cents)

    total = float(money.from_cents(total_cents))
    confirmed = float(money.from_cents(confirmed_cents))
    pending = float(money.from_cents(pending_cents))
    remaining = float(money.from_cents(remaining_cents))

    currency = (order.get("currency") or BASE_CURRENCY).upper()
    return {
        "total": total,
        "confirmed": confirmed,
        "pending": pending,
        "remaining": remaining,
        "total_cents": total_cents,
        "confirmed_cents": confirmed_cents,
        "pending_cents": pending_cents,
        "remaining_cents": remaining_cents,
        "currency": currency,
        # *_base — None если convert_to_base не смог (курс не задан админом).
        "total_base": convert_to_base(total, currency),
        "confirmed_base": convert_to_base(confirmed, currency),
        "pending_base": convert_to_base(pending, currency),
        "remaining_base": convert_to_base(remaining, currency),
        "base_currency": (BASE_CURRENCY or "USD").upper(),
    }


def mark_shipment_notified(demand_id: str) -> bool:
    """Атомарно «застолбить» отправку уведомления об отгрузке.

    Возвращает True, если demand_id записан впервые (т.е. уведомлять НАДО),
    и False, если уже был (другой процесс/проход опередил — не дублируем).

    Используется и MS-вебхуком (webapp), и поллером (bot) — общий Postgres
    обеспечивает дедуп между процессами. INSERT-if-absent атомарен, гонка
    двух процессов разрешается на уровне PRIMARY KEY.
    """
    if not demand_id:
        return False
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO notified_shipments (demand_id, notified_at) "
                "VALUES (%s, %s) ON CONFLICT (demand_id) DO NOTHING",
                (demand_id, now_str()),
            )
        else:
            cur.execute(
                "INSERT OR IGNORE INTO notified_shipments (demand_id, notified_at) VALUES (?, ?)",
                (demand_id, now_str()),
            )
        inserted = cur.rowcount > 0
        conn.commit()
    return inserted


def prune_notified_shipments(older_than_days: int = 30) -> int:
    """Удалить старые записи дедупа отгрузок (таблица иначе растёт без предела).

    demand_id уникален и больше не «всплывёт» спустя месяцы, поэтому хранить
    их вечно незачем. Возвращает число удалённых строк. now_str() формата
    'YYYY-MM-DD HH:MM:SS' лексикографически сортируется — сравнение строкой ок.
    """
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("DELETE FROM notified_shipments WHERE notified_at < ?"),
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
    return deleted


def prune_audit_log(retention_months: int = 6) -> int:
    """Удалить записи аудита старше retention_months (janitor, §13). Экспорт в
    Drive перед удалением — отдельной интеграцией (audit_archive_exports);
    здесь только чистка БД. Месяц ≈ 30 дней (для janitor-задачи достаточно).
    Возвращает число удалённых строк."""
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


def purge_soft_deleted(retention_days: int = 365) -> dict[str, int]:
    """Физически удалить soft-deleted строки (deleted_at IS NOT NULL) старше
    retention_days. Возвращает {table: removed}. Таблицы с deleted_at:
    orders, cash_deposits, returns. Удаление порциями (L2)."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    out: dict[str, int] = {}
    for table in ("orders", "cash_deposits", "returns"):
        out[table] = _batched_delete(table, "deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))
    return out


def set_order_ms_demand_id(order_id: int, ms_demand_id: str) -> bool:
    """Сохранить id демэнд-документа МойСклад на заказе. Legacy: новые
    заказы используют set_order_ms_customerorder_id."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET ms_demand_id = ?, updated_at = ? WHERE id = ?"),
            (ms_demand_id, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def set_order_ms_customerorder_id(order_id: int, co_id: str) -> bool:
    """Сохранить id customerorder МойСклад на заказе. Используется
    после успешного create_customerorder_from_request — нужно чтобы
    paymentin привязался к этому заказу через operations."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET ms_customerorder_id = ?, updated_at = ? WHERE id = ?"),
            (co_id, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def set_order_ms_cancel_synced(order_id: int) -> bool:
    """Отметить, что отмена заказа отражена в МойСклад (реверс customerorder).
    Идемпотентность ms_cancel: повторный реверс пропускается по этому полю."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET ms_cancel_synced_at = ?, updated_at = ? WHERE id = ?"),
            (now_str(), now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def get_payments_needing_ms_sync(limit: int = 100) -> list[dict]:
    """Confirmed-платежи, привязанные к заказу, которые ещё НЕ улетели
    в МойСклад (либо upload не сработал, либо ещё не пробовали).

    Используется и cron-retry'ем, и /sync_payments командой.
    Включает:
      - 'failed' — предыдущая попытка упала (MS_TOKEN, 429, 5xx и т.п.)
      - NULL    — confirmed до того как фича была деплоена, или
                  fire-and-forget hook не успел отработать (event loop
                  закрылся)
    Исключает уже синхронизированные (ms_paymentin_id IS NOT NULL).
    """
    query = (
        "SELECT * FROM payments "
        "WHERE status = 'confirmed' "
        "  AND order_id IS NOT NULL "
        "  AND ms_paymentin_id IS NULL "
        "ORDER BY confirmed_at ASC LIMIT ?"
    )
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_ms_sync_stats() -> dict:
    """Сводка статуса синхронизации платежей с МойСклад.
    Для /sync_payments команды — показывает админу что в каком состоянии.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            "SELECT "
            "  COUNT(*) FILTER (WHERE ms_paymentin_id IS NOT NULL) AS synced, "
            "  COUNT(*) FILTER (WHERE ms_sync_status = 'failed') AS failed, "
            "  COUNT(*) FILTER (WHERE status = 'confirmed' AND order_id IS NOT NULL "
            "                    AND ms_paymentin_id IS NULL "
            "                    AND (ms_sync_status IS NULL OR ms_sync_status != 'failed')) "
            "    AS never_tried "
            "FROM payments"
            if USE_POSTGRES
            # SQLite не поддерживает FILTER — используем SUM(CASE...)
            else "SELECT "
            "  SUM(CASE WHEN ms_paymentin_id IS NOT NULL THEN 1 ELSE 0 END) AS synced, "
            "  SUM(CASE WHEN ms_sync_status = 'failed' THEN 1 ELSE 0 END) AS failed, "
            "  SUM(CASE WHEN status = 'confirmed' AND order_id IS NOT NULL "
            "                AND ms_paymentin_id IS NULL "
            "                AND (ms_sync_status IS NULL OR ms_sync_status != 'failed') "
            "           THEN 1 ELSE 0 END) AS never_tried "
            "FROM payments"
        )
        row = cur.fetchone()
    if not row:
        return {"synced": 0, "failed": 0, "never_tried": 0}
    return {
        "synced": int(row["synced"] or 0) if USE_POSTGRES else int(row[0] or 0),
        "failed": int(row["failed"] or 0) if USE_POSTGRES else int(row[1] or 0),
        "never_tried": int(row["never_tried"] or 0) if USE_POSTGRES else int(row[2] or 0),
    }


async def get_recent_ms_sync_failures(limit: int = 5) -> list[dict]:
    """Последние failed-синхронизации с текстом ошибки. asyncpg Stage 8 (#21)."""
    return await adb_core.fetch(
        "SELECT id, amount, currency, order_id, ms_sync_error "
        "FROM payments WHERE ms_sync_status = 'failed' "
        "ORDER BY id DESC LIMIT $1",
        limit,
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


def get_last_cron_runs() -> list[dict]:
    """Вернуть по одной записи на task — последнюю по started_at.

    Используется в ops_monitor для алерта «cron не запускался».
    SQL: SELECT DISTINCT ON (task_name) для Postgres, GROUP BY + JOIN
    для SQLite (DISTINCT ON только pg-only).
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "SELECT DISTINCT ON (task_name) task_name, started_at, finished_at, "
                "status, error_message, metadata "
                "FROM cron_runs "
                "ORDER BY task_name, started_at DESC"
            )
        else:
            # SQLite: max(started_at) per task через GROUP BY с трюком
            # «max-of-tuple» (max(started_at || '|' || id) даёт стабильную
            # выборку даже при совпадении started_at).
            cur.execute(
                "SELECT cr.task_name, cr.started_at, cr.finished_at, cr.status, "
                "cr.error_message, cr.metadata "
                "FROM cron_runs cr "
                "INNER JOIN ("
                "  SELECT task_name, MAX(id) AS max_id "
                "  FROM cron_runs GROUP BY task_name"
                ") last ON cr.task_name = last.task_name AND cr.id = last.max_id "
                "ORDER BY cr.task_name"
            )
        return [dict(r) for r in cur.fetchall()]


def get_stale_crons(thresholds_hours: dict[str, float]) -> list[dict]:
    """Вернуть cron'ы, чей последний УСПЕШНЫЙ запуск старше порога.

    `thresholds_hours`: {'ms_sync_retry': 1.0, 'ops_monitor': 26.0, ...}
    Порог в часах. Если task'и нет в выдаче `get_last_cron_runs` —
    включаем в результат с last_success=None (значит, ни разу не было).

    Возвращаем: [{task_name, last_success_at, hours_ago, threshold_hours,
                  last_status, last_error}]
    """
    now = datetime.now()
    rows = get_last_cron_runs()
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


# ─── Per-user permissions (PR #44 / tech debt #3c) ──────────────────────────


def get_user_permission_overrides(user_id: int) -> dict[str, bool]:
    """Вернуть словарь permission_code → granted (True/False) для user'а.

    Только записи из user_permissions; defaults по роли — отдельно через
    ROLE_DEFAULTS (services.roles). Пустой dict = у юзера нет ни одного
    override, работают только дефолты.
    """
    if not user_id:
        return {}
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT permission_code, granted FROM user_permissions WHERE user_id = ?"),
            (int(user_id),),
        )
        out: dict[str, bool] = {}
        for r in cur.fetchall():
            d = dict(r)
            out[d["permission_code"]] = bool(d["granted"])
        return out


def set_user_permission_override(
    user_id: int,
    permission_code: str,
    granted: bool,
    updated_by: int,
) -> None:
    """UPSERT user_permissions: явный grant (True) или revoke (False)."""
    if not user_id or not permission_code:
        return
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                q(
                    "INSERT INTO user_permissions "
                    "(user_id, permission_code, granted, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (user_id, permission_code) DO UPDATE SET "
                    "granted = EXCLUDED.granted, "
                    "updated_at = EXCLUDED.updated_at, "
                    "updated_by = EXCLUDED.updated_by"
                ),
                (int(user_id), permission_code, 1 if granted else 0, now_str(), updated_by),
            )
        else:
            cur.execute(
                q(
                    "INSERT OR REPLACE INTO user_permissions "
                    "(user_id, permission_code, granted, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?)"
                ),
                (int(user_id), permission_code, 1 if granted else 0, now_str(), updated_by),
            )
        conn.commit()


def clear_user_permission_override(user_id: int, permission_code: str) -> bool:
    """Удалить override — юзер вернётся к role-default'у. Возвращает True если
    запись была."""
    if not user_id or not permission_code:
        return False
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("DELETE FROM user_permissions WHERE user_id = ? AND permission_code = ?"),
            (int(user_id), permission_code),
        )
        deleted = (cur.rowcount or 0) > 0
        conn.commit()
    return deleted


def reset_stale_in_progress_payments(older_than_minutes: int = 30) -> int:
    """Сбросить ms_sync_status='in_progress' у платежей, застрявших дольше N минут.

    Защита от orphan'ов: claim_payment_for_ms_sync ставит 'in_progress'
    ДО HTTP-POST в МойСклад. Если процесс убили mid-claim (Railway SIGTERM,
    OOM, увеличенное окно из-за init_demand_context), строка навсегда
    залипает — claim-UPDATE отвергает 'in_progress', retry никогда её не
    возьмёт.

    Вызывается из tasks/run_ms_sync_retry.main() в самом начале, до
    основной логики. Возвращает количество сброшенных строк.

    Порог по confirmed_at (а не отдельной колонке ms_sync_claimed_at) —
    достаточно точный для текущей нагрузки: легальный sync укладывается
    в секунды, 30+ минут — гарантированно orphan. Дополнительная колонка
    потребовала бы миграции и пока не оправдана.

    Порог вычисляем в Python через тот же `now_str()` (local TZ через
    `datetime.now()`), что и при записи `confirmed_at` — иначе бы
    SQLite/Postgres-side функции `datetime('now',...)`/`NOW()` вернули
    UTC, и сравнение строк в разных TZ всегда было бы False (тихий баг).
    """
    threshold = (datetime.now() - timedelta(minutes=older_than_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE payments "
                "SET ms_sync_status = NULL "
                "WHERE ms_sync_status = 'in_progress' "
                "  AND ms_paymentin_id IS NULL "
                "  AND confirmed_at < ?"
            ),
            (threshold,),
        )
        reset = cur.rowcount or 0
        conn.commit()
    if reset > 0:
        logger.warning(
            "reset_stale_in_progress_payments: сброшено %d orphan-строк "
            "(старше %d мин)",
            reset,
            older_than_minutes,
        )
    return reset


def claim_payment_for_ms_sync(payment_id: int) -> bool:
    """Атомарно «застолбить» платёж для синхронизации с МойСклад.

    Защита от race condition между cron-retry и in-process confirm-hook:
    оба могут одновременно решить «надо синкать» и оба вызовут POST в
    МойСклад → дубль paymentin.

    Логика: атомарный UPDATE-WHERE-status-not-in-progress. Только тот,
    кто выиграл гонку (rowcount == 1), идёт в МойСклад. Остальные
    видят False и тихо пропускают.

    Returns True если этот вызов застолбил; False если уже застолбили,
    уже syncнули или платежа нет.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        # Не используем NOT IN потому что SQLite не любит NULL-сравнения
        # таким способом. Явные проверки IS NULL OR != 'in_progress'.
        cur.execute(
            q(
                "UPDATE payments "
                "SET ms_sync_status = 'in_progress' "
                "WHERE id = ? "
                "  AND ms_paymentin_id IS NULL "
                "  AND (ms_sync_status IS NULL OR ms_sync_status != 'in_progress')"
            ),
            (payment_id,),
        )
        claimed = cur.rowcount > 0
        conn.commit()
    return claimed


def set_payment_ms_sync(
    payment_id: int,
    *,
    paymentin_id: str | None = None,
    status: str | None = None,
    error: str | None = None,
) -> bool:
    """Обновить состояние синхронизации платежа с МойСклад.
    status: 'synced' | 'failed' | None (не менять)."""
    fields = []
    params: list = []
    if paymentin_id is not None:
        fields.append("ms_paymentin_id = ?")
        params.append(paymentin_id)
    if status is not None:
        fields.append("ms_sync_status = ?")
        params.append(status)
    if error is not None:
        fields.append("ms_sync_error = ?")
        params.append(error[:500])  # обрезаем чтоб не раздуть row
    if not fields:
        return False
    params.append(payment_id)
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"UPDATE payments SET {', '.join(fields)} WHERE id = ?"),
            params,
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def find_payment_by_ms_paymentin_id(paymentin_id: str) -> dict | None:
    """Найти локальный платёж по ID paymentin в МойСклад.
    Используется когда МойСклад присылает webhook о удалении/изменении paymentin."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT * FROM payments WHERE ms_paymentin_id = ? LIMIT 1"),
            (paymentin_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def reset_payment_ms_sync(payment_id: int) -> bool:
    """Сбросить привязку к paymentin (он удалён в МойСклад).

    ms_paymentin_id → NULL, ms_sync_status → 'deleted_in_ms'.
    Существующий cron run_ms_sync_retry подберёт платёж при следующем
    запуске (условие: ms_paymentin_id IS NULL AND status='confirmed')
    и создаст новый paymentin автоматически.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE payments "
                "SET ms_paymentin_id = NULL, ms_sync_status = 'deleted_in_ms' "
                "WHERE id = ?"
            ),
            (payment_id,),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def find_order_by_ms_customerorder_id(co_id: str) -> dict | None:
    """Найти локальный заказ по ID customerorder в МойСклад.
    Используется для обработки webhook-событий customerorder."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT * FROM orders WHERE ms_customerorder_id = ? LIMIT 1"),
            (co_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def clear_order_ms_customerorder_id(order_id: int) -> bool:
    """Снять ссылку на customerorder (документ удалён в МойСклад).
    После сброса повторный approve заявки создаст новый customerorder."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET ms_customerorder_id = NULL, updated_at = ? WHERE id = ?"),
            (now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def delete_order(order_id: int, requested_by: int) -> bool:
    """Удалить заказ-черновик. Только status='draft' можно удалять, и
    только владелец (даже boss/admin не удаляет чужие — нужно жёстче
    через audit). Каскадно сносим order_items.

    Возвращает True если что-то удалили, False если заказ не найден,
    не draft, или не принадлежит requested_by.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        # Проверяем, что заказ существует, draft и принадлежит юзеру
        cur.execute(
            q("SELECT user_id, status FROM orders WHERE id = ?"),
            (order_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        if USE_POSTGRES:
            owner_id, status = row["user_id"], row["status"]
        else:
            owner_id, status = row[0], row[1]
        if owner_id != requested_by or status != "draft":
            return False

        # Каскад вручную, чтобы работало и в SQLite (FK off by default)
        cur.execute(q("DELETE FROM order_items WHERE order_id = ?"), (order_id,))
        cur.execute(q("DELETE FROM orders WHERE id = ?"), (order_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    if deleted:
        add_audit_log(
            requested_by,
            "",
            get_role(requested_by),
            "order_deleted",
            f"Удалён черновик заказа #{order_id}",
        )
    return deleted


def confirm_payment(
    payment_id: int, confirmed_by: int | None = None, confirmed_name: str = ""
) -> bool:
    """Подтвердить платёж. Если платёж привязан к заказу (order_id) —
    проверяем суммарно, не закрыли ли мы тем самым заказ полностью.
    Полностью означает: SUM(amount where status='confirmed') >= order.total.
    Тогда автоматически проставляем order.paid_confirmed_at."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE payments SET status = 'confirmed', confirmed_at = ? WHERE id = ? AND status = 'pending'"
            ),
            (now_str(), payment_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if not updated:
        return False
    payment = get_payment(payment_id)
    if confirmed_by and payment:
        add_audit_log(
            confirmed_by,
            confirmed_name,
            get_role(confirmed_by),
            "payment_confirmed",
            f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}",
        )
    # Если платёж был привязан к заказу — проверяем не закрылся ли заказ.
    if payment and payment.get("order_id"):
        _maybe_close_order_after_payment(
            payment["order_id"],
            confirmed_by,
            confirmed_name,
        )
        # Best-effort: синхронизируем входящий платёж в МойСклад.
        # Делаем fire-and-forget — БД-операция уже коммитнута, ошибка
        # MS-API не должна откатывать подтверждение. Статус синхрона
        # пишется в payments.ms_sync_status; failed можно ретраить
        # вручную или фоновой задачей.
        _trigger_ms_paymentin_sync(payment_id)
    return updated


def link_payment_to_order(
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
    """
    if not payment_id or not order_id:
        return {"ok": False, "error": "payment_id и order_id обязательны"}

    payment = get_payment(payment_id)
    if not payment:
        return {"ok": False, "error": "Платёж не найден"}
    if payment.get("order_id"):
        return {
            "ok": False,
            "error": f"Платёж уже привязан к заказу #{payment['order_id']}",
        }
    order = get_order(order_id)
    if not order:
        return {"ok": False, "error": "Заказ не найден"}
    if order.get("deleted_at"):
        return {"ok": False, "error": "Заказ удалён"}

    # Атомарно линкуем — защита от race: два параллельных линка к РАЗНЫМ
    # заказам, оба прошли проверки выше; UPDATE-WHERE-NULL выиграет только
    # один. WHERE id = ? обязательно дополняет, иначе при concurrent
    # link'е разных платежей к разным заказам мы случайно обновим не тот.
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE payments SET order_id = ? WHERE id = ? AND order_id IS NULL"),
            (order_id, payment_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if not updated:
        # Кто-то между нашими read и UPDATE прилинковал другой заказ.
        # Перечитываем чтобы вернуть оператору актуальную картину.
        fresh = get_payment(payment_id)
        return {
            "ok": False,
            "error": f"Параллельная привязка: платёж уже принадлежит заказу #{fresh.get('order_id') if fresh else '?'}",
        }

    add_audit_log(
        linked_by,
        linked_name or "",
        get_role(linked_by) or "",
        "payment_linked_to_order",
        f"Платёж #{payment_id} ({payment['amount']:,.2f} {payment.get('currency', 'USD')}) "
        f"привязан к заказу #{order_id} (был стендалон)",
    )

    result: dict = {"ok": True, "ms_sync_triggered": False, "order_closed": False}

    # Если платёж уже был подтверждён до линка — теперь это credit на заказ,
    # надо проверить не закрыли ли мы его и создать paymentin в МС.
    if payment.get("status") == "confirmed":
        _maybe_close_order_after_payment(order_id, linked_by, linked_name)
        # Проверяем статус заказа после _maybe_close.
        updated_order = get_order(order_id)
        if updated_order and updated_order.get("paid_confirmed_at"):
            result["order_closed"] = True
        _trigger_ms_paymentin_sync(payment_id)
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
    return await adb_core.fetch(
        "SELECT id, user_id, username, full_name, amount, currency, "
        "comment, confirmed_at, status "
        "FROM payments "
        "WHERE order_id IS NULL AND status = 'confirmed' "
        "ORDER BY id DESC LIMIT $1",
        int(limit),
    )


def _trigger_ms_paymentin_sync(payment_id: int) -> None:
    """Запустить async create_paymentin_for_payment в фоне.

    Три контекста вызова:
      1. Внутри активного event loop (редко: код уже в loop'е) —
         loop.create_task, fire-and-forget.
      2. В to_thread-воркере (типичный путь: confirm_payment вызывается
         через services.async_db → asyncio.to_thread из bot/webapp).
         В воркере НЕТ running loop, а MS-сессия привязана к ГЛАВНОМУ
         loop'у. Раньше тут делался asyncio.run(), который поднимал НОВЫЙ
         loop и дёргал сессию чужого loop'а → RuntimeError "Timeout
         context manager should be used inside a task" — синк молча падал
         на каждом подтверждении, и спасал только cron-retry. Теперь
         планируем корутину на loop сессии через run_coroutine_threadsafe.
      3. Истинно sync-контекст (cron-скрипт, главного loop'а нет) —
         asyncio.run поднимает свой loop и создаёт в нём свежую сессию.

    БД-confirm уже закоммичен к моменту вызова; ошибки синка некритичны
    (cron-retry добирает), поэтому всё best-effort.
    """
    import asyncio

    try:
        from services.ms_payments import create_paymentin_for_payment
    except Exception:
        return  # окружение без MS_TOKEN

    # (1) Уже внутри running loop'а — просто планируем задачу.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(create_paymentin_for_payment(payment_id))

        def _log_exc(t: asyncio.Task) -> None:
            if not t.cancelled() and (exc := t.exception()):
                logger.exception("ms_paymentin_sync payment #%d failed: %s", payment_id, exc)

        task.add_done_callback(_log_exc)
        return

    # (2) Воркер-тред: если главный loop с MS-сессией ещё жив — планируем
    # корутину НА НЕГО (там валидна сессия), не поднимая чужой loop.
    try:
        from services.moysklad import get_session_loop

        main_loop = get_session_loop()
    except Exception:
        main_loop = None
    if main_loop is not None and main_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                create_paymentin_for_payment(payment_id), main_loop
            )
        except Exception:
            logger.exception("ms_paymentin_sync payment #%d: schedule failed", payment_id)
        return

    # (3) Нет главного loop'а (cron) — свой короткий loop + свежая сессия.
    try:
        asyncio.run(create_paymentin_for_payment(payment_id))
    except Exception:
        # Статус failed уже записан внутри ms_payments — молча
        pass


def _maybe_close_order_after_payment(
    order_id: int,
    confirmed_by: int | None,
    confirmed_name: str,
) -> None:
    """Атомарно проверить, закрыт ли заказ суммой confirmed-платежей,
    и проставить paid_confirmed_at если да.

    Параллельный confirm двух платежей одного заказа без блокировки приводил
    к гонке: каждый вызов видел `remaining > 0` (потому что второй платёж
    ещё не был зафиксирован для текущей транзакции) и оба пропускали
    закрытие. Решение — `SELECT ... FOR UPDATE` на orders в начале
    транзакции: пока первый confirm считает summary и UPDATE'ит заказ,
    второй ждёт на lock'е и затем видит уже актуальные данные.

    Для SQLite (локальная разработка) FOR UPDATE не поддерживается, но
    там и нет конкуренции — один процесс. Условный SQL.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)

        # Lock: для Postgres эта строка блокирует order до conn.commit().
        if USE_POSTGRES:
            cur.execute(
                "SELECT paid_confirmed_at FROM orders WHERE id = %s FOR UPDATE",
                (order_id,),
            )
        else:
            cur.execute(
                "SELECT paid_confirmed_at FROM orders WHERE id = ?",
                (order_id,),
            )
        row = cur.fetchone()
        if not row:
            return
        already_closed = (row["paid_confirmed_at"] if USE_POSTGRES else row[0]) is not None
        if already_closed:
            return

        # Пересчёт ВНУТРИ транзакции — видим актуальную сумму confirmed.
        # Считаем в копейках (точное сравнение) — без epsilon-костыля.
        cur.execute(
            q(
                f"SELECT {_SUM_PAYMENTS_CENTS} AS s FROM payments "
                "WHERE order_id = ? AND status = 'confirmed'"
            ),
            (order_id,),
        )
        r = cur.fetchone()
        confirmed_cents = int((r["s"] if USE_POSTGRES else r[0]) or 0)

        cur.execute(
            q(f"SELECT {_SUM_ORDER_TOTAL_CENTS} AS t FROM order_items WHERE order_id = ?"),
            (order_id,),
        )
        r = cur.fetchone()
        total_cents = int((r["t"] if USE_POSTGRES else r[0]) or 0)

        if confirmed_cents < total_cents:
            return  # ещё не полностью оплачен

        # Закрываем
        cur.execute(
            q(
                "UPDATE orders "
                "SET paid_confirmed_at = ?, paid_confirmed_by = ?, "
                "    paid_confirmed_by_name = ?, "
                "    paid_at = COALESCE(paid_at, ?), "
                "    updated_at = ? "
                "WHERE id = ? AND paid_confirmed_at IS NULL"
            ),
            (
                now_str(),
                confirmed_by or 0,
                confirmed_name or "",
                now_str(),
                now_str(),
                order_id,
            ),
        )
        closed = cur.rowcount > 0
        conn.commit()

    if closed:
        add_audit_log(
            confirmed_by or 0,
            confirmed_name,
            get_role(confirmed_by) if confirmed_by else "",
            "order_fully_paid",
            f"Заказ #{order_id} полностью оплачен "
            f"(сумма подтверждённых платежей: {money.format_cents(confirmed_cents)})",
        )


def reject_payment(
    payment_id: int, rejected_by: int | None = None, rejected_name: str = ""
) -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE payments SET status = 'rejected' WHERE id = ? AND status = 'pending'"),
            (payment_id,),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated and rejected_by:
        payment = get_payment(payment_id)
        if payment:
            add_audit_log(
                rejected_by,
                rejected_name,
                get_role(rejected_by),
                "payment_rejected",
                f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}",
            )
    return updated


def archive_payment(payment_id: int, archived_by: int, archived_name: str) -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("UPDATE payments SET status = 'archived' WHERE id = ?"), (payment_id,))
        updated = cur.rowcount > 0
        conn.commit()
    if updated:
        payment = get_payment(payment_id)
        if payment:
            add_audit_log(
                archived_by,
                archived_name,
                get_role(archived_by),
                "payment_archived",
                f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}",
            )
    return updated


def get_payment(payment_id: int) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM payments WHERE id = ?"), (payment_id,))
        row = cur.fetchone()
    return dict(row) if row else None


async def get_payments_report(since: str | None = None, until: str | None = None) -> list[dict]:
    """Подтверждённые платежи за период. asyncpg-миграция Stage 7 (задача #21):
    нативный async через adb_core. Вызов — только handlers/payments (`await adb`)."""
    query = "SELECT * FROM payments WHERE status = 'confirmed'"
    params: list = []
    if since:
        params.append(since)
        query += f" AND created_at >= ${len(params)}"
    if until:
        params.append(until)
        query += f" AND created_at <= ${len(params)}"
    query += " ORDER BY created_at DESC"
    return await adb_core.fetch(query, *params)


def get_cashbox_stats(since: str | None = None, until: str | None = None) -> dict:
    """Касса: подтверждённые поступления денег за период.

    Сумма CONFIRMED платежей по `created_at` в [since, until] — «сколько
    реально получили». Группируем по валюте (платежи бывают в разных
    валютах); `total_cents` — суммарно в копейках по всем валютам, а
    `by_currency` даёт разбивку, если валют больше одной.

    since/until — ISO-строки 'YYYY-MM-DD HH:MM:SS' в локальной TZ (как
    `created_at` пишется через now_str()), поэтому лексикографическое
    сравнение строк корректно. Порог считаем в Python и передаём
    параметром — НЕ сравниваем с SQL NOW()/datetime('now') (разные TZ,
    silent-bug; см. CLAUDE.md).
    """
    query = (
        f"SELECT currency, COUNT(*) AS cnt, {_SUM_PAYMENTS_CENTS} AS total_cents "
        "FROM payments WHERE status = 'confirmed'"
    )
    params: list = []
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if until:
        query += " AND created_at <= ?"
        params.append(until)
    query += " GROUP BY currency"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = [dict(r) for r in cur.fetchall()]
    total_cents = sum(int(r["total_cents"] or 0) for r in rows)
    count = sum(int(r["cnt"] or 0) for r in rows)
    by_currency = {(r.get("currency") or "—"): int(r["total_cents"] or 0) for r in rows}
    return {"total_cents": total_cents, "count": count, "by_currency": by_currency}


async def get_summary_by_employee(
    since: str | None = None, until: str | None = None
) -> list[dict]:
    """Платежи по сотрудникам (confirmed). asyncpg Stage 8 (#21)."""
    query = (
        "SELECT full_name, currency, SUM(amount) as total, COUNT(*) as count "
        "FROM payments WHERE status = 'confirmed'"
    )
    params: list = []
    if since:
        params.append(since)
        query += f" AND created_at >= ${len(params)}"
    if until:
        params.append(until)
        query += f" AND created_at <= ${len(params)}"
    query += " GROUP BY full_name, currency ORDER BY total DESC"
    return await adb_core.fetch(query, *params)


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


def get_order(order_id: int) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM orders WHERE id = ?"), (order_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_orders_by_ids(order_ids: list[int]) -> dict[int, dict]:
    """Батч-загрузка заказов по id → словарь {id: order_dict}.
    order_ids дедуплицируется — placeholder'ы не расходуем впустую."""
    if not order_ids:
        return {}
    unique_ids = list(set(order_ids))
    placeholders = ",".join(["?"] * len(unique_ids))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"SELECT * FROM orders WHERE id IN ({placeholders})"),
            unique_ids,
        )
        rows = cur.fetchall()
    return {r["id"]: dict(r) for r in rows}


def get_user_orders(user_id: int, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM orders WHERE user_id = ?"
    params: list = [user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


async def get_all_orders(status: str | None = None) -> list[dict]:
    """Все заказы (опц. фильтр по статусу). asyncpg-миграция Stage 6
    (задача #21): нативный async через adb_core. Вызов — только webapp
    (`await adb.get_all_orders()`)."""
    query = "SELECT * FROM orders"
    params: list = []
    if status:
        params.append(status)
        query += f" WHERE status = ${len(params)}"
    query += " ORDER BY created_at DESC"
    return await adb_core.fetch(query, *params)


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
    None → все (boss/admin). Исключаем soft-deleted (deleted_at IS NULL).
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
    where = "(" + " OR ".join(conds) + ") AND deleted_at IS NULL"
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
        return [dict(r) for r in cur.fetchall()]


def update_order_agent(order_id: int, agent_id: str, agent_name: str) -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET agent_id = ?, agent_name = ?, updated_at = ? WHERE id = ?"),
            (agent_id, agent_name, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def set_order_payment(
    order_id: int,
    payment_type: str,
    due_date: str | None = None,
) -> bool:
    """Установить тип оплаты заказа (paid|credit).

    Для credit обязателен due_date (ISO YYYY-MM-DD) — дата к которой
    клиент обязался погасить долг. Для paid due_date игнорируется
    и обнуляется (на случай если заказ переводят из credit обратно).

    Не сбрасывает paid_at — закрытый долг остаётся закрытым.
    """
    if payment_type not in ("paid", "credit"):
        return False
    if payment_type == "credit" and not due_date:
        return False
    with get_conn() as conn:
        cur = get_cursor(conn)
        if payment_type == "paid":
            cur.execute(
                q(
                    "UPDATE orders SET payment_type = ?, due_date = NULL, "
                    "updated_at = ? WHERE id = ?"
                ),
                (payment_type, now_str(), order_id),
            )
        else:
            cur.execute(
                q("UPDATE orders SET payment_type = ?, due_date = ?, updated_at = ? WHERE id = ?"),
                (payment_type, due_date, now_str(), order_id),
            )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def mark_order_paid(
    order_id: int,
    marked_by: int,
    marked_by_name: str,
    amount: float | None = None,
    username: str = "",
) -> tuple[bool, int | None]:
    """Менеджер отмечает поступление денег по заказу.

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
    """
    # Все шаги — внутри одной транзакции с lock'ом на заказ.
    # Раньше без блокировки два менеджера могли одновременно отметить
    # частичные суммы 70+70 на остаток 100 — оба проходили проверку
    # `amount ≤ remaining` (каждый считал «свой» remaining до второго),
    # и в pending копилось 140 при долге 100. Босс потом разбирался.
    # Теперь FOR UPDATE на orders сериализует параллельные mark_paid'ы.
    from config import BASE_CURRENCY

    with get_conn() as conn:
        cur = get_cursor(conn)

        # Lock заказа
        if USE_POSTGRES:
            cur.execute(
                "SELECT payment_type, currency, agent_name, paid_confirmed_at "
                "FROM orders WHERE id = %s FOR UPDATE",
                (order_id,),
            )
        else:
            cur.execute(
                "SELECT payment_type, currency, agent_name, paid_confirmed_at "
                "FROM orders WHERE id = ?",
                (order_id,),
            )
        row = cur.fetchone()
        if not row:
            return (False, None)
        if USE_POSTGRES:
            payment_type = row["payment_type"]
            currency = row["currency"] or BASE_CURRENCY
            agent_name = row["agent_name"]
            already_closed = row["paid_confirmed_at"] is not None
        else:
            payment_type = row[0]
            currency = row[1] or BASE_CURRENCY
            agent_name = row[2]
            already_closed = row[3] is not None

        if payment_type != "credit":
            return (False, None)
        if already_closed:
            return (False, None)

        # Под locкам считаем суммы — гарантия что между recompute и
        # INSERT никто другой не добавит payment. Считаем в копейках (точно).
        cur.execute(
            q(
                f"SELECT {_SUM_PAYMENTS_CENTS} AS s FROM payments "
                "WHERE order_id = ? AND status IN ('pending', 'confirmed')"
            ),
            (order_id,),
        )
        r = cur.fetchone()
        used_cents = int((r["s"] if USE_POSTGRES else r[0]) or 0)

        cur.execute(
            q(f"SELECT {_SUM_ORDER_TOTAL_CENTS} AS t FROM order_items WHERE order_id = ?"),
            (order_id,),
        )
        r = cur.fetchone()
        total_cents = int((r["t"] if USE_POSTGRES else r[0]) or 0)
        remaining_cents = max(0, total_cents - used_cents)

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
            cur.execute(
                "INSERT INTO payments "
                "(user_id, username, full_name, amount, amount_cents, currency, comment, "
                " status, created_at, order_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s) "
                "RETURNING id",
                (
                    marked_by,
                    username,
                    marked_by_name,
                    amount,
                    amount_cents,
                    currency,
                    comment,
                    now_str(),
                    order_id,
                ),
            )
            payment_id = cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO payments "
                "(user_id, username, full_name, amount, amount_cents, currency, comment, "
                " status, created_at, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    marked_by,
                    username,
                    marked_by_name,
                    amount,
                    amount_cents,
                    currency,
                    comment,
                    now_str(),
                    order_id,
                ),
            )
            payment_id = cur.lastrowid

        # paid_at — флаг «менеджер хоть раз отметил». COALESCE сохраняет
        # самое раннее время для последующих частичных платежей.
        cur.execute(
            q("UPDATE orders SET paid_at = COALESCE(paid_at, ?), updated_at = ? WHERE id = ?"),
            (now_str(), now_str(), order_id),
        )
        conn.commit()

    remaining_after = float(money.from_cents(max(0, remaining_cents - amount_cents)))
    add_audit_log(
        marked_by,
        marked_by_name,
        get_role(marked_by),
        "debt_payment_claimed",
        f"Заказ #{order_id}: менеджер отметил {amount:,.0f} {currency} "
        f"(после подтверждения останется: {remaining_after:,.0f})",
    )
    return (True, payment_id)


def confirm_all_pending_payments_for_order(
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
    """
    payments = get_payments_for_order(order_id)
    pending = [p for p in payments if p["status"] == "pending"]
    n = 0
    for p in pending:
        if confirm_payment(p["id"], confirmed_by, confirmed_by_name):
            n += 1
    return n


def reject_all_pending_payments_for_order(
    order_id: int,
    rejected_by: int,
    rejected_by_name: str,
) -> int:
    """Босс отклоняет ВСЕ pending платежи по заказу. Аналог confirm_all."""
    payments = get_payments_for_order(order_id)
    pending = [p for p in payments if p["status"] == "pending"]
    n = 0
    for p in pending:
        if reject_payment(p["id"], rejected_by, rejected_by_name):
            n += 1
    return n


def confirm_payment_received(
    order_id: int,
    confirmed_by: int,
    confirmed_by_name: str,
) -> bool:
    """Босс подтверждает что деньги по заказу реально пришли в кассу.

    Возможно только если менеджер до этого уже отметил paid_at
    (нельзя подтвердить то, чего ещё нет). Идемпотентно: повторный
    вызов на уже подтверждённый заказ возвращает False.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE orders "
                "SET paid_confirmed_at = ?, paid_confirmed_by = ?, "
                "    paid_confirmed_by_name = ?, updated_at = ? "
                "WHERE id = ? AND paid_at IS NOT NULL "
                "AND paid_confirmed_at IS NULL"
            ),
            (now_str(), confirmed_by, confirmed_by_name, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated:
        order = get_order(order_id)
        details = (
            f"Получение денег по заказу #{order_id} подтверждено "
            f"(клиент: {order.get('agent_name') or '—'}, "
            f"менеджер: {order.get('full_name') or '—'})"
            if order
            else f"Получение денег по #{order_id} подтверждено"
        )
        add_audit_log(
            confirmed_by,
            confirmed_by_name,
            get_role(confirmed_by),
            "payment_confirmed",
            details,
        )
    return updated


def reject_payment_received(
    order_id: int,
    rejected_by: int,
    rejected_by_name: str,
) -> bool:
    """Босс отклоняет: «нет, денег не вижу». Сбрасываем paid_at в NULL,
    цикл начинается заново — менеджер должен снова отметить когда деньги
    реально появятся, и подтвердить заново.

    Срабатывает только на «висящих» подтверждениях (paid_at стоит,
    paid_confirmed_at пуст). На уже подтверждённый — игнор.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "UPDATE orders "
                "SET paid_at = NULL, updated_at = ? "
                "WHERE id = ? AND paid_at IS NOT NULL "
                "AND paid_confirmed_at IS NULL"
            ),
            (now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated:
        order = get_order(order_id)
        details = (
            f"Подтверждение оплаты #{order_id} отклонено "
            f"(клиент: {order.get('agent_name') or '—'}, "
            f"менеджер: {order.get('full_name') or '—'})"
            if order
            else f"Подтверждение #{order_id} отклонено"
        )
        add_audit_log(
            rejected_by,
            rejected_by_name,
            get_role(rejected_by),
            "payment_rejected_received",
            details,
        )
    return updated


async def get_pending_confirmations(user_id: int | None = None) -> list[dict]:
    """Заказы, где менеджер отметил оплату, но босс ещё не подтвердил.
    user_id фильтрует по автору. asyncpg Stage 8 (#21)."""
    query = "SELECT * FROM orders WHERE paid_at IS NOT NULL AND paid_confirmed_at IS NULL"
    params: list = []
    if user_id is not None:
        params.append(user_id)
        query += f" AND user_id = ${len(params)}"
    query += " ORDER BY paid_at ASC, id ASC"
    return await adb_core.fetch(query, *params)


def get_open_debts(
    user_id: int | None = None,
    due_through: str | None = None,
) -> list[dict]:
    """Список открытых долгов (credit + paid_at IS NULL).

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
    """
    query = (
        "SELECT * FROM orders "
        "WHERE payment_type = 'credit' AND paid_confirmed_at IS NULL "
        "AND status IN ('approved', 'shipped')"
    )
    params: list = []
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    if due_through is not None:
        query += " AND due_date IS NOT NULL AND due_date <= ?"
        params.append(due_through)
    query += (
        " ORDER BY due_date ASC NULLS LAST, id ASC"
        if USE_POSTGRES
        else " ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date ASC, id ASC"
    )
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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
        "AND EXISTS (SELECT 1 FROM payments p "
        "            WHERE p.order_id = o.id AND p.status = 'pending')"
    )
    params: list = []
    if user_id is not None:
        params.append(user_id)
        query += f" AND o.user_id = ${len(params)}"
    query += " ORDER BY o.id ASC"
    return await adb_core.fetch(query, *params)


def update_order_currency(order_id: int, currency: str) -> bool:
    """Установить валюту заказа. Применяется ко всем позициям одного
    ордера — менять между позициями не имеет смысла."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET currency = ?, updated_at = ? WHERE id = ?"),
            (currency, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def update_order_status(order_id: int, status: str) -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?"),
            (status, now_str(), order_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    return updated


def add_order_item(
    order_id: int,
    product_name: str,
    product_href: str,
    quantity: float,
    unit: str,
    price: float = 0.0,
    note: str = "",
) -> int:
    price_cents = money.to_cents(price or 0)
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price, price_cents, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
                (order_id, product_name, product_href, quantity, unit, price, price_cents, note),
            )
            item_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price, price_cents, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (order_id, product_name, product_href, quantity, unit, price, price_cents, note),
            )
            item_id = cur.lastrowid
        conn.commit()
    return item_id


def get_order_items(order_id: int) -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM order_items WHERE order_id = ?"), (order_id,))
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_order_items_by_ids(order_ids: list[int]) -> dict[int, list[dict]]:
    """Батч-загрузка позиций для списка заказов — один SQL вместо N.
    Возвращает {order_id: [items, ...]}. Заказы без позиций отсутствуют
    в результате (вызывающий должен использовать .get(oid, []))."""
    if not order_ids:
        return {}
    unique_ids = list(set(order_ids))
    placeholders = ",".join(["?"] * len(unique_ids))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"SELECT * FROM order_items WHERE order_id IN ({placeholders})"),
            unique_ids,
        )
        rows = cur.fetchall()
    grouped: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        grouped.setdefault(d["order_id"], []).append(d)
    return grouped


def get_order_item(item_id: int) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM order_items WHERE id = ?"), (item_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def remove_order_item(item_id: int) -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
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


def approve_shipment_request(req_id: int, approved_by: int, approved_name: str) -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("""UPDATE shipment_requests
               SET status = 'approved', approved_by = ?, approved_by_name = ?, approved_at = ?
               WHERE id = ? AND status = 'pending'"""),
            (approved_by, approved_name, now_str(), req_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated:
        req = get_shipment_request(req_id)
        if req is not None:
            update_order_status(req["order_id"], "approved")
            add_audit_log(
                approved_by,
                approved_name,
                get_role(approved_by),
                "shipment_approved",
                f"Заявка #{req_id} одобрена (заказ #{req['order_id']} от {req['full_name']})",
            )
    return updated


def reject_shipment_request(req_id: int, rejected_by: int, rejected_name: str) -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("""UPDATE shipment_requests
               SET status = 'rejected', approved_by = ?, approved_by_name = ?, approved_at = ?
               WHERE id = ? AND status = 'pending'"""),
            (rejected_by, rejected_name, now_str(), req_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated:
        req = get_shipment_request(req_id)
        if req is not None:
            update_order_status(req["order_id"], "rejected")
            add_audit_log(
                rejected_by,
                rejected_name,
                get_role(rejected_by),
                "shipment_rejected",
                f"Заявка #{req_id} отклонена (заказ #{req['order_id']} от {req['full_name']})",
            )
    return updated


# ─── Загрузка предопределённых пользователей ──────────────────────────────────


def _load_predefined_users():
    try:
        from config import ADMIN_IDS, BOSS_IDS

        try:
            MANAGER_IDS = __import__("config").MANAGER_IDS
        except Exception:
            MANAGER_IDS = []
        try:
            PREDEFINED_USERS = __import__("config").PREDEFINED_USERS
        except Exception:
            PREDEFINED_USERS = []

        with get_conn() as conn:
            cur = get_cursor(conn)
            for u in PREDEFINED_USERS:
                cur.execute(
                    q(
                        "INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING"
                    ),
                    (u["user_id"], "", u.get("full_name", ""), u["role"], now_str()),
                )
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
