"""
Клавиатуры бота.

T3.3: остались только отгрузки — каталог, аналитика и «что дальше» после
решения (next_actions_keyboard) вырезаны вместе со своими экранами; вход в
WebApp строит handlers._ui.webapp_keyboard.

Bot API 10.3: неактивные кнопки и force_reply у inline-клавиатуры — сборщики
ниже (`disabled_button`, `status_keyboard`, `settle_markup`,
`prompt_keyboard`). Правила применения — handlers/_ui.py и CLAUDE.md.
"""

from aiogram.types import DisabledButton, InlineKeyboardButton, InlineKeyboardMarkup
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


# ─── Bot API 10.3: неактивные кнопки и force_reply ───────────────────────────

# Telegram обрезает длинную подпись кнопки многоточием по ширине экрана;
# исход длиннее этого не читается на телефоне целиком, поэтому режем сами —
# по имени, а не посередине времени.
_BUTTON_TEXT_MAX = 48


def disabled_button(text: str) -> InlineKeyboardButton:
    """Неактивная кнопка (Bot API 10.3): подпись без действия.

    Годится для исхода («✅ Принято · Фаридун 14:05») и для шага, который
    пока недоступен, с причиной («🚚 Отгрузка — после ввода оплаты»). Колбэка у
    неё нет, поэтому сторож висячих кнопок (`test_bot_trimmed`) её не видит.
    """
    text = (text or "").strip() or "—"
    if len(text) > _BUTTON_TEXT_MAX:
        text = text[: _BUTTON_TEXT_MAX - 1].rstrip() + "…"
    return InlineKeyboardButton(text=text, disabled=DisabledButton())


def status_keyboard(
    *labels: str, tail: InlineKeyboardMarkup | None = None
) -> InlineKeyboardMarkup:
    """Строки неактивных кнопок-статусов, под ними — `tail` (WebApp, «Меню»)."""
    rows = [[disabled_button(label)] for label in labels if label]
    if tail is not None:
        rows.extend(tail.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_decision(button: InlineKeyboardButton) -> bool:
    """Кнопка-действие карточки: с колбэком и не «🏠 Меню»."""
    return bool(button.callback_data) and button.callback_data != "menu"


def settle_markup(
    markup: InlineKeyboardMarkup | None,
    callbacks: set[str] | frozenset[str],
    label: str,
    *,
    tail: InlineKeyboardMarkup | None = None,
) -> InlineKeyboardMarkup:
    """Заменить в клавиатуре карточки строку(и) с `callbacks` на исход `label`.

    Остальные строки остаются как были: в пачке платежей из WebApp под одной
    карточкой по строке на платёж, и решение по одному не должно гасить
    кнопки соседей (раньше гасило — клавиатура менялась целиком). Когда
    живых кнопок решения не осталось, под исходом встаёт `tail` («что дальше»
    — WebApp). Нет исходной клавиатуры (фейк в тесте, сообщение без неё) —
    строится с нуля: исход + `tail`.
    """
    rows: list[list[InlineKeyboardButton]] = []
    placed = False
    for row in (markup.inline_keyboard if markup is not None else []):
        if any(b.callback_data in callbacks for b in row):
            if not placed:
                rows.append([disabled_button(label)])
                placed = True
            continue
        rows.append(list(row))
    if not placed:
        rows.insert(0, [disabled_button(label)])
    alive = any(_is_decision(b) for row in rows for b in row)
    has_webapp = any(b.web_app for row in rows for b in row)
    if tail is not None and not alive and not has_webapp:
        rows.extend(tail.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def prompt_keyboard(*buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
    """Клавиатура вопроса «напишите одним сообщением» (Bot API 10.3).

    `force_reply=True` у INLINE-клавиатуры: Telegram сразу открывает ответ на
    этот вопрос (поле ввода с цитатой), а кнопка «Отмена» под вопросом
    остаётся. До 10.3 было либо одно, либо другое (`ForceReply` — это
    отдельный вид reply_markup), и человек после нажатия «На доработку»
    смотрел на чат, не понимая, что бот ждёт текст.
    """
    return InlineKeyboardMarkup(inline_keyboard=[[b] for b in buttons], force_reply=True)
