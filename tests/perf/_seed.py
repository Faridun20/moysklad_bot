"""Массовое наполнение БД для perf-тестов — напрямую в таблицы.

Сервисные функции здесь не годятся: они делают по 3–5 запросов на строку, и
5 000 товаров через них — минута на один тест. Прямые INSERT'ы — секунды.
Это единственное место, где так можно: остаток тут не «двигается», а
задаётся как исходное состояние.
"""

from __future__ import annotations

from tests.liveserver import run_async


def seed_products(db, n: int, *, warehouse_id: int | None = None, qty: float = 10) -> list[int]:
    """n товаров с остатком qty на складе. Возвращает product_id."""
    from services import warehouse

    wid = warehouse_id or run_async(warehouse.default_warehouse_id())
    now = db.now_str()
    ids: list[int] = []
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COALESCE(MAX(id), 0) AS m FROM products"))
        offset = int(cur.fetchone()["m"])  # sku UNIQUE: повторный сид не должен упираться
        for k in range(n):
            i = offset + k
            cur.execute(
                db.q("INSERT INTO products (name, category, sku, unit, created_at) VALUES (?, ?, ?, ?, ?)"),
                (f"Товар {i:05d}", f"Категория {i % 20}", f"SKU-{i:05d}", "шт", now),
            )
            pid = cur.lastrowid
            ids.append(pid)
            cur.execute(
                db.q("INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)"),
                (pid, wid, qty),
            )
        conn.commit()
    return ids


def seed_orders(db, n: int, *, owner: int, product_id: int, agent_id: str = "1",
                status: str = "approved", payment_type: str = "credit",
                items_per_order: int = 3, price: float = 10.0,
                due_date: str | None = "2030-01-01", with_request: bool = False) -> list[int]:
    """n заказов владельца `owner` по `items_per_order` позиций каждый."""
    now = db.now_str()
    ids: list[int] = []
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for _ in range(n):
            cur.execute(
                db.q(
                    "INSERT INTO orders (user_id, full_name, status, comment, agent_id, "
                    "agent_name, payment_type, due_date, currency, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (owner, "Manager", status, "", agent_id, "ООО Ромашка",
                 payment_type, due_date, "USD", now, now),
            )
            oid = cur.lastrowid
            ids.append(oid)
            for _ in range(items_per_order):
                cur.execute(
                    db.q(
                        "INSERT INTO order_items (order_id, product_name, product_href, quantity, "
                        "unit, price_cents) VALUES (?, ?, ?, ?, ?, ?)"
                    ),
                    (oid, "Кабель ВВГ 3x2.5", "", 1, "м", int(round(price * 100))),
                )
                item_id = cur.lastrowid
                cur.execute(
                    db.q("INSERT INTO order_item_products (item_id, order_id, product_id, created_at) "
                         "VALUES (?, ?, ?, ?)"),
                    (item_id, oid, product_id, now),
                )
            if with_request:
                cur.execute(
                    db.q(
                        "INSERT INTO shipment_requests (order_id, user_id, full_name, status, comment, created_at) "
                        "VALUES (?, ?, ?, 'pending', '', ?)"
                    ),
                    (oid, owner, "Manager", now),
                )
        conn.commit()
    return ids


def seed_payments(db, order_ids: list[int], *, amount_cents: int = 500, status: str = "pending",
                  user_id: int = 200) -> None:
    now = db.now_str()
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for oid in order_ids:
            cur.execute(
                db.q(
                    "INSERT INTO payments (user_id, username, full_name, amount_cents, currency, comment, "
                    "status, order_id, created_at) VALUES (?, ?, ?, ?, 'USD', '', ?, ?, ?)"
                ),
                (user_id, "u", "Manager", amount_cents, status, oid, now),
            )
        conn.commit()


def seed_counterparties(db, n: int) -> list[int]:
    """n новых контрагентов; возвращает их id."""
    now = db.now_str()
    ids: list[int] = []
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COALESCE(MAX(id), 0) AS m FROM counterparties"))
        start = int(cur.fetchone()["m"])
        for k in range(n):
            cur.execute(
                db.q("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"),
                (f"Клиент {start + k + 1}", "customer", "", now),
            )
            ids.append(cur.lastrowid)
        conn.commit()
    return ids
