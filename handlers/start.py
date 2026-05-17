"""
Общие хэндлеры: /start, меню, управление ролями
"""
import os
import logging
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import WebAppInfo   
from config import ADMIN_IDS
from services.database import (
    add_audit_log,
    get_role,
    ensure_user,
)

logger = logging.getLogger(__name__)
router = Router()

ROLE_NAMES = {
    "admin": "👑 Администратор",
    "boss": "🏆 Руководитель",
    "manager": "💼 Менеджер",
    "employee": "👤 Сотрудник",
}


def get_keyboard_for_role(role: str):
    kb = InlineKeyboardBuilder()

    webapp_url = os.environ.get("WEBAPP_URL", "")
    if webapp_url:
        kb.button(text="🌐 Открыть WebApp", web_app=WebAppInfo(url=webapp_url))

    if role in ("admin", "boss", "manager"):
        kb.button(text="📦 Все остатки", callback_data="sp:0")
        kb.button(text="🗂 По категориям", callback_data="cats:0")
        kb.button(text="🚚 Отгрузки", callback_data="sh_period")
        kb.button(text="📊 Аналитика продаж", callback_data="analytics")

    if role in ("admin", "boss", "employee"):
        kb.button(text="💵 Отправить платёж", callback_data="pay_start")

    if role in ("admin", "boss"):
        kb.button(text="📋 Отчёт по платежам", callback_data="pr:menu")

    if role in ("admin", "boss"):
        kb.button(text="📊 Отчёты", callback_data="reports_menu")

    if role == "admin":
        kb.button(text="👥 Пользователи", callback_data="users_list")
        kb.button(text="📋 Аудит лог", callback_data="al:today")

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
        return base + ("Доступные команды:\n" "/pay — отправить платёж руководителю")


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


