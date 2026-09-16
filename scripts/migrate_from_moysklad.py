"""
ОДНОРАЗОВЫЙ скрипт миграции справочников и остатков МойСклад → локальные таблицы.

НЕ часть кода бота: ничем не импортируется, автоматически не вызывается,
после перехода остаётся в репозитории как документ того, что и откуда
приехало. Запускается руками на шаге 2 плана переключения — когда новые
таблицы уже развёрнуты, а МойСклад-путь ещё активен.

Что переносит:
    entity/product     → products        (name, category, sku, unit)
                       → product_prices  (цена продажи, «оптовая», закупочная)
    entity/counterparty→ counterparties  (name, type, phone)
    report/stock/all   → stock           (quantity на складе по умолчанию)
    entity/currency    — только словарь «uuid валюты → ISO» для цен

Плюс `ms_id_map` — соответствие «UUID МойСклад → локальный id». Это рабочий
артефакт миграции: по нему сверяются данные и по нему же на шаге 4 заказы,
кредит-лимиты и цены переводятся с ms-ссылок на числовые ключи.

Использование:
    python -m scripts.migrate_from_moysklad --dry-run   # выгрузка + отчёт (запись откатывается)
    python -m scripts.migrate_from_moysklad --apply     # выгрузка + запись + сверка
    python -m scripts.migrate_from_moysklad --verify    # только сверка с МС

Идемпотентность: повторный --apply не плодит дубли. Совпадение ищется по
legacy_ms_id; уже перенесённые строки обновляются, новые добавляются.

**`--dry-run` идёт ТЕМ ЖЕ путём записи**, что и --apply, в транзакции, которая
в конце откатывается, — вместе со сверкой внутри неё. Отчёт (отрицательные
остатки, дубли артикулов, цены) считается по настоящей записи со всеми
ограничениями базы, а не по отдельной ветке «как будто», которая разошлась бы
с боевой молча.

**Отрицательный остаток пишется НУЛЁМ.** МойСклад разрешает продавать в минус,
и прошлый перенос привёз 32 строки `stock` < 0. Такая строка противоречит
всему локальному складу: `services/warehouse.py` не уводит остаток в минус,
CHECK `stock_quantity_chk` (quantity >= 0) из `scripts/apply_constraints` на
такой базе не ставится вовсе, а после его постановки приход +5 на остаток −10
падал бы. Полки с минусом товара не бывает: реальное число неизвестно и
требует пересчёта, а ноль — ближайшее к правде, что можно записать. Каждый
такой товар идёт в отчёт (имя, количество в МС), сверка сравнивает с нулём, а
не с минусом, и число обнулённых позиций печатается отдельно — не молча.

**Артикул уникален в базе, а в МС — нет.** У `products` UNIQUE (sku), а sku
берётся как `code` или `article`; артикул в МС не уникален, и код одного
товара может совпасть с артикулом другого. Один дубль ронял бы весь перенос.
Поэтому артикул получает ПЕРВЫЙ товар (в порядке выгрузки МС), у следующих sku
пуст, и каждый такой товар — в отчёте. Занятый живой карточкой (не из
переноса) артикул — тоже дубль. Повтор переноса оставляет товару его артикул.

**Цены — первый тип цены продажи МС.** `salePrices[0]` → `sale_price_cents`
(в МС цена уже в минорных единицах, как наши копейки) и валюта (ISO по словарю
`entity/currency`: в цене лежит только ссылка на валюту). Тип, в названии
которого есть «опт», → `wholesale_price_cents` («для постоянных»), закупочная
`buyPrice` → `cost_price_cents`; обе — только в валюте цены продажи: у строки
`product_prices` валюта одна, а пересчёт по курсу превратил бы прайс в
выдумку. Валюта не из ALLOWED_CURRENCIES — цена не переносится. Всё
несошедшееся — в отчёт. Ключ строки — НАШ id товара строкой
(`product_prices.ms_id`, см. «Идентификаторы» в CLAUDE.md). Цену, которую
человек уже поправил (`updated_by` не 0), повтор переноса НЕ перетирает.

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

# Отметка «цену записал перенос» в `product_prices.updated_by`. Всё прочее —
# правка человека, и повтор переноса её не трогает.
MIGRATION_USER = 0

# Сколько строк каждой категории печатать в отчёте; остальное — числом.
SHOW = 20


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


def _meta_id(obj: dict | None) -> str:
    """UUID из `obj.meta.href` (или `obj.id`). Пусто — ссылки нет."""
    from utils.helpers import extract_id_from_href

    obj = obj or {}
    return str(obj.get("id") or extract_id_from_href(((obj.get("meta") or {}).get("href")) or ""))


async def pull_currency_isos() -> dict[str, str]:
    """{uuid валюты МС: ISO-код}. В цене товара валюта — только ссылка.

    `name` у валюты МС — «сум»/«доллар», не ISO; код лежит в `isoCode`.
    """
    out: dict[str, str] = {}
    for c in await _fetch_all("entity/currency"):
        ms_id = _meta_id(c)
        iso = str(c.get("isoCode") or "").upper()
        if ms_id and iso:
            out[ms_id] = iso
    return out


def _allowed_currencies() -> tuple[str, ...]:
    from config import ALLOWED_CURRENCIES

    return tuple(c.upper() for c in ALLOWED_CURRENCIES)


def extract_price(raw: dict, iso_by_id: dict[str, str]) -> tuple[dict | None, list[str]]:
    """Цены одного товара МС → строка для `product_prices` и список замечаний.

    Чистая функция (без БД и сети) — чтобы правила были видны и проверяемы:
      * цена продажи — ПЕРВЫЙ тип цены (`salePrices[0]`), ноль/нет — без неё;
      * «оптовая» — первый тип с «опт» в названии, только в валюте продажи;
      * закупочная — `buyPrice`, только в валюте продажи (без цены продажи —
        в своей валюте);
      * валюта не из ALLOWED_CURRENCIES или не найдена в словаре — цена не
        переносится вовсе (замечание);
      * нечего записать — None.
    """
    name = (raw.get("name") or "").strip() or "—"
    issues: list[str] = []
    allowed = _allowed_currencies()

    def _cur(price: dict) -> str:
        return iso_by_id.get(_meta_id(price.get("currency")), "")

    sale_prices = [sp for sp in (raw.get("salePrices") or []) if isinstance(sp, dict)]
    sale = sale_prices[0] if sale_prices else None
    sale_cents = int(sale.get("value") or 0) if sale else 0
    currency = ""
    if sale_cents > 0:
        currency = _cur(sale)
        if not currency:
            return None, [f"«{name}»: валюта цены продажи не найдена в справочнике МС"]
        if currency not in allowed:
            return None, [f"«{name}»: цена продажи в {currency} — валюта не из {', '.join(allowed)}"]

    buy = raw.get("buyPrice") if isinstance(raw.get("buyPrice"), dict) else None
    buy_cents = int(buy.get("value") or 0) if buy else 0
    cost_cents = None
    if buy_cents > 0:
        buy_cur = _cur(buy)
        if not currency:
            if buy_cur in allowed:
                currency, cost_cents = buy_cur, buy_cents
            else:
                issues.append(f"«{name}»: закупочная цена в {buy_cur or '?'} — валюта не из "
                              f"{', '.join(allowed)}")
        elif buy_cur == currency:
            cost_cents = buy_cents
        else:
            issues.append(f"«{name}»: закупочная цена в {buy_cur or '?'}, а продажа в "
                          f"{currency} — закупочная не перенесена")

    wholesale_cents = None
    for sp in sale_prices[1:]:
        type_name = str(((sp.get("priceType") or {}).get("name")) or "")
        value = int(sp.get("value") or 0)
        if "опт" not in type_name.lower() or value <= 0:
            continue
        w_cur = _cur(sp)
        if currency and w_cur == currency:
            wholesale_cents = value
        else:
            issues.append(f"«{name}»: «{type_name}» в {w_cur or '?'}, а продажа в "
                          f"{currency or '—'} — не перенесена")
        break

    if sale_cents <= 0 and cost_cents is None:
        return None, issues
    return {
        "sale_price_cents": sale_cents if sale_cents > 0 else None,
        "wholesale_price_cents": wholesale_cents,
        "cost_price_cents": cost_cents,
        "currency": currency,
    }, issues


async def pull_products() -> list[dict]:
    rows = await _fetch_all("entity/product")
    iso_by_id = await pull_currency_isos() if rows else {}

    out = []
    price_types: set[str] = set()
    for r in rows:
        ms_id = _meta_id(r)
        if not ms_id:
            continue
        for sp in r.get("salePrices") or []:
            if isinstance(sp, dict):
                price_types.add(str(((sp.get("priceType") or {}).get("name")) or "—"))
        price, price_issues = extract_price(r, iso_by_id)
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
                "price": price,
                "price_issues": price_issues,
            }
        )
    if price_types:
        logger.info("Типы цен в МС: %s (цена продажи — первый)", ", ".join(sorted(price_types)))
    return out


async def pull_counterparties() -> list[dict]:
    out = []
    for r in await _fetch_all("entity/counterparty", params={"order": "name"}):
        ms_id = _meta_id(r)
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


# ─── Правила записи (чистые функции) ──────────────────────────────────────────


def target_quantity(qty: Decimal) -> Decimal:
    """Что пишется в `stock`: минус МС → ноль (см. докстринг модуля)."""
    return qty if qty > 0 else Decimal("0")


def negative_stock(products: list[dict], stock: dict[str, Decimal]) -> list[tuple[str, Decimal]]:
    """[(имя товара, количество в МС)] — перенесённые товары с минусом в МС."""
    names = {p["ms_id"]: p["name"] for p in products}
    return [(names[m], q) for m, q in stock.items() if m in names and q < 0]


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


async def _assign_skus(txn, products: list[dict]) -> tuple[dict[str, str | None], list[str]]:
    """Артикул каждому товару переноса: {ms_id: sku | None} и список дублей.

    Первый в порядке выгрузки МС получает артикул, следующие — пусто. Занят
    живой карточкой (не из этой выгрузки) — тоже дубль. Товары выгрузки, у
    которых артикул в базе МЕНЯЕТСЯ, сначала его отпускают (UPDATE sku = NULL):
    иначе порядок UPDATE решал бы, упадёт ли UNIQUE на обмене артикулами.
    """
    batch = {p["ms_id"] for p in products}
    taken: dict[str, str] = {}  # sku → «владелец» вне выгрузки
    current: dict[str, str | None] = {}
    for r in await txn.fetch("SELECT sku, legacy_ms_id FROM products WHERE sku IS NOT NULL"):
        owner = r["legacy_ms_id"]
        if owner is not None and str(owner) in batch:
            current[str(owner)] = str(r["sku"])
        else:
            taken[str(r["sku"])] = str(owner or "живая карточка")

    assigned: dict[str, str | None] = {}
    dups: list[str] = []
    used: dict[str, str] = {}
    for p in products:
        sku = p.get("sku")
        if sku and (sku in taken or sku in used):
            who = used.get(sku) or "карточка не из переноса"
            dups.append(f"«{p['name']}»: артикул {sku} уже у «{who}» — оставлен пустым")
            sku = None
        if sku:
            used[sku] = p["name"]
        assigned[p["ms_id"]] = sku

    for ms_id, sku in current.items():
        if assigned.get(ms_id) != sku:
            await txn.execute("UPDATE products SET sku = NULL WHERE legacy_ms_id = $1", ms_id)
    return assigned, dups


async def _write_price(txn, product_id: int, name: str, price: dict, now: str) -> str:
    """Цена товара. 'written' | 'kept_manual' — правку человека не трогаем."""
    key = str(product_id)
    existing = await txn.fetchrow(
        "SELECT updated_by FROM product_prices WHERE ms_id = $1", key
    )
    values = (
        name, price["sale_price_cents"], price["cost_price_cents"],
        price["wholesale_price_cents"], price["currency"], MIGRATION_USER, now,
    )
    if existing is None:
        await txn.execute(
            "INSERT INTO product_prices (ms_id, product_name, sale_price_cents, "
            "cost_price_cents, wholesale_price_cents, currency, updated_by, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            key, *values,
        )
        return "written"
    if existing["updated_by"] not in (None, MIGRATION_USER):
        return "kept_manual"
    await txn.execute(
        "UPDATE product_prices SET product_name = $1, sale_price_cents = $2, "
        "cost_price_cents = $3, wholesale_price_cents = $4, currency = $5, "
        "updated_by = $6, updated_at = $7 WHERE ms_id = $8",
        *values, key,
    )
    return "written"


class LiveDataError(RuntimeError):
    """После переноса в базе уже идёт живая работа — снимок остатков её сотрёт."""


async def live_activity() -> dict:
    """Что изменилось в базе ПОСЛЕ первого переноса справочников.

    Граница — самый ранний `ms_id_map.migrated_at` у товаров/контрагентов:
    `_write_map` его больше не сдвигает, поэтому повторный прогон не отодвигает
    границу вперёд и не «прячет» уже случившиеся живые накладные.

    Живое — накладные не из переноса истории (`warehouse.historical_invoice_sql`) и заказы с настоящим
    автором (`user_id <> 0` и без ключа документа МС — перенесённый заказ может
    быть записан на сотрудника `--orders-owner`). Возвращает счётчики и
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
            # Заказ из переноса истории живым не считается, даже если записан на
            # сотрудника (`--orders-owner`): признак истории — ключ документа МС.
            "SELECT COUNT(*) FROM orders WHERE created_at > $1 AND user_id <> 0 "
            "AND ms_customerorder_id IS NULL AND ms_demand_id IS NULL", since
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


class _Rollback(Exception):
    """Сигнал отката для --dry-run. Не ошибка."""


async def apply_migration(
    products: list[dict],
    counterparties: list[dict],
    stock: dict[str, Decimal],
    *,
    protect_ms_ids: set[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Записать всё одной транзакцией. Частично применённой миграции не бывает.

    `protect_ms_ids` — товары, чей остаток НЕ перезаписывается снимком МС:
    по ним уже прошли живые накладные, и снимок откатил бы их.

    `dry_run` — та же запись, а в конце — сверка ВНУТРИ транзакции и откат.
    Результат сверки кладётся в `stats["verify_problems"]`.

    В `stats["issues"]` — категории отчёта: {название: [строки]}.
    """
    from services import adb_core
    from services.database import now_str

    now = now_str()
    protect = protect_ms_ids or set()
    stats: dict = {
        "products": 0, "counterparties": 0, "stock_rows": 0, "stock_skipped": 0,
        "stock_protected": 0, "stock_negative_clamped": 0,
        "stock_negative_total": Decimal("0"), "sku_duplicates": 0,
        "prices": 0, "prices_skipped": 0, "prices_kept_manual": 0,
    }
    issues: dict[str, list[str]] = {}
    stats["issues"] = issues

    try:
        async with adb_core.transaction() as txn:
            warehouse_id = await _default_warehouse_id(txn)
            skus, dups = await _assign_skus(txn, products)
            if dups:
                issues["артикул-дубль (у товара артикул оставлен пустым)"] = dups
                stats["sku_duplicates"] = len(dups)

            product_local: dict[str, int] = {}
            names: dict[str, str] = {}
            price_problems: list[str] = []
            for p in products:
                local_id = await _upsert_entity(
                    txn,
                    "products",
                    p["ms_id"],
                    {
                        "name": p["name"],
                        "category": p["category"],
                        "sku": skus.get(p["ms_id"]),
                        "unit": p["unit"],
                    },
                    now,
                )
                product_local[p["ms_id"]] = local_id
                names[p["ms_id"]] = p["name"]
                stats["products"] += 1

                price_problems.extend(p.get("price_issues") or [])
                price = p.get("price")
                if price is None:
                    if p.get("price_issues"):
                        stats["prices_skipped"] += 1
                    continue
                outcome = await _write_price(txn, local_id, p["name"], price, now)
                stats["prices" if outcome == "written" else "prices_kept_manual"] += 1
            if price_problems:
                issues["цены: не перенесено или перенесено не полностью"] = price_problems

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

            negatives: list[str] = []
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
                if qty < 0:
                    stats["stock_negative_clamped"] += 1
                    stats["stock_negative_total"] += -qty
                    negatives.append(f"«{names[ms_id]}»: в МС {qty} → записано 0")
                await txn.execute(
                    "INSERT INTO stock (product_id, warehouse_id, quantity) VALUES ($1, $2, $3) "
                    "ON CONFLICT (product_id, warehouse_id) DO UPDATE SET quantity = EXCLUDED.quantity",
                    local_id,
                    warehouse_id,
                    float(target_quantity(qty)),
                )
                stats["stock_rows"] += 1
            if negatives:
                issues["отрицательный остаток в МС — записан 0, нужен пересчёт"] = negatives

            if dry_run:
                stats["verify_problems"] = await verify(
                    products, counterparties, stock, skip_ms_ids=protect, conn=txn
                )
                raise _Rollback
    except _Rollback:
        logger.info("--dry-run: транзакция откачена, в базе ничего не изменилось")

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
    conn=None,
) -> list[str]:
    """Сверить локальную базу с выгрузкой. Пустой список — расхождений нет.

    `skip_ms_ids` — товары с живыми движениями: их остаток законно отличается
    от снимка МС, и сверять его значит получить расхождение, которое нечем
    и незачем чинить.

    Остаток сверяется с тем, что ОБЯЗАН был записать перенос: минус МС — это
    ноль (`target_quantity`). Сколько таких позиций, печатает отчёт отдельно.

    `conn` — транзакция dry-run (сверка до отката); по умолчанию своё соединение.
    """
    from services import adb_core

    db = conn if conn is not None else adb_core
    problems: list[str] = []
    skip = skip_ms_ids or set()

    local_products = await db.fetchval(
        "SELECT COUNT(*) FROM products WHERE legacy_ms_id IS NOT NULL"
    )
    if int(local_products or 0) != len(products):
        problems.append(
            f"товары: в МойСклад {len(products)}, локально {int(local_products or 0)}"
        )

    local_cp = await db.fetchval(
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
    migrated = {p["ms_id"] for p in products} - skip

    def expected(m: str) -> Decimal:
        return target_quantity(stock.get(m, Decimal("0")))

    expected_total = sum((expected(m) for m in migrated), Decimal("0"))

    rows = await db.fetch(
        "SELECT p.legacy_ms_id, s.quantity FROM stock s LEFT JOIN products p ON p.id = s.product_id"
    )
    local_total = sum(
        (_dec(r["quantity"]) for r in rows if r["legacy_ms_id"] not in skip),
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
        for r in await db.fetch(
            "SELECT p.legacy_ms_id, COALESCE(s.quantity, 0) AS quantity "
            "FROM products p LEFT JOIN stock s ON s.product_id = p.id "
            "WHERE p.legacy_ms_id IS NOT NULL"
        )
    }
    mismatched = [
        (m, expected(m), local_by_ms.get(m, Decimal("0")))
        for m in migrated
        if abs(local_by_ms.get(m, Decimal("0")) - expected(m)) > TOLERANCE
    ]
    if mismatched:
        head = ", ".join(f"{m}: МС={a}, локально={b}" for m, a, b in mismatched[:10])
        suffix = f" (ещё {len(mismatched) - 10})" if len(mismatched) > 10 else ""
        problems.append(f"остатки разошлись по {len(mismatched)} позициям: {head}{suffix}")

    # Цены продажи: у скольких перенесённых товаров она есть в МС и у скольких
    # локально. Правка человека цену не убирает, поэтому число сравнимо и после
    # повтора; отсутствие строки — это потеря прайса, её и ловим.
    ms_ids = {p["ms_id"] for p in products}
    want = sum(1 for p in products if (p.get("price") or {}).get("sale_price_cents"))
    if want:
        priced = {
            str(r["legacy_ms_id"])
            for r in await db.fetch(
                "SELECT p.legacy_ms_id FROM product_prices pp "
                "JOIN products p ON CAST(p.id AS TEXT) = pp.ms_id "
                "WHERE p.legacy_ms_id IS NOT NULL AND pp.sale_price_cents IS NOT NULL"
            )
        }
        got = len(priced & ms_ids)
        if got < want:
            problems.append(f"цены продажи: в МойСклад {want}, локально {got}")

    return problems


# ─── Отчёт ────────────────────────────────────────────────────────────────────


def print_report(stats: dict, *, dry_run: bool) -> None:
    head = "ПРЕДПРОСМОТР (в базу НЕ записано)" if dry_run else "ЗАПИСАНО"
    logger.info("═══ %s ═══", head)
    logger.info(
        "товаров %d, контрагентов %d, строк остатка %d (без карточки %d, "
        "защищено живыми движениями %d)",
        stats["products"], stats["counterparties"], stats["stock_rows"],
        stats["stock_skipped"], stats["stock_protected"],
    )
    logger.info(
        "цен записано %d (не перенесено %d, оставлена ручная правка %d)",
        stats["prices"], stats["prices_skipped"], stats["prices_kept_manual"],
    )
    if stats["stock_negative_clamped"]:
        logger.warning(
            "⚠ ОТРИЦАТЕЛЬНЫЙ ОСТАТОК в МС: %d позиций записаны нулём "
            "(всего минуса %s) — пересчитайте их на складе",
            stats["stock_negative_clamped"], stats["stock_negative_total"],
        )
    if stats["sku_duplicates"]:
        logger.warning("⚠ дублей артикула: %d (у этих товаров артикул пуст)",
                       stats["sku_duplicates"])
    for title, lines in (stats.get("issues") or {}).items():
        logger.warning("%s: %d", title, len(lines))
        for line in lines[:SHOW]:
            logger.warning("    • %s", line)
        if len(lines) > SHOW:
            logger.warning("    …и ещё %d", len(lines) - SHOW)


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
            stats = await apply_migration(products, counterparties, stock, dry_run=True)
            print_report(stats, dry_run=True)
            problems = stats.get("verify_problems") or []
            if problems:
                logger.error("СВЕРКА В ПРЕДПРОСМОТРЕ НЕ СОШЛАСЬ:")
                for p in problems:
                    logger.error("  • %s", p)
                return 1
            logger.info("--dry-run: сверка внутри транзакции сошлась, в базу ничего не записано.")
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
            print_report(stats, dry_run=False)

        if mode == "verify":
            if refusal:
                # Сверка после переключения: остаток товаров с живыми движениями
                # законно ушёл от снимка МС — это не расхождение переноса.
                protected = set(live["product_ms_ids"])
                logger.warning(
                    "После переноса была живая работа — остаток %d товаров с движениями "
                    "в сверке не участвует", len(protected),
                )
            neg = negative_stock(products, stock)
            if neg:
                logger.warning(
                    "Отрицательный остаток в МС у %d позиций — сверяется с нулём", len(neg)
                )

        logger.info("Сверка…")
        problems = await verify(products, counterparties, stock, skip_ms_ids=protected)
        if problems:
            logger.error("СВЕРКА НЕ СОШЛАСЬ — переключаться НЕЛЬЗЯ:")
            for p in problems:
                logger.error("  • %s", p)
            return 1

        logger.info("✓ Сверка сошлась: расхождений нет. Дальше — перенос истории (migrate_history_from_moysklad --dry-run).")
        return 0
    except Exception:
        logger.exception("Миграция упала — база могла остаться в прежнем состоянии (транзакция откатывается)")
        return 1
    finally:
        await close_session()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Одноразовая миграция МойСклад → локальные таблицы")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="выгрузка, запись в откатываемой транзакции и отчёт")
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
