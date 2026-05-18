"""
Хэндлеры: аналитика продаж
- boss/admin видит общую аналитику компании
- manager видит только свою личную аналитику
"""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from services.roles import can_view_analytics, is_boss
from services.moysklad import get_sales_stats, get_employee_stats, get_employee_href
from services.database import (
    get_moysklad_employee_id, get_role,
    get_user_orders, get_order_items,
)
from utils.formatters import format_sales_report
from utils.keyboards import analytics_keyboard, analytics_back_keyboard
from utils.helpers import user_safe_error

logger = logging.getLogger(__name__)
router = Router()


def get_period(period: str, now: datetime) -> tuple:
    """Вернуть (since, until, prev_since, prev_until, label) для периода."""
    periods = {
        "week": (
            now - timedelta(weeks=1), now,
            now - timedelta(weeks=2), now - timedelta(weeks=1),
            "Эта неделя",
        ),
        "month": (
            now - timedelta(days=30), now,
            now - timedelta(days=60), now - timedelta(days=30),
            "Этот месяц",
        ),
        "3month": (
            now - timedelta(days=90), now,
            now - timedelta(days=180), now - timedelta(days=90),
            "3 месяца",
        ),
        "6month": (
            now - timedelta(days=182), now,
            now - timedelta(days=365), now - timedelta(days=182),
            "Полгода",
        ),
        "year": (
            now - timedelta(days=365), now,
            now - timedelta(days=730), now - timedelta(days=365),
            "Год",
        ),
    }
    return periods.get(
        period,
        (now - timedelta(days=30), now,
         now - timedelta(days=60), now - timedelta(days=30),
         "Месяц"),
    )


@router.message(Command("analytics"))
async def cmd_analytics(message: Message):
    if not can_view_analytics(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    role = get_role(message.from_user.id)
    prefix = "📊 Ваша аналитика" if role == "manager" else "📊 Аналитика компании"
    await message.answer(
        f"{prefix}\nЗа какой период?",
        reply_markup=analytics_keyboard(),
    )


@router.callback_query(F.data == "analytics")
async def cb_analytics(call: CallbackQuery):
    if not can_view_analytics(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    role = get_role(call.from_user.id)
    prefix = "📊 Ваша аналитика" if role == "manager" else "📊 Аналитика компании"
    await call.message.answer(
        f"{prefix}\nЗа какой период?",
        reply_markup=analytics_keyboard(),
    )


@router.callback_query(F.data.startswith("an:"))
async def cb_analytics_period(call: CallbackQuery, bot: Bot):
    if not can_view_analytics(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]
    now = datetime.utcnow()
    since, until, prev_since, prev_until, label = get_period(period, now)

    role = get_role(call.from_user.id)

    if role == "manager":
        await show_manager_analytics(
            bot, call.message.chat.id,
            call.from_user.id,
            since, until, prev_since, prev_until, label,
        )
    else:
        await show_company_analytics(
            bot, call.message.chat.id,
            since, until, prev_since, prev_until, label,
        )


async def show_company_analytics(
    bot: Bot, chat_id: int,
    since: datetime, until: datetime,
    prev_since: datetime, prev_until: datetime,
    label: str,
):
    """Общая аналитика компании для boss/admin."""
    await bot.send_message(
        chat_id,
        f"⏳ Считаю аналитику компании за {label}…\n<i>До 30 секунд</i>",
        parse_mode="HTML",
    )
    try:
        current_stats, prev_stats = await asyncio.gather(
            get_sales_stats(since, until),
            get_sales_stats(prev_since, prev_until),
        )
        txt = format_sales_report(f"🏢 Компания · {label}", current_stats, prev_stats)
        await bot.send_message(
            chat_id, txt, parse_mode="HTML",
            reply_markup=analytics_back_keyboard(),
        )
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "company_analytics"))


def _personal_stats_from_local(
    user_id: int, since: datetime, until: datetime
) -> dict:
    """
    Личные показатели менеджера из локальной БД — суммы и счётчики по
    одобренным/отгруженным заказам в период.

    Источник истины — наша БД (orders + order_items), без зависимости
    от связки МойСклад employee. Так аналитика работает у любого
    менеджера, даже если его не получилось привязать к employee в MS.
    """
    orders = get_user_orders(user_id)
    since_iso = since.strftime("%Y-%m-%d %H:%M:%S")
    until_iso = until.strftime("%Y-%m-%d %H:%M:%S")

    total = 0.0
    count = 0
    clients: set[str] = set()
    products_agg: dict[str, dict] = {}

    for o in orders:
        if o["status"] not in ("approved", "shipped"):
            continue
        ts = (o.get("updated_at") or o.get("created_at") or "")[:19]
        if ts < since_iso or ts > until_iso:
            continue
        items = get_order_items(o["id"])
        order_sum = sum(
            float(it.get("quantity", 0)) * float(it.get("price", 0) or 0)
            for it in items
        )
        total += order_sum
        count += 1
        if o.get("agent_name"):
            clients.add(o["agent_name"])
        for it in items:
            name = it.get("product_name", "—")
            qty = float(it.get("quantity", 0))
            price = float(it.get("price", 0) or 0)
            agg = products_agg.setdefault(name, {"sum": 0.0, "qty": 0.0})
            agg["sum"] += qty * price
            agg["qty"] += qty

    top_products = sorted(
        products_agg.items(), key=lambda kv: kv[1]["sum"], reverse=True
    )[:5]

    # Формат совместим с format_sales_report — total в минорных единицах (×100)
    return {
        "total": int(round(total * 100)),
        "count": count,
        "clients": len(clients),
        "top_products": top_products,
    }


async def show_manager_analytics(
    bot: Bot, chat_id: int,
    user_id: int,
    since: datetime, until: datetime,
    prev_since: datetime, prev_until: datetime,
    label: str,
):
    """Персональная аналитика менеджера — из локальной БД.

    Раньше требовалось чтобы аккаунт был привязан к employee в МойСклад,
    что часто не работало (создание сотрудников требует платного тарифа).
    Теперь считаем по одобренным заявкам бота — данные всегда доступны.
    """
    await bot.send_message(
        chat_id,
        f"⏳ Считаю вашу аналитику за {label}…",
        parse_mode="HTML",
    )
    try:
        current_stats = _personal_stats_from_local(user_id, since, until)
        prev_stats = _personal_stats_from_local(user_id, prev_since, prev_until)

        if current_stats["count"] == 0 and prev_stats["count"] == 0:
            return await bot.send_message(
                chat_id,
                f"📊 За «{label}» нет ваших одобренных отгрузок.\n"
                f"Аналитика появится после первой одобренной заявки.",
                reply_markup=analytics_back_keyboard(),
            )

        txt = format_sales_report(
            f"👤 Моя аналитика · {label}", current_stats, prev_stats
        )
        await bot.send_message(
            chat_id, txt, parse_mode="HTML",
            reply_markup=analytics_back_keyboard(),
        )
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, f"manager_analytics:{user_id}"))


async def show_manager_summary(bot: Bot, chat_id: int, user_id: int):
    """Краткая сводка менеджера за текущий месяц — из локальной БД."""
    try:
        now = datetime.utcnow()
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        stats = _personal_stats_from_local(user_id, since, now)
        if stats["count"] == 0:
            return

        from utils.helpers import format_price
        from utils.formatters import DIV

        total_str = format_price(stats["total"])
        avg_str = format_price(stats["total"] / stats["count"]) if stats["count"] else "0"

        text = (
            f"{DIV}\n"
            f"📊 <b>Ваши показатели · этот месяц</b>\n"
            f"\n"
            f"💰 Выручка: <b>{total_str}</b>\n"
            f"🚚 Отгрузок: <b>{stats['count']}</b>\n"
            f"👥 Клиентов: <b>{stats['clients']}</b>\n"
            f"📋 Средний чек: <b>{avg_str}</b>"
        )
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning("Не удалось показать сводку менеджера: %s", e)
