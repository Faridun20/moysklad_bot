"""
База данных SQLite для хранения платежей сотрудников
"""

import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = "payments.db"


def init_db():
    """Создать таблицы если не существуют."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            username    TEXT,
            full_name   TEXT,
            amount      REAL NOT NULL,
            currency    TEXT NOT NULL DEFAULT 'USD',
            comment     TEXT,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TEXT NOT NULL,
            confirmed_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")


def add_payment(user_id: int, username: str, full_name: str,
                amount: float, currency: str, comment: str) -> int:
    """Добавить новый платёж. Возвращает ID платежа."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO payments (user_id, username, full_name, amount, currency, comment, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
    """, (user_id, username, full_name, amount, currency, comment, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    payment_id = cur.lastrowid
    conn.commit()
    conn.close()
    return payment_id


def confirm_payment(payment_id: int) -> bool:
    """Подтвердить платёж. Возвращает True если успешно."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE payments
        SET status = 'confirmed', confirmed_at = ?
        WHERE id = ? AND status = 'pending'
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), payment_id))
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def reject_payment(payment_id: int) -> bool:
    """Отклонить платёж. Возвращает True если успешно."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE payments
        SET status = 'rejected'
        WHERE id = ? AND status = 'pending'
    """, (payment_id,))
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_payment(payment_id: int) -> dict | None:
    """Получить платёж по ID."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_payments_report(since: str = None, until: str = None) -> list[dict]:
    """Получить все подтверждённые платежи за период."""
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
    """Итог по каждому сотруднику."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    query = """
        SELECT full_name, currency,
               SUM(amount) as total,
               COUNT(*) as count
        FROM payments
        WHERE status = 'confirmed'
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
