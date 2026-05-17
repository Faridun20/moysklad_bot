"""
Хэндлеры: остатки на складе и категории
"""

import logging
from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from utils.roles import (
    can_view_stock,
    can_view_analytics,
    can_manage_payments,
    is_admin,
)
from services.moysklad import get_all_stock, get_categories
from utils.helpers import extract_id_from_href
from utils.formatters import format_stock_page
from utils.keyboards import stock_nav_keyboard, categories_keyboard, main_keyboard

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 10

# Кэш в памяти
categories_cache: dict[int, list[dict]] = {}
stock_cache: dict[int, dict] = {}


def is_allowed(user_id: int) -> bool:
    return can_view_stock(user_id)


# ─── Команды ─────────────────────────────────────────────────────────────────


@router.message(Command("stock"))
async def cmd_stock(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        return
    await show_stock_all(bot, message.chat.id, 0)


@router.message(Command("categories"))
async def cmd_categories(message: Message, bot: Bot):
    if not is_allowed(message.from_user.id):
        return
    await show_categories(bot, message.chat.id, 0)


# ─── Callback ─────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("sp:"))
async def cb_stock_all(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    page = int(call.data.split(":")[1])
    cached = stock_cache.get(call.message.chat.id)
    if cached and cached.get("mode") == "all":
        rows = cached["rows"]
        txt = format_stock_page(rows, page)
        kb = stock_nav_keyboard(page, len(rows), "all")
        await call.message.answer(txt, parse_mode="HTML", reply_markup=kb)
    else:
        await show_stock_all(bot, call.message.chat.id, page)


@router.callback_query(F.data.startswith("cats:"))
async def cb_categories_page(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    page = int(call.data.split(":")[1])
    await show_categories(bot, call.message.chat.id, page)


@router.callback_query(F.data.startswith("ci:"))
async def cb_category_select(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    idx = int(call.data.split(":")[1])
    cats = categories_cache.get(call.message.chat.id, [])
    if not cats or idx >= len(cats):
        return await call.message.answer("❌ Список устарел. Нажмите /categories")
    await show_stock_category(bot, call.message.chat.id, 0, idx, cats[idx])


@router.callback_query(F.data.startswith("sc:"))
async def cb_stock_cat_page(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    parts = call.data.split(":")
    page = int(parts[1])
    idx = int(parts[2])
    cached = stock_cache.get(call.message.chat.id)
    if cached and cached.get("mode") == "cat" and cached.get("cat_idx") == idx:
        rows = cached["rows"]
        cat_name = cached.get("cat_name", "")
        txt = format_stock_page(rows, page, cat_name)
        kb = stock_nav_keyboard(page, len(rows), "cat", idx)
        await call.message.answer(txt, parse_mode="HTML", reply_markup=kb)
    else:
        cats = categories_cache.get(call.message.chat.id, [])
        if not cats or idx >= len(cats):
            return await call.message.answer("❌ Список устарел. Нажмите /categories")
        await show_stock_category(bot, call.message.chat.id, page, idx, cats[idx])


# ─── Логика ───────────────────────────────────────────────────────────────────


async def show_categories(bot: Bot, chat_id: int, page: int):
    try:
        cats = categories_cache.get(chat_id)
        if not cats:
            await bot.send_message(chat_id, "⏳ Загружаю категории…")
            cats = await get_categories()
            if not cats:
                return await bot.send_message(chat_id, "📂 Категории не найдены.")
            categories_cache[chat_id] = cats
        await bot.send_message(
            chat_id,
            f"🗂 <b>Выберите категорию</b> (всего {len(cats)}):",
            parse_mode="HTML",
            reply_markup=categories_keyboard(cats, page),
        )
    except Exception as e:
        logger.exception("Ошибка категорий")
        await bot.send_message(
            chat_id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML"
        )


async def show_stock_all(bot: Bot, chat_id: int, page: int):
    await bot.send_message(chat_id, "⏳ Загружаю все остатки…")
    try:
        rows = await get_all_stock()
        if not rows:
            return await bot.send_message(chat_id, "📦 Склад пуст.")
        stock_cache[chat_id] = {
            "rows": rows,
            "mode": "all",
            "cat_name": "",
            "cat_idx": 0,
        }
        txt = format_stock_page(rows, page)
        kb = stock_nav_keyboard(page, len(rows), "all")
        await bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error("Ошибка остатков: %s", e)
        await bot.send_message(
            chat_id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML"
        )


async def show_stock_category(bot: Bot, chat_id: int, page: int, idx: int, cat: dict):
    await bot.send_message(chat_id, "⏳ Загружаю остатки категории…")
    try:
        all_rows = await get_all_stock()
        cat_href = cat.get("meta", {}).get("href", "")
        cat_id = extract_id_from_href(cat_href)
        cat_name = cat.get("name", "—")
        rows = [
            r
            for r in all_rows
            if extract_id_from_href(r.get("folder", {}).get("meta", {}).get("href", ""))
            == cat_id
        ]
        stock_cache[chat_id] = {
            "rows": rows,
            "mode": "cat",
            "cat_name": cat_name,
            "cat_idx": idx,
        }
        if not rows:
            return await bot.send_message(
                chat_id, f"📦 В категории «{cat_name}» нет товаров с остатком."
            )
        txt = format_stock_page(rows, page, cat_name)
        kb = stock_nav_keyboard(page, len(rows), "cat", idx)
        await bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error("Ошибка категории: %s", e)
        await bot.send_message(
            chat_id, f"❌ Ошибка:\n<code>{e}</code>", parse_mode="HTML"
        )
