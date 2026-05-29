"""
Все клавиатуры бота
"""

from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import PAGE_SIZE  # единый источник в config


_AN_PERIODS = [
    ("week", "Неделя"),
    ("month", "Месяц"),
    ("3month", "3 мес"),
    ("6month", "Полгода"),
    ("year", "Год"),
]


def analytics_keyboard(view: str = "sales", period: str = "month", is_boss: bool = False):
    """Клавиатура под отчётом аналитики.

    - чипы периода (активный помечен •…•), callback `an:<view>:<period>`;
    - «📅 Свой период» → `anp:<view>` (FSM ввода произвольного диапазона);
    - для босса — переключатель Продажи/Касса;
    - «🏠 Меню».

    `view` ∈ {"sales","cash"}; `period` — ключ периода или "custom"
    (тогда ни один чип не подсвечен).
    """
    kb = InlineKeyboardBuilder()
    for pk, plabel in _AN_PERIODS:
        text = f"• {plabel} •" if pk == period else plabel
        kb.button(text=text, callback_data=f"an:{view}:{pk}")
    kb.button(text="📅 Свой период", callback_data=f"anp:{view}")
    rows = [3, 2, 1]
    if is_boss:
        # При custom-периоде у переключателя нет валидного period-ключа —
        # откатываемся на "month", чтобы callback оставался кликабельным.
        keep = period if period != "custom" else "month"
        if view == "cash":
            kb.button(text="📊 Продажи", callback_data=f"an:sales:{keep}")
        else:
            kb.button(text="💰 Касса", callback_data=f"an:cash:{keep}")
        rows.append(1)
    kb.button(text="🏠 Меню", callback_data="menu")
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def analytics_back_keyboard():
    """Backward-compat обёртка: продажи / месяц / без босс-чипов."""
    return analytics_keyboard("sales", "month", False)


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

    # Стрелки ◀️/▶️ — в одном ряду (единый паттерн со stock/shipments).
    nav_count = 0
    if page > 0:
        kb.button(text="◀️", callback_data=f"cats:{page - 1}")
        nav_count += 1
    if page < total_pages - 1:
        kb.button(text="▶️", callback_data=f"cats:{page + 1}")
        nav_count += 1

    kb.button(text="🏠 Меню", callback_data="menu")
    rows = [1, *([1] * (end - start))]
    if nav_count:
        rows.append(nav_count)
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


def stock_nav_keyboard(page: int, total: int, mode: str, cat_idx: int = 0):
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    kb = InlineKeyboardBuilder()
    nav_count = 0
    if page > 0:
        cb = f"sp:{page - 1}" if mode == "all" else f"sc:{page - 1}:{cat_idx}"
        kb.button(text="◀️ Назад", callback_data=cb)
        nav_count += 1
    if page < total_pages - 1:
        cb = f"sp:{page + 1}" if mode == "all" else f"sc:{page + 1}:{cat_idx}"
        kb.button(text="Вперёд ▶️", callback_data=cb)
        nav_count += 1
    kb.button(text="🗂 Категории", callback_data="cats:0")
    kb.button(text="🏠 Меню", callback_data="menu")
    # Стрелки в одном ряду, затем «Категории» и «Меню» — каждая своей строкой.
    rows = []
    if nav_count:
        rows.append(nav_count)
    rows.extend([1, 1])
    kb.adjust(*rows)
    return kb.as_markup()


def _shipment_period_chips(kb: InlineKeyboardBuilder) -> None:
    kb.button(text="Сегодня", callback_data="sh:today")
    kb.button(text="Вчера", callback_data="sh:yesterday")
    kb.button(text="7д", callback_data="sh:7d")
    kb.button(text="30д", callback_data="sh:30d")
    kb.button(text="Месяц", callback_data="sh:month")


def shipments_nav_keyboard(page: int, total_pages: int):
    kb = InlineKeyboardBuilder()
    nav_count = 0
    if page > 0:
        kb.button(text="◀️ Назад", callback_data=f"shp:{page - 1}")
        nav_count += 1
    if page < total_pages - 1:
        kb.button(text="Вперёд ▶️", callback_data=f"shp:{page + 1}")
        nav_count += 1
    _shipment_period_chips(kb)
    kb.button(text="🏠 Меню", callback_data="menu")
    rows = []
    if nav_count:
        rows.append(nav_count)
    rows.extend([3, 2, 1])
    kb.adjust(*rows)
    return kb.as_markup()


def shipments_back_keyboard():
    """Чипы периода под результатом отгрузок (вместо отдельного экрана выбора)."""
    kb = InlineKeyboardBuilder()
    _shipment_period_chips(kb)
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(3, 2, 1)
    return kb.as_markup()
