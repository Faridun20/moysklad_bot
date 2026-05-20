"""
Хэндлеры: управление пользователями и ролями
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from services.roles import can_manage_users, invalidate_role
from services import async_db as adb

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
            "<code>manager</code>, <code>guest</code>\n\n"
            "Пример: <code>/addrole 123456789 manager</code>",
            parse_mode="HTML",
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ User ID должен быть числом.")

    # Whitelist должен совпадать с services.database.set_role.
    # Раньше тут принимали 'employee', но set_role его молча отвергал,
    # и юзер оставался в прежней роли при «✅ назначено»-сообщении.
    role = parts[2].lower()
    VALID_ROLES = ("admin", "boss", "manager", "guest")
    if role not in VALID_ROLES:
        return await message.answer(f"❌ Роль должна быть одной из: {', '.join(VALID_ROLES)}")

    ok = await adb.set_role(target_id, "", "", role)
    if not ok:
        # Сюда можно попасть если БД отвалилась — set_role вернул False.
        return await message.answer("❌ Не удалось назначить роль. Проверьте логи бота.")
    invalidate_role(target_id)

    admin_name = message.from_user.full_name or str(message.from_user.id)
    admin_role = await adb.get_role(message.from_user.id)
    await adb.add_audit_log(
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
    users = await adb.get_all_users()
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
        lines.append(f"{role_name}\n  {name}{username}\n  ID: <code>{u['user_id']}</code>\n")

    lines.append("\n<i>Чтобы изменить роль:</i>")
    lines.append("<code>/addrole [ID] [admin/boss/manager/employee]</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("syncms"))
async def cmd_syncms(message: Message):
    """Ручная синхронизация всех менеджеров с МойСклад."""
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")

    await message.answer("⏳ Синхронизирую менеджеров с МойСклад…")

    from services.ms_sync import sync_all_managers

    users = await adb.get_all_users()
    results = await sync_all_managers(users)

    await message.answer(
        f"<code>━━━━━━━━━━━━━━━━━━━━</code>\n"
        f"✅ <b>Синхронизация завершена</b>\n\n"
        f"🔗 Привязано: <b>{results['linked']}</b>\n"
        f"🆕 Создано в МойСклад: <b>{results['created']}</b>\n"
        f"❌ Ошибок: <b>{results['failed']}</b>",
        parse_mode="HTML",
    )


@router.message(Command("msstaff"))
async def cmd_msstaff(message: Message):
    """Показать список сотрудников МойСклад с их ID."""
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")

    await message.answer("⏳ Загружаю сотрудников МойСклад…")

    from services.ms_sync import get_ms_employees

    try:
        employees = await get_ms_employees()
        if not employees:
            return await message.answer("👥 Сотрудников не найдено.")

        lines = [
            "<code>━━━━━━━━━━━━━━━━━━━━</code>",
            "👥 <b>Сотрудники МойСклад:</b>\n",
        ]
        for emp in employees[:20]:
            name = emp.get("name", "—")
            uid = emp.get("id", "—")
            lines.append(f"• <b>{name}</b>\n  <code>{uid}</code>")

        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
