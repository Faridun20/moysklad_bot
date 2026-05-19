"""
Хэндлеры: отгрузки
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from services.roles import can_view_stock
from services.moysklad import get_shipments, get_shipment_positions
from utils.helpers import extract_id_from_href, user_safe_error, utc_now
from utils.formatters import format_shipment
from utils.keyboards import (
    period_keyboard,
    shipments_nav_keyboard,
    shipments_back_keyboard,
)

logger = logging.getLogger(__name__)
router = Router()

# Навигационное состояние: {chat_id: {since, until, label}}.
# Полный список отгрузок НЕ храним — get_shipments() имеет собственный
# 60-секундный TTL-кэш. Такой подход убирает неограниченный рост памяти
# при большом количестве чатов или тяжёлых выборках.
shipments_cache: dict[int, dict] = {}

PER_PAGE = 5


def is_allowed(user_id: int) -> bool:
    return can_view_stock(user_id)


# ─── Команды ─────────────────────────────────────────────────────────────────


@router.message(Command("shipments"))
async def cmd_shipments(message: Message):
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "📅 За какой период показать отгрузки?", reply_markup=period_keyboard()
    )


# ─── Callback ─────────────────────────────────────────────────────────────────


@router.callback_query(F.data == "sh_period")
async def cb_sh_period(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📅 За какой период показать отгрузки?", reply_markup=period_keyboard()
    )


@router.callback_query(F.data.startswith("sh:"))
async def cb_shipments_period(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]
    now = utc_now()

    if period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        until, label = None, "сегодня"
    elif period == "yesterday":
        since = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        until = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "вчера"
    elif period == "7d":
        since, until, label = now - timedelta(days=7), None, "последние 7 дней"
    elif period == "30d":
        since, until, label = now - timedelta(days=30), None, "последние 30 дней"
    elif period == "month":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        until, label = None, "этот месяц"
    else:
        since, until, label = now - timedelta(hours=24), None, "последние 24 ч"

    await show_shipments(bot, call.message.chat.id, since, until, label, page=0)


@router.callback_query(F.data.startswith("shp:"))
async def cb_shipments_page(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    page = int(call.data.split(":")[1])
    cached = shipments_cache.get(call.message.chat.id)
    if not cached:
        return await call.message.answer(
            "❌ Данные устарели. Выберите период заново.",
            reply_markup=shipments_back_keyboard(),
        )
    await show_shipments(
        bot,
        call.message.chat.id,
        cached["since"],
        cached["until"],
        cached["label"],
        page,
    )


# ─── Логика ───────────────────────────────────────────────────────────────────


async def show_shipments(
    bot: Bot, chat_id: int, since: datetime, until: datetime, label: str, page: int = 0
):
    is_first = page == 0
    if is_first:
        await bot.send_message(chat_id, f"⏳ Загружаю отгрузки за {label}…")
    try:
        # Всегда вызываем get_shipments — при пагинации данные придут из
        # 60-секундного TTL-кэша (быстро), при первом запросе — из API.
        # Навигационные параметры (since/until/label) обновляем при is_first.
        shipments = await get_shipments(since, until)
        if is_first:
            shipments_cache[chat_id] = {"label": label, "since": since, "until": until}
        elif chat_id in shipments_cache:
            label = shipments_cache[chat_id].get("label", label)

        if not shipments:
            return await bot.send_message(
                chat_id,
                f"🚚 Нет отгрузок за {label}.",
                reply_markup=shipments_back_keyboard(),
            )

        total = len(shipments)
        total_pages = (total + PER_PAGE - 1) // PER_PAGE
        start = page * PER_PAGE
        end = min(start + PER_PAGE, total)

        header = (
            f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"🚚 <b>Отгрузки · {label}</b>\n"
            f"<code>стр {page + 1}/{total_pages} · всего {total}</code>"
        )
        await bot.send_message(chat_id, header, parse_mode="HTML")

        for s in shipments[start:end]:
            demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
            positions = []
            if demand_id:
                try:
                    positions = await get_shipment_positions(demand_id)
                except Exception as e:
                    logger.warning("Позиции %s: %s", demand_id, e)
            txt = format_shipment(s, positions)
            await bot.send_message(chat_id, txt, parse_mode="HTML")

        kb = shipments_nav_keyboard(page, total_pages)
        await bot.send_message(
            chat_id, f"Стр. {page + 1} из {total_pages}", reply_markup=kb
        )

    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "shipments_list"))
