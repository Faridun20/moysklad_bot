"""
ОДНОРАЗОВЫЙ скрипт миграции справочников и остатков МойСклад → локальные таблицы.

НЕ часть кода бота: ничем не импортируется, автоматически не вызывается,
после перехода остаётся в репозитории как документ того, что и откуда
приехало. Запускается руками на шаге 2 плана переключения — когда новые
таблицы уже развёрнуты, а МойСклад-путь ещё активен.

Что переносит:
    entity/product     → products        (name, category, sku, unit)
    entity/counterparty→ counterparties  (name, type, phone)
    report/stock/all   → stock           (quantity на складе по умолчанию)

Плюс `ms_id_map` — соответствие «UUID МойСклад → локальный id». Это рабочий
артефакт миграции: по нему сверяются данные и по нему же на шаге 4 заказы,
кредит-лимиты и цены переводятся с ms-ссылок на числовые ключи.

Использование:
    python -m scripts.migrate_from_moysklad --dry-run   # только выгрузка + отчёт
    python -m scripts.migrate_from_moysklad --apply     # выгрузка + запись + сверка
    python -m scripts.migrate_from_moysklad --verify    # только сверка с МС

Идемпотентность: повторный --apply не плодит дубли. Совпадение ищется по
legacy_ms_id; уже перенесённые строки обновляются, новые добавляются.

**Повторный --apply ПОСЛЕ переключения запрещён.** Остаток пишется снимком из
МС (`quantity = EXCLUDED.quantity`), а после переключения склад двигают живые
накладные — снимок стёр бы их молча. Скрипт отказывается, если после первого
переноса в базе уже есть живые накладные или заказы. Осознанный повтор —
`--apply --i-know-live-data`: справочники обновятся, а остаток товаров, по
которым были живые движения, НЕ перезаписывается.

Код возврата: 0 — успех и сверка сошлась; 1 — ошибка или РАСХОЖДЕНИЕ.
Ненулевой код обязан блокировать переключение: расхождение в остатках
означает, что локальная база разойдётся с реальностью в первый же день,
а обратной синхронизации, которая это починит, после перехода нет.
"""

import argparse
import asyncio
import logging
import os
import sys
from decimal import Decimal

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ms_migrate")

# Что считаем «расхождением». Ноль — буквально ноль: количества сравниваем
# через Decimal(str(x)), а не float, поэтому точное сравнение корректно и
# не ловит бинарный шум (0.1 + 0.2 != 0.3).
TOLERANCE = Decimal("0")


def _dec(value) -> Decimal:
    """Число из JSON МойСклад → точный Decimal (через str, не через float)."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


# ─── Минимальный клиент МойСклад ──────────────────────────────────────────────
#
# СВОЙ, а не `services.moysklad`: вся МойСклад-интеграция из кода бота удалена,
# и импортировать оттуда больше нечего. Скрипт обязан пережить это удаление —
# иначе перенос перестал бы воспроизводиться ровно тогда, когда он ещё может
# понадобиться (аккаунт МС живёт ещё месяц-другой после переключения).
#
# Всё, что здесь нужно, — постраничный GET с ретраем на 429/5xx. Ни брейкера,
# ни бюджета запросов: скрипт запускают вручную, один раз, и он единственный
# клиент токена в этот момент.

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"
_MS_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MS_MAX_RETRIES = 5
_session: aiohttp.ClientSession | None = None


async def _ms_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        token = os.getenv("MS_TOKEN", "")
        if not token:
            raise RuntimeError("MS_TOKEN не задан — выгружать нечем")
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
            },
        )
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def ms_get(path: str, params: dict | None = None) -> dict:
    """GET к МойСклад с ретраем. `X-Lognex-Retry-After` — в миллисекундах."""
    sess = await _ms_session()
    url = f"{MS_BASE}/{path}"
    delay = 1.0
    for attempt in range(_MS_MAX_RETRIES):
        async with sess.get(url, params=params) as resp:
            if resp.status in _MS_RETRY_STATUSES and attempt < _MS_MAX_RETRIES - 1:
                wait = delay
                raw = resp.headers.get("X-Lognex-Retry-After")
                if raw and raw.isdigit():
                    wait = max(wait, int(raw) / 1000.0)
                logger.warning("МойСклад HTTP %s — жду %.1f с", resp.status, wait)
                await asyncio.sleep(wait)
                delay *= 2
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError(f"МойСклад не ответил после {_MS_MAX_RETRIES} попыток: {path}")


async def _fetch_all(path: str, params: dict | None = None) -> list[dict]:
    """Постранично выкачать коллекцию МойСклад."""
    limit = 1000
    rows: list[dict] = []
    offset = 0
    while True:
        p = dict(params or {})
        p.update({"limit": limit, "offset": offset})
        data = await ms_get(path, params=p)
        chunk = data if isinstance(data, list) else data.get("rows", [])
        rows.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
    return rows


# ─── Выгрузка из МойСклад ─────────────────────────────────────────────────────


async def pull_products() -> list[dict]:
    from utils.helpers import extract_id_from_href

    def _href(r: dict) -> str:
        return ((r.get("meta") or {}).get("href")) or ""

    out = []
    for r in await _fetch_all("entity/product"):
        ms_id = r.get("id") or extract_id_from_href(_href(r))
        if not ms_id:
            continue
        out.append(
            {
                "ms_id": ms_id,
                "name": (r.get("name") or "").strip(),
                # pathName — путь группы товаров («Крепёж/Болты»). Локальная
                # схема держит категорию одной строкой, иерархии групп у неё
                # нет: путь и есть самое близкое к «категории».
                "category": (r.get("pathName") or "").strip() or None,
                "sku": ((r.get("code") or r.get("article") or "").strip() or None),
                "unit": ((r.get("uom") or {}).get("name") or "шт"),
            }
        )
    return out


async def pull_counterparties() -> list[dict]:
    from utils.helpers import extract_id_from_href

    def _href(r: dict) -> str:
        return ((r.get("meta") or {}).get("href")) or ""

    out = []
    for r in await _fetch_all("entity/counterparty", params={"order": "name"}):
        ms_id = r.get("id") or extract_id_from_href(_href(r))
        if not ms_id:
            continue
        out.append(
            {
                "ms_id": ms_id,
                "name": (r.get("name") or "").strip(),
                "phone": (r.get("phone") or "").strip() or None,
                # МойСклад не разделяет поставщиков и покупателей отдельным
                # полем (только тегами, которые у нас не заполнены), поэтому
                # всех переносим как customer. Поставщиков, если они есть,
                # переключают руками в карточке контрагента после миграции.
                "type": "customer",
            }
        )
    return out


async def pull_stock() -> dict[str, Decimal]:
    """{ms_id товара: остаток}. Агрегат по всем складам МойСклад."""
    from utils.helpers import extract_id_from_href

    out: dict[str, Decimal] = {}
    for r in await _fetch_all("report/stock/all"):
        ms_id = extract_id_from_href(((r.get("meta") or {}).get("href")) or "")
        if not ms_id:
            continue
        out[ms_id] = out.get(ms_id, Decimal("0")) + _dec(r.get("stock"))
    return out


# ─── Запись в локальные таблицы ───────────────────────────────────────────────


async def _default_warehouse_id(txn) -> int:
    wid = await txn.fetchval("SELECT id FROM warehouses ORDER BY id LIMIT 1")
    if wid is None:
        raise RuntimeError(
            "В таблице warehouses нет ни одного склада. "
            "Сначала прогоните `python -m tasks.migrate` (сидинг «Основной склад»)."
        )
    return int(wid)


async def _upsert_entity(
    txn, table: str, ms_id: str, fields: dict, now: str, *, insert_only: tuple[str, ...] = ()
) -> int:
    """Вставить или обновить строку по legacy_ms_id. Возвращает локальный id.

    `insert_only` — поля, которые пишутся только при создании. Тип контрагента
    из МС не выводится (там всё «customer»), а уточняют его перенос истории и
    человек в карточке; повторный прогон справочника не должен это стирать.

    Без ON CONFLICT: партиальный UNIQUE-индекс по legacy_ms_id есть, но
    поддержка `ON CONFLICT` по партиальному индексу требует повторять его
    предикат, и на двух бэкендах это расходится. Явные SELECT-then-write
    внутри транзакции здесь надёжнее и читаются однозначно.
    """
    existing = await txn.fetchval(f"SELECT id FROM {table} WHERE legacy_ms_id = $1", ms_id)
    cols = list(fields)
    if existing is not None:
        cols = [c for c in cols if c not in insert_only]
        assignments = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        await txn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ${len(cols) + 1}",
            *[fields[c] for c in cols],
            int(existing),
        )
        return int(existing)

    all_cols = cols + ["legacy_ms_id", "created_at"]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(all_cols)))
    await txn.execute(
        f"INSERT INTO {table} ({', '.join(all_cols)}) VALUES ({placeholders})",
        *[fields[c] for c in cols],
        ms_id,
        now,
    )
    new_id = await txn.fetchval(f"SELECT id FROM {table} WHERE legacy_ms_id = $1", ms_id)
    return int(new_id)


class LiveDataError(RuntimeError):
    """После переноса в базе уже идёт живая работа — снимок остатков её сотрёт."""


async def live_activity() -> dict:
    """Что изменилось в базе ПОСЛЕ первого переноса справочников.

    Граница — самый ранний `ms_id_map.migrated_at` у товаров/контрагентов:
    `_write_map` его больше не сдвигает, поэтому повторный прогон не отодвигает
    границу вперёд и не «прячет» уже случившиеся живые накладные.

    Живое — накладные не из переноса истории (`warehouse.historical_invoice_sql`) и заказы с настоящим
    автором (`user_id <> 0`; у исторических — 0). Возвращает счётчики и
    `product_ms_ids` — товары МС, по которым были живые движения: их остаток
    при осознанном повторе трогать нельзя.
    """
    from services import adb_core
    from services.warehouse import historical_invoice_sql

    since = await adb_core.fetchval(
        "SELECT MIN(migrated_at) FROM ms_id_map WHERE entity_type IN ('product', 'counterparty')"
    )
    out: dict = {"since": since, "invoices": 0, "orders": 0, "product_ms_ids": set()}
    if since is None:
        return out
    # Признак истории — тот же, по которому warehouse запрещает её отмену: одно
    # определение на проект, иначе два списка разойдутся при первой правке.
    not_history = f"NOT {historical_invoice_sql('i')}"
    out["invoices"] = int(
        await adb_core.fetchval(
            f"SELECT COUNT(*) FROM invoices i WHERE i.created_at > $1 AND {not_history}", since
        )
        or 0
    )
    out["orders"] = int(
        await adb_core.fetchval(
            "SELECT COUNT(*) FROM orders WHERE created_at > $1 AND user_id <> 0", since
        )
        or 0
    )
    rows = await adb_core.fetch(
        "SELECT DISTINCT p.legacy_ms_id FROM invoice_items ii "
        "JOIN invoices i ON i.id = ii.invoice_id "
        "JOIN products p ON p.id = ii.product_id "
        f"WHERE i.created_at > $1 AND {not_history} AND p.legacy_ms_id IS NOT NULL",
        since,
    )
    out["product_ms_ids"] = {str(r["legacy_ms_id"]) for r in rows}
    return out


def live_data_refusal(live: dict) -> str | None:
    """Текст отказа для оператора; None — живой работы нет, перенос безопасен."""
    if not (live["invoices"] or live["orders"]):
        return None
    return (
        f"После первого переноса ({live['since']}) в базе уже есть живая работа: "
        f"накладных {live['invoices']}, заказов {live['orders']}. Повторный --apply "
        "перезаписал бы остатки снимком из МойСклад и молча стёр бы эти движения. "
        "Если повтор действительно нужен (например, догнать новые карточки товаров), "
        "запустите с --i-know-live-data: справочники обновятся, а остаток товаров "
        f"с живыми движениями ({len(live['product_ms_ids'])}) останется как есть."
    )


async def apply_migration(
    products: list[dict],
    counterparties: list[dict],
    stock: dict[str, Decimal],
    *,
    protect_ms_ids: set[str] | None = None,
) -> dict:
    """Записать всё одной транзакцией. Частично применённой миграции не бывает.

    `protect_ms_ids` — товары, чей остаток НЕ перезаписывается снимком МС:
    по ним уже прошли живые накладные, и снимок откатил бы их.
    """
    from services import adb_core
    from services.database import now_str

    now = now_str()
    protect = protect_ms_ids or set()
    stats = {
        "products": 0, "counterparties": 0, "stock_rows": 0, "stock_skipped": 0,
        "stock_protected": 0,
    }

    async with adb_core.transaction() as txn:
        warehouse_id = await _default_warehouse_id(txn)

        product_local: dict[str, int] = {}
        for p in products:
            local_id = await _upsert_entity(
                txn,
                "products",
                p["ms_id"],
                {
                    "name": p["name"],
                    "category": p["category"],
                    "sku": p["sku"],
                    "unit": p["unit"],
                },
                now,
            )
            product_local[p["ms_id"]] = local_id
            stats["products"] += 1

        for c in counterparties:
            local_id = await _upsert_entity(
                txn,
                "counterparties",
                c["ms_id"],
                {"name": c["name"], "phone": c["phone"], "type": c["type"]},
                now,
                insert_only=("type",),
            )
            await _write_map(txn, "counterparty", c["ms_id"], local_id, now)
            stats["counterparties"] += 1

        for ms_id, local_id in product_local.items():
            await _write_map(txn, "product", ms_id, local_id, now)

        for ms_id, qty in stock.items():
            local_id = product_local.get(ms_id)
            if local_id is None:
                # Остаток есть, а товара в номенклатуре нет: в МойСклад так
                # бывает у архивных/удалённых позиций. Переносить некуда;
                # считаем и показываем в отчёте, сверка это учтёт.
                stats["stock_skipped"] += 1
                continue
            if ms_id in protect:
                stats["stock_protected"] += 1
                continue
            await txn.execute(
                "INSERT INTO stock (product_id, warehouse_id, quantity) VALUES ($1, $2, $3) "
                "ON CONFLICT (product_id, warehouse_id) DO UPDATE SET quantity = EXCLUDED.quantity",
                local_id,
                warehouse_id,
                float(qty),
            )
            stats["stock_rows"] += 1

    return stats


async def _write_map(txn, entity_type: str, ms_id: str, local_id: int, now: str) -> None:
    await txn.execute(
        "INSERT INTO ms_id_map (entity_type, ms_id, local_id, migrated_at) "
        "VALUES ($1, $2, $3, $4) "
        # migrated_at НЕ обновляем: это граница «до переноса / после», по
        # которой live_activity отличает живые накладные. Сдвинь её повтор —
        # и следующий повтор уже не увидел бы живую работу между ними.
        "ON CONFLICT (entity_type, ms_id) DO UPDATE SET local_id = EXCLUDED.local_id",
        entity_type,
        ms_id,
        local_id,
        now,
    )


# ─── Сверка ───────────────────────────────────────────────────────────────────


async def verify(
    products: list[dict],
    counterparties: list[dict],
    stock: dict[str, Decimal],
    *,
    skip_ms_ids: set[str] | None = None,
) -> list[str]:
    """Сверить локальную базу с выгрузкой. Пустой список — расхождений нет.

    `skip_ms_ids` — товары с живыми движениями: их остаток законно отличается
    от снимка МС, и сверять его значит получить расхождение, которое нечем
    и незачем чинить.
    """
    from services import adb_core

    problems: list[str] = []

    local_products = await adb_core.fetchval(
        "SELECT COUNT(*) FROM products WHERE legacy_ms_id IS NOT NULL"
    )
    if int(local_products or 0) != len(products):
        problems.append(
            f"товары: в МойСклад {len(products)}, локально {int(local_products or 0)}"
        )

    local_cp = await adb_core.fetchval(
        "SELECT COUNT(*) FROM counterparties WHERE legacy_ms_id IS NOT NULL"
    )
    if int(local_cp or 0) != len(counterparties):
        problems.append(
            f"контрагенты: в МойСклад {len(counterparties)}, локально {int(local_cp or 0)}"
        )

    # Сумма остатков. Считаем ТОЛЬКО по товарам, которые реально перенесены:
    # остатки «висячих» ms_id (товар удалён, остаток остался) переносить
    # некуда, и включать их в ожидаемую сумму значило бы гарантированно
    # получить расхождение, которое ничем не чинится.
    migrated = {p["ms_id"] for p in products} - (skip_ms_ids or set())
    expected_total = sum((stock.get(m, Decimal("0")) for m in migrated), Decimal("0"))

    rows = await adb_core.fetch(
        "SELECT p.legacy_ms_id, s.quantity FROM stock s LEFT JOIN products p ON p.id = s.product_id"
    )
    local_total = sum(
        (_dec(r["quantity"]) for r in rows if r["legacy_ms_id"] not in (skip_ms_ids or set())),
        Decimal("0"),
    )

    if abs(local_total - expected_total) > TOLERANCE:
        problems.append(
            f"сумма остатков: в МойСклад {expected_total}, локально {local_total} "
            f"(расхождение {local_total - expected_total})"
        )

    # Построчная сверка — какие именно позиции разошлись.
    local_by_ms = {
        r["legacy_ms_id"]: _dec(r["quantity"])
        for r in await adb_core.fetch(
            "SELECT p.legacy_ms_id, COALESCE(s.quantity, 0) AS quantity "
            "FROM products p LEFT JOIN stock s ON s.product_id = p.id "
            "WHERE p.legacy_ms_id IS NOT NULL"
        )
    }
    mismatched = [
        (m, stock.get(m, Decimal("0")), local_by_ms.get(m, Decimal("0")))
        for m in migrated
        if abs(local_by_ms.get(m, Decimal("0")) - stock.get(m, Decimal("0"))) > TOLERANCE
    ]
    if mismatched:
        head = ", ".join(f"{m}: МС={a}, локально={b}" for m, a, b in mismatched[:10])
        suffix = f" (ещё {len(mismatched) - 10})" if len(mismatched) > 10 else ""
        problems.append(f"остатки разошлись по {len(mismatched)} позициям: {head}{suffix}")

    return problems


# ─── CLI ──────────────────────────────────────────────────────────────────────


async def main(mode: str, *, allow_live: bool = False) -> int:
    from services.database import init_db

    init_db()
    try:
        logger.info("Выгружаю справочники из МойСклад…")
        products = await pull_products()
        counterparties = await pull_counterparties()
        stock = await pull_stock()
        stock_total = sum(stock.values(), Decimal("0"))
        logger.info(
            "Из МойСклад: товаров %d, контрагентов %d, позиций с остатком %d (сумма %s)",
            len(products), len(counterparties), len(stock), stock_total,
        )

        orphan = [m for m in stock if m not in {p["ms_id"] for p in products}]
        if orphan:
            logger.warning(
                "Остатки по %d позициям без карточки товара (архив/удалённые) — "
                "перенести некуда, в сверке не участвуют: %s",
                len(orphan), ", ".join(orphan[:5]),
            )

        live = await live_activity()
        refusal = live_data_refusal(live)
        protected: set[str] = set()

        if mode == "dry-run":
            if refusal:
                logger.warning("%s", refusal)
            logger.info("--dry-run: в базу ничего не записано.")
            return 0

        if mode == "apply":
            if refusal and not allow_live:
                logger.error("ОТКАЗ: %s", refusal)
                return 1
            if refusal:
                protected = set(live["product_ms_ids"])
                logger.warning(
                    "--i-know-live-data: остаток %d товаров с живыми движениями "
                    "не перезаписывается и в сверке остатков не участвует",
                    len(protected),
                )
            stats = await apply_migration(
                products, counterparties, stock, protect_ms_ids=protected
            )
            logger.info(
                "Записано: товаров %d, контрагентов %d, строк остатка %d "
                "(пропущено %d, защищено живыми движениями %d)",
                stats["products"], stats["counterparties"],
                stats["stock_rows"], stats["stock_skipped"], stats["stock_protected"],
            )

        if mode == "verify" and refusal:
            # Сверка после переключения: остаток товаров с живыми движениями
            # законно ушёл от снимка МС — это не расхождение переноса.
            protected = set(live["product_ms_ids"])
            logger.warning(
                "После переноса была живая работа — остаток %d товаров с движениями "
                "в сверке не участвует", len(protected),
            )

        logger.info("Сверка…")
        problems = await verify(products, counterparties, stock, skip_ms_ids=protected)
        if problems:
            logger.error("СВЕРКА НЕ СОШЛАСЬ — переключаться НЕЛЬЗЯ:")
            for p in problems:
                logger.error("  • %s", p)
            return 1

        logger.info("✓ Сверка сошлась: расхождений нет. Можно переходить к шагу 3 (тестовые накладные).")
        return 0
    except Exception:
        logger.exception("Миграция упала — база могла остаться в прежнем состоянии (транзакция откатывается)")
        return 1
    finally:
        await close_session()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Одноразовая миграция МойСклад → локальные таблицы")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="только выгрузка и отчёт")
    g.add_argument("--apply", action="store_true", help="выгрузка, запись и сверка")
    g.add_argument("--verify", action="store_true", help="только сверка локальной базы с МС")
    p.add_argument(
        "--i-know-live-data",
        dest="i_know_live_data",
        action="store_true",
        help="разрешить --apply при живых накладных/заказах; их остаток не перезаписывается",
    )
    args = p.parse_args(argv)
    if args.i_know_live_data and not args.apply:
        p.error("--i-know-live-data имеет смысл только вместе с --apply")
    return args


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    mode = "dry-run" if args.dry_run else ("apply" if args.apply else "verify")
    sys.exit(asyncio.run(main(mode, allow_live=args.i_know_live_data)))
