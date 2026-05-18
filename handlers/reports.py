"""
Автоматические отчёты: ежедневные, еженедельные, ежемесячные
+ Отчёт по остаткам склада (залежавшиеся / быстро уходящие)
"""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from tasks.scheduled import build_sales_and_stock_report

from config import ADMIN_IDS
from services.moysklad import get_all_stock, get_sales_stats
from services.roles import can_manage_users, is_boss
from utils.helpers import format_price, trend_arrow, extract_id_from_href
from utils.formatters import format_sales_report, format_stock_report
from services.database import get_role

logger = logging.getLogger(__name__)
router = Router()

MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

async def get_stock_report_data() -> dict:
    rows = await get_all_stock()
    if not rows:
        return {"slow": [], "fast": [], "critical": []}

    rows_with_days = [(r, r.get("stockDays", 0)) for r in rows]

    slow = sorted(
        [(r, d) for r, d in rows_with_days if d >= 30], key=lambda x: x[1], reverse=True
    )[:10]

    fast = sorted([(r, d) for r, d in rows_with_days if 0 < d < 7], key=lambda x: x[1])[
        :10
    ]

    critical = [r for r in rows if r.get("stock", 0) < 20][:10]

    return {"slow": slow, "fast": fast, "critical": critical}

def reports_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Отчёт за сегодня", callback_data="rep:today")
    kb.button(text="📊 Отчёт за неделю", callback_data="rep:week")
    kb.button(text="📊 Отчёт за месяц", callback_data="rep:month")
    kb.button(text="📦 Остатки склада", callback_data="rep:stock")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command("reports"))
async def cmd_reports(message: Message):
    if not is_boss(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer("📊 Выберите отчёт:", reply_markup=reports_keyboard())


@router.callback_query(F.data == "reports_menu")
async def cb_reports_menu(call: CallbackQuery):
    if not is_boss(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()
    await call.message.answer("📊 Выберите отчёт:", reply_markup=reports_keyboard())


@router.callback_query(F.data.startswith("rep:"))
async def cb_report(call: CallbackQuery, bot: Bot):
    if not is_boss(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]
    now = datetime.utcnow()

    if period == "stock":
        await call.message.answer("⏳ Анализирую остатки…")
        try:
            data = await get_stock_report_data()
            txt = format_stock_report(data)
            kb = InlineKeyboardBuilder()
            kb.button(text="📊 Другие отчёты", callback_data="reports_menu")
            kb.button(text="🏠 Меню", callback_data="menu")
            kb.adjust(1)
            await call.message.answer(
                txt, parse_mode="HTML", reply_markup=kb.as_markup()
            )
        except Exception as e:
            await call.message.answer(
                f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML"
            )
        return

    await call.message.answer(
        "⏳ Формирую отчёт…\n<i>До 30 секунд</i>", parse_mode="HTML"
    )

    if period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        until = now
        prev_since = since - timedelta(days=1)
        prev_until = since
        label = f"сегодня {since.strftime('%d.%m.%Y')}"

    elif period == "week":
        since = now - timedelta(days=7)
        until = now
        prev_since = since - timedelta(days=7)
        prev_until = since
        label = "последние 7 дней"

    elif period == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        until = now
        if since.month == 1:
            prev_since = since.replace(year=since.year - 1, month=12)
        else:
            prev_since = since.replace(month=since.month - 1)
        prev_until = since
        label = f"{MONTH_NAMES[since.month]} {since.year}"

    else:
        return

    try:
        text = await build_sales_and_stock_report(
            label, since, until, prev_since, prev_until
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="📊 Другие отчёты", callback_data="reports_menu")
        kb.button(text="🏠 Меню", callback_data="menu")
        kb.adjust(1)
        await call.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception as e:
        logger.error("Ошибка отчёта: %s", e)
        await call.message.answer(f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML")
