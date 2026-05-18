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
from services.roles import is_boss
from utils.formatters import DIV   
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

    if role in ("admin", "boss", "manager"):
        kb.button(text="💵 Отправить платёж", callback_data="pay_start")
        kb.button(text="📋 Мои заказы",       callback_data="ord_my")
        kb.button(text="➕ Новый заказ",       callback_data="ord_new")

    if role in ("admin", "boss"):
        kb.button(text="📋 Отчёт по платежам", callback_data="pr:menu")
        kb.button(text="📊 Отчёты", callback_data="reports_menu")
        kb.button(text="⏳ Заявки на отгрузку", callback_data="ord_requests")

    if role == "admin":
        kb.button(text="👥 Пользователи",  callback_data="users_list")
        kb.button(text="📋 Аудит лог",     callback_data="al:today")
        kb.button(text="🔍 Быстрый лог",   callback_data="log:20")

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

    # Автоматически синхронизируем менеджеров с МойСклад
    if role == "manager":
        from services.ms_sync import sync_manager
        result = await sync_manager(
            user.id,
            user.full_name or user.username or str(user.id),
            user.username or "",
        )
        if result["status"] == "linked":
            logger.info(
                "Менеджер %s привязан к МойСклад: %s",
                user.full_name, result["ms_id"]
            )
        elif result["status"] == "created":
            logger.info(
                "Создан сотрудник МойСклад для %s: %s",
                user.full_name, result["ms_id"]
            )

    await message.answer(
        get_welcome_text(role),
        parse_mode="HTML",
        reply_markup=get_keyboard_for_role(role),
    )

    # Показываем сводку за месяц для менеджера
    if role == "manager":
        from handlers.analytics import show_manager_summary
        await show_manager_summary(message.bot, message.chat.id, message.from_user.id)


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

@router.callback_query(F.data == "ord_my")
async def cb_ord_my(call: CallbackQuery):
    await call.answer()
    from services.database import get_user_orders
    from handlers.orders import my_orders_keyboard
    orders = get_user_orders(call.from_user.id)
    if not orders:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Создать заказ", callback_data="ord_new")
        kb.button(text="🏠 Меню",          callback_data="menu")
        kb.adjust(1)
        return await call.message.answer("📋 У вас пока нет заказов.", reply_markup=kb.as_markup())
    await call.message.answer(
        f"📋 <b>Мои заказы</b> ({len(orders)}):",
        parse_mode="HTML",
        reply_markup=my_orders_keyboard(orders),
    )


@router.callback_query(F.data == "ord_requests")
async def cb_ord_requests(call: CallbackQuery):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    from services.database import get_pending_requests
    from handlers.orders import pending_requests_keyboard
    requests = get_pending_requests()
    if not requests:
        return await call.message.answer(
            f"{DIV}\n⏳ <b>Заявки на отгрузку</b>\n\n<i>Нет новых заявок</i>",
            parse_mode="HTML",
        )
    await call.message.answer(
        f"⏳ <b>Заявки ({len(requests)}):</b>",
        parse_mode="HTML",
        reply_markup=pending_requests_keyboard(requests),
    )
