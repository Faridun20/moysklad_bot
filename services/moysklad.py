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
# Event loop, к которому привязана _session. aiohttp-сессия живёт на том
# loop'е, где её создали; вызвать её из ДРУГОГО loop'а (например из
# asyncio.run() в to_thread-воркере) → RuntimeError. Сохраняем ссылку,
# чтобы sync-вызывающие (confirm_payment в to_thread) могли запланировать
# корутину на правильный loop через run_coroutine_threadsafe. См.
# database._trigger_ms_paymentin_sync.
_session_loop: asyncio.AbstractEventLoop | None = None


async def get_session() -> aiohttp.ClientSession:
    """Вернуть глобальную сессию, создавая её при первом обращении."""
    global _session, _session_loop
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
                _session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=_HTTP_TIMEOUT,
                    headers=MS_HEADERS,
                )
                _session_loop = asyncio.get_running_loop()
    return _session


def get_session_loop() -> asyncio.AbstractEventLoop | None:
    """Loop, на котором создана MS-сессия (None — сессии ещё нет)."""
    return _session_loop


async def close_session() -> None:
    """Закрыть сессию при остановке бота."""
    global _session, _session_loop
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
    _session_loop = None


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
                self._fails,
                self.HALF_OPEN_AFTER,
            )
        elif self._half_open():
            self._opened_at = time.monotonic()
            logger.warning(
                "МойСклад circuit breaker: пробный запрос упал, цепь снова открыта на %.0f сек.",
                self.HALF_OPEN_AFTER,
            )


_circuit = _CircuitBreaker()


async def ms_get(
    path: str, params: dict | None = None, session: aiohttp.ClientSession | None = None
):
    """GET с ретраями: сетевые ошибки, таймауты и 429/5xx.
    Защищён circuit breaker'ом: после 5 подряд ошибок отклоняет без попыток.
    """
    if _circuit.is_open():
        raise RuntimeError(
            "МойСклад временно недоступен (circuit breaker открыт). Повторите позже."
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
                        float(retry_after)
                        if retry_after and retry_after.isdigit()
                        else _RETRY_BASE_DELAY * (2**attempt)
                    )
                    logger.warning(
                        "MS %s → %s, retry %d/%d через %.1fs",
                        path,
                        resp.status,
                        attempt + 1,
                        _MAX_RETRIES,
                        delay,
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
            delay = _RETRY_BASE_DELAY * (2**attempt)
            logger.warning(
                "MS %s → %s, retry %d/%d через %.1fs",
                path,
                type(e).__name__,
                attempt + 1,
                _MAX_RETRIES,
                delay,
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
                try:
                    result = await fn(*args, **kwargs)
                except BaseException:
                    # При исключении (включая CancelledError) cache[key]
                    # не пишется → cache не растёт → stale-prune (gate'нут
                    # на len(cache)>200) никогда не сработает → locks[key]
                    # утечёт навсегда. Чистим явно перед re-raise. Для
                    # high-cardinality persistently-failing ключей (например,
                    # удалённых demand_id из старой истории аналитики)
                    # это закрывает unbounded memory growth.
                    locks.pop(key, None)
                    raise
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

        # ВАЖНО: clear НЕ трогает locks. Иначе invalidate_ms_cache() из
        # MS-webhook'а может race'ить с in-flight winner'ом: winner держит
        # local-var lock + делает await ms_get, мы из webhook'а делаем
        # locks.clear() — следующий caller для того же ключа создаёт
        # СВЕЖИЙ lock и запускает СВОЙ HTTP параллельно с winner'ом.
        # Inflight-coalescing нарушен. Trade-off: stale locks сидят в dict
        # до следующего stale-prune (gated на len(cache)>200) — безвредно,
        # любая повторная попытка для того же ключа просто пройдёт через
        # пустой lock мгновенно.
        wrapper.cache_clear = lambda: cache.clear()
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
        all_rows: list = []
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
    folder_href = f"{MS_BASE}/entity/productfolder/{r['folder_id']}" if r.get("folder_id") else ""
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
async def get_shipments(since: datetime, until: datetime | None = None) -> list[dict]:
    """Получить отгрузки за период. Результат кэшируется на 60 сек —
    несколько боссов смотрят аналитику одновременно без 429 от МС.

    Пагинация offset-loop (CLAUDE.md: paginated MS endpoint — крути offset).
    Раньше был один запрос limit=100 без offset: за длинный период (год)
    с >100 отгрузок терялся хвост → выручка/топ занижались, а аналитика
    «за год» выглядела неполной. Cap MAX_PAGES — защита от тысяч строк/таймаута."""
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.000")
    filter_str = f"moment>{since_str}"
    if until:
        until_str = until.strftime("%Y-%m-%d %H:%M:%S.000")
        filter_str += f";moment<{until_str}"
    rows: list[dict] = []
    offset = 0
    page = 100
    max_pages = 10  # ≤1000 отгрузок — разумный потолок для аналитики
    for _ in range(max_pages):
        data = await ms_get(
            "entity/demand",
            params={
                "filter": filter_str,
                "expand": "agent,owner",
                "order": "moment,desc",
                "limit": page,
                "offset": offset,
            },
        )
        chunk = data if isinstance(data, list) else data.get("rows", [])
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


async def get_shipment(demand_id: str) -> dict | None:
    """Получить один demand-документ с раскрытыми agent/owner.

    Нужен для событийных уведомлений (MS-вебхук даёт только demand_id, а
    format_shipment ждёт объект с agent/owner/sum). Без кэша — событие
    про конкретную новую отгрузку приходит один раз."""
    data = await ms_get(
        f"entity/demand/{demand_id}",
        params={"expand": "agent,owner"},
    )
    return data if isinstance(data, dict) else None


# Параллельно тянем позиции 15+ отгрузок (asyncio.gather в get_sales_stats /
# get_employee_stats). Без семафора это залп 15 одновременных запросов,
# стабильно бьющий 429-rate-limit МС → retry-chain 0.5/1.0/2.0с цепочкой и
# заметная latency у /api/analytics. Семафор держит ≤8 конкурентных HTTP
# (Connector limit=20 выдержит; МС rate ~45req/s). На холодном кэше с 30
# fan-out: ceil(30/8)*RTT ≈ 800мс — приемлемо. Кэш-хит сюда не доходит
# (декоратор отдаёт до тела).
#
# Lazy-init по loop-id: модульный Semaphore() лениво-binds к first loop
# при contention; короткоживущие asyncio.run() в CLI + per-test loops в
# pytest могут привести к "bound to a different event loop" если waiter
# когда-нибудь enqueued. Helper отдаёт свежий семафор для каждого loop'а,
# проблему обходит начисто (precedent — _session_lock — пока пронесло).
_POSITIONS_SEM_BY_LOOP: dict[int, asyncio.Semaphore] = {}
_POSITIONS_CONCURRENCY_LIMIT = 8


def _get_positions_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _POSITIONS_SEM_BY_LOOP.get(id(loop))
    if sem is None:
        sem = asyncio.Semaphore(_POSITIONS_CONCURRENCY_LIMIT)
        _POSITIONS_SEM_BY_LOOP[id(loop)] = sem
    return sem


@_ms_ttl_cache(ttl=3600.0, name="get_shipment_positions")
async def get_shipment_positions(demand_id: str) -> list[dict]:
    """Получить ВСЕ позиции (товары) конкретной отгрузки.

    Позиции demand-документа неизменяемы после создания, поэтому кэш
    на час безопасен. До этого 15+ позиций отгрузки запрашивались на
    каждое открытие «Аналитики» — теперь только один раз за demand.

    Пагинация: МС /positions возвращает максимум 100 строк за запрос.
    До пагинации demand'ы с >100 line items (крупные B2B-заказы) молча
    теряли хвост → top_products в аналитике врал. Loop offset+=100
    собирает все страницы. Семафор acquire'ится на каждую страницу
    (а не на весь demand) — это правильно: для больших demand'ов
    нагрузка размывается равномерно по rate-limit.
    """
    rows: list[dict] = []
    offset = 0
    page_limit = 100
    sem = _get_positions_semaphore()
    while True:
        async with sem:
            data = await ms_get(
                f"entity/demand/{demand_id}/positions",
                params={
                    "limit": page_limit,
                    "offset": offset,
                    "expand": "assortment,uom",
                },
            )
        chunk = data if isinstance(data, list) else data.get("rows", [])
        rows.extend(chunk)
        if len(chunk) < page_limit:
            break
        offset += page_limit
    return rows


@_ms_ttl_cache(ttl=60.0, name="get_sales_stats")
async def get_sales_stats(since: datetime, until: datetime | None = None) -> dict:
    """Статистика продаж за период: выручка, отгрузки, клиенты, топ товаров.
    Кэш 60 сек поверх get_shipments+get_shipment_positions: даже если
    несколько ролей одновременно запросили одинаковый период, агрегат
    считается один раз."""
    shipments = await get_shipments(since, until)
    if not shipments:
        return {"total": 0, "count": 0, "clients": 0, "top_products": []}

    total = sum(s.get("sum", 0) for s in shipments)
    clients = len(
        set(s.get("agent", {}).get("name", "") for s in shipments if s.get("agent", {}).get("name"))
    )

    # PR D: выручка по клиентам (из ВСЕХ отгрузок — s["sum"], точно).
    by_client: dict[str, dict] = {}
    for s in shipments:
        cname = s.get("agent", {}).get("name") or "—"
        c = by_client.setdefault(cname, {"sum": 0, "count": 0})
        c["sum"] += s.get("sum", 0) or 0
        c["count"] += 1

    product_sums: dict[str, dict] = {}
    demand_ids: list[str] = []
    for s in shipments[:15]:
        did = extract_id_from_href(s.get("meta", {}).get("href", ""))
        if did:
            demand_ids.append(did)
    # Позиции отгрузок независимы между собой — тянем параллельно. Раньше это
    # был цикл из 15 последовательных HTTP к МС на каждое открытие «Аналитики»
    # (секунды ожидания на холодном кэше). get_shipment_positions TTL-кэширован,
    # ключи (demand_id) различны → без cache-стэмпиды.
    results = await asyncio.gather(
        *(get_shipment_positions(did) for did in demand_ids),
        return_exceptions=True,
    )
    for positions in results:
        if isinstance(positions, BaseException):
            continue  # эквивалент прежнего per-item try/except: pass
        for pos in positions:
            assortment = pos.get("assortment", {}) or {}
            name = assortment.get("name", "—")
            # ms_id товара — для расчёта маржи (cost из product_prices).
            ms_id = extract_id_from_href(assortment.get("meta", {}).get("href", ""))
            qty = pos.get("quantity", 0)
            price = pos.get("price", 0)
            pos_sum = qty * price
            if name not in product_sums:
                product_sums[name] = {"sum": 0, "qty": 0, "ms_id": ms_id}
            product_sums[name]["sum"] += pos_sum
            product_sums[name]["qty"] += qty
            if ms_id and not product_sums[name].get("ms_id"):
                product_sums[name]["ms_id"] = ms_id

    top_products = sorted(product_sums.items(), key=lambda x: x[1]["sum"], reverse=True)[:5]
    top_clients = sorted(by_client.items(), key=lambda x: x[1]["sum"], reverse=True)[:10]

    return {
        "total": total,
        "count": len(shipments),
        "clients": clients,
        "top_products": top_products,
        "top_clients": top_clients,
    }


@_ms_ttl_cache(ttl=300.0, name="get_counterparty_purchases")
async def get_counterparty_purchases(agent_id: str, max_demands: int = 20) -> dict:
    """Покупки контрагента из МС (для карточки клиента): топ-товары + последние
    отгрузки. Фильтр demand по agent=href; позиции последних max_demands отгрузок
    тянем параллельно (семафор), агрегируем по товару. Суммы — в копейках.
    Кэш 5 мин на agent_id (TTL-декоратор) — не дёргаем МС на каждый тап."""
    if not agent_id:
        return {"top_products": [], "recent": [], "total_cents": 0, "count": 0}
    agent_href = f"{MS_BASE}/entity/counterparty/{agent_id}"
    demands: list[dict] = []
    offset = 0
    page = 100
    while len(demands) < 1000:  # cap ~10 страниц на клиента
        data = await ms_get(
            "entity/demand",
            params={
                "filter": f"agent={agent_href}",
                "order": "moment,desc",
                "limit": page,
                "offset": offset,
                "expand": "agent",
            },
        )
        chunk = data if isinstance(data, list) else data.get("rows", [])
        demands.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    if not demands:
        return {"top_products": [], "recent": [], "total_cents": 0, "count": 0}
    total_cents = sum(s.get("sum", 0) or 0 for s in demands)
    recent = [
        {
            "id": extract_id_from_href(s.get("meta", {}).get("href", "")),
            "date": (s.get("moment") or "")[:16],
            "sum_cents": s.get("sum", 0) or 0,
        }
        for s in demands[:10]
    ]
    demand_ids = [extract_id_from_href(s.get("meta", {}).get("href", "")) for s in demands[:max_demands]]
    demand_ids = [d for d in demand_ids if d]
    results = await asyncio.gather(
        *(get_shipment_positions(d) for d in demand_ids), return_exceptions=True
    )
    product_sums: dict[str, dict] = {}
    for positions in results:
        if isinstance(positions, BaseException):
            continue
        for pos in positions:
            name = (pos.get("assortment", {}) or {}).get("name", "—")
            qty = pos.get("quantity", 0) or 0
            price = pos.get("price", 0) or 0
            p = product_sums.setdefault(name, {"sum_cents": 0, "qty": 0})
            p["sum_cents"] += qty * price
            p["qty"] += qty
    top_products = [
        {"name": n, "qty": v["qty"], "sum_cents": int(v["sum_cents"])}
        for n, v in sorted(product_sums.items(), key=lambda x: x[1]["sum_cents"], reverse=True)[:10]
    ]
    return {
        "top_products": top_products,
        "recent": recent,
        "total_cents": int(total_cents),
        "count": len(demands),
    }


@_ms_ttl_cache(ttl=60.0, name="get_employee_shipments")
async def get_employee_shipments(
    since: datetime,
    until: datetime | None = None,
    employee_href: str | None = None,
) -> list[dict]:
    """Получить отгрузки конкретного сотрудника по его href.

    Пагинация offset-loop (CLAUDE.md): без неё у сотрудника с >100
    отгрузками за период терялся хвост → личная статистика занижалась."""
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.000")
    filter_str = f"moment>{since_str}"
    if until:
        filter_str += f";moment<{until.strftime('%Y-%m-%d %H:%M:%S.000')}"
    if employee_href:
        filter_str += f";owner={employee_href}"

    rows: list[dict] = []
    offset = 0
    page = 100
    max_pages = 10  # ≤1000 отгрузок на сотрудника — разумный потолок
    for _ in range(max_pages):
        data = await ms_get(
            "entity/demand",
            params={
                "filter": filter_str,
                "expand": "agent,owner",
                "order": "moment,desc",
                "limit": page,
                "offset": offset,
            },
        )
        chunk = data if isinstance(data, list) else data.get("rows", [])
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


@_ms_ttl_cache(ttl=60.0, name="get_employee_stats")
async def get_employee_stats(
    since: datetime,
    until: datetime | None = None,
    employee_href: str | None = None,
) -> dict:
    """Персональная статистика сотрудника."""
    shipments = await get_employee_shipments(since, until, employee_href)
    if not shipments:
        return {
            "total": 0,
            "count": 0,
            "clients": 0,
            "top_products": [],
            "by_day": {},
            "product_sums": {},
        }

    total = sum(s.get("sum", 0) for s in shipments)
    clients = len(
        set(s.get("agent", {}).get("name", "") for s in shipments if s.get("agent", {}).get("name"))
    )

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
    demand_ids: list[str] = []
    for s in shipments[:10]:  # ограничиваем, чтобы не превышать лимит MS
        did = extract_id_from_href(s.get("meta", {}).get("href", ""))
        if did:
            demand_ids.append(did)
    # Параллельно (см. комментарий в get_sales_stats) — независимые позиции.
    results = await asyncio.gather(
        *(get_shipment_positions(did) for did in demand_ids),
        return_exceptions=True,
    )
    for positions in results:
        if isinstance(positions, BaseException):
            continue
        for pos in positions:
            name = pos.get("assortment", {}).get("name", "—")
            qty = pos.get("quantity", 0)
            price = pos.get("price", 0)
            if name not in product_sums:
                product_sums[name] = {"sum": 0, "qty": 0}
            product_sums[name]["sum"] += qty * price
            product_sums[name]["qty"] += qty

    top_products = sorted(product_sums.items(), key=lambda x: x[1]["sum"], reverse=True)[:5]

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
