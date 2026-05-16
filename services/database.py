"""
База данных SQLite — платежи и роли пользователей
"""
import os
import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "payments.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Таблица платежей
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
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
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            full_name TEXT,
            role      TEXT NOT NULL DEFAULT 'employee'
        )
    """)

    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

    # Подтягиваем предопределённых пользователей из config
    _load_predefined_users()


# ─── Роли ────────────────────────────────────────────────────────────────────

def get_role(user_id: int) -> str:
    """Получить роль пользователя. По умолчанию — employee."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT role FROM user_roles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else "employee"


def set_role(user_id: int, username: str, full_name: str, role: str) -> bool:
    """Установить роль пользователя."""
    if role not in ("admin", "boss", "manager", "employee"):
        return False
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_roles (user_id, username, full_name, role)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            role = excluded.role
    """, (user_id, username, full_name, role))
    conn.commit()
    conn.close()
    return True


def get_all_users() -> list[dict]:
    """Получить всех пользователей с ролями."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_roles ORDER BY role, full_name")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ensure_user(user_id: int, username: str, full_name: str, admin_ids: list[int]):
    """
    Гарантирует наличие пользователя в БД с правильной ролью.
    Если пользователь уже есть — обновляет только username/full_name, роль не трогает.
    Если нет — определяет роль по спискам из config и создаёт запись.
    """
    from config import MANAGER_IDS, BOSS_IDS

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT role FROM user_roles WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row:
        # Пользователь уже есть — просто освежим username/full_name
        cur.execute("""
            UPDATE user_roles
               SET username = ?, full_name = ?
             WHERE user_id = ?
        """, (username, full_name, user_id))
        conn.commit()
        conn.close()
        return

    # Новый пользователь — определяем роль
    if user_id in admin_ids:
        role = "admin"
    elif user_id in BOSS_IDS:
        role = "boss"
    elif user_id in MANAGER_IDS:
        role = "manager"
    else:
        role = "employee"

    cur.execute("""
        INSERT INTO user_roles (user_id, username, full_name, role)
        VALUES (?, ?, ?, ?)
    """, (user_id, username, full_name, role))
    conn.commit()
    conn.close()


# ─── Платежи ─────────────────────────────────────────────────────────────────

def add_payment(user_id: int, username: str, full_name: str,
                amount: float, currency: str, comment: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO payments (user_id, username, full_name, amount, currency, comment, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (user_id, username, full_name, amount, currency, comment,
          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    payment_id = cur.lastrowid
    conn.commit()
    conn.close()
    return payment_id


def confirm_payment(payment_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE payments SET status = 'confirmed', confirmed_at = ?
        WHERE id = ? AND status = 'pending'
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payment_id))
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def reject_payment(payment_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE payments SET status = 'rejected'
        WHERE id = ? AND status = 'pending'
    """, (payment_id,))
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_payment(payment_id: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_payments_report(since: str = None, until: str = None) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    query = "SELECT * FROM payments WHERE status = 'confirmed'"
    params = []
    if since:
        query += " AND created_at >= ?"
        params.append(since)
    if until:
        query += " AND created_at <= ?"
        params.append(until)
    query += " ORDER BY created_at DESC"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_summary_by_employee(since: str = None, until: str = None) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
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
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_user(user_id: int, username: str, full_name: str, role: str) -> bool:
    """Добавить пользователя вручную."""
    if role not in ("admin", "boss", "manager", "employee"):
        return False
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_roles (user_id, username, full_name, role)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            role = excluded.role
    """, (user_id, username, full_name, role))
    conn.commit()
    conn.close()
    return True


def remove_user(user_id: int) -> bool:
    """Удалить пользователя из базы."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def _load_predefined_users():
    """Загрузить предопределённых пользователей из config при запуске."""
    try:
        from config import ADMIN_IDS, BOSS_IDS, MANAGER_IDS
        try:
            from config import PREDEFINED_USERS
        except ImportError:
            PREDEFINED_USERS = []

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # Из PREDEFINED_USERS (если задан в config_local)
        for u in PREDEFINED_USERS:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO NOTHING
            """, (u["user_id"], "", u.get("full_name", ""), u["role"]))

        # Из ADMIN_IDS, BOSS_IDS, MANAGER_IDS
        for uid in ADMIN_IDS:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role)
                VALUES (?, ?, ?, 'admin')
                ON CONFLICT(user_id) DO NOTHING
            """, (uid, "", "Admin"))

        for uid in BOSS_IDS:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role)
                VALUES (?, ?, ?, 'boss')
                ON CONFLICT(user_id) DO NOTHING
            """, (uid, "", "Boss"))

        for uid in MANAGER_IDS:
            cur.execute("""
                INSERT INTO user_roles (user_id, username, full_name, role)
                VALUES (?, ?, ?, 'manager')
                ON CONFLICT(user_id) DO NOTHING
            """, (uid, "", "Manager"))

        conn.commit()
        conn.close()
        logger.info("Предопределённые пользователи загружены")
    except Exception as e:
        logger.warning("Ошибка загрузки пользователей: %s", e)