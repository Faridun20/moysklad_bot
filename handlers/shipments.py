"""
Хэндлеры: отгрузки.

Отгрузка = расходная накладная нашего склада (`services.warehouse`). Раньше
список приезжал из МойСклад (`entity/demand` + позиции каждого документа
отдельным запросом), и ради этого в модуле жил TTL-кэш и постраничная догрузка
по сети. Запрос теперь локальный: берём период целиком одним SELECT, позиции —
по одной накладной на сообщение.
"""

import logging
from datetime import datetime, timedelta

from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from services.roles import can_view_stock
from utils.formatters import format_shipment
from utils.helpers import local_now, user_safe_error
from utils.keyboards import (
    shipments_nav_keyboard,
    shipments_back_keyboard,
)

logger = logging.getLogger(__name__)
router = Router()

# Навигационное состояние: {chat_id: {since, until, label}}. Сами накладные не
# храним — выборка локальная и стоит один запрос.
shipments_cache: dict[int, dict] = {}

PER_PAGE = 5


def is_allowed(user_id: int) -> bool:
    return can_view_stock(user_id)


# ─── Команды ─────────────────────────────────────────────────────────────────


@router.message(Command("shipments"))
async def cmd_shipments(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        return
    # Сразу показываем отгрузки за сегодня; период переключается чипами под списком.
    now = local_now()
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await show_shipments(bot, message.chat.id, since, None, "сегодня", page=0)


# ─── Callback ─────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("sh:"))
async def cb_shipments_period(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Отгрузки смотрит склад и руководство", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]
    # local_now, а не utc_now: `invoices.invoice_date` пишется в локальной зоне
    # (как и весь остальной «сегодня» в проекте). На UTC-сервере вечерняя
    # накладная иначе выпадала бы из «сегодня».
    now = local_now()

    if period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        until, label = None, "сегодня"
    elif period == "yesterday":
        since = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
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
        return await call.answer("Отгрузки смотрит склад и руководство", show_alert=True)
    await call.answer()
    page = int(call.data.split(":")[1])
    cached = shipments_cache.get(call.message.chat.id)
    if not cached:
        return await call.message.answer(
            "❌ Список уже не в памяти бота. Выберите период заново кнопкой ниже.",
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
    from services import warehouse

    is_first = page == 0
    try:
        rows = await warehouse.list_shipments(since, until)
        if is_first:
            shipments_cache[chat_id] = {"label": label, "since": since, "until": until}
        elif chat_id in shipments_cache:
            label = shipments_cache[chat_id].get("label", label)

        if not rows:
            return await bot.send_message(
                chat_id,
                f"🚚 Нет отгрузок за {label}.",
                reply_markup=shipments_back_keyboard(),
            )

        total = len(rows)
        total_pages = (total + PER_PAGE - 1) // PER_PAGE
        start = page * PER_PAGE
        end = min(start + PER_PAGE, total)

        header = (
            f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
            f"🚚 <b>Отгрузки · {label}</b>\n"
            f"<code>стр {page + 1}/{total_pages} · всего {total}</code>"
        )
        await bot.send_message(chat_id, header, parse_mode="HTML")

        for row in rows[start:end]:
            invoice = await warehouse.get_invoice(int(row["id"]))
            if invoice is None:
                continue
            await bot.send_message(chat_id, format_shipment(invoice), parse_mode="HTML")

        kb = shipments_nav_keyboard(page, total_pages)
        await bot.send_message(chat_id, f"Стр. {page + 1} из {total_pages}", reply_markup=kb)

    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "shipments_list"))
