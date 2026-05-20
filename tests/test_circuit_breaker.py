"""
Тесты circuit breaker для МойСклад API — services.moysklad._CircuitBreaker.

Зачем: при длительном outage МойСклад каждый запрос висит до таймаута
(30с) и копит блокировки. Breaker после OPEN_AFTER_FAILS ошибок подряд
«размыкает цепь» — запросы падают сразу, не дёргая сеть. Через
HALF_OPEN_AFTER секунд пускает один пробный.

Время контролируем подменой time.monotonic — тесты детерминированы и
не зависят от реальных таймеров.
"""

import time

import pytest

from services.moysklad import _CircuitBreaker


@pytest.fixture
def clock(monkeypatch):
    """Управляемые «часы»: возвращает объект, у которого .now двигается
    вручную. Подменяет time.monotonic, который читает breaker."""
    holder = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: holder["t"])
    return holder


def test_starts_closed(clock):
    cb = _CircuitBreaker()
    assert cb.is_open() is False


def test_opens_only_after_threshold(clock):
    cb = _CircuitBreaker()
    # На один меньше порога — ещё закрыт
    for _ in range(_CircuitBreaker.OPEN_AFTER_FAILS - 1):
        cb.record_failure()
    assert cb.is_open() is False
    # Порог достигнут — размыкается
    cb.record_failure()
    assert cb.is_open() is True


def test_success_resets_failures(clock):
    cb = _CircuitBreaker()
    for _ in range(_CircuitBreaker.OPEN_AFTER_FAILS - 1):
        cb.record_failure()
    cb.record_success()  # сброс счётчика
    # После сброса ещё (порог-1) ошибок не должны открыть
    for _ in range(_CircuitBreaker.OPEN_AFTER_FAILS - 1):
        cb.record_failure()
    assert cb.is_open() is False


def test_half_opens_after_timeout(clock):
    cb = _CircuitBreaker()
    for _ in range(_CircuitBreaker.OPEN_AFTER_FAILS):
        cb.record_failure()
    assert cb.is_open() is True
    # Чуть-чуть не дождались — всё ещё открыт
    clock["t"] += _CircuitBreaker.HALF_OPEN_AFTER - 1
    assert cb.is_open() is True
    # Таймаут прошёл — half-open: пускаем пробный
    clock["t"] += 2
    assert cb.is_open() is False


def test_success_after_half_open_fully_closes(clock):
    cb = _CircuitBreaker()
    for _ in range(_CircuitBreaker.OPEN_AFTER_FAILS):
        cb.record_failure()
    clock["t"] += _CircuitBreaker.HALF_OPEN_AFTER + 1
    assert cb.is_open() is False  # half-open
    cb.record_success()           # пробный удался → полностью закрыт
    assert cb.is_open() is False
    # И счётчик обнулён — нужен снова полный порог чтобы открыть
    for _ in range(_CircuitBreaker.OPEN_AFTER_FAILS - 1):
        cb.record_failure()
    assert cb.is_open() is False


def test_failed_probe_reopens_circuit(clock):
    """Если пробный запрос в half-open снова падает — цепь должна
    переоткрыться со свежим таймером, а не пропускать всё подряд.

    Регрессия: раньше record_failure не обновлял _opened_at пока он
    не None, и breaker навсегда застревал в half-open (без защиты)."""
    cb = _CircuitBreaker()
    for _ in range(_CircuitBreaker.OPEN_AFTER_FAILS):
        cb.record_failure()
    # Ждём таймаут → half-open
    clock["t"] += _CircuitBreaker.HALF_OPEN_AFTER + 1
    assert cb.is_open() is False
    # Пробный запрос упал — цепь снова открыта, новый таймер пошёл
    cb.record_failure()
    assert cb.is_open() is True
    # И не открывается раньше нового HALF_OPEN_AFTER
    clock["t"] += _CircuitBreaker.HALF_OPEN_AFTER - 1
    assert cb.is_open() is True
    clock["t"] += 2
    assert cb.is_open() is False
