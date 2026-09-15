"""
Массовый импорт каталога из Excel/CSV (продуктовый аудит, B5).

Экран «Склад → Каталог» открыт менеджеру (как и приёмка контейнера), поэтому
импорт — тоже admin/boss/manager: создание карточки товара и приход остатка
для менеджера не новая возможность, а тот же путь, что у приёмки.

Три решения, определяющие модуль:

* **Дубликаты — по нормализованному имени, как в приёмке контейнера**
  (`services.container_receipt.normalize_name`/`_products_by_name`): регистр,
  лишние пробелы и «ё» не должны заводить вторую карточку одного товара.
  Совпадение — и с уже существующим каталогом, и ВНУТРИ самого файла (два
  ряда с одинаковым названием сливаются в один товар, остаток суммируется).
* **Остаток заводится ТОЛЬКО через `services.warehouse.create_invoice_in`**
  (обычная приходная накладная с комментарием «импорт из Excel») — прямой
  UPDATE `stock` в обход накладной означал бы движение без следа в журнале.
* **Весь батч — одна транзакция.** Опечатка в середине файла не должна
  оставить первую половину товаров заведённой, а остаток — начатым наполовину:
  `commit_import` validates ДО открытия транзакции (весь файл целиком) и пишет
  ВСЁ одним `adb_core.transaction()`; откат накладной откатывает и вставленные
  карточки товаров той же транзакцией.

Себестоимость не спрашиваем (как приёмка контейнера) — только цена продажи,
и то опционально: v1 без учётных последствий.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from typing import Any

from services import adb_core, money, warehouse
from services.container_receipt import normalize_name
from services.database import USE_POSTGRES, now_str, validate_amount_in_currency

logger = logging.getLogger(__name__)

# Заголовок шаблона — порядок колонок канон, содержимое заголовка не разбираем
# (первая непустая строка файла всегда пропускается как шапка).
TEMPLATE_HEADERS = ["Название", "Единица", "Категория", "Цена", "Остаток"]

# Потолок строк одного импорта — файл на порядки больше похож на ошибку
# экспорта чужого каталога, чем на реальную поставку.
MAX_ROWS = 5000

# Потолок размера файла (проверяется на границе, в webapp/server.py, здесь —
# для справки и для тестов, которые хотят собрать заведомо большой файл).
MAX_FILE_BYTES = 5 * 1024 * 1024


class CatalogImportError(Exception):
    """Файл целиком не читается (не тот формат, битый архив и т.п.)."""


@dataclass
class ImportRow:
    line: int
    name: str = ""
    unit: str = "шт"
    category: str | None = None
    price: float | None = None
    stock: float = 0.0
    error: str | None = None


def build_template_xlsx() -> bytes:
    """Шаблон .xlsx с примером строки — чтобы формат файла не пришлось объяснять."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Каталог"
    ws.append(TEMPLATE_HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.append(["Кабель ПВ 0.6", "м", "Кабель", 12.5, 100])
    ws.append(["Розетка 220В", "шт", "Электрика", 3.2, 500])
    widths = (28, 10, 18, 12, 12)
    for col, width in zip("ABCDE", widths, strict=True):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse_xlsx(content: bytes) -> list[list[Any]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as e:
        raise CatalogImportError(f"Не удалось прочитать .xlsx: {e}") from e
    ws = wb.worksheets[0]
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _parse_csv(content: bytes) -> list[list[Any]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CatalogImportError(f"Файл не в UTF-8: {e}") from e
    first_line = text.splitlines()[0] if text.splitlines() else ""
    # Excel в RU-локали сохраняет CSV с «;» — выбираем разделитель по первой
    # строке, иначе «Название;Единица;…» превращается в одну колонку.
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return [list(row) for row in reader]


def parse_rows(filename: str, content: bytes) -> list[list[Any]]:
    """Сырые строки файла (без заголовка). Бросает `CatalogImportError`, если
    формат не .xlsx/.csv или файл битый."""
    name = (filename or "").strip().lower()
    if name.endswith(".xlsx"):
        raw = _parse_xlsx(content)
    elif name.endswith(".csv"):
        raw = _parse_csv(content)
    else:
        raise CatalogImportError("Поддерживаются только файлы .xlsx и .csv")
    rows = [r for r in raw if any(str(c or "").strip() for c in r)]
    if rows:
        rows = rows[1:]  # первая непустая строка — заголовок
    return rows


def _parse_qty(raw: Any) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip().replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def validate_rows(raw_rows: list[list[Any]]) -> list[ImportRow]:
    """Построчная валидация без обращений к БД. Одна и та же функция кормит и
    превью, и коммит — расхождения между «что показали» и «что записали»
    здесь появиться неоткуда."""
    out: list[ImportRow] = []
    for i, r in enumerate(raw_rows[:MAX_ROWS], start=2):  # строка 1 — заголовок
        cells = list(r) + [None] * (5 - len(r))
        name_raw, unit_raw, category_raw, price_raw, stock_raw = cells[:5]
        name = " ".join(str(name_raw or "").split())[:255]
        row = ImportRow(line=i, name=name)
        if not name:
            row.error = "Название обязательно"
            out.append(row)
            continue

        row.unit = (str(unit_raw or "").strip() or "шт")[:16]
        category = str(category_raw or "").strip()
        row.category = category[:100] or None

        if price_raw not in (None, ""):
            parsed_price = _parse_qty(price_raw)
            if parsed_price is None:
                row.error = f"Цена «{price_raw}» — не число"
                out.append(row)
                continue
            ok, err = validate_amount_in_currency(parsed_price, None)
            if not ok:
                row.error = f"Цена: {err}"
                out.append(row)
                continue
            row.price = parsed_price

        if stock_raw not in (None, ""):
            parsed_stock = _parse_qty(stock_raw)
            if parsed_stock is None or parsed_stock < 0:
                row.error = f"Остаток «{stock_raw}» — должен быть неотрицательным числом"
                out.append(row)
                continue
            row.stock = parsed_stock

        out.append(row)
    if len(raw_rows) > MAX_ROWS:
        # Не роняем импорт целиком — отдаём первые MAX_ROWS строк с явной
        # пометкой, что файл обрезан: молча потерянные строки хуже.
        out.append(
            ImportRow(
                line=MAX_ROWS + 2,
                error=f"Файл содержит больше {MAX_ROWS} строк — обработаны только первые",
            )
        )
    return out


async def preview_import(raw_rows: list[list[Any]]) -> dict:
    """Разложить строки на «новый товар» / «сольётся с …» / «ошибка» — для
    экрана подтверждения ДО записи."""
    rows = validate_rows(raw_rows)
    valid = [r for r in rows if not r.error]
    existing_by_norm = await _products_by_name([r.name for r in valid]) if valid else {}

    seen_norm: dict[str, int] = {}
    out_rows: list[dict] = []
    new_count = merge_count = 0
    for r in rows:
        item = {
            "line": r.line,
            "name": r.name,
            "unit": r.unit,
            "category": r.category,
            "price": r.price,
            "stock": r.stock,
            "error": r.error,
        }
        if r.error:
            item["status"] = "error"
        else:
            norm = normalize_name(r.name)
            if norm in seen_norm:
                item["status"] = "merge"
                item["merge_with"] = f"строка {seen_norm[norm]}"
                merge_count += 1
            elif norm in existing_by_norm:
                match = existing_by_norm[norm][0]
                item["status"] = "merge"
                item["merge_with"] = f"№{match['id']} «{match['name']}»"
                merge_count += 1
                seen_norm[norm] = r.line
            else:
                item["status"] = "new"
                new_count += 1
                seen_norm[norm] = r.line
        out_rows.append(item)
    errors = sum(1 for r in rows if r.error)
    return {
        "rows": out_rows,
        "total": len(rows),
        "new": new_count,
        "merge": merge_count,
        "errors": errors,
    }


async def _products_by_name(names: list[str], conn: Any = None):
    from services.container_receipt import _products_by_name as _lookup

    return await _lookup(names, conn=conn)


async def _insert_product(txn, name: str, unit: str, category: str | None) -> int:
    insert = "INSERT INTO products (name, unit, category, created_at) VALUES ($1, $2, $3, $4)"
    if USE_POSTGRES:
        return int(await txn.fetchval(insert + " RETURNING id", name, unit, category, now_str()))
    await txn.execute(insert, name, unit, category, now_str())
    return int(await txn.fetchval("SELECT last_insert_rowid()"))


async def _insert_price(
    txn, product_id: int, name: str, price: float, currency: str, user_id: int
) -> None:
    cents = money.to_cents(price)
    await txn.execute(
        "INSERT INTO product_prices "
        "(ms_id, product_name, sale_price_cents, currency, updated_by, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        str(product_id),
        name,
        cents,
        currency,
        user_id,
        now_str(),
    )


async def commit_import(
    raw_rows: list[list[Any]],
    *,
    user_id: int,
    warehouse_id: int | None = None,
    currency: str | None = None,
) -> dict:
    """Провести импорт целиком. Всё-или-ничего: любая ошибка (валидация ИЛИ
    сбой накладной) не оставляет ни одной новой карточки товара.

    Возвращает {"ok": True, "rows", "created", "merged", "invoice"} либо
    {"ok": False, "error", "errors": [{"line", "message"}, …]}.
    """
    from config import BASE_CURRENCY

    rows = validate_rows(raw_rows)
    errors = [r for r in rows if r.error]
    if errors:
        return {
            "ok": False,
            "error": "В файле есть ошибки — исправьте и загрузите заново",
            "errors": [{"line": r.line, "message": r.error} for r in errors],
        }
    valid = [r for r in rows if r.name]
    if not valid:
        return {"ok": False, "error": "Файл пуст", "errors": []}

    cur_code = (currency or BASE_CURRENCY or "USD").upper()
    wh_id = warehouse_id if warehouse_id is not None else await warehouse.default_warehouse_id()

    try:
        async with adb_core.transaction() as txn:
            existing_by_norm = await _products_by_name([r.name for r in valid], conn=txn)
            resolved: dict[str, int] = {}
            created = merged = 0
            stock_items: list[dict] = []
            for r in valid:
                norm = normalize_name(r.name)
                if norm in resolved:
                    product_id = resolved[norm]
                    merged += 1
                else:
                    match = existing_by_norm.get(norm)
                    if match:
                        product_id = int(match[0]["id"])
                        merged += 1
                    else:
                        product_id = await _insert_product(txn, r.name, r.unit, r.category)
                        created += 1
                        if r.price is not None:
                            await _insert_price(
                                txn, product_id, r.name, r.price, cur_code, user_id
                            )
                    resolved[norm] = product_id
                if r.stock and r.stock > 0:
                    stock_items.append(
                        {"product_id": product_id, "quantity": r.stock, "price_cents": None}
                    )

            invoice = None
            if stock_items:
                invoice = await warehouse.create_invoice_in(
                    txn,
                    invoice_type="incoming",
                    warehouse_id=wh_id,
                    items=stock_items,
                    currency=cur_code,
                    comment="импорт из Excel",
                    created_by=user_id,
                )
    except warehouse.InvoiceError as e:
        logger.info("Импорт каталога не проведён (%s): %s", e.code, e.message)
        return {"ok": False, "error": e.message, "errors": []}

    logger.info(
        "Импорт каталога: строк=%d новых=%d слито=%d накладная=%s",
        len(valid),
        created,
        merged,
        invoice.get("invoice_id") if invoice else None,
    )
    return {
        "ok": True,
        "rows": len(valid),
        "created": created,
        "merged": merged,
        "invoice": invoice,
    }
