"""
Тесты складских алертов (PR B): low-stock + dead-stock.

  * get_low_stock — доступный остаток (остаток − резерв) ≤ порога, остаток>0
  * build_low_stock_block / build_dead_stock_block — None/счёт/truncate
  * diff_dead_stock — чистая diff-логика (продано → не dead)

Резерв локально — это одобренные, но не отгруженные заказы: именно они держат
товар, как раньше держал customerorder в МойСклад.
"""

import asyncio

from tasks.run_ops_monitor import (
    build_dead_stock_block,
    build_low_stock_block,
    diff_dead_stock,
)


def _run(coro):
    return asyncio.run(coro)


def _seed_stock(db, rows):
    """rows: list of (name, stock, reserve). Резерв делаем одобренным заказом."""
    from services import container_receipt, warehouse

    wid = _run(warehouse.default_warehouse_id())
    db.set_role(1, "m", "Mgr", "manager")
    for name, stock, reserve in rows:
        pid = _run(container_receipt.create_product(name))["product_id"]
        if stock:
            _run(warehouse.create_invoice(
                invoice_type="incoming", warehouse_id=wid,
                items=[{"product_id": pid, "quantity": stock, "price_cents": None}],
            ))
        if reserve:
            oid = db.create_order(1, "Mgr", "")
            db.add_order_item(oid, name, "", reserve, "шт", 1.0, product_id=pid)
            db.update_order_status(oid, "approved")


# ─── get_low_stock ───────────────────────────────────────────────────────────


def test_get_low_stock_threshold(isolated_db):
    from services import warehouse

    db = isolated_db
    _seed_stock(
        db,
        [
            ("Мало", 3, 0),  # доступно 3 ≤ 5 → low
            ("Норма", 50, 0),  # доступно 50 → not low
            ("Резерв", 10, 8),  # доступно 2 ≤ 5 → low
            ("Ноль", 0, 0),  # остатка нет → исключается (нечего продавать)
        ],
    )
    low = _run(warehouse.get_low_stock(threshold=5))
    names = {r["name"] for r in low}
    assert names == {"Мало", "Резерв"}
    # Сортировка: худший доступный сверху (Резерв avail=2 < Мало avail=3)
    assert low[0]["name"] == "Резерв"


def test_get_low_stock_excludes_zero_stock(isolated_db):
    from services import warehouse

    db = isolated_db
    _seed_stock(db, [("Распродан", 0, 0)])
    assert _run(warehouse.get_low_stock(threshold=5)) == []


# ─── build-блоки ─────────────────────────────────────────────────────────────


def test_build_low_stock_block_none_when_empty():
    assert build_low_stock_block([], 5) is None


def test_build_low_stock_block_counts_and_truncates():
    rows = [{"name": f"Товар{i}", "available": 2, "unit": "шт"} for i in range(20)]
    block = build_low_stock_block(rows, 5)
    assert "20" in block
    assert "и ещё 5" in block  # 15 показано, 5 свёрнуто


def test_build_low_stock_block_shows_available():
    rows = [{"name": "Гвозди", "available": 3, "unit": "кг"}]
    block = build_low_stock_block(rows, 5)
    assert "Гвозди" in block
    assert "3" in block  # available = 10 - 7
    assert "кг" in block


def test_build_dead_stock_block_none_when_empty():
    assert build_dead_stock_block([], 90) is None


def test_build_dead_stock_block_counts():
    rows = [{"name": f"Залежь{i}", "stock": 100, "unit": "шт"} for i in range(3)]
    block = build_dead_stock_block(rows, 90)
    assert "3" in block
    assert "90" in block
    assert "Залежь0" in block


# ─── diff_dead_stock ─────────────────────────────────────────────────────────


def test_diff_dead_stock_excludes_sold():
    in_stock = [
        {"name": "Продаётся", "stock": 50},
        {"name": "Залежь", "stock": 30},
    ]
    sold = {"Продаётся"}
    dead = diff_dead_stock(in_stock, sold)
    assert len(dead) == 1
    assert dead[0]["name"] == "Залежь"


def test_diff_dead_stock_case_insensitive_match():
    in_stock = [{"name": "ТоварА", "stock": 10}]
    # Продано записано в другом регистре — всё равно не dead
    dead = diff_dead_stock(in_stock, {"товара"})
    assert dead == []


def test_diff_dead_stock_skips_zero_stock():
    in_stock = [{"name": "Пусто", "stock": 0}]
    dead = diff_dead_stock(in_stock, set())
    assert dead == []  # нулевой остаток — не «мёртвый», его просто нет
