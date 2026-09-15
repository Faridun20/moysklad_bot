"""
CLI: авто-обновление курса валют от ЦБ РУз (CBU).

Запускается из Railway Cron раз в сутки — обновляет «текущий» курс UZS↔USD
(currency_rates) и пишет дневной архив (currency_rate_daily). Босс больше не
обязан вбивать курс руками.

Использование:
    python -m tasks.run_fx_sync                 # обновить курс на сегодня
    python -m tasks.run_fx_sync --backfill-days 90   # + история за N дней
                                                     #   и снимки прошлым заказам

Расписание (рекомендуется):
    Cron Schedule: 0 6 * * *   (раз в сутки; CBU обновляет курс по будням)

Поведение:
  1. Тянет курс USD у CBU (сум за 1 USD) → rate_to_base UZS = 1/курс.
  2. Обновляет «текущий» курс UZS (set_currency_rate) и пишет дневной архив
     UZS+USD (set_currency_rate_daily, source='cbu').
  3. Пишет app_settings.fx_sync_last_run (ISO-время) — для ops-мониторинга.
  4. С --backfill-days N: тянет архив CBU за N прошлых дней и проставляет
     снимки fx_rate_to_base прошлым orders/payments (backfill_fx_rate_snapshots).
  5. rc=0 если курс на сегодня обновлён (даже если часть бэкфилл-дней пуста);
     rc=1 только при необработанном исключении (например, CBU недоступен).
"""

import argparse
import logging
import sys
from datetime import date, timedelta

from services.database import (
    backfill_fx_rate_snapshots,
    get_currency_rate_daily_source,
    ensure_schema,
    set_currency_rate,
    set_currency_rate_daily,
    set_setting,
)
from services.fx_rates import (
    close_session,
    fetch_cbu_usd_per_uzs,
    usd_uzs_to_rate_to_base,
)
from utils.helpers import utc_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("fx_sync")

# updated_by=0 — системный «пользователь» (авто-обновление, не человек).
_SYSTEM_UID = 0


def _store_day(day: date, uzs_rate_to_base: float) -> None:
    """Записать дневной архив за day для UZS и USD (база = 1.0)."""
    day_str = day.strftime("%Y-%m-%d")
    set_currency_rate_daily("UZS", day_str, uzs_rate_to_base, source="cbu")
    set_currency_rate_daily("USD", day_str, 1.0, source="cbu")


async def main(backfill_days: int = 0) -> int:
    ensure_schema()

    # 1. Сегодняшний курс — основной результат прогона.
    usd_per_uzs = await fetch_cbu_usd_per_uzs()
    uzs_rate = usd_uzs_to_rate_to_base(usd_per_uzs)
    today = date.today()
    if get_currency_rate_daily_source("UZS", today.strftime("%Y-%m-%d")) == "manual":
        # Курс на сегодня поправил человек (WebApp → «Курсы валют»). Синк его
        # не трогает ни в текущем курсе, ни в архиве дня: раньше ручная правка
        # молча исчезала при ближайшем прогоне. Завтра синк пишет как обычно.
        logger.warning(
            "fx_sync: курс UZS на %s задан вручную — не перезаписываем "
            "(ЦБ: 1 USD = %.2f сум)", today, usd_per_uzs,
        )
    else:
        ok, err = set_currency_rate("UZS", uzs_rate, updated_by=_SYSTEM_UID)
        if not ok:
            logger.error("set_currency_rate(UZS) отклонён: %s", err)
            return 1
        logger.info(
            "fx_sync: UZS обновлён — 1 USD = %.2f сум (rate_to_base=%.10f)",
            usd_per_uzs, uzs_rate,
        )
    # Архив: ручную запись дня set_currency_rate_daily сама не затирает.
    _store_day(today, uzs_rate)
    set_setting("fx_sync_last_run", utc_now().isoformat(), updated_by=_SYSTEM_UID)

    # 2. Опциональный бэкфилл истории + снимков прошлым строкам.
    if backfill_days and backfill_days > 0:
        filled = 0
        for delta in range(1, backfill_days + 1):
            day = today - timedelta(days=delta)
            try:
                per_uzs = await fetch_cbu_usd_per_uzs(on_date=day)
            except Exception as e:
                # Выходные/праздники/пропуски — CBU может не отдать день. Норм.
                logger.info("backfill %s: пропуск (%s)", day, type(e).__name__)
                continue
            _store_day(day, usd_uzs_to_rate_to_base(per_uzs))
            filled += 1
        logger.info("fx_sync: архив пополнен за %d из %d дней", filled, backfill_days)
        snaps = backfill_fx_rate_snapshots()
        logger.info(
            "fx_sync: снимки проставлены — orders=%d, payments=%d",
            snaps["orders"], snaps["payments"],
        )

    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Авто-обновление курса валют от ЦБ РУз (CBU)")
    p.add_argument(
        "--backfill-days",
        type=int,
        default=0,
        help="Пополнить дневной архив за N прошлых дней и проставить снимки (default 0)",
    )
    return p.parse_args(argv)


async def _main_and_close(backfill_days: int) -> int:
    try:
        return await main(backfill_days)
    finally:
        await close_session()


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    args = _parse_args(sys.argv[1:])
    sys.exit(run_cron("fx_sync", _main_and_close, args.backfill_days))
