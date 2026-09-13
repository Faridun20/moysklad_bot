"""
ОДНОРАЗОВЫЙ перенос ИСТОРИИ операций МойСклад → локальные таблицы.

Второй скрипт переноса. Первый (`scripts/migrate_from_moysklad.py`) перенёс
СПРАВОЧНИКИ и остаток на сегодня — товары, контрагентов, `stock`. Этот
переносит ДОКУМЕНТЫ за всю историю: заказы покупателей, отгрузки, входящие
платежи, и выводит из них реальный статус оплаты по каждому заказу.

Что переносит:
    entity/customerorder → orders + order_items + order_item_products
    entity/demand        → invoices + invoice_items + order_shipment
    entity/paymentin     → payments (payments.order_id — привязка к заказу)

Использование:
    python -m scripts.migrate_history_from_moysklad --dry-run   # выгрузка + отчёт
    python -m scripts.migrate_history_from_moysklad --apply     # выгрузка + запись + сверка

Код возврата: 0 — успех; 1 — ошибка или сверка не сошлась.

═══════════════════════════════════════════════════════════════════════════
РЕШЕНИЯ, БЕЗ КОТОРЫХ ЭТОТ СКРИПТ ЧИТАЕТСЯ НЕПРАВИЛЬНО
═══════════════════════════════════════════════════════════════════════════

**ОСТАТКИ НЕ ДВИГАЕМ. ВООБЩЕ.** Это главное. `stock` уже перенесён первым
скриптом как снимок НА СЕГОДНЯ — то есть он уже учитывает все исторические
отгрузки. Если провести их повторно через `warehouse.create_invoice`, каждая
спишет товар второй раз, и остаток уедет в глубокий минус. Поэтому строки
`invoices`/`invoice_items` пишутся НАПРЯМУЮ, минуя `services/warehouse.py`:
здесь нужен исторический документ, а не движение склада. Единственное место
в проекте, где запись в накладные идёт мимо warehouse, и причина ровно эта.

**НЕ УГАДЫВАЕМ.** Отгрузка без заказа, платёж без документа-основания,
позиция без карточки товара, контрагент, которого нет в справочнике, —
всё это попадает в отчёт отдельной категорией `unmatched`, а не
привязывается к первому подходящему и не пропускается молча. Это
исторические финансовые данные: неверная привязка хуже отсутствующей,
потому что она выглядит достоверной.

**ИДЕМПОТЕНТНОСТЬ — по родным ms-полям, а не по новому `legacy_ms_id`.**
В схеме уже есть `orders.ms_customerorder_id`, `orders.ms_demand_id`,
`payments.ms_paymentin_id` (у последнего — партиальный UNIQUE). Это остатки
прежней интеграции, и для переноса они ровно то, что нужно: повторный прогон
находит по ним уже созданную строку и обновляет её, а не плодит вторую.

**НОМЕРА НАКЛАДНЫХ — СВОЯ СЕРИЯ.** Исторические накладные нумеруются
`MS-D-<номер документа в МС>` и НЕ трогают `invoice_counters`: иначе перенос
съел бы номера у живой нумерации, и следующая накладная, выписанная людьми,
получила бы номер из середины истории.

**ЗАКАЗ ↔ ОТГРУЗКА: СВЯЗЬ ОДИН-К-ОДНОМУ, А В МС ОДИН-КО-МНОГИМ.**
`order_shipment.order_id` — PRIMARY KEY, то есть у заказа ровно одна строка
отгрузки. В МойСклад по одному заказу может быть несколько demand'ов
(частичные отгрузки). Все они переносятся в `invoices` (состав сохраняется
полностью, каждая со своим номером), но в `order_shipment` попадает
ПЕРВАЯ по дате, а заказы с несколькими отгрузками выводятся отдельным
счётчиком `multi_demand_orders` — чтобы это не выглядело потерей данных.

**СТАТУС ЗАКАЗА ВЫВОДИМ ИЗ ФАКТОВ, А НЕ ИЗ `state` МС.** Названия статусов
в МС задаёт аккаунт, они произвольные («Согласован», «В работе»…) и в нашу
FSM не отображаются однозначно. Поэтому: есть отгрузка → `shipped`, нет →
`approved`. Имя MS-статуса кладём в `orders.comment` — не для логики, а
чтобы при ручном разборе было видно, чем документ был в МС.

**ОПЛАЧЕН / В ДОЛГ — ПО `payedSum` ПРОТИВ `sum`.** Заказ, у которого
оплачено меньше суммы, переносится как `payment_type='credit'` с пустым
`paid_confirmed_at` — именно так он попадёт в список должников. Полностью
оплаченный — `paid`, с проставленными `paid_at`/`paid_confirmed_at`.
`due_date` НЕ выдумываем: в МС нет поля «срок оплаты» (`deliveryPlannedMoment`
— это плановая ОТГРУЗКА, другое), а подставить туда дату документа значит
объявить просроченным всё подряд.

**`user_id = 0`** у исторических заказов: в МС нет нашего Telegram-id, а
приписать их живому менеджеру значит испортить его статистику продаж.
Ноль честно означает «заказ приехал миграцией». Руководство видит такие
заказы через `get_all_orders`, в «свои» они не попадают ни к кому.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import defaultdict

import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ms_history")


# ─── HTTP-клиент МойСклад ─────────────────────────────────────────────────────
#
# Свой, как и у первого скрипта: интеграции в коде больше нет, импортировать
# неоткуда. Отличие от первого — темп запросов. Тот выкачивал три справочника
# десятком запросов, здесь же история в тысячи документов, и в бюджет лимитов
# надо укладываться осознанно (MS_API_GAPS.md §1).

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"
_MS_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MS_MAX_RETRIES = 6

# Бюджет токена ПОЛЬЗОВАТЕЛЯ (MS_API_GAPS.md §1): с 01.09.2026 вес запроса 3 →
# 15 запросов за 3 секунды, с 01.12.2026 вес 4 → 11. Держим 3 запроса в секунду
# с запасом: скрипт всё равно упирается не в темп, а в размер истории, а 429
# на середине переноса дороже лишней минуты. Параллельности нет вовсе — лимит
# 5 одновременных запросов соблюдается тем, что запрос всегда один.
_MIN_INTERVAL_SEC = 1.0 / 3.0
_last_request_at = 0.0

_session: aiohttp.ClientSession | None = None


async def _ms_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        token = os.getenv("MS_TOKEN", "")
        if not token:
            raise RuntimeError("MS_TOKEN не задан — выгружать нечем")
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
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
    """GET к МойСклад с темпом и ретраем.

    `X-Lognex-Retry-After` — в МИЛЛИСЕКУНДАХ (заголовка `Retry-After` у МС нет
    вовсе, MS_API_GAPS.md §1). `X-RateLimit-Remaining` читаем проактивно: когда
    остаток мал, тормозим ДО отказа, а не после.
    """
    global _last_request_at
    sess = await _ms_session()
    url = f"{MS_BASE}/{path}"
    delay = 2.0
    for attempt in range(_MS_MAX_RETRIES):
        gap = time.monotonic() - _last_request_at
        if gap < _MIN_INTERVAL_SEC:
            await asyncio.sleep(_MIN_INTERVAL_SEC - gap)
        _last_request_at = time.monotonic()

        async with sess.get(url, params=params) as resp:
            if resp.status in _MS_RETRY_STATUSES and attempt < _MS_MAX_RETRIES - 1:
                wait = delay
                raw = resp.headers.get("X-Lognex-Retry-After")
                if raw and raw.isdigit():
                    wait = max(wait, int(raw) / 1000.0)
                logger.warning("МойСклад HTTP %s — жду %.1f с (%s)", resp.status, wait, path)
                await asyncio.sleep(wait)
                delay *= 2
                continue
            resp.raise_for_status()
            data = await resp.json()

        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining and remaining.isdigit() and int(remaining) <= 2:
            # Бюджет на исходе — переждём окно, не дожидаясь 429.
            await asyncio.sleep(1.5)
        return data
    raise RuntimeError(f"МойСклад не ответил после {_MS_MAX_RETRIES} попыток: {path}")


async def fetch_paged(path: str, params: dict | None = None, page: int = 100) -> list[dict]:
    """Постранично выкачать коллекцию.

    `page=100`, а не 1000: `expand` работает ТОЛЬКО при `limit <= 100`, иначе
    МС молча его игнорирует и позиции не приедут вовсе (MS_API_GAPS.md §3).
    Молча — то есть ошибки не будет, просто заказы окажутся без строк.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        p = dict(params or {})
        p.update({"limit": page, "offset": offset})
        data = await ms_get(path, params=p)
        chunk = data if isinstance(data, list) else data.get("rows", [])
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
        if offset and offset % 1000 == 0:
            logger.info("  …%s: выгружено %d", path, len(rows))
    return rows


async def _positions_of(entity: str, doc: dict) -> list[dict]:
    """Позиции документа, с дотягиванием хвоста.

    Вложенный `positions` — это MetaArray со СВОИМ лимитом: в списочном ответе
    он равен 100. Документ со 120 строками приедет с 20 потерянными, и никакой
    ошибки при этом не будет (MS_API_GAPS.md §3.1). Поэтому сверяем
    `positions.meta.size` с числом пришедших строк и хвост дотягиваем.
    """
    pos = doc.get("positions") or {}
    rows = list(pos.get("rows") or [])
    size = int((pos.get("meta") or {}).get("size") or len(rows))
    if len(rows) >= size:
        return rows
    doc_id = doc.get("id")
    logger.info("  документ %s: позиций %d из %d — дотягиваю", doc_id, len(rows), size)
    return await fetch_paged(
        f"entity/{entity}/{doc_id}/positions", {"expand": "assortment"}, page=100
    )


# ─── Выгрузка ─────────────────────────────────────────────────────────────────


def _href_id(meta: dict | None) -> str:
    """UUID из `meta.href`. Пусто — значит связи нет, и это не ошибка вызова."""
    href = ((meta or {}).get("meta") or {}).get("href") or ""
    return href.rstrip("/").rsplit("/", 1)[-1].split("?")[0] if href else ""


async def pull_orders() -> list[dict]:
    logger.info("Выгружаю заказы покупателей (вся история)…")
    rows = await fetch_paged(
        "entity/customerorder",
        {"expand": "agent,state,positions.assortment", "order": "moment,asc"},
    )
    out = []
    for o in rows:
        out.append(
            {
                "ms_id": o["id"],
                "name": o.get("name") or "",
                "moment": o.get("moment") or "",
                "agent_ms_id": _href_id(o.get("agent")),
                "agent_name": ((o.get("agent") or {}).get("name")) or "",
                "state_name": ((o.get("state") or {}).get("name")) or "",
                "sum_minor": int(o.get("sum") or 0),
                "payed_minor": int(o.get("payedSum") or 0),
                "shipped_minor": int(o.get("shippedSum") or 0),
                "currency": ((o.get("rate") or {}).get("currency") or {}).get("name") or "",
                "description": o.get("description") or "",
                "positions": await _positions_of("customerorder", o),
            }
        )
    logger.info("Заказов: %d", len(out))
    return out


async def pull_demands() -> list[dict]:
    logger.info("Выгружаю отгрузки (вся история)…")
    rows = await fetch_paged(
        "entity/demand",
        {"expand": "agent,customerOrder,positions.assortment", "order": "moment,asc"},
    )
    out = []
    for d in rows:
        out.append(
            {
                "ms_id": d["id"],
                "name": d.get("name") or "",
                "moment": d.get("moment") or "",
                "agent_ms_id": _href_id(d.get("agent")),
                "agent_name": ((d.get("agent") or {}).get("name")) or "",
                "order_ms_id": _href_id(d.get("customerOrder")),
                "sum_minor": int(d.get("sum") or 0),
                "currency": ((d.get("rate") or {}).get("currency") or {}).get("name") or "",
                "positions": await _positions_of("demand", d),
            }
        )
    logger.info("Отгрузок: %d", len(out))
    return out


async def pull_payments() -> list[dict]:
    logger.info("Выгружаю входящие платежи (вся история)…")
    rows = await fetch_paged(
        "entity/paymentin", {"expand": "agent,operations", "order": "moment,asc"}
    )
    out = []
    for p in rows:
        ops = p.get("operations") or []
        # operations — массив ссылок на документы-основания (заказ и/или
        # отгрузка). Собираем ВСЕ: какой из них наш, решает сопоставление ниже.
        op_ids: list[tuple[str, str]] = []
        for op in ops:
            meta = op.get("meta") or {}
            oid = _href_id(op)
            if oid:
                op_ids.append((meta.get("type") or "", oid))
        out.append(
            {
                "ms_id": p["id"],
                "name": p.get("name") or "",
                "moment": p.get("moment") or "",
                "agent_ms_id": _href_id(p.get("agent")),
                "agent_name": ((p.get("agent") or {}).get("name")) or "",
                "sum_minor": int(p.get("sum") or 0),
                "currency": ((p.get("rate") or {}).get("currency") or {}).get("name") or "",
                "purpose": p.get("paymentPurpose") or "",
                "operations": op_ids,
            }
        )
    logger.info("Платежей: %d", len(out))
    return out


# ─── Сопоставление с уже перенесёнными справочниками ──────────────────────────


async def load_maps(txn) -> tuple[dict[str, int], dict[str, int]]:
    """UUID МойСклад → локальный id, для товаров и контрагентов.

    Источник — `legacy_ms_id`, который проставил первый скрипт переноса.
    `ms_id_map` мог бы дать то же самое, но `legacy_ms_id` лежит в самой
    таблице и под партиальным UNIQUE: если справочник переносили повторно,
    расходиться этим двум негде, а читать лучше то, на что есть ограничение.
    """
    products = {
        str(r["legacy_ms_id"]): int(r["id"])
        for r in await txn.fetch(
            "SELECT id, legacy_ms_id FROM products WHERE legacy_ms_id IS NOT NULL"
        )
    }
    counterparties = {
        str(r["legacy_ms_id"]): int(r["id"])
        for r in await txn.fetch(
            "SELECT id, legacy_ms_id FROM counterparties WHERE legacy_ms_id IS NOT NULL"
        )
    }
    if not products or not counterparties:
        raise RuntimeError(
            "В products/counterparties нет ни одной строки с legacy_ms_id. "
            "Сначала прогоните `python -m scripts.migrate_from_moysklad --apply`."
        )
    return products, counterparties


class Unmatched:
    """Всё, что не удалось сопоставить, с причиной и идентификатором.

    Отдельный класс, а не счётчики: «12 позиций не сопоставлено» бесполезно,
    разбирать руками нужно КОНКРЕТНЫЕ документы. В отчёт идут первые N каждой
    категории плюс общее число.
    """

    def __init__(self) -> None:
        self.buckets: dict[str, list[str]] = defaultdict(list)

    def add(self, kind: str, what: str) -> None:
        self.buckets[kind].append(what)

    def total(self) -> int:
        return sum(len(v) for v in self.buckets.values())

    def report(self, show: int = 10) -> list[str]:
        lines = []
        for kind in sorted(self.buckets):
            items = self.buckets[kind]
            lines.append(f"  {kind}: {len(items)}")
            for item in items[:show]:
                lines.append(f"      • {item}")
            if len(items) > show:
                lines.append(f"      …и ещё {len(items) - show}")
        return lines


# ─── Разбор позиций документа ─────────────────────────────────────────────────


def _position_rows(
    positions: list[dict], product_map: dict[str, int], doc_label: str, unmatched: Unmatched
) -> list[dict]:
    """Позиции МС → строки для записи. Несопоставленные — в `unmatched`.

    Цена в МС хранится в МИНОРНЫХ единицах валюты документа — то есть ровно в
    том же виде, что наш `price_cents`. Пересчёт через float здесь был бы
    лишним источником копеечных расхождений, поэтому берём как есть.
    """
    out = []
    for pos in positions:
        assortment = pos.get("assortment") or {}
        ms_id = assortment.get("id") or _href_id(assortment)
        name = assortment.get("name") or "—"
        qty = float(pos.get("quantity") or 0)
        price_minor = int(pos.get("price") or 0)
        product_id = product_map.get(ms_id)
        if not product_id:
            unmatched.add("позиция без карточки товара", f"{doc_label}: «{name}» ({ms_id or '—'})")
            product_id = None
        out.append(
            {
                "product_id": product_id,
                "product_name": name,
                "quantity": qty,
                "price_cents": price_minor,
                "unit": ((assortment.get("uom") or {}).get("name")) or "шт",
            }
        )
    return out


def _doc_total_cents(rows: list[dict]) -> int:
    """Сумма документа из строк — тем же правилом, что и остальной проект."""
    from services.money import mul_qty

    return sum(mul_qty(r["price_cents"], r["quantity"]) for r in rows)


# ─── Запись ───────────────────────────────────────────────────────────────────


def _ms_moment_to_local(moment: str) -> str:
    """`2026-03-14 09:20:00.000` → `2026-03-14 09:20:00`.

    Формат `now_str()` в проекте — без миллисекунд, и сравнения дат в SQL
    лексические: строка с хвостом `.000` сортировалась бы отдельно от всех
    остальных.
    """
    return (moment or "").replace("T", " ").split(".")[0].strip()


async def _base_currency() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


async def write_history(
    orders: list[dict],
    demands: list[dict],
    payments: list[dict],
    *,
    dry_run: bool,
) -> tuple[dict, Unmatched, list[str]]:
    """Перенести историю одной транзакцией. Частично применённой не бывает.

    В `--dry-run` та же транзакция открывается и в конце откатывается: так
    отчёт считается по РЕАЛЬНОЙ записи со всеми её проверками, а не по
    отдельной ветке «как будто». Ветка «как будто» неизбежно разошлась бы с
    боевой, и разошлась бы молча.
    """
    from services import adb_core
    from services.database import now_str
    from services.money import mul_qty

    now = now_str()
    base_cur = await _base_currency()
    unmatched = Unmatched()
    stats: dict[str, int] = defaultdict(int)
    problems: list[str] = []

    class _Rollback(Exception):
        """Сигнал отката для --dry-run. Не ошибка."""

    try:
        async with adb_core.transaction() as txn:
            product_map, cp_map = await load_maps(txn)
            stats["products_known"] = len(product_map)
            stats["counterparties_known"] = len(cp_map)

            # ── Заказы ───────────────────────────────────────────────
            order_local: dict[str, int] = {}
            order_total_cents: dict[str, int] = {}
            for o in orders:
                label = f"заказ {o['name'] or o['ms_id']}"
                cp_id = cp_map.get(o["agent_ms_id"])
                if not cp_id:
                    unmatched.add(
                        "заказ: контрагента нет в справочнике",
                        f"{label} — «{o['agent_name'] or '—'}» ({o['agent_ms_id'] or 'без agent'})",
                    )
                rows = _position_rows(o["positions"], product_map, label, unmatched)
                total_cents = _doc_total_cents(rows)
                order_total_cents[o["ms_id"]] = total_cents

                currency = (o["currency"] or base_cur).upper()
                moment = _ms_moment_to_local(o["moment"])
                fully_paid = o["payed_minor"] >= o["sum_minor"] > 0
                # Курс фиксируем только когда он заведомо 1: валюта документа
                # совпадает с базовой. Для остальных оставляем NULL — курс на
                # дату операции знает `tasks/run_fx_sync --backfill`, а
                # выдуманный здесь коэффициент разошёлся бы с ним молча.
                fx = 1.0 if currency == base_cur else None

                order_id = await _upsert_order(
                    txn,
                    o,
                    cp_id=cp_id,
                    currency=currency,
                    moment=moment,
                    fully_paid=fully_paid,
                    fx=fx,
                    now=now,
                )
                order_local[o["ms_id"]] = order_id
                stats["orders"] += 1
                stats["orders_credit" if not fully_paid else "orders_paid"] += 1

                await txn.execute("DELETE FROM order_item_products WHERE order_id = $1", order_id)
                await txn.execute("DELETE FROM order_items WHERE order_id = $1", order_id)
                for r in rows:
                    await txn.execute(
                        "INSERT INTO order_items "
                        "(order_id, product_name, product_href, quantity, unit, price_cents) "
                        "VALUES ($1, $2, '', $3, $4, $5)",
                        order_id, r["product_name"], r["quantity"], r["unit"], r["price_cents"],
                    )
                    stats["order_items"] += 1
                    if r["product_id"]:
                        item_id = await txn.fetchval(
                            "SELECT id FROM order_items WHERE order_id = $1 ORDER BY id DESC", order_id
                        )
                        await txn.execute(
                            "INSERT INTO order_item_products "
                            "(item_id, order_id, product_id, created_at) VALUES ($1, $2, $3, $4)",
                            int(item_id), order_id, r["product_id"], now,
                        )
                        stats["order_items_linked"] += 1

            # ── Отгрузки ─────────────────────────────────────────────
            demands_by_order: dict[str, list[dict]] = defaultdict(list)
            for d in demands:
                label = f"отгрузка {d['name'] or d['ms_id']}"
                if not d["order_ms_id"]:
                    unmatched.add(
                        "отгрузка без заказа-основания",
                        f"{label} от {d['moment'][:10]} — «{d['agent_name'] or '—'}», "
                        f"сумма {d['sum_minor'] / 100:.2f}",
                    )
                    continue
                if d["order_ms_id"] not in order_local:
                    unmatched.add(
                        "отгрузка: заказ-основание не перенесён",
                        f"{label} → заказ {d['order_ms_id']}",
                    )
                    continue
                demands_by_order[d["order_ms_id"]].append(d)

            shipped_cents: dict[str, int] = defaultdict(int)
            for ms_order_id, group in demands_by_order.items():
                group.sort(key=lambda x: x["moment"])
                if len(group) > 1:
                    stats["multi_demand_orders"] += 1
                order_id = order_local[ms_order_id]
                first_invoice_id = None
                for d in group:
                    label = f"отгрузка {d['name'] or d['ms_id']}"
                    rows = _position_rows(d["positions"], product_map, label, unmatched)
                    invoice_id = await _write_invoice(
                        txn,
                        d,
                        rows,
                        counterparty_id=cp_map.get(d["agent_ms_id"]),
                        order_id=order_id,
                        base_cur=base_cur,
                        now=now,
                    )
                    shipped_cents[ms_order_id] += sum(
                        mul_qty(r["price_cents"], r["quantity"]) for r in rows
                    )
                    stats["demands"] += 1
                    stats["demand_items"] += len(rows)
                    if first_invoice_id is None:
                        first_invoice_id = invoice_id

                await txn.execute("DELETE FROM order_shipment WHERE order_id = $1", order_id)
                await txn.execute(
                    "INSERT INTO order_shipment (order_id, invoice_id, shipped_at) "
                    "VALUES ($1, $2, $3)",
                    order_id, first_invoice_id, _ms_moment_to_local(group[0]["moment"]),
                )
                await txn.execute(
                    "UPDATE orders SET status = 'shipped', shipped_at = $1, "
                    "ms_demand_id = $2, updated_at = $3 WHERE id = $4",
                    _ms_moment_to_local(group[0]["moment"]), group[0]["ms_id"], now, order_id,
                )

            # ── Платежи ──────────────────────────────────────────────
            paid_cents: dict[str, int] = defaultdict(int)
            for p in payments:
                label = f"платёж {p['name'] or p['ms_id']} от {p['moment'][:10]}"
                target = None
                for op_type, op_id in p["operations"]:
                    if op_type == "customerorder" and op_id in order_local:
                        target = op_id
                        break
                if target is None:
                    # Второй заход: платёж мог быть привязан к ОТГРУЗКЕ, а не к
                    # заказу — в МС законны оба варианта. Через отгрузку выходим
                    # на её заказ.
                    by_demand = {d["ms_id"]: d["order_ms_id"] for d in demands}
                    for op_type, op_id in p["operations"]:
                        if op_type == "demand" and by_demand.get(op_id) in order_local:
                            target = by_demand[op_id]
                            break
                if target is None:
                    reason = (
                        "платёж без документа-основания"
                        if not p["operations"]
                        else "платёж: основание не сопоставлено с заказом"
                    )
                    unmatched.add(
                        reason,
                        f"{label} — «{p['agent_name'] or '—'}», сумма {p['sum_minor'] / 100:.2f}",
                    )
                    stats["payments_unlinked"] += 1
                    continue

                await _upsert_payment(
                    txn, p, order_id=order_local[target], base_cur=base_cur, now=now
                )
                paid_cents[target] += p["sum_minor"]
                stats["payments"] += 1

            problems = _check_consistency(
                orders, order_total_cents, shipped_cents, paid_cents, order_local
            )
            if dry_run:
                raise _Rollback
    except _Rollback:
        logger.info("--dry-run: транзакция откачена, в базе ничего не изменилось")

    stats["unmatched"] = unmatched.total()
    return dict(stats), unmatched, problems


async def _upsert_order(
    txn, o: dict, *, cp_id: int | None, currency: str, moment: str,
    fully_paid: bool, fx: float | None, now: str,
) -> int:
    """Заказ по `ms_customerorder_id`. Повторный прогон обновляет, не дублирует."""
    existing = await txn.fetchval(
        "SELECT id FROM orders WHERE ms_customerorder_id = $1", o["ms_id"]
    )
    # Имя MS-статуса — в комментарий: на логику не влияет (статус выводим из
    # фактов), но при ручном разборе объясняет, чем документ был в МС.
    comment_bits = [b for b in (o.get("description") or "", o["state_name"]) if b]
    comment = " · ".join(["Перенос из МойСклад", *comment_bits])[:1000]
    fields = {
        "user_id": 0,
        "full_name": "Перенос из МойСклад",
        "status": "approved",
        "comment": comment,
        "agent_id": str(cp_id) if cp_id else None,
        "agent_name": o["agent_name"] or None,
        "currency": currency,
        "payment_type": "paid" if fully_paid else "credit",
        "paid_at": moment if fully_paid else None,
        "paid_confirmed_at": moment if fully_paid else None,
        "paid_confirmed_by": 0 if fully_paid else None,
        "paid_confirmed_by_name": "Перенос из МойСклад" if fully_paid else None,
        "payment_confirmed": 1 if fully_paid else 0,
        "payment_confirmed_at": moment if fully_paid else None,
        "submitted_at": moment,
        "fx_rate_to_base": fx,
        "updated_at": now,
    }
    cols = list(fields)
    if existing is not None:
        assignments = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        await txn.execute(
            f"UPDATE orders SET {assignments} WHERE id = ${len(cols) + 1}",
            *[fields[c] for c in cols], int(existing),
        )
        return int(existing)
    all_cols = cols + ["ms_customerorder_id", "created_at"]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(all_cols)))
    await txn.execute(
        f"INSERT INTO orders ({', '.join(all_cols)}) VALUES ({placeholders})",
        *[fields[c] for c in cols], o["ms_id"], moment or now,
    )
    new_id = await txn.fetchval(
        "SELECT id FROM orders WHERE ms_customerorder_id = $1", o["ms_id"]
    )
    return int(new_id)


async def _write_invoice(
    txn, d: dict, rows: list[dict], *, counterparty_id: int | None,
    order_id: int, base_cur: str, now: str,
) -> int:
    """Историческая расходная накладная. ОСТАТОК НЕ ДВИГАЕТ.

    Пишем напрямую, минуя `services/warehouse.py`: там каждая проведённая
    накладная сдвигает `stock`, а он уже перенесён снимком на сегодня и все
    эти отгрузки в себя включает. Повторное списание увело бы остаток в минус
    на весь исторический оборот.

    Номер — своей серией `MS-D-<номер в МС>`, `invoice_counters` не трогаем.
    """
    from services.money import mul_qty

    number = f"MS-D-{d['name'] or d['ms_id'][:8]}"
    existing = await txn.fetchval("SELECT id FROM invoices WHERE invoice_number = $1", number)
    if existing is not None:
        await txn.execute("DELETE FROM invoice_items WHERE invoice_id = $1", int(existing))
        invoice_id = int(existing)
        await txn.execute(
            "UPDATE invoices SET counterparty_id = $1, invoice_date = $2, "
            "currency = $3, total_amount_cents = $4, comment = $5 WHERE id = $6",
            counterparty_id,
            _ms_moment_to_local(d["moment"])[:10],
            (d["currency"] or base_cur).upper(),
            sum(mul_qty(r["price_cents"], r["quantity"]) for r in rows),
            f"Перенос из МойСклад · заказ #{order_id}",
            invoice_id,
        )
    else:
        warehouse_id = await txn.fetchval("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        if warehouse_id is None:
            raise RuntimeError("Нет ни одного склада — прогоните `python -m tasks.migrate`")
        await txn.execute(
            "INSERT INTO invoices (type, counterparty_id, warehouse_id, invoice_number, "
            "invoice_date, status, currency, total_amount_cents, comment, created_by, created_at) "
            "VALUES ('outgoing', $1, $2, $3, $4, 'confirmed', $5, $6, $7, 0, $8)",
            counterparty_id,
            int(warehouse_id),
            number,
            _ms_moment_to_local(d["moment"])[:10],
            (d["currency"] or base_cur).upper(),
            sum(mul_qty(r["price_cents"], r["quantity"]) for r in rows),
            f"Перенос из МойСклад · заказ #{order_id}",
            _ms_moment_to_local(d["moment"]) or now,
        )
        invoice_id = int(
            await txn.fetchval("SELECT id FROM invoices WHERE invoice_number = $1", number)
        )

    for r in rows:
        if not r["product_id"]:
            # Строку без карточки товара в накладную не пишем: product_id в
            # invoice_items NOT NULL, а выдуманная привязка — это приход не на
            # ту карточку. Она уже в `unmatched`, разбирается руками.
            continue
        await txn.execute(
            "INSERT INTO invoice_items (invoice_id, product_id, quantity, price_cents) "
            "VALUES ($1, $2, $3, $4)",
            invoice_id, r["product_id"], r["quantity"], r["price_cents"],
        )
    return invoice_id


async def _upsert_payment(txn, p: dict, *, order_id: int, base_cur: str, now: str) -> None:
    """Платёж по `ms_paymentin_id` (партиальный UNIQUE — идемпотентность в схеме).

    Статус `confirmed`: деньги в МойСклад уже проведены, и переносить их как
    `pending` значило бы выставить всю историю на повторное подтверждение боссу.
    """
    moment = _ms_moment_to_local(p["moment"])
    existing = await txn.fetchval(
        "SELECT id FROM payments WHERE ms_paymentin_id = $1", p["ms_id"]
    )
    currency = (p["currency"] or base_cur).upper()
    fx = 1.0 if currency == base_cur else None
    comment = (f"Перенос из МойСклад · {p['purpose']}" if p["purpose"] else "Перенос из МойСклад")[:500]
    if existing is not None:
        await txn.execute(
            "UPDATE payments SET order_id = $1, amount_cents = $2, currency = $3, "
            "comment = $4, status = 'confirmed', confirmed_at = $5, fx_rate_to_base = $6 "
            "WHERE id = $7",
            order_id, p["sum_minor"], currency, comment, moment, fx, int(existing),
        )
        return
    await txn.execute(
        "INSERT INTO payments (user_id, username, full_name, amount_cents, currency, "
        "comment, status, order_id, ms_paymentin_id, fx_rate_to_base, created_at, confirmed_at) "
        "VALUES (0, '', 'Перенос из МойСклад', $1, $2, $3, 'confirmed', $4, $5, $6, $7, $8)",
        p["sum_minor"], currency, comment, order_id, p["ms_id"], fx, moment or now, moment,
    )


# ─── Сверка ───────────────────────────────────────────────────────────────────


def _check_consistency(
    orders: list[dict],
    order_total_cents: dict[str, int],
    shipped_cents: dict[str, int],
    paid_cents: dict[str, int],
    order_local: dict[str, int],
) -> list[str]:
    """Логические проверки поверх перенесённого.

    Отгружено больше, чем заказано, или оплачено больше, чем выставлено, —
    это не «бывает», а признак того, что документ привязан не к тому заказу.
    Такое обязано быть видно СРАЗУ: разбирать потом, по остаткам и долгам,
    в разы дороже.
    """
    problems: list[str] = []
    by_ms = {o["ms_id"]: o for o in orders}
    for ms_id, total in order_total_cents.items():
        o = by_ms.get(ms_id, {})
        label = f"заказ {o.get('name') or ms_id} (#{order_local.get(ms_id, '?')})"
        shipped = shipped_cents.get(ms_id, 0)
        if total and shipped > total:
            problems.append(
                f"{label}: отгружено {shipped / 100:.2f} > заказано {total / 100:.2f}"
            )
        paid = paid_cents.get(ms_id, 0)
        if total and paid > total:
            problems.append(
                f"{label}: оплачено {paid / 100:.2f} > сумма заказа {total / 100:.2f}"
            )
        ms_sum = int(o.get("sum_minor") or 0)
        if ms_sum and abs(ms_sum - total) > 1:
            problems.append(
                f"{label}: сумма позиций {total / 100:.2f} расходится с суммой "
                f"документа в МС {ms_sum / 100:.2f}"
            )
    return problems


# ─── Отчёт и CLI ──────────────────────────────────────────────────────────────


def _money(minor: int) -> str:
    return f"{minor / 100:,.2f}".replace(",", " ")


def print_report(
    orders: list[dict], demands: list[dict], payments: list[dict],
    stats: dict, unmatched: Unmatched, problems: list[str], *, dry_run: bool,
) -> None:
    head = "ПРЕДПРОСМОТР (в базу НЕ записано)" if dry_run else "ПЕРЕНЕСЕНО"
    ms_orders_sum = sum(int(o["sum_minor"]) for o in orders)
    ms_demands_sum = sum(int(d["sum_minor"]) for d in demands)
    ms_payments_sum = sum(int(p["sum_minor"]) for p in payments)

    logger.info("")
    logger.info("═══ %s ═══", head)
    logger.info("Из МойСклад выгружено:")
    logger.info("  заказов покупателей : %5d  на %s", len(orders), _money(ms_orders_sum))
    logger.info("  отгрузок            : %5d  на %s", len(demands), _money(ms_demands_sum))
    logger.info("  входящих платежей   : %5d  на %s", len(payments), _money(ms_payments_sum))
    logger.info("")
    logger.info("Записано в локальные таблицы:")
    logger.info("  orders              : %5d  (оплачено %d · в долг %d)",
                stats.get("orders", 0), stats.get("orders_paid", 0), stats.get("orders_credit", 0))
    logger.info("  order_items         : %5d  (с карточкой товара %d)",
                stats.get("order_items", 0), stats.get("order_items_linked", 0))
    logger.info("  invoices (отгрузки) : %5d  (позиций %d)",
                stats.get("demands", 0), stats.get("demand_items", 0))
    logger.info("  payments            : %5d  (не привязано %d)",
                stats.get("payments", 0), stats.get("payments_unlinked", 0))
    if stats.get("multi_demand_orders"):
        logger.info("")
        logger.info("  ⚠ заказов с НЕСКОЛЬКИМИ отгрузками: %d", stats["multi_demand_orders"])
        logger.info("    order_shipment хранит одну строку на заказ (PK) — туда попала")
        logger.info("    первая по дате. Сами накладные перенесены все, состав не потерян.")

    if unmatched.total():
        logger.info("")
        logger.warning("НЕ СОПОСТАВЛЕНО — %d (разбирать руками, НЕ угадано):", unmatched.total())
        for line in unmatched.report():
            logger.warning("%s", line)

    if problems:
        logger.info("")
        logger.error("СВЕРКА НЕ СОШЛАСЬ — %d расхождений:", len(problems))
        for line in problems[:20]:
            logger.error("  • %s", line)
        if len(problems) > 20:
            logger.error("  …и ещё %d", len(problems) - 20)
    else:
        logger.info("")
        logger.info("✓ Сверка сошлась: отгружено ≤ заказано, оплачено ≤ выставлено,")
        logger.info("  суммы позиций совпадают с суммами документов МойСклад.")


async def show_debtors() -> None:
    """Должники ПОСЛЕ переноса — для ручной сверки «помню по паре контрагентов».

    Сумма заказа считается из позиций (`orders.total_amount` в схеме нет —
    сумма выводится из `order_items`, как и во всём проекте).
    """
    from services import adb_core
    from services.debts import SUM_ORDER_TOTAL_CENTS

    rows = await adb_core.fetch(
        "SELECT o.id, o.agent_name, o.currency, o.created_at, "
        f"  (SELECT {SUM_ORDER_TOTAL_CENTS} FROM order_items WHERE order_id = o.id) AS total_cents, "
        "  (SELECT COALESCE(SUM(amount_cents), 0) FROM payments "
        "     WHERE order_id = o.id AND status = 'confirmed') AS paid_cents "
        "FROM orders o "
        "WHERE o.payment_type = 'credit' AND o.paid_confirmed_at IS NULL "
        "ORDER BY o.agent_name, o.created_at"
    )
    logger.info("")
    logger.info("═══ ДОЛЖНИКИ ПОСЛЕ ПЕРЕНОСА ═══")
    if not rows:
        logger.info("  (пусто)")
        return
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_agent[str(r["agent_name"] or "— без контрагента —")].append(dict(r))
    grand: dict[str, int] = defaultdict(int)
    for agent in sorted(by_agent):
        items = by_agent[agent]
        logger.info("")
        logger.info("%s — заказов %d:", agent, len(items))
        for r in items:
            debt = int(r["total_cents"] or 0) - int(r["paid_cents"] or 0)
            cur = str(r["currency"] or "")
            grand[cur] += debt
            logger.info(
                "   заказ #%-6s %s  долг %s %s  (сумма %s, оплачено %s)",
                r["id"], str(r["created_at"])[:10], _money(debt), cur,
                _money(int(r["total_cents"] or 0)), _money(int(r["paid_cents"] or 0)),
            )
    logger.info("")
    logger.info("ИТОГО долг по валютам:")
    for cur, total in sorted(grand.items()):
        logger.info("  %s: %s", cur or "—", _money(total))


async def show_totals() -> None:
    """Сводка по типам оплаты. Суммы — из позиций, не из несуществующей колонки."""
    from services import adb_core
    from services.debts import SUM_ORDER_TOTAL_CENTS

    rows = await adb_core.fetch(
        "SELECT o.payment_type, COUNT(*) AS cnt, "
        f"  COALESCE(SUM((SELECT {SUM_ORDER_TOTAL_CENTS} FROM order_items "
        "     WHERE order_id = o.id)), 0) AS total_cents "
        "FROM orders o GROUP BY o.payment_type ORDER BY o.payment_type"
    )
    logger.info("")
    logger.info("═══ ЗАКАЗЫ ПО ТИПУ ОПЛАТЫ ═══")
    for r in rows:
        logger.info(
            "  %-8s заказов %5d  на сумму %s",
            r["payment_type"], int(r["cnt"]), _money(int(r["total_cents"] or 0)),
        )


async def main(mode: str) -> int:
    try:
        orders = await pull_orders()
        demands = await pull_demands()
        payments = await pull_payments()

        stats, unmatched, problems = await write_history(
            orders, demands, payments, dry_run=(mode == "dry-run")
        )
        print_report(
            orders, demands, payments, stats, unmatched, problems,
            dry_run=(mode == "dry-run"),
        )

        if mode == "dry-run":
            logger.info("")
            logger.info("Это предпросмотр. Для записи: --apply")
            return 1 if problems else 0

        await show_totals()
        await show_debtors()
        if problems:
            logger.error("")
            logger.error("Перенос ВЫПОЛНЕН, но сверка нашла расхождения — разберите список выше.")
            return 1
        return 0
    except Exception:
        logger.exception("Перенос упал — транзакция откатилась, база в прежнем состоянии")
        return 1
    finally:
        await close_session()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Одноразовый перенос истории операций МойСклад → локальные таблицы"
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="выгрузка и отчёт, без записи")
    g.add_argument("--apply", action="store_true", help="выгрузка, запись и сверка")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    sys.exit(asyncio.run(main("dry-run" if args.dry_run else "apply")))
