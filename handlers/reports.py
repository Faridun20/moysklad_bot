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

from config import ADMIN_IDS
from services.moysklad import get_all_stock, get_sales_stats
from utils.roles import can_manage_users, is_boss
from utils.helpers import format_price, trend_arrow, extract_id_from_href
from utils.formatters import format_sales_report
from services.database import get_role

logger = logging.getLogger(__name__)
router = Router()

MONTH_NAMES = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]


# ─── Отчёт по остаткам ────────────────────────────────────────────────────────


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


def format_stock_report(data: dict) -> str:
    slow = data["slow"]
    fast = data["fast"]
    critical = data["critical"]

    lines = []
    lines.append("<code>━━━━━━━━━━━━━━━━━━━━</code>")
    lines.append("📦 <b>Отчёт по остаткам склада</b>")
    lines.append("")

    if slow:
        lines.append("🐢 <b>Залежались (≥30 дней):</b>")
        for r, days in slow[:5]:
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = r.get("uom", {}).get("name", "шт")
            lines.append(f"  • <b>{name}</b>")
            lines.append(f"    <code>{stock} {unit} · {days} дней</code>")
        lines.append("")

    if fast:
        lines.append("🚀 <b>Быстро уходят (&lt;7 дней):</b>")
        for r, days in fast[:5]:
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = r.get("uom", {}).get("name", "шт")
            lines.append(f"  • <b>{name}</b>")
            lines.append(f"    <code>{stock} {unit} · {days} дней</code>")
        lines.append("")

    if critical:
        lines.append("🔴 <b>Критический остаток (&lt;20):</b>")
        for r in critical[:5]:
            name = r.get("name", "—")
            stock = r.get("stock", 0)
            unit = r.get("uom", {}).get("name", "шт")
            lines.append(f"  • <b>{name}</b>: <code>{stock} {unit}</code>")

    if not slow and not fast and not critical:
        lines.append("✅ <i>Всё в норме — проблем не обнаружено</i>")

    return "\n".join(lines)


# ─── Отправка отчётов ─────────────────────────────────────────────────────────


async def send_to_bosses(bot: Bot, text: str):
    """Отправить сообщение всем boss и admin."""
    from services.database import get_all_users

    users = get_all_users()
    recipients = [u["user_id"] for u in users if u["role"] in ("admin", "boss")]
    # Добавляем ADMIN_IDS на случай если их нет в БД
    for uid in ADMIN_IDS:
        if uid not in recipients:
            recipients.append(uid)
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось отправить %d: %s", uid, e)


async def build_sales_and_stock_report(
    label: str,
    since: datetime,
    until: datetime,
    prev_since: datetime,
    prev_until: datetime,
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


# ─── Фоновые задачи ───────────────────────────────────────────────────────────


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
                f"{since.strftime('%d.%m.%Y')}", since, until, prev_since, since
            )
            await send_to_bosses(bot, text)
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
                "прошедшая неделя", since, now, prev_since, since
            )
            await send_to_bosses(bot, text)
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
                year=now.year + 1,
                month=1,
                day=1,
                hour=9,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            next_first = now.replace(
                month=now.month + 1, day=1, hour=9, minute=0, second=0, microsecond=0
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
                label, first_prev, first_this, first_prev_prev, first_prev
            )
            await send_to_bosses(bot, text)
            logger.info("Ежемесячный отчёт отправлен")
        except Exception as e:
            logger.error("Ошибка ежемесячного отчёта: %s", e)


# ─── Ручной запрос отчётов ────────────────────────────────────────────────────


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
