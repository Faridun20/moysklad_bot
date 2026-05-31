"""
Хэндлеры: остатки на складе и категории
"""

import logging
import time
from aiogram import Bot, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from services.roles import (
    can_view_stock,
)
from services.moysklad import get_all_stock, get_categories
from services import async_db as adb
from config import BASE_CURRENCY
from utils.helpers import extract_id_from_href, extract_href, user_safe_error
from utils.formatters import format_stock_page
from utils.keyboards import stock_nav_keyboard, categories_keyboard


logger = logging.getLogger(__name__)
router = Router()


async def _attach_prices(rows: list[dict]) -> None:
    """Прикрепить к строкам остатков цену продажи (из /prices) → r['_sale_price_str'].
    Один батч-запрос; форматтер потом просто рендерит поле. Best-effort."""
    try:
        prices = await adb.get_all_product_prices()
    except Exception:
        return
    by_ms = {p["ms_id"]: p for p in prices}
    for r in rows:
        ms_id = extract_id_from_href(extract_href(r))
        p = by_ms.get(ms_id)
        if p and p.get("sale_price"):
            cur = p.get("currency") or BASE_CURRENCY
            r["_sale_price_str"] = f"💵 {int(round(p['sale_price']))} {cur}"

# TTL-кэши в памяти (на чат). Чистятся ленивым GC при превышении лимита.
_CATEGORIES_TTL = 300.0  # 5 мин — категории редко меняются
_STOCK_TTL = 60.0  # 1 мин — остатки могут двигаться
_CACHE_MAX_ENTRIES = 500  # верхняя граница, чтобы кэш не рос бесконечно

categories_cache: dict[int, tuple[float, list[dict]]] = {}
stock_cache: dict[int, tuple[float, dict]] = {}


def _evict_expired(cache: dict, ttl: float) -> None:
    """Удалить просроченные записи. При переполнении сбросить самые старые."""
    now = time.monotonic()
    expired = [k for k, (ts, _) in cache.items() if now - ts >= ttl]
    for k in expired:
        cache.pop(k, None)
    if len(cache) > _CACHE_MAX_ENTRIES:
        # отсортировать по возрастанию ts и выкинуть половину старых
        for k, _ in sorted(cache.items(), key=lambda kv: kv[1][0])[: len(cache) // 2]:
            cache.pop(k, None)


def _cache_get(cache: dict, key: int, ttl: float):
    entry = cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts >= ttl:
        cache.pop(key, None)
        return None
    return value


def _cache_put(cache: dict, key: int, value, ttl: float) -> None:
    _evict_expired(cache, ttl)
    cache[key] = (time.monotonic(), value)


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
    cached = _cache_get(stock_cache, call.message.chat.id, _STOCK_TTL)
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
    cats = _cache_get(categories_cache, call.message.chat.id, _CATEGORIES_TTL) or []
    if not cats or idx >= len(cats):
        # Кэш истёк — не оставляем пользователя в тупике, сразу
        # перезагружаем список категорий с рабочими кнопками.
        return await show_categories(bot, call.message.chat.id, 0)
    await show_stock_category(bot, call.message.chat.id, 0, idx, cats[idx])


@router.callback_query(F.data.startswith("sc:"))
async def cb_stock_cat_page(call: CallbackQuery, bot: Bot):
    if not is_allowed(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    parts = call.data.split(":")
    page = int(parts[1])
    idx = int(parts[2])
    cached = _cache_get(stock_cache, call.message.chat.id, _STOCK_TTL)
    if cached and cached.get("mode") == "cat" and cached.get("cat_idx") == idx:
        rows = cached["rows"]
        cat_name = cached.get("cat_name", "")
        txt = format_stock_page(rows, page, cat_name)
        kb = stock_nav_keyboard(page, len(rows), "cat", idx)
        await call.message.answer(txt, parse_mode="HTML", reply_markup=kb)
    else:
        cats = _cache_get(categories_cache, call.message.chat.id, _CATEGORIES_TTL) or []
        if not cats or idx >= len(cats):
            # Кэш истёк — перезагружаем категории вместо тупикового сообщения.
            return await show_categories(bot, call.message.chat.id, 0)
        await show_stock_category(bot, call.message.chat.id, page, idx, cats[idx])


# ─── Логика ───────────────────────────────────────────────────────────────────


async def show_categories(bot: Bot, chat_id: int, page: int):
    try:
        cats = _cache_get(categories_cache, chat_id, _CATEGORIES_TTL)
        if not cats:
            await bot.send_message(chat_id, "⏳ Загружаю категории…")
            cats = await get_categories()
            if not cats:
                return await bot.send_message(chat_id, "📂 Категории не найдены.")
            _cache_put(categories_cache, chat_id, cats, _CATEGORIES_TTL)
        await bot.send_message(
            chat_id,
            f"🗂 <b>Выберите категорию</b> (всего {len(cats)}):",
            parse_mode="HTML",
            reply_markup=categories_keyboard(cats, page),
        )
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "show_categories"))


async def show_stock_all(bot: Bot, chat_id: int, page: int):
    await bot.send_message(chat_id, "⏳ Загружаю все остатки…")
    try:
        rows = await get_all_stock()
        if not rows:
            return await bot.send_message(chat_id, "📦 Склад пуст.")
        await _attach_prices(rows)
        _cache_put(
            stock_cache,
            chat_id,
            {
                "rows": rows,
                "mode": "all",
                "cat_name": "",
                "cat_idx": 0,
            },
            _STOCK_TTL,
        )
        txt = format_stock_page(rows, page)
        kb = stock_nav_keyboard(page, len(rows), "all")
        await bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "show_stock_all"))


async def show_stock_category(bot: Bot, chat_id: int, page: int, idx: int, cat: dict):
    await bot.send_message(chat_id, "⏳ Загружаю остатки категории…")
    try:
        all_rows = await get_all_stock()
        cat_id = extract_id_from_href(extract_href(cat))
        cat_name = cat.get("name", "—")
        rows = [r for r in all_rows if extract_id_from_href(extract_href(r, "folder")) == cat_id]
        await _attach_prices(rows)
        _cache_put(
            stock_cache,
            chat_id,
            {
                "rows": rows,
                "mode": "cat",
                "cat_name": cat_name,
                "cat_idx": idx,
            },
            _STOCK_TTL,
        )
        if not rows:
            return await bot.send_message(
                chat_id, f"📦 В категории «{cat_name}» нет товаров с остатком."
            )
        txt = format_stock_page(rows, page, cat_name)
        kb = stock_nav_keyboard(page, len(rows), "cat", idx)
        await bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        await bot.send_message(chat_id, user_safe_error(e, "show_stock_category"))
