"""
База данных — SQLite (локально) и PostgreSQL (продакшен).
"""

import os
import time
import logging
from datetime import datetime
from contextlib import contextmanager

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
    import psycopg2
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
                _PG_POOL_MIN, _PG_POOL_MAX,
            )
        return _pg_connection_pool
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
            return self._cur.execute(query, params) if params is not None else self._cur.execute(query)
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
        conn = pool.getconn()
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
            f"""CREATE TABLE IF NOT EXISTS user_roles (
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

            f"""CREATE TABLE IF NOT EXISTS ms_products (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                folder_id  TEXT,
                code       TEXT,
                unit       TEXT,
                href       TEXT,
                updated_at TEXT
            )""",

            f"""CREATE TABLE IF NOT EXISTS ms_categories (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                parent_id  TEXT,
                href       TEXT,
                updated_at TEXT
            )""",

            f"""CREATE TABLE IF NOT EXISTS ms_counterparties (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                phone      TEXT,
                href       TEXT,
                updated_at TEXT
            )""",

            f"""CREATE TABLE IF NOT EXISTS ms_employees (
                ms_id      TEXT PRIMARY KEY,
                name       TEXT,
                href       TEXT,
                updated_at TEXT
            )""",

            f"""CREATE TABLE IF NOT EXISTS ms_stock (
                ms_id       TEXT PRIMARY KEY,
                name        TEXT,
                folder_id   TEXT,
                folder_name TEXT,
                unit        TEXT,
                stock       REAL DEFAULT 0,
                reserve     REAL DEFAULT 0,
                updated_at  TEXT
            )""",

            f"""CREATE TABLE IF NOT EXISTS ms_snapshot_meta (
                dataset           TEXT PRIMARY KEY,
                last_refresh      TEXT,
                last_full_refresh TEXT,
                last_webhook_at   TEXT,
                rows_count        INTEGER DEFAULT 0,
                status            TEXT
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
            ("payments", "ms_sync_error",  "TEXT"),
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



# ─── Роли ────────────────────────────────────────────────────────────────────


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
    valid_roles = ("admin", "boss", "manager", "guest")
    if role not in valid_roles:
        return False
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    role = EXCLUDED.role
            """, (user_id, username, full_name, role, now_str()))
        else:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    role = excluded.role
            """, (user_id, username, full_name, role, now_str()))
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
            q("INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, ?, ?, ?)"),
            (user_id, username, full_name, role, now_str()),
        )
        conn.commit()
    _invalidate_role_cache(user_id)


def set_moysklad_employee(user_id: int, ms_employee_id: str, status: str = "linked") -> bool:
    """Привязать Telegram пользователя к сотруднику МойСклад."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE user_roles SET moysklad_employee_id = ?, ms_sync_status = ? WHERE user_id = ?"),
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


def remove_user(user_id: int, removed_by: int = None, removed_name: str = "") -> bool:
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
            removed_by, removed_name, get_role(removed_by),
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
            cur.execute("""
                INSERT INTO payments
                    (user_id, username, full_name, amount, currency, comment, status, order_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id
            """, (user_id, username, full_name, amount, currency, comment, order_id, now_str()))
            payment_id = cur.fetchone()["id"]
        else:
            cur.execute("""
                INSERT INTO payments
                    (user_id, username, full_name, amount, currency, comment, status, order_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """, (user_id, username, full_name, amount, currency, comment, order_id, now_str()))
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
            q(f"SELECT * FROM payments WHERE order_id IN ({placeholders}) "
              f"ORDER BY created_at ASC"),
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
    total = sum(
        float(it.get("quantity", 0)) * float(it.get("price", 0) or 0)
        for it in items
    )
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
            if USE_POSTGRES else
            # SQLite не поддерживает FILTER — используем SUM(CASE...)
            "SELECT "
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
        "synced":      int(row["synced"] or 0) if USE_POSTGRES else int(row[0] or 0),
        "failed":      int(row["failed"] or 0) if USE_POSTGRES else int(row[1] or 0),
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
            requested_by, "", get_role(requested_by),
            "order_deleted",
            f"Удалён черновик заказа #{order_id}",
        )
    return deleted


def confirm_payment(payment_id: int, confirmed_by: int = None, confirmed_name: str = "") -> bool:
    """Подтвердить платёж. Если платёж привязан к заказу (order_id) —
    проверяем суммарно, не закрыли ли мы тем самым заказ полностью.
    Полностью означает: SUM(amount where status='confirmed') >= order.total.
    Тогда автоматически проставляем order.paid_confirmed_at."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE payments SET status = 'confirmed', confirmed_at = ? WHERE id = ? AND status = 'pending'"),
            (now_str(), payment_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if not updated:
        return False
    payment = get_payment(payment_id)
    if confirmed_by and payment:
        add_audit_log(
            confirmed_by, confirmed_name, get_role(confirmed_by),
            "payment_confirmed",
            f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}",
        )
    # Если платёж был привязан к заказу — проверяем не закрылся ли заказ.
    if payment and payment.get("order_id"):
        _maybe_close_order_after_payment(
            payment["order_id"], confirmed_by, confirmed_name,
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
    task.add_done_callback(lambda t: t.exception() and None)


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
        already_closed = (
            row["paid_confirmed_at"] if USE_POSTGRES else row[0]
        ) is not None
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
            q(
                "SELECT COALESCE(SUM(quantity * price), 0) AS t "
                "FROM order_items WHERE order_id = ?"
            ),
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
                now_str(), confirmed_by or 0, confirmed_name or "",
                now_str(), now_str(), order_id,
            ),
        )
        closed = cur.rowcount > 0
        conn.commit()

    if closed:
        add_audit_log(
            confirmed_by or 0, confirmed_name,
            get_role(confirmed_by) if confirmed_by else "",
            "order_fully_paid",
            f"Заказ #{order_id} полностью оплачен "
            f"(сумма подтверждённых платежей: {confirmed_sum:,.0f})",
        )


def reject_payment(payment_id: int, rejected_by: int = None, rejected_name: str = "") -> bool:
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
                rejected_by, rejected_name, get_role(rejected_by),
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
                archived_by, archived_name, get_role(archived_by),
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


def get_payments_report(since: str = None, until: str = None) -> list[dict]:
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


def get_summary_by_employee(since: str = None, until: str = None) -> list[dict]:
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
            q("INSERT INTO audit_log (user_id, full_name, role, action, details, created_at) VALUES (?, ?, ?, ?, ?, ?)"),
            (user_id, full_name, role, action, details, now_str()),
        )
        conn.commit()


def get_audit_log(limit: int = 50, user_id: int = None) -> list[dict]:
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
            cur.execute("""
                INSERT INTO orders (user_id, full_name, status, comment, created_at, updated_at)
                VALUES (%s, %s, 'draft', %s, %s, %s) RETURNING id
            """, (user_id, full_name, comment, now_str(), now_str()))
            order_id = cur.fetchone()["id"]
        else:
            cur.execute("""
                INSERT INTO orders (user_id, full_name, status, comment, created_at, updated_at)
                VALUES (?, ?, 'draft', ?, ?, ?)
            """, (user_id, full_name, comment, now_str(), now_str()))
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


def get_user_orders(user_id: int, status: str = None) -> list[dict]:
    query = "SELECT * FROM orders WHERE user_id = ?"
    params = [user_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(query), params)
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def get_all_orders(status: str = None) -> list[dict]:
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
                q(
                    "UPDATE orders SET payment_type = ?, due_date = ?, "
                    "updated_at = ? WHERE id = ?"
                ),
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
            q(
                "SELECT COALESCE(SUM(quantity * price), 0) AS t "
                "FROM order_items WHERE order_id = ?"
            ),
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
        comment = (
            f"Оплата по заказу #{order_id}"
            + (f" ({agent_name})" if agent_name else "")
        )
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO payments "
                "(user_id, username, full_name, amount, currency, comment, "
                " status, created_at, order_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) "
                "RETURNING id",
                (marked_by, username, marked_by_name, amount, currency,
                 comment, now_str(), order_id),
            )
            payment_id = cur.fetchone()["id"]
        else:
            cur.execute(
                "INSERT INTO payments "
                "(user_id, username, full_name, amount, currency, comment, "
                " status, created_at, order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (marked_by, username, marked_by_name, amount, currency,
                 comment, now_str(), order_id),
            )
            payment_id = cur.lastrowid

        # paid_at — флаг «менеджер хоть раз отметил». COALESCE сохраняет
        # самое раннее время для последующих частичных платежей.
        cur.execute(
            q(
                "UPDATE orders SET paid_at = COALESCE(paid_at, ?), updated_at = ? "
                "WHERE id = ?"
            ),
            (now_str(), now_str(), order_id),
        )
        conn.commit()

    remaining_after = max(0.0, remaining - amount)
    add_audit_log(
        marked_by, marked_by_name, get_role(marked_by),
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
            if order else f"Получение денег по #{order_id} подтверждено"
        )
        add_audit_log(
            confirmed_by, confirmed_by_name, get_role(confirmed_by),
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
            if order else f"Подтверждение #{order_id} отклонено"
        )
        add_audit_log(
            rejected_by, rejected_by_name, get_role(rejected_by),
            "payment_rejected_received",
            details,
        )
    return updated


def get_pending_confirmations(user_id: int | None = None) -> list[dict]:
    """Заказы, где менеджер отметил оплату, но босс ещё не подтвердил.
    user_id фильтрует по автору (для менеджера — показать свои)."""
    query = (
        "SELECT * FROM orders "
        "WHERE paid_at IS NOT NULL AND paid_confirmed_at IS NULL"
    )
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
    query += " ORDER BY due_date ASC NULLS LAST, id ASC" if USE_POSTGRES \
        else " ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date ASC, id ASC"
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


def add_order_item(order_id: int, product_name: str, product_href: str,
                   quantity: float, unit: str, price: float = 0.0,
                   note: str = "") -> int:
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute("""
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (order_id, product_name, product_href, quantity, unit, price, note))
            item_id = cur.fetchone()["id"]
        else:
            cur.execute("""
                INSERT INTO order_items
                    (order_id, product_name, product_href, quantity, unit, price, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (order_id, product_name, product_href, quantity, unit, price, note))
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
            cur.execute("""
                INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, created_at)
                VALUES (%s, %s, %s, 'pending', %s, %s) RETURNING id
            """, (order_id, user_id, full_name, comment, now_str()))
            req_id = cur.fetchone()["id"]
        else:
            cur.execute("""
                INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, created_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
            """, (order_id, user_id, full_name, comment, now_str()))
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
        cur.execute("SELECT * FROM shipment_requests WHERE status = 'pending' ORDER BY created_at DESC")
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
        update_order_status(req["order_id"], "approved")
        add_audit_log(
            approved_by, approved_name, get_role(approved_by),
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
        update_order_status(req["order_id"], "rejected")
        add_audit_log(
            rejected_by, rejected_name, get_role(rejected_by),
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
                    q("INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING"),
                    (u["user_id"], "", u.get("full_name", ""), u["role"], now_str()),
                )
            for uid in ADMIN_IDS:
                cur.execute(
                    q("INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, 'Admin', 'admin', ?) ON CONFLICT(user_id) DO NOTHING"),
                    (uid, "", now_str()),
                )
            for uid in BOSS_IDS:
                cur.execute(
                    q("INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, 'Boss', 'boss', ?) ON CONFLICT(user_id) DO NOTHING"),
                    (uid, "", now_str()),
                )
            for uid in MANAGER_IDS:
                cur.execute(
                    q("INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, 'Manager', 'manager', ?) ON CONFLICT(user_id) DO NOTHING"),
                    (uid, "", now_str()),
                )
            conn.commit()
        logger.info("Предопределённые пользователи загружены")
    except Exception as e:
        logger.warning("Ошибка загрузки пользователей: %s", e)
