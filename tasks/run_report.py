"""
CLI-entrypoint для отчётов: запускается из Railway Cron Jobs.

Использование:
    python -m tasks.run_report daily
    python -m tasks.run_report weekly
    python -m tasks.run_report monthly

Каждый запуск — это отдельный короткоживущий процесс, который:
  1. Считает данные за нужный период из МойСклад
  2. Отправляет HTML-отчёт всем boss/admin через Telegram Bot API
  3. Выходит с кодом 0 (или 1 при фатальной ошибке)

В отличие от tasks/scheduled.py, тут нет бесконечного цикла со sleep —
расписание задаёт Railway Cron, не Python. Это значит:
  - Отчёт НЕ потеряется при рестарте основного бот-процесса
  - Контейнер не висит между запусками (не платим за idle)
  - Можно вручную перезапустить отчёт за день/неделю/месяц,
    просто нажав "Run Now" на cron-сервисе в Railway dashboard
"""

import argparse
import asyncio
import logging
import sys
from datetime import timedelta

from aiogram import Bot

from config import TELEGRAM_TOKEN
from services.database import init_db
from services.moysklad import close_session
from services.notifier import close_tg_session
from utils.helpers import utc_now
from tasks.scheduled import (
    MONTH_NAMES,
    build_sales_and_stock_report,
    send_report,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("run_report")


async def _daily(bot: Bot) -> None:
    """Отчёт за вчерашний полный день."""
    now = utc_now()
    since = (now - timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    until = now.replace(hour=0, minute=0, second=0, microsecond=0)
    prev_since = since - timedelta(days=1)

    text = "📬 <b>Ежедневный отчёт</b>\n\n"
    text += await build_sales_and_stock_report(
        f"{since.strftime('%d.%m.%Y')}",
        since,
        until,
        prev_since,
        since,
    )
    await send_report(bot, text)


async def _weekly(bot: Bot) -> None:
    """Отчёт за прошедшие 7 дней."""
    now = utc_now()
    since = now - timedelta(days=7)
    prev_since = since - timedelta(days=7)

    text = "📬 <b>Еженедельный отчёт</b>\n\n"
    text += await build_sales_and_stock_report(
        "прошедшая неделя",
        since,
        now,
        prev_since,
        since,
    )
    await send_report(bot, text)


async def _monthly(bot: Bot) -> None:
    """Отчёт за прошедший календарный месяц."""
    now = utc_now()
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first_this.month == 1:
        first_prev = first_this.replace(year=first_this.year - 1, month=12)
    else:
        first_prev = first_this.replace(month=first_this.month - 1)

    if first_prev.month == 1:
        first_prev_prev = first_prev.replace(year=first_prev.year - 1, month=12)
    else:
        first_prev_prev = first_prev.replace(month=first_prev.month - 1)

    label = f"{MONTH_NAMES[first_prev.month]} {first_prev.year}"
    text = f"📬 <b>Ежемесячный отчёт · {label}</b>\n\n"
    text += await build_sales_and_stock_report(
        label,
        first_prev,
        first_this,
        first_prev_prev,
        first_prev,
    )
    await send_report(bot, text)


_RUNNERS = {
    "daily": _daily,
    "weekly": _weekly,
    "monthly": _monthly,
}


async def main(report_type: str) -> int:
    runner = _RUNNERS.get(report_type)
    if runner is None:
        logger.error("unknown report type: %s (use: daily|weekly|monthly)", report_type)
        return 2

    # init_db гарантирует, что схема существует и подключение к Postgres
    # установлено. Дублирует то, что делает основной бот при старте —
    # на случай если cron запустится РАНЬШЕ первого старта bot-сервиса.
    init_db()

    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        logger.info("Старт %s-отчёта", report_type)
        await runner(bot)
        logger.info("%s-отчёт отправлен", report_type)
        return 0
    except Exception:
        logger.exception("Ошибка %s-отчёта", report_type)
        return 1
    finally:
        # Корректно закрываем все aiohttp-сессии и aiogram-сессию.
        # Без этого процесс «висит» 30+ секунд в ожидании сборки мусора.
        try:
            await bot.session.close()
        except Exception:
            pass
        await close_session()
        await close_tg_session()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Отправить отчёт из Railway Cron Job",
    )
    parser.add_argument(
        "report_type",
        choices=sorted(_RUNNERS.keys()),
        help="Какой отчёт собрать и отправить",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    sys.exit(asyncio.run(main(args.report_type)))
