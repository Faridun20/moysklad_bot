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

import asyncio
import logging
from datetime import datetime

from services import adb_core, money
from services import database as _db

# Через модуль, а не `from ... import USE_POSTGRES`: importlib.reload(db)
# в тестовой фикстуре мутирует объект модуля на месте, и обращение
# `_db.USE_POSTGRES` подхватывает новое значение, а скопированное при
# импорте имя осталось бы навсегда прежним.

logger = logging.getLogger(__name__)


def _base_currency() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


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



async def create_invoice_in(
    txn,
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
    """Провести накладную ВНУТРИ уже открытой транзакции.

    Отдельно от `create_invoice`, потому что есть операции, которые обязаны
    попасть в накладную и в свою собственную запись одним коммитом: приёмка
    контейнера сначала отменяет прежний приход, потом заводит новый, и
    оборваться между этими двумя движениями склад не имеет права.

    В отличие от публичной обёртки, при отказе БРОСАЕТ `InvoiceError` — иначе
    вызывающая транзакция получила бы «не ок» словарём и спокойно закоммитила
    всё, что успела записать до него.
    """
    if invoice_type not in INVOICE_TYPES:
        raise InvoiceError("bad_type", f"Неизвестный тип: {invoice_type}")

    positions = _normalize_items(items, invoice_type)
    date_str = invoice_date or _today()
    created = _db.now_str()
    sign = 1.0 if invoice_type == "incoming" else -1.0
    product_ids = [p["product_id"] for p in positions]

    # 1) Товары существуют. Проверяем ДО блокировок: дешевле и сообщение об
    #    опечатке в id понятнее, чем «нехватка остатка».
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
        cp = await txn.fetchval("SELECT id FROM counterparties WHERE id = $1", counterparty_id)
        if cp is None:
            raise InvoiceError("unknown_counterparty", f"Контрагент #{counterparty_id} не найден")

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
                f"#{s['product_id']} (нужно {s['need']:g}, есть {s['have']:g})" for s in short
            )
            raise InvoiceError(
                "insufficient_stock", f"Не хватает остатка: {names}", {"positions": short}
            )

    # 3) Номер и шапка.
    year = int(date_str[:4])
    number = await _next_invoice_number(txn, invoice_type, year)
    total_cents = sum(money.mul_qty(p["price_cents"] or 0, p["quantity"]) for p in positions)

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
    invoice_id = await txn.fetchval("SELECT id FROM invoices WHERE invoice_number = $1", number)

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
        await _apply_stock_delta(txn, p["product_id"], warehouse_id, sign * p["quantity"])

    # Себестоимость: приход заводит партии, расход фиксирует, из каких партий
    # ушёл товар. Той же транзакцией — полу-учёт хуже отказа. При выключенном
    # учёте (app_settings.accounting_enabled) — ничего. Стоит ПОСЛЕ движения
    # остатка: FIFO читает остаток «после» и знает, сколько было «до».
    from services import costing

    await costing.record_invoice_in(
        txn,
        invoice_id=int(invoice_id),
        invoice_type=invoice_type,
        invoice_date=date_str,
        currency=currency,
        positions=positions,
    )

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
    try:
        async with adb_core.transaction() as txn:
            return await create_invoice_in(
                txn,
                invoice_type=invoice_type,
                warehouse_id=warehouse_id,
                items=items,
                counterparty_id=counterparty_id,
                currency=currency,
                invoice_date=invoice_date,
                comment=comment,
                created_by=created_by,
            )
    except InvoiceError as e:
        logger.info("Накладная не проведена (%s): %s", e.code, e.message)
        return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}


async def cancel_invoice_in(txn, invoice_id: int, cancelled_by: int | None = None) -> dict:
    """Отменить накладную ВНУТРИ уже открытой транзакции. Бросает `InvoiceError`."""
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
        "SELECT product_id, quantity FROM invoice_items WHERE invoice_id = $1 ORDER BY product_id",
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
    logger.info("Накладная #%s отменена, остатки откачены", invoice_id)
    return {"ok": True, "invoice_id": invoice_id}


async def cancel_invoice(invoice_id: int, cancelled_by: int | None = None) -> dict:
    """Отменить накладную: движение остатков в обратную сторону.

    Отмена прихода вычитает то, что было добавлено, и может упереться в
    нехватку — товар уже успели отгрузить. В этом случае отмена блокируется
    целиком: «откатить частично» означало бы разойтись с историей движений.
    """
    try:
        async with adb_core.transaction() as txn:
            return await cancel_invoice_in(txn, invoice_id, cancelled_by)
    except InvoiceError as e:
        logger.info("Отмена накладной #%s отклонена (%s): %s", invoice_id, e.code, e.message)
        return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}



# ─── Чтения ───────────────────────────────────────────────────────────────────


async def default_warehouse_id() -> int:
    """Склад по умолчанию — тот, что засеял `seed_warehouses`.

    Берём минимальный id, а не константу 1: на проде склад могли завести
    руками раньше сидинга, и захардкоженная единица указывала бы в пустоту —
    накладная отвергалась бы «склад не найден» на ровном месте.
    """
    wid = await adb_core.fetchval("SELECT MIN(id) FROM warehouses")
    return int(wid) if wid is not None else 1



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


# ─── Каталог ──────────────────────────────────────────────────────────────────
#
# Заменяет читающую часть снапшота МойСклад (`snapshot.get_stock`,
# `search_products`, `get_categories`, `get_low_stock`). Источник — наши
# `products` + `stock`, промежуточного зеркала больше нет: зеркалить нечего.

# «Резерв» локально — это одобренные, но ещё не СПИСАННЫЕ заказы. В МойСклад
# им соответствовал customerorder, который держал товар; у нас заказ живёт в
# своей таблице, и доступный остаток обязан его учитывать — иначе один и тот же
# ящик пообещают двум клиентам.
#
# Списанное — не резерв. Одобрение сразу проводит расходную накладную
# (`order_shipment.invoice_id`), и остаток уже уменьшен; считать тот же заказ
# ещё и резервом значит вычесть его дважды («на складе 18, доступно 16», пока
# кладовщик не нажмёт «Отгрузить»). В резерве остаются одобренные заказы, по
# которым накладной нет: отгрузка не прошла (failed_at) или ещё не дошла.
_RESERVED_STATUSES = ("approved",)


async def _reserved_by_product() -> dict[int, float]:
    rows = await adb_core.fetch(
        "SELECT op.product_id AS product_id, SUM(oi.quantity) AS qty "
        "FROM order_item_products op "
        "JOIN order_items oi ON oi.id = op.item_id "
        "JOIN orders o ON o.id = op.order_id "
        "WHERE o.status = $1 "
        "AND NOT EXISTS (SELECT 1 FROM order_shipment s "
        "                WHERE s.order_id = o.id AND s.invoice_id IS NOT NULL) "
        "GROUP BY op.product_id",
        _RESERVED_STATUSES[0],
    )
    return {int(r["product_id"]): float(r["qty"] or 0) for r in rows}


async def search_products(query: str, limit: int = 20) -> list[dict]:
    """Поиск по номенклатуре. Кириллица — через `lower()` с обеих сторон."""
    text = (query or "").strip()
    if not text:
        return []
    return await adb_core.fetch(
        "SELECT id AS product_id, name, unit, category, sku FROM products "
        "WHERE lower(name) LIKE $1 ORDER BY name LIMIT $2",
        f"%{text.lower()}%",
        max(1, min(int(limit or 20), 100)),
    )


async def get_product(product_id: int | str | None) -> dict | None:
    """Карточка товара по id. Принимает и строку — id ездят через JSON."""
    if product_id is None or str(product_id).strip() == "":
        return None
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return None
    return await adb_core.fetchrow(
        "SELECT id AS product_id, name, unit, category, sku FROM products WHERE id = $1", pid
    )


async def get_categories() -> list[dict]:
    """Категории каталога.

    Категория у товара — ТЕКСТ, а не ссылка на справочник: в МойСклад это была
    папка номенклатуры, и переносить дерево ради фильтра в одну кнопку незачем.
    Поэтому id категории — само её название.
    """
    rows = await adb_core.fetch(
        "SELECT DISTINCT category FROM products "
        "WHERE category IS NOT NULL AND category <> '' ORDER BY category"
    )
    return [{"id": r["category"], "name": r["category"]} for r in rows]


async def get_catalog(category: str | None = None, only_positive: bool = False) -> list[dict]:
    """Каталог с остатком, резервом и доступным количеством.

    Нулевые остатки по умолчанию НЕ прячем: товар не должен исчезать из
    каталога после полной отгрузки — иначе менеджер теряет позицию из списка
    ровно в тот момент, когда её надо заказать снова.
    """
    sql = (
        "SELECT p.id AS product_id, p.name, p.unit, p.category, p.sku, "
        "       COALESCE(SUM(s.quantity), 0) AS quantity "
        "FROM products p LEFT JOIN stock s ON s.product_id = p.id"
    )
    args: list = []
    if category and category != "all":
        args.append(category)
        sql += f" WHERE p.category = ${len(args)}"
    sql += " GROUP BY p.id, p.name, p.unit, p.category, p.sku ORDER BY p.name"
    rows = await adb_core.fetch(sql, *args)

    reserved = await _reserved_by_product()
    out = []
    for r in rows:
        pid = int(r["product_id"])
        qty = float(r["quantity"] or 0)
        res = reserved.get(pid, 0.0)
        if only_positive and qty == 0:
            continue
        out.append(
            {
                "product_id": pid,
                "name": r["name"],
                "unit": r["unit"] or "шт",
                "category": r["category"] or "",
                "sku": r["sku"] or "",
                "quantity": qty,
                "reserved": res,
                "available": qty - res,
            }
        )
    return out


async def get_low_stock(threshold: float = 5.0) -> list[dict]:
    """Товары с низким ДОСТУПНЫМ остатком: (остаток − резерв) ≤ порога, но в
    наличии. Худшие сверху."""
    rows = await get_catalog()
    low = [r for r in rows if r["quantity"] > 0 and r["available"] <= float(threshold)]
    low.sort(key=lambda r: (r["available"], r["name"]))
    return low


# ─── Продажи (аналитика) ──────────────────────────────────────────────────────
#
# Считаем по расходным накладным — это и есть отгрузки. Раньше цифры приезжали
# из МойСклад (`moysklad.get_sales_stats`/`get_shipments`); источник сменился,
# форма ответа осталась прежней, чтобы экраны аналитики не переписывать.
#
# Отменённые накладные в выручку не идут: отмена вернула товар на склад, и
# продажи не было.


def _day(value) -> str:
    """`datetime` или строка → `YYYY-MM-DD` для сравнения с `invoice_date`."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:10]
    return value.strftime("%Y-%m-%d")


def _upper_bound(value) -> tuple[str, bool]:
    """Верхняя граница периода → (дата, строго ли меньше).

    `invoice_date` хранит ДАТУ, а границы приходят моментами. Полуинтервал
    [since, until) с обрезкой до дня ломает самый частый запрос — «сегодня»:
    начало дня и «сейчас» дают одну и ту же дату, и условие
    `date >= X AND date < X` не находит ничего.

    Поэтому: если у `until` есть время суток, день включаем (момент внутри
    него); если это ровно полночь — исключаем, потому что фронт передаёт
    следующую полночь именно как «до, не включая».
    """
    day = _day(value)
    if isinstance(value, str):
        # Строка без времени — это «по этот день включительно».
        return day, len(value) > 10 and value[11:].lstrip("0:") == ""
    midnight = (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0)
    return day, midnight


def _shipments_where(since, until, args: list) -> str:
    """WHERE расходных накладных за период — одно определение на список,
    итоги и топы. Границы: `since` включающая, верхняя — `_upper_bound`."""
    args.append("outgoing")
    args.append(_day(since))
    sql = f"i.type = ${len(args) - 1} AND i.status = 'confirmed' AND i.invoice_date >= ${len(args)}"
    if until is not None:
        day, exclusive = _upper_bound(until)
        args.append(day)
        sql += f" AND i.invoice_date {'<' if exclusive else '<='} ${len(args)}"
    return sql


async def list_shipments(since, until=None, limit: int = 1000) -> list[dict]:
    """Расходные накладные за период, новые сверху — СПИСОК для показа.

    Для итогов не годится: он обрезан `limit`. Итоги и топы считает
    `sales_stats` агрегатами в SQL, разбивку по дням — `shipment_counts_by_day`.
    """
    args: list = []
    where = _shipments_where(since, until, args)
    args.append(max(1, min(int(limit or 1000), 5000)))
    rows = await adb_core.fetch(
        "SELECT i.id, i.invoice_number, i.invoice_date, i.currency, i.total_amount_cents, "
        "       i.created_at, i.counterparty_id, c.name AS counterparty_name "
        "FROM invoices i LEFT JOIN counterparties c ON c.id = i.counterparty_id "
        f"WHERE {where} ORDER BY i.invoice_date DESC, i.id DESC LIMIT ${len(args)}",
        *args,
    )
    # `moment` — имя, на которое опирается разбор по дням недели в аналитике.
    for r in rows:
        r["moment"] = r["invoice_date"]
        r["sum"] = int(r["total_amount_cents"] or 0)
    return rows


async def shipment_counts_by_day(since, until=None) -> dict[str, int]:
    """Число отгрузок по дням периода {YYYY-MM-DD: n} — агрегатом, без лимита.

    Разбивка «по дням недели» в аналитике считалась по `list_shipments`, а он
    обрезан тысячей строк: в длинном периоде ранние дни молча пропадали."""
    args: list = []
    where = _shipments_where(since, until, args)
    rows = await adb_core.fetch(
        f"SELECT i.invoice_date AS day, COUNT(*) AS n FROM invoices i WHERE {where} "
        "GROUP BY i.invoice_date",
        *args,
    )
    return {str(r["day"])[:10]: int(r["n"] or 0) for r in rows}


_TOP_LIMIT = 20


def _base_equivalent_order(rates: dict[str, float | None], sum_sql: str, args: list) -> str:
    """ORDER BY для топа «по эквиваленту в базовой валюте», пригодный для LIMIT.

    Курсы — параметрами в CASE по валюте. Строки без курса не выбрасываем
    (продажа существует и без курса) — они идут после пересчитанных, по сумме.
    """
    known = [(cur, float(rate)) for cur, rate in sorted(rates.items()) if rate and rate > 0]
    if known:
        whens = []
        for cur, rate in known:
            args.append(cur)
            args.append(rate)
            whens.append(f"WHEN ${len(args) - 1} THEN CAST(${len(args)} AS DOUBLE PRECISION)")
        rate_sql = f"(CASE UPPER(i.currency) {' '.join(whens)} END)"
    else:
        rate_sql = "CAST(NULL AS DOUBLE PRECISION)"
    amount = f"CAST({sum_sql} AS DOUBLE PRECISION)"
    return (
        f"CASE WHEN {rate_sql} IS NULL THEN 1 ELSE 0 END, "
        f"COALESCE({amount} * {rate_sql}, {amount}) DESC"
    )


async def sales_stats(since, until=None) -> dict:
    """Выручка, число отгрузок, клиентов, топ товаров и клиентов за период.

    Итоги — агрегатами в SQL по ВСЕМ отгрузкам периода, топы — `ORDER BY …
    LIMIT` в SQL. Раньше всё считалось в Python по `list_shipments`, а тот
    обрезан тысячей строк: за период с большим числом отгрузок выручка, число
    отгрузок, клиенты и топы молча занижались, и ничего об этом не говорило.

    Валюты НЕ складываем молча (правило слоя дебиторки, CLAUDE.md). Отчёт
    продаж складывал USD и UZS в одно число: 1 000 USD + 12 500 000 UZS
    выглядели как «12 501 000» выручки, и по этой цифре считались тренд и
    средний чек. Поэтому:

    * `by_currency` — {валюта: копейки}, по отгрузкам как есть;
    * `base_total` — итог в копейках БАЗОВОЙ валюты по текущему курсу, только
      то, что пересчитать удалось (пересчёт — один раз на валюту по её сумме);
      `base_count` — сколько отгрузок в него вошло (для среднего чека);
      `missing` — {валюта: копейки} без курса, `base_partial` — часть выручки в
      итог не вошла. Снимка курса у накладной нет, поэтому курс текущий — как в
      «Долгах»;
    * топ товаров и клиентов — раздельно по валютам (`currency` в строке), а
      порядок — по эквиваленту в базовой валюте;
    * `total` — прежняя сумма копеек всех валют, оставлена для совместимости;
      показывать её человеку нельзя.
    """
    base = _base_currency()
    args: list = []
    where = _shipments_where(since, until, args)
    cur_rows = await adb_core.fetch(
        "SELECT UPPER(i.currency) AS currency, COUNT(*) AS cnt, "
        "       COALESCE(SUM(i.total_amount_cents), 0) AS cents "
        f"FROM invoices i WHERE {where} GROUP BY UPPER(i.currency)",
        *args,
    )
    count = sum(int(r["cnt"] or 0) for r in cur_rows)
    if not count:
        return {
            "total": 0,
            "count": 0,
            "clients": 0,
            "top_products": [],
            "top_clients": [],
            "by_currency": {},
            "base_currency": base,
            "base_total": 0,
            "base_count": 0,
            "base_partial": False,
            "missing": {},
        }

    by_currency: dict[str, int] = {}
    counts: dict[str, int] = {}
    for r in cur_rows:
        cur = (r["currency"] or base).upper()
        by_currency[cur] = by_currency.get(cur, 0) + int(r["cents"] or 0)
        counts[cur] = counts.get(cur, 0) + int(r["cnt"] or 0)

    # Курс — синхронное чтение с кэшем; один раз на валюту и в потоке, чтобы
    # промах кэша не держал event loop.
    rates: dict[str, float | None] = {}
    for cur in by_currency:
        rates[cur] = await asyncio.to_thread(_db.current_rate_to_base, cur)

    base_total = 0
    base_count = 0
    missing: dict[str, int] = {}
    for cur, cents in by_currency.items():
        rate = rates.get(cur)
        if rate is None or rate <= 0:
            missing[cur] = cents
        else:
            base_total += money.convert_cents(cents, rate)
            base_count += counts[cur]

    # Клиенты — отдельным запросом: один и тот же покупатель в двух валютах —
    # всё равно один клиент, сумма COUNT(DISTINCT) по группам его удвоила бы.
    clients = int(
        await adb_core.fetchval(
            f"SELECT COUNT(DISTINCT i.counterparty_id) FROM invoices i WHERE {where}", *args
        )
        or 0
    )

    client_args = list(args)
    client_order = _base_equivalent_order(rates, "SUM(i.total_amount_cents)", client_args)
    client_args.append(_TOP_LIMIT)
    client_rows = await adb_core.fetch(
        "SELECT COALESCE(c.name, '—') AS name, UPPER(i.currency) AS currency, "
        "       COALESCE(SUM(i.total_amount_cents), 0) AS sum_cents, COUNT(*) AS cnt "
        "FROM invoices i LEFT JOIN counterparties c ON c.id = i.counterparty_id "
        f"WHERE {where} GROUP BY COALESCE(c.name, '—'), UPPER(i.currency) "
        f"ORDER BY {client_order}, COALESCE(c.name, '—') LIMIT ${len(client_args)}",
        *client_args,
    )

    line_sum = "SUM(CAST(round(ii.quantity * ii.price_cents) AS BIGINT))"
    prod_args = list(args)
    prod_order = _base_equivalent_order(rates, f"COALESCE({line_sum}, 0)", prod_args)
    prod_args.append(_TOP_LIMIT)
    prod_rows = await adb_core.fetch(
        "SELECT p.name AS name, UPPER(i.currency) AS currency, "
        f"       COALESCE({line_sum}, 0) AS sum_cents, "
        "       COALESCE(SUM(ii.quantity), 0) AS qty, MAX(ii.product_id) AS product_id "
        "FROM invoice_items ii "
        "JOIN invoices i ON i.id = ii.invoice_id "
        "JOIN products p ON p.id = ii.product_id "
        f"WHERE {where} GROUP BY p.name, UPPER(i.currency) "
        f"ORDER BY {prod_order}, p.name LIMIT ${len(prod_args)}",
        *prod_args,
    )

    top_products = [
        (
            r["name"] or "—",
            {
                "sum": int(r["sum_cents"] or 0),
                "qty": float(r["qty"] or 0),
                "product_id": int(r["product_id"]) if r["product_id"] is not None else None,
                "currency": (r["currency"] or base).upper(),
            },
        )
        for r in prod_rows
    ]
    top_clients = [
        (
            r["name"] or "—",
            {
                "sum": int(r["sum_cents"] or 0),
                "count": int(r["cnt"] or 0),
                "currency": (r["currency"] or base).upper(),
            },
        )
        for r in client_rows
    ]
    return {
        "total": sum(by_currency.values()),
        "count": count,
        "clients": clients,
        "top_products": top_products,
        "top_clients": top_clients,
        "by_currency": by_currency,
        "base_currency": base,
        "base_total": base_total,
        "base_count": base_count,
        "base_partial": bool(missing),
        "missing": missing,
    }


async def counterparty_purchases(counterparty_id, limit: int = 20) -> dict:
    """Покупки контрагента: топ товаров и последние отгрузки. Для карточки клиента."""
    try:
        cid = int(counterparty_id)
    except (TypeError, ValueError):
        return {"top_products": [], "recent": [], "total_cents": 0, "count": 0}

    rows = await adb_core.fetch(
        "SELECT id, invoice_number, invoice_date, currency, total_amount_cents "
        "FROM invoices WHERE counterparty_id = $1 AND type = 'outgoing' "
        "AND status = 'confirmed' ORDER BY invoice_date DESC, id DESC LIMIT $2",
        cid,
        max(1, min(int(limit or 20), 200)),
    )
    if not rows:
        return {"top_products": [], "recent": [], "total_cents": 0, "count": 0}

    ids = [int(r["id"]) for r in rows]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(ids)))
    positions = await adb_core.fetch(
        f"SELECT ii.product_id, p.name, ii.quantity, ii.price_cents "
        f"FROM invoice_items ii JOIN products p ON p.id = ii.product_id "
        f"WHERE ii.invoice_id IN ({placeholders})",
        *ids,
    )
    agg: dict[str, dict] = {}
    for pos in positions:
        name = pos["name"] or "—"
        d = agg.setdefault(name, {"sum_cents": 0, "qty": 0.0})
        d["sum_cents"] += money.mul_qty(int(pos["price_cents"] or 0), float(pos["quantity"] or 0))
        d["qty"] += float(pos["quantity"] or 0)
    top = sorted(
        ({"name": k, **v} for k, v in agg.items()), key=lambda d: d["sum_cents"], reverse=True
    )
    return {
        "top_products": top[:10],
        "recent": [
            {
                "id": int(r["id"]),
                "number": r["invoice_number"],
                "date": r["invoice_date"],
                "currency": r["currency"],
                "sum_cents": int(r["total_amount_cents"] or 0),
            }
            for r in rows[:10]
        ],
        "total_cents": sum(int(r["total_amount_cents"] or 0) for r in rows),
        "count": len(rows),
    }
