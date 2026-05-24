"""
Lightweight in-process metrics — counters + timing histograms.

Дизайн:
  * **в памяти процесса**, без Prometheus/StatsD. Цель — diagnostics
    («где тормозит / что валится прямо сейчас»), не долговременный TSDB.
    Каждый bot/webapp процесс собирает свою картину; для prod это OK,
    так как у нас один webapp процесс на сервис в Railway.
  * **rolling window** через `deque(maxlen=N)` — latency-сэмплы
    автоматически вытесняются старыми по достижении кэпа. По умолчанию
    1024 точки на метрику → ~последний час трафика для типичной
    нагрузки. p50/p95 считаем по текущему окну.
  * **counters** — обычные int'ы (общий счётчик с момента старта
    процесса). Не overflow'нут реалистично (Python int unbounded).
  * **thread-safe** — `threading.Lock` per-metric. asyncio coroutines
    тоже безопасны: GIL + Lock защищают list-операции.
  * **гранулярность** — string-имя метрики («api.analytics», «ms.create_demand»).
    Лейблы (типа `status=200`) не поддерживаем — для нашей дозы
    избыточно; разные исходы пишем под разными именами (foo.ok / foo.error).

Использование:
    from services import metrics

    # counter
    metrics.incr("api.home.calls")

    # timing (sync context manager)
    with metrics.timer("api.home.latency"):
        do_work()

    # timing (async)
    async with metrics.atimer("ms.create_demand.latency"):
        await create_demand(...)

    # snapshot для /api/metrics endpoint
    metrics.snapshot()  # → {"counters": {...}, "timings": {...}}
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from threading import Lock
from typing import Any

# Размер ring-буфера на метрику. 1024 точки даёт честный p95 при
# умеренной нагрузке и фиксированную память (~8KB на метрику).
_HISTOGRAM_WINDOW = 1024

# Глобальное состояние процесса. Внутри обёрнуто Lock'ом — публичный
# API не выдаёт ссылки наружу, читатели/писатели работают через хелперы.
_counters: dict[str, int] = defaultdict(int)
_timings: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_HISTOGRAM_WINDOW))
_lock = Lock()

# Стартовое время процесса — для uptime в snapshot.
_started_at = time.time()


def incr(name: str, value: int = 1) -> None:
    """Увеличить счётчик. Создаётся лениво при первом вызове."""
    if not name:
        return
    with _lock:
        _counters[name] += int(value)


def record_timing(name: str, duration_ms: float) -> None:
    """Записать latency-сэмпл (в миллисекундах) в rolling-window метрики."""
    if not name or duration_ms < 0:
        return
    with _lock:
        _timings[name].append(float(duration_ms))


@contextmanager
def timer(name: str):
    """Sync context manager — измеряет время блока и пишет в `name`.

    Использование:
        with metrics.timer("operation"):
            do_work()
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        record_timing(name, (time.perf_counter() - start) * 1000.0)


@asynccontextmanager
async def atimer(name: str):
    """Async-версия timer. Не отличается семантически от sync — счётчик
    real-time, не CPU; работает одинаково внутри coroutine."""
    start = time.perf_counter()
    try:
        yield
    finally:
        record_timing(name, (time.perf_counter() - start) * 1000.0)


def _percentile(samples: list[float], p: float) -> float:
    """p-percentile (0..1). Простая «nearest-rank» реализация — для нашей
    диагностики (p50/p95) точности достаточно. На пустом списке → 0.0."""
    if not samples:
        return 0.0
    s = sorted(samples)
    if p <= 0:
        return s[0]
    if p >= 1:
        return s[-1]
    idx = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[idx]


def snapshot() -> dict[str, Any]:
    """Замороженный отчёт текущего состояния. Безопасно сериализуется в JSON.

    Возвращает:
        {
          "uptime_sec": float,
          "counters": {"api.home.calls": 1234, ...},
          "timings": {
            "api.home.latency": {
              "count": 256, "p50_ms": 12.3, "p95_ms": 145.8, "max_ms": 320.0
            },
            ...
          }
        }
    """
    with _lock:
        counters = dict(_counters)
        # Копируем сэмплы вне lock'а считать p* — сами расчёты могут быть
        # не-микроскопическими (sort 1024 floats), не хотим держать lock
        # дольше необходимого.
        timing_copies = {name: list(buf) for name, buf in _timings.items()}

    timings: dict[str, dict[str, float | int]] = {}
    for name, samples in timing_copies.items():
        if not samples:
            continue
        timings[name] = {
            "count": len(samples),
            "p50_ms": round(_percentile(samples, 0.50), 2),
            "p95_ms": round(_percentile(samples, 0.95), 2),
            "max_ms": round(max(samples), 2),
        }
    return {
        "uptime_sec": round(time.time() - _started_at, 1),
        "counters": counters,
        "timings": timings,
    }


def reset() -> None:
    """Сбросить все метрики (для тестов / debug-эндпоинта)."""
    global _started_at
    with _lock:
        _counters.clear()
        _timings.clear()
        _started_at = time.time()


# ─── Удобные high-level декораторы / wrappers ─────────────────────────────────


def measure_async(name: str):
    """Декоратор для async функций: пишет timing и `.error` counter при exception.

    Использование:
        @measure_async("ms.create_demand")
        async def create_demand_from_request(...): ...

    После прохода: _timings["ms.create_demand"] получит сэмпл, и при
    исключении: _counters["ms.create_demand.error"] += 1 + reraise.
    Успешные вызовы дополнительно incr'ят `.ok` для error-rate расчёта.
    """

    def deco(fn):
        async def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await fn(*args, **kwargs)
                incr(f"{name}.ok")
                return result
            except Exception:
                incr(f"{name}.error")
                raise
            finally:
                record_timing(name, (time.perf_counter() - start) * 1000.0)

        # Сохраняем сигнатуру/имя для FastAPI/aiogram introspection.
        wrapper.__name__ = getattr(fn, "__name__", "wrapper")
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    return deco


__all__ = [
    "incr",
    "record_timing",
    "timer",
    "atimer",
    "measure_async",
    "snapshot",
    "reset",
]


# Удерживаем ссылку на asyncio чтобы убедиться что модуль не пустой
# в линт-моде (asyncio импортирован для type-hint совместимости с
# asynccontextmanager).
_ = asyncio
