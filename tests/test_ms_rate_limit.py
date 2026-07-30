"""
MS-1/MS-2 (волна 7) — лимиты МойСклад: backoff по реальным заголовкам,
различение 1049/1073, упреждающая пауза и брейкер по доле 429.

Почему это важнее обычного ретрая: больше 200 отказов в минуту в течение часа —
и МойСклад отключает аккаунту доступ к API, восстановление только через
поддержку. Для бизнеса это остановка отгрузок, а не деградация.

Мокаем ТРАНСПОРТ (aioresponses) — реально исполняется разбор статуса,
заголовков и тела.
"""

import asyncio

import pytest
from aioresponses import aioresponses

import services.moysklad as ms
from services.moysklad import MS_BASE


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    ms._circuit.record_success()
    yield
    ms._circuit.record_success()


# ─── Разбор заголовков ────────────────────────────────────────────────────────


def test_delay_prefers_lognex_retry_after_in_ms():
    """X-Lognex-Retry-After — в МИЛЛИсекундах. 1500 → 1,5 с, а не 1500 с."""
    assert ms._retry_delay({"X-Lognex-Retry-After": "1500"}, 0) == 1.5


def test_delay_falls_back_to_reset_then_exponential():
    assert ms._retry_delay({"X-Lognex-Reset": "800"}, 0) == 0.8
    # Reset=0 значит «ограничений нет» — это не нулевая пауза, а отсутствие
    # информации: уходим на экспоненту.
    assert ms._retry_delay({"X-Lognex-Reset": "0"}, 1) == ms._RETRY_BASE_DELAY * 2
    assert ms._retry_delay({}, 2) == ms._RETRY_BASE_DELAY * 4


def test_standard_retry_after_is_ignored():
    """Стандартного Retry-After МойСклад не присылает; если он вдруг приедет от
    прокси — не путаем секунды с миллисекундами, идём по экспоненте."""
    assert ms._retry_delay({"Retry-After": "120"}, 0) == ms._RETRY_BASE_DELAY


def test_delay_is_capped():
    """Сервер может попросить ждать минуту — воркер столько висеть не должен."""
    assert ms._retry_delay({"X-Lognex-Retry-After": "600000"}, 0) == ms._MAX_RETRY_DELAY


def test_proactive_pause_only_when_budget_is_out():
    assert ms._proactive_pause({"X-RateLimit-Remaining": "17"}) == 0.0
    assert ms._proactive_pause({"X-RateLimit-Remaining": "0", "X-Lognex-Reset": "200"}) == 0.2
    # Заголовков нет вовсе (старый ответ/прокси) — не тормозим на пустом месте.
    assert ms._proactive_pause({}) == 0.0


def test_proactive_pause_is_capped():
    pause = ms._proactive_pause({"X-RateLimit-Remaining": "0", "X-Lognex-Reset": "60000"})
    assert pause == ms._MAX_PROACTIVE_PAUSE


# ─── Поведение ms_get ─────────────────────────────────────────────────────────


def _run_get(mock_setup, *, retries: int = 2, monkeypatch=None):
    async def scenario():
        ms._session = None
        try:
            with aioresponses() as m:
                mock_setup(m)
                return await ms.ms_get("entity/organization")
        finally:
            await ms.close_session()

    if monkeypatch is not None:
        monkeypatch.setattr(ms, "_MAX_RETRIES", retries)
    return asyncio.run(scenario())


def test_429_by_rate_waits_header_and_succeeds(monkeypatch):
    """1049 — превышен темп: ждём столько, сколько сказал сервер, и повторяем."""
    slept: list[float] = []

    async def _fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(ms.asyncio, "sleep", _fake_sleep)

    def setup(m):
        m.get(
            f"{MS_BASE}/entity/organization",
            status=429,
            headers={"X-Lognex-Retry-After": "1500"},
            payload={"errors": [{"code": 1049, "error": "Превышен лимит"}]},
        )
        m.get(f"{MS_BASE}/entity/organization", status=200, payload={"rows": []})

    assert _run_get(setup, monkeypatch=monkeypatch) == {"rows": []}
    assert 1.5 in slept


def test_429_by_parallelism_does_not_use_long_backoff(monkeypatch):
    """1073 — превышена ПАРАЛЛЕЛЬНОСТЬ: ожидание не лечит, длинная пауза только
    тормозит. Ждём коротко и логируем отдельно."""
    slept: list[float] = []

    async def _fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(ms.asyncio, "sleep", _fake_sleep)

    def setup(m):
        m.get(
            f"{MS_BASE}/entity/organization",
            status=429,
            headers={"X-Lognex-Retry-After": "9000"},  # сервер просит долго ждать
            payload={"errors": [{"code": 1073, "error": "Превышен лимит параллельности"}]},
        )
        m.get(f"{MS_BASE}/entity/organization", status=200, payload={"ok": True})

    assert _run_get(setup, monkeypatch=monkeypatch) == {"ok": True}
    assert slept == [ms._PARALLEL_RETRY_DELAY]
    assert 9.0 not in slept


def test_non_json_429_body_still_retries(monkeypatch):
    """Тело 429 может прийти HTML-заглушкой от прокси — разбор кода не должен
    ронять ретрай."""
    async def _no_sleep(_sec):
        return None

    monkeypatch.setattr(ms.asyncio, "sleep", _no_sleep)

    def setup(m):
        m.get(f"{MS_BASE}/entity/organization", status=429, body="<html>429</html>")
        m.get(f"{MS_BASE}/entity/organization", status=200, payload={"ok": 1})

    assert _run_get(setup, monkeypatch=monkeypatch) == {"ok": 1}


# ─── Брейкер по доле 429 ──────────────────────────────────────────────────────


def test_breaker_opens_on_share_of_429_without_consecutive_failures():
    """«Каждый третий запрос — 429» не даёт подряд идущих ошибок, поэтому
    старый счётчик не срабатывал никогда — а именно этот режим уводит аккаунт
    в автоотключение."""
    breaker = ms._CircuitBreaker()
    for i in range(30):
        breaker.record_call(rate_limited=(i % 3 == 0))
    assert breaker.is_open() is True


def test_breaker_stays_closed_on_rare_429():
    breaker = ms._CircuitBreaker()
    for i in range(40):
        breaker.record_call(rate_limited=(i == 7))
    assert breaker.is_open() is False


def test_breaker_needs_minimum_sample():
    """Два запроса, оба 429 — это ещё не «доля», а совпадение: на таком
    основании глушить весь МС нельзя."""
    breaker = ms._CircuitBreaker()
    breaker.record_call(rate_limited=True)
    breaker.record_call(rate_limited=True)
    assert breaker.is_open() is False


def test_success_clears_the_window():
    breaker = ms._CircuitBreaker()
    for _ in range(25):
        breaker.record_call(rate_limited=True)
    breaker.record_success()
    assert breaker.is_open() is False
    assert not breaker._window


# ─── MS-2: конкурентность в рамках лимита ─────────────────────────────────────


def test_concurrency_stays_within_documented_limit():
    """Лимит МойСклад — 5 параллельных запросов на пользователя, 20 на аккаунт.
    Наш предел ниже намеренно: поверх одного аккаунта работают бот, webapp и
    cron'ы, у каждого свой семафор. Поднимать без пересчёта всех потребителей
    нельзя — часть 429 придёт кодом 1073, который backoff не лечит."""
    assert ms._MS_PARALLEL_LIMIT <= 4
    assert ms._POSITIONS_CONCURRENCY_LIMIT == ms._MS_PARALLEL_LIMIT
    assert ms._HTTP_CONCURRENCY <= 20


def test_reconcile_uses_the_same_limit():
    """Cron бежит одновременно с ботом поверх того же аккаунта — своей
    восьмёрки у него быть не должно."""
    import inspect

    from tasks import run_ms_reconcile

    src = inspect.getsource(run_ms_reconcile.main)
    assert "_MS_PARALLEL_LIMIT" in src
    assert "Semaphore(8)" not in src
