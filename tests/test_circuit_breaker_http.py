"""T2.9 — circuit breaker МойСклад считает HTTP-ошибки.

Критерий готовности: пять ответов 500 открывают брейкер; пять ответов 400
не открывают.

§2.6: `resp.raise_for_status()` бросает `aiohttp.ClientResponseError`, а
`except` ловил только `TimeoutError` и `ClientConnectionError`. Исключение
уходило из функции МИМО `record_failure()` — и мимо `record_success()`.
Счётчик застывал, поэтому цепь открывалась только на таймаутах и сетевых
обрывах, хотя докстринг обещал «после 5 подряд провальных запросов».

Мокаем ТРАНСПОРТ (aioresponses): реально исполняется разбор статуса.
"""

import asyncio

import pytest
from aioresponses import aioresponses

import services.moysklad as ms
from services.moysklad import MS_BASE


@pytest.fixture(autouse=True)
def _fresh_breaker(monkeypatch):
    """Брейкер — модульный синглтон: сбрасываем до и после каждого теста.
    Один ретрай вместо трёх — тесты не должны спать на backoff'е."""
    ms._circuit.record_success()
    monkeypatch.setattr(ms, "_MAX_RETRIES", 1)
    yield
    ms._circuit.record_success()


def _call_n_times(status: int, n: int) -> list:
    """n вызовов ms_get, каждый отвечает `status`. Возвращает список исходов."""
    outcomes = []

    async def scenario():
        ms._session = None
        try:
            with aioresponses() as m:
                for _ in range(n):
                    m.get(f"{MS_BASE}/entity/product", status=status, payload={})
                for _ in range(n):
                    try:
                        await ms.ms_get("entity/product")
                        outcomes.append("ok")
                    except Exception as e:  # noqa: BLE001 — фиксируем тип исхода
                        outcomes.append(type(e).__name__)
        finally:
            await ms.close_session()

    asyncio.run(scenario())
    return outcomes


# ─── Критерий готовности ────────────────────────────────────────────────────


def test_five_server_errors_open_the_breaker():
    """500 — деградация МойСклад: считаем и открываем цепь."""
    assert ms._circuit.is_open() is False
    _call_n_times(500, ms._CircuitBreaker.OPEN_AFTER_FAILS)
    assert ms._circuit.is_open() is True, "цепь должна открыться после 5 провалов"


def test_five_client_errors_do_not_open_the_breaker():
    """400 — НАША ошибка (кривой фильтр, нет прав). Повтор даст то же самое,
    а открытая цепь положила бы все остальные МС-операции."""
    outcomes = _call_n_times(400, ms._CircuitBreaker.OPEN_AFTER_FAILS)
    assert ms._circuit.is_open() is False, "4xx не должна открывать цепь"
    # Ошибка при этом доходит до вызывающего, а не проглатывается.
    assert outcomes == ["ClientResponseError"] * ms._CircuitBreaker.OPEN_AFTER_FAILS


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_ms_side_failures_are_counted(status):
    _call_n_times(status, ms._CircuitBreaker.OPEN_AFTER_FAILS)
    assert ms._circuit.is_open() is True, f"{status} должен считаться провалом МС"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_our_errors_are_not_counted(status):
    _call_n_times(status, ms._CircuitBreaker.OPEN_AFTER_FAILS)
    assert ms._circuit.is_open() is False, f"{status} не должен открывать цепь"


# ─── Счётчик реально движется ───────────────────────────────────────────────


def test_failure_counter_advances_on_http_error():
    """Раньше счётчик застывал: исключение уходило мимо record_failure()."""
    before = ms._circuit._fails
    _call_n_times(500, 1)
    assert ms._circuit._fails == before + 1


def test_client_error_leaves_counter_untouched():
    """«Брейкер не трогаем» — ни +1 к провалам, ни сброс в 0."""
    _call_n_times(500, 2)
    mid = ms._circuit._fails
    assert mid == 2
    _call_n_times(400, 3)
    assert ms._circuit._fails == mid, "4xx не должна ни считаться, ни сбрасывать счётчик"


def test_success_closes_the_breaker():
    _call_n_times(500, ms._CircuitBreaker.OPEN_AFTER_FAILS - 1)
    assert ms._circuit._fails == ms._CircuitBreaker.OPEN_AFTER_FAILS - 1

    async def ok():
        ms._session = None
        try:
            with aioresponses() as m:
                m.get(f"{MS_BASE}/entity/product", status=200, payload={"rows": []})
                return await ms.ms_get("entity/product")
        finally:
            await ms.close_session()

    assert asyncio.run(ok()) == {"rows": []}
    assert ms._circuit._fails == 0
    assert ms._circuit.is_open() is False


def test_open_breaker_rejects_without_network():
    """Открытая цепь отклоняет сразу — не тратя таймаут на каждый запрос."""
    _call_n_times(500, ms._CircuitBreaker.OPEN_AFTER_FAILS)
    assert ms._circuit.is_open() is True

    async def blocked():
        ms._session = None
        try:
            with aioresponses():  # ни одного мока: сеть не должна тронуться
                await ms.ms_get("entity/product")
        finally:
            await ms.close_session()

    with pytest.raises(RuntimeError, match="circuit breaker"):
        asyncio.run(blocked())
