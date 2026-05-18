"""
Telegram-бот МойСклад — точка запуска
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, MenuButtonDefault, WebAppInfo

from handlers import orders
from config import TELEGRAM_TOKEN

# Хэндлеры
from handlers import (
    start, users, stock, shipments,
    analytics, payments, reports, audit, log,
)

# Сервисы и задачи
from services.database import init_db
from services.moysklad import get_session, close_session
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
        orders.router,
    ]
    for r in routers:
        dp.include_router(r)


def start_background_tasks(bot: Bot) -> list[asyncio.Task]:
    """Запустить фоновые задачи. Возвращает список созданных Task'ов."""
    coros = [
        shipment_notifier(bot),
        daily_report_task(bot),
        weekly_report_task(bot),
        monthly_report_task(bot),
    ]
    return [asyncio.create_task(c, name=c.__qualname__) for c in coros]


async def _shutdown(tasks: list[asyncio.Task]) -> None:
    """Аккуратно отменить фоновые задачи и дождаться их завершения."""
    for t in tasks:
        if not t.done():
            t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await close_session()


async def main():
    init_db()

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    register_routers(dp)

    # Предварительно прогреваем общую aiohttp-сессию для МойСклад
    await get_session()

    # Закрепляем кнопку «Открыть» в композере чата, если задан WEBAPP_URL.
    # Это делает WebApp доступным в один тап рядом с полем ввода.
    webapp_url = os.environ.get("WEBAPP_URL", "").strip()
    if webapp_url:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Открыть",
                    web_app=WebAppInfo(url=webapp_url),
                )
            )
            logger.info("Menu Button установлен на %s", webapp_url)
        except Exception as e:
            logger.warning("Не удалось установить Menu Button: %s", e)
    else:
        # На случай если WEBAPP_URL убрали — возвращаем дефолтную кнопку
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        except Exception:
            pass

    bg_tasks = start_background_tasks(bot)

    # Запускаем WebApp параллельно с ботом
    from webapp.server import start_webapp
    bg_tasks.append(asyncio.create_task(start_webapp(), name="webapp"))

    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        await _shutdown(bg_tasks)


if __name__ == "__main__":
    asyncio.run(main())