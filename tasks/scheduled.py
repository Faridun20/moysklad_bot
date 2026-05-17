"""
Фоновые задачи: автоматические отчёты по расписанию
- Ежедневный отчёт
- Еженедельный отчёт
- Ежемесячный отчёт
"""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot

from services.moysklad import get_sales_stats
from services.notifier import get_notify_recipients, send_to_recipients
from utils.formatters import format_sales_report, format_stock_report
from handlers.reports import get_stock_report_data

logger = logging.getLogger(__name__)

MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def seconds_until(hour: int, minute: int = 0, weekday: int = None) -> float:
    """Секунды до следующего наступления указанного времени UTC."""
    now = datetime.utcnow()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is not None:
        days_ahead = (weekday - now.weekday()) % 7
        if days_ahead == 0 and now >= target:
            days_ahead = 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
    else:
        if now >= target:
            target += timedelta(days=1)
    return max((target - now).total_seconds(), 1)


async def build_sales_and_stock_report(
    label: str,
    since: datetime, until: datetime,
    prev_since: datetime, prev_until: datetime,
) -> str:
    """Собрать полный отчёт: продажи + склад."""
    try:
        current_stats, prev_stats, stock_data = await asyncio.gather(
            get_sales_stats(since, until),
            get_sales_stats(prev_since, prev_until),
            get_stock_report_data(),
        )
        sales_text = format_sales_report(label, current_stats, prev_stats)
        stock_text = format_stock_report(stock_data)
        return sales_text + "\n\n" + stock_text
    except Exception as e:
        logger.error("Ошибка сборки отчёта: %s", e)
        return f"❌ Ошибка при формировании отчёта:\n<code>{e}</code>"


async def send_report(bot: Bot, text: str):
    """Разослать отчёт всем admin и boss."""
    recipients = get_notify_recipients()
    await send_to_recipients(bot, text, recipients)


# ─── Задачи ───────────────────────────────────────────────────────────────────


async def daily_report_task(bot: Bot):
    """Каждый день в 09:00 UTC (12:00 Ташкент)."""
    logger.info("Ежедневный отчёт запущен")
    while True:
        await asyncio.sleep(seconds_until(9, 0))
        try:
            now = datetime.utcnow()
            since = (now - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            until = now.replace(hour=0, minute=0, second=0, microsecond=0)
            prev_since = since - timedelta(days=1)

            text = "📬 <b>Ежедневный отчёт</b>\n\n"
            text += await build_sales_and_stock_report(
                f"{since.strftime('%d.%m.%Y')}",
                since, until, prev_since, since,
            )
            await send_report(bot, text)
            logger.info("Ежедневный отчёт отправлен")
        except Exception as e:
            logger.error("Ошибка ежедневного отчёта: %s", e)


async def weekly_report_task(bot: Bot):
    """Каждый понедельник в 09:00 UTC."""
    logger.info("Еженедельный отчёт запущен")
    while True:
        await asyncio.sleep(seconds_until(9, 0, weekday=0))
        try:
            now = datetime.utcnow()
            since = now - timedelta(days=7)
            prev_since = since - timedelta(days=7)

            text = "📬 <b>Еженедельный отчёт</b>\n\n"
            text += await build_sales_and_stock_report(
                "прошедшая неделя",
                since, now, prev_since, since,
            )
            await send_report(bot, text)
            logger.info("Еженедельный отчёт отправлен")
        except Exception as e:
            logger.error("Ошибка еженедельного отчёта: %s", e)


async def monthly_report_task(bot: Bot):
    """1-го числа каждого месяца в 09:00 UTC."""
    logger.info("Ежемесячный отчёт запущен")
    while True:
        now = datetime.utcnow()
        if now.month == 12:
            next_first = now.replace(
                year=now.year + 1, month=1, day=1,
                hour=9, minute=0, second=0, microsecond=0,
            )
        else:
            next_first = now.replace(
                month=now.month + 1, day=1,
                hour=9, minute=0, second=0, microsecond=0,
            )
        wait = max((next_first - now).total_seconds(), 1)
        logger.info("Ежемесячный отчёт через %.0f ч", wait / 3600)
        await asyncio.sleep(wait)

        try:
            now = datetime.utcnow()
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
                label, first_prev, first_this, first_prev_prev, first_prev,
            )
            await send_report(bot, text)
            logger.info("Ежемесячный отчёт отправлен")
        except Exception as e:
            logger.error("Ошибка ежемесячного отчёта: %s", e)
