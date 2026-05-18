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
from services.roles import is_boss, is_guest
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
    """
    Главное меню под роль. «Каталог» (Остатки + Категории) собран в
    одну кнопку, открывающую подменю — раньше эти два пункта занимали
    отдельный ряд. Заказы рядом с каталогом тематически.
    """
    kb = InlineKeyboardBuilder()

    # Каталог — одна кнопка, ведёт в shop_menu (там Остатки+Категории)
    if role in ("admin", "boss", "manager"):
        kb.button(text="🛒 Каталог", callback_data="shop_menu")

    # Заказы — самое частое действие, в один тап
    if role in ("admin", "boss", "manager"):
        kb.button(text="➕ Новый заказ", callback_data="ord_new")
        kb.button(text="📋 Мои заказы", callback_data="ord_my")

    # Отгрузки + Аналитика
    if role in ("admin", "boss", "manager"):
        kb.button(text="🚚 Отгрузки", callback_data="sh_period")
        kb.button(text="📊 Аналитика", callback_data="analytics")

    # Заявки на отгрузку — только босс/админ
    if role in ("admin", "boss"):
        kb.button(text="⏳ Заявки на апрув", callback_data="ord_requests")
        kb.button(text="📈 Отчёты", callback_data="reports_menu")

    # Платежи: отправляют только менеджеры (и админ для теста).
    if role in ("admin", "manager"):
        kb.button(text="💵 Отправить платёж", callback_data="pay_start")
    if role in ("admin", "boss"):
        kb.button(text="📊 Отчёт по платежам", callback_data="pr:menu")

    # Админская строка
    if role == "admin":
        kb.button(text="👥 Пользователи", callback_data="users_list")
        kb.button(text="📋 Аудит", callback_data="al:today")

    # WebApp кнопкой во всю ширину
    webapp_url = os.environ.get("WEBAPP_URL", "")
    has_webapp = bool(webapp_url)
    if has_webapp:
        kb.button(text="🌐 Открыть WebApp", web_app=WebAppInfo(url=webapp_url))

    # Раскладка
    rows: list[int] = []
    if role in ("admin", "boss", "manager"):
        rows += [1]      # Каталог во всю ширину (важная точка входа)
        rows += [2]      # Новый + Мои
        rows += [2]      # Отгрузки + Аналитика
    if role in ("admin", "boss"):
        rows += [2]      # Заявки + Отчёты
    if role == "admin":
        rows += [2]      # Платёж + Отчёт по платежам
    elif role == "boss":
        rows += [1]      # Только отчёт
    elif role == "manager":
        rows += [1]      # Только отправка
    if role == "admin":
        rows += [2]      # Пользователи + Аудит
    if has_webapp:
        rows += [1]
    kb.adjust(*rows)
    return kb.as_markup()


def shop_submenu_keyboard():
    """Подменю «Каталог» — Остатки + Категории + назад."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Все остатки", callback_data="sp:0")
    kb.button(text="🗂 По категориям", callback_data="cats:0")
    kb.button(text="◀️ Назад", callback_data="menu")
    kb.adjust(2, 1)
    return kb.as_markup()


def get_welcome_text(role: str, first_name: str = "") -> str:
    """
    Короткое приветствие — без длинного списка команд (он висит в
    автокомплите Telegram и дублирует кнопки ниже).
    """
    role_name = ROLE_NAMES.get(role, "👤 Сотрудник")
    name_part = f", <b>{first_name}</b>" if first_name else ""
    if role == "guest":
        return (
            f"👋 Здравствуйте{name_part}!\n"
            f"Аккаунт ещё не активирован."
        )
    return (
        f"👋 Привет{name_part}!\n"
        f"{role_name}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    ensure_user(user.id, user.username or "", user.full_name or "", ADMIN_IDS)
    role = get_role(user.id)

    # Гости — те, кого админ ещё не активировал. Не показываем им меню
    # каталога/заявок, только короткое сообщение со своим ID. Админ
    # повышает роль командой /addrole <id> manager.
    if role == "guest":
        return await message.answer(
            "👋 Здравствуйте!\n\n"
            "Ваш аккаунт ещё не активирован для работы с этим ботом.\n"
            f"Передайте свой ID администратору: <code>{user.id}</code>\n\n"
            "После активации напишите /start ещё раз.",
            parse_mode="HTML",
        )

    # Автоматически синхронизируем менеджеров с МойСклад.
    # Статус показываем коротко — только в случае проблем. Успех тихий,
    # чтобы не шуметь при каждом /start. Полный текст ошибки от МойСклад
    # уходит в лог через sync_manager.
    sync_status_line = ""
    if role == "manager":
        from services.ms_sync import sync_manager
        import html as _html
        result = await sync_manager(
            user.id,
            user.full_name or user.username or str(user.id),
            user.username or "",
        )
        status = result.get("status")
        if status == "created":
            sync_status_line = "\n\n✅ Создан профиль в МойСклад."
        elif status == "failed":
            reason = result.get("reason", "неизвестная ошибка")
            logger.warning(
                "MS link failed for %s: %s", user.full_name, reason
            )
            sync_status_line = (
                f"\n\n⚠️ <i>Не привязан к МойСклад: "
                f"{_html.escape(reason[:160])}</i>\n"
                f"<i>Передайте админу — аналитика по сотруднику "
                f"недоступна без привязки.</i>"
            )

    await message.answer(
        get_welcome_text(role, user.first_name or "") + sync_status_line,
        parse_mode="HTML",
        reply_markup=get_keyboard_for_role(role),
    )

    # Показываем сводку за месяц для менеджера
    if role == "manager":
        from handlers.analytics import show_manager_summary
        await show_manager_summary(message.bot, message.chat.id, message.from_user.id)


@router.message(Command("refresh"))
async def cmd_refresh(message: Message):
    """Принудительно перечитать snapshot МойСклад. Доступно только админу."""
    if message.from_user.id not in ADMIN_IDS and get_role(message.from_user.id) != "admin":
        return await message.answer("⛔ Только для администратора.")

    from services import snapshot
    await message.answer("⏳ Перечитываю snapshot МойСклад…")
    try:
        counts = await snapshot.refresh_all()
    except Exception as e:
        return await message.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")

    lines = ["✅ <b>Snapshot обновлён</b>", ""]
    for key, val in counts.items():
        lines.append(f"• {key}: <code>{val}</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("snapshot"))
async def cmd_snapshot_stats(message: Message):
    """Показать статистику snapshot — что и когда последний раз обновлялось."""
    if message.from_user.id not in ADMIN_IDS and get_role(message.from_user.id) != "admin":
        return await message.answer("⛔ Только для администратора.")
    from services import snapshot
    stats = snapshot.stats()
    lines = ["📊 <b>Snapshot МойСклад</b>", ""]
    lines.append("<b>Строк в локальных таблицах:</b>")
    for tbl in ("ms_products", "ms_categories", "ms_counterparties",
                "ms_employees", "ms_stock"):
        lines.append(f"  • {tbl}: <code>{stats.get(tbl, 0)}</code>")
    lines.append("")
    lines.append("<b>Метаданные:</b>")
    for m in stats.get("meta", []):
        last = m.get("last_full_refresh") or m.get("last_refresh") or "—"
        lines.append(f"  • {m['dataset']}: {last} ({m.get('rows_count', 0)} rows)")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    user = call.from_user
    ensure_user(user.id, user.username or "", user.full_name or "", ADMIN_IDS)
    role = get_role(user.id)
    await call.answer()
    if role == "guest":
        return await call.message.answer(
            "⛔ Ваш аккаунт ещё не активирован. Напишите /start."
        )
    await call.message.answer(
        get_welcome_text(role, user.first_name or ""),
        parse_mode="HTML",
        reply_markup=get_keyboard_for_role(role),
    )

@router.callback_query(F.data == "shop_menu")
async def cb_shop_menu(call: CallbackQuery):
    if get_role(call.from_user.id) not in ("admin", "boss", "manager"):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    await call.message.answer(
        "🛒 <b>Каталог склада</b>\n\nЧто хотите посмотреть?",
        parse_mode="HTML",
        reply_markup=shop_submenu_keyboard(),
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
