"""
Приёмка прибывшего контейнера на ЛОКАЛЬНЫЙ склад.

Заменяет `ms_supply`: раньше посчитанный контейнер уезжал документом
«Приёмка» в МойСклад, теперь он становится приходной накладной у нас
(`services.warehouse`), и остаток двигается в нашей же таблице `stock`.

Три решения, определяющие модуль:

* **Приход — обычная incoming-накладная.** Отдельного вида документа для
  контейнера нет: движение склада должно считаться одним кодом, иначе
  остаток начинает зависеть от того, каким путём товар приехал.
* **Цены не спрашиваем.** Человек в этот момент считает коробки, а не
  деньги: позиции уходят без цены, закупочную вписывают позже. Пустая цена
  в приходе лучше, чем выдуманная. (`warehouse` требует цену только для
  расхода — из неё считается сумма в PDF клиенту.) Цены закупки вписывает
  руководство отдельно (`services/costing.py`), и при включённом учёте они
  доезжают в накладную и партии той же транзакцией приёмки.
* **Поставщик необязателен.** МойСклад требовал контрагента для «Приёмки»,
  и его приходилось задавать заранее. Локальной накладной он не нужен, и
  выдумывать блокировку, которой больше нет, незачем: поле осталось — его
  заполняют, когда знают, — но приёмку оно не держит.

Повторная приёмка — ОТМЕНА прежней накладной и создание новой, одной
транзакцией (`warehouse.cancel_invoice_in` + `create_invoice_in`).
Количества правятся сутки после приёмки (`containers.EDIT_WINDOW_HOURS`), и
остаток обязан ехать за ними. Дельта-накладной здесь нет намеренно: история
движений должна читаться как «приняли столько», а не как «приняли столько,
потом ещё минус два» — и отмена сама упрётся в нехватку, если товар уже
отгрузили, а это ровно тот случай, когда молча уменьшать приход нельзя.
"""

from __future__ import annotations

import json
import logging

from services import adb_core, warehouse
from services.database import USE_POSTGRES, now_str

logger = logging.getLogger(__name__)


def normalize_name(raw: str | None) -> str:
    """Название к сравнимому виду: регистр и лишние пробелы не должны мешать."""
    return " ".join(str(raw or "").split()).casefold()


async def _products_by_name(names: list[str]) -> dict[str, list[dict]]:
    """Карточки номенклатуры под нормализованными именами. {norm: [rows]}.

    Одним запросом на весь состав, а не по позиции: контейнер на полсотни
    строк иначе дал бы полсотни обращений к БД внутри одной приёмки.

    В `IN` кладём и «как ввели» (в нижнем регистре), и нормализованный
    вариант: в каталоге, приехавшем из МойСклад, встречаются двойные пробелы
    внутри названия, и по одному только нормализованному ключу такая карточка
    не нашлась бы.
    """
    variants: set[str] = set()
    for n in names:
        variants.add(str(n or "").strip().lower())
        variants.add(normalize_name(n))
    variants.discard("")
    if not variants:
        return {}
    ordered = sorted(variants)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(ordered)))
    rows = await adb_core.fetch(
        f"SELECT id, name, unit FROM products WHERE lower(name) IN ({placeholders})",
        *ordered,
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(normalize_name(r["name"]), []).append(dict(r))
    return out


async def match_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разложить состав на «нашли в номенклатуре» и «нет такого товара».

    Товар берётся из ПРИВЯЗКИ позиции (`product_id`), если она есть: человек
    выбрал карточку в каталоге, и переспрашивать у поиска, что он имел в виду,
    незачем.

    Совпадение по названию осталось запасным путём — для позиций, заведённых
    до появления выбора, и для тех, что вбили руками. Оно требуется ТОЧНОЕ по
    нормализованному имени: «Кабель PV 0.6» и «Кабель PV 0.6 чёрный» — разные
    товары, и угадывание здесь означает приход не на ту карточку.
    """
    arrived = [it for it in items if float(it.get("arrived_qty") or 0) > 0]
    # Позиции без факта не ошибка сопоставления: оприходовать нечего.
    unlinked = [it for it in arrived if not it.get("product_id")]
    by_name = await _products_by_name([str(it.get("name") or "") for it in unlinked])

    matched: list[dict] = []
    unmatched: list[dict] = []
    for it in arrived:
        qty = float(it.get("arrived_qty") or 0)
        name = str(it.get("name") or "")
        linked = it.get("product_id")
        if linked:
            matched.append({**it, "product_id": int(linked), "quantity": qty})
            continue
        candidates = by_name.get(normalize_name(name), [])
        if len(candidates) == 1:
            matched.append({**it, "product_id": int(candidates[0]["id"]), "quantity": qty})
        else:
            unmatched.append(
                {
                    "item_id": it.get("id"),
                    "name": name,
                    "quantity": qty,
                    "reason": (
                        "не найден в номенклатуре" if not candidates else "несколько совпадений"
                    ),
                }
            )
    return matched, unmatched


async def create_product(name: str, *, unit: str = "шт") -> dict:
    """Завести карточку товара в локальной номенклатуре по названию позиции.

    Заводит ЧЕЛОВЕК кнопкой, а не приёмка сама: автосоздание превратило бы
    каждую опечатку в новый товар справочника, и остаток разъехался бы по двум
    почти одинаковым карточкам.

    Дубликат проверяем по нормализованному имени — «есть ли такой товар» и для
    человека решается сравнением названий.
    """
    clean = " ".join(str(name or "").split())[:255]
    if not clean:
        return {"ok": False, "error": "Название товара обязательно"}

    existing = (await _products_by_name([clean])).get(normalize_name(clean)) or []
    if existing:
        return {
            "ok": True,
            "product_id": int(existing[0]["id"]),
            "name": existing[0]["name"],
            "existed": True,
        }

    clean_unit = (str(unit or "шт").strip() or "шт")[:16]
    async with adb_core.transaction() as txn:
        # Второй такой же товар мог появиться, пока мы проверяли: карточку
        # заводят кнопкой, а кнопку можно нажать дважды. Перепроверяем внутри
        # транзакции — UNIQUE на имени нет и быть не должно (тёзки в каталоге
        # законны), поэтому гонку ловим здесь.
        # Тёзку ловим замком, а не только SELECT'ом: на Postgres (READ
        # COMMITTED) две одновременные транзакции обе не видят дубля и обе
        # вставляют — проверка внутри транзакции сама по себе гонку не
        # закрывает. Advisory-lock по нормализованному имени сериализует
        # именно тёзок, а не все вставки подряд. UNIQUE на имени по-прежнему
        # нет и быть не должно: тёзки в каталоге законны.
        if USE_POSTGRES:
            await txn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"product:name:{clean.lower()}",
            )
        dup = await txn.fetchrow(
            "SELECT id, name FROM products WHERE lower(name) = $1", clean.lower()
        )
        if dup:
            return {
                "ok": True,
                "product_id": int(dup["id"]),
                "name": dup["name"],
                "existed": True,
            }
        await txn.execute(
            "INSERT INTO products (name, unit, created_at) VALUES ($1, $2, $3)",
            clean,
            clean_unit,
            now_str(),
        )
        product_id = await txn.fetchval(
            "SELECT id FROM products WHERE lower(name) = $1 ORDER BY id DESC", clean.lower()
        )
    logger.info("Заведена карточка товара #%s «%s»", product_id, clean)
    return {"ok": True, "product_id": int(product_id), "name": clean, "existed": False}


async def get_link(container_id: int) -> dict:
    """Строка приёмки контейнера. Пустой словарь с `unmatched: []`, если её нет."""
    row = await adb_core.fetchrow(
        "SELECT * FROM container_receipt WHERE container_id = $1", container_id
    )
    data = dict(row) if row else {}
    if data.get("unmatched"):
        try:
            data["unmatched"] = json.loads(data["unmatched"])
        except (TypeError, ValueError):
            data["unmatched"] = []
    else:
        data["unmatched"] = []
    # Приёмка, приехавшая из МойСклад: отметка есть, накладной нет. Фронт по
    # этому флагу объясняет, почему кнопки «Оприходовать» нет, вместо того
    # чтобы показывать её и получать отказ после нажатия.
    data["legacy"] = bool(data.get("received_at")) and not data.get("invoice_id")
    return data


async def set_supplier(container_id: int, *, supplier_id: int | None, name: str | None) -> dict:
    """Задать поставщика контейнера.

    Приёмку он не держит (локальной накладной контрагент не обязателен), но
    заполненный поставщик — это ответ на «от кого пришло», который потом
    некому восстановить.
    """
    if supplier_id is not None:
        exists = await adb_core.fetchval(
            "SELECT id FROM counterparties WHERE id = $1", int(supplier_id)
        )
        if exists is None:
            return {"ok": False, "error": f"Контрагент #{supplier_id} не найден"}

    stamp = now_str()
    existing = await adb_core.fetchval(
        "SELECT container_id FROM container_receipt WHERE container_id = $1", container_id
    )
    if existing is not None:
        await adb_core.execute(
            "UPDATE container_receipt SET supplier_id = $1, supplier_name = $2, "
            "updated_at = $3 WHERE container_id = $4",
            supplier_id,
            name,
            stamp,
            container_id,
        )
    else:
        await adb_core.execute(
            "INSERT INTO container_receipt (container_id, supplier_id, supplier_name, "
            "updated_at) VALUES ($1, $2, $3, $4)",
            container_id,
            supplier_id,
            name,
            stamp,
        )
    return {"ok": True}


async def _store_result(txn, container_id: int, invoice_id: int, unmatched: list[dict]) -> None:
    stamp = now_str()
    payload = json.dumps(unmatched, ensure_ascii=False)
    updated = await txn.execute(
        "UPDATE container_receipt SET invoice_id = $1, received_at = $2, unmatched = $3, "
        "updated_at = $2 WHERE container_id = $4",
        invoice_id,
        stamp,
        payload,
        container_id,
    )
    if not updated:
        # Поставщика могли не задавать вовсе — тогда строки ещё нет.
        await txn.execute(
            "INSERT INTO container_receipt (container_id, invoice_id, received_at, "
            "unmatched, updated_at) VALUES ($1, $2, $3, $4, $3)",
            container_id,
            invoice_id,
            stamp,
            payload,
        )


async def receive(container_id: int, *, user_id: int | None = None) -> dict:
    """Оприходовать прибывший контейнер: incoming-накладная + движение остатка.

    Повторный вызов ПЕРЕОПРИХОДУЕТ: прежняя накладная отменяется, новая
    создаётся с актуальными количествами — обе операции одной транзакцией,
    чтобы склад не мог застрять между отменой и созданием.
    """
    from services import containers

    container = await containers.get_container(container_id)
    if not container:
        return {"ok": False, "error": "Контейнер не найден"}
    if container.get("status") != "arrived":
        return {"ok": False, "error": "Оприходовать можно только прибывший контейнер"}

    link = await get_link(container_id)
    if link.get("legacy"):
        return {
            "ok": False,
            "error": "Контейнер оприходован ещё в МойСклад — его остаток перенесён "
            "миграцией. Повторный приход прибавил бы товар второй раз.",
            "legacy": True,
        }

    items = await containers.list_items(container_id)
    matched, unmatched = await match_items(items)
    if not matched:
        return {
            "ok": False,
            "error": "Нечего оприходовать: ни одна позиция не найдена в номенклатуре",
            "unmatched": unmatched,
        }

    warehouse_id = await warehouse.default_warehouse_id()
    positions = [
        # Цену не выдумываем: её впишут, когда будут считать деньги.
        {"product_id": m["product_id"], "quantity": m["quantity"], "price_cents": None}
        for m in matched
    ]

    try:
        async with adb_core.transaction() as txn:
            # Две приёмки одного контейнера сериализуются: двойной тап
            # «Оприходовать» или сверка с двух телефонов раньше обе читали
            # «накладной ещё нет» и проводили ДВЕ приходные — товар удваивался.
            # Под замком перечитываем строку приёмки: вторая приёмка видит
            # накладную первой и переоприходует (отмена + новая), а не
            # добавляет к ней. На SQLite пишущая транзакция и так одна.
            if USE_POSTGRES:
                await txn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"container:receive:{container_id}",
                )
            fresh = await txn.fetchrow(
                "SELECT invoice_id, received_at FROM container_receipt WHERE container_id = $1",
                container_id,
            )
            existing_invoice = (fresh or {}).get("invoice_id")
            if fresh and fresh.get("received_at") and not existing_invoice:
                return {
                    "ok": False,
                    "error": "Контейнер оприходован ещё в МойСклад — его остаток перенесён "
                    "миграцией. Повторный приход прибавил бы товар второй раз.",
                    "legacy": True,
                }
            if existing_invoice:
                status = await txn.fetchval(
                    "SELECT status FROM invoices WHERE id = $1", int(existing_invoice)
                )
                # Уже отменённую накладную второй раз не откатываем: её остаток
                # ушёл вместе с отменой.
                if status is not None and status != "cancelled":
                    await warehouse.cancel_invoice_in(txn, int(existing_invoice), user_id)
            created = await warehouse.create_invoice_in(
                txn,
                invoice_type="incoming",
                warehouse_id=warehouse_id,
                items=positions,
                counterparty_id=(
                    int(link["supplier_id"]) if link.get("supplier_id") is not None else None
                ),
                comment=f"Контейнер {container.get('number') or container_id}",
                created_by=user_id,
            )
            await _store_result(txn, container_id, int(created["invoice_id"]), unmatched)
            # Цены закупки и курс прибытия (если учёт включён и их вписали) —
            # в партии и в саму накладную. Переоприходование переносит уже
            # проданное со старых партий на новые.
            from services import costing

            await costing.after_container_receipt_in(
                txn,
                container_id=container_id,
                invoice_id=int(created["invoice_id"]),
                previous_invoice_id=int(existing_invoice) if existing_invoice else None,
                matched=matched,
            )
    except warehouse.InvoiceError as e:
        logger.info("Приёмка контейнера #%s отклонена (%s): %s", container_id, e.code, e.message)
        return {"ok": False, "code": e.code, "error": e.message, "details": e.details}

    return {
        "ok": True,
        "invoice_id": int(created["invoice_id"]),
        "invoice_number": created["invoice_number"],
        "matched": len(matched),
        "unmatched": unmatched,
        "updated": bool(existing_invoice),
    }
