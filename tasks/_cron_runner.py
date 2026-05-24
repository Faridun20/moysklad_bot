"""
Общая обёртка для CLI-cron'ов: Sentry + cron_runs телеметрия.

Каждый `tasks/run_*.py` в своём `__main__` вызывает `run_cron(task_name, main)`.
Обёртка:
  * `init_sentry(component="cron-<task_name>")` — no-op без SENTRY_DSN;
  * замеряет время main();
  * по результату (rc=0 / exception) делает `record_cron_run(...)` в
    таблицу `cron_runs` — ops_monitor читает её и алертит про stale'ы;
  * exception'ы — `capture_exception` в Sentry перед re-raise (process
    падает с rc=1 как раньше; supervisor видит crash, аналитика
    включена).

Поддерживает async (`asyncio.run(main())`) и sync (`main()`) callables —
по сигнатуре. Возвращает rc для sys.exit'а вызывающего.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Можно вызывать как sync→int, sync→None, async→int, async→None.
CronMain = Callable[..., Any] | Callable[..., Awaitable[Any]]


def run_cron(task_name: str, main: CronMain, *args: Any, **kwargs: Any) -> int:
    """Запустить main() как cron-задачу.

    `task_name` — короткое имя для cron_runs (например, 'ms_sync_retry').
    Используется и для Sentry component = 'cron-<task_name>'.

    Возвращает int rc (0 = OK, 1 = exception). Вызывающий должен сам
    делать `sys.exit(rc)` — обёртка не вызывает sys.exit, чтобы CLI
    мог при необходимости что-то ещё сделать перед exit'ом.

    Если main() — async function, оборачиваем `asyncio.run(main(...))`.
    Если sync — вызываем напрямую. rc определяется так: если main
    вернул int — используем; если None — 0 (success).
    """
    # Импортируем тут, не на module-level: tasks/_cron_runner.py может
    # быть импортирован в тестах ДО init_db, не хочется тащить тяжёлые
    # зависимости (psycopg2 pool init) если cron не запускается.
    from services.database import init_db, record_cron_run
    from services.observability import (
        capture_exception,
        init_sentry,
    )

    init_sentry(component=f"cron-{task_name}")

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.monotonic()
    rc = 1
    err: str | None = None
    exc: BaseException | None = None

    try:
        result = main(*args, **kwargs)
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
        # rc: int если main вернул, иначе 0 (success без явного rc).
        rc = int(result) if isinstance(result, int) else 0
    except BaseException as e:
        err = f"{type(e).__name__}: {e}"[:1000]
        exc = e
        capture_exception(e)
        rc = 1
    finally:
        # Гарантируем init_db перед записью — main() мог упасть ДО своего
        # init_db, или его вообще не делать (некоторые CLI полагаются на
        # уже-проинициализированную БД). Если init_db сам бросает —
        # глотаем, чтобы не маскировать оригинальный exception.
        try:
            init_db()
            record_cron_run(
                task_name=task_name,
                status="ok" if rc == 0 else "failed",
                started_at=started_at,
                finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                duration_ms=int((time.monotonic() - t0) * 1000),
                error_message=err,
            )
        except Exception:
            logger.exception("record_cron_run failed (cron=%s, rc=%d)", task_name, rc)

    if exc is not None and rc != 0:
        # Перезбрасываем оригинал — supervisor / Railway увидят crash,
        # а Sentry уже сохранил event'у с stack trace выше.
        # Но для предсказуемости лучше отдать rc — supervisor увидит
        # ненулевой код, traceback уже залогирован в `except`.
        # Перезбрасывать БЕЗ raise здесь — потеряем traceback в stderr.
        # Компромисс: traceback ИДЕТ в logger.exception (его делает
        # caller через `logger.exception` в except внутри main()) и в
        # Sentry. Здесь возвращаем rc — это и есть «cron упал», логи
        # уже залогированы.
        pass

    return rc
