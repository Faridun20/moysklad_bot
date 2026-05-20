"""
Все запросы к API МойСклад
"""

import asyncio
import functools
import json as _json
import logging
import time
import aiohttp
from datetime import datetime

from config import MS_TOKEN
from utils.helpers import extract_id_from_href


def redact_ms_error(body: str, max_len: int = 200) -> str:
    """Безопасный лог ошибки от МойСклад API.

    SECURITY.md H7: МойСклад в error-body часто включает реальные UUID,
    имена контрагентов, поля заказов — PII, которая попадает в Railway
    logs / Better Stack / Axiom log drain (если подключён). Аудит-агент
    может видеть чужие данные.

    Что делаем: пробуем распарсить body как JSON и достать только
    `error` + `code` поля. Остальное (parameter, moreInfo, line) теряем —
    они полезны для отладки, но могут содержать чувствительное.
    Fallback на жёсткий truncate если JSON не парсится.
    """
    if not body:
        return ""
    try:
        data = _json.loads(body)
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors:
            err0 = errors[0] or {}
            code = err0.get("code", "?")
            # error-text иногда сам содержит имена / уникальные id —
            # обрезаем агрессивно.
            text = str(err0.get("error", ""))[:120]
            return f"code={code} error={text!r}"
    except Exception:
        pass
    return body[:max_len]

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


class _CircuitBreaker:
    """Простой circuit breaker для МойСклад API.

    Открывается после OPEN_AFTER_FAILS подряд провальных запросов.
    В открытом состоянии все запросы сразу падают без попыток — это
    защищает event loop от накапливающихся таймаутов (30с каждый) при
    длительном outage МойСклад. После HALF_OPEN_AFTER секунд переходим
    в half-open и делаем один пробный запрос. Если успех — закрываем.
    """

    OPEN_AFTER_FAILS = 5
    HALF_OPEN_AFTER = 60.0  # секунд

    def __init__(self) -> None:
        self._fails = 0
        self._opened_at: float | None = None

    def _half_open(self) -> bool:
        """Открыт, но таймаут прошёл — пора пропустить один пробный запрос."""
        return (
            self._opened_at is not None
            and time.monotonic() - self._opened_at >= self.HALF_OPEN_AFTER
        )

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if self._half_open():
            return False  # half-open: пробуем
        return True

    def record_success(self) -> None:
        self._fails = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._fails += 1
        # Открываем цепь когда: (а) накопился порог ошибок и она ещё
        # закрыта, ИЛИ (б) упал пробный запрос в half-open — тогда
        # переоткрываем со свежим таймером. Без (б) breaker навсегда
        # застревал в half-open и пропускал все запросы без защиты.
        if self._opened_at is None and self._fails >= self.OPEN_AFTER_FAILS:
            self._opened_at = time.monotonic()
            logger.error(
                "МойСклад circuit breaker ОТКРЫТ после %d ошибок подряд. "
                "Запросы будут отклоняться %.0f сек.",
                self._fails, self.HALF_OPEN_AFTER,
            )
        elif self._half_open():
            self._opened_at = time.monotonic()
            logger.warning(
                "МойСклад circuit breaker: пробный запрос упал, "
                "цепь снова открыта на %.0f сек.", self.HALF_OPEN_AFTER,
            )


_circuit = _CircuitBreaker()


async def ms_get(path: str, params: dict = None, session: aiohttp.ClientSession = None):
    """GET с ретраями: сетевые ошибки, таймауты и 429/5xx.
    Защищён circuit breaker'ом: после 5 подряд ошибок отклоняет без попыток.
    """
    if _circuit.is_open():
        raise RuntimeError(
            "МойСклад временно недоступен (circuit breaker открыт). "
            "Повторите позже."
        )

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
                _circuit.record_success()
                return await resp.json()
        except (TimeoutError, aiohttp.ClientConnectionError) as e:
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
    _circuit.record_failure()
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"MoyСклад {path}: исчерпаны {_MAX_RETRIES} попыток")


# ─── Универсальный TTL-кэш для read-only МС эндпоинтов ───────────────────────
#
# Зачем: открытие «Аналитики» одним боссом = 1×get_sales_stats (≥1 запрос
# к МС) + 15×get_shipment_positions внутри неё + ещё 1 для prev-периода +
# 1×get_shipments. Итого ~30+ запросов. Если 5 боссов жмут одновременно —
# 150 запросов, привет 429. Кэш + inflight-coalescing превращают залп в
# один поход за ключ-периодом.

_MS_CACHE_REGISTRY: list[tuple[str, dict]] = []  # для invalidate_all()


def _ms_cache_key(args, kwargs):
    """Нормализуем datetime в ключе до минуты, иначе каждый вызов с
    datetime.utcnow() даёт уникальный ключ и кэш бесполезен."""
    def _norm(v):
        if isinstance(v, datetime):
            return v.replace(second=0, microsecond=0)
        return v
    return (
        tuple(_norm(a) for a in args),
        tuple((k, _norm(v)) for k, v in sorted(kwargs.items())),
    )


def _ms_ttl_cache(ttl: float, name: str = ""):
    """Async TTL-cache + inflight-coalescing.

    Поведение:
      - hit моложе ttl сек → возвращаем как есть, без похода в API
      - miss или истёкший — берём per-key lock, проверяем повторно,
        делаем один запрос, кладём в кэш. Второй concurrent-вызов
        с тем же ключом ждёт на том же lock'е и получает готовое
        значение (никакого двойного похода в МС).
    """
    def decorator(fn):
        cache: dict = {}
        locks: dict = {}
        registry_lock = asyncio.Lock()

        _MS_CACHE_REGISTRY.append((name or fn.__name__, cache))

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = _ms_cache_key(args, kwargs)
            now = time.monotonic()
            entry = cache.get(key)
            if entry is not None and now - entry[0] < ttl:
                return entry[1]

            async with registry_lock:
                lock = locks.get(key)
                if lock is None:
                    lock = asyncio.Lock()
                    locks[key] = lock

            async with lock:
                # Повторная проверка под локом — другой coroutine мог
                # уже наполнить кэш, пока мы ждали.
                entry = cache.get(key)
                if entry is not None and time.monotonic() - entry[0] < ttl:
                    return entry[1]
                result = await fn(*args, **kwargs)
                cache[key] = (time.monotonic(), result)
                # Ленивая чистка: если ключей больше 200, выкидываем
                # все протухшие. Для нашего usage'а (4 периода × роли)
                # этого хватит навечно.
                if len(cache) > 200:
                    cutoff = time.monotonic() - ttl
                    stale = [k for k, (t, _) in cache.items() if t < cutoff]
                    for k in stale:
                        cache.pop(k, None)
                        locks.pop(k, None)
                return result

        wrapper.cache_clear = lambda: (cache.clear(), locks.clear())
        return wrapper
    return decorator


def invalidate_ms_cache() -> None:
    """Сбросить ВСЕ TTL-кэши read-only МС-эндпоинтов.
    Вызывать когда хотим гарантированно свежие данные (например,
    из webhook handler'а после события об изменении документа)."""
    for _, cache in _MS_CACHE_REGISTRY:
        cache.clear()
    invalidate_stock_cache()


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


@_ms_ttl_cache(ttl=60.0, name="get_shipments")
async def get_shipments(since: datetime, until: datetime = None) -> list[dict]:
    """Получить отгрузки за период. Результат кэшируется на 60 сек —
    несколько боссов смотрят аналитику одновременно без 429 от МС."""
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


@_ms_ttl_cache(ttl=3600.0, name="get_shipment_positions")
async def get_shipment_positions(demand_id: str) -> list[dict]:
    """Получить позиции (товары) конкретной отгрузки.

    Позиции demand-документа неизменяемы после создания, поэтому кэш
    на час безопасен. До этого 15+ позиций отгрузки запрашивались на
    каждое открытие «Аналитики» — теперь только один раз за demand."""
    data = await ms_get(
        f"entity/demand/{demand_id}/positions",
        params={"limit": 100, "expand": "assortment,uom"},
    )
    return data if isinstance(data, list) else data.get("rows", [])


@_ms_ttl_cache(ttl=60.0, name="get_sales_stats")
async def get_sales_stats(since: datetime, until: datetime = None) -> dict:
    """Статистика продаж за период: выручка, отгрузки, клиенты, топ товаров.
    Кэш 60 сек поверх get_shipments+get_shipment_positions: даже если
    несколько ролей одновременно запросили одинаковый период, агрегат
    считается один раз."""
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

@_ms_ttl_cache(ttl=60.0, name="get_employee_shipments")
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


@_ms_ttl_cache(ttl=60.0, name="get_employee_stats")
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