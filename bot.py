"""
Точка запуска бота — только инициализация и старт
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TELEGRAM_TOKEN
from handlers import common, stock, shipments, analytics, payments
from services.notifier import shipment_notifier
from services.database import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    # Инициализируем базу данных
    init_db()

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Подключаем все роутеры
    dp.include_router(common.router)
    dp.include_router(stock.router)
    dp.include_router(shipments.router)
    dp.include_router(analytics.router)
    dp.include_router(payments.router)

    # Запускаем фоновый мониторинг отгрузок
    asyncio.create_task(shipment_notifier(bot))

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
