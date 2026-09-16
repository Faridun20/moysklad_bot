"""
Списание с причиной и инвентаризация (пересчёт склада).

Зачем модуль: до него остаток менялся только «по документу продажи» — приход,
отгрузка, приёмка контейнера, возврат. Ответа на «пересчитали, не сходится» и
«разбилось/пропало» не было вовсе, и расхождение либо оставалось в складе
навсегда, либо его выправляли фиктивной накладной без причины.

Решения, определяющие модуль:

* **Товар двигает ОБЫЧНАЯ накладная склада.** Списание — расходная,
  излишек — приходная, обе через `warehouse.create_invoice_in`. Своего
  движения остатка здесь нет ни одной строки: `_apply_stock_delta` живёт в
  одном месте, вместе с ним живут блокировки `FOR UPDATE`, схлопывание
  повторов, отказ «остаток не уходит в минус» и хук себестоимости. То же
  решение, что у приёмки контейнера («отдельного вида документа нет, иначе
  остаток начал бы зависеть от того, каким путём товар приехал»), и оно же
  держит инвариант сценариев «остаток = приходы − расходы по действующим
  накладным» (`tests/scenarios/invariants.stock_matches_invoices`).
* **Причина — не комментарий накладной, а поле.** `stock_writeoffs` —
  таблица-sidecar к накладной: причина, фото, сессия пересчёта, себестоимость.
  Быстрые причины (`QUICK_REASONS`) — подсказка, а не справочник: на площадке
  пишут «бой», «порча», «недостача», но список закрытым быть не должен.
* **Списание — не продажа.** Цена позиций 0, и накладная списания ИСКЛЮЧЕНА
  из выручки и прибыли (`warehouse._shipments_where`,
  `costing.period_report`): иначе отчёт продаж получил бы отгрузки на ноль
  сумм, средний чек поехал бы, а прибыль показала бы убыточную «сделку».
  Себестоимость ушедшего при этом считается штатным FIFO — она и есть цена
  потери — и кладётся в `stock_writeoffs.cost_cents`.
* **Дельту пересчёта считаем в момент ПРОВЕДЕНИЯ**, от живого остатка под
  блокировкой. Человек считает склад полчаса; если запомнить «было столько» в
  момент ввода, параллельная отгрузка превратится в недостачу, а приход — в
  излишек. Введённое количество — факт («на полке 7»), остальное выводится.
* **Пересчёт применяется целиком или никак.** Все строки одной сессии едут в
  ОДНОЙ транзакции: полупроведённая инвентаризация — это склад, про который
  никто не знает, посчитан он или нет.
* **Сторно — отмена той же накладной** (`warehouse.cancel_invoice_in`), а не
  «обратное списание»: обратное движение оставило бы в истории два документа
  на один факт, а отмена честно говорит «этого не было». Окно — сутки
  (`VOID_WINDOW_HOURS`, как у правки приёмки контейнера), дальше только
  руководство.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from services import adb_core
from services import database as _db
from services import warehouse

# Классы ошибок берём ЧЕРЕЗ модуль (`warehouse.InvoiceError`), а не именем в
# импорте: тестовая фикстура делает `importlib.reload(warehouse)`, после него
# имя, скопированное при импорте, указывает на СТАРЫЙ класс — и `except` мимо
# него пропускает отказ «не хватает остатка» наружу пятисоткой. Тот же приём,
# что у `_db.USE_POSTGRES` в самом складе.

logger = logging.getLogger(__name__)

# Быстрые причины для формы. Не справочник и не CHECK: свободный ввод остаётся
# законным — причина, которой нет в списке, случается чаще, чем кажется.
#
# Обычными словами, а не складским жаргоном: владелец читает эту ленту сам и
# спросил прямо — «что значит „бой“? „пересортица“?». Причина уезжает в журнал
# и в отчёт о потерях, её будут перечитывать через полгода; «разбили» понятно
# и тогда, «бой» — только тому, кто вырос на складе.
QUICK_REASONS: tuple[str, ...] = (
    "разбили",
    "испортился",
    "не хватает на складе",
    "привезли не тот товар",
    "истёк срок годности",
)

# Причина строк, созданных проведением пересчёта. Одна на обе стороны: в ленте
# видно, что запись родилась из пересчёта, а не из решения человека.
COUNT_REASON = "пересчёт на складе"

KINDS = ("writeoff", "surplus")
COUNT_STATUSES = ("open", "applied", "cancelled")

# Сколько часов автор может сторнировать своё списание. Ровно как окно правки
# приёмки контейнера (`containers.EDIT_WINDOW_HOURS`): ошибку замечают в тот же
# день, а недельной давности запись — уже история, и трогает её руководство.
VOID_WINDOW_HOURS = 24

REASON_MAX = 300

# Потолок строк в одной сессии пересчёта. Причина та же, что у MAX_POSITIONS
# накладной: каждая строка — это строка `stock` под FOR UPDATE.
MAX_COUNT_LINES = warehouse.MAX_POSITIONS


class InventoryError(Exception):
    """Операция не может быть проведена. `code` — для WebApp, `message` — человеку."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def clean_reason(raw: Any) -> str:
    """Причина обязательна и непустая: списание без причины — это дыра в
    остатке, о которой через месяц никто ничего не скажет."""
    text = " ".join(str(raw or "").split())[:REASON_MAX]
    if not text:
        raise InventoryError("reason_required", "Укажите причину списания")
    return text


def _qty(raw: Any, *, field: str = "количество") -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InventoryError("bad_quantity", f"Введите {field} числом, например 2 или 1,5")
    if value != value or value in (float("inf"), float("-inf")):
        raise InventoryError("bad_quantity", f"Введите {field} числом, например 2 или 1,5")
    return value


def _stale_cutoff() -> str:
    """Граница окна сторно строкой local-TZ — как `created_at`.

    Сравнение в SQL с `NOW()`/`datetime('now')` было бы сравнением разных
    зон (CLAUDE.md, «Time»), поэтому порог считаем в Python и передаём
    параметром.
    """
    return (datetime.now() - timedelta(hours=VOID_WINDOW_HOURS)).strftime("%Y-%m-%d %H:%M:%S")


# ─── Списание / излишек ───────────────────────────────────────────────────────


async def _cost_of_invoice(txn, invoice_id: int) -> int | None:
    """Себестоимость списанного — сумма фиксаций FIFO по этой накладной.

    Учёт выключен — фиксаций нет и вернётся None: базовое списание обязано
    работать и без бухгалтерии, просто без цифры потери.
    """
    row = await txn.fetchrow(
        "SELECT COUNT(*) AS n, SUM(cost_base_cents) AS cost FROM sale_costs WHERE invoice_id = $1",
        int(invoice_id),
    )
    if row is None or not int(row["n"] or 0) or row["cost"] is None:
        return None
    return int(row["cost"])


async def create_writeoff_in(
    txn,
    *,
    warehouse_id: int,
    items: list[dict],
    reason: str,
    kind: str = "writeoff",
    photo_file_id: str | None = None,
    count_id: int | None = None,
    created_by: int | None = None,
) -> dict:
    """Провести списание (или излишек) ВНУТРИ уже открытой транзакции.

    Бросает `warehouse.InvoiceError`/`InventoryError` — как сам склад:
    вызывающий не должен иметь возможности закоммитить «не ок» словарём.
    """
    if kind not in KINDS:
        raise InventoryError("bad_kind", "Неизвестный вид записи — обновите приложение")
    text = clean_reason(reason)
    positions = [
        {
            "product_id": it.get("product_id"),
            "quantity": it.get("quantity"),
            # Списание — не продажа: цена 0. Расход без цены накладная не
            # принимает вовсе (`price_required`), а выдумывать цену значит
            # подмешать в выручку документ, по которому никто не платил.
            "price_cents": 0 if kind == "writeoff" else None,
        }
        for it in items
    ]
    invoice = await warehouse.create_invoice_in(
        txn,
        invoice_type="outgoing" if kind == "writeoff" else "incoming",
        warehouse_id=warehouse_id,
        items=positions,
        currency=_base_currency(),
        comment=("Списание: " if kind == "writeoff" else "Излишек: ") + text,
        created_by=created_by,
    )
    invoice_id = int(invoice["invoice_id"])
    cost_cents = await _cost_of_invoice(txn, invoice_id) if kind == "writeoff" else None

    sql = (
        "INSERT INTO stock_writeoffs (kind, invoice_id, warehouse_id, reason, photo_file_id, "
        "count_id, cost_cents, created_by, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)"
    )
    values = (
        kind,
        invoice_id,
        int(warehouse_id),
        text,
        (str(photo_file_id)[:255] if photo_file_id else None),
        int(count_id) if count_id else None,
        cost_cents,
        created_by,
        _db.now_str(),
    )
    if _db.USE_POSTGRES:
        writeoff_id = await txn.fetchval(sql + " RETURNING id", *values)
    else:
        await txn.execute(sql, *values)
        writeoff_id = await txn.fetchval("SELECT last_insert_rowid()")

    return {
        "ok": True,
        "writeoff_id": int(writeoff_id),
        "kind": kind,
        "invoice_id": invoice_id,
        "invoice_number": invoice["invoice_number"],
        "reason": text,
        "cost_cents": cost_cents,
        "positions": invoice["positions"],
    }


def _base_currency() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


async def create_writeoff(
    *,
    warehouse_id: int | None = None,
    items: list[dict],
    reason: str,
    photo_file_id: str | None = None,
    created_by: int | None = None,
) -> dict:
    """Списать товар с причиной. Одна транзакция: накладная, остаток, запись.

    Возвращает `{"ok": True, …}` либо `{"ok": False, "code", "reason"}` —
    во втором случае не изменилось ничего, включая счётчик номеров накладных.
    """
    wid = int(warehouse_id) if warehouse_id else await warehouse.default_warehouse_id()
    try:
        async with adb_core.transaction() as txn:
            return await create_writeoff_in(
                txn,
                warehouse_id=wid,
                items=items,
                reason=reason,
                photo_file_id=photo_file_id,
                created_by=created_by,
            )
    except (warehouse.InvoiceError, InventoryError) as e:
        logger.info("Списание не проведено (%s): %s", e.code, e.message)
        return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}


async def void_writeoff(writeoff_id: int, *, user_id: int, is_boss: bool) -> dict:
    """Сторнировать списание: отмена накладной вернёт товар на остаток.

    Менеджеру — только своё и только в окне суток: запись недельной давности
    уже вошла в отчёты, и правит её тот, кто за них отвечает. Руководству —
    всегда (та же логика, что у отмены накладной).
    """
    async with adb_core.transaction() as txn:
        row = await txn.fetchrow(
            "SELECT id, kind, invoice_id, created_by, created_at, cancelled_at "
            "FROM stock_writeoffs WHERE id = $1",
            int(writeoff_id),
        )
        if row is None:
            return {"ok": False, "code": "not_found", "reason": "Списание не найдено — обновите список"}
        if row["cancelled_at"]:
            return {"ok": False, "code": "already_cancelled", "reason": "Это списание уже отменено"}
        if not is_boss:
            if row["created_by"] is None or int(row["created_by"]) != int(user_id):
                return {
                    "ok": False,
                    "code": "not_owner",
                    "reason": "Отменить чужое списание может только руководитель",
                }
            if str(row["created_at"] or "") < _stale_cutoff():
                return {
                    "ok": False,
                    "code": "window_closed",
                    "reason": (
                        f"Отменить своё списание можно в течение {VOID_WINDOW_HOURS} часов после записи. "
                        "Позже это делает руководитель."
                    ),
                }
        try:
            if row["invoice_id"] is not None:
                await warehouse.cancel_invoice_in(txn, int(row["invoice_id"]), cancelled_by=user_id)
        except warehouse.InvoiceError as e:
            # Отмена прихода-излишка может упереться в нехватку (товар успели
            # отгрузить) — это ответ, а не сбой: пишем причину как есть.
            return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}
        await txn.execute(
            "UPDATE stock_writeoffs SET cancelled_by = $1, cancelled_at = $2 WHERE id = $3",
            int(user_id),
            _db.now_str(),
            int(writeoff_id),
        )
    logger.info("Списание #%s сторнировано пользователем %s", writeoff_id, user_id)
    return {"ok": True, "writeoff_id": int(writeoff_id)}


async def list_writeoffs(
    *, limit: int = 50, offset: int = 0, count_id: int | None = None
) -> list[dict]:
    """Лента списаний и излишков, новые сверху, с позициями накладной."""
    args: list[Any] = []
    where = ""
    if count_id is not None:
        args.append(int(count_id))
        where = f"WHERE w.count_id = ${len(args)} "
    args.extend([max(1, min(int(limit or 50), 200)), max(0, int(offset or 0))])
    rows = await adb_core.fetch(
        "SELECT w.id, w.kind, w.invoice_id, w.reason, w.photo_file_id, w.count_id, "
        "       w.cost_cents, w.created_by, w.created_at, w.cancelled_at, w.cancelled_by, "
        "       i.invoice_number, i.invoice_date, i.status AS invoice_status, "
        "       u.full_name AS author_name "
        "FROM stock_writeoffs w "
        "LEFT JOIN invoices i ON i.id = w.invoice_id "
        "LEFT JOIN user_roles u ON u.user_id = w.created_by "
        f"{where}"
        f"ORDER BY w.id DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}",
        *args,
    )
    if not rows:
        return []
    return await _attach_positions(rows)


async def _attach_positions(rows: list[dict]) -> list[dict]:
    """Позиции всех записей ленты — ОДНИМ запросом.

    По запросу на строку это классический N+1: лента отдаёт до двух сотен
    записей (`tests/perf/test_query_counts.py` ловит именно рост).
    """
    ids = [int(r["invoice_id"]) for r in rows if r["invoice_id"] is not None]
    items: dict[int, list[dict]] = {}
    if ids:
        placeholders = ", ".join(f"${i + 1}" for i in range(len(ids)))
        for pos in await adb_core.fetch(
            "SELECT ii.invoice_id, ii.product_id, ii.quantity, p.name, p.unit "
            f"FROM invoice_items ii JOIN products p ON p.id = ii.product_id "
            f"WHERE ii.invoice_id IN ({placeholders}) ORDER BY ii.id",
            *ids,
        ):
            items.setdefault(int(pos["invoice_id"]), []).append(
                {
                    "product_id": int(pos["product_id"]),
                    "name": pos["name"],
                    "unit": pos["unit"] or "шт",
                    "quantity": float(pos["quantity"] or 0),
                }
            )
    out = []
    for r in rows:
        out.append(
            {
                "id": int(r["id"]),
                "kind": r["kind"] or "writeoff",
                "reason": r["reason"],
                "count_id": int(r["count_id"]) if r["count_id"] is not None else None,
                "cost_cents": int(r["cost_cents"]) if r["cost_cents"] is not None else None,
                "photo_file_id": r["photo_file_id"],
                "created_by": int(r["created_by"]) if r["created_by"] is not None else None,
                "author_name": r["author_name"] or "",
                "created_at": r["created_at"],
                "cancelled_at": r["cancelled_at"],
                "invoice_id": int(r["invoice_id"]) if r["invoice_id"] is not None else None,
                "invoice_number": r["invoice_number"],
                "invoice_date": r["invoice_date"],
                "items": items.get(int(r["invoice_id"]), []) if r["invoice_id"] is not None else [],
            }
        )
    return out


# ─── Инвентаризация ───────────────────────────────────────────────────────────


async def start_count(
    *, warehouse_id: int | None = None, note: str | None = None, started_by: int | None = None
) -> dict:
    """Открыть сессию пересчёта. Открытая сессия у склада только одна.

    Вторая параллельная сессия по тому же складу означала бы две правды об
    одном остатке: обе посчитали «до», обе применили дельту — и вторая списала
    бы то, что уже списала первая.
    """
    wid = int(warehouse_id) if warehouse_id else await warehouse.default_warehouse_id()
    stamp = _db.now_str()
    sql = (
        "INSERT INTO stock_counts (warehouse_id, status, note, started_by, started_at) "
        "VALUES ($1, 'open', $2, $3, $4)"
    )
    values = (wid, (str(note).strip()[:REASON_MAX] if note else None), started_by, stamp)
    async with adb_core.transaction() as txn:
        open_row = await txn.fetchrow(
            "SELECT id FROM stock_counts WHERE warehouse_id = $1 AND status = 'open' "
            "ORDER BY id LIMIT 1",
            wid,
        )
        if open_row is not None:
            return {
                "ok": False,
                "code": "already_open",
                "reason": "Пересчёт по этому складу уже идёт — продолжите его",
                "count_id": int(open_row["id"]),
            }
        if _db.USE_POSTGRES:
            count_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            count_id = await txn.fetchval("SELECT last_insert_rowid()")
    logger.info("Пересчёт #%s открыт пользователем %s", count_id, started_by)
    return {"ok": True, "count_id": int(count_id), "warehouse_id": wid}


async def _open_count(txn, count_id: int) -> dict:
    row = await txn.fetchrow(
        "SELECT id, warehouse_id, status, note, started_by FROM stock_counts WHERE id = $1",
        int(count_id),
    )
    if row is None:
        raise InventoryError("not_found", "Пересчёт не найден — обновите список")
    if row["status"] != "open":
        raise InventoryError(
            "count_closed",
            "Этот пересчёт уже оформлен или отменён — откройте новый",
        )
    return row


async def set_count_line(count_id: int, product_id: int, counted_qty: Any) -> dict:
    """Записать ФАКТ по товару («на полке 7»). Повтор правит строку.

    Отдаём вместе с текущим остатком и дельтой — но это подсказка для экрана,
    а не то, что будет применено: дельту пересчитает проведение.
    """
    qty = _qty(counted_qty, field="посчитанное количество")
    if qty < 0:
        raise InventoryError("bad_quantity", "Посчитанное количество не может быть меньше нуля — введите 0 или больше")
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        raise InventoryError("bad_product_id", "Выберите товар")

    async with adb_core.transaction() as txn:
        head = await _open_count(txn, count_id)
        product = await txn.fetchrow("SELECT id, name, unit FROM products WHERE id = $1", pid)
        if product is None:
            raise InventoryError("unknown_product", f"Товар #{pid} не найден в каталоге — выберите его заново")
        total = await txn.fetchval(
            "SELECT COUNT(*) FROM stock_count_lines WHERE count_id = $1", int(count_id)
        )
        existing = await txn.fetchrow(
            "SELECT id FROM stock_count_lines WHERE count_id = $1 AND product_id = $2",
            int(count_id),
            pid,
        )
        if existing is None and int(total or 0) >= MAX_COUNT_LINES:
            raise InventoryError(
                "too_many_lines",
                f"В одном пересчёте не больше {MAX_COUNT_LINES} позиций — разбейте на несколько",
            )
        expected = await txn.fetchval(
            "SELECT quantity FROM stock WHERE product_id = $1 AND warehouse_id = $2",
            pid,
            int(head["warehouse_id"]),
        )
        expected_f = float(expected or 0)
        stamp = _db.now_str()
        if existing is None:
            await txn.execute(
                "INSERT INTO stock_count_lines (count_id, product_id, counted_qty, expected_qty, "
                "created_at) VALUES ($1, $2, $3, $4, $5)",
                int(count_id),
                pid,
                qty,
                expected_f,
                stamp,
            )
        else:
            await txn.execute(
                "UPDATE stock_count_lines SET counted_qty = $1, expected_qty = $2, updated_at = $3 "
                "WHERE id = $4",
                qty,
                expected_f,
                stamp,
                int(existing["id"]),
            )
    return {
        "ok": True,
        "count_id": int(count_id),
        "product_id": pid,
        "name": product["name"],
        "unit": product["unit"] or "шт",
        "counted_qty": qty,
        "expected_qty": expected_f,
        "delta": qty - expected_f,
    }


async def remove_count_line(count_id: int, product_id: int) -> dict:
    """Убрать строку из пересчёта (ошиблись товаром)."""
    async with adb_core.transaction() as txn:
        await _open_count(txn, count_id)
        n = await txn.execute(
            "DELETE FROM stock_count_lines WHERE count_id = $1 AND product_id = $2",
            int(count_id),
            int(product_id),
        )
    return {"ok": True, "removed": int(n)}


def _count_summary(lines: list[dict]) -> dict:
    """Итог по строкам: сколько списать, сколько оприходовать, сколько сошлось."""
    short = [ln for ln in lines if ln["delta"] < 0]
    over = [ln for ln in lines if ln["delta"] > 0]
    return {
        "lines": len(lines),
        "short": len(short),
        "surplus": len(over),
        "match": len(lines) - len(short) - len(over),
        "short_qty": round(sum(-ln["delta"] for ln in short), 6),
        "surplus_qty": round(sum(ln["delta"] for ln in over), 6),
    }


async def count_card(count_id: int) -> dict | None:
    """Шапка пересчёта + строки с ЖИВЫМ остатком и дельтой — экран «перед тем,
    как применить». Ничего вслепую: человек видит список расхождений."""
    head = await adb_core.fetchrow(
        "SELECT c.id, c.warehouse_id, c.status, c.note, c.started_by, c.started_at, "
        "       c.finished_by, c.finished_at, u.full_name AS started_by_name "
        "FROM stock_counts c LEFT JOIN user_roles u ON u.user_id = c.started_by "
        "WHERE c.id = $1",
        int(count_id),
    )
    if head is None:
        return None
    rows = await adb_core.fetch(
        "SELECT l.product_id, l.counted_qty, l.created_at, l.updated_at, "
        "       p.name, p.unit, COALESCE(s.quantity, 0) AS stock_qty "
        "FROM stock_count_lines l "
        "JOIN products p ON p.id = l.product_id "
        "LEFT JOIN stock s ON s.product_id = l.product_id AND s.warehouse_id = $2 "
        f"WHERE l.count_id = $1 ORDER BY {adb_core.order_by_name('p.name')}, l.product_id",
        int(count_id),
        int(head["warehouse_id"]),
    )
    lines = []
    for r in rows:
        counted = float(r["counted_qty"] or 0)
        expected = float(r["stock_qty"] or 0)
        lines.append(
            {
                "product_id": int(r["product_id"]),
                "name": r["name"],
                "unit": r["unit"] or "шт",
                "counted_qty": counted,
                "expected_qty": expected,
                "delta": round(counted - expected, 6),
            }
        )
    return {
        "count_id": int(head["id"]),
        "warehouse_id": int(head["warehouse_id"]),
        "status": head["status"],
        "note": head["note"] or "",
        "started_by": int(head["started_by"]) if head["started_by"] is not None else None,
        "started_by_name": head["started_by_name"] or "",
        "started_at": head["started_at"],
        "finished_at": head["finished_at"],
        "lines": lines,
        "summary": _count_summary(lines),
    }


async def list_counts(limit: int = 20) -> list[dict]:
    """Сессии пересчёта, новые сверху, со сводкой расхождений."""
    rows = await adb_core.fetch(
        "SELECT c.id, c.status, c.note, c.started_at, c.finished_at, c.warehouse_id, "
        "       u.full_name AS started_by_name, "
        "       (SELECT COUNT(*) FROM stock_count_lines l WHERE l.count_id = c.id) AS lines "
        "FROM stock_counts c LEFT JOIN user_roles u ON u.user_id = c.started_by "
        "ORDER BY c.id DESC LIMIT $1",
        max(1, min(int(limit or 20), 100)),
    )
    return [
        {
            "count_id": int(r["id"]),
            "status": r["status"],
            "note": r["note"] or "",
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "started_by_name": r["started_by_name"] or "",
            "lines": int(r["lines"] or 0),
        }
        for r in rows
    ]


async def cancel_count(count_id: int, *, user_id: int | None = None) -> dict:
    """Закрыть сессию, ничего не применяя. Строки остаются — это история того,
    что считали, и удалять её вместе с решением «не применять» незачем."""
    async with adb_core.transaction() as txn:
        await _open_count(txn, count_id)
        await txn.execute(
            "UPDATE stock_counts SET status = 'cancelled', finished_by = $1, finished_at = $2 "
            "WHERE id = $3 AND status = 'open'",
            user_id,
            _db.now_str(),
            int(count_id),
        )
    return {"ok": True, "count_id": int(count_id)}


async def apply_count(
    count_id: int, *, user_id: int | None = None, idem_key: str | None = None
) -> dict:
    """Провести пересчёт: недостача — списанием, излишек — приходом. Всё разом.

    Дельта считается ЗДЕСЬ, от остатка под блокировкой строк `stock` (её берёт
    `warehouse.create_invoice_in`), а не от запомненного при вводе: между
    подсчётом и проведением склад живёт.

    Идемпотентность двойная: CAS по статусу сессии (`status = 'open'`) —
    второе проведение не найдёт открытой сессии — и ключ ручки, который
    пишется ЭТОЙ же транзакцией (`database.idem_store_in`).
    """
    async with adb_core.transaction() as txn:
        head = await _open_count(txn, count_id)
        wid = int(head["warehouse_id"])
        rows = await txn.fetch(
            "SELECT l.product_id, l.counted_qty, COALESCE(s.quantity, 0) AS stock_qty "
            "FROM stock_count_lines l "
            "LEFT JOIN stock s ON s.product_id = l.product_id AND s.warehouse_id = $2 "
            "WHERE l.count_id = $1 ORDER BY l.product_id",
            int(count_id),
            wid,
        )
        if not rows:
            raise InventoryError("empty_count", "В пересчёте нет ни одной позиции — впишите посчитанное количество хотя бы по одному товару")

        short: list[dict] = []
        surplus: list[dict] = []
        for r in rows:
            delta = round(float(r["counted_qty"] or 0) - float(r["stock_qty"] or 0), 6)
            if delta < 0:
                short.append({"product_id": int(r["product_id"]), "quantity": -delta})
            elif delta > 0:
                surplus.append({"product_id": int(r["product_id"]), "quantity": delta})

        result: dict[str, Any] = {
            "ok": True,
            "count_id": int(count_id),
            "writeoff": None,
            "surplus": None,
            "matched": len(rows) - len(short) - len(surplus),
        }
        if short:
            result["writeoff"] = await create_writeoff_in(
                txn,
                warehouse_id=wid,
                items=short,
                reason=COUNT_REASON,
                kind="writeoff",
                count_id=int(count_id),
                created_by=user_id,
            )
        if surplus:
            result["surplus"] = await create_writeoff_in(
                txn,
                warehouse_id=wid,
                items=surplus,
                reason=COUNT_REASON,
                kind="surplus",
                count_id=int(count_id),
                created_by=user_id,
            )
        # CAS: закрываем ТУ сессию, которая всё ещё открыта. Два одновременных
        # «Провести» — второй увидит 0 строк и получит отказ вместо второго
        # комплекта накладных.
        closed = await txn.execute(
            "UPDATE stock_counts SET status = 'applied', finished_by = $1, finished_at = $2 "
            "WHERE id = $3 AND status = 'open'",
            user_id,
            _db.now_str(),
            int(count_id),
        )
        if not closed:
            raise InventoryError("count_closed", "Этот пересчёт уже оформлен")
        await _db.idem_store_in(txn, idem_key, result)
    logger.info(
        "Пересчёт #%s проведён: списано позиций %d, оприходовано %d",
        count_id,
        len(short),
        len(surplus),
    )
    return result
