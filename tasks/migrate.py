"""
CLI: миграции БД для МойСклад-бота.

Запускается ОДНИМ процессом ПЕРЕД стартом сервисов (bot, webapp, cron).
Раньше всё это (DDL + ALTER + backfill + recovery) гонялось внутри
init_db на каждом старте каждого процесса. При rolling deploy
нескольких контейнеров получалась гонка: ALTER одного процесса
конфликтовал с UPDATE другого → битые транзакции, потерянные данные
в crash-сценариях (см. SECURITY.md H4).

Использование:
    python -m tasks.migrate

Что делает:
    1. _create_tables() — CREATE TABLE IF NOT EXISTS (на свежей БД
       сразу создаёт схему с полным набором колонок).
    2. run_migrations() — ALTER TABLE ADD COLUMN для старых БД.
       Идемпотентно: «колонка уже есть» — норм, пропускаем.
    3. _create_indexes() — индексы (включая UNIQUE для paymentin).
    4. run_backfills() — одноразовые data-миграции (paid_confirmed_at
       backfill + recovery от старого backfill-бага).

В Railway: добавь pre-deploy команду на сервисах bot/webapp:
    python -m tasks.migrate && python bot.py
Или вынеси в отдельный one-shot Cron Job, который запускается
вручную при необходимости («Run Now»).

Локально (свежая БД): не обязательно — init_db в bot.py покрывает
CREATE TABLE, а свежая схема уже содержит все колонки. Но прогнать
один раз для верности — лишним не будет.
"""

import logging
import sys

from services.database import (
    _create_tables,
    _create_indexes,
    run_migrations,
    run_backfills,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("migrate")


def main() -> int:
    logger.info("Старт миграции БД")

    try:
        _create_tables()
        logger.info("✓ CREATE TABLE завершён")

        run_migrations()
        logger.info("✓ ALTER TABLE миграции завершены")

        _create_indexes()
        logger.info("✓ Индексы созданы")

        run_backfills()
        logger.info("✓ Backfill/recovery завершены")

        logger.info("Миграция успешно завершена")
        return 0
    except Exception:
        logger.exception("Миграция упала — сервисы НЕ запускайте до фикса")
        return 1


if __name__ == "__main__":
    sys.exit(main())
