"""
Хэндлер: быстрый просмотр аудит лога прямо в боте
Команда /log — последние 20 действий
"""

import logging
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from services.roles import can_manage_users
from services.database import get_audit_log, get_all_users
from utils.formatters import DIV, DIV2

logger = logging.getLogger(__name__)
router = Router()

ACTION_EMOJI = {
    "user_added":        "🟢",
    "user_removed":      "🔴",
    "role_changed":      "🔄",
    "payment_sent":      "💵",
    "payment_confirmed": "✅",
    "payment_rejected":  "❌",
    "payment_archived":  "📦",
    "login":             "👤",
}


def format_log_entry(r: dict) -> str:
    from utils.helpers import esc
    emoji = ACTION_EMOJI.get(r["action"], "▪️")
    try:
        dt = datetime.strptime(r["created_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
    except Exception:
        dt = r["created_at"][:16]
    # full_name / role / details — пользовательский ввод, escape перед HTML
    role_str = f" [{esc(r['role'])}]" if r.get("role") else ""
    detail_str = f"\n    <i>{esc(r['details'])}</i>" if r.get("details") else ""
    return (
        f"{emoji} <code>{dt}</code>  <b>{esc(r['full_name'])}</b>"
        f"{role_str}{detail_str}"
    )


def log_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🕐 Последние 20",   callback_data="log:20")
    kb.button(text="📅 Сегодня",        callback_data="log:today")
    kb.button(text="📅 Неделя",         callback_data="log:week")
    kb.button(text="👤 По сотруднику",  callback_data="log:user")
    kb.button(text="💵 Только платежи", callback_data="log:payments")
    kb.button(text="🏠 Меню",           callback_data="menu")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


@router.message(Command("log"))
async def cmd_log(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer("📋 Что показать?", reply_markup=log_keyboard())


@router.callback_query(F.data.startswith("log:"))
async def cb_log(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    mode = call.data.split(":")[1]

    if mode == "user":
        users = get_all_users()
        if not users:
            return await call.message.answer("👥 Пользователей нет.")
        kb = InlineKeyboardBuilder()
        for u in users[:20]:
            name = u["full_name"] or u["username"] or str(u["user_id"])
            kb.button(text=f"👤 {name}", callback_data=f"logu:{u['user_id']}")
        kb.button(text="◀️ Назад", callback_data="log:menu")
        kb.adjust(1)
        return await call.message.answer("👤 Выберите сотрудника:", reply_markup=kb.as_markup())

    now = datetime.now()

    if mode == "20":
        records = get_audit_log(limit=20)
        label = "последние 20"
    elif mode == "today":
        records = get_audit_log(limit=200)
        cutoff = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        records = [r for r in records if r["created_at"] >= cutoff]
        label = "сегодня"
    elif mode == "week":
        records = get_audit_log(limit=500)
        cutoff = (now - timedelta(weeks=1)).strftime("%Y-%m-%d %H:%M:%S")
        records = [r for r in records if r["created_at"] >= cutoff]
        label = "эта неделя"
    elif mode == "payments":
        records = get_audit_log(limit=200)
        records = [r for r in records if "payment" in r["action"]]
        label = "только платежи"
    else:
        records = get_audit_log(limit=20)
        label = "последние 20"

    await send_log(call.message, records, label)


@router.callback_query(F.data.startswith("logu:"))
async def cb_log_user(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()

    user_id = int(call.data.split(":")[1])
    records = get_audit_log(limit=50, user_id=user_id)
    users = get_all_users()
    user = next((u for u in users if u["user_id"] == user_id), None)
    name = user["full_name"] if user else str(user_id)
    await send_log(call.message, records, f"сотрудник {name}")


@router.callback_query(F.data == "log:menu")
async def cb_log_menu(call: CallbackQuery):
    await call.answer()
    await call.message.answer("📋 Что показать?", reply_markup=log_keyboard())


async def send_log(message, records: list[dict], label: str):
    if not records:
        return await message.answer(
            f"{DIV}\n📋 <b>Лог · {label}</b>\n\n<i>Нет записей</i>",
            parse_mode="HTML",
        )

    lines = [
        DIV,
        f"📋 <b>Лог действий · {label}</b>",
        f"<code>Записей: {len(records)}</code>",
        "",
    ]

    for r in records:
        lines.append(format_log_entry(r))

    # Разбиваем если длинно
    full_text = "\n".join(lines)
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 4000:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)

    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML")

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Другой фильтр", callback_data="log:menu")
    kb.button(text="🏠 Меню",          callback_data="menu")
    kb.adjust(1)
    await message.answer("Выберите действие:", reply_markup=kb.as_markup())