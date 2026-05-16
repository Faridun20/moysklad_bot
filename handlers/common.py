"""
Общие хэндлеры: /start, меню, управление ролями
"""

import logging
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS
from services.database import get_role, set_role, get_all_users, ensure_user

logger = logging.getLogger(__name__)
router = Router()

ROLE_NAMES = {
    "admin":    "👑 Администратор",
    "boss":     "🏆 Руководитель",
    "manager":  "💼 Менеджер",
    "employee": "👤 Сотрудник",
}


def get_keyboard_for_role(role: str):
    kb = InlineKeyboardBuilder()

    if role in ("admin", "boss", "manager"):
        kb.button(text="📦 Все остатки",       callback_data="sp:0")
        kb.button(text="🗂 По категориям",     callback_data="cats:0")
        kb.button(text="🚚 Отгрузки",         callback_data="sh_period")
        kb.button(text="📊 Аналитика продаж", callback_data="analytics")

    if role in ("admin", "boss", "employee"):
        kb.button(text="💵 Отправить платёж", callback_data="pay_start")

    if role in ("admin", "boss"):
        kb.button(text="📋 Отчёт по платежам", callback_data="pr:menu")

    if role == "admin":
        kb.button(text="👥 Пользователи", callback_data="users_list")

    kb.adjust(1)
    return kb.as_markup()


def get_welcome_text(role: str) -> str:
    role_name = ROLE_NAMES.get(role, "👤 Сотрудник")
    base = f"👋 Привет! Я бот МойСклад.\n🎭 Ваша роль: <b>{role_name}</b>\n\n"

    if role == "admin":
        return base + (
            "Доступные команды:\n"
            "/stock — остатки на складе\n"
            "/categories — категории товаров\n"
            "/shipments — отгрузки\n"
            "/analytics — аналитика продаж\n"
            "/pay — отправить платёж\n"
            "/payreport — отчёт по платежам\n"
            "/users — список пользователей\n"
            "/addrole [id] [роль] — назначить роль"
        )
    elif role == "boss":
        return base + (
            "Доступные команды:\n"
            "/stock — остатки на складе\n"
            "/categories — категории товаров\n"
            "/shipments — отгрузки\n"
            "/analytics — аналитика продаж\n"
            "/pay — отправить платёж\n"
            "/payreport — отчёт по платежам"
        )
    elif role == "manager":
        return base + (
            "Доступные команды:\n"
            "/stock — остатки на складе\n"
            "/categories — категории товаров\n"
            "/shipments — отгрузки\n"
            "/analytics — аналитика продаж"
        )
    else:
        return base + (
            "Доступные команды:\n"
            "/pay — отправить платёж руководителю"
        )


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    ensure_user(user.id, user.username or "", user.full_name or "", ADMIN_IDS)
    role = get_role(user.id)
    await message.answer(
        get_welcome_text(role),
        parse_mode="HTML",
        reply_markup=get_keyboard_for_role(role),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    user = call.from_user
    ensure_user(user.id, user.username or "", user.full_name or "", ADMIN_IDS)
    role = get_role(user.id)
    await call.answer()
    await call.message.answer(
        get_welcome_text(role),
        parse_mode="HTML",
        reply_markup=get_keyboard_for_role(role),
    )


# ─── Управление ролями (только для админов) ───────────────────────────────────

@router.message(Command("addrole"))
async def cmd_addrole(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("⛔ Нет доступа.")

    parts = message.text.strip().split()
    if len(parts) != 3:
        return await message.answer(
            "❌ Формат: <code>/addrole [user_id] [роль]</code>\n\n"
            "Роли: <code>admin</code>, <code>manager</code>, <code>employee</code>\n\n"
            "Пример: <code>/addrole 123456789 manager</code>",
            parse_mode="HTML"
        )

    try:
        target_id = int(parts[1])
    except ValueError:
        return await message.answer("❌ User ID должен быть числом.")

    role = parts[2].lower()
    if role not in ("admin", "boss", "manager", "employee"):
        return await message.answer("❌ Роль должна быть: admin, boss, manager или employee")

    set_role(target_id, "", "", role)
    role_name = ROLE_NAMES.get(role, role)
    await message.answer(
        f"✅ Пользователю <code>{target_id}</code> назначена роль <b>{role_name}</b>",
        parse_mode="HTML"
    )


@router.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("⛔ Нет доступа.")
    await show_users(message)


@router.callback_query(F.data == "users_list")
async def cb_users(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        return await call.answer("⛔ Нет доступа", show_alert=True)
    await call.answer()
    await show_users(call.message)


async def show_users(message):
    users = get_all_users()
    if not users:
        return await message.answer("👥 Пользователей пока нет.")

    lines = ["<code>━━━━━━━━━━━━━━━━━━━━</code>", "👥 <b>Список пользователей:</b>\n"]
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
    lines.append("<code>/addrole [ID] [admin/manager/employee]</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")
