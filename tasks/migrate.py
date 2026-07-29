"""
CLI: инициализация схемы БД для МойСклад-бота.

Запускается ОДНИМ процессом ПЕРЕД стартом сервисов (bot, webapp, cron).
Инкрементальных миграций в проекте нет: каждая таблица объявлена
в `_create_tables` один раз, сразу с финальным набором колонок, типов
и ограничений. Нужна новая колонка — правится определение таблицы.

Использование:
    python -m tasks.migrate

Что делает:
    1. _create_tables() — CREATE TABLE IF NOT EXISTS (полная схема за проход).
    2. _create_indexes() — индексы (включая UNIQUE для paymentin).
    3. run_backfills() — сидинг app_settings + одноразовые data-миграции.

В Railway: добавь pre-deploy команду на сервисах bot/webapp:
    python -m tasks.migrate && python bot.py
Или вынеси в отдельный one-shot Cron Job, который запускается
вручную при необходимости («Run Now»).

Локально (свежая БД): не обязательно — init_db в bot.py делает то же
самое, кроме backfill'ов.
"""

import logging
import sys

from services.database import (
    _create_tables,
    _create_indexes,
    run_backfills,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("migrate")


def main() -> int:
    logger.info("Старт инициализации схемы БД")

    try:
        _create_tables()
        logger.info("✓ CREATE TABLE завершён")

        _create_indexes()
        logger.info("✓ Индексы созданы")

        run_backfills()
        logger.info("✓ Backfill/сидинг завершены")

        logger.info("Схема успешно инициализирована")
        return 0
    except Exception:
        logger.exception("Инициализация упала — сервисы НЕ запускайте до фикса")
        return 1


if __name__ == "__main__":
    sys.exit(main())
