"""
Хэндлеры: управление пользователями и ролями
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from config import ADMIN_IDS
from services.database import set_role, get_role, get_all_users, add_audit_log
from services.roles import can_manage_users

logger = logging.getLogger(__name__)
router = Router()

ROLE_NAMES = {
    "admin": "👑 Администратор",
    "boss": "🏆 Руководитель",
    "manager": "💼 Менеджер",
    "employee": "👤 Сотрудник",
}


# ─── Команды ─────────────────────────────────────────────────────────────────


@router.message(Command("addrole"))
async def cmd_addrole(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")

    parts = message.text.strip().split()
    if len(parts) != 3:
        return await message.answer(
            "❌ Формат: <code>/addrole [user_id] [роль]</code>\n\n"
            "Роли: <code>admin</code>, <code>boss</code>, "
            "<code>manager</code>, <code>employee</code>\n\n"
            "Пример: <code>/addrole 123456789 manager</code>",
            parse_mode="HTML",
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ User ID должен быть числом.")

    role = parts[2].lower()
    if role not in ("admin", "boss", "manager", "employee"):
        return await message.answer(
            "❌ Роль должна быть: admin, boss, manager или employee"
        )

    set_role(target_id, "", "", role)

    admin_name = message.from_user.full_name or str(message.from_user.id)
    admin_role = get_role(message.from_user.id)
    add_audit_log(
        message.from_user.id,
        admin_name,
        admin_role,
        "role_changed",
        f"Пользователю {target_id} назначена роль {role}",
    )

    role_name = ROLE_NAMES.get(role, role)
    await message.answer(
        f"✅ Пользователю <code>{target_id}</code> назначена роль <b>{role_name}</b>",
        parse_mode="HTML",
    )


@router.message(Command("users"))
async def cmd_users(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await show_users(message)


@router.callback_query(F.data == "users_list")
async def cb_users(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()
    await show_users(call.message)


async def show_users(message):
    users = get_all_users()
    if not users:
        return await message.answer("👥 Пользователей пока нет.")

    lines = [
        "<code>━━━━━━━━━━━━━━━━━━━━</code>",
        "👥 <b>Список пользователей:</b>\n",
    ]
    for u in users:
        role_name = ROLE_NAMES.get(u["role"], u["role"])
        name = u["full_name"] or u["username"] or str(u["user_id"])
        username = f" (@{u['username']})" if u["username"] else ""
        lines.append(
            f"{role_name}\n"
            f"  {name}{username}\n"
            f"  ID: <code>{u['user_id']}</code>\n"
        )

    lines.append("\n<i>Чтобы изменить роль:</i>")
    lines.append("<code>/addrole [ID] [admin/boss/manager/employee]</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")
