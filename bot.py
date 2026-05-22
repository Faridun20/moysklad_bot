"""
Telegram-бот МойСклад — точка запуска
"""

import asyncio
import logging
import os
from typing import Any
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    MenuButtonWebApp,
    MenuButtonDefault,
    WebAppInfo,
    Message,
    CallbackQuery,
    TelegramObject,
    User,
)

from config import (
    TELEGRAM_TOKEN,
    TG_USE_WEBHOOK,
    TG_WEBHOOK_SECRET,
    WEBAPP_URL,
    REDIS_URL,
    BOT_MODE,
    ENABLE_SCHEDULED_REPORTS,
)
from services.rate_limit import acquire as rate_limit_acquire

# Сервисы и задачи
from services.database import init_db
from services.moysklad import get_session, close_session
from services.notifier import shipment_notifier, close_tg_session
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
                        await event.answer("⏳ Слишком много сообщений — подождите минуту.")
                    except Exception:
                        pass
                return
        return await handler(event, data)


def register_routers(dp: Dispatcher):
    """Подключить все роутеры."""
    from handlers import (
        start,
        users,
        stock,
        shipments,
        analytics,
        payments,
        reports,
        audit,
        log,
        orders,
        debts,
        deposits,
        returns,
        credit,
        order_cancel,
    )

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
        debts.router,
        deposits.router,
        returns.router,
        credit.router,
        order_cancel.router,
    ]
    for r in routers:
        dp.include_router(r)


def build_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    """Создать готовую пару Bot+Dispatcher с middleware и роутерами.

    Выделено, чтобы режим BOT_MODE=webapp мог поднять dispatcher для
    приёма Telegram-webhook'ов в FastAPI без запуска polling-цикла.
    """
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=_build_fsm_storage())
    rate_mw = RateLimitMiddleware()
    dp.message.middleware(rate_mw)
    dp.callback_query.middleware(rate_mw)
    register_routers(dp)
    return bot, dp


def start_background_tasks(bot: Bot) -> list[asyncio.Task]:
    """Запустить фоновые задачи. Возвращает список созданных Task'ов.

    Если ENABLE_SCHEDULED_REPORTS=0 — отчётные циклы не запускаются
    (предполагается, что их дёргает Railway Cron через
    tasks.run_report). Snapshot и notifier остаются всегда — они
    нужны 24/7 и под cron не подходят (snapshot реагирует на webhook'и
    в реальном времени, notifier проверяет новые отгрузки каждые
    CHECK_INTERVAL_SEC секунд).
    """
    coros = [
        shipment_notifier(bot),
        snapshot_refresh_task(bot),
        snapshot._stock_debounce_loop(),
    ]
    if ENABLE_SCHEDULED_REPORTS:
        # SECURITY.md Medium: предупреждаем при подозрении на дубли.
        # Если в проекте есть Cron-сервисы (`cron-daily`/etc) — ENABLE
        # должен быть 0, иначе отчёты улетают дважды. Точного API
        # «есть ли cron-сервис» нет, проверяем мягкий heuristic:
        # наличие RAILWAY-окружения + дефолтное значение env.
        import os as _os

        if (
            _os.environ.get("RAILWAY_ENVIRONMENT")
            and _os.environ.get("ENABLE_SCHEDULED_REPORTS") is None
        ):
            logger.warning(
                "ENABLE_SCHEDULED_REPORTS не задан явно в Railway. Если "
                "у тебя есть отдельные cron-сервисы (cron-daily/weekly/"
                "monthly) — поставь ENABLE_SCHEDULED_REPORTS=0 чтобы "
                "отчёты не дублировались."
            )
        coros.extend(
            [
                daily_report_task(bot),
                weekly_report_task(bot),
                monthly_report_task(bot),
            ]
        )
    else:
        logger.info(
            "ENABLE_SCHEDULED_REPORTS=0 — отчёты внутри бота отключены, "
            "ожидается запуск через Railway Cron Jobs."
        )
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
    await close_tg_session()
    # В BOT_MODE=all webapp поднят в этом же процессе и мог создать
    # собственный notify-bot для approve/reject API. Закрываем его.
    if BOT_MODE == "all":
        try:
            from webapp import server as webapp_server

            await webapp_server.close_notify_bot()
        except Exception:
            pass


def _build_fsm_storage():
    """RedisStorage если задан REDIS_URL, иначе MemoryStorage.

    Зачем: с MemoryStorage черновики заказов и любые FSM-состояния
    теряются на каждом редеплое Railway. С Redis они переживают
    рестарт и могут жить между несколькими bot-инстансами.
    """
    if not REDIS_URL:
        logger.warning(
            "REDIS_URL не задан — FSM использует MemoryStorage. "
            "Состояния заказов (черновики, шаг добавления товара) "
            "теряются при каждом рестарте бота."
        )
        return MemoryStorage()
    try:
        from aiogram.fsm.storage.redis import RedisStorage

        storage = RedisStorage.from_url(REDIS_URL)
        logger.info("FSM storage: Redis (%s)", REDIS_URL.split("@")[-1])
        return storage
    except Exception as e:
        # Не валим бот из-за проблем с Redis — фолбэк на память.
        logger.warning(
            "Redis недоступен (%s) — FSM фолбэк на MemoryStorage",
            e,
        )
        return MemoryStorage()


async def _startup_selfcheck():
    """Fail-fast диагностика на старте: режим, критичные env и что URL для
    уведомлений реально собирается.

    Прямой урок инцидента: notifier.tg_send_message строил невалидный
    aiohttp base_url (с path), и ВСЕ уведомления молча падали в рантайме.
    Здесь это видно сразу в логах старта, а не «когда полезли в логи».
    Не роняем процесс — только громкий error, чтобы было заметно.
    """
    logger.info("Старт в режиме BOT_MODE=%s", BOT_MODE or "all")
    if not TELEGRAM_TOKEN or ":" not in TELEGRAM_TOKEN:
        logger.error(
            "TELEGRAM_TOKEN пуст или не вида '<id>:<secret>' — бот и уведомления работать не будут",
        )
    try:
        import services.notifier as notifier

        sess = await notifier.get_tg_session()
        base = getattr(sess, "_base_url", None)
        if base is not None and base.path not in ("", "/"):
            logger.error(
                "notify self-check: base_url содержит path %r — "
                "уведомления будут падать ещё до сети",
                base.path,
            )
        else:
            logger.info("notify self-check: Telegram base_url корректен ✓")
    except Exception:
        logger.exception(
            "notify self-check провален — уведомления, вероятно, не будут отправляться",
        )


async def _run_webapp_only():
    """Режим BOT_MODE=webapp: поднимаем только FastAPI.

    Если TG_USE_WEBHOOK=1 — создаём пару Bot+Dispatcher с роутерами и
    регистрируем у webapp, чтобы webhook-апдейты от Telegram обрабатывались
    прямо здесь. Polling в этом режиме никогда не запускается — бот
    либо принимает webhook'и, либо вообще не обрабатывает Telegram
    (полезно, если парный BOT_MODE=bot процесс делает polling)."""
    await _startup_selfcheck()
    init_db()
    from webapp import server as webapp_server

    bot = None
    if TG_USE_WEBHOOK and TG_WEBHOOK_SECRET and WEBAPP_URL:
        bot, dp = build_bot_and_dispatcher()
        webapp_server.set_telegram_dispatcher(bot, dp)
        # Прогрев МС-сессии (handlers будут её использовать)
        await get_session()
        webhook_url = f"{WEBAPP_URL}/tg/{TG_WEBHOOK_SECRET}"
        try:
            await bot.set_webhook(
                url=webhook_url,
                secret_token=TG_WEBHOOK_SECRET,
                drop_pending_updates=False,
                allowed_updates=dp.resolve_used_update_types(),
            )
            logger.info("BOT_MODE=webapp + webhook: установлен на %s", webhook_url)
        except Exception:
            logger.exception("set_webhook failed; webhook'и приходить не будут")
    else:
        logger.info(
            "BOT_MODE=webapp: Telegram-апдейты не обрабатываются "
            "(включите TG_USE_WEBHOOK=1 или используйте парный сервис с BOT_MODE=bot)"
        )

    # Прогрев МС-сессии — нужна для approve-флоу (создание customerorder/
    # demand) при одобрении заявок через /api/requests/approve.
    await get_session()

    # КРИТИЧНО: webapp-процесс тоже одобряет заявки (через WebApp), а значит
    # создаёт документы в МойСклад. Без demand-контекста (org/store/attrs)
    # ms_ready()=False → PDF и demand не создаются. main() инициализирует
    # его для bot-процесса; здесь делаем то же для webapp-процесса.
    async def _init_demand():
        try:
            result = await init_demand_context()
            logger.info("ms_demand.init_demand_context: %s", result)
        except Exception:
            logger.exception("init_demand_context failed")

    asyncio.create_task(_init_demand(), name="init_demand")

    try:
        await webapp_server.start_webapp()
    finally:
        if bot is not None:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
        await close_session()
        await close_tg_session()
        await webapp_server.close_notify_bot()


async def main():
    # Режим webapp полностью самодостаточен — не нужны ни Bot, ни роутеры
    if BOT_MODE == "webapp":
        await _run_webapp_only()
        return

    await _startup_selfcheck()
    init_db()

    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=_build_fsm_storage())

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
                    stats.get("ms_products", 0),
                    stats.get("ms_stock", 0),
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
    # Это ГЛОБАЛЬНЫЙ default menu button. Per-user кэш он НЕ перетирает —
    # для этого в handlers/start.py в cmd_start идёт явный per-chat вызов
    # на каждый /start, чтобы старые URL у юзеров принудительно обновились.
    webapp_url = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
    logger.info("Текущий WEBAPP_URL: %r", webapp_url or "(не задан)")
    if webapp_url:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Открыть",
                    web_app=WebAppInfo(url=webapp_url),
                )
            )
            logger.info("Global Menu Button установлен на %s", webapp_url)
        except Exception as e:
            logger.warning("Не удалось установить Menu Button: %s", e)
    else:
        # На случай если WEBAPP_URL убрали — возвращаем дефолтную кнопку
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        except Exception:
            pass

    bg_tasks = start_background_tasks(bot)

    # WebApp поднимаем только если этот процесс отвечает за HTTP-слой.
    # В режиме BOT_MODE=bot предполагается, что FastAPI крутится в
    # парном сервисе с BOT_MODE=webapp — задваивать порт не нужно.
    webapp_server = None
    if BOT_MODE == "all":
        from webapp import server as webapp_server

        bg_tasks.append(asyncio.create_task(webapp_server.start_webapp(), name="webapp"))

    # ─── Режим приёма апдейтов ───────────────────────────────────
    # Webhook: webapp принимает POST'ы от Telegram, мы не дёргаем
    # api.telegram.org каждую секунду. Polling: классическая
    # модель, работает без публичного URL.
    use_webhook = (
        TG_USE_WEBHOOK
        and TG_WEBHOOK_SECRET
        and WEBAPP_URL
        and webapp_server is not None  # webhook нуждается в локальном FastAPI
    )
    if TG_USE_WEBHOOK and not use_webhook:
        if webapp_server is None:
            logger.warning(
                "TG_USE_WEBHOOK=1, но BOT_MODE=bot — webhook требует FastAPI в "
                "этом же процессе. Фолбэк на polling. Для webhook используйте "
                "BOT_MODE=all (один процесс) или BOT_MODE=webapp (парный сервис)."
            )
        else:
            logger.warning(
                "TG_USE_WEBHOOK=1, но не задан WEBAPP_URL или TG_WEBHOOK_SECRET — "
                "фолбэк на polling. Задайте обе переменные, чтобы включить webhook."
            )

    logger.info(
        "Бот запущен в режиме: %s",
        "webhook" if use_webhook else "polling",
    )
    try:
        if use_webhook:
            # Регистрируем dp/bot у webapp, чтобы /tg/<secret> мог отдавать
            # апдейты в dispatcher.
            webapp_server.set_telegram_dispatcher(bot, dp)
            webhook_url = f"{WEBAPP_URL}/tg/{TG_WEBHOOK_SECRET}"
            # secret_token шлётся Telegram'ом в заголовке X-Telegram-Bot-
            # Api-Secret-Token; webapp проверяет соответствие.
            await bot.set_webhook(
                url=webhook_url,
                secret_token=TG_WEBHOOK_SECRET,
                drop_pending_updates=False,
                allowed_updates=dp.resolve_used_update_types(),
            )
            logger.info("Telegram webhook установлен: %s", webhook_url)
            # Просто блокируемся, пока приходят сигналы — реальная обработка
            # апдейтов идёт через FastAPI endpoint /tg/<secret>.
            stop_event = asyncio.Event()
            await stop_event.wait()
        else:
            # Принудительно снимаем webhook перед polling. Если на
            # Telegram-стороне остался webhook от предыдущего запуска
            # (например, был включён TG_USE_WEBHOOK, потом переключили
            # на polling) — getUpdates даёт TelegramConflictError, и
            # бот зависает в бесконечном retry-loop. delete_webhook
            # идемпотентно и безопасно: на «чистом» боте ничего не
            # ломает.
            try:
                await bot.delete_webhook(drop_pending_updates=False)
                logger.info("Перед polling сняли webhook (если был установлен)")
            except Exception as e:
                logger.warning("delete_webhook перед polling упал: %s", e)
            await dp.start_polling(bot)
    finally:
        if use_webhook:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                logger.exception("Не удалось снять webhook")
        await _shutdown(bg_tasks)


if __name__ == "__main__":
    asyncio.run(main())
