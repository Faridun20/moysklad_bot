"""
База данных — SQLite (локально) и PostgreSQL (продакшен).
"""

import os
import time
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Any

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
    _load_predefined_users()
    logger.info("База данных инициализирована (CREATE TABLE only)")


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
                amount        REAL NOT NULL
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
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO credit_limits (agent_id, agent_name, limit_amount, set_by, notes, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (agent_id) DO UPDATE SET limit_amount = EXCLUDED.limit_amount, "
                "set_by = EXCLUDED.set_by, notes = EXCLUDED.notes, updated_at = EXCLUDED.updated_at",
                (agent_id, agent_name, limit_amount, set_by, notes, now_str(), now_str()),
            )
        else:
            cur.execute(
                "INSERT INTO credit_limits (agent_id, agent_name, limit_amount, set_by, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id) DO UPDATE SET limit_amount = excluded.limit_amount, "
                "set_by = excluded.set_by, notes = excluded.notes, updated_at = excluded.updated_at",
                (agent_id, agent_name, limit_amount, set_by, notes, now_str(), now_str()),
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
    debt = 0.0
    for oid in order_ids:
        summary = get_order_payment_summary(oid)
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q(
                    "SELECT COALESCE(SUM(total_amount), 0) AS s FROM returns "
                    "WHERE order_id = ? AND status = 'confirmed' AND (deleted_at IS NULL)"
                ),
                (oid,),
            )
            row = cur.fetchone()
        returns_sum = float((row["s"] if USE_POSTGRES else row[0]) or 0)
        debt += max(0.0, summary["remaining"] - returns_sum)
    return debt


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


def get_stale_pending_orders(hours: int = 48) -> list[dict]:
    """Заявки, висящие в pending дольше `hours` (для stale-мониторинга, §13).
    Берём COALESCE(submitted_at, created_at)."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT * FROM orders WHERE status = 'pending' AND (deleted_at IS NULL) "
                "AND COALESCE(submitted_at, created_at) < ? ORDER BY created_at ASC"
            ),
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


# ─── IMPLEMENTATION.md Фаза 4: сдача наличных (cash deposits) ─────────────────
#
# Менеджер сдаёт собранные деньги в кассу; босс/бухгалтер подтверждает.
# Закрывает дыру «собрал у клиента, но не сдал в офис». Используем новую
# модель: при покрытии заказа подтверждёнными сдачами он переходит в 'paid'
# (payment_confirmed=1). order total берём из get_order_payment_summary.


def _order_total(order_id: int) -> float:
    return float(get_order_payment_summary(order_id)["total"])


def _order_confirmed_deposit_amount(order_id: int) -> float:
    """Сколько уже распределено на заказ подтверждёнными сдачами."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT COALESCE(SUM(cdo.amount_allocated), 0) AS s "
                "FROM cash_deposit_orders cdo "
                "JOIN cash_deposits d ON d.id = cdo.deposit_id "
                "WHERE cdo.order_id = ? AND d.status = 'confirmed' AND (d.deleted_at IS NULL)"
            ),
            (order_id,),
        )
        row = cur.fetchone()
    return float((row["s"] if USE_POSTGRES else row[0]) or 0)


def _order_allocated_deposit_amount(order_id: int) -> float:
    """Сколько распределено на заказ сдачами в статусе pending ИЛИ confirmed.
    M3: при FIFO-распределении учитываем и pending — иначе две сдачи подряд
    «забронируют» один и тот же остаток дважды."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT COALESCE(SUM(cdo.amount_allocated), 0) AS s "
                "FROM cash_deposit_orders cdo "
                "JOIN cash_deposits d ON d.id = cdo.deposit_id "
                "WHERE cdo.order_id = ? AND d.status IN ('pending', 'confirmed') "
                "AND (d.deleted_at IS NULL)"
            ),
            (order_id,),
        )
        row = cur.fetchone()
    return float((row["s"] if USE_POSTGRES else row[0]) or 0)


def get_manager_open_orders_for_deposit(manager_id: int) -> list[dict]:
    """Отгруженные неоплаченные заказы менеджера (для распределения сдачи).
    Возвращает [{id, total, covered, remaining}] по возрастанию created_at.
    covered учитывает уже распределённое pending+confirmed-сдачами (M3)."""
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
        total = _order_total(oid)
        covered = _order_allocated_deposit_amount(oid)
        if total - covered > 0.01:
            out.append(
                {
                    "id": oid,
                    "total": total,
                    "covered": covered,
                    "remaining": max(0.0, total - covered),
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

        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO cash_deposits (manager_id, amount, deposited_at, status, created_at) "
                "VALUES (%s, %s, %s, 'pending', %s) RETURNING id",
                (manager_id, amount, now_str(), now_str()),
            )
            deposit_id = cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO cash_deposits (manager_id, amount, deposited_at, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (manager_id, amount, now_str(), now_str()),
            )
            deposit_id = cur.lastrowid
        for order_id, alloc in allocs:
            cur.execute(
                q(
                    "INSERT INTO cash_deposit_orders (deposit_id, order_id, amount_allocated, is_manual) "
                    "VALUES (?, ?, ?, ?)"
                ),
                (deposit_id, order_id, alloc, 1 if is_manual else 0),
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
        if _order_confirmed_deposit_amount(oid) + 0.01 >= _order_total(oid):
            with get_conn() as conn:
                cur = get_cursor(conn)
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


def get_cash_deposit(deposit_id: int) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT * FROM cash_deposits WHERE id = ?"), (deposit_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_cash_deposit_orders(deposit_id: int) -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT order_id, amount_allocated FROM cash_deposit_orders WHERE deposit_id = ?"),
            (deposit_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def get_cash_deposit_orders_batch(deposit_ids: list[int]) -> dict[int, list[dict]]:
    """Батч-версия get_cash_deposit_orders: {deposit_id: [{order_id, amount_allocated}]}.
    Один SQL вместо N (был N+1 в /api/deposits/pending). Депозиты без привязанных
    заказов в результат не попадают — caller использует .get(id, [])."""
    if not deposit_ids:
        return {}
    unique_ids = list(set(deposit_ids))
    placeholders = ",".join(["?"] * len(unique_ids))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                f"SELECT deposit_id, order_id, amount_allocated FROM cash_deposit_orders "
                f"WHERE deposit_id IN ({placeholders})"
            ),
            unique_ids,
        )
        rows = cur.fetchall()
    grouped: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        # Форма элемента — как у get_cash_deposit_orders (без deposit_id).
        grouped.setdefault(d["deposit_id"], []).append(
            {"order_id": d["order_id"], "amount_allocated": d["amount_allocated"]}
        )
    return grouped


def get_manager_cash_deposits(manager_id: int, limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT * FROM cash_deposits WHERE manager_id = ? AND (deleted_at IS NULL) "
                "ORDER BY created_at DESC LIMIT ?"
            ),
            (manager_id, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def get_deposit_confirmers() -> list[int]:
    """user_id ролей, которые подтверждают сдачи: admin/boss/bookkeeper."""
    try:
        users = get_all_users()
    except Exception:
        return []
    return [u["user_id"] for u in users if u["role"] in ("admin", "boss", "bookkeeper")]


def get_pending_cash_deposits() -> list[dict]:
    """Сдачи, ждущие подтверждения (для боса/бухгалтера)."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT * FROM cash_deposits WHERE status = 'pending' AND (deleted_at IS NULL) "
                "ORDER BY deposited_at ASC"
            )
        )
        return [dict(r) for r in cur.fetchall()]


def get_overdue_undeposited_orders(days: int = 2) -> list[dict]:
    """Отгруженные неоплаченные заказы старше `days` (cash-эскалация, §7.6)."""
    from datetime import timedelta

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT * FROM orders WHERE status = 'shipped' AND payment_confirmed = 0 "
                "AND (deleted_at IS NULL) AND COALESCE(shipped_at, created_at) < ? "
                "ORDER BY user_id, created_at"
            ),
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


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


def get_batches_expiring_within(days: int = 7) -> list[dict]:
    """Партии с остатком, истекающие в ближайшие `days` дней (§9.4)."""
    from datetime import timedelta

    cutoff = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT * FROM product_batches WHERE qty_remaining > 0 "
                "AND expiry_date IS NOT NULL AND expiry_date <= ? ORDER BY expiry_date ASC"
            ),
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]


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

    # H1 + Round 6 RACE-2: не плодим параллельные возвраты по одному заказу.
    # Advisory-lock по order_id сериализует две одновременные create_return
    # (раньше TOCTOU между SELECT COUNT и INSERT пропускал оба, потом каждый
    # confirm_return наращивал returned_qty → overflow > quantity).
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

    # H1: режем количество по доступному остатку (quantity - returned_qty) и
    # пересчитываем сумму по цене позиции — не доверяем переданному amount.
    oitems = {it["id"]: it for it in get_order_items(order_id)}
    clamped: list[tuple] = []
    for oitem_id, qty, _amount in items:
        oi = oitems.get(oitem_id)
        if not oi:
            continue
        available = float(oi.get("quantity", 0) or 0) - float(oi.get("returned_qty", 0) or 0)
        take = min(float(qty), available)
        if take <= 0:
            continue
        price = float(oi.get("price", 0) or 0)
        clamped.append((oitem_id, take, round(take * price, 2)))
    if not clamped:
        return {"ok": False, "error": "Нет позиций, доступных к возврату"}
    items = clamped

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

    total_amount = round(sum(float(a) for _, _, a in items), 2)
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "refund_method, created_by, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s) RETURNING id",
                (order_id, return_type, reason, total_amount, refund_method, created_by, now_str()),
            )
            return_id = cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO returns (order_id, return_type, reason, total_amount, "
                "refund_method, created_by, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (order_id, return_type, reason, total_amount, refund_method, created_by, now_str()),
            )
            return_id = cur.lastrowid
        for oitem_id, qty, amount in items:
            cur.execute(
                q(
                    "INSERT INTO return_items (return_id, order_item_id, qty, amount) "
                    "VALUES (?, ?, ?, ?)"
                ),
                (return_id, oitem_id, qty, amount),
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
            cur.execute(
                q(
                    "INSERT INTO cash_deposits (manager_id, amount, deposited_at, status, "
                    "confirmed_by, confirmed_at, notes, created_at) "
                    "VALUES (?, ?, ?, 'confirmed', ?, ?, ?, ?)"
                ),
                (
                    (order or {}).get("user_id") or confirmed_by,
                    -float(ret["total_amount"]),
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


def get_pending_returns() -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT * FROM returns WHERE status = 'pending' AND (deleted_at IS NULL) "
                "ORDER BY created_at ASC"
            )
        )
        return [dict(r) for r in cur.fetchall()]


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


def get_unsynced_managers() -> list[dict]:
    """Получить менеджеров без привязки к МойСклад."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("""
            SELECT * FROM user_roles
            WHERE role = 'manager'
            AND (moysklad_employee_id IS NULL OR ms_sync_status = 'pending')
        """)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO payments
                    (user_id, username, full_name, amount, currency, comment, status, order_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id
            """,
                (user_id, username, full_name, amount, currency, comment, order_id, now_str()),
            )
            payment_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO payments
                    (user_id, username, full_name, amount, currency, comment, status, order_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
                (user_id, username, full_name, amount, currency, comment, order_id, now_str()),
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


def get_order_payment_summary(order_id: int) -> dict:
    """Сумма по заказу: total / confirmed / pending / remaining.

    Используется для отображения «оплачено X из Y, остаток Z» и для
    решения, закрыт ли заказ (remaining == 0).
    """
    order = get_order(order_id)
    if not order:
        return {"total": 0.0, "confirmed": 0.0, "pending": 0.0, "remaining": 0.0}
    items = get_order_items(order_id)
    total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
    payments = get_payments_for_order(order_id)
    confirmed = sum(p["amount"] for p in payments if p["status"] == "confirmed")
    pending = sum(p["amount"] for p in payments if p["status"] == "pending")
    remaining = max(0.0, total - confirmed)
    return {
        "total": total,
        "confirmed": confirmed,
        "pending": pending,
        "remaining": remaining,
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


def get_recent_ms_sync_failures(limit: int = 5) -> list[dict]:
    """Последние failed-синхронизации с текстом ошибки — для UI команды."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT id, amount, currency, order_id, ms_sync_error "
                "FROM payments "
                "WHERE ms_sync_status = 'failed' "
                "ORDER BY id DESC LIMIT ?"
            ),
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


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


def _trigger_ms_paymentin_sync(payment_id: int) -> None:
    """Запустить async create_paymentin_for_payment в фоне.

    Стратегия:
      - Внутри активного event loop (aiogram/aiohttp в bot/webapp) —
        кидаем create_task. Fire-and-forget: ошибки логируются в
        ms_payments, БД-confirm уже закоммичен.
      - В чисто sync-контексте (cron-скрипт, который не делает
        confirm_payment напрямую, но если делает — короткоживущий
        процесс) — поднимаем мини-loop через asyncio.run.

    asyncio.get_running_loop() в Python 3.12+ — правильный способ;
    бросает RuntimeError если нет активного loop'а, что нам нужно
    как сигнал для fallback на asyncio.run.
    """
    import asyncio

    try:
        from services.ms_payments import create_paymentin_for_payment
    except Exception:
        return  # окружение без MS_TOKEN

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Не в async-контексте — короткоживущий loop
        try:
            asyncio.run(create_paymentin_for_payment(payment_id))
        except Exception:
            # Статус failed уже записан внутри ms_payments — молча
            pass
        return

    # В активном loop'е — fire-and-forget. Сохраняем ссылку чтобы
    # не было RuntimeWarning «Task was destroyed».
    task = loop.create_task(create_paymentin_for_payment(payment_id))

    def _log_exc(t: asyncio.Task) -> None:
        if not t.cancelled() and (exc := t.exception()):
            logger.exception("ms_paymentin_sync payment #%d failed: %s", payment_id, exc)

    task.add_done_callback(_log_exc)


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

        # Пересчёт ВНУТРИ транзакции — видим актуальную сумму confirmed
        cur.execute(
            q(
                "SELECT COALESCE(SUM(amount), 0) AS s FROM payments "
                "WHERE order_id = ? AND status = 'confirmed'"
            ),
            (order_id,),
        )
        r = cur.fetchone()
        confirmed_sum = float((r["s"] if USE_POSTGRES else r[0]) or 0)

        cur.execute(
            q("SELECT COALESCE(SUM(quantity * price), 0) AS t FROM order_items WHERE order_id = ?"),
            (order_id,),
        )
        r = cur.fetchone()
        total = float((r["t"] if USE_POSTGRES else r[0]) or 0)

        if confirmed_sum < total - 0.01:
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
            f"(сумма подтверждённых платежей: {confirmed_sum:,.0f})",
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


def get_payments_report(since: str | None = None, until: str | None = None) -> list[dict]:
    query = "SELECT * FROM payments WHERE status = 'confirmed'"
    params = []
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if until:
        query += " AND created_at <= ?"
        params.append(until)
    query += " ORDER BY created_at DESC"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_summary_by_employee(since: str | None = None, until: str | None = None) -> list[dict]:
    query = """
        SELECT full_name, currency, SUM(amount) as total, COUNT(*) as count
        FROM payments WHERE status = 'confirmed'
    """
    params = []
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if until:
        query += " AND created_at <= ?"
        params.append(until)
    query += " GROUP BY full_name, currency ORDER BY total DESC"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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


def get_audit_log(limit: int = 50, user_id: int | None = None) -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        if user_id:
            cur.execute(
                q("SELECT * FROM audit_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?"),
                (user_id, limit),
            )
        else:
            cur.execute(
                q("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?"),
                (limit,),
            )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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


def get_all_orders(status: str | None = None) -> list[dict]:
    query = "SELECT * FROM orders"
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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
        # INSERT никто другой не добавит payment.
        cur.execute(
            q(
                "SELECT COALESCE(SUM(amount), 0) AS s FROM payments "
                "WHERE order_id = ? AND status IN ('pending', 'confirmed')"
            ),
            (order_id,),
        )
        r = cur.fetchone()
        used = float((r["s"] if USE_POSTGRES else r[0]) or 0)

        cur.execute(
            q("SELECT COALESCE(SUM(quantity * price), 0) AS t FROM order_items WHERE order_id = ?"),
            (order_id,),
        )
        r = cur.fetchone()
        total = float((r["t"] if USE_POSTGRES else r[0]) or 0)
        remaining = max(0.0, total - used)

        # Если amount не задан — берём остаток (полная доплата)
        if amount is None:
            amount = remaining
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return (False, None)
        if amount <= 0:
            return (False, None)
        # Не даём ввести больше остатка
        if amount > remaining + 0.01:
            amount = remaining
            if amount <= 0:
                return (False, None)

        # INSERT payment в той же транзакции
        comment = f"Оплата по заказу #{order_id}" + (f" ({agent_name})" if agent_name else "")
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO payments "
                "(user_id, username, full_name, amount, currency, comment, "
                " status, created_at, order_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) "
                "RETURNING id",
                (
                    marked_by,
                    username,
                    marked_by_name,
                    amount,
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
                "(user_id, username, full_name, amount, currency, comment, "
                " status, created_at, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    marked_by,
                    username,
                    marked_by_name,
                    amount,
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

    remaining_after = max(0.0, remaining - amount)
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


def get_pending_confirmations(user_id: int | None = None) -> list[dict]:
    """Заказы, где менеджер отметил оплату, но босс ещё не подтвердил.
    user_id фильтрует по автору (для менеджера — показать свои)."""
    query = "SELECT * FROM orders WHERE paid_at IS NOT NULL AND paid_confirmed_at IS NULL"
    params: list = []
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    query += " ORDER BY paid_at ASC, id ASC"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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


def get_paid_orders_awaiting_confirmation(user_id: int | None = None) -> list[dict]:
    """Paid-заказы с pending-платежом, ожидающие подтверждения боссом.

    Когда босс одобряет отгрузку по заказу payment_type='paid', авто-
    создаётся pending-платёж (фиксация поступления денег + синк в МойСклад).
    Credit-долги уже видны через get_open_debts; здесь — ТОЛЬКО paid, чтобы
    дать боссу surface для подтверждения в WebApp (таб «Платежи»).

    user_id — если указан, только заказы этого менеджера; иначе все.
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
        query += " AND o.user_id = ?"
        params.append(user_id)
    query += " ORDER BY o.id ASC"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute(
                """
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
                (order_id, product_name, product_href, quantity, unit, price, note),
            )
            item_id = cur.fetchone()["id"]
        else:
            cur.execute(
                """
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (order_id, product_name, product_href, quantity, unit, price, note),
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


def get_pending_requests() -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            "SELECT * FROM shipment_requests WHERE status = 'pending' ORDER BY created_at DESC"
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


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
