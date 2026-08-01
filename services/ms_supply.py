"""
Приёмка в МойСклад по прибывшему контейнеру.

Когда контейнер посчитан, остаток на складе надо пополнить. Раньше это делали
руками: открыть МойСклад, завести документ, перебить те же строки — то есть
посчитать товар дважды и один раз ошибиться.

Три решения, определяющие весь модуль:

* **Документ — «Приёмка»** (`entity/supply`), а не оприходование: это закупка,
  и она должна попасть в себестоимость и в расчёты с поставщиком.
* **Цены не спрашиваем при приёмке.** Человек в этот момент считает коробки, а
  не деньги; документ создаётся с нулевыми ценами, а суммы вписывают позже —
  в МойСклад или отдельной правкой. Пустая цена в приёмке лучше, чем
  выдуманная.
* **Товары сопоставляются по названию** со снапшотом номенклатуры. Что не
  совпало — не выдумываем и не создаём: такие строки возвращаются списком
  «не оприходовано, заведите товар». Молча пропущенная позиция — это остаток,
  которого нет на складе, но который считают существующим.
"""

from __future__ import annotations

import json
import logging

from services import adb_core, snapshot
from services.database import now_str
from services.moysklad import MS_BASE, get_session, order_sync_id, redact_ms_error
from utils.helpers import utc_now

logger = logging.getLogger(__name__)


def _meta(href: str, entity_type: str) -> dict:
    return {"meta": {"href": href, "type": entity_type, "mediaType": "application/json"}}


def normalize_name(raw: str | None) -> str:
    """Название к сравнимому виду: регистр и лишние пробелы не должны мешать."""
    return " ".join(str(raw or "").split()).casefold()


def match_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разложить состав на «нашли в номенклатуре» и «нет такого товара».

    Совпадение требуется ТОЧНОЕ по нормализованному названию. Похожие имена не
    склеиваем: «Кабель PV 0.6» и «Кабель PV 0.6 чёрный» — разные товары, и
    угадывание здесь означает приход не на ту карточку.
    """
    matched: list[dict] = []
    unmatched: list[dict] = []
    for it in items:
        qty = float(it.get("arrived_qty") or 0)
        if qty <= 0:
            # Не приехало — оприходовать нечего. Это не ошибка сопоставления.
            continue
        name = str(it.get("name") or "")
        # Ищем по НОРМАЛИЗОВАННОМУ имени: поиск в снапшоте — это LIKE, и
        # «  кабель   pv 0.6 » из накладной не совпал бы с «Кабель PV 0.6»
        # из-за лишних пробелов, хотя это один и тот же товар.
        candidates = [
            p for p in snapshot.search_products(normalize_name(name), limit=25)
            if normalize_name(p.get("name")) == normalize_name(name)
        ]
        if len(candidates) == 1:
            matched.append({**it, "ms_id": candidates[0]["ms_id"], "quantity": qty})
        else:
            unmatched.append({
                "name": name,
                "quantity": qty,
                "reason": "не найден в номенклатуре" if not candidates else "несколько совпадений",
            })
    return matched, unmatched


async def get_link(container_id: int) -> dict:
    row = await adb_core.fetchrow(
        "SELECT * FROM container_supply WHERE container_id = $1", container_id
    )
    data = dict(row) if row else {}
    if data.get("unmatched"):
        try:
            data["unmatched"] = json.loads(data["unmatched"])
        except (TypeError, ValueError):
            data["unmatched"] = []
    else:
        data["unmatched"] = []
    return data


async def set_supplier(
    container_id: int, *, ms_id: str | None, name: str | None
) -> dict:
    """Задать поставщика контейнера.

    Спрашиваем заранее, при заведении контейнера, а не в момент приёмки: когда
    коробки считают, о поставщике не думают, а «Приёмке» он нужен обязательно.
    """
    stamp = now_str()
    existing = await adb_core.fetchrow(
        "SELECT container_id FROM container_supply WHERE container_id = $1", container_id
    )
    if existing:
        await adb_core.execute(
            "UPDATE container_supply SET supplier_ms_id = $1, supplier_name = $2, "
            "updated_at = $3 WHERE container_id = $4",
            ms_id, name, stamp, container_id,
        )
    else:
        await adb_core.execute(
            "INSERT INTO container_supply (container_id, supplier_ms_id, supplier_name, "
            "updated_at) VALUES ($1, $2, $3, $4)",
            container_id, ms_id, name, stamp,
        )
    return {"ok": True}


async def _store_result(container_id: int, ms_supply_id: str, unmatched: list[dict]) -> None:
    stamp = now_str()
    await adb_core.execute(
        "UPDATE container_supply SET ms_supply_id = $1, synced_at = $2, unmatched = $3, "
        "updated_at = $2 WHERE container_id = $4",
        ms_supply_id, stamp, json.dumps(unmatched, ensure_ascii=False), container_id,
    )


async def sync_supply(container_id: int) -> dict:
    """Создать или обновить «Приёмку» по прибывшему контейнеру.

    Именно синхронизация, а не разовое создание: количества правятся сутки после
    приёмки, и документ обязан ехать за ними. Иначе исправленная в карточке
    недостача останется в остатках МойСклад в первоначальном виде — то есть
    сверка починит цифру у нас и оставит её неверной там, где по ней торгуют.

    Создание идемпотентно: `syncId` детерминирован по id контейнера, поэтому
    повторный POST возвращает ТОТ ЖЕ документ, а не заводит второй приход.
    """
    from services import containers, ms_demand

    if not ms_demand.is_ready():
        return {"ok": False, "error": "МойСклад не настроен (организация/склад)"}
    ctx = ms_demand._CTX

    container = await containers.get_container(container_id)
    if not container:
        return {"ok": False, "error": "Контейнер не найден"}
    if container.get("status") != "arrived":
        return {"ok": False, "error": "Оприходовать можно только прибывший контейнер"}

    link = await get_link(container_id)
    supplier_id = link.get("supplier_ms_id")
    if not supplier_id:
        return {"ok": False, "error": "Укажите поставщика — «Приёмке» он обязателен",
                "needs_supplier": True}

    items = await containers.list_items(container_id)
    matched, unmatched = match_items(items)
    if not matched:
        return {"ok": False, "error": "Нечего оприходовать: ни одна позиция не найдена "
                                      "в номенклатуре", "unmatched": unmatched}

    positions = [
        {
            "quantity": m["quantity"],
            # Цену не выдумываем: её впишут, когда будут считать деньги.
            "price": 0,
            "assortment": _meta(f"{MS_BASE}/entity/product/{m['ms_id']}", "product"),
        }
        for m in matched
    ]
    existing_id = link.get("ms_supply_id")
    payload: dict = {"positions": positions}
    if not existing_id:
        payload.update({
            "name": f"Контейнер {container['number']} (бот)",
            "syncId": order_sync_id("supply", container_id),
            "organization": _meta(ctx["org_meta"]["href"], "organization"),
            "agent": _meta(f"{MS_BASE}/entity/counterparty/{supplier_id}", "counterparty"),
            "store": _meta(ctx["store_meta"]["href"], "store"),
            "moment": utc_now().strftime("%Y-%m-%d %H:%M:%S.000"),
            "description": (container.get("notes") or "")[:255],
            "applicable": True,
        })

    url = f"{MS_BASE}/entity/supply" + (f"/{existing_id}" if existing_id else "")
    try:
        sess = await get_session()
        method = sess.put if existing_id else sess.post
        async with method(url, json=payload) as resp:
            body = await resp.text()
            if resp.status >= 400:
                safe = redact_ms_error(body)
                logger.error("MS supply HTTP %s: %s", resp.status, safe)
                return {"ok": False, "error": f"МойСклад отказал: {safe}"}
            created = json.loads(body)
    except Exception as e:
        logger.warning("Приёмка контейнера #%s не синхронизирована: %s", container_id, e)
        return {"ok": False, "error": "МойСклад недоступен, попробуйте позже"}

    ms_id = created.get("id", "") or existing_id or ""
    await _store_result(container_id, ms_id, unmatched)
    return {
        "ok": True, "ms_id": ms_id, "matched": len(matched),
        "unmatched": unmatched, "updated": bool(existing_id),
    }
