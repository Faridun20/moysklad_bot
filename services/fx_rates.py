"""
Получение курса валют от ЦБ РУз (CBU) — официальный источник.

bank.uz в «шапке» показывает ровно курс ЦБ, но не отдаёт чистый API
(пришлось бы парсить HTML и выбирать конкретный банк/покупку-продажу).
CBU отдаёт чистый JSON, есть архив по датам → используем его.

Эндпоинты (проверено):
  Текущий USD:   https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/
  USD на дату:   https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/YYYY-MM-DD/
Ответ — массив из одного объекта: {"Ccy":"USD","Rate":"12052.05","Nominal":"1",
"Date":"18.06.2026", ...}. `Rate` = сум за 1 USD.

Граница с внешним миром — изолирована тут, мокается в тестах через aioresponses.
Свой минимальный aiohttp-клиент по образцу services/moysklad.py (без MS-хедеров).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

import aiohttp

logger = logging.getLogger(__name__)

CBU_BASE = "https://cbu.uz/ru/arkhiv-kursov-valyut/json"

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # сек, удваивается на каждой попытке
_RETRY_STATUSES = {429, 500, 502, 503, 504}

_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def get_session() -> aiohttp.ClientSession:
    """Глобальная сессия, создаётся при первом обращении."""
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
                _session = aiohttp.ClientSession(timeout=_HTTP_TIMEOUT, connector=connector)
    return _session


async def close_session() -> None:
    """Закрыть сессию (вызывать в finally cron-задачи)."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _cbu_get(url: str) -> list:
    """GET с ретраями на сеть/429/5xx. Возвращает распарсенный JSON (массив)."""
    sess = await get_session()
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            async with sess.get(url) as resp:
                if resp.status in _RETRY_STATUSES and attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2**attempt)
                    logger.warning(
                        "CBU %s → %s, retry %d/%d через %.1fs",
                        url, resp.status, attempt + 1, _MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientConnectionError) as e:
            last_exc = e
            if attempt >= _MAX_RETRIES - 1:
                break
            delay = _RETRY_BASE_DELAY * (2**attempt)
            logger.warning(
                "CBU %s → %s, retry %d/%d через %.1fs",
                url, type(e).__name__, attempt + 1, _MAX_RETRIES, delay,
            )
            await asyncio.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"CBU {url}: исчерпаны {_MAX_RETRIES} попыток")


async def fetch_cbu_usd_per_uzs(on_date: date | None = None) -> float:
    """Сколько сум стоит 1 USD по курсу ЦБ (поле `Rate`).

    `on_date` — конкретный день (для бэкфилла истории), None → последний курс.
    Raise при сетевой ошибке или если USD не найден / Rate невалиден.
    """
    if on_date is not None:
        url = f"{CBU_BASE}/USD/{on_date.strftime('%Y-%m-%d')}/"
    else:
        url = f"{CBU_BASE}/USD/"
    data = await _cbu_get(url)
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"CBU вернул неожиданный ответ для USD: {data!r:.200}")
    row = data[0]
    rate_str = row.get("Rate") if isinstance(row, dict) else None
    try:
        rate = float(rate_str)
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"CBU: невалидный Rate для USD: {rate_str!r}") from e
    if rate <= 0:
        raise RuntimeError(f"CBU: неположительный Rate для USD: {rate}")
    return rate


def usd_uzs_to_rate_to_base(usd_per_uzs: float) -> float:
    """CBU отдаёт сум-за-1-USD; наша семантика rate_to_base = «1 UZS = X USD».

    Чистый хелпер (тестируется без сети): 1 / (сум за USD).
    """
    usd_per_uzs = float(usd_per_uzs)
    if usd_per_uzs <= 0:
        raise ValueError(f"usd_per_uzs должен быть > 0, получено {usd_per_uzs}")
    return 1.0 / usd_per_uzs
