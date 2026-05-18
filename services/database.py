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
    from psycopg2.extras import RealDictCursor
    logger.info("Используется PostgreSQL")
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
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL)
        try:
            yield conn
        finally:
            conn.close()
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


# ─── Инициализация ────────────────────────────────────────────────────────────


def init_db():
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
                id           {id_type},
                user_id      BIGINT NOT NULL,
                username     TEXT,
                full_name    TEXT,
                amount       REAL NOT NULL,
                currency     TEXT NOT NULL DEFAULT 'USD',
                comment      TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL,
                confirmed_at TEXT
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
                id          {id_type},
                user_id     BIGINT NOT NULL,
                full_name   TEXT,
                status      TEXT NOT NULL DEFAULT 'draft',
                comment     TEXT,
                agent_id    TEXT,
                agent_name  TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
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

        # Миграции в отдельных транзакциях
        migrations = [
            ("user_roles", "moysklad_employee_id", "TEXT"),
            ("user_roles", "ms_sync_status", "TEXT DEFAULT 'pending'"),
            ("user_roles", "created_at", "TEXT"),
            # Цена за единицу для позиции заказа (в основной валюте,
            # т.е. как пользователь ввёл — например 150.50 USD).
            # При создании demand в МойСклад умножаем на 100 (минорные единицы).
            ("order_items", "price", "REAL DEFAULT 0"),
        ]
        for table, column, col_type in migrations:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                conn.commit()
            except Exception:
                conn.rollback()  # Колонка уже существует — норм

        # Индексы для snapshot-таблиц
        snapshot_indexes = [
            "CREATE INDEX IF NOT EXISTS idx_ms_products_folder ON ms_products(folder_id)",
            "CREATE INDEX IF NOT EXISTS idx_ms_stock_folder ON ms_stock(folder_id)",
            "CREATE INDEX IF NOT EXISTS idx_ms_categories_parent ON ms_categories(parent_id)",
        ]
        for sql in snapshot_indexes:
            try:
                cur.execute(sql)
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.debug("Индекс не создан: %s", e)

    logger.info("База данных инициализирована")
    _load_predefined_users()



def _migrate(cur):
    """Добавляем новые колонки в существующие таблицы."""
    migrations = [
        ("user_roles", "moysklad_employee_id", "TEXT"),
        ("user_roles", "ms_sync_status", "TEXT DEFAULT 'pending'"),
        ("user_roles", "created_at", "TEXT"),
    ]
    for table, column, col_type in migrations:
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except Exception:
            pass  # Колонка уже существует


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
      - Если ALLOWED_USERS НЕ задан (пустой) → manager (backward compat)
      - Иначе → guest (нулевые права, нужно повысить через /addrole)

    Раньше всем по умолчанию ставили 'manager' — это открывало бот любому
    Telegram-юзеру, который найдёт его @username.
    """
    from config import BOSS_IDS, ALLOWED_USERS

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
        elif not ALLOWED_USERS:
            # Whitelist не настроен — оставляем старое поведение,
            # чтобы не сломать существующие развёртки одним deploy-ом.
            role = "manager"
        else:
            role = "guest"

        cur.execute(
            q("INSERT INTO user_roles (user_id, username, full_name, role, created_at) VALUES (?, ?, ?, ?, ?)"),
            (user_id, username, full_name, role, now_str()),
        )
        conn.commit()


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
    if deleted and removed_by and target:
        add_audit_log(
            removed_by, removed_name, get_role(removed_by),
            "user_removed",
            f"Удалён {target['full_name']} (ID: {user_id}, роль: {target['role']})",
        )
    return deleted


# ─── Платежи ─────────────────────────────────────────────────────────────────


def add_payment(user_id, username, full_name, amount, currency, comment) -> int:
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute("""
                INSERT INTO payments (user_id, username, full_name, amount, currency, comment, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s) RETURNING id
            """, (user_id, username, full_name, amount, currency, comment, now_str()))
            payment_id = cur.fetchone()["id"]
        else:
            cur.execute("""
                INSERT INTO payments (user_id, username, full_name, amount, currency, comment, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (user_id, username, full_name, amount, currency, comment, now_str()))
            payment_id = cur.lastrowid
        conn.commit()
    return payment_id


def confirm_payment(payment_id: int, confirmed_by: int = None, confirmed_name: str = "") -> bool:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE payments SET status = 'confirmed', confirmed_at = ? WHERE id = ? AND status = 'pending'"),
            (now_str(), payment_id),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated and confirmed_by:
        payment = get_payment(payment_id)
        if payment:
            add_audit_log(
                confirmed_by, confirmed_name, get_role(confirmed_by),
                "payment_confirmed",
                f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}",
            )
    return updated


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
    """Батч-загрузка заказов по id → словарь {id: order_dict}."""
    if not order_ids:
        return {}
    placeholders = ",".join(["?"] * len(order_ids))
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"SELECT * FROM orders WHERE id IN ({placeholders})"),
            list(order_ids),
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
