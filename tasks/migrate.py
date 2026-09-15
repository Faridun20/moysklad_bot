"""
CLI: инициализация схемы БД для МойСклад-бота.

Запускается ОДНИМ процессом ПЕРЕД стартом сервисов (bot, webapp, cron).
Инкрементальных миграций в проекте нет: каждая таблица объявлена
в `_create_tables` один раз, сразу с финальным набором колонок, типов
и ограничений. Нужна новая колонка — правится определение таблицы.

Использование:
    python -m tasks.migrate
    python -m tasks.migrate --rerun-backfill local_identifiers   # осознанный повтор

Что делает:
    1. _create_tables() — CREATE TABLE IF NOT EXISTS (полная схема за проход).
    2. _create_indexes() — индексы (включая UNIQUE для paymentin).
    3. run_backfills() — сидинг app_settings/складов + одноразовые data-миграции.
       Разовые выполняются ОДИН раз на базу (отметка backfill_done:<имя> в
       app_settings): этот скрипт гоняется на каждом `docker compose up`, и
       мутация живых денег при каждом деплое недопустима. Повтор — только
       явным `--rerun-backfill <имя>[,<имя>]` (или `all`).

В Railway: добавь pre-deploy команду на сервисах bot/webapp:
    python -m tasks.migrate && python bot.py
Или вынеси в отдельный one-shot Cron Job, который запускается
вручную при необходимости («Run Now»).

Локально (свежая БД): не обязательно — init_db в bot.py делает то же
самое, кроме backfill'ов.
"""

import argparse
import logging
import sys

from services.database import (
    ONE_TIME_BACKFILLS,
    _create_tables,
    _create_indexes,
    run_backfills,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("migrate")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Схема БД + сидинг + разовые backfill'ы")
    parser.add_argument(
        "--rerun-backfill",
        default="",
        help="Повторить разовые backfill'ы через запятую (или all), даже если уже выполнены",
    )
    args = parser.parse_args(argv)
    rerun = tuple(x.strip() for x in args.rerun_backfill.split(",") if x.strip())
    known = {name for name, _ in ONE_TIME_BACKFILLS} | {"all"}
    unknown = sorted(set(rerun) - known)
    if unknown:
        # Опечатка в имени не должна молча превращаться в «ничего не повторили».
        logger.error("Неизвестные backfill'ы: %s (есть: %s)", unknown, sorted(known))
        return 2

    logger.info("Старт инициализации схемы БД")

    try:
        _create_tables()
        logger.info("✓ CREATE TABLE завершён")

        _create_indexes()
        logger.info("✓ Индексы созданы")

        report = run_backfills(rerun=rerun)
        logger.info("✓ Backfill/сидинг завершены: %s", report)

        logger.info("Схема успешно инициализирована")
        return 0
    except Exception:
        logger.exception("Инициализация упала — сервисы НЕ запускайте до фикса")
        return 1


if __name__ == "__main__":
    sys.exit(main())
