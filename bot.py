"""
Точка запуска бота
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TELEGRAM_TOKEN
from handlers import common, stock, shipments, analytics, payments, audit
from handlers import reports
from services.notifier import shipment_notifier
from services.database import init_db
from handlers.reports import daily_report_task, weekly_report_task, monthly_report_task

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    init_db()

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(common.router)
    dp.include_router(stock.router)
    dp.include_router(shipments.router)
    dp.include_router(analytics.router)
    dp.include_router(payments.router)
    dp.include_router(audit.router)
    dp.include_router(reports.router)

    # Фоновые задачи
    asyncio.create_task(shipment_notifier(bot))
    asyncio.create_task(daily_report_task(bot))
    asyncio.create_task(weekly_report_task(bot))
    asyncio.create_task(monthly_report_task(bot))

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
