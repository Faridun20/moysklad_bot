"""
Тесты services/metrics.py — counters + timing histograms.

Цель: проверить чистую логику (percentile, ring-buffer cap, decorator
wraps exception/success разный counter). Без сети, без БД.
"""

import asyncio
import importlib

import pytest


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Свежие метрики на каждый тест — иначе counters текут между."""
    import services.metrics as m

    importlib.reload(m)
    yield m
    m.reset()


def test_incr_creates_and_accumulates(_reset_metrics):
    m = _reset_metrics
    m.incr("api.calls")
    m.incr("api.calls")
    m.incr("api.calls", 3)
    snap = m.snapshot()
    assert snap["counters"]["api.calls"] == 5


def test_incr_ignores_empty_name(_reset_metrics):
    m = _reset_metrics
    m.incr("")
    assert m.snapshot()["counters"] == {}


def test_record_timing_and_percentiles(_reset_metrics):
    """100 равномерных сэмплов 1..100ms → p50≈50, p95≈95."""
    m = _reset_metrics
    for i in range(1, 101):
        m.record_timing("op", float(i))
    snap = m.snapshot()
    t = snap["timings"]["op"]
    assert t["count"] == 100
    assert 49 <= t["p50_ms"] <= 51
    assert 94 <= t["p95_ms"] <= 96
    assert t["max_ms"] == 100.0


def test_record_timing_ignores_invalid(_reset_metrics):
    m = _reset_metrics
    m.record_timing("", 10.0)  # пустое имя
    m.record_timing("op", -1.0)  # отрицательное
    assert m.snapshot()["timings"] == {}


def test_record_timing_ring_buffer_cap(_reset_metrics):
    """>1024 сэмплов → старые вытесняются, count остаётся 1024."""
    m = _reset_metrics
    # 2000 сэмплов; последние 1024 — значения 977..2000 (значения = i+1)
    for i in range(2000):
        m.record_timing("op", float(i + 1))
    snap = m.snapshot()
    t = snap["timings"]["op"]
    assert t["count"] == 1024  # capped
    # max — это последнее значение (2000), его не выкинули.
    assert t["max_ms"] == 2000.0
    # p50 у окна [977..2000] = ~1488
    assert 1450 <= t["p50_ms"] <= 1520


def test_timer_context_manager_records_latency(_reset_metrics):
    m = _reset_metrics
    with m.timer("block"):
        # Спим какое-то измеримое время. Используем busy-wait чтобы
        # тест не зависел от точности time.sleep на CI.
        import time

        end = time.perf_counter() + 0.001  # 1ms
        while time.perf_counter() < end:
            pass
    snap = m.snapshot()
    assert snap["timings"]["block"]["count"] == 1
    assert snap["timings"]["block"]["max_ms"] > 0


def test_atimer_async_context_manager_records(_reset_metrics):
    m = _reset_metrics

    async def scenario():
        async with m.atimer("ablock"):
            await asyncio.sleep(0.001)

    asyncio.run(scenario())
    snap = m.snapshot()
    assert snap["timings"]["ablock"]["count"] == 1


def test_measure_async_decorator_records_success_and_timing(_reset_metrics):
    m = _reset_metrics

    @m.measure_async("op")
    async def do_work():
        await asyncio.sleep(0.001)
        return 42

    result = asyncio.run(do_work())
    assert result == 42
    snap = m.snapshot()
    assert snap["counters"]["op.ok"] == 1
    assert snap["counters"].get("op.error", 0) == 0
    assert snap["timings"]["op"]["count"] == 1


def test_measure_async_decorator_records_failure_and_reraises(_reset_metrics):
    m = _reset_metrics

    @m.measure_async("op")
    async def explode():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(explode())

    snap = m.snapshot()
    assert snap["counters"].get("op.ok", 0) == 0
    assert snap["counters"]["op.error"] == 1
    # И timing записан — мы знаем сколько падает (важно: 5xx slow == проблема)
    assert snap["timings"]["op"]["count"] == 1


def test_measure_async_preserves_function_name(_reset_metrics):
    """FastAPI / aiogram могут полагаться на __name__/__wrapped__ для
    introspection — декоратор должен их сохранить."""
    m = _reset_metrics

    @m.measure_async("op")
    async def named_function():
        return None

    assert named_function.__name__ == "named_function"
    assert hasattr(named_function, "__wrapped__")


def test_snapshot_includes_uptime(_reset_metrics):
    m = _reset_metrics
    snap = m.snapshot()
    assert "uptime_sec" in snap
    assert snap["uptime_sec"] >= 0


def test_snapshot_empty_for_unused_metrics(_reset_metrics):
    m = _reset_metrics
    snap = m.snapshot()
    # Нет ни counters, ни timings — пустые dict'ы
    assert snap["counters"] == {}
    assert snap["timings"] == {}


def test_reset_clears_everything(_reset_metrics):
    m = _reset_metrics
    m.incr("a")
    m.record_timing("t", 100.0)
    m.reset()
    snap = m.snapshot()
    assert snap["counters"] == {}
    assert snap["timings"] == {}


def test_percentile_edge_cases():
    """_percentile должен корректно работать на p=0/p=1 и пустых списках."""
    from services.metrics import _percentile

    assert _percentile([], 0.5) == 0.0
    assert _percentile([10.0], 0.5) == 10.0
    assert _percentile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert _percentile([1.0, 2.0, 3.0], 1.0) == 3.0
