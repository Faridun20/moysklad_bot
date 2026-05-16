"""
Хэндлеры: аналитика продаж
"""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from config import ALLOWED_USERS
from services.moysklad import get_sales_stats
from utils.formatters import format_sales_report
from utils.keyboards import analytics_keyboard, analytics_back_keyboard

logger = logging.getLogger(__name__)
router = Router()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


@router.message(Command("analytics"))
async def cmd_analytics(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer("📊 За какой период показать аналитику?", reply_markup=analytics_keyboard())


@router.callback_query(F.data == "analytics")
async def cb_analytics(call: CallbackQuery):
    await call.answer()
    await call.message.answer("📊 За какой период показать аналитику?", reply_markup=analytics_keyboard())


@router.callback_query(F.data.startswith("an:"))
async def cb_analytics_period(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]
    now = datetime.utcnow()

    periods = {
        "week":   (now - timedelta(weeks=1),  now - timedelta(weeks=2),  "Эта неделя"),
        "month":  (now - timedelta(days=30),   now - timedelta(days=60),  "Этот месяц"),
        "3month": (now - timedelta(days=90),   now - timedelta(days=180), "3 месяца"),
        "6month": (now - timedelta(days=182),  now - timedelta(days=365), "Полгода"),
        "year":   (now - timedelta(days=365),  now - timedelta(days=730), "Год"),
    }

    since, prev_since, label = periods.get(
        period, (now - timedelta(days=30), now - timedelta(days=60), "Месяц")
    )
    prev_until = since

    await show_analytics(bot, call.message.chat.id, since, now, prev_since, prev_until, label)


async def show_analytics(
    bot: Bot, chat_id: int,
    since: datetime, until: datetime,
    prev_since: datetime, prev_until: datetime,
    label: str,
):
    await bot.send_message(
        chat_id,
        f"⏳ Считаю статистику за {label}…\n<i>Это может занять до 30 секунд</i>",
        parse_mode="HTML"
    )
    try:
        current_stats, prev_stats = await asyncio.gather(
            get_sales_stats(since, until),
            get_sales_stats(prev_since, prev_until),
        )
        txt = format_sales_report(label, current_stats, prev_stats)
        await bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=analytics_back_keyboard())
    except Exception as e:
        logger.error("Ошибка аналитики: %s", e)
        await bot.send_message(chat_id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")
