"""
Точка запуска бота — только инициализация и старт
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import TELEGRAM_TOKEN
from handlers import common, stock, shipments, analytics
from services.notifier import shipment_notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    logger.info("TELEGRAM_TOKEN = '%s'", TELEGRAM_TOKEN[:10] if TELEGRAM_TOKEN else "ПУСТОЙ")
    bot = Bot(token=TELEGRAM_TOKEN)
    
    # Подключаем все роутеры
    dp.include_router(common.router)
    dp.include_router(stock.router)
    dp.include_router(shipments.router)
    dp.include_router(analytics.router)

    # Запускаем фоновый мониторинг отгрузок
    asyncio.create_task(shipment_notifier(bot))

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
