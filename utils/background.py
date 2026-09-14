"""Фоновые задачи процесса: сильная ссылка + лог необработанного исключения.

`asyncio.create_task` без ссылки — задача, которую GC может убить до
завершения, а её исключение никто не увидит. Здесь одна точка на бот и
WebApp: задача живёт в `_tasks`, пока не завершится, а падение уходит в лог с
именем задачи.

Что сюда уводить: работу, результата которой вызывающий НЕ ждёт и ошибка
которой не должна отменять уже сделанное — печатная форма после отгрузки,
уведомления после коммита. Что НЕ уводить: всё, что должно быть в ответе.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
    """Запустить корутину фоном. Возвращает Task — тесты могут его дождаться."""
    task = asyncio.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    def _log_exc(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error("Фоновая задача %s упала", name, exc_info=t.exception())

    task.add_done_callback(_log_exc)
    return task


def pending() -> set[asyncio.Task]:
    """Незавершённые фоновые задачи (для тестов и graceful shutdown)."""
    return {t for t in _tasks if not t.done()}
