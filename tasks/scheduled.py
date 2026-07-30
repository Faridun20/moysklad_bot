"""
Фоновая задача поддержания свежести локального snapshot МойСклад.

Отчётные циклы (продажи/склад) убраны: отчёты и аналитику смотрят в WebApp
(вкладка «Аналитика»), а бот шлёт лишь дневной пинг (`tasks/run_ops_monitor`).
Здесь остаётся только snapshot_refresh_task — она нужна 24/7 и под cron не
подходит (реагирует на webhook'и в реальном времени).
"""

import asyncio
import logging

from aiogram import Bot

from utils.helpers import utc_now

logger = logging.getLogger(__name__)


async def snapshot_refresh_task(bot: Bot):
    """
    Background-задача поддержания свежести локального snapshot МойСклад.

    - Раз в день в 06:00 UTC — полный рефреш справочников (товары,
      категории, контрагенты, сотрудники).
    - Каждые 2 часа — полный re-sync остатков как safety-net на случай
      потерянных webhook-событий.

    Webhook-события (через webapp /api/ms-webhook) триггерят
    snapshot.mark_stock_dirty() — фактический рефреш делает
    snapshot._stock_debounce_loop, который запускается отдельно из bot.py.
    """
    from services import snapshot

    logger.info("snapshot_refresh_task запущен")
    last_reference_day = None
    last_stock_refresh = 0.0
    STOCK_INTERVAL = 24 * 3600  # сутки: полный срез — страховка, горячий путь — дельта (MS-5)

    while True:
        try:
            now = utc_now()

            # 1) Справочники — раз в день после 06:00 UTC
            today = now.date()
            if now.hour >= 6 and last_reference_day != today:
                logger.info("snapshot: запускаем ежедневный refresh_reference")
                try:
                    counts = await snapshot.refresh_reference()
                    logger.info("snapshot: refresh_reference готов: %s", counts)
                    last_reference_day = today
                except Exception as e:
                    logger.exception("snapshot: refresh_reference failed: %s", e)

            # 2) Остатки — ПОЛНЫЙ срез раз в сутки (MS-5).
            #
            # Горячий путь теперь дельта по вебхукам, а полный
            # `report/stock/all` стоит 5 единиц бюджета за страницу — по
            # документации его берут не чаще раза в сутки. Он остаётся
            # страховкой: подбирает то, что дельта пропустила (новые позиции,
            # окно старше суток, сбой вебхуков).
            mono = asyncio.get_event_loop().time()
            if mono - last_stock_refresh >= STOCK_INTERVAL:
                logger.info("snapshot: суточный полный refresh_stock (страховка)")
                try:
                    await snapshot.refresh_stock()
                    last_stock_refresh = mono
                except Exception as e:
                    logger.exception("snapshot: refresh_stock failed: %s", e)
                    # повторим через минуту
                    last_stock_refresh = mono - STOCK_INTERVAL + 60

            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("snapshot_refresh_task loop error")
            await asyncio.sleep(30)
