"""
Telegram-бот МойСклад — точка запуска
"""

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    MenuButtonWebApp, MenuButtonDefault, WebAppInfo,
    Message, CallbackQuery, TelegramObject, User,
)

from handlers import orders
from config import TELEGRAM_TOKEN
from services.rate_limit import acquire as rate_limit_acquire

# Хэндлеры
from handlers import (
    start, users, stock, shipments,
    analytics, payments, reports, audit, log,
)

# Сервисы и задачи
from services.database import init_db
from services.moysklad import get_session, close_session
from services.notifier import shipment_notifier
from services import snapshot
from services.ms_webhooks import ensure_subscriptions
from services.ms_demand import init_demand_context
from tasks.scheduled import (
    daily_report_task,
    weekly_report_task,
    monthly_report_task,
    snapshot_refresh_task,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseMiddleware):
    """
    Бросает «вы шлёте слишком быстро» вместо обработки, если юзер
    превысил лимит. По умолчанию 30 действий в минуту — комфортно для
    нормального использования, душит спам кнопками и сообщениями.

    Сообщения и callback'и считаются отдельно (разные скоупы), чтобы
    тыкание кнопок «Назад/Меню» не блокировало печать сообщения.
    """

    def __init__(self, max_calls: int = 30, window_sec: float = 60.0):
        self.max_calls = max_calls
        self.window_sec = window_sec

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None:
            scope = "bot_msg" if isinstance(event, Message) else "bot_cb"
            if not rate_limit_acquire(scope, user.id, self.max_calls, self.window_sec):
                # Тихо отвечаем юзеру и не пропускаем дальше
                if isinstance(event, CallbackQuery):
                    await event.answer(
                        "⏳ Слишком много действий — подождите минуту",
                        show_alert=True,
                    )
                elif isinstance(event, Message):
                    try:
                        await event.answer(
                            "⏳ Слишком много сообщений — подождите минуту."
                        )
                    except Exception:
                        pass
                return
        return await handler(event, data)


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
        snapshot_refresh_task(bot),
        snapshot._stock_debounce_loop(),
    ]
    return [
        asyncio.create_task(
            c, name=getattr(c, "__qualname__", None) or getattr(c, "__name__", "task")
        )
        for c in coros
    ]


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

    # Защита от спама. Применяется глобально ко всем сообщениям и
    # callback'ам перед роутингом. ADMIN_IDS не освобождаются от лимита
    # сознательно — лимит «30 действий в минуту» комфортен и для них.
    rate_mw = RateLimitMiddleware()
    dp.message.middleware(rate_mw)
    dp.callback_query.middleware(rate_mw)

    register_routers(dp)

    # Предварительно прогреваем общую aiohttp-сессию для МойСклад
    await get_session()

    # Первичный snapshot МойСклад. Делаем fire-and-forget, чтобы не
    # задерживать старт бота — пока заливается snapshot, hot-path функции
    # автоматически работают через live API fallback.
    async def _initial_snapshot():
        try:
            stats = snapshot.stats()
            if stats.get("ms_stock", 0) == 0:
                logger.info("snapshot пуст — делаю первичный refresh_all")
                await snapshot.refresh_all()
            else:
                logger.info(
                    "snapshot уже инициализирован: products=%d, stock=%d",
                    stats.get("ms_products", 0), stats.get("ms_stock", 0),
                )
        except Exception:
            logger.exception("initial snapshot failed")

    asyncio.create_task(_initial_snapshot(), name="initial_snapshot")

    # Регистрируем webhook-подписки в МойСклад (идемпотентно).
    # При смене WEBAPP_URL или ротации MS_WEBHOOK_SECRET — старые подписки
    # удаляются, новые ставятся.
    async def _register_webhooks():
        try:
            result = await ensure_subscriptions()
            logger.info("ms_webhooks.ensure_subscriptions: %s", result)
        except Exception:
            logger.exception("ensure_subscriptions failed")

    asyncio.create_task(_register_webhooks(), name="register_webhooks")

    # Готовим контекст для push-а отгрузок в МойСклад (org/store/attribute).
    # Без него cb_approve_request не сможет создавать demand-документы.
    async def _init_demand():
        try:
            result = await init_demand_context()
            logger.info("ms_demand.init_demand_context: %s", result)
        except Exception:
            logger.exception("init_demand_context failed")

    asyncio.create_task(_init_demand(), name="init_demand")

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