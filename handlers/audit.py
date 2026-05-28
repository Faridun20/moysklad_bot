"""
Хэндлеры: аудит лог действий пользователей
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.roles import can_manage_users
from services import async_db as adb

logger = logging.getLogger(__name__)
router = Router()

from utils.formatters import format_audit_entry
from utils.helpers import chunk_messages, filter_records_by_period

PERIOD_LABELS = {
    "today": "сегодня",
    "week": "эта неделя",
    "month": "этот месяц",
    "all": "всё время",
}


def format_audit_log(records: list[dict], label: str) -> list[str]:
    """Возвращает список сообщений (разбитых по TELEGRAM_MESSAGE_MAX).
    chunk_messages в utils.helpers — единая реализация склейки
    (раньше дублировалась в handlers/log.py:send_log)."""
    if not records:
        return [f"📋 Нет действий за {label}."]

    header = (
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📋 <b>Аудит лог · {label}</b>\n"
        f"<code>Записей: {len(records)}</code>\n"
    )
    lines = [header] + [format_audit_entry(r) for r in records]
    return chunk_messages(lines)


# Обратная совместимость: импортирующие `filter_by_period` из audit.py
# продолжат работать. Сама реализация теперь в utils.helpers.
filter_by_period = filter_records_by_period


def audit_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Сегодня", callback_data="al:today")
    kb.button(text="📅 Неделя", callback_data="al:week")
    kb.button(text="📅 Месяц", callback_data="al:month")
    kb.button(text="📋 Всё время", callback_data="al:all")
    kb.button(text="👤 По сотруднику", callback_data="al:by_user")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


# ─── Команды ─────────────────────────────────────────────────────────────────


async def _render_audit_period(target: Message, period: str):
    """Рисует записи за период + чипы переключения под ними (audit_keyboard)."""
    label = PERIOD_LABELS.get(period, period)
    records = await adb.get_audit_log(limit=200)
    records = filter_by_period(records, period)

    messages = format_audit_log(records, label)
    for i, msg in enumerate(messages):
        # Чипы крепим к последнему сообщению — без отдельного экрана выбора.
        kb = audit_keyboard() if i == len(messages) - 1 else None
        await target.answer(msg, parse_mode="HTML", reply_markup=kb)


@router.message(Command("audit"))
async def cmd_audit(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    # Сразу показываем сегодняшний лог; период переключается чипами под ним.
    await _render_audit_period(message, "today")


# ─── Callback ─────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("al:"))
async def cb_audit(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]

    if period == "by_user":
        # Показать список пользователей для выбора
        users = await adb.get_all_users()
        if not users:
            return await call.message.answer("👥 Пользователей нет.")
        kb = InlineKeyboardBuilder()
        for u in users[:20]:
            name = u["full_name"] or u["username"] or str(u["user_id"])
            kb.button(text=f"👤 {name}", callback_data=f"alu:{u['user_id']}")
        kb.button(text="🏠 Меню", callback_data="menu")
        kb.adjust(1)
        await call.message.answer("👤 Выберите сотрудника:", reply_markup=kb.as_markup())
        return

    await _render_audit_period(call.message, period)


@router.callback_query(F.data.startswith("alu:"))
async def cb_audit_user(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    user_id = int(call.data.split(":")[1])
    records = await adb.get_audit_log(limit=100, user_id=user_id)

    # Имя пользователя
    users = await adb.get_all_users()
    user = next((u for u in users if u["user_id"] == user_id), None)
    name = user["full_name"] if user else str(user_id)

    messages = format_audit_log(records, f"сотрудник {name}")
    for msg in messages:
        await call.message.answer(msg, parse_mode="HTML")

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 К списку", callback_data="al:by_user")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    await call.message.answer("Выберите действие:", reply_markup=kb.as_markup())
