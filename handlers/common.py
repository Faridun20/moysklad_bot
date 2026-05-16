"""
Общие хэндлеры: /start, меню
"""

from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery

from config import ALLOWED_USERS
from utils.keyboards import main_keyboard

router = Router()


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


@router.message(CommandStart())
async def cmd_start(message: Message):
    if not is_allowed(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer(
        "👋 Привет! Я бот МойСклад.\n\n"
        "Команды:\n"
        "/stock — остатки на складе\n"
        "/categories — категории товаров\n"
        "/shipments — отгрузки\n"
        "/analytics — аналитика продаж\n"
        "/pay — отправить платёж\n"
        "/payreport — отчёт по платежам",
        reply_markup=main_keyboard(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await call.answer()
    await call.message.answer("👋 Главное меню:", reply_markup=main_keyboard())
