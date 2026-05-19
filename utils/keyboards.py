"""
Все клавиатуры бота
"""

from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import PAGE_SIZE  # единый источник в config


def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Все остатки", callback_data="sp:0")
    kb.button(text="🗂 По категориям", callback_data="cats:0")
    kb.button(text="🚚 Отгрузки", callback_data="sh_period")
    kb.button(text="📊 Аналитика продаж", callback_data="analytics")
    kb.button(text="💵 Отправить платёж", callback_data="pay_start")
    kb.button(text="📋 Отчёт по платежам", callback_data="pr:menu")
    kb.adjust(1)
    return kb.as_markup()


def period_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сегодня", callback_data="sh:today")
    kb.button(text="📅 Вчера", callback_data="sh:yesterday")
    kb.button(text="📅 7 дней", callback_data="sh:7d")
    kb.button(text="📅 30 дней", callback_data="sh:30d")
    kb.button(text="📅 Этот месяц", callback_data="sh:month")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def analytics_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Эта неделя", callback_data="an:week")
    kb.button(text="📅 Этот месяц", callback_data="an:month")
    kb.button(text="📅 3 месяца", callback_data="an:3month")
    kb.button(text="📅 Полгода", callback_data="an:6month")
    kb.button(text="📅 Год", callback_data="an:year")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def analytics_back_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Другой период", callback_data="analytics")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def categories_keyboard(cats: list[dict], page: int = 0):
    per_page = 8
    total = len(cats)
    total_pages = (total + per_page - 1) // per_page
    start = page * per_page
    end = min(start + per_page, total)

    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Все товары", callback_data="sp:0")
    for i in range(start, end):
        name = cats[i].get("name", "—")[:28]
        kb.button(text=f"📁 {name}", callback_data=f"ci:{i}")

    nav = []
    if page > 0:
        kb.button(text="◀️", callback_data=f"cats:{page - 1}")
        nav.append(1)
    if page < total_pages - 1:
        kb.button(text="▶️", callback_data=f"cats:{page + 1}")
        nav.append(1)

    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1, *([1] * (end - start)), *nav, 1)
    return kb.as_markup()


def stock_nav_keyboard(page: int, total: int, mode: str, cat_idx: int = 0):
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    kb = InlineKeyboardBuilder()
    nav = []
    if mode == "all":
        if page > 0:
            kb.button(text="◀️ Назад", callback_data=f"sp:{page - 1}")
            nav.append(1)
        if page < total_pages - 1:
            kb.button(text="Вперёд ▶️", callback_data=f"sp:{page + 1}")
            nav.append(1)
    else:
        if page > 0:
            kb.button(text="◀️ Назад", callback_data=f"sc:{page - 1}:{cat_idx}")
            nav.append(1)
        if page < total_pages - 1:
            kb.button(text="Вперёд ▶️", callback_data=f"sc:{page + 1}:{cat_idx}")
            nav.append(1)
    kb.button(text="🗂 Категории", callback_data="cats:0")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(*nav, 1, 1)
    return kb.as_markup()


def shipments_nav_keyboard(page: int, total_pages: int):
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        kb.button(text="◀️ Назад", callback_data=f"shp:{page - 1}")
        nav.append(1)
    if page < total_pages - 1:
        kb.button(text="Вперёд ▶️", callback_data=f"shp:{page + 1}")
        nav.append(1)
    kb.button(text="📅 Другой период", callback_data="sh_period")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(*nav, 1, 1)
    return kb.as_markup()


def shipments_back_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Другой период", callback_data="sh_period")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()
