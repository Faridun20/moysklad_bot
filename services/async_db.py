"""
Async-обёртки для services.database (Этап 1 миграции на async-БД).

Зачем:
  services.database использует psycopg2 — синхронный драйвер. Каждый
  вызов get_*/add_*/update_* блокирует event loop на время SQL-запроса
  (типично 5-50 мс на быстрых запросах, до сотен мс на медленных). На
  высокой конкурентности это значит: пока один запрос обслуживается,
  все остальные ждут.

Как работает:
  Любой атрибут этого модуля — если он callable в services.database —
  возвращается обёрнутым в `asyncio.to_thread`. Вызов на async
  стороне выглядит так:

      from services import async_db as adb
      orders = await adb.get_user_orders(uid)

  Под капотом: коротенький wrapper запускает синхронную функцию в
  thread pool, освобождая event loop на время ожидания I/O. Сама
  функция и драйвер остаются прежними.

Что НЕ оборачивать:
  - Pure-Python helpers (now_str, q, get_cursor) — они не делают I/O,
    оборачивать смысла нет, только overhead.
  - get_role — в webapp используется services.roles.cached_role (TTL-
    кэш в памяти, микросекунды). Оборачивать тоже не надо.

Этап 2 (отложен): переписать services.database с psycopg2 на asyncpg.
Будет 2-3x быстрее за счёт реального async I/O, но требует переписать
весь SQL-API (другие сигнатуры курсоров, нет executemany и т.д.).
"""

import asyncio
import inspect

from services import database as _db

# Эти атрибуты НЕ нужно оборачивать — это либо чистые helper'ы без
# I/O, либо константы/контекстные менеджеры с особой семантикой.
_SKIP = frozenset(
    {
        "get_conn",  # contextmanager — обернуть нельзя, использовать редко
        "get_cursor",  # принимает уже открытый conn, sync wrapping излишен
        "q",  # просто string substitution
        "now_str",  # просто datetime → string
        "USE_POSTGRES",
        "DATABASE_URL",
        "DB_PATH",
        "SQL_SLOW_MS",
    }
)


def __getattr__(name: str):
    """Возвращает async-обёртку для функции из services.database.

    Реализовано через module-level __getattr__ (PEP 562) — все
    функции БД доступны как `adb.<имя>` без явного перечисления.
    """
    if name in _SKIP:
        return getattr(_db, name)

    obj = getattr(_db, name, None)
    if obj is None:
        raise AttributeError(f"services.async_db has no attribute {name!r}")
    if not callable(obj):
        return obj

    # async-функцию из database.py оборачивать не надо — она уже async
    if inspect.iscoroutinefunction(obj):
        return obj

    async def _wrapper(*args, **kwargs):
        return await asyncio.to_thread(obj, *args, **kwargs)

    _wrapper.__name__ = name
    _wrapper.__qualname__ = f"async_db.{name}"
    _wrapper.__doc__ = obj.__doc__
    return _wrapper
