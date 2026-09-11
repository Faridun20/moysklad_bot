"""
Локальный складской учёт: накладные и движение остатков.

Заменяет складскую часть МойСклад. Единственный источник правды об
остатках — таблица `stock`; никакой синхронизации наружу нет.

Модель:
  • накладная (`invoices`) сразу `confirmed` — промежуточного draft нет,
    остатки двигаются в той же транзакции, что и вставка строк;
  • `incoming` прибавляет остаток, `outgoing` вычитает;
  • отмена (`cancelled`) двигает остаток в обратную сторону.

Инварианты, которые держит этот модуль:
  1. Остаток никогда не уходит в минус. Нехватка хотя бы по одной позиции
     откатывает ВСЮ накладную — частичных списаний не бывает.
  2. Номер, шапка, строки и остатки пишутся одной транзакцией: упавшая
     на полпути накладная не оставляет ни дырки в нумерации, ни
     сдвинутого остатка.
  3. Параллельные накладные по одному товару сериализуются `FOR UPDATE`
     на строках `stock` — без него два одновременных списания читают
     один и тот же остаток и оба проходят проверку.

Деньги — копейки (BIGINT), как везде в проекте: `services.money`.
Умножение цены на дробное количество — только через `money.mul_qty`
(Decimal + ROUND_HALF_UP), никогда float.
"""

from __future__ import annotations

import logging
from datetime import datetime

from services import adb_core, money
from services import database as _db

# Через модуль, а не `from ... import USE_POSTGRES`: importlib.reload(db)
# в тестовой фикстуре мутирует объект модуля на месте, и обращение
# `_db.USE_POSTGRES` подхватывает новое значение, а скопированное при
# импорте имя осталось бы навсегда прежним.

logger = logging.getLogger(__name__)

INVOICE_TYPES = ("incoming", "outgoing")

# Префикс номера по типу накладной: IN-2026-0001 / OUT-2026-0001.
_NUMBER_PREFIX = {"incoming": "IN", "outgoing": "OUT"}

# Потолок позиций в одной накладной. Не защита от злоупотребления, а
# предохранитель: каждая позиция — это строка под FOR UPDATE, и накладная
# на тысячи позиций держит блокировки на заметное время, подвешивая
# параллельные отгрузки тех же товаров.
MAX_POSITIONS = 500


class InvoiceError(Exception):
    """Накладная не может быть проведена. Транзакция откатывается целиком.

    `code` — машиночитаемая причина для WebApp (`insufficient_stock`,
    `unknown_product`, …), `message` — текст для менеджера.
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _normalize_items(items: list[dict], invoice_type: str) -> list[dict]:
    """Провалидировать позиции и схлопнуть повторы одного товара.

    Схлопывание обязательно: без него накладная `[товар A ×6, товар A ×6]`
    при остатке 10 проходит обе построчные проверки (каждая видит 10 ≥ 6)
    и уводит остаток в −2. Складываем количества до проверки.
    """
    if not items:
        raise InvoiceError("empty_invoice", "В накладной нет позиций")
    if len(items) > MAX_POSITIONS:
        raise InvoiceError(
            "too_many_positions",
            f"В накладной больше {MAX_POSITIONS} позиций — разбейте на несколько",
        )

    merged: dict[int, dict] = {}
    for raw in items:
        try:
            product_id = int(raw.get("product_id") or 0)
        except (TypeError, ValueError):
            raise InvoiceError("bad_product_id", f"Некорректный товар: {raw.get('product_id')!r}")
        if product_id <= 0:
            raise InvoiceError("bad_product_id", "В позиции не указан товар")

        try:
            quantity = float(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            raise InvoiceError("bad_quantity", f"Некорректное количество: {raw.get('quantity')!r}")
        if quantity <= 0:
            raise InvoiceError(
                "bad_quantity", f"Количество должно быть больше нуля (товар #{product_id})"
            )

        price_cents = raw.get("price_cents")
        if price_cents is None:
            # Цена обязательна для расхода: из неё считается сумма накладной,
            # которая уходит клиенту в PDF. Для прихода допускаем пустую —
            # закупочную цену не всегда вводят.
            if invoice_type == "outgoing":
                raise InvoiceError(
                    "price_required", f"Для расхода нужна цена (товар #{product_id})"
                )
        else:
            try:
                price_cents = int(price_cents)
            except (TypeError, ValueError):
                raise InvoiceError("bad_price", f"Некорректная цена: {raw.get('price_cents')!r}")
            if price_cents < 0:
                raise InvoiceError("bad_price", f"Цена не может быть отрицательной (#{product_id})")

        if product_id in merged:
            prev = merged[product_id]
            prev["quantity"] += quantity
            # Повтор товара с РАЗНОЙ ценой схлопнуть нельзя: сумма накладной
            # станет неоднозначной. Это ошибка ввода, а не валидный кейс.
            if prev["price_cents"] != price_cents:
                raise InvoiceError(
                    "duplicate_price_conflict",
                    f"Товар #{product_id} указан дважды с разными ценами",
                )
        else:
            merged[product_id] = {
                "product_id": product_id,
                "quantity": quantity,
                "price_cents": price_cents,
            }

    # Сортировка по product_id — единый порядок захвата блокировок. Без него
    # две накладные с позициями [A,B] и [B,A] берут строки stock в обратном
    # порядке и получают взаимный deadlock на Postgres.
    return [merged[k] for k in sorted(merged)]


async def _next_invoice_number(txn, invoice_type: str, year: int) -> str:
    """Следующий номер вида `IN-2026-0001`, атомарно в текущей транзакции.

    UPSERT инкрементит счётчик под блокировкой строки — два параллельных
    создателя получают разные номера, второй ждёт первого.
    """
    await txn.execute(
        "INSERT INTO invoice_counters (type, year, last_number) VALUES ($1, $2, 1) "
        "ON CONFLICT (type, year) DO UPDATE SET last_number = invoice_counters.last_number + 1",
        invoice_type,
        year,
    )
    # RETURNING не используем: на SQLite он появился только в 3.35, а версия
    # там системная. Отдельный SELECT безопасен — мы внутри той же транзакции,
    # строка счётчика уже заблокирована нашим UPSERT'ом.
    last = await txn.fetchval(
        "SELECT last_number FROM invoice_counters WHERE type = $1 AND year = $2",
        invoice_type,
        year,
    )
    return f"{_NUMBER_PREFIX[invoice_type]}-{year}-{int(last):04d}"


async def _lock_stock(txn, product_ids: list[int], warehouse_id: int) -> dict[int, float]:
    """Заблокировать и прочитать остатки по списку товаров. {product_id: qty}.

    Товары, которых ещё нет в `stock`, в результат не попадают — вызывающий
    трактует их как остаток 0 (для прихода это норма, для расхода — нехватка).
    """
    if not product_ids:
        return {}
    placeholders = ", ".join(f"${i + 2}" for i in range(len(product_ids)))
    sql = (
        f"SELECT product_id, quantity FROM stock "
        f"WHERE warehouse_id = $1 AND product_id IN ({placeholders})"
    )
    if _db.USE_POSTGRES:
        # SQLite не знает FOR UPDATE, но там пишущая транзакция и так одна.
        sql += " ORDER BY product_id FOR UPDATE"
    rows = await txn.fetch(sql, warehouse_id, *product_ids)
    return {int(r["product_id"]): float(r["quantity"] or 0) for r in rows}


async def _apply_stock_delta(txn, product_id: int, warehouse_id: int, delta: float) -> None:
    """Сдвинуть остаток на delta. UPSERT, а не UPDATE.

    Обычный UPDATE на товаре, которого ещё нет в `stock`, молча затрагивает
    0 строк — приход нового товара терялся бы без единой ошибки.
    """
    await txn.execute(
        "INSERT INTO stock (product_id, warehouse_id, quantity) VALUES ($1, $2, $3) "
        "ON CONFLICT (product_id, warehouse_id) "
        "DO UPDATE SET quantity = stock.quantity + EXCLUDED.quantity",
        product_id,
        warehouse_id,
        delta,
    )


async def create_invoice(
    *,
    invoice_type: str,
    warehouse_id: int,
    items: list[dict],
    counterparty_id: int | None = None,
    currency: str = "USD",
    invoice_date: str | None = None,
    comment: str | None = None,
    created_by: int | None = None,
) -> dict:
    """Провести накладную. Номер, шапка, строки и остатки — одной транзакцией.

    items: [{"product_id": int, "quantity": float, "price_cents": int|None}]

    Возвращает {"ok": True, "invoice_id", "invoice_number", "total_amount_cents"}
    либо {"ok": False, "code", "reason"} — во втором случае в БД не изменилось
    ничего, включая счётчик номеров.
    """
    if invoice_type not in INVOICE_TYPES:
        return {"ok": False, "code": "bad_type", "reason": f"Неизвестный тип: {invoice_type}"}

    try:
        positions = _normalize_items(items, invoice_type)
    except InvoiceError as e:
        return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}

    date_str = invoice_date or _today()
    created = _db.now_str()
    sign = 1.0 if invoice_type == "incoming" else -1.0
    product_ids = [p["product_id"] for p in positions]

    try:
        async with adb_core.transaction() as txn:
            # 1) Товары существуют. Проверяем ДО блокировок: дешевле и
            #    сообщение об опечатке в id понятнее, чем «нехватка остатка».
            known = await txn.fetch(
                "SELECT id FROM products WHERE id IN ("
                + ", ".join(f"${i + 1}" for i in range(len(product_ids)))
                + ")",
                *product_ids,
            )
            known_ids = {int(r["id"]) for r in known}
            missing = [pid for pid in product_ids if pid not in known_ids]
            if missing:
                raise InvoiceError(
                    "unknown_product",
                    f"Товары не найдены: {', '.join(map(str, missing))}",
                    {"product_ids": missing},
                )

            wh = await txn.fetchval("SELECT id FROM warehouses WHERE id = $1", warehouse_id)
            if wh is None:
                raise InvoiceError("unknown_warehouse", f"Склад #{warehouse_id} не найден")

            if counterparty_id is not None:
                cp = await txn.fetchval(
                    "SELECT id FROM counterparties WHERE id = $1", counterparty_id
                )
                if cp is None:
                    raise InvoiceError(
                        "unknown_counterparty", f"Контрагент #{counterparty_id} не найден"
                    )

            # 2) Блокируем остатки и проверяем достаточность — до записи.
            current = await _lock_stock(txn, product_ids, warehouse_id)
            if invoice_type == "outgoing":
                short = [
                    {
                        "product_id": p["product_id"],
                        "need": p["quantity"],
                        "have": current.get(p["product_id"], 0.0),
                    }
                    for p in positions
                    if current.get(p["product_id"], 0.0) < p["quantity"]
                ]
                if short:
                    names = ", ".join(
                        f"#{s['product_id']} (нужно {s['need']:g}, есть {s['have']:g})"
                        for s in short
                    )
                    raise InvoiceError(
                        "insufficient_stock", f"Не хватает остатка: {names}", {"positions": short}
                    )

            # 3) Номер и шапка.
            year = int(date_str[:4])
            number = await _next_invoice_number(txn, invoice_type, year)
            total_cents = sum(
                money.mul_qty(p["price_cents"] or 0, p["quantity"]) for p in positions
            )

            await txn.execute(
                "INSERT INTO invoices (type, counterparty_id, warehouse_id, invoice_number, "
                "invoice_date, status, currency, total_amount_cents, comment, created_by, "
                "created_at) VALUES ($1, $2, $3, $4, $5, 'confirmed', $6, $7, $8, $9, $10)",
                invoice_type,
                counterparty_id,
                warehouse_id,
                number,
                date_str,
                currency,
                total_cents,
                comment,
                created_by,
                created,
            )
            invoice_id = await txn.fetchval(
                "SELECT id FROM invoices WHERE invoice_number = $1", number
            )

            # 4) Строки и движение остатков.
            for p in positions:
                await txn.execute(
                    "INSERT INTO invoice_items (invoice_id, product_id, quantity, price_cents) "
                    "VALUES ($1, $2, $3, $4)",
                    invoice_id,
                    p["product_id"],
                    p["quantity"],
                    p["price_cents"],
                )
                await _apply_stock_delta(
                    txn, p["product_id"], warehouse_id, sign * p["quantity"]
                )
    except InvoiceError as e:
        logger.info("Накладная не проведена (%s): %s", e.code, e.message)
        return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}

    logger.info(
        "Накладная %s проведена: id=%s, позиций=%d, сумма=%d коп.",
        number,
        invoice_id,
        len(positions),
        total_cents,
    )
    return {
        "ok": True,
        "invoice_id": int(invoice_id),
        "invoice_number": number,
        "total_amount_cents": int(total_cents),
        "positions": len(positions),
    }


async def cancel_invoice(invoice_id: int, cancelled_by: int | None = None) -> dict:
    """Отменить накладную: движение остатков в обратную сторону.

    Отмена прихода вычитает то, что было добавлено, и может упереться в
    нехватку — товар уже успели отгрузить. В этом случае отмена блокируется
    целиком: «откатить частично» означало бы разойтись с историей движений.
    """
    try:
        async with adb_core.transaction() as txn:
            if _db.USE_POSTGRES:
                inv = await txn.fetchrow(
                    "SELECT id, type, status, warehouse_id FROM invoices WHERE id = $1 FOR UPDATE",
                    invoice_id,
                )
            else:
                inv = await txn.fetchrow(
                    "SELECT id, type, status, warehouse_id FROM invoices WHERE id = $1",
                    invoice_id,
                )
            if inv is None:
                raise InvoiceError("not_found", f"Накладная #{invoice_id} не найдена")
            if inv["status"] == "cancelled":
                # Идемпотентно: повторная отмена не двигает остаток второй раз.
                raise InvoiceError("already_cancelled", "Накладная уже отменена")

            warehouse_id = int(inv["warehouse_id"])
            rows = await txn.fetch(
                "SELECT product_id, quantity FROM invoice_items WHERE invoice_id = $1 "
                "ORDER BY product_id",
                invoice_id,
            )
            if not rows:
                raise InvoiceError("empty_invoice", "В накладной нет позиций — нечего откатывать")

            # Откат меняет знак исходного движения.
            sign = -1.0 if inv["type"] == "incoming" else 1.0
            product_ids = [int(r["product_id"]) for r in rows]
            current = await _lock_stock(txn, product_ids, warehouse_id)

            if sign < 0:
                short = [
                    {
                        "product_id": int(r["product_id"]),
                        "need": float(r["quantity"]),
                        "have": current.get(int(r["product_id"]), 0.0),
                    }
                    for r in rows
                    if current.get(int(r["product_id"]), 0.0) < float(r["quantity"])
                ]
                if short:
                    names = ", ".join(
                        f"#{s['product_id']} (нужно вернуть {s['need']:g}, есть {s['have']:g})"
                        for s in short
                    )
                    raise InvoiceError(
                        "insufficient_stock",
                        f"Отмена увела бы остаток в минус: {names}. "
                        f"Товар уже отгружен — сначала отмените расходные накладные.",
                        {"positions": short},
                    )

            for r in rows:
                await _apply_stock_delta(
                    txn, int(r["product_id"]), warehouse_id, sign * float(r["quantity"])
                )

            await txn.execute(
                "UPDATE invoices SET status = 'cancelled', cancelled_by = $1, cancelled_at = $2 "
                "WHERE id = $3",
                cancelled_by,
                _db.now_str(),
                invoice_id,
            )
    except InvoiceError as e:
        logger.info("Отмена накладной #%s отклонена (%s): %s", invoice_id, e.code, e.message)
        return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}

    logger.info("Накладная #%s отменена, остатки откачены", invoice_id)
    return {"ok": True, "invoice_id": invoice_id}


# ─── Чтения ───────────────────────────────────────────────────────────────────


async def get_stock(warehouse_id: int | None = None, only_positive: bool = False) -> list[dict]:
    """Текущие остатки с именами товаров."""
    sql = (
        "SELECT p.id AS product_id, p.name, p.category, p.sku, p.unit, "
        "       COALESCE(s.quantity, 0) AS quantity, s.warehouse_id "
        "FROM products p LEFT JOIN stock s ON s.product_id = p.id"
    )
    args: list = []
    where = []
    if warehouse_id is not None:
        args.append(warehouse_id)
        where.append(f"s.warehouse_id = ${len(args)}")
    if only_positive:
        where.append("COALESCE(s.quantity, 0) > 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.name"
    return await adb_core.fetch(sql, *args)


async def get_invoice(invoice_id: int) -> dict | None:
    """Накладная с позициями и именами товаров."""
    inv = await adb_core.fetchrow(
        "SELECT i.*, c.name AS counterparty_name, c.telegram_id AS counterparty_telegram_id, "
        "       w.name AS warehouse_name "
        "FROM invoices i "
        "LEFT JOIN counterparties c ON c.id = i.counterparty_id "
        "LEFT JOIN warehouses w ON w.id = i.warehouse_id "
        "WHERE i.id = $1",
        invoice_id,
    )
    if inv is None:
        return None
    inv["items"] = await adb_core.fetch(
        "SELECT ii.id, ii.product_id, ii.quantity, ii.price_cents, "
        "       p.name AS product_name, p.unit, p.sku "
        "FROM invoice_items ii JOIN products p ON p.id = ii.product_id "
        "WHERE ii.invoice_id = $1 ORDER BY ii.id",
        invoice_id,
    )
    return inv


async def list_invoices(
    invoice_type: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Список накладных, новые сверху."""
    sql = (
        "SELECT i.id, i.type, i.invoice_number, i.invoice_date, i.status, i.currency, "
        "       i.total_amount_cents, i.telegram_sent, i.created_at, "
        "       c.name AS counterparty_name "
        "FROM invoices i LEFT JOIN counterparties c ON c.id = i.counterparty_id"
    )
    args: list = []
    if invoice_type:
        args.append(invoice_type)
        sql += f" WHERE i.type = ${len(args)}"
    args.extend([limit, offset])
    sql += f" ORDER BY i.id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
    return await adb_core.fetch(sql, *args)


async def mark_telegram_sent(invoice_id: int) -> bool:
    """Отметить, что PDF накладной ушёл клиенту."""
    n = await adb_core.execute(
        "UPDATE invoices SET telegram_sent = 1, telegram_sent_at = $1 WHERE id = $2",
        _db.now_str(),
        invoice_id,
    )
    return n > 0
