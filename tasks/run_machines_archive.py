"""
CLI: архивация проданной техники (волна 4, T4.3). Railway Cron, раз в сутки.

Машины в статусе `sold`, проданные больше `machines_archive_days` дней назад
(по умолчанию 90), уходят в `archived`. Физически ничего не удаляем: история
цен нужна, когда та же модель приедет снова — по ней считают, за сколько
продавать следующую.

Порог считается в Python и уезжает параметром: `sold_at` пишется локальным
кадром времени (`now_str()`), а `NOW()`/`datetime('now')` в SQL дают UTC —
лексикографическое сравнение строк в разных кадрах молча всегда ложно
(CLAUDE.md), и крон тихо не архивировал бы ничего.

Использование:
    python -m tasks.run_machines_archive

Расписание Railway Cron (пример): 30 3 * * *  (после ночного maintenance).
"""

import asyncio
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("machines_archive")

from services.database import get_setting, init_db  # noqa: E402
from services.machines import archive_sold_machines  # noqa: E402


def main() -> int:
    init_db()
    try:
        days = int(get_setting("machines_archive_days", 90))
    except (TypeError, ValueError):
        # Настройку правит человек; кривое значение не повод молча не
        # архивировать вовсе — берём дефолт и говорим об этом в логе.
        logger.warning("machines_archive_days не число — беру 90")
        days = 90
    try:
        archived = asyncio.run(archive_sold_machines(days=days))
        logger.info("machines_archive: в архив ушло %d машин (порог %d дней)", archived, days)
        return 0
    except Exception:
        logger.exception("machines_archive: ошибка")
        return 1


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("machines_archive", main))
