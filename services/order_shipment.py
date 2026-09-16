"""
Отгрузка заказа = расходная накладная локального склада.

Заменяет цепочку МойСклад «customerorder → demand»: раньше одобрение заявки
создавало в МС заказ покупателя (ради печатной формы) и отгрузку (ради
списания остатка). Оба документа теперь наши: остаток двигает
`warehouse.create_invoice`, печатную форму собирает `services.invoice_pdf`.

Решения, определяющие модуль:

* **Отгрузка — ОДИН документ.** Пары «заказ покупателя + отгрузка» здесь нет:
  она существовала потому, что в МС печатная форма висела на первом, а
  списание — на втором. У нас накладная и печатается, и двигает склад.
* **Идемпотентность — по `order_shipment.order_id`.** Повторное одобрение
  (два босса, ретрай, старая кнопка) не должно списать товар дважды. Прежде от
  этого спасал детерминированный syncId на стороне МойСклад; теперь ключ наш,
  и он же PRIMARY KEY.
* **Несопоставленная позиция не пропадает молча.** Строка, у которой нет
  карточки номенклатуры, в накладную не попадает и возвращается в `skipped` —
  списанный не полностью заказ это расхождение склада, и узнать о нём надо
  сразу, а не при инвентаризации.
* **Отказ не откатывает одобрение.** Заявка одобрена решением человека; если
  списать остаток не вышло (не хватило товара, позиции не сопоставлены),
  заказ помечается `order_shipment.failed_at` и попадает в дайджест «нужна
  доделка» — ровно так же, как раньше вёл себя провалившийся demand.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from services import adb_core, warehouse
from services.database import USE_POSTGRES, now_str

logger = logging.getLogger(__name__)

# Статусы, в которых заказ законно списывать со склада. Одобрение проводит
# накладную сразу, отгрузка кладовщиком (`shipped`) и оплата её не отменяют.
# Отменённый, отклонённый или возвращённый в черновик — нет: такой заказ
# «одобрить против отменить» успел закрыть, и списание увело бы товар по
# заказу, которого больше нет.
_SHIPPABLE_STATUSES = ("approved", "shipped", "paid", "partially_returned")

# Сколько минут одобренный заказ без накладной считается «ещё в пути», а не
# зависшим: между CAS одобрения и накладной — секунды, но на деплое процесс
# убивают ровно посередине, и такой заказ не оставляет даже failed_at.
STUCK_APPROVAL_MINUTES = 10


async def _lock_order_for_shipment(txn, order_id: int) -> str | None:
    """Взять замок отгрузки заказа и вернуть его статус (None — заказа нет).

    Один замок на ship_order и cancel_shipment: advisory по номеру заказа
    (сериализует сами документы) и `FOR UPDATE` строки заказа (отмена
    `UPDATE orders ... status='cancelled'` ждёт, пока списание не решится, и
    наоборот). Порядок одинаковый в обеих функциях — сначала advisory, потом
    строка, — поэтому взаимной блокировки нет. На SQLite то же даёт
    `BEGIN IMMEDIATE` транзакции.
    """
    if USE_POSTGRES:
        await txn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"ship:order:{order_id}")
        row = await txn.fetchrow("SELECT status FROM orders WHERE id = $1 FOR UPDATE", order_id)
    else:
        row = await txn.fetchrow("SELECT status FROM orders WHERE id = $1", order_id)
    return None if row is None else str(row["status"])


async def get_shipment(order_id: int) -> dict | None:
    """Строка отгрузки заказа: накладная, время, ошибка последней попытки."""
    return await adb_core.fetchrow(
        "SELECT * FROM order_shipment WHERE order_id = $1", int(order_id)
    )


async def _resolve_products(items: list[dict]) -> tuple[list[dict], list[str]]:
    """Позиции заказа → строки накладной. Второй элемент — что не сопоставили.

    Товар берём из привязки (`product_id`); если её нет — ищем ТОЧНОЕ
    совпадение по названию. Похожие имена не склеиваем: угадывание означает
    списание не той карточки.
    """
    unlinked = [it for it in items if not it.get("product_id")]
    by_name: dict[str, list[int]] = {}
    if unlinked:
        names = sorted({str(it.get("product_name") or "").strip().lower() for it in unlinked})
        names = [n for n in names if n]
        if names:
            placeholders = ", ".join(f"${i + 1}" for i in range(len(names)))
            rows = await adb_core.fetch(
                f"SELECT id, name FROM products WHERE lower(name) IN ({placeholders})", *names
            )
            for r in rows:
                by_name.setdefault(str(r["name"]).strip().lower(), []).append(int(r["id"]))

    positions: list[dict] = []
    skipped: list[str] = []
    for it in items:
        name = str(it.get("product_name") or "—")
        qty = float(it.get("quantity") or 0)
        if qty <= 0:
            continue
        pid = it.get("product_id")
        if not pid:
            candidates = by_name.get(name.strip().lower(), [])
            pid = candidates[0] if len(candidates) == 1 else None
        if not pid:
            skipped.append(name)
            continue
        positions.append(
            {
                "product_id": int(pid),
                "quantity": qty,
                "price_cents": int(it.get("price_cents") or 0),
            }
        )
    return positions, skipped


async def _remember_failure(order_id: int, error: str) -> None:
    stamp = now_str()
    updated = await adb_core.execute(
        "UPDATE order_shipment SET failed_at = $1, error = $2 WHERE order_id = $3",
        stamp,
        error[:500],
        int(order_id),
    )
    if not updated:
        await adb_core.execute(
            "INSERT INTO order_shipment (order_id, failed_at, error) VALUES ($1, $2, $3)",
            int(order_id),
            stamp,
            error[:500],
        )


async def ship_order(order: dict, items: list[dict], *, user_id: int | None = None) -> dict:
    """Списать заказ со склада расходной накладной.

    Возвращает `{ok, invoice_id, invoice_number, skipped}` либо
    `{ok: False, code, reason, skipped}`. Безопасно вызывать повторно: заказ,
    у которого накладная уже есть, возвращается с `already_shipped`.
    """
    order_id = int(order["id"])
    existing = await get_shipment(order_id)
    if existing and existing.get("invoice_id"):
        return {
            "ok": True,
            "invoice_id": int(existing["invoice_id"]),
            "already_shipped": True,
            "skipped": [],
        }

    positions, skipped = await _resolve_products(items)
    if not positions:
        reason = "Ни одна позиция заказа не связана с товаром из каталога — свяжите их и повторите"
        await _remember_failure(order_id, reason)
        return {"ok": False, "code": "no_positions", "reason": reason, "skipped": skipped}

    # Склад берём из выбора менеджера при создании заказа (`order_warehouse`),
    # если он есть; иначе — как раньше, по умолчанию. В однoскладском случае
    # строки в `order_warehouse` не бывает никогда, и ответ совпадает с
    # `default_warehouse_id()` — поведение не меняется ни на бит.
    warehouse_id = await warehouse.resolve_order_warehouse(order_id)
    # `agent_id` после перехода хранит id НАШЕГО контрагента (строкой — колонка
    # TEXT, см. backfill_local_identifiers). Нечисловое значение — legacy-uuid,
    # который backfill не сматчил: накладную всё равно проводим, просто без
    # контрагента, иначе отгрузка встанет из-за справочника.
    counterparty_id: int | None
    try:
        counterparty_id = int(str(order.get("agent_id") or "").strip())
    except (TypeError, ValueError):
        counterparty_id = None

    try:
        async with adb_core.transaction() as txn:
            # Идемпотентность — ПОД замком, а не только проверкой выше: два
            # одновременных вызова (два босса, ретрай на середине) оба видят
            # «накладной нет» и оба её проводят. Проверка выше остаётся как
            # дешёвый ранний выход, решает — эта.
            #
            # Статус — тоже под замком. `order` пришёл снимком до списания, а
            # между CAS одобрения и этой транзакцией босс мог нажать «Отменить»:
            # отмена не нашла накладной (её ещё нет), и без перепроверки товар
            # списался бы по уже отменённому заказу — навсегда.
            status = await _lock_order_for_shipment(txn, order_id)
            if status not in _SHIPPABLE_STATUSES:
                from services.order_workflow import _STATUS_RU

                human = (
                    "не найден"
                    if status is None
                    else f"уже «{_STATUS_RU.get(status, status)}»"
                )
                return {
                    "ok": False,
                    "code": "order_moved",
                    "reason": f"Заказ #{order_id} {human}",
                    "status": status,
                    "skipped": skipped,
                }
            locked = await txn.fetchrow(
                "SELECT invoice_id FROM order_shipment WHERE order_id = $1", order_id
            )
            if locked and locked.get("invoice_id"):
                return {
                    "ok": True,
                    "invoice_id": int(locked["invoice_id"]),
                    "already_shipped": True,
                    "skipped": [],
                }
            created = await warehouse.create_invoice_in(
                txn,
                invoice_type="outgoing",
                warehouse_id=warehouse_id,
                items=positions,
                counterparty_id=counterparty_id,
                currency=str(order.get("currency") or "USD"),
                comment=f"Заказ #{order_id}",
                created_by=user_id,
            )
            stamp = now_str()
            # `AND invoice_id IS NULL` — второй рубеж: строка с прежней
            # неудачей (failed_at) обновляется, строка с накладной — никогда.
            updated = await txn.execute(
                "UPDATE order_shipment SET invoice_id = $1, shipped_at = $2, "
                "failed_at = NULL, error = NULL "
                "WHERE order_id = $3 AND invoice_id IS NULL",
                int(created["invoice_id"]),
                stamp,
                order_id,
            )
            if not updated:
                await txn.execute(
                    "INSERT INTO order_shipment (order_id, invoice_id, shipped_at) "
                    "VALUES ($1, $2, $3)",
                    order_id,
                    int(created["invoice_id"]),
                    stamp,
                )
    except warehouse.InvoiceError as e:
        logger.warning("Заказ #%s не списан со склада (%s): %s", order_id, e.code, e.message)
        await _remember_failure(order_id, e.message)
        return {"ok": False, "code": e.code, "reason": e.message, "skipped": skipped}

    if skipped:
        # L1: накладная неполна относительно заказа — видно в логах и в ответе.
        logger.warning(
            "Заказ #%s отгружен без позиций (нет карточки): %s", order_id, skipped
        )
    logger.info(
        "Заказ #%s отгружен накладной %s (позиций %d)",
        order_id, created["invoice_number"], len(positions),
    )
    return {
        "ok": True,
        "invoice_id": int(created["invoice_id"]),
        "invoice_number": created["invoice_number"],
        "skipped": skipped,
    }


async def historical_cancel_refusal(order_id: int) -> str | None:
    """Текст отказа в отмене заказа, чья отгрузка — история МойСклад; None — можно.

    Отгрузку такого заказа склад не двигал: остаток приехал снимком, который
    её уже учитывает (`scripts/migrate_history_from_moysklad.py`). Отмена
    заказа откатывает накладную и вернула бы на склад товар, давно уехавший
    к клиенту. Признаки — любой из:
      * `ms_demand_id` — отгрузка проведена в МойСклад (так помечены и
        заказы из переноса, и заказы эпохи интеграции; у заказа с несколькими
        отгрузками МС вторая и далее в `order_shipment` не попадают вовсе —
        ловить их можно только по этому полю);
      * накладная отгрузки — историческая (`warehouse.historical_invoice_sql`).

    Исторический заказ БЕЗ отгрузки (заказ покупателя МС, так и не отгруженный)
    отменять можно: склад по нему не двигался ни там, ни здесь, а отмена —
    единственный способ снять его из резерва доступного остатка.
    """
    row = await adb_core.fetchrow(
        "SELECT o.ms_demand_id, "
        f"  (SELECT {warehouse.historical_invoice_sql('i')} FROM invoices i "
        "     WHERE i.id = s.invoice_id) AS historical_invoice "
        "FROM orders o LEFT JOIN order_shipment s ON s.order_id = o.id WHERE o.id = $1",
        int(order_id),
    )
    if row is None:
        return None
    if not (row["ms_demand_id"] or row["historical_invoice"]):
        return None
    return (
        f"Заказ #{order_id} отгружен ещё в МойСклад, и отменить его нельзя: остаток склада "
        "приехал снимком, который эту отгрузку уже учитывает, и отмена вернула бы на склад "
        "товар, давно уехавший к клиенту. Если клиент вернул товар — оформите возврат."
    )


async def cancel_shipment_locked(txn, order_id: int, *, user_id: int | None = None) -> dict:
    """Откатить отгрузку ВНУТРИ транзакции, которая уже держит замок отгрузки
    (`_lock_order_for_shipment`). Для отмены заказа: статус и возврат остатка
    обязаны коммититься вместе.

    Отказ склада возвращается словарём `{ok: False, ...}` ДО любой записи
    (все отказы `cancel_invoice_in` случаются до первой записи), и вызывающий
    сам решает — откатить свою транзакцию или нет.
    """
    row = await txn.fetchrow(
        "SELECT invoice_id FROM order_shipment WHERE order_id = $1", order_id
    )
    if not row or not row.get("invoice_id"):
        return {"ok": True, "skipped": "no-shipment"}
    invoice_id = int(row["invoice_id"])
    try:
        await warehouse.cancel_invoice_in(txn, invoice_id, user_id)
    except warehouse.InvoiceError as e:
        if e.code != "already_cancelled":
            logger.info(
                "Отмена накладной #%s по заказу #%s отклонена (%s): %s",
                invoice_id, order_id, e.code, e.message,
            )
            return {"ok": False, "code": e.code, "reason": e.message, "details": e.details}
    await txn.execute(
        "UPDATE order_shipment SET invoice_id = NULL, shipped_at = NULL WHERE order_id = $1",
        order_id,
    )
    return {"ok": True, "invoice_id": invoice_id}


async def cancel_shipment(order_id: int, *, user_id: int | None = None) -> dict:
    """Откатить отгрузку заказа: отменить накладную, вернуть остаток.

    Если накладной нет — делать нечего, это не ошибка: заказ могли отменить до
    одобрения. Отмена ЗАКАЗА зовёт `cancel_shipment_locked` внутри своей
    транзакции (database.cancel_order).
    """
    order_id = int(order_id)
    # Под тем же замком, что и ship_order: иначе отмена читает «накладной нет»,
    # пока параллельное одобрение её проводит, и списание остаётся висеть на
    # отменённом заказе. Накладная и строка отгрузки меняются одной транзакцией.
    async with adb_core.transaction() as txn:
        await _lock_order_for_shipment(txn, order_id)
        res = await cancel_shipment_locked(txn, order_id, user_id=user_id)
    if res.get("ok") and res.get("invoice_id"):
        logger.info("Заказ #%s: отгрузка откачена, остаток возвращён", order_id)
    return res


async def list_failed(limit: int = 50) -> list[dict]:
    """Заказы, которые одобрили, но со склада не списали. Для дайджеста.

    Два источника. (1) Отгрузка пробовала и упала — `failed_at`. (2) Одобрен
    дольше `STUCK_APPROVAL_MINUTES` назад, а строки отгрузки с накладной нет
    вовсе: процесс умер между одобрением и списанием (деплой, OOM), и
    `failed_at` записать было некому. Без второго такой заказ выглядел
    одобренным, товар по нему не списывался, и никто об этом не узнавал.
    Заказы эпохи МойСклад (`ms_*_id`) списывались там — их не трогаем.
    Отменённый и отклонённый заказ списывать уже не нужно.
    """
    cap = max(1, min(int(limit or 50), 500))
    failed = await adb_core.fetch(
        "SELECT s.order_id, s.failed_at, s.error, o.status, o.full_name, o.agent_name "
        "FROM order_shipment s JOIN orders o ON o.id = s.order_id "
        "WHERE s.failed_at IS NOT NULL AND s.invoice_id IS NULL "
        "AND o.status NOT IN ('cancelled', 'rejected', 'draft', 'returned') "
        "ORDER BY s.failed_at DESC LIMIT $1",
        cap,
    )
    # Порог — в Python: approved_at пишется локальным now_str(), и сравнение
    # со временем БД разъехалось бы по часовому поясу (см. CLAUDE.md, Time).
    cutoff = (datetime.now() - timedelta(minutes=STUCK_APPROVAL_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    stuck = await adb_core.fetch(
        "SELECT o.id AS order_id, MAX(r.approved_at) AS failed_at, o.status, "
        "       o.full_name, o.agent_name "
        "FROM orders o JOIN shipment_requests r "
        "  ON r.order_id = o.id AND r.status = 'approved' "
        "WHERE o.status = 'approved' "
        "AND (o.ms_customerorder_id IS NULL OR o.ms_customerorder_id = '') "
        "AND (o.ms_demand_id IS NULL OR o.ms_demand_id = '') "
        "AND NOT EXISTS (SELECT 1 FROM order_shipment s WHERE s.order_id = o.id "
        "                AND (s.invoice_id IS NOT NULL OR s.failed_at IS NOT NULL)) "
        "AND EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.id) "
        "GROUP BY o.id, o.status, o.full_name, o.agent_name "
        "HAVING MAX(r.approved_at) < $1 "
        "ORDER BY MAX(r.approved_at) DESC LIMIT $2",
        cutoff,
        cap,
    )
    for r in stuck:
        r["error"] = "Заказ одобрен, но отгрузка не оформлена — работа прервалась. Отметьте отгрузку заново"
    rows = list(failed) + list(stuck)
    rows.sort(key=lambda r: str(r.get("failed_at") or ""), reverse=True)
    return rows[:cap]
