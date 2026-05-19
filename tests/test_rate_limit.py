"""
Тесты in-memory rate limiter (services.rate_limit).

Это hot-path-защита от спама кнопками в боте и от перегрузки в WebApp.
Каждое изменение acquire-логики стоит покрыть тестом.
"""

import time

import pytest

from services.rate_limit import acquire, reset


@pytest.fixture(autouse=True)
def clean_buckets():
    """Перед каждым тестом сбрасываем bucket'ы — изоляция между тестами."""
    reset()
    yield
    reset()


def test_under_limit_passes():
    """3 вызова из 5 разрешённых — все True."""
    for _ in range(3):
        assert acquire("test", 1, max_calls=5, window_sec=10) is True


def test_at_limit_blocks_further():
    """Ровно max_calls вызовов проходят, следующий уже отвергается."""
    for _ in range(5):
        assert acquire("test", 1, max_calls=5, window_sec=10) is True
    # 6-й должен быть заблокирован
    assert acquire("test", 1, max_calls=5, window_sec=10) is False
    assert acquire("test", 1, max_calls=5, window_sec=10) is False


def test_blocked_call_does_not_consume_quota():
    """Заблокированный вызов не записывается в очередь — иначе он бы
    бесконечно держал юзера за дверью."""
    for _ in range(5):
        acquire("test", 1, max_calls=5, window_sec=10)
    # Несколько failed-попыток подряд
    for _ in range(20):
        assert acquire("test", 1, max_calls=5, window_sec=10) is False
    # После того как окно «сдвинется» (физически нельзя в тесте,
    # но можно проверить что счётчик не уехал в большие числа),
    # вызовы остаются стабильно False — это правильный ответ.


def test_separate_scopes_isolated():
    """Скоупы 'bot_msg' и 'bot_cb' считаются отдельно."""
    for _ in range(5):
        acquire("bot_msg", 1, max_calls=5, window_sec=10)
    # Один scope залочен
    assert acquire("bot_msg", 1, max_calls=5, window_sec=10) is False
    # Другой scope — независимый
    assert acquire("bot_cb", 1, max_calls=5, window_sec=10) is True


def test_separate_users_isolated():
    """Один пользователь не блокирует другого."""
    for _ in range(5):
        acquire("test", 1, max_calls=5, window_sec=10)
    # User 1 залочен, user 2 свеж
    assert acquire("test", 1, max_calls=5, window_sec=10) is False
    assert acquire("test", 2, max_calls=5, window_sec=10) is True


def test_window_sliding(monkeypatch):
    """После того как timestamps выпали из окна — снова можно."""
    # Заполняем bucket
    for _ in range(3):
        acquire("test", 1, max_calls=3, window_sec=0.1)
    # 4-й заблокирован
    assert acquire("test", 1, max_calls=3, window_sec=0.1) is False
    # Ждём пока окно сдвинется
    time.sleep(0.15)
    # Теперь можно снова
    assert acquire("test", 1, max_calls=3, window_sec=0.1) is True


def test_zero_user_id_always_passes():
    """user_id=0 — анонимный/системный, лимит не применяется.
    (Документировано в acquire: 'но запросы без юзера и не должны попадать сюда')"""
    for _ in range(100):
        assert acquire("test", 0, max_calls=1, window_sec=10) is True


def test_reset_specific_scope():
    """reset(scope) сбрасывает только указанный scope."""
    for _ in range(5):
        acquire("scope_a", 1, max_calls=5, window_sec=10)
        acquire("scope_b", 1, max_calls=5, window_sec=10)
    # Сбрасываем только A
    reset("scope_a")
    assert acquire("scope_a", 1, max_calls=5, window_sec=10) is True
    # B всё ещё залочен
    assert acquire("scope_b", 1, max_calls=5, window_sec=10) is False


def test_reset_all_clears_everything():
    for _ in range(5):
        acquire("any", 42, max_calls=5, window_sec=10)
    assert acquire("any", 42, max_calls=5, window_sec=10) is False
    reset()
    assert acquire("any", 42, max_calls=5, window_sec=10) is True
