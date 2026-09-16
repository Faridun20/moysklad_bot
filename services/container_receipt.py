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
from typing import Any

from services import adb_core, warehouse
from services.database import USE_POSTGRES, now_str

logger = logging.getLogger(__name__)


def normalize_name(raw: str | None) -> str:
    """Название к сравнимому виду: регистр, лишние пробелы и «ё» не должны мешать.

    «Ёлочный кронштейн» и «елочный  кронштейн» — один товар: на телефоне «ё»
    набирают долгим нажатием и чаще не набирают вовсе, а двойной пробел
    приезжает из МойСклад. Не заметить такой дубль значит завести вторую
    карточку и развести остаток по двум.
    """
    return " ".join(str(raw or "").split()).casefold().replace("ё", "е")


# Сколько LIKE-шаблонов в одном запросе. Позиций в контейнере бывает сотни, а
# у asyncpg предел параметров; пачка по сотне — один проход по каталогу на пачку.
_NAME_CHUNK = 100


def _name_pattern(norm: str) -> str:
    """LIKE-шаблон, который заведомо ловит карточку с тем же нормализованным
    именем: слова по порядку, между ними что угодно.

    Пробелы в каталоге бывают двойными, по краям — лишними, и точного `=` по
    выражению SQL не построить одинаково на двух базах (в SQLite нет regexp).
    Шаблон шире, чем нужно, — окончательное сравнение делается в Python по
    `normalize_name`, поэтому «похожее» за одинаковое не сойдёт.
    """
    return "%" + "%".join(norm.split()) + "%"


async def _products_by_name(names: list[str], conn: Any = None) -> dict[str, list[dict]]:
    """Карточки номенклатуры под нормализованными именами. {norm: [rows]}.

    Одним запросом на пачку позиций, а не по позиции: контейнер на полсотни
    строк иначе дал бы полсотни обращений к БД внутри одной приёмки.

    Сравнение — по `normalize_name` (регистр, пробелы, ё=е) с обеих сторон:
    SQL отбирает кандидатов шаблоном (`_name_pattern` против `name_search_sql`),
    Python оставляет только точные совпадения. `conn` — транзакция, если
    проверка должна видеть её же незакоммиченные вставки.
    """
    keys = sorted({normalize_name(n) for n in names} - {""})
    if not keys:
        return {}
    db = conn if conn is not None else adb_core
    out: dict[str, list[dict]] = {}
    seen: set[int] = set()
    for start in range(0, len(keys), _NAME_CHUNK):
        chunk = keys[start:start + _NAME_CHUNK]
        where = " OR ".join(
            f"{adb_core.name_search_sql('name')} LIKE ${i + 1}" for i in range(len(chunk))
        )
        rows = await db.fetch(
            f"SELECT id, name, unit FROM products WHERE {where} ORDER BY id",
            *[_name_pattern(k) for k in chunk],
        )
        wanted = set(chunk)
        for r in rows:
            norm = normalize_name(r["name"])
            if norm in wanted and int(r["id"]) not in seen:
                seen.add(int(r["id"]))
                out.setdefault(norm, []).append(dict(r))
    return out


async def same_name_products(name: str) -> list[dict]:
    """Карточки каталога с тем же названием (регистр, пробелы и ё не в счёт).

    Позицию «новым товаром» с таким именем заводить нельзя молча: это не новый
    товар, а опечатка поиска, и приход разъехался бы по двум карточкам.
    """
    return (await _products_by_name([name])).get(normalize_name(name), [])


async def catalog_matches(items: list[dict]) -> dict[int, list[dict]]:
    """Для непривязанных позиций — карточки с тем же названием. {item_id: [rows]}.

    Карточка контейнера показывает их сразу: человек видит, куда уйдёт приход
    позиции, заведённой свободным текстом, и подтверждает это, а не узнаёт
    после оприходования.
    """
    unlinked = [it for it in items if not it.get("product_id")]
    by_name = await _products_by_name([str(it.get("name") or "") for it in unlinked])
    out: dict[int, list[dict]] = {}
    for it in unlinked:
        found = by_name.get(normalize_name(it.get("name")), [])
        if found:
            out[int(it["id"])] = [
                {"product_id": int(p["id"]), "name": p["name"], "unit": p.get("unit")}
                for p in found[:5]
            ]
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
                        "нет такого товара в каталоге" if not candidates else "подходит сразу несколько товаров"
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
        return {"ok": False, "error": "Укажите название товара"}

    existing = (await _products_by_name([clean])).get(normalize_name(clean)) or []
    if existing:
        return {
            "ok": True,
            "product_id": int(existing[0]["id"]),
            "name": existing[0]["name"],
            "existed": True,
        }

    clean_unit = (str(unit or "шт").strip() or "шт")[:16]
    key = normalize_name(clean)
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
                f"product:name:{key}",
            )
        same = (await _products_by_name([clean], conn=txn)).get(key) or []
        dup = same[0] if same else None
        if dup:
            return {
                "ok": True,
                "product_id": int(dup["id"]),
                "name": dup["name"],
                "existed": True,
            }
        insert = "INSERT INTO products (name, unit, created_at) VALUES ($1, $2, $3)"
        if USE_POSTGRES:
            product_id = await txn.fetchval(insert + " RETURNING id", clean, clean_unit, now_str())
        else:
            await txn.execute(insert, clean, clean_unit, now_str())
            product_id = await txn.fetchval("SELECT last_insert_rowid()")
    logger.info("Заведена карточка товара #%s «%s»", product_id, clean)
    return {"ok": True, "product_id": int(product_id), "name": clean, "existed": False}


def parse_resolutions(raw: Any) -> dict[int, dict] | None:
    """Решения по непривязанным позициям из тела запроса.

    `{"<item_id>": {"product_id": N}}` — позиция это товар N из каталога;
    `{"<item_id>": {"new": true}}` — завести карточку по названию позиции.
    None — формат не тот (ручка отвечает 400, а не угадывает).
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        return None
    out: dict[int, dict] = {}
    for key, choice in raw.items():
        try:
            item_id = int(key)
        except (TypeError, ValueError):
            return None
        if not isinstance(choice, dict):
            return None
        if choice.get("new") is True and not choice.get("product_id"):
            out[item_id] = {"new": True}
            continue
        try:
            product_id = int(choice.get("product_id") or 0)
        except (TypeError, ValueError):
            return None
        if product_id <= 0:
            return None
        out[item_id] = {"product_id": product_id}
    return out


async def resolve_items(container_id: int, resolutions: dict[int, dict]) -> dict:
    """Применить выбор человека перед оприходованием: привязать позиции к
    карточкам каталога или завести новые карточки по их названиям.

    Это ТА САМАЯ «кнопка», ради которой автосоздания из приёмки нет: форма
    оприходования показывает каждую непривязанную позицию и то, куда она
    уйдёт, и человек подтверждает. «Новый товар» с именем, которое в каталоге
    уже есть, дубля не заводит — `create_product` привязывает существующую
    карточку (`existed`).

    Всё проверяется ДО первой записи: позиция из чужого контейнера или
    несуществующий товар отказывают целиком, а не на середине списка.
    """
    from services import containers

    if not resolutions:
        return {"ok": True, "linked": 0, "created": [], "existed": []}
    # Окно правки — первым: иначе «новый товар» успел бы завести карточку, а
    # привязка к ней получила бы отказ.
    guard = await containers._require_open_window(container_id)
    if guard:
        return guard
    items ={int(i["id"]): i for i in await containers.list_items(container_id)}
    if set(resolutions) - set(items):
        return {"ok": False, "error": "Эта позиция не из этого контейнера — обновите экран"}
    wanted = sorted({c["product_id"] for c in resolutions.values() if c.get("product_id")})
    if wanted:
        placeholders = ", ".join(f"${i + 1}" for i in range(len(wanted)))
        known = {
            int(r["id"])
            for r in await adb_core.fetch(
                f"SELECT id FROM products WHERE id IN ({placeholders})", *wanted
            )
        }
        missing = [pid for pid in wanted if pid not in known]
        if missing:
            return {"ok": False, "error": f"Товар #{missing[0]} не найден в каталоге — выберите его заново"}

    created: list[str] = []
    existed: list[str] = []
    for item_id in sorted(resolutions):
        choice = resolutions[item_id]
        item = items[item_id]
        product_id = choice.get("product_id")
        if choice.get("new"):
            made = await create_product(str(item["name"]), unit=str(item.get("unit") or "шт"))
            if not made.get("ok"):
                return made
            product_id = int(made["product_id"])
            (existed if made.get("existed") else created).append(str(made.get("name")))
        res = await containers.link_item(container_id, item_id, product_id=int(product_id or 0))
        if not res.get("ok"):
            return res
    return {"ok": True, "linked": len(resolutions), "created": created, "existed": existed}


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
            return {"ok": False, "error": f"Поставщик #{supplier_id} не найден — выберите его в справочнике «Клиенты и поставщики»"}

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
        return {"ok": False, "error": "Контейнер не найден — обновите список"}
    if container.get("status") != "arrived":
        return {"ok": False, "error": "Принять на склад можно только прибывший контейнер — сначала отметьте, что он приехал"}

    link = await get_link(container_id)
    if link.get("legacy"):
        return {
            "ok": False,
            "error": "Этот контейнер приняли на склад ещё в МойСклад, его остаток "
            "перенесён к нам. Повторный приход прибавил бы товар второй раз.",
            "legacy": True,
        }

    items = await containers.list_items(container_id)
    matched, unmatched = await match_items(items)
    if not matched:
        return {
            "ok": False,
            "error": "Принимать нечего: ни один товар контейнера не найден в каталоге — "
            "свяжите позиции с товарами и повторите",
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
                    "error": "Этот контейнер приняли на склад ещё в МойСклад, его остаток "
                    "перенесён к нам. Повторный приход прибавил бы товар второй раз.",
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
