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
from datetime import datetime, timedelta
from typing import Any

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
        raise InvoiceError("empty_invoice", "В документе нет ни одной позиции — добавьте товар")
    if len(items) > MAX_POSITIONS:
        raise InvoiceError(
            "too_many_positions",
            f"В одном документе не больше {MAX_POSITIONS} позиций — разбейте на несколько",
        )

    merged: dict[int, dict] = {}
    for raw in items:
        try:
            product_id = int(raw.get("product_id") or 0)
        except (TypeError, ValueError):
            raise InvoiceError("bad_product_id", "В позиции выбран неизвестный товар — выберите его из каталога")
        if product_id <= 0:
            raise InvoiceError("bad_product_id", "В позиции не выбран товар — выберите его из каталога")

        try:
            quantity = float(raw.get("quantity") or 0)
        except (TypeError, ValueError):
            raise InvoiceError("bad_quantity", "Количество должно быть числом больше нуля")
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
                    "price_required", f"Укажите цену — без неё отгрузку не оформить (товар #{product_id})"
                )
        else:
            try:
                price_cents = int(price_cents)
            except (TypeError, ValueError):
                raise InvoiceError("bad_price", f"Цена должна быть числом (товар #{product_id})")
            if price_cents < 0:
                raise InvoiceError("bad_price", f"Цена не может быть отрицательной (товар #{product_id})")

        if product_id in merged:
            prev = merged[product_id]
            prev["quantity"] += quantity
            # Повтор товара с РАЗНОЙ ценой схлопнуть нельзя: сумма накладной
            # станет неоднозначной. Это ошибка ввода, а не валидный кейс.
            if prev["price_cents"] != price_cents:
                raise InvoiceError(
                    "duplicate_price_conflict",
                    f"Товар #{product_id} добавлен дважды с разными ценами — оставьте одну строку",
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

    Списание (delta < 0) — сначала UPDATE. Postgres проверяет CHECK у
    ВСТАВЛЯЕМОЙ строки UPSERT'а до разрешения конфликта, и `quantity >= 0`
    (`scripts/apply_constraints`) отверг бы любую расходную накладную: строка
    (товар, склад, −2.5) не проходит проверку ещё до того, как превратится в
    UPDATE существующего остатка. Строки нет — вставляем как раньше.
    """
    if delta < 0:
        updated = await txn.execute(
            "UPDATE stock SET quantity = quantity + $3 "
            "WHERE product_id = $1 AND warehouse_id = $2",
            product_id,
            warehouse_id,
            delta,
        )
        if updated:
            return
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
        raise InvoiceError("bad_type", "Неизвестный вид движения — выберите приход или отгрузку")

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
            f"Товары не найдены в каталоге: {', '.join('#' + str(m) for m in missing)} — "
            "обновите каталог и выберите их заново",
            {"product_ids": missing},
        )

    wh_row = await txn.fetchrow(
        "SELECT w.id, (wa.warehouse_id IS NOT NULL) AS archived FROM warehouses w "
        "LEFT JOIN warehouse_archived wa ON wa.warehouse_id = w.id WHERE w.id = $1",
        warehouse_id,
    )
    if wh_row is None:
        raise InvoiceError("unknown_warehouse", f"Склад #{warehouse_id} не найден — выберите склад из списка")
    # Архивный склад не предлагается в форме (`list_warehouses(include_archived=
    # False)`), но проверяем и здесь — прямой вызов ручки с чужим id не должен
    # тихо провести накладную на склад, который уже считается пустым и закрытым.
    # Пока архивных складов нет вовсе (сегодняшний случай), это условие никогда
    # не срабатывает — поведение при одном складе не меняется.
    if bool(wh_row["archived"]):
        raise InvoiceError("archived_warehouse", f"Склад #{warehouse_id} убран в архив — выберите действующий склад")

    if counterparty_id is not None:
        cp = await txn.fetchval("SELECT id FROM counterparties WHERE id = $1", counterparty_id)
        if cp is None:
            raise InvoiceError("unknown_counterparty", f"Клиент или поставщик #{counterparty_id} не найден — выберите его в справочнике «Клиенты и поставщики»")

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
                f"товар #{s['product_id']}: нужно {s['need']:g}, на складе {s['have']:g}" for s in short
            )
            raise InvoiceError(
                "insufficient_stock",
                f"Не хватает товара на складе — {names}. Уменьшите количество или оформите приход",
                {"positions": short},
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


def invoice_currency_error(currency: str | None) -> str:
    """Валюта накладной из формы — только `config.ALLOWED_CURRENCIES`. Пусто — ок.

    Внутренние проводки (`create_invoice_in`: отгрузка, контейнер) берут валюту
    из своих документов и сюда не ходят."""
    from config import ALLOWED_CURRENCIES

    allowed = [c.upper() for c in ALLOWED_CURRENCIES]
    if str(currency or "").strip().upper() in allowed:
        return ""
    return f"Валюта не поддерживается — выберите {' или '.join(allowed)}"


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
    currency_err = invoice_currency_error(currency)
    if currency_err:
        return {"ok": False, "code": "bad_currency", "reason": currency_err, "details": {}}
    currency = str(currency).strip().upper()
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


# ─── Исторические накладные (перенос из МойСклад) ─────────────────────────────
#
# `scripts/migrate_history_from_moysklad.py` пишет накладные МИМО этого модуля:
# остаток приехал снимком на сегодня и все исторические движения уже включает.
# Значит, и отмена такой накладной обязана НЕ двигать склад — а отменить её,
# ничего не двигая, бессмысленно. Поэтому отмена запрещена целиком: обратное
# движение по документу, который склад не двигал, вернуло бы на склад товар,
# давно уехавший к клиенту (или списало приход, давно проданный).
#
# Признак — два независимых следа переноса, любой из них:
#   * номер из серии переноса `MS-D-*`/`MS-S-*`. Живая нумерация — только
#     `IN-/OUT-ГГГГ-NNNN` из `_next_invoice_number`, номер руками не вводится,
#     `invoice_number` UNIQUE — спутать нельзя. Этот след есть и у накладных,
#     записанных ранней версией скрипта, которая ms_id_map не вела;
#   * строка `ms_id_map(entity_type in demand/supply)` — ключ идемпотентности
#     переноса, пишется той же транзакцией, что и накладная. Он переживёт,
#     если номер когда-нибудь поправят руками.
# `created_by = 0` признаком НЕ берём: колонка nullable, и «0 = система» —
# соглашение, которое живой код может однажды повторить для своих накладных.
HISTORY_NUMBER_PREFIXES = ("MS-D-", "MS-S-")
HISTORY_MAP_ENTITIES = ("demand", "supply")


def writeoff_invoice_sql(alias: str = "i") -> str:
    """SQL-условие «накладная — это списание/излишек» (`services/inventory.py`).

    Одно определение на всех: аналитика продаж его вычитает, список накладных
    помечает такую строку, отмена — отсылает к ленте списаний. Признак —
    запись-владелец в `stock_writeoffs`, а не номер или комментарий: номер
    берётся из общей серии (документ склада один), а комментарий человек правит.
    """
    return (
        f"EXISTS (SELECT 1 FROM stock_writeoffs sw WHERE sw.invoice_id = {alias}.id)"
    )


def not_writeoff_sql(alias: str = "i") -> str:
    return f"NOT {writeoff_invoice_sql(alias)}"


def historical_invoice_sql(alias: str = "i") -> str:
    """SQL-условие «накладная из переноса истории» — для списков и отказов."""
    by_number = " OR ".join(
        f"{alias}.invoice_number LIKE '{prefix}%'" for prefix in HISTORY_NUMBER_PREFIXES
    )
    entities = ", ".join(f"'{e}'" for e in HISTORY_MAP_ENTITIES)
    return (
        f"({by_number} OR EXISTS (SELECT 1 FROM ms_id_map hm "
        f"WHERE hm.entity_type IN ({entities}) AND hm.local_id = {alias}.id))"
    )


async def historical_invoice_refusal(invoice_id: int, *, txn=None) -> str | None:
    """Текст отказа в отмене исторической накладной; None — накладная живая."""
    runner = txn if txn is not None else adb_core
    row = await runner.fetchrow(
        f"SELECT i.type, i.invoice_number, {historical_invoice_sql('i')} AS historical "
        "FROM invoices i WHERE i.id = $1",
        int(invoice_id),
    )
    if row is None or not row["historical"]:
        return None
    what = "отгрузку" if row["type"] == "outgoing" else "приход"
    fix = (
        "Если клиент вернул товар — оформите возврат по заказу."
        if row["type"] == "outgoing"
        else "Если остаток расходится с фактом — поправьте его новым приходом или списанием."
    )
    return (
        f"Движение {row['invoice_number']} перенесено из МойСклад, и отменить его нельзя: "
        f"остаток склада приехал снимком, который эту {what} уже учитывает, и отмена "
        f"сдвинула бы склад на товар, которого перенос не двигал. {fix}"
    )


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
        raise InvoiceError("not_found", f"Документ #{invoice_id} не найден — обновите список")
    if inv["status"] == "cancelled":
        # Идемпотентно: повторная отмена не двигает остаток второй раз.
        raise InvoiceError("already_cancelled", "Этот документ уже отменён")
    # Здесь, а не только в ручке: отмену зовут и заказ (cancel_shipment), и
    # контейнер, и приёмка — запрет в одном месте закрывает все пути сразу.
    historical = await historical_invoice_refusal(invoice_id, txn=txn)
    if historical:
        raise InvoiceError("historical", historical)

    warehouse_id = int(inv["warehouse_id"])
    rows = await txn.fetch(
        "SELECT product_id, quantity FROM invoice_items WHERE invoice_id = $1 ORDER BY product_id",
        invoice_id,
    )
    if not rows:
        raise InvoiceError("empty_invoice", "В документе нет позиций — отменять нечего")

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
                f"товар #{s['product_id']}: нужно вернуть {s['need']:g}, на складе {s['have']:g}"
                for s in short
            )
            raise InvoiceError(
                "insufficient_stock",
                f"Отменить нельзя: остаток ушёл бы в минус — {names}. "
                f"Товар уже отгружен — сначала отмените отгрузки.",
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

    Берём минимальный id СРЕДИ АКТИВНЫХ, а не константу 1: на проде склад
    могли завести руками раньше сидинга, и захардкоженная единица указывала
    бы в пустоту — накладная отвергалась бы «склад не найден» на ровном
    месте. Архивный склад в кандидаты не попадает: он не предлагается ни в
    одной форме, и молчаливое списание туда/оттуда удивило бы кладовщика.
    """
    wid = await adb_core.fetchval(
        "SELECT MIN(w.id) FROM warehouses w "
        "LEFT JOIN warehouse_archived wa ON wa.warehouse_id = w.id "
        "WHERE wa.warehouse_id IS NULL"
    )
    if wid is not None:
        return int(wid)
    # Все склады в архиве (или их нет) — деградируем к минимальному id вообще,
    # чтобы не отвечать «склад не найден» там, где раньше отвечали.
    wid = await adb_core.fetchval("SELECT MIN(id) FROM warehouses")
    return int(wid) if wid is not None else 1


# ─── Справочник складов (B8 — несколько складов) ───────────────────────────
#
# Пока в компании ОДНА физическая точка, и `seed_warehouses` заводит её одну
# («Основной склад»). Экран управления складами — на будущее: список/
# добавление/переименование/архив, admin/boss-only. Инвариант: пока склад
# один — поведение системы byte-в-byte как до этого раздела (все выборки
# `default_warehouse_id()`/`list_warehouses(include_archived=False)` с одним
# складом отдают ровно его).


class WarehouseError(Exception):
    """Операция со справочником складов/перемещением отклонена.

    `code` — машиночитаемая причина для WebApp, `message` — текст менеджеру.
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


_WAREHOUSE_NAME_MAX = 200


def _clean_warehouse_name(raw: str | None) -> str:
    text = " ".join(str(raw or "").split())
    if not text:
        raise WarehouseError("bad_name", "Укажите название склада")
    if len(text) > _WAREHOUSE_NAME_MAX:
        raise WarehouseError("bad_name", "Название склада слишком длинное — сократите его")
    return text


async def list_warehouses(include_archived: bool = True) -> list[dict]:
    """Справочник складов. `include_archived=False` — только для выбора
    (накладная, перемещение, заказ): архивный склад в форме не нужен."""
    sql = (
        "SELECT w.id, w.name, (wa.warehouse_id IS NOT NULL) AS archived "
        "FROM warehouses w LEFT JOIN warehouse_archived wa ON wa.warehouse_id = w.id"
    )
    if not include_archived:
        sql += " WHERE wa.warehouse_id IS NULL"
    sql += f" ORDER BY {adb_core.order_by_name('w.name')}, w.id"
    rows = await adb_core.fetch(sql)
    return [{"id": int(r["id"]), "name": r["name"], "archived": bool(r["archived"])} for r in rows]


async def active_warehouse_count() -> int:
    """Сколько складов НЕ в архиве — им управляют экраны выбора («показывать
    ли пикер», «показывать ли разбивку по складам в каталоге»)."""
    n = await adb_core.fetchval(
        "SELECT COUNT(*) FROM warehouses w "
        "LEFT JOIN warehouse_archived wa ON wa.warehouse_id = w.id "
        "WHERE wa.warehouse_id IS NULL"
    )
    return int(n or 0)


async def create_warehouse(name: str, *, created_by: int | None = None) -> dict:
    """Завести склад. Тёзку (без учёта регистра/пробелов) не заводим —
    проверка ВНУТРИ транзакции, кнопку можно нажать дважды."""
    clean = _clean_warehouse_name(name)
    async with adb_core.transaction() as txn:
        existing = await txn.fetchrow(
            "SELECT id, name FROM warehouses WHERE lower(name) = lower($1)", clean
        )
        if existing is not None:
            return {"ok": True, "warehouse_id": int(existing["id"]), "name": existing["name"], "existed": True}
        await txn.execute("INSERT INTO warehouses (name) VALUES ($1)", clean)
        wid = await txn.fetchval("SELECT id FROM warehouses WHERE lower(name) = lower($1)", clean)
    logger.info("Склад #%s «%s» заведён (created_by=%s)", wid, clean, created_by)
    return {"ok": True, "warehouse_id": int(wid), "name": clean, "existed": False}


async def rename_warehouse(warehouse_id: int, name: str) -> dict:
    clean = _clean_warehouse_name(name)
    async with adb_core.transaction() as txn:
        row = await txn.fetchrow("SELECT id FROM warehouses WHERE id = $1", int(warehouse_id))
        if row is None:
            raise WarehouseError("not_found", f"Склад #{warehouse_id} не найден — обновите список")
        dupe = await txn.fetchrow(
            "SELECT id FROM warehouses WHERE lower(name) = lower($1) AND id <> $2",
            clean, int(warehouse_id),
        )
        if dupe is not None:
            raise WarehouseError("duplicate_name", f"Склад «{clean}» уже заведён — выберите его или назовите новый иначе")
        await txn.execute("UPDATE warehouses SET name = $1 WHERE id = $2", clean, int(warehouse_id))
    return {"ok": True, "warehouse_id": int(warehouse_id), "name": clean}


async def archive_warehouse(warehouse_id: int, *, archived_by: int | None = None) -> dict:
    """В архив можно только пустой склад (остаток 0 по всем товарам) — иначе
    товар «пропадает» из выбора при живом остатке."""
    wid = int(warehouse_id)
    async with adb_core.transaction() as txn:
        row = await txn.fetchrow("SELECT id FROM warehouses WHERE id = $1", wid)
        if row is None:
            raise WarehouseError("not_found", f"Склад #{wid} не найден — обновите список")
        already = await txn.fetchval(
            "SELECT warehouse_id FROM warehouse_archived WHERE warehouse_id = $1", wid
        )
        if already is not None:
            return {"ok": True, "warehouse_id": wid, "already_archived": True}
        nonzero = await txn.fetchval(
            "SELECT COUNT(*) FROM stock WHERE warehouse_id = $1 AND quantity <> 0", wid
        )
        if int(nonzero or 0):
            raise WarehouseError(
                "nonzero_stock",
                "На складе есть остаток — сначала переместите товар на другой склад",
            )
        remaining = await active_warehouse_count()
        if remaining <= 1:
            raise WarehouseError(
                "last_active_warehouse",
                "Это последний действующий склад — его нельзя убрать в архив",
            )
        await txn.execute(
            "INSERT INTO warehouse_archived (warehouse_id, archived_at, archived_by) "
            "VALUES ($1, $2, $3)",
            wid, _db.now_str(), archived_by,
        )
    logger.info("Склад #%s отправлен в архив (archived_by=%s)", wid, archived_by)
    return {"ok": True, "warehouse_id": wid, "already_archived": False}


async def unarchive_warehouse(warehouse_id: int) -> dict:
    n = await adb_core.execute(
        "DELETE FROM warehouse_archived WHERE warehouse_id = $1", int(warehouse_id)
    )
    return {"ok": True, "warehouse_id": int(warehouse_id), "restored": bool(n)}


async def last_used_warehouse_id(user_id: int) -> int | None:
    """Склад, который человек указывал последним в своих накладных — предлагаем
    его по умолчанию в форме, как `pay_accounts.last_used`. None — ещё ничего
    не проводил (форма отдаёт единственный/первый активный склад)."""
    wid = await adb_core.fetchval(
        "SELECT warehouse_id FROM invoices WHERE created_by = $1 "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        int(user_id),
    )
    return int(wid) if wid is not None else None


async def get_order_warehouse(order_id: int) -> int | None:
    """Склад, с которого отгружать ЭТОТ заказ, если менеджер его выбрал.
    None — обычный случай (нет строки), отгрузка берёт склад по умолчанию."""
    wid = await adb_core.fetchval(
        "SELECT warehouse_id FROM order_warehouse WHERE order_id = $1", int(order_id)
    )
    return int(wid) if wid is not None else None


async def set_order_warehouse(order_id: int, warehouse_id: int) -> dict:
    """Запомнить выбор склада для черновика заказа. UPSERT — форма может
    вызываться повторно (передумал менеджер)."""
    wid = int(warehouse_id)
    row = await adb_core.fetchrow(
        "SELECT id FROM warehouses w LEFT JOIN warehouse_archived wa "
        "ON wa.warehouse_id = w.id WHERE w.id = $1 AND wa.warehouse_id IS NULL",
        wid,
    )
    if row is None:
        raise WarehouseError("unknown_warehouse", f"Склад #{wid} не найден или убран в архив — выберите другой")
    if _db.USE_POSTGRES:
        await adb_core.execute(
            "INSERT INTO order_warehouse (order_id, warehouse_id) VALUES ($1, $2) "
            "ON CONFLICT (order_id) DO UPDATE SET warehouse_id = EXCLUDED.warehouse_id",
            int(order_id), wid,
        )
    else:
        updated = await adb_core.execute(
            "UPDATE order_warehouse SET warehouse_id = $1 WHERE order_id = $2", wid, int(order_id)
        )
        if not updated:
            await adb_core.execute(
                "INSERT INTO order_warehouse (order_id, warehouse_id) VALUES ($1, $2)",
                int(order_id), wid,
            )
    return {"ok": True, "order_id": int(order_id), "warehouse_id": wid}


async def resolve_order_warehouse(order_id: int) -> int:
    """Склад отгрузки заказа: выбор менеджера, если есть, иначе — по
    умолчанию. Единственная точка, которую зовёт `order_shipment.ship_order` —
    в однoскладском случае строки в `order_warehouse` нет никогда, и ответ
    всегда `default_warehouse_id()`, как до этого раздела."""
    chosen = await get_order_warehouse(order_id)
    if chosen is not None:
        return chosen
    return await default_warehouse_id()


# ─── Перемещение остатка между складами ─────────────────────────────────────


async def transfer_stock(
    *,
    product_id: int,
    quantity: float,
    from_warehouse_id: int,
    to_warehouse_id: int,
    comment: str | None = None,
    created_by: int | None = None,
) -> dict:
    """Переместить остаток товара между складами — одна транзакция.

    Списывает с одного склада, приходует на другой, пишет строку в
    `stock_transfers` для истории. Остаток никогда не уходит в минус: как и
    у накладных, нехватка откатывает всё перемещение целиком. Возвращает
    `{"ok": True, "transfer_id", ...}` либо `{"ok": False, "code", "reason"}`.
    """
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return {"ok": False, "code": "bad_product_id", "reason": "Не выбран товар"}
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return {"ok": False, "code": "bad_quantity", "reason": "Количество должно быть числом больше нуля"}
    if qty <= 0:
        return {"ok": False, "code": "bad_quantity", "reason": "Количество должно быть больше нуля"}
    from_wh = int(from_warehouse_id)
    to_wh = int(to_warehouse_id)
    if from_wh == to_wh:
        return {
            "ok": False,
            "code": "same_warehouse",
            "reason": "Склад отправления и назначения — один и тот же: выберите разные",
        }

    try:
        async with adb_core.transaction() as txn:
            wh_rows = await txn.fetch(
                "SELECT w.id, (wa.warehouse_id IS NOT NULL) AS archived "
                "FROM warehouses w LEFT JOIN warehouse_archived wa ON wa.warehouse_id = w.id "
                "WHERE w.id IN ($1, $2)",
                from_wh, to_wh,
            )
            found = {int(r["id"]): bool(r["archived"]) for r in wh_rows}
            missing = [w for w in (from_wh, to_wh) if w not in found]
            if missing:
                raise WarehouseError(
                    "unknown_warehouse",
                    f"Склад не найден: {', '.join('#' + str(m) for m in missing)} — обновите список",
                )
            archived = [w for w in (from_wh, to_wh) if found.get(w)]
            if archived:
                raise WarehouseError(
                    "archived_warehouse",
                    f"Склад убран в архив: {', '.join('#' + str(a) for a in archived)} — выберите действующий",
                )
            product = await txn.fetchval("SELECT id FROM products WHERE id = $1", pid)
            if product is None:
                raise WarehouseError("unknown_product", f"Товар #{pid} не найден в каталоге — выберите его заново")

            # Блокируем обе строки остатка в детерминированном порядке (по
            # warehouse_id) — как позиции накладной сортируются по product_id:
            # без него встречное перемещение того же товара [A→B] и [B→A]
            # берёт строки в обратном порядке и ловит deadlock на Postgres.
            locked_qty: dict[int, float] = {}
            for wh in sorted((from_wh, to_wh)):
                sql = "SELECT quantity FROM stock WHERE product_id = $1 AND warehouse_id = $2"
                if _db.USE_POSTGRES:
                    sql += " FOR UPDATE"
                val = await txn.fetchval(sql, pid, wh)
                locked_qty[wh] = float(val or 0)
            have_from = locked_qty[from_wh]
            if have_from < qty:
                raise WarehouseError(
                    "insufficient_stock",
                    f"На складе не хватает товара: нужно {qty:g}, есть {have_from:g} — уменьшите количество или оформите приход",
                    {"have": have_from, "need": qty},
                )

            await _apply_stock_delta(txn, pid, from_wh, -qty)
            await _apply_stock_delta(txn, pid, to_wh, qty)

            created = _db.now_str()
            await txn.execute(
                "INSERT INTO stock_transfers (product_id, from_warehouse_id, to_warehouse_id, "
                "quantity, comment, created_by, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                pid, from_wh, to_wh, qty, comment, created_by, created,
            )
            # RETURNING обходим тем же приёмом, что и накладные: SQLite до
            # 3.35 его не знает, а строка уже под нашей транзакцией — второй
            # SELECT безопасен. MAX(id) — счётчик перемещений один на всю
            # таблицу, гонки внутри своей же открытой транзакции нет.
            transfer_id = await txn.fetchval(
                "SELECT MAX(id) FROM stock_transfers WHERE product_id = $1 "
                "AND from_warehouse_id = $2 AND to_warehouse_id = $3 AND created_at = $4",
                pid, from_wh, to_wh, created,
            )
    except WarehouseError as e:
        logger.info("Перемещение не проведено (%s): %s", e.code, e.message)
        return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}

    logger.info(
        "Перемещение #%s: товар #%s, %s → %s, %.4g",
        transfer_id, pid, from_wh, to_wh, qty,
    )
    return {
        "ok": True,
        "transfer_id": int(transfer_id) if transfer_id is not None else None,
        "product_id": pid,
        "from_warehouse_id": from_wh,
        "to_warehouse_id": to_wh,
        "quantity": qty,
    }


async def list_stock_transfers(limit: int = 50, offset: int = 0) -> list[dict]:
    """История перемещений, новые сверху — для экрана босса."""
    cap = max(1, min(int(limit or 50), 500))
    return await adb_core.fetch(
        "SELECT t.id, t.product_id, p.name AS product_name, p.unit, "
        "       t.from_warehouse_id, wf.name AS from_warehouse_name, "
        "       t.to_warehouse_id, wt.name AS to_warehouse_name, "
        "       t.quantity, t.comment, t.created_by, t.created_at "
        "FROM stock_transfers t "
        "JOIN products p ON p.id = t.product_id "
        "JOIN warehouses wf ON wf.id = t.from_warehouse_id "
        "JOIN warehouses wt ON wt.id = t.to_warehouse_id "
        "ORDER BY t.id DESC LIMIT $1 OFFSET $2",
        cap, offset,
    )


async def stock_breakdown(product_ids: list[int] | None = None) -> dict[int, list[dict]]:
    """Остаток по складам для каталога: {product_id: [{warehouse_id, name, quantity}]}.

    Зовётся ТОЛЬКО когда активных складов больше одного (см. `api_stock`) —
    при одном складе разбивка не нужна: сумма и так равна остатку на нём."""
    args: list = []
    where = ""
    if product_ids:
        placeholders = ", ".join(f"${i + 1}" for i in range(len(product_ids)))
        where = f"WHERE s.product_id IN ({placeholders})"
        args = list(product_ids)
    rows = await adb_core.fetch(
        "SELECT s.product_id, s.warehouse_id, w.name AS warehouse_name, s.quantity "
        "FROM stock s JOIN warehouses w ON w.id = s.warehouse_id "
        f"{where} "
        f"ORDER BY s.product_id, {adb_core.order_by_name('w.name')}",
        *args,
    )
    out: dict[int, list[dict]] = {}
    for r in rows:
        pid = int(r["product_id"])
        out.setdefault(pid, []).append(
            {
                "warehouse_id": int(r["warehouse_id"]),
                "warehouse_name": r["warehouse_name"],
                "quantity": float(r["quantity"] or 0),
            }
        )
    return out


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
    sql += f" ORDER BY {adb_core.order_by_name('p.name')}, p.id"
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
    # `historical` — для фронта: кнопку «Отменить» у накладной из переноса не
    # рисуем, она гарантированно ответила бы отказом.
    sql = (
        "SELECT i.id, i.type, i.invoice_number, i.invoice_date, i.status, i.currency, "
        "       i.total_amount_cents, i.telegram_sent, i.created_at, i.created_by, "
        f"      {historical_invoice_sql('i')} AS historical, "
        f"      {writeoff_invoice_sql('i')} AS writeoff, "
        "       c.name AS counterparty_name "
        "FROM invoices i LEFT JOIN counterparties c ON c.id = i.counterparty_id"
    )
    args: list = []
    if invoice_type:
        args.append(invoice_type)
        sql += f" WHERE i.type = ${len(args)}"
    args.extend([limit, offset])
    sql += f" ORDER BY i.id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
    rows = await adb_core.fetch(sql, *args)
    # SQLite отдаёт условие числом 0/1, Postgres — bool; фронту нужен bool.
    return [
        {**r, "historical": bool(r["historical"]), "writeoff": bool(r["writeoff"])} for r in rows
    ]


async def list_invoices_for_export(
    date_from: str | None = None,
    date_to: str | None = None,
    invoice_type: str | None = None,
) -> list[dict]:
    """Накладные за период — для Excel-выгрузки (B6, «Склад → Накладные»).

    `invoice_date` — ДАТА (не момент), поэтому границы включительные с обеих
    сторон: полуинтервал с обрезкой до дня здесь ни к чему (в отличие от
    `_upper_bound`, который нужен там, где сравнивают с МОМЕНТОМ). Отменённые
    накладные попадают в выгрузку тоже — статус виден отдельной колонкой,
    прятать историю от того, кто и так видит её на экране, незачем.
    """
    sql = (
        "SELECT i.id, i.type, i.invoice_number, i.invoice_date, i.status, i.currency, "
        "       i.total_amount_cents, c.name AS counterparty_name "
        "FROM invoices i LEFT JOIN counterparties c ON c.id = i.counterparty_id"
    )
    where: list[str] = []
    args: list = []
    if date_from:
        args.append(date_from)
        where.append(f"i.invoice_date >= ${len(args)}")
    if date_to:
        args.append(date_to)
        where.append(f"i.invoice_date <= ${len(args)}")
    if invoice_type:
        args.append(invoice_type)
        where.append(f"i.type = ${len(args)}")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.invoice_date ASC, i.id ASC"
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


async def search_products(query: str, limit: int = 20, *, browse: bool = False) -> list[dict]:
    """Поиск по номенклатуре: название или артикул, кириллица через `lower()` с
    обеих сторон, ё = е. В строке — остаток (сумма по складам).

    `browse` — выбор товара из списка (шторка позиции контейнера): пустой запрос
    отдаёт первые `limit` товаров по алфавиту, а не пустоту, — список виден до
    первой буквы. Без него пустой запрос — пустой ответ (подсказка под полем).
    Остаток нужен выбирающему, чтобы сверить «тот ли это товар».
    """
    text = (query or "").strip()
    if not text and not browse:
        return []
    args: list[Any] = []
    where = ""
    if text:
        args.append(adb_core.name_search_param(text))
        where = (
            f"WHERE {adb_core.name_search_sql('p.name')} LIKE $1 "
            f"OR {adb_core.name_search_sql('p.sku')} LIKE $1 "
        )
    args.append(max(1, min(int(limit or 20), 100)))
    rows = await adb_core.fetch(
        "SELECT p.id AS product_id, p.name, p.unit, p.category, p.sku, "
        "       COALESCE(s.qty, 0) AS quantity "
        "FROM products p "
        "LEFT JOIN (SELECT product_id, SUM(quantity) AS qty FROM stock GROUP BY product_id) s "
        "       ON s.product_id = p.id "
        f"{where}"
        f"ORDER BY {adb_core.order_by_name('p.name')}, p.id LIMIT ${len(args)}",
        *args,
    )
    for row in rows:
        row["quantity"] = float(row.get("quantity") or 0)
    return rows


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
    # GROUP BY, а не DISTINCT: Postgres не пускает в ORDER BY при DISTINCT
    # выражение, которого нет в списке выборки, а `category COLLATE …` для него
    # уже другое выражение.
    rows = await adb_core.fetch(
        "SELECT category FROM products "
        "WHERE category IS NOT NULL AND category <> '' GROUP BY category "
        f"ORDER BY {adb_core.order_by_name('category')}"
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
    sql += (
        " GROUP BY p.id, p.name, p.unit, p.category, p.sku "
        f"ORDER BY {adb_core.order_by_name('p.name')}, p.id"
    )
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
    итоги и топы. Границы: `since` включающая, верхняя — `_upper_bound`.

    Списание — тоже расходная накладная (`services/inventory.py`), но НЕ
    продажа: её позиции уходят с ценой 0. Без исключения отчёт продаж получил
    бы отгрузки на нулевую сумму — число отгрузок и клиентов росло бы, средний
    чек падал, а «топ товаров» считал бы разбитое проданным.
    """
    args.append("outgoing")
    args.append(_day(since))
    sql = (
        f"i.type = ${len(args) - 1} AND i.status = 'confirmed' "
        f"AND {not_writeoff_sql('i')} AND i.invoice_date >= ${len(args)}"
    )
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


# Сколько всего купил — считается по ВСЕМ подтверждённым расходным накладным
# клиента, а не по той горсти, что показана в карточке. Раньше `total_cents`
# был суммой последних `limit` отгрузок и складывал разные валюты в одно
# число: владелец спрашивал «на какую общую сумму он покупал», а видел итог
# двадцати последних накладных в чужой валюте. Теперь итог — отдельная
# агрегация с GROUP BY currency (складывать USD и UZS нельзя) плюс перевод в
# базовую для сортировки списка.
_PURCHASE_TOTALS_SQL = (
    "SELECT i.currency AS currency, SUM(i.total_amount_cents) AS sum_cents, "
    "COUNT(*) AS cnt, MAX(i.invoice_date) AS last_date "
    "FROM invoices i WHERE i.counterparty_id = $1 AND i.type = 'outgoing' "
    f"AND i.status = 'confirmed' AND {not_writeoff_sql('i')} "
    "GROUP BY i.currency"
)

# «За период» в карточке — последние 12 месяцев. Год, а не месяц: продают
# технику и партии товара, и за месяц у половины клиентов было бы «0».
PURCHASES_PERIOD_DAYS = 365


def purchases_base_total(by_currency: list[dict]) -> tuple[int, bool]:
    """Итог покупок в БАЗОВОЙ валюте (копейки) + признак «посчитано не всё».

    Валюта без заданного курса в итог не попадает (`convert_to_base` отдаёт
    None намеренно) — про это и говорит второй элемент: показать «1 200 USD»
    вместо «1 200 USD + сколько-то сумов» честнее, чем умножить на 1.0.
    """
    total = 0
    partial = False
    for row in by_currency:
        major = float(money.from_cents(int(row["amount_cents"] or 0)))
        conv = _db.convert_to_base(major, row["currency"])
        if conv is None:
            partial = True
            continue
        total += money.to_cents(conv)
    return int(total), partial


async def counterparty_purchases(counterparty_id, limit: int = 20) -> dict:
    """Покупки контрагента для карточки клиента.

    Отвечает на три вопроса владельца разом: сколько всего купил
    (`total_by_currency` за всё время + `period_by_currency` за последние
    `PURCHASES_PERIOD_DAYS` дней), когда отгружали (`recent` с номером, датой,
    валютой и суммой; `last_date` — последняя) и что берёт (`top_products`).
    """
    empty = {
        "top_products": [], "recent": [], "count": 0, "last_date": None,
        "total_by_currency": [], "period_by_currency": [],
        "total_base_cents": 0, "total_base_partial": False,
        "period_days": PURCHASES_PERIOD_DAYS,
    }
    try:
        cid = int(counterparty_id)
    except (TypeError, ValueError):
        return empty

    totals = await adb_core.fetch(_PURCHASE_TOTALS_SQL, cid)
    if not totals:
        return empty
    base = _base_currency()
    by_currency = sorted(
        (
            {"currency": (t["currency"] or base).upper(), "amount_cents": int(t["sum_cents"] or 0)}
            for t in totals
        ),
        key=lambda d: d["amount_cents"],
        reverse=True,
    )
    count = sum(int(t["cnt"] or 0) for t in totals)
    last_date = max((t["last_date"] or "") for t in totals) or None
    base_total, base_partial = purchases_base_total(by_currency)

    since = (datetime.now() - timedelta(days=PURCHASES_PERIOD_DAYS)).strftime("%Y-%m-%d")
    period = await adb_core.fetch(
        "SELECT i.currency AS currency, SUM(i.total_amount_cents) AS sum_cents, COUNT(*) AS cnt "
        "FROM invoices i WHERE i.counterparty_id = $1 AND i.type = 'outgoing' "
        f"AND i.status = 'confirmed' AND {not_writeoff_sql('i')} AND i.invoice_date >= $2 "
        "GROUP BY i.currency",
        cid, since,
    )
    period_by_currency = sorted(
        (
            {"currency": (p["currency"] or base).upper(), "amount_cents": int(p["sum_cents"] or 0)}
            for p in period
        ),
        key=lambda d: d["amount_cents"],
        reverse=True,
    )

    rows = await adb_core.fetch(
        "SELECT i.id, i.invoice_number, i.invoice_date, i.currency, i.total_amount_cents "
        "FROM invoices i WHERE i.counterparty_id = $1 AND i.type = 'outgoing' "
        f"AND i.status = 'confirmed' AND {not_writeoff_sql('i')} "
        "ORDER BY i.invoice_date DESC, i.id DESC LIMIT $2",
        cid,
        max(1, min(int(limit or 20), 200)),
    )

    ids = [int(r["id"]) for r in rows]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(ids)))
    positions = await adb_core.fetch(
        f"SELECT ii.product_id, p.name, ii.quantity, ii.price_cents "
        f"FROM invoice_items ii JOIN products p ON p.id = ii.product_id "
        f"WHERE ii.invoice_id IN ({placeholders})",
        *ids,
    ) if ids else []
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
                "currency": (r["currency"] or base).upper(),
                "sum_cents": int(r["total_amount_cents"] or 0),
            }
            for r in rows
        ],
        "count": count,
        "last_date": last_date,
        "total_by_currency": by_currency,
        "period_by_currency": period_by_currency,
        "total_base_cents": base_total,
        "total_base_partial": base_partial,
        "period_days": PURCHASES_PERIOD_DAYS,
    }


# ─── Подсказка «что предложить» в «Выбор товара» (D1 продуктового аудита) ────

# Сколько product_id отдаём подсказкой — экран всё равно рисует не более
# ~50 строк за раз («Показать ещё»), большего просто не покажут.
_PICKER_HINT_LIMIT = 30
# Окно «часто заказываемое» у менеджера — сознательно грубо (последний месяц),
# как и разрешает продуктовый аудит: не отчёт, а намёк порядка в списке.
_PICKER_HINT_WINDOW_DAYS = 30

# Заказ без реального намерения купить (отклонён/отменён) в подсказку не
# попадает — иначе «часто заказываемое» подсовывало бы то, что уже
# отбраковали.
_PICKER_HINT_EXCLUDED_STATUSES = ("cancelled", "rejected")


async def picker_hints(user_id: int, agent_id: str | None) -> dict:
    """Порядок подсказки для `openProductPicker`: → {kind, label, product_ids}.

    `kind`:
      - `client_recent` — у контрагента `agent_id` есть свои позиции заказов
        (`order_item_products`), `product_ids` — по давности ПОСЛЕДНЕЙ
        покупки, новые сверху;
      - `manager_frequent` — нет `agent_id` ИЛИ у клиента ещё нет истории
        (первый заказ): собственные позиции менеджера `user_id` за последние
        `_PICKER_HINT_WINDOW_DAYS` дней, по частоте (при равной — тоже по
        давности);
      - `none` — истории нет вовсе (новый менеджер/новый клиент без заказов).

    Список НЕ ограничивает выбор — фронт им только переставляет/секционирует
    ПОЛНЫЙ алфавитный каталог, поэтому здесь достаточно id без имён/остатков.
    """
    if agent_id:
        try:
            aid = str(int(str(agent_id).strip()))
        except (TypeError, ValueError):
            aid = None
        if aid:
            args: list = [aid, *_PICKER_HINT_EXCLUDED_STATUSES, _PICKER_HINT_LIMIT]
            placeholders = ", ".join(f"${i + 2}" for i in range(len(_PICKER_HINT_EXCLUDED_STATUSES)))
            rows = await adb_core.fetch(
                "SELECT op.product_id AS product_id, MAX(op.created_at) AS last_at "
                "FROM order_item_products op JOIN orders o ON o.id = op.order_id "
                f"WHERE o.agent_id = $1 AND o.status NOT IN ({placeholders}) "
                f"GROUP BY op.product_id ORDER BY last_at DESC LIMIT ${len(args)}",
                *args,
            )
            if rows:
                return {
                    "kind": "client_recent",
                    "label": "Недавно у этого клиента",
                    "product_ids": [int(r["product_id"]) for r in rows],
                }

    cutoff = (datetime.now() - timedelta(days=_PICKER_HINT_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    args = [int(user_id), cutoff, *_PICKER_HINT_EXCLUDED_STATUSES, _PICKER_HINT_LIMIT]
    placeholders = ", ".join(f"${i + 3}" for i in range(len(_PICKER_HINT_EXCLUDED_STATUSES)))
    rows = await adb_core.fetch(
        "SELECT op.product_id AS product_id, COUNT(*) AS cnt, MAX(op.created_at) AS last_at "
        "FROM order_item_products op JOIN orders o ON o.id = op.order_id "
        f"WHERE o.user_id = $1 AND o.created_at >= $2 AND o.status NOT IN ({placeholders}) "
        f"GROUP BY op.product_id ORDER BY cnt DESC, last_at DESC LIMIT ${len(args)}",
        *args,
    )
    if not rows:
        return {"kind": "none", "label": "", "product_ids": []}
    return {
        "kind": "manager_frequent",
        "label": "Часто заказываемое",
        "product_ids": [int(r["product_id"]) for r in rows],
    }
