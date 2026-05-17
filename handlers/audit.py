"""
Хэндлеры: аудит лог действий пользователей
"""

import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from utils.roles import can_manage_users
from services.database import get_audit_log, get_all_users

logger = logging.getLogger(__name__)
router = Router()

ACTION_EMOJI = {
    "user_added": "🟢",
    "user_removed": "🔴",
    "role_changed": "🔄",
    "payment_sent": "💵",
    "payment_confirmed": "✅",
    "payment_rejected": "❌",
    "login": "👤",
}

PERIOD_LABELS = {
    "today": "сегодня",
    "week": "эта неделя",
    "month": "этот месяц",
    "all": "всё время",
}


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


def format_audit_log(records: list[dict], label: str) -> list[str]:
    """
    Возвращает список сообщений (разбитых по 4096 символов).
    """
    if not records:
        return [f"📋 Нет действий за {label}."]

    header = (
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"📋 <b>Аудит лог · {label}</b>\n"
        f"<code>Записей: {len(records)}</code>\n"
    )

    lines = [header]
    for r in records:
        emoji = ACTION_EMOJI.get(r["action"], "▪️")
        dt = r["created_at"][:16]
        try:
            dt = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").strftime(
                "%d.%m %H:%M"
            )
        except Exception:
            pass

        role_str = f" [{r['role']}]" if r.get("role") else ""
        details_str = f"\n     <i>{r['details']}</i>" if r.get("details") else ""

        lines.append(
            f"{emoji} <code>{dt}</code>  <b>{r['full_name']}</b>{role_str}"
            f"{details_str}"
        )

    # Разбиваем на части по 4000 символов
    messages = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 4000:
            messages.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        messages.append(current)

    return messages


def filter_by_period(records: list[dict], period: str) -> list[dict]:
    now = datetime.now()
    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        cutoff = now - timedelta(weeks=1)
    elif period == "month":
        cutoff = now - timedelta(days=30)
    else:
        return records

    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    return [r for r in records if r["created_at"] >= cutoff_str]


# ─── Команды ─────────────────────────────────────────────────────────────────


@router.message(Command("audit"))
async def cmd_audit(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer(
        "📋 За какой период показать лог?", reply_markup=audit_keyboard()
    )


# ─── Callback ─────────────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("al:"))
async def cb_audit(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    period = call.data.split(":")[1]

    if period == "by_user":
        # Показать список пользователей для выбора
        users = get_all_users()
        if not users:
            return await call.message.answer("👥 Пользователей нет.")
        kb = InlineKeyboardBuilder()
        for u in users[:20]:
            name = u["full_name"] or u["username"] or str(u["user_id"])
            kb.button(text=f"👤 {name}", callback_data=f"alu:{u['user_id']}")
        kb.button(text="🏠 Меню", callback_data="menu")
        kb.adjust(1)
        await call.message.answer(
            "👤 Выберите сотрудника:", reply_markup=kb.as_markup()
        )
        return

    label = PERIOD_LABELS.get(period, period)
    records = get_audit_log(limit=200)
    records = filter_by_period(records, period)

    messages = format_audit_log(records, label)
    for msg in messages:
        await call.message.answer(msg, parse_mode="HTML")

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Другой период", callback_data="audit_menu")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    await call.message.answer("Выберите действие:", reply_markup=kb.as_markup())


@router.callback_query(F.data == "audit_menu")
async def cb_audit_menu(call: CallbackQuery):
    await call.answer()
    await call.message.answer(
        "📋 За какой период показать лог?", reply_markup=audit_keyboard()
    )


@router.callback_query(F.data.startswith("alu:"))
async def cb_audit_user(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    user_id = int(call.data.split(":")[1])
    records = get_audit_log(limit=100, user_id=user_id)

    # Имя пользователя
    users = get_all_users()
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
