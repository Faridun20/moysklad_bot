"""
Тесты B5 — массовый импорт каталога из Excel/CSV (`services/catalog_import.py`).

Проверяем чистую логику (парсинг/валидация/коммит) на `isolated_db`: настоящая
БД (SQLite), без мока своего кода — граница с внешним миром здесь только
чтение файла (openpyxl/csv), она проверяется напрямую.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from services import catalog_import as ci


def _xlsx_bytes(rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(ci.TEMPLATE_HEADERS)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _csv_bytes(rows: list[list], header=None) -> bytes:
    lines = [",".join(str(c) for c in (header or ci.TEMPLATE_HEADERS))]
    for r in rows:
        lines.append(",".join(str(c) for c in r))
    return ("\n".join(lines)).encode("utf-8-sig")


# ─── parse_rows / validate_rows ──────────────────────────────────────────────


def test_parse_xlsx_skips_header():
    content = _xlsx_bytes([["Товар А", "шт", "Кат", 1.5, 10]])
    rows = ci.parse_rows("file.xlsx", content)
    assert len(rows) == 1
    assert rows[0][0] == "Товар А"


def test_parse_csv_semicolon_delimiter():
    text = "Название;Единица;Категория;Цена;Остаток\nТовар Б;шт;Кат;2,5;5\n"
    rows = ci.parse_rows("file.csv", text.encode("utf-8-sig"))
    assert rows == [["Товар Б", "шт", "Кат", "2,5", "5"]]


def test_unsupported_extension_raises():
    with pytest.raises(ci.CatalogImportError):
        ci.parse_rows("file.txt", b"whatever")


def test_broken_xlsx_raises_catalog_import_error():
    with pytest.raises(ci.CatalogImportError):
        ci.parse_rows("file.xlsx", b"not an xlsx file at all")


def test_validate_rows_missing_name_is_error():
    rows = ci.validate_rows([["", "шт", "", "", ""]])
    assert rows[0].error == "Название обязательно"


def test_validate_rows_bad_price_is_error():
    rows = ci.validate_rows([["Товар", "шт", "", "не число", "5"]])
    assert rows[0].error is not None
    assert "Цена" in rows[0].error


def test_validate_rows_negative_stock_is_error():
    rows = ci.validate_rows([["Товар", "шт", "", "1.5", "-5"]])
    assert rows[0].error is not None


def test_validate_rows_defaults_unit_and_stock():
    rows = ci.validate_rows([["Товар", "", "", "", ""]])
    assert not rows[0].error
    assert rows[0].unit == "шт"
    assert rows[0].stock == 0.0
    assert rows[0].price is None


# ─── preview_import / commit_import (БД) ─────────────────────────────────────


def test_preview_marks_new_and_merge(isolated_db):
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO products (name, unit, created_at) VALUES (?, ?, ?)"),
            ("Ёлочный кронштейн", "шт", db.now_str()),
        )
        conn.commit()

    raw = [
        ["елочный  кронштейн", "шт", "", "", "3"],  # сольётся (ё=е, пробелы, регистр)
        ["Совсем новый товар", "шт", "Кат", "1.5", "10"],
        ["", "шт", "", "", ""],  # ошибка — без имени
    ]

    async def scenario():
        return await ci.preview_import(raw)

    result = asyncio.run(scenario())
    statuses = [r["status"] for r in result["rows"]]
    assert statuses == ["merge", "new", "error"]
    assert result["new"] == 1
    assert result["merge"] == 1
    assert result["errors"] == 1


def test_commit_creates_product_price_and_stock(isolated_db):
    db = isolated_db

    async def scenario():
        raw = [["Новый товар", "м", "Кабель", "12.5", "100"]]
        return await ci.commit_import(raw, user_id=1)

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["created"] == 1
    assert result["merged"] == 0
    assert result["invoice"]["positions"] == 1

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT id, name, unit, category FROM products WHERE name = ?"), ("Новый товар",))
        product = dict(cur.fetchone())
        cur.execute(db.q("SELECT quantity FROM stock WHERE product_id = ?"), (product["id"],))
        stock_row = cur.fetchone()
        cur.execute(
            db.q("SELECT sale_price_cents, currency FROM product_prices WHERE ms_id = ?"),
            (str(product["id"]),),
        )
        price_row = dict(cur.fetchone())
    assert product["unit"] == "м"
    assert product["category"] == "Кабель"
    assert float(stock_row[0]) == 100.0
    assert price_row["sale_price_cents"] == 1250
    # Комментарий накладной — источник остатка виден в журнале.
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT comment FROM invoices WHERE id = ?"), (result["invoice"]["invoice_id"],))
        comment = cur.fetchone()[0]
    assert comment == "импорт из Excel"


def test_commit_merges_duplicate_by_normalized_name(isolated_db):
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO products (name, unit, created_at) VALUES (?, ?, ?)"),
            ("Кабель ПВ 0.6", "м", db.now_str()),
        )
        pid = cur.lastrowid
        cur.execute(
            db.q("INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)"),
            (pid, 1, 20.0),
        )
        conn.commit()

    async def scenario():
        raw = [["кабель  пв 0.6", "м", "", "", "30"]]  # ё/регистр/пробелы — тот же товар
        return await ci.commit_import(raw, user_id=1)

    result = asyncio.run(scenario())
    assert result["ok"] is True
    assert result["created"] == 0
    assert result["merged"] == 1

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM products WHERE lower(name) LIKE ?"), ("%кабель%",))
        count = cur.fetchone()[0]
        cur.execute(db.q("SELECT quantity FROM stock WHERE product_id = ?"), (pid,))
        qty = cur.fetchone()[0]
    assert count == 1  # не завели вторую карточку
    assert float(qty) == 50.0  # 20 было + 30 из импорта


def test_commit_malformed_row_blocks_whole_batch(isolated_db):
    db = isolated_db

    async def scenario():
        raw = [
            ["Хороший товар", "шт", "", "1", "5"],
            ["Плохая строка", "шт", "", "не цена", "5"],
        ]
        return await ci.commit_import(raw, user_id=1)

    result = asyncio.run(scenario())
    assert result["ok"] is False
    assert result["errors"]

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM products WHERE name = ?"), ("Хороший товар",))
        count = cur.fetchone()[0]
    assert count == 0  # ни одна строка не записалась — всё-или-ничего


def test_commit_all_or_nothing_on_invoice_failure(isolated_db):
    """Сбой ПОСЛЕ создания карточек (внутри той же транзакции) откатывает и их —
    иначе первый товар файла оставался бы заведённым, а второй — нет."""
    db = isolated_db

    async def scenario():
        raw = [
            ["Товар раз", "шт", "", "", "5"],
            ["Товар два", "шт", "", "", "5"],
        ]
        # Несуществующий склад — warehouse.create_invoice_in бросит InvoiceError
        # уже ПОСЛЕ вставки карточек товаров той же транзакцией.
        return await ci.commit_import(raw, user_id=1, warehouse_id=999999)

    result = asyncio.run(scenario())
    assert result["ok"] is False

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM products WHERE name IN (?, ?)"), ("Товар раз", "Товар два"))
        count = cur.fetchone()[0]
    assert count == 0


def test_commit_rejects_more_than_max_rows_without_writing(isolated_db):
    db = isolated_db
    raw = [[f"Товар {i}", "шт", "", "", "1"] for i in range(ci.MAX_ROWS + 5)]

    async def scenario():
        return await ci.commit_import(raw, user_id=1)

    result = asyncio.run(scenario())
    assert result["ok"] is False
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM products"))
        count = cur.fetchone()[0]
    assert count == 0
