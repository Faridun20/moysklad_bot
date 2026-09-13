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

from services import adb_core, warehouse
from services.database import USE_POSTGRES, now_str

logger = logging.getLogger(__name__)


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
        reason = "Ни одна позиция заказа не сопоставлена с номенклатурой"
        await _remember_failure(order_id, reason)
        return {"ok": False, "code": "no_positions", "reason": reason, "skipped": skipped}

    warehouse_id = await warehouse.default_warehouse_id()
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
            if USE_POSTGRES:
                await txn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", f"ship:order:{order_id}"
                )
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


async def cancel_shipment(order_id: int, *, user_id: int | None = None) -> dict:
    """Откатить отгрузку заказа: отменить накладную, вернуть остаток.

    Зовётся при отмене заказа. Если накладной нет — делать нечего, это не
    ошибка: заказ могли отменить до одобрения.
    """
    row = await get_shipment(int(order_id))
    if not row or not row.get("invoice_id"):
        return {"ok": True, "skipped": "no-shipment"}

    res = await warehouse.cancel_invoice(int(row["invoice_id"]), user_id)
    if not res.get("ok") and res.get("code") != "already_cancelled":
        return res
    await adb_core.execute(
        "UPDATE order_shipment SET invoice_id = NULL, shipped_at = NULL WHERE order_id = $1",
        int(order_id),
    )
    logger.info("Заказ #%s: отгрузка откачена, остаток возвращён", order_id)
    return {"ok": True, "invoice_id": int(row["invoice_id"])}


async def list_failed(limit: int = 50) -> list[dict]:
    """Заказы, которые одобрили, но со склада не списали. Для дайджеста."""
    return await adb_core.fetch(
        "SELECT s.order_id, s.failed_at, s.error, o.status, o.full_name, o.agent_name "
        "FROM order_shipment s JOIN orders o ON o.id = s.order_id "
        "WHERE s.failed_at IS NOT NULL AND s.invoice_id IS NULL "
        "ORDER BY s.failed_at DESC LIMIT $1",
        max(1, min(int(limit or 50), 500)),
    )
