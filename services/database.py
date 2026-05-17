"""
База данных — работает с SQLite (локально) и PostgreSQL (продакшен).
Выбор движка автоматический — если есть DATABASE_URL → PostgreSQL,
иначе → SQLite.
"""

import os
import logging
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ─── Определяем тип БД ────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH = os.environ.get("DB_PATH", "payments.db")
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    logger.info("Используется PostgreSQL")
else:
    import sqlite3
    logger.info("Используется SQLite: %s", DB_PATH)


# ─── Подключение ──────────────────────────────────────────────────────────────


@contextmanager
def get_conn():
    """Контекстный менеджер для подключения к БД."""
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
    """Возвращает курсор с правильным row_factory."""
    if USE_POSTGRES:
        return conn.cursor(cursor_factory=RealDictCursor)
    return conn.cursor()


# Для PostgreSQL — заменяем ? на %s в запросах
def q(query: str) -> str:
    """Адаптировать SQL под выбранный движок."""
    if USE_POSTGRES:
        return query.replace("?", "%s")
    return query


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─── Инициализация таблиц ─────────────────────────────────────────────────────


def init_db():
    with get_conn() as conn:
        cur = get_cursor(conn)

        if USE_POSTGRES:
            id_type = "SERIAL PRIMARY KEY"
        else:
            id_type = "INTEGER PRIMARY KEY AUTOINCREMENT"

        # Таблица платежей
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS payments (
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
            )
        """)

        # Таблица ролей
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_roles (
                user_id   BIGINT PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                role      TEXT NOT NULL DEFAULT 'employee'
            )
        """)

        # Аудит лог
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS audit_log (
                id         {id_type},
                user_id    BIGINT NOT NULL,
                full_name  TEXT,
                role       TEXT,
                action     TEXT NOT NULL,
                details    TEXT,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()
    logger.info("База данных инициализирована")

    _load_predefined_users()


# ─── Роли ────────────────────────────────────────────────────────────────────


def get_role(user_id: int) -> str:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT role FROM user_roles WHERE user_id = ?"), (user_id,))
        row = cur.fetchone()
    if not row:
        return "employee"
    return row["role"] if USE_POSTGRES else row[0]


def set_role(user_id: int, username: str, full_name: str, role: str) -> bool:
    if role not in ("admin", "boss", "manager", "employee"):
        return False
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    role = EXCLUDED.role
            """, (user_id, username, full_name, role))
        else:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    role = excluded.role
            """, (user_id, username, full_name, role))
        conn.commit()
    return True


def get_all_users() -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("SELECT * FROM user_roles ORDER BY role, full_name")
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def ensure_user(user_id: int, username: str, full_name: str, admin_ids: list[int]):
    from config import MANAGER_IDS, BOSS_IDS

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
        elif user_id in MANAGER_IDS:
            role = "manager"
        else:
            role = "employee"

        cur.execute(
            q("INSERT INTO user_roles (user_id, username, full_name, role) VALUES (?, ?, ?, ?)"),
            (user_id, username, full_name, role),
        )
        conn.commit()


def add_user(user_id: int, username: str, full_name: str, role: str) -> bool:
    return set_role(user_id, username, full_name, role)


def remove_user(user_id: int, removed_by: int = None, removed_name: str = "") -> bool:
    # Перед удалением сохраняем данные для лога
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
            f"Удалён пользователь {target['full_name']} (ID: {user_id}, роль: {target['role']})",
        )
    return deleted


# ─── Платежи ─────────────────────────────────────────────────────────────────


def add_payment(user_id, username, full_name, amount, currency, comment) -> int:
    with get_conn() as conn:
        cur = get_cursor(conn)
        if USE_POSTGRES:
            cur.execute("""
                INSERT INTO payments (user_id, username, full_name, amount, currency, comment, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
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
        add_audit_log(
            confirmed_by, confirmed_name, get_role(confirmed_by),
            "payment_confirmed",
            f"Платёж #{payment_id}: {payment['amount']:,.0f} {payment['currency']} от {payment['full_name']}",
        )
    return updated

def archive_payment(payment_id: int, archived_by: int, archived_name: str) -> bool:
    """Архивировать платёж — данные сохраняются, но помечаются как archived."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("UPDATE payments SET status = 'archived' WHERE id = ?"),
            (payment_id,),
        )
        updated = cur.rowcount > 0
        conn.commit()
    if updated:
        payment = get_payment(payment_id)
        add_audit_log(
            archived_by, archived_name, get_role(archived_by),
            "payment_archived",
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
        add_audit_log(
            rejected_by, rejected_name, get_role(rejected_by),
            "payment_rejected",
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


# ─── Загрузка предопределённых пользователей ──────────────────────────────────


def _load_predefined_users():
    try:
        from config import ADMIN_IDS, BOSS_IDS, MANAGER_IDS
        try:
            from config import PREDEFINED_USERS
        except ImportError:
            PREDEFINED_USERS = []

        with get_conn() as conn:
            cur = get_cursor(conn)

            for u in PREDEFINED_USERS:
                cur.execute(
                    q("INSERT INTO user_roles (user_id, username, full_name, role) VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO NOTHING"),
                    (u["user_id"], "", u.get("full_name", ""), u["role"]),
                )

            for uid in ADMIN_IDS:
                cur.execute(
                    q("INSERT INTO user_roles (user_id, username, full_name, role) VALUES (?, ?, ?, 'admin') ON CONFLICT(user_id) DO NOTHING"),
                    (uid, "", "Admin"),
                )
            for uid in BOSS_IDS:
                cur.execute(
                    q("INSERT INTO user_roles (user_id, username, full_name, role) VALUES (?, ?, ?, 'boss') ON CONFLICT(user_id) DO NOTHING"),
                    (uid, "", "Boss"),
                )
            for uid in MANAGER_IDS:
                cur.execute(
                    q("INSERT INTO user_roles (user_id, username, full_name, role) VALUES (?, ?, ?, 'manager') ON CONFLICT(user_id) DO NOTHING"),
                    (uid, "", "Manager"),
                )
            conn.commit()
        logger.info("Предопределённые пользователи загружены")
    except Exception as e:
        logger.warning("Ошибка загрузки пользователей: %s", e)