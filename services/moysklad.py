"""
Все запросы к API МойСклад
"""

import asyncio
import logging
import time
import aiohttp
from datetime import datetime

from config import MS_TOKEN
from utils.helpers import extract_id_from_href

logger = logging.getLogger(__name__)

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"
MS_HEADERS = {
    "Authorization": f"Bearer {MS_TOKEN}",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
}

# Таймаут на любой запрос к МойСклад
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Персистентная сессия — создаётся один раз на старте бота.
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def get_session() -> aiohttp.ClientSession:
    """Вернуть глобальную сессию, создавая её при первом обращении."""
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
                _session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=_HTTP_TIMEOUT,
                    headers=MS_HEADERS,
                )
    return _session


async def close_session() -> None:
    """Закрыть сессию при остановке бота."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # сек, удваивается на каждой попытке
# 429 — rate limit; 5xx — временный сбой на стороне МойСклад
_RETRY_STATUSES = {429, 500, 502, 503, 504}


async def ms_get(path: str, params: dict = None, session: aiohttp.ClientSession = None):
    """GET с ретраями: сетевые ошибки, таймауты и 429/5xx."""
    sess = session if session is not None else await get_session()
    url = f"{MS_BASE}/{path}"
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES):
        try:
            async with sess.get(url, params=params) as resp:
                if resp.status in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    # МойСклад возвращает Retry-After для 429 — уважаем его
                    retry_after = resp.headers.get("Retry-After")
                    delay = (
                        float(retry_after) if retry_after and retry_after.isdigit()
                        else _RETRY_BASE_DELAY * (2 ** attempt)
                    )
                    logger.warning(
                        "MS %s → %s, retry %d/%d через %.1fs",
                        path, resp.status, attempt + 1, _MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            last_exc = e
            if attempt >= _MAX_RETRIES - 1:
                break
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "MS %s → %s, retry %d/%d через %.1fs",
                path, type(e).__name__, attempt + 1, _MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)

    # Все попытки исчерпаны
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"MoyСклад {path}: исчерпаны {_MAX_RETRIES} попыток")


# ─── TTL-кэш для raw API stock (используется при отсутствии snapshot) ───────

_STOCK_TTL = 30  # сек
_stock_cache: dict = {"ts": 0.0, "data": None}
_stock_lock = asyncio.Lock()


def invalidate_stock_cache() -> None:
    """Сбросить TTL-кэш сырого API-выкачивания склада."""
    _stock_cache["ts"] = 0.0
    _stock_cache["data"] = None


async def _api_get_all_stock() -> list[dict]:
    """Старый путь: качаем остатки с API МойСклад страница за страницей.
    Оставлен как fallback и для использования из services.snapshot.refresh_stock.
    Не вызывайте напрямую из handlers — используйте get_all_stock()."""
    now = time.monotonic()
    if _stock_cache["data"] is not None and now - _stock_cache["ts"] < _STOCK_TTL:
        return _stock_cache["data"]
    async with _stock_lock:
        now = time.monotonic()
        if _stock_cache["data"] is not None and now - _stock_cache["ts"] < _STOCK_TTL:
            return _stock_cache["data"]
        all_rows = []
        offset = 0
        limit = 1000
        sess = await get_session()
        while True:
            data = await ms_get(
                "report/stock/all",
                params={"limit": limit, "offset": offset},
                session=sess,
            )
            rows = data if isinstance(data, list) else data.get("rows", [])
            all_rows.extend(rows)
            if len(rows) < limit:
                break
            offset += limit
        # Не фильтруем по stock != 0 — нулевые остатки тоже показываем
        # в каталоге, чтобы товар не пропадал после полной отгрузки.
        _stock_cache["data"] = all_rows
        _stock_cache["ts"] = time.monotonic()
        return all_rows


def _reshape_stock_row(r: dict) -> dict:
    """Snapshot-row → формат МойСклад API (с meta/folder/uom).
    Сохраняет совместимость с handlers и webapp, которые ожидают
    оригинальные ключи r.get('folder', {}).get('meta', {}).get('href') и т.д."""
    href = f"{MS_BASE}/entity/product/{r['ms_id']}"
    folder_href = (
        f"{MS_BASE}/entity/productfolder/{r['folder_id']}" if r.get("folder_id") else ""
    )
    return {
        "name": r.get("name", ""),
        "stock": r.get("stock", 0),
        "reserve": r.get("reserve", 0),
        "meta": {"href": href},
        "uom": {"name": r.get("unit") or "шт"},
        "folder": (
            {
                "meta": {"href": folder_href},
                "name": r.get("folder_name", "") or "",
            }
            if folder_href
            else {}
        ),
    }


def _reshape_category_row(r: dict) -> dict:
    href = f"{MS_BASE}/entity/productfolder/{r['ms_id']}"
    return {
        "name": r.get("name", ""),
        "meta": {"href": href},
    }


async def get_all_stock() -> list[dict]:
    """
    Список остатков для UI. Сначала пытается отдать из локального
    snapshot (мгновенно, без запросов к МойСклад). Если snapshot пуст
    (только что развернулись) — падает на сырой API-pull с TTL-кэшем
    и параллельно инициирует первичный рефреш.
    """
    from services import snapshot  # lazy чтобы избежать циклов
    rows = snapshot.get_stock(only_positive=False)
    if rows:
        return [_reshape_stock_row(r) for r in rows]
    # Snapshot ещё не наполнен — fallback на raw API
    logger.info("snapshot пуст — отдаём stock из live API")
    raw = await _api_get_all_stock()
    # Параллельно запускаем первичный рефреш, чтобы при следующем запросе
    # snapshot уже работал. Не ждём результата — это fire-and-forget.
    asyncio.create_task(snapshot.refresh_stock())
    return raw


async def get_categories() -> list[dict]:
    """Список категорий для UI. Аналогично — сначала snapshot, потом API."""
    from services import snapshot
    rows = snapshot.get_categories()
    if rows:
        return [_reshape_category_row(r) for r in rows]
    logger.info("snapshot пуст — отдаём categories из live API")
    data = await ms_get("entity/productfolder", params={"limit": 1000})
    raw = data if isinstance(data, list) else data.get("rows", [])
    asyncio.create_task(snapshot.refresh_categories())
    return raw


async def get_shipments(since: datetime, until: datetime = None) -> list[dict]:
    """Получить отгрузки за период."""
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.000")
    filter_str = f"moment>{since_str}"
    if until:
        until_str = until.strftime("%Y-%m-%d %H:%M:%S.000")
        filter_str += f";moment<{until_str}"
    data = await ms_get(
        "entity/demand",
        params={
            "filter": filter_str,
            "expand": "agent,owner",
            "order": "moment,desc",
            "limit": 100,
        },
    )
    return data if isinstance(data, list) else data.get("rows", [])


async def get_shipment_positions(demand_id: str) -> list[dict]:
    """Получить позиции (товары) конкретной отгрузки."""
    data = await ms_get(
        f"entity/demand/{demand_id}/positions",
        params={"limit": 100, "expand": "assortment,uom"},
    )
    return data if isinstance(data, list) else data.get("rows", [])


async def get_sales_stats(since: datetime, until: datetime = None) -> dict:
    """Статистика продаж за период: выручка, отгрузки, клиенты, топ товаров."""
    shipments = await get_shipments(since, until)
    if not shipments:
        return {"total": 0, "count": 0, "clients": 0, "top_products": []}

    total = sum(s.get("sum", 0) for s in shipments)
    clients = len(
        set(
            s.get("agent", {}).get("name", "")
            for s in shipments
            if s.get("agent", {}).get("name")
        )
    )

    product_sums: dict[str, dict] = {}
    for s in shipments[:15]:
        demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
        if not demand_id:
            continue
        try:
            positions = await get_shipment_positions(demand_id)
            for pos in positions:
                name = pos.get("assortment", {}).get("name", "—")
                qty = pos.get("quantity", 0)
                price = pos.get("price", 0)
                pos_sum = qty * price
                if name not in product_sums:
                    product_sums[name] = {"sum": 0, "qty": 0}
                product_sums[name]["sum"] += pos_sum
                product_sums[name]["qty"] += qty
        except Exception:
            pass

    top_products = sorted(
        product_sums.items(), key=lambda x: x[1]["sum"], reverse=True
    )[:5]

    return {
        "total": total,
        "count": len(shipments),
        "clients": clients,
        "top_products": top_products,
    }

async def get_employee_shipments(
    since: datetime,
    until: datetime = None,
    employee_href: str = None,
) -> list[dict]:
    """Получить отгрузки конкретного сотрудника по его href."""
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.000")
    filter_str = f"moment>{since_str}"
    if until:
        filter_str += f";moment<{until.strftime('%Y-%m-%d %H:%M:%S.000')}"
    if employee_href:
        filter_str += f";owner={employee_href}"

    data = await ms_get(
        "entity/demand",
        params={
            "filter": filter_str,
            "expand": "agent,owner",
            "order": "moment,desc",
            "limit": 100,
        },
    )
    return data if isinstance(data, list) else data.get("rows", [])


async def get_employee_stats(
    since: datetime,
    until: datetime = None,
    employee_href: str = None,
) -> dict:
    """Персональная статистика сотрудника."""
    shipments = await get_employee_shipments(since, until, employee_href)
    if not shipments:
        return {
            "total": 0, "count": 0, "clients": 0,
            "top_products": [], "by_day": {}, "product_sums": {}
        }

    total = sum(s.get("sum", 0) for s in shipments)
    clients = len(set(
        s.get("agent", {}).get("name", "")
        for s in shipments if s.get("agent", {}).get("name")
    ))

    # По дням недели
    days_ru = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
    by_day = {v: 0 for v in days_ru.values()}
    for s in shipments:
        try:
            day_num = datetime.strptime(s.get("moment", "")[:10], "%Y-%m-%d").weekday()
            by_day[days_ru[day_num]] += 1
        except Exception:
            pass

    # По товарам
    product_sums: dict[str, dict] = {}
    for s in shipments[:10]:  # Уменьшаем чтобы не превышать лимит
        demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
        if not demand_id:
            continue
        try:
            positions = await get_shipment_positions(demand_id)
            for pos in positions:
                name = pos.get("assortment", {}).get("name", "—")
                qty = pos.get("quantity", 0)
                price = pos.get("price", 0)
                if name not in product_sums:
                    product_sums[name] = {"sum": 0, "qty": 0}
                product_sums[name]["sum"] += qty * price
                product_sums[name]["qty"] += qty
        except Exception:
            pass

    top_products = sorted(
        product_sums.items(), key=lambda x: x[1]["sum"], reverse=True
    )[:5]

    return {
        "total": total,
        "count": len(shipments),
        "clients": clients,
        "top_products": top_products,
        "by_day": by_day,
        "product_sums": product_sums,
    }


async def get_employee_href(ms_employee_id: str) -> str:
    """Получить href сотрудника по его ID."""
    return f"{MS_BASE}/entity/employee/{ms_employee_id}"