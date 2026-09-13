"""
Клавиатуры бота.

T3.3: остались только отгрузки — каталог, аналитика и «что дальше» после
решения (next_actions_keyboard) вырезаны вместе со своими экранами; вход в
WebApp строит handlers._ui.webapp_keyboard.
"""

from aiogram.utils.keyboard import InlineKeyboardBuilder


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
