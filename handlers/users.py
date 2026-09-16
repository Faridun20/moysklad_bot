"""
Хэндлеры: управление пользователями и ролями
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery

from services.roles import can_manage_users, invalidate_role
from services import async_db as adb
from utils.helpers import esc

logger = logging.getLogger(__name__)
router = Router()

ROLE_NAMES = {
    "admin": "👑 Администратор",
    "boss": "🏆 Руководитель",
    "manager": "💼 Менеджер",
    "warehouse_keeper": "📦 Кладовщик",
    "bookkeeper": "🧮 Бухгалтер",
    "employee": "👤 Сотрудник",
    "guest": "🚫 Гость (без прав)",
}


# ─── Команды ─────────────────────────────────────────────────────────────────


@router.message(Command("addrole"))
async def cmd_addrole(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Роли меняет администратор.")

    parts = message.text.strip().split()
    if len(parts) != 3:
        return await message.answer(
            "❌ Напишите номер сотрудника и роль: <code>/addrole [ID] [роль]</code>\n\n"
            "Роли: <code>admin</code>, <code>boss</code>, "
            "<code>manager</code>, <code>guest</code>\n\n"
            "Например: <code>/addrole 123456789 manager</code>",
            parse_mode="HTML",
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ Номер сотрудника должен быть числом.")

    # Whitelist — единый источник из services.database (через roles).
    # Раньше тут была своя копия, рассинхрон с set_role давал silent-fail.
    role = parts[2].lower()
    from services.roles import ASSIGNABLE_ROLES, PAUSED_ROLES

    # Кладовщик и бухгалтер пока не назначаются: их права у менеджера
    # (services.roles.ROLE_ALSO_ACTS_AS). Уже назначенные продолжают работать.
    if role in PAUSED_ROLES:
        return await message.answer(
            f"❌ Роль {ROLE_NAMES.get(role, role)} сейчас не назначается — "
            "её работу выполняет менеджер. Назначьте <code>manager</code>.",
            parse_mode="HTML",
        )
    if role not in ASSIGNABLE_ROLES:
        return await message.answer(f"❌ Роль должна быть одной из: {', '.join(ASSIGNABLE_ROLES)}")

    ok = await adb.set_role(target_id, "", "", role)
    if not ok:
        # Сюда можно попасть если БД отвалилась — set_role вернул False.
        return await message.answer(
            "❌ Роль не сохранилась: база данных не ответила. "
            "Повторите через минуту, а если не поможет — смотрите логи бота."
        )
    invalidate_role(target_id)

    admin_name = message.from_user.full_name or str(message.from_user.id)
    admin_role = await adb.get_role(message.from_user.id)
    await adb.add_audit_log(
        message.from_user.id,
        admin_name,
        admin_role,
        "role_changed",
        f"Сотруднику {target_id} назначена роль {role}",
    )

    role_name = ROLE_NAMES.get(role, role)
    text = f"✅ Сотруднику <code>{target_id}</code> назначена роль <b>{role_name}</b>"
    # T2.13 (§2.8): set_role не снимает deactivated_at, а get_role у
    # деактивированного отдаёт guest. Без этой строки админ видел «роль
    # назначена», человек не мог ничего сделать, и причина нигде не всплывала.
    if await adb.is_user_deactivated(target_id):
        text += (
            "\n\n⚠️ <b>Доступ этому сотруднику закрыт</b> — роль пока не действует, "
            "прав у него нет.\n"
            f"Верните доступ: <code>/reactivate {target_id}</code>"
        )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("users"))
async def cmd_users(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Список сотрудников открыт администратору.")
    await show_users(message)


@router.callback_query(F.data == "users_list")
async def cb_users(call: CallbackQuery):
    if not can_manage_users(call.from_user.id):
        return await call.answer("⛔ Список сотрудников открыт администратору", show_alert=True)
    await call.answer()
    await show_users(call.message)


async def show_users(message):
    users = await adb.get_all_users()
    if not users:
        return await message.answer("👥 Сотрудников пока нет.")

    lines = [
        "<code>━━━━━━━━━━━━━━━━━━━━</code>",
        "👥 <b>Сотрудники:</b>\n",
    ]
    for u in users:
        role_name = ROLE_NAMES.get(u["role"], u["role"])
        name = u["full_name"] or u["username"] or str(u["user_id"])
        username = f" (@{u['username']})" if u["username"] else ""
        flag = "  🚫 <b>доступ закрыт</b>" if u.get("deactivated_at") else ""
        lines.append(
            f"{role_name}\n  {esc(name)}{esc(username)}\n  ID: <code>{u['user_id']}</code>{flag}\n"
        )

    # Подсказка — из того же списка, что проверяет /addrole (была «employee»,
    # которой нет среди ролей).
    from services.roles import ASSIGNABLE_ROLES

    lines.append(f"\n<i>Сменить роль:</i> <code>/addrole [ID] [{'/'.join(ASSIGNABLE_ROLES)}]</code>")
    lines.append(
        "<i>Закрыть доступ:</i> <code>/deactivate [ID]</code> · "
        "<i>вернуть:</i> <code>/reactivate [ID]</code>"
    )
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("deactivate"))
async def cmd_deactivate(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Доступом сотрудников управляет администратор.")
    parts = message.text.strip().split()
    if len(parts) != 2:
        return await message.answer(
            "❌ Напишите номер сотрудника: <code>/deactivate [ID]</code>", parse_mode="HTML"
        )
    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ Номер сотрудника должен быть числом.")
    if target_id == message.from_user.id:
        return await message.answer("❌ Себе закрыть доступ нельзя.")

    ok = await adb.deactivate_user(target_id, message.from_user.id)
    invalidate_role(target_id)
    if not ok:
        return await message.answer(
            "ℹ️ У этого сотрудника доступ уже закрыт — либо такого номера нет в системе."
        )
    admin_name = message.from_user.full_name or str(message.from_user.id)
    await adb.add_audit_log(
        message.from_user.id,
        admin_name,
        await adb.get_role(message.from_user.id),
        "user_deactivated",
        f"Сотруднику {target_id} закрыт доступ",
    )
    await message.answer(
        f"🚫 Сотруднику <code>{target_id}</code> закрыт доступ — все права сняты.",
        parse_mode="HTML",
    )


@router.message(Command("reactivate"))
async def cmd_reactivate(message: Message):
    if not can_manage_users(message.from_user.id):
        return await message.answer("⛔ Доступом сотрудников управляет администратор.")
    parts = message.text.strip().split()
    if len(parts) != 2:
        return await message.answer(
            "❌ Напишите номер сотрудника: <code>/reactivate [ID]</code>", parse_mode="HTML"
        )
    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ Номер сотрудника должен быть числом.")

    ok = await adb.reactivate_user(target_id, message.from_user.id)
    invalidate_role(target_id)
    if not ok:
        return await message.answer("ℹ️ У этого сотрудника доступ и так открыт.")
    admin_name = message.from_user.full_name or str(message.from_user.id)
    await adb.add_audit_log(
        message.from_user.id,
        admin_name,
        await adb.get_role(message.from_user.id),
        "user_reactivated",
        f"Сотруднику {target_id} возвращён доступ",
    )
    await message.answer(
        f"✅ Сотруднику <code>{target_id}</code> возвращён доступ.", parse_mode="HTML"
    )


