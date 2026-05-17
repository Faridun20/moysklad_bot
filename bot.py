"""
Telegram-бот МойСклад — точка запуска
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import TELEGRAM_TOKEN

# Хэндлеры
from handlers import (
    start, users, stock, shipments,
    analytics, payments, reports, audit, log,
)

# Сервисы и задачи
from services.database import init_db
from services.notifier import shipment_notifier
from tasks.scheduled import (
    daily_report_task,
    weekly_report_task,
    monthly_report_task,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def register_routers(dp: Dispatcher):
    """Подключить все роутеры."""
    routers = [
        start.router,
        users.router,
        stock.router,
        shipments.router,
        analytics.router,
        payments.router,
        reports.router,
        audit.router,
        log.router,
    ]
    for r in routers:
        dp.include_router(r)


def start_background_tasks(bot: Bot):
    """Запустить фоновые задачи."""
    tasks = [
        shipment_notifier(bot),
        daily_report_task(bot),
        weekly_report_task(bot),
        monthly_report_task(bot),
    ]
    for task in tasks:
        asyncio.create_task(task)


async def main():
    init_db()

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    register_routers(dp)
    start_background_tasks(bot)

    # Запускаем WebApp параллельно с ботом
    from webapp.server import start_webapp
    asyncio.create_task(start_webapp())

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())