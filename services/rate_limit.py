"""
Простой in-memory rate limiter.

Используется в двух местах:
  - aiogram middleware для бот-сообщений и callback'ов (защита от спама
    кнопками и сообщениями)
  - _authorize helper в webapp/server.py для дорогих endpoint'ов
    (всё что бьёт в МойСклад API или шлёт Telegram-уведомления)

Реализация: скользящее окно по timestamp'ам, на каждого юзера/скоуп
своя очередь. Без Redis — хватает на тысячи активных юзеров. При
перезапуске история сбрасывается (sliding window заполняется заново).

Памяти: на каждого активного юзера хранится N последних timestamp'ов
(N = max_calls лимита), это считаные сотни байт. Дополнительно есть
ленивая чистка устаревших ключей в acquire().
"""

import time
from collections import deque

# Ключ: (scope, user_id) → очередь monotonic timestamp'ов вызовов
_buckets: dict[tuple[str, int], deque[float]] = {}

# Сколько ключей-«сирот» удалять за один acquire (чтобы не разрастаться).
_GC_PROBE_KEYS = 50
_gc_cursor = 0


def acquire(scope: str, user_id: int, max_calls: int, window_sec: float) -> bool:
    """
    Записать попытку в bucket и вернуть True если она в пределах лимита.
    Если max_calls вызовов уже было за последние window_sec секунд —
    вернёт False и НЕ запишет новую попытку (чтобы юзер не «толкался
    в дверь» бесплатно).
    """
    global _gc_cursor
    if not user_id:
        return True  # анонимный — пропускаем (но запросы без юзера и не должны попадать сюда)

    now = time.monotonic()
    key = (scope, int(user_id))
    bucket = _buckets.get(key)
    if bucket is None:
        bucket = deque()
        _buckets[key] = bucket

    # Очистить просроченные timestamps в текущем bucket
    while bucket and now - bucket[0] >= window_sec:
        bucket.popleft()

    if len(bucket) >= max_calls:
        return False

    bucket.append(now)

    # Ленивый GC: каждую N-ю попытку «продвигаем курсор» и выбрасываем
    # ключи, у которых очередь полностью протухла.
    _gc_cursor += 1
    if _gc_cursor >= _GC_PROBE_KEYS:
        _gc_cursor = 0
        _gc_sweep(now, window_sec)

    return True


def _gc_sweep(now: float, window_sec: float) -> None:
    """Удалить ключи, у которых ни одного актуального timestamp'а."""
    stale = []
    for k, q in _buckets.items():
        while q and now - q[0] >= window_sec:
            q.popleft()
        if not q:
            stale.append(k)
    for k in stale:
        _buckets.pop(k, None)


def reset(scope: str | None = None) -> None:
    """Сброс — для тестов или ручного «отпустить» юзера."""
    if scope is None:
        _buckets.clear()
        return
    for key in list(_buckets.keys()):
        if key[0] == scope:
            _buckets.pop(key, None)
