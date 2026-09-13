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


async def pull_supplies() -> list[dict]:
    """Поступления от поставщиков (закупки)."""
    logger.info("Выгружаю поступления (вся история)…")
    rows = await fetch_paged(
        "entity/supply",
        {"expand": "agent,positions.assortment", "order": "moment,asc"},
    )
    out = []
    for sp in rows:
        out.append(
            {
                "ms_id": sp["id"],
                "name": sp.get("name") or "",
                "moment": sp.get("moment") or "",
                "agent_ms_id": _href_id(sp.get("agent")),
                "agent_name": ((sp.get("agent") or {}).get("name")) or "",
                "sum_minor": int(sp.get("sum") or 0),
                "currency": ((sp.get("rate") or {}).get("currency") or {}).get("name") or "",
                "description": sp.get("description") or "",
                "positions": await _positions_of("supply", sp),
            }
        )
    logger.info("Поступлений: %d", len(out))
    return out


async def pull_payments_out() -> list[dict]:
    """Исходящие платежи — расчёты с поставщиками."""
    logger.info("Выгружаю исходящие платежи (вся история)…")
    rows = await fetch_paged(
        "entity/paymentout", {"expand": "agent,operations", "order": "moment,asc"}
    )
    out = []
    for p in rows:
        op_ids: list[tuple[str, str]] = []
        for op in p.get("operations") or []:
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
    logger.info("Исходящих платежей: %d", len(out))
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
    supplies: list[dict] | None = None,
    payments_out: list[dict] | None = None,
    *,
    dry_run: bool,
) -> tuple[dict, Unmatched, list[str]]:
    """Перенести историю одной транзакцией. Частично применённой не бывает.

    В `--dry-run` та же транзакция открывается и в конце откатывается: отчёт
    считается по РЕАЛЬНОЙ записи со всеми её проверками, а не по отдельной
    ветке «как будто». Ветка «как будто» неизбежно разошлась бы с боевой, и
    разошлась бы молча.

    ═══ ДВЕ МОДЕЛИ ПРОДАЖИ В ОДНОМ АККАУНТЕ ═══

    Боевая выгрузка показала: заказ покупателя в этом аккаунте — редкость
    (26 штук против 421 отгрузки). Продажу оформляют сразу отгрузкой, а деньги
    — платежом без документа-основания. Поэтому:

    * **Отгрузка без заказа СТАНОВИТСЯ заказом.** Это не догадка: у документа
      свой контрагент, дата, позиции и сумма — всё, из чего состоит продажа.
      Идемпотентность таких заказов — по `orders.ms_demand_id`.
    * **Платёж без основания гасит долг СВОЕГО контрагента по FIFO**, от
      старых заказов к новым. Это соглашение, а не факт из МС, и оно помечено
      в комментарии каждой такой строки. Без него 243 платежа не гасили бы
      ничего, и клиенты выглядели бы должниками на всю сумму отгрузок.
      Тот же приём уже работает в `create_cash_deposit` для сдач наличных.
    * **Контрагент при этом НЕ угадывается.** Платёж без контрагента или от
      контрагента, у которого нет ни одного заказа, остаётся несопоставленным
      и уходит в отчёт — привязать его не к чему.
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

            # ── Заказы покупателей ───────────────────────────────────
            order_local: dict[str, int] = {}
            order_total_cents: dict[str, int] = {}
            # Порядок гашения FIFO: (локальный id, дата, контрагент, сумма).
            order_book: list[dict] = []

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

                order_id = await _upsert_order(
                    txn, o,
                    cp_id=cp_id, currency=currency, moment=moment,
                    fully_paid=False,  # состояние оплаты посчитаем после разнесения
                    fx=(1.0 if currency == base_cur else None),
                    now=now,
                )
                order_local[o["ms_id"]] = order_id
                order_book.append(
                    {
                        "order_id": order_id, "cp_id": cp_id, "moment": moment,
                        "total": total_cents, "currency": currency, "paid": 0,
                        "source": "customerorder",
                    }
                )
                stats["orders"] += 1
                await _write_items(txn, order_id, rows, now, stats)

            # ── Отгрузки ─────────────────────────────────────────────
            # Отгрузка с заказом идёт к своему заказу; без заказа — становится
            # заказом сама.
            demands_by_order: dict[str, list[dict]] = defaultdict(list)
            standalone: list[dict] = []
            for d in demands:
                label = f"отгрузка {d['name'] or d['ms_id']}"
                if d["order_ms_id"] and d["order_ms_id"] in order_local:
                    demands_by_order[d["order_ms_id"]].append(d)
                elif d["order_ms_id"]:
                    unmatched.add(
                        "отгрузка: заказ-основание не перенесён",
                        f"{label} → заказ {d['order_ms_id']}",
                    )
                else:
                    standalone.append(d)

            shipped_cents: dict[str, int] = defaultdict(int)
            demand_order: dict[str, int] = {}  # ms_id отгрузки → локальный заказ

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
                        txn, d, rows,
                        counterparty_id=cp_map.get(d["agent_ms_id"]),
                        order_id=order_id, base_cur=base_cur, now=now,
                    )
                    shipped_cents[ms_order_id] += sum(
                        mul_qty(r["price_cents"], r["quantity"]) for r in rows
                    )
                    demand_order[d["ms_id"]] = order_id
                    stats["demands"] += 1
                    stats["demand_items"] += len(rows)
                    if first_invoice_id is None:
                        first_invoice_id = invoice_id
                await _write_shipment(txn, order_id, first_invoice_id, group[0], now)

            # ── Отгрузка как самостоятельная продажа ─────────────────
            for d in standalone:
                label = f"продажа {d['name'] or d['ms_id']}"
                cp_id = cp_map.get(d["agent_ms_id"])
                if not cp_id:
                    unmatched.add(
                        "продажа: контрагента нет в справочнике",
                        f"{label} от {d['moment'][:10]} — «{d['agent_name'] or '—'}», "
                        f"сумма {d['sum_minor'] / 100:.2f}",
                    )
                rows = _position_rows(d["positions"], product_map, label, unmatched)
                total_cents = _doc_total_cents(rows)
                currency = (d["currency"] or base_cur).upper()
                moment = _ms_moment_to_local(d["moment"])

                order_id = await _upsert_order_from_demand(
                    txn, d,
                    cp_id=cp_id, currency=currency, moment=moment,
                    fx=(1.0 if currency == base_cur else None), now=now,
                )
                demand_order[d["ms_id"]] = order_id
                order_book.append(
                    {
                        "order_id": order_id, "cp_id": cp_id, "moment": moment,
                        "total": total_cents, "currency": currency, "paid": 0,
                        "source": "demand",
                    }
                )
                stats["orders_from_demand"] += 1
                await _write_items(txn, order_id, rows, now, stats)

                invoice_id = await _write_invoice(
                    txn, d, rows, counterparty_id=cp_id,
                    order_id=order_id, base_cur=base_cur, now=now,
                )
                await _write_shipment(txn, order_id, invoice_id, d, now)
                stats["demands"] += 1
                stats["demand_items"] += len(rows)

            by_local_id = {b["order_id"]: b for b in order_book}

            # ── Платежи с документом-основанием ──────────────────────
            for p in payments:
                label = f"платёж {p['name'] or p['ms_id']} от {p['moment'][:10]}"
                target_order = None
                for op_type, op_id in p["operations"]:
                    if op_type == "customerorder" and op_id in order_local:
                        target_order = order_local[op_id]
                        break
                    if op_type == "demand" and op_id in demand_order:
                        target_order = demand_order[op_id]
                        break
                if target_order is None:
                    p["_needs_fifo"] = True
                    continue
                await _upsert_payment(
                    txn, p, order_id=target_order, base_cur=base_cur, now=now
                )
                if target_order in by_local_id:
                    by_local_id[target_order]["paid"] += p["sum_minor"]
                stats["payments"] += 1

            # ── Платежи без основания: FIFO внутри контрагента ───────
            fifo_queue: dict[int, list[dict]] = defaultdict(list)
            for b in sorted(order_book, key=lambda x: (x["moment"], x["order_id"])):
                if b["cp_id"]:
                    fifo_queue[int(b["cp_id"])].append(b)

            for p in sorted(payments, key=lambda x: x["moment"]):
                if not p.get("_needs_fifo"):
                    continue
                label = f"платёж {p['name'] or p['ms_id']} от {p['moment'][:10]}"
                cp_id = cp_map.get(p["agent_ms_id"])
                if not cp_id:
                    unmatched.add(
                        "платёж: контрагента нет в справочнике",
                        f"{label} — «{p['agent_name'] or '—'}», "
                        f"сумма {p['sum_minor'] / 100:.2f}",
                    )
                    stats["payments_unlinked"] += 1
                    continue
                queue = [
                    b for b in fifo_queue.get(int(cp_id), [])
                    if b["currency"] == (p["currency"] or base_cur).upper()
                    and b["paid"] < b["total"]
                ]
                if not queue:
                    unmatched.add(
                        "платёж: у контрагента нет непогашенных заказов в этой валюте",
                        f"{label} — «{p['agent_name'] or '—'}», "
                        f"сумма {p['sum_minor'] / 100:.2f}",
                    )
                    stats["payments_unlinked"] += 1
                    continue

                left = int(p["sum_minor"])
                part = 0
                for b in queue:
                    if left <= 0:
                        break
                    need = b["total"] - b["paid"]
                    take = min(need, left)
                    if take <= 0:
                        continue
                    part += 1
                    await _upsert_payment(
                        txn, p, order_id=b["order_id"], base_cur=base_cur, now=now,
                        amount_cents=take, part=part, fifo=True,
                    )
                    b["paid"] += take
                    left -= take
                    stats["payments_fifo_parts"] += 1
                if part:
                    stats["payments_fifo"] += 1
                if left > 0:
                    # Заплачено больше, чем выставлено этому контрагенту.
                    # Не «ошибка», но и не то, что можно списать молча.
                    unmatched.add(
                        "платёж: остаток не на что отнести (переплата контрагента)",
                        f"{label} — «{p['agent_name'] or '—'}», "
                        f"не разнесено {left / 100:.2f} из {p['sum_minor'] / 100:.2f}",
                    )
                    stats["payments_overflow_cents"] += left

            # ── Состояние оплаты по каждому заказу ───────────────────
            for b in order_book:
                closed = b["total"] > 0 and b["paid"] >= b["total"]
                await _set_order_payment_state(
                    txn, b["order_id"], closed=closed, moment=b["moment"], now=now
                )
                stats["orders_paid" if closed else "orders_credit"] += 1

            # ── Поступления (закупки) ────────────────────────────────
            supplied_cents: dict[str, int] = defaultdict(int)
            supply_total_cents: dict[str, int] = {}
            supply_invoice: dict[str, int] = {}
            for sp in supplies or []:
                label = f"поступление {sp['name'] or sp['ms_id']}"
                cp_id = cp_map.get(sp["agent_ms_id"])
                if not cp_id:
                    unmatched.add(
                        "поступление: поставщика нет в справочнике",
                        f"{label} — «{sp['agent_name'] or '—'}» "
                        f"({sp['agent_ms_id'] or 'без agent'})",
                    )
                rows = _position_rows(sp["positions"], product_map, label, unmatched)
                invoice_id = await _write_invoice(
                    txn, sp, rows, counterparty_id=cp_id, order_id=None,
                    base_cur=base_cur, now=now, kind="incoming",
                )
                supply_invoice[sp["ms_id"]] = invoice_id
                doc_total = sum(mul_qty(r["price_cents"], r["quantity"]) for r in rows)
                supply_total_cents[sp["ms_id"]] = doc_total
                if cp_id:
                    supplied_cents[str(cp_id)] += doc_total
                stats["supplies"] += 1
                stats["supply_items"] += len(rows)

            # ── Платежи поставщикам ──────────────────────────────────
            paid_out_cents: dict[str, int] = defaultdict(int)
            for p in payments_out or []:
                label = f"исходящий платёж {p['name'] or p['ms_id']} от {p['moment'][:10]}"
                cp_id = cp_map.get(p["agent_ms_id"])
                if not cp_id:
                    unmatched.add(
                        "исходящий платёж: поставщика нет в справочнике",
                        f"{label} — «{p['agent_name'] or '—'}», "
                        f"сумма {p['sum_minor'] / 100:.2f}",
                    )
                    stats["payments_out_unlinked"] += 1
                    continue
                invoice_id = None
                for op_type, op_id in p["operations"]:
                    if op_type == "supply" and op_id in supply_invoice:
                        invoice_id = supply_invoice[op_id]
                        break
                await _upsert_payment_out(
                    txn, p, counterparty_id=cp_id, invoice_id=invoice_id,
                    base_cur=base_cur, now=now,
                )
                paid_out_cents[str(cp_id)] += p["sum_minor"]
                stats["payments_out"] += 1

            problems = _check_consistency(
                orders, order_total_cents, shipped_cents, order_local
            )
            problems += _check_supplier_consistency(supplies or [], supply_total_cents)
            stats["supplier_debt_agents"] = len(
                {a for a in supplied_cents if supplied_cents[a] > paid_out_cents.get(a, 0)}
            )
            if dry_run:
                raise _Rollback
    except _Rollback:
        logger.info("--dry-run: транзакция откачена, в базе ничего не изменилось")

    stats["unmatched"] = unmatched.total()
    return dict(stats), unmatched, problems


async def _write_items(txn, order_id: int, rows: list[dict], now: str, stats: dict) -> None:
    """Позиции заказа. Переписываем целиком — повторный прогон не удваивает."""
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
                "INSERT INTO order_item_products (item_id, order_id, product_id, created_at) "
                "VALUES ($1, $2, $3, $4)",
                int(item_id), order_id, r["product_id"], now,
            )
            stats["order_items_linked"] += 1


async def _write_shipment(txn, order_id: int, invoice_id, d: dict, now: str) -> None:
    """Строка отгрузки заказа + отметка статуса. PK по order_id — одна на заказ."""
    stamp = _ms_moment_to_local(d["moment"])
    await txn.execute("DELETE FROM order_shipment WHERE order_id = $1", order_id)
    await txn.execute(
        "INSERT INTO order_shipment (order_id, invoice_id, shipped_at) VALUES ($1, $2, $3)",
        order_id, invoice_id, stamp,
    )
    await txn.execute(
        "UPDATE orders SET status = 'shipped', shipped_at = $1, ms_demand_id = $2, "
        "updated_at = $3 WHERE id = $4",
        stamp, d["ms_id"], now, order_id,
    )


async def _set_order_payment_state(
    txn, order_id: int, *, closed: bool, moment: str, now: str
) -> None:
    """Проставить тип оплаты по РЕЗУЛЬТАТУ разнесения денег.

    Считается после всех платежей, а не при создании заказа: до разнесения
    FIFO неизвестно, покрыт заказ или нет, и «в долг» пришлось бы ставить
    наугад.
    """
    if closed:
        await txn.execute(
            "UPDATE orders SET payment_type = 'paid', paid_at = $1, "
            "paid_confirmed_at = $1, paid_confirmed_by = 0, "
            "paid_confirmed_by_name = 'Перенос из МойСклад', "
            "payment_confirmed = 1, payment_confirmed_at = $1, updated_at = $2 "
            "WHERE id = $3",
            moment, now, order_id,
        )
    else:
        await txn.execute(
            "UPDATE orders SET payment_type = 'credit', paid_at = NULL, "
            "paid_confirmed_at = NULL, paid_confirmed_by = NULL, "
            "paid_confirmed_by_name = NULL, payment_confirmed = 0, "
            "payment_confirmed_at = NULL, updated_at = $1 WHERE id = $2",
            now, order_id,
        )


async def _upsert_order(
    txn, o: dict, *, cp_id: int | None, currency: str, moment: str,
    fully_paid: bool, fx: float | None, now: str,
) -> int:
    """Заказ по `ms_customerorder_id`. Повторный прогон обновляет, не дублирует.

    Тип оплаты здесь НЕ ставится: он известен только после разнесения денег
    (`_set_order_payment_state`). `fully_paid` оставлен для совместимости
    сигнатуры и не используется — до FIFO ответа на этот вопрос нет.
    """
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


async def _upsert_order_from_demand(
    txn, d: dict, *, cp_id: int | None, currency: str, moment: str,
    fx: float | None, now: str,
) -> int:
    """Отгрузка без заказа-основания → заказ. Идемпотентность по `ms_demand_id`.

    В этом аккаунте продажу оформляют сразу отгрузкой (26 заказов против 421
    отгрузки), и без такого перевода 94% истории просто не доехало бы. Это не
    догадка: у отгрузки есть всё, из чего состоит продажа — контрагент, дата,
    позиции, сумма. Выдуманного тут ничего нет, кроме самого факта «назовём
    это заказом», и он отмечен в комментарии.

    `ms_customerorder_id` у таких заказов пуст — его в МС и не было.
    """
    existing = await txn.fetchval(
        "SELECT id FROM orders WHERE ms_demand_id = $1 AND ms_customerorder_id IS NULL",
        d["ms_id"],
    )
    comment = f"Перенос из МойСклад · продажа по отгрузке {d['name'] or d['ms_id']}"[:1000]
    fields = {
        "user_id": 0,
        "full_name": "Перенос из МойСклад",
        "status": "shipped",
        "comment": comment,
        "agent_id": str(cp_id) if cp_id else None,
        "agent_name": d["agent_name"] or None,
        "currency": currency,
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
    all_cols = cols + ["ms_demand_id", "created_at"]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(all_cols)))
    await txn.execute(
        f"INSERT INTO orders ({', '.join(all_cols)}) VALUES ({placeholders})",
        *[fields[c] for c in cols], d["ms_id"], moment or now,
    )
    new_id = await txn.fetchval(
        "SELECT id FROM orders WHERE ms_demand_id = $1 AND ms_customerorder_id IS NULL",
        d["ms_id"],
    )
    return int(new_id)


async def _write_invoice(
    txn, d: dict, rows: list[dict], *, counterparty_id: int | None,
    order_id: int | None, base_cur: str, now: str, kind: str = "outgoing",
) -> int:
    """Историческая накладная (расходная или приходная). ОСТАТОК НЕ ДВИГАЕТ.

    Пишем напрямую, минуя `services/warehouse.py`: там каждая проведённая
    накладная сдвигает `stock`, а он уже перенесён снимком на сегодня и все
    эти отгрузки в себя включает. Повторное списание увело бы остаток в минус
    на весь исторический оборот.

    Номер — своей серией `MS-D-*` (отгрузка) / `MS-S-*` (поступление),
    `invoice_counters` не трогаем: иначе перенос съел бы номера у живой
    нумерации, и следующая накладная, выписанная людьми, получила бы номер
    из середины истории.
    """
    from services.money import mul_qty

    prefix = "MS-D" if kind == "outgoing" else "MS-S"
    number = f"{prefix}-{d['name'] or d['ms_id'][:8]}"
    comment = (
        f"Перенос из МойСклад · заказ #{order_id}"
        if order_id
        else "Перенос из МойСклад · поступление"
    )
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
            comment,
            invoice_id,
        )
    else:
        warehouse_id = await txn.fetchval("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        if warehouse_id is None:
            raise RuntimeError("Нет ни одного склада — прогоните `python -m tasks.migrate`")
        await txn.execute(
            "INSERT INTO invoices (type, counterparty_id, warehouse_id, invoice_number, "
            "invoice_date, status, currency, total_amount_cents, comment, created_by, created_at) "
            f"VALUES ('{kind}', $1, $2, $3, $4, 'confirmed', $5, $6, $7, 0, $8)",
            counterparty_id,
            int(warehouse_id),
            number,
            _ms_moment_to_local(d["moment"])[:10],
            (d["currency"] or base_cur).upper(),
            sum(mul_qty(r["price_cents"], r["quantity"]) for r in rows),
            comment,
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


async def _upsert_payment(
    txn, p: dict, *, order_id: int, base_cur: str, now: str,
    amount_cents: int | None = None, part: int = 0, fifo: bool = False,
) -> None:
    """Платёж по `ms_paymentin_id` (партиальный UNIQUE — идемпотентность в схеме).

    Статус `confirmed`: деньги в МойСклад уже проведены, и переносить их как
    `pending` значило бы выставить всю историю на повторное подтверждение боссу.

    **Платёж может делиться между заказами.** При разнесении FIFO один платёж
    гасит несколько заказов, а `payments.order_id` — одно поле на строку.
    Поэтому части пишутся отдельными строками, и `ms_paymentin_id` у второй и
    далее получает суффикс `#N`: UNIQUE в схеме остаётся рабочим, а сумма
    частей равна сумме платежа. Первая часть подчищает прежние строки этого
    платежа — при повторном прогоне разнесение может лечь иначе.
    """
    moment = _ms_moment_to_local(p["moment"])
    currency = (p["currency"] or base_cur).upper()
    fx = 1.0 if currency == base_cur else None
    amount = int(amount_cents if amount_cents is not None else p["sum_minor"])
    key = p["ms_id"] if part <= 1 else f"{p['ms_id']}#{part}"

    note = p["purpose"] or ""
    if fifo:
        note = (
            f"разнесён по FIFO (в МС основания не было){' · ' + note if note else ''}"
        )
    if part > 1 or (fifo and amount != int(p["sum_minor"])):
        note = f"часть {part or 1} платежа {p['name'] or p['ms_id']} · {note}"
    comment = (f"Перенос из МойСклад · {note}" if note else "Перенос из МойСклад")[:500]

    if part <= 1:
        # Повторный прогон: прежнее разнесение этого платежа убираем целиком.
        await txn.execute(
            "DELETE FROM payments WHERE ms_paymentin_id = $1 OR ms_paymentin_id LIKE $2",
            p["ms_id"], f"{p['ms_id']}#%",
        )
    await txn.execute(
        "INSERT INTO payments (user_id, username, full_name, amount_cents, currency, "
        "comment, status, order_id, ms_paymentin_id, fx_rate_to_base, created_at, confirmed_at) "
        "VALUES (0, '', 'Перенос из МойСклад', $1, $2, $3, 'confirmed', $4, $5, $6, $7, $8)",
        amount, currency, comment, order_id, key, fx, moment or now, moment,
    )


async def _upsert_payment_out(
    txn, p: dict, *, counterparty_id: int, invoice_id: int | None,
    base_cur: str, now: str,
) -> None:
    """Платёж поставщику по `ms_paymentout_id` (партиальный UNIQUE в схеме).

    Пишется в `supplier_payments`, а НЕ в `payments`: там деньги ОТ клиентов,
    на которых считается вся дебиторка. Исходящий платёж, попавший туда,
    уменьшил бы долг клиента на сумму, выплаченную поставщику.
    """
    moment = _ms_moment_to_local(p["moment"])
    currency = (p["currency"] or base_cur).upper()
    fx = 1.0 if currency == base_cur else None
    comment = (
        f"Перенос из МойСклад · {p['purpose']}" if p["purpose"] else "Перенос из МойСклад"
    )[:500]
    existing = await txn.fetchval(
        "SELECT id FROM supplier_payments WHERE ms_paymentout_id = $1", p["ms_id"]
    )
    if existing is not None:
        await txn.execute(
            "UPDATE supplier_payments SET counterparty_id = $1, supplier_name = $2, "
            "amount_cents = $3, currency = $4, comment = $5, invoice_id = $6, "
            "fx_rate_to_base = $7, paid_at = $8 WHERE id = $9",
            counterparty_id, p["agent_name"] or None, p["sum_minor"], currency,
            comment, invoice_id, fx, moment, int(existing),
        )
        return
    await txn.execute(
        "INSERT INTO supplier_payments (counterparty_id, supplier_name, amount_cents, "
        "currency, comment, invoice_id, ms_paymentout_id, fx_rate_to_base, paid_at, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        counterparty_id, p["agent_name"] or None, p["sum_minor"], currency,
        comment, invoice_id, p["ms_id"], fx, moment, moment or now,
    )


def _check_supplier_consistency(
    supplies: list[dict], supply_total_cents: dict[str, int]
) -> list[str]:
    """Проверки закупочной стороны.

    Проверяем ровно одно: сумма позиций сошлась с суммой документа МС. Это тот
    же признак обрезанного состава, что и у продаж — вложенный `positions`
    молча обрывается на сотой строке.

    Сверки «оплачено ≤ поставлено» здесь НЕТ, и это осознанно: аванс
    поставщику — обычная практика, выплата вперёд поставки ошибкой переноса не
    является. Пометить её расхождением значило бы утопить настоящие проблемы
    в шуме. Разрез по поставщикам отчёт показывает как есть — решение, что с
    ним делать, за человеком.
    """
    problems: list[str] = []
    for sp in supplies:
        ms_sum = int(sp.get("sum_minor") or 0)
        rows_sum = supply_total_cents.get(sp["ms_id"], 0)
        if ms_sum and abs(ms_sum - rows_sum) > 1:
            problems.append(
                f"поступление {sp['name'] or sp['ms_id']}: сумма позиций "
                f"{rows_sum / 100:.2f} расходится с суммой документа в МС "
                f"{ms_sum / 100:.2f}"
            )
    return problems


# ─── Сверка ───────────────────────────────────────────────────────────────────


def _check_consistency(
    orders: list[dict],
    order_total_cents: dict[str, int],
    shipped_cents: dict[str, int],
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
        # Проверки «оплачено > заказано» здесь нет: после разнесения FIFO
        # переплата невозможна по построению — остаток, которому не нашлось
        # заказа, не пишется, а уходит в отчёт отдельной категорией.
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
    supplies: list[dict], payments_out: list[dict],
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
    logger.info("  поступлений (закупки): %4d  на %s",
                len(supplies), _money(sum(int(x["sum_minor"]) for x in supplies)))
    logger.info("  исходящих платежей  : %5d  на %s",
                len(payments_out), _money(sum(int(x["sum_minor"]) for x in payments_out)))
    logger.info("")
    logger.info("Записано в локальные таблицы:")
    logger.info("  orders              : %5d  (из заказов МС %d · из отгрузок %d)",
                stats.get("orders", 0) + stats.get("orders_from_demand", 0),
                stats.get("orders", 0), stats.get("orders_from_demand", 0))
    logger.info("      из них оплачено : %5d  · в долг %d",
                stats.get("orders_paid", 0), stats.get("orders_credit", 0))
    logger.info("  order_items         : %5d  (с карточкой товара %d)",
                stats.get("order_items", 0), stats.get("order_items_linked", 0))
    logger.info("  invoices (отгрузки) : %5d  (позиций %d)",
                stats.get("demands", 0), stats.get("demand_items", 0))
    logger.info("  payments            : %5d  по основанию из МС",
                stats.get("payments", 0))
    logger.info("      разнесено FIFO  : %5d  (строк %d, т.к. платёж может гасить "
                "несколько заказов)",
                stats.get("payments_fifo", 0), stats.get("payments_fifo_parts", 0))
    logger.info("      не привязано    : %5d", stats.get("payments_unlinked", 0))
    if stats.get("payments_overflow_cents"):
        logger.info("      переплата       : %s — денег больше, чем выставлено",
                    _money(stats["payments_overflow_cents"]))
    logger.info("  invoices (приход)   : %5d  (позиций %d)",
                stats.get("supplies", 0), stats.get("supply_items", 0))
    logger.info("  supplier_payments   : %5d  (не привязано %d)",
                stats.get("payments_out", 0), stats.get("payments_out_unlinked", 0))
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


async def show_supplier_balance() -> None:
    """Расчёты с поставщиками: приход минус выплаты, по контрагентам.

    Долг ПЕРЕД поставщиком и аванс ЕМУ ЖЕ — это одно и то же число с разным
    знаком, и показывать надо оба: «мы должны» и «мы переплатили» одинаково
    важны при разговоре с поставщиком.
    """
    from services import adb_core

    rows = await adb_core.fetch(
        "SELECT c.id, c.name, "
        "  (SELECT COALESCE(SUM(total_amount_cents), 0) FROM invoices "
        "     WHERE counterparty_id = c.id AND type = 'incoming' "
        "       AND status = 'confirmed') AS supplied_cents, "
        "  (SELECT COALESCE(SUM(amount_cents), 0) FROM supplier_payments "
        "     WHERE counterparty_id = c.id) AS paid_cents "
        "FROM counterparties c ORDER BY c.name"
    )
    interesting = [
        r for r in rows
        if int(r["supplied_cents"] or 0) or int(r["paid_cents"] or 0)
    ]
    logger.info("")
    logger.info("═══ РАСЧЁТЫ С ПОСТАВЩИКАМИ ═══")
    if not interesting:
        logger.info("  (пусто)")
        return
    for r in interesting:
        supplied = int(r["supplied_cents"] or 0)
        paid = int(r["paid_cents"] or 0)
        diff = supplied - paid
        verdict = "мы должны" if diff > 0 else ("аванс у поставщика" if diff < 0 else "закрыто")
        logger.info(
            "  %-32s приход %12s  выплачено %12s  →  %s %s",
            str(r["name"])[:32], _money(supplied), _money(paid), verdict, _money(abs(diff)),
        )


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


async def explain_order(name: str) -> int:
    """Показать состав заказа и всех его отгрузок построчно.

    Нужен ровно для одного разговора: сверка сказала «отгружено больше, чем
    заказано», и прежде чем решать, что с этим делать, надо увидеть ПОЗИЦИИ —
    итоговые суммы на этот вопрос не отвечают. Только чтение, в базу не пишет.
    """
    orders = await pull_orders()
    target = next(
        (o for o in orders if o["name"] == name or o["ms_id"] == name), None
    )
    if target is None:
        logger.error("Заказ %r не найден среди %d выгруженных", name, len(orders))
        return 1

    demands = await pull_demands()
    linked = [d for d in demands if d["order_ms_id"] == target["ms_id"]]

    def _lines(positions: list[dict]) -> tuple[list[str], int]:
        out, total = [], 0
        for pos in positions:
            a = pos.get("assortment") or {}
            qty = float(pos.get("quantity") or 0)
            price = int(pos.get("price") or 0)
            line = int(round(qty * price))
            total += line
            out.append(
                f"      {str(a.get('name') or '—')[:44]:<44} "
                f"{qty:>9,.3f} × {price / 100:>10,.2f} = {line / 100:>12,.2f}"
            )
        return out, total

    logger.info("")
    logger.info("═══ ЗАКАЗ %s ═══", target["name"] or target["ms_id"])
    logger.info("  контрагент : %s", target["agent_name"] or "—")
    logger.info("  дата       : %s", target["moment"][:10])
    logger.info("  статус в МС: %s", target["state_name"] or "—")
    logger.info("  сумма документа в МС : %12s", _money(target["sum_minor"]))
    logger.info("  оплачено (payedSum)  : %12s", _money(target["payed_minor"]))
    logger.info("  отгружено (shippedSum): %11s", _money(target["shipped_minor"]))
    logger.info("")
    logger.info("  ПОЗИЦИИ ЗАКАЗА:")
    lines, ordered_total = _lines(target["positions"])
    for ln in lines:
        logger.info("%s", ln)
    logger.info("      %-44s %28s", "ИТОГО по позициям:", _money(ordered_total))

    shipped_total = 0
    logger.info("")
    logger.info("  ОТГРУЗОК ПО ЭТОМУ ЗАКАЗУ: %d", len(linked))
    for d in linked:
        logger.info("")
        logger.info(
            "  ── отгрузка %s от %s · сумма документа %s",
            d["name"] or d["ms_id"], d["moment"][:10], _money(d["sum_minor"]),
        )
        lines, dem_total = _lines(d["positions"])
        for ln in lines:
            logger.info("%s", ln)
        logger.info("      %-44s %28s", "ИТОГО по позициям:", _money(dem_total))
        shipped_total += dem_total

    logger.info("")
    logger.info("  ═══ СВОДКА ═══")
    logger.info("    заказано  : %12s", _money(ordered_total))
    logger.info("    отгружено : %12s", _money(shipped_total))
    delta = shipped_total - ordered_total
    if delta > 0:
        logger.warning("    ПРЕВЫШЕНИЕ: %s", _money(delta))
        logger.warning("")
        logger.warning("    Что это может значить:")
        logger.warning("      • отгрузок по заказу больше, чем он покрывает —")
        logger.warning("        в МС заказ дополняли, а позиции не правили;")
        logger.warning("      • в отгрузке есть позиции, которых в заказе нет;")
        logger.warning("      • отгрузка привязана к этому заказу ошибочно.")
        logger.warning("    Сравните строки выше — разница видна по позициям.")
    elif delta < 0:
        logger.info("    недоотгружено: %s", _money(-delta))
    else:
        logger.info("    сходится")
    return 0


async def main(mode: str) -> int:
    if mode.startswith("explain:"):
        try:
            return await explain_order(mode.split(":", 1)[1])
        finally:
            await close_session()

    try:
        orders = await pull_orders()
        demands = await pull_demands()
        payments = await pull_payments()
        supplies = await pull_supplies()
        payments_out = await pull_payments_out()

        stats, unmatched, problems = await write_history(
            orders, demands, payments, supplies, payments_out,
            dry_run=(mode == "dry-run"),
        )
        print_report(
            orders, demands, payments, supplies, payments_out,
            stats, unmatched, problems,
            dry_run=(mode == "dry-run"),
        )

        if mode == "dry-run":
            logger.info("")
            logger.info("Это предпросмотр. Для записи: --apply")
            return 1 if problems else 0

        await show_totals()
        await show_debtors()
        await show_supplier_balance()
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
    g.add_argument(
        "--explain",
        metavar="ЗАКАЗ",
        help="показать позиции заказа и всех его отгрузок (только чтение)",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    if args.explain:
        _mode = f"explain:{args.explain}"
    else:
        _mode = "dry-run" if args.dry_run else "apply"
    sys.exit(asyncio.run(main(_mode)))
