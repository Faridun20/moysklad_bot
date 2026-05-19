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

from services.moysklad import get_all_stock, get_sales_stats
from services.notifier import get_notify_recipients, send_to_recipients
from utils.formatters import format_sales_report, format_stock_report
from utils.helpers import utc_now

logger = logging.getLogger(__name__)

MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def seconds_until(hour: int, minute: int = 0, weekday: int = None) -> float:
    """Секунды до следующего наступления указанного времени UTC."""
    now = utc_now()
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


async def get_stock_report_data() -> dict:
    """Данные для отчёта по остаткам: залежавшиеся / быстро уходящие / критичные."""
    rows = await get_all_stock()
    if not rows:
        return {"slow": [], "fast": [], "critical": []}

    rows_with_days = [(r, r.get("stockDays", 0)) for r in rows]

    slow = sorted(
        [(r, d) for r, d in rows_with_days if d >= 30],
        key=lambda x: x[1],
        reverse=True,
    )[:10]

    fast = sorted(
        [(r, d) for r, d in rows_with_days if 0 < d < 7],
        key=lambda x: x[1],
    )[:10]

    critical = [r for r in rows if r.get("stock", 0) < 20][:10]

    return {"slow": slow, "fast": fast, "critical": critical}


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
    except Exception:
        # Полное исключение в логи, в отчёт — generic. Раньше exception
        # text вставлялся в <code>…</code> и мог сломать HTML parsing
        # (или утечь внутренности МойСклад API в Telegram-чат).
        logger.exception("Ошибка сборки отчёта")
        return (
            "❌ Не удалось собрать отчёт. Подробности в логах сервиса."
        )


async def send_report(bot: Bot, text: str):
    """Разослать отчёт всем admin и boss."""
    recipients = get_notify_recipients()
    await send_to_recipients(bot, text, recipients)


# ─── Задачи ───────────────────────────────────────────────────────────────────


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
    STOCK_INTERVAL = 2 * 3600  # 2 часа

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

            # 2) Остатки — каждые 2 часа
            mono = asyncio.get_event_loop().time()
            if mono - last_stock_refresh >= STOCK_INTERVAL:
                logger.info("snapshot: 2h safety-net refresh_stock")
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


async def daily_report_task(bot: Bot):
    """Каждый день в 09:00 UTC (12:00 Ташкент)."""
    logger.info("Ежедневный отчёт запущен")
    while True:
        await asyncio.sleep(seconds_until(9, 0))
        try:
            now = utc_now()
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
            now = utc_now()
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
        now = utc_now()
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
                label, first_prev, first_this, first_prev_prev, first_prev,
            )
            await send_report(bot, text)
            logger.info("Ежемесячный отчёт отправлен")
        except Exception as e:
            logger.error("Ошибка ежемесячного отчёта: %s", e)
