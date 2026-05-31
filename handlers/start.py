"""
Общие хэндлеры: /start, меню, управление ролями
"""

import asyncio
import logging
from aiogram import Bot, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardRemove,
    BotCommand,
    BotCommandScopeChat,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from services.roles import is_boss
from utils.formatters import DIV
from utils.helpers import user_safe_error
from config import ADMIN_IDS, WEBAPP_URL
from services.database import (
    get_role,
    ensure_user,
)

logger = logging.getLogger(__name__)
router = Router()

ROLE_NAMES = {
    "admin": "👑 Администратор",
    "boss": "🏆 Руководитель",
    "manager": "💼 Менеджер",
    "warehouse_keeper": "📦 Кладовщик",
    "bookkeeper": "🧮 Бухгалтер",
    "employee": "👤 Сотрудник",
}


def get_keyboard_for_role(role: str):
    """
    Сокращённое inline-меню — только то, что неудобно делать через WebApp:
    срочные действия (Заявки на апрув для босса) и admin-only функции
    (Пользователи, Аудит). Каталог / Новый заказ / Аналитика / Платежи
    живут в WebApp — туда ведёт Menu Button слева от поля ввода
    (set_chat_menu_button в bot.py).

    Раньше тут было 7-9 кнопок которые дублировали WebApp. Это
    путало пользователя: где «правильно» создавать заказ — в чате
    или в WebApp? Теперь чёткое разделение.
    """
    kb = InlineKeyboardBuilder()
    rows: list[int] = []

    # Основной блок для работающих ролей: каталог, заказы/долги и
    # просмотры (отгрузки/аналитика) — все потоки теперь открываются
    # одним тапом (период/фильтр выбирается чипами уже внутри).
    if role in ("admin", "boss", "manager"):
        kb.button(text="📦 Остатки", callback_data="sp:0")
        kb.button(text="🗂 Категории", callback_data="cats:0")
        rows += [2]
        kb.button(text="📋 Мои заказы", callback_data="ord_my")
        kb.button(text="💳 Долги", callback_data="debts_my")
        rows += [2]
        kb.button(text="🚚 Отгрузки", callback_data="sh:today")
        kb.button(text="📊 Аналитика", callback_data="analytics")
        rows += [2]

    # Срочное для босса/админа — апрув заявок (push-driven но дублируем
    # сюда чтобы можно было пересмотреть «все pending» одним тапом).
    if role in ("admin", "boss"):
        kb.button(text="⏳ Заявки на апрув", callback_data="ord_requests")
        rows += [1]

    # Менеджер: быстрая отправка платежа в кассу (не привязанного к заказу).
    if role == "manager":
        kb.button(text="💵 Отправить платёж", callback_data="pay_start")
        rows += [1]

    # Кладовщик: очередь возвратов на подтверждение (раньше — только из push).
    if role in ("admin", "boss", "warehouse_keeper"):
        kb.button(text="↩️ Возвраты на подтверждении", callback_data="ret_pending")
        rows += [1]

    # Бухгалтер: очередь сдач налички на подтверждение.
    if role in ("admin", "boss", "bookkeeper"):
        kb.button(text="💵 Сдачи на подтверждении", callback_data="dep_pending")
        rows += [1]

    # Админский ряд — управление пользователями и аудит.
    if role == "admin":
        kb.button(text="👥 Пользователи", callback_data="users_list")
        kb.button(text="📋 Аудит", callback_data="al:today")
        rows += [2]

    if not rows:
        # Guest или unknown role — пустой markup
        return None
    kb.adjust(*rows)
    return kb.as_markup()


# Reply Keyboard убран по запросу пользователя (PR #47). Дублировал
# Menu Button (слева от поля ввода) и занимал место снизу. WebApp
# теперь только через Menu Button.
#
# При первом /start после обновления юзерам шлём ReplyKeyboardRemove,
# чтобы у тех кто видел persistent-кнопку она исчезла. После одного
# /start клиент Telegram кэширует «нет reply-keyboard» и больше не
# показывает.


# Команды для /-автокомплита Telegram. Разные наборы для разных ролей —
# менеджеру не показываем admin-only /addrole, /audit и т.п.
_COMMANDS_MANAGER = [
    BotCommand(command="start", description="🏠 Главное меню"),
    BotCommand(command="neworder", description="➕ Новый заказ"),
    BotCommand(command="myorders", description="📋 Мои заказы"),
    BotCommand(command="stock", description="📦 Остатки на складе"),
    BotCommand(command="categories", description="🗂 Товары по категориям"),
    BotCommand(command="pay", description="💵 Отправить платёж"),
    BotCommand(command="debts", description="💳 Мои долги"),
    BotCommand(command="deposit", description="💵 Сдать наличные в кассу"),
    BotCommand(command="return", description="↩️ Оформить возврат"),
    BotCommand(command="find", description="🔍 Поиск (заказ/платёж/клиент)"),
]
_COMMANDS_BOSS = _COMMANDS_MANAGER + [
    BotCommand(command="orders", description="⏳ Заявки на апрув"),
    BotCommand(command="ship", description="🚚 Отгрузить заказ"),
    BotCommand(command="shipments", description="🚚 Последние отгрузки"),
    BotCommand(command="analytics", description="📊 Аналитика продаж"),
    BotCommand(command="cashbox", description="💰 Касса и дебиторка"),
    BotCommand(command="reports", description="📈 Отчёты"),
    BotCommand(command="cancel", description="🚫 Отменить заказ"),
    BotCommand(command="limit", description="📊 Кредитные лимиты"),
    BotCommand(command="rates", description="💱 Курсы валют"),
    BotCommand(command="prices", description="🏷 Цены товаров"),
    BotCommand(command="sync_payments", description="🔄 Статус синка с МойСклад"),
]
_COMMANDS_ADMIN = _COMMANDS_BOSS + [
    BotCommand(command="users", description="👥 Пользователи"),
    BotCommand(command="addrole", description="🔧 Сменить роль"),
    BotCommand(command="deactivate", description="🚫 Деактивировать пользователя"),
    BotCommand(command="audit", description="📋 Аудит лог"),
    BotCommand(command="frozen", description="🧊 Замороженные заказы"),
]
_COMMANDS_WAREHOUSE = [
    BotCommand(command="start", description="🏠 Главное меню"),
    BotCommand(command="returns", description="↩️ Возвраты на подтверждении"),
    BotCommand(command="return", description="↩️ Оформить возврат"),
    BotCommand(command="ship", description="🚚 Отгрузить заказ"),
]
_COMMANDS_BOOKKEEPER = [
    BotCommand(command="start", description="🏠 Главное меню"),
    BotCommand(command="deposits", description="💵 Сдачи на подтверждении"),
]


async def set_commands_for_user(bot: Bot, chat_id: int, role: str) -> None:
    """Установить per-chat список /-команд под роль. Telegram кэширует
    эти команды у клиента — после /start пользователь сразу увидит
    свой набор в автокомплите.
    """
    if role == "admin":
        commands = _COMMANDS_ADMIN
    elif role == "boss":
        commands = _COMMANDS_BOSS
    elif role == "manager":
        commands = _COMMANDS_MANAGER
    elif role == "warehouse_keeper":
        commands = _COMMANDS_WAREHOUSE
    elif role == "bookkeeper":
        commands = _COMMANDS_BOOKKEEPER
    else:
        commands = [BotCommand(command="start", description="🏠 Активировать аккаунт")]
    try:
        await bot.set_my_commands(
            commands=commands,
            scope=BotCommandScopeChat(chat_id=chat_id),
        )
    except Exception as e:
        logger.warning("set_my_commands для chat=%s failed: %s", chat_id, e)


def get_welcome_text(role: str, first_name: str = "") -> str:
    """Приветствие с короткой подсказкой про навигацию.

    Главная мысль: «жми Открыть слева от поля ввода». Inline-меню
    для срочного/admin-only. Без длинного списка команд (он висит
    в /-автокомплите Telegram через set_my_commands).
    """
    role_name = ROLE_NAMES.get(role, "👤 Сотрудник")
    name_part = f", <b>{first_name}</b>" if first_name else ""
    if role == "guest":
        return (
            f"👋 Здравствуйте{name_part}!\n\n"
            "Ваш аккаунт ещё не активирован — обратитесь к администратору.\n\n"
            "<i>Когда активируют — снова напишите /start</i>"
        )

    hints = {
        "admin": "Полный доступ. Управление пользователями и аудит — кнопками ниже.",
        "boss": "Заявки на одобрение и подтверждение платежей — приходят push'ами.",
        "manager": "Создавайте заказы и отмечайте оплаты — всё в WebApp.",
        "warehouse_keeper": "Отгрузки и возвраты: /ship, /return, /returns и кнопки ниже.",
        "bookkeeper": "Подтверждение сдачи налички: /deposits и кнопка ниже.",
    }
    hint = hints.get(role, "")

    return (
        f"👋 Привет{name_part}!\n"
        f"{role_name}\n\n"
        f"🌐 <b>Жмите «Открыть» слева от поля ввода</b> — там всё:\n"
        f"каталог, заказы, аналитика, долги, платежи.\n\n"
        f"<i>{hint}</i>"
    )


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    # /start — универсальный выход: сбрасываем любое залипшее FSM-состояние
    # (черновик платежа/заказа/возврата), иначе следующий текст юзера снова
    # перехватит state-обработчик.
    await state.clear()
    user = message.from_user
    ensure_user(user.id, user.username or "", user.full_name or "", ADMIN_IDS)
    role = get_role(user.id)

    # Перезаписываем per-chat menu button у этого юзера с актуальным
    # WEBAPP_URL. Это важно: Telegram кэширует Menu Button per-user,
    # и если когда-то URL был другой (старый домен webapp-сервиса) —
    # у юзера в превью чата осталась кнопка с битым URL → «Not Found».
    # Глобальный set_chat_menu_button (без chat_id) кэш у юзеров НЕ
    # перетирает; нужен явный per-chat вызов.
    #
    # Гостям ставим MenuButtonDefault даже при наличии WEBAPP_URL —
    # WebApp у них откажет 403 на _authorize, лучше не показывать
    # тизерную кнопку которая всё равно не работает.
    from aiogram.types import MenuButtonWebApp, MenuButtonDefault, WebAppInfo

    try:
        if WEBAPP_URL and role != "guest":
            await message.bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonWebApp(
                    text="Открыть",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                ),
            )
        else:
            await message.bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonDefault(),
            )
    except Exception as e:
        logger.warning("set_chat_menu_button per-chat failed for %s: %s", user.id, e)

    # Ставим список /-команд под роль (Telegram автокомплит).
    await set_commands_for_user(message.bot, message.chat.id, role)

    # Гости — те, кого админ ещё не активировал.
    if role == "guest":
        return await message.answer(
            f"👋 Здравствуйте!\n\n"
            f"Ваш аккаунт ещё не активирован для работы с этим ботом.\n"
            f"Передайте свой ID администратору: <code>{user.id}</code>\n\n"
            f"После активации напишите /start ещё раз.",
            parse_mode="HTML",
            # Снимаем reply-кнопку, чтобы гость не видел «🌐 Открыть»
            # которая всё равно вернёт 403 (нет роли).
            reply_markup=ReplyKeyboardRemove(),
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
            logger.warning("MS link failed for %s: %s", user.full_name, reason)
            sync_status_line = (
                f"\n\n⚠️ <i>Не привязан к МойСклад: "
                f"{_html.escape(reason[:160])}</i>\n"
                f"<i>Передайте админу — аналитика по сотруднику "
                f"недоступна без привязки.</i>"
            )

    # Welcome + единое меню одним сообщением (раньше было двумя: текст
    # отдельно, «⚡ Быстрые действия» с кнопками — отдельно).
    # Если у роли нет меню (employee/гость уже отсеян) — снимаем возможную
    # устаревшую reply-кнопку «🌐 Открыть» (PR #47) тем же сообщением.
    inline_markup = get_keyboard_for_role(role)
    await message.answer(
        get_welcome_text(role, user.first_name or "") + sync_status_line,
        parse_mode="HTML",
        reply_markup=inline_markup or ReplyKeyboardRemove(),
    )

    # Показываем сводку за месяц для менеджера
    if role == "manager":
        from handlers.analytics import show_manager_summary

        await show_manager_summary(message.bot, message.chat.id, message.from_user.id)


@router.message(Command("find"))
async def cmd_find(message: Message):
    """Глобальный поиск: /find <текст> — заказы, платежи, клиенты.

    Менеджер видит только свои заказы/платежи; начальство — все.
    Клиенты (контрагенты) видны всем.
    """
    from services import async_db as adb
    from services import snapshot
    from services.roles import _has_role
    from utils.helpers import esc

    role = get_role(message.from_user.id)
    if role == "guest":
        return await message.answer("⛔ Нет доступа.")
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return await message.answer(
            "🔍 Формат: <code>/find текст</code>\nНапример: <code>/find Иванов</code>",
            parse_mode="HTML",
        )
    query = parts[1].strip()[:100]
    privileged = _has_role(message.from_user.id, "admin", "boss")
    scope_uid = None if privileged else message.from_user.id

    orders = await adb.search_orders(query, user_id=scope_uid, limit=10)
    payments = await adb.search_payments(query, user_id=scope_uid, limit=10)
    agents = await asyncio.to_thread(snapshot.get_counterparties, query, 10)

    lines: list[str] = [f"🔍 <b>Поиск: {esc(query)}</b>"]
    if orders:
        lines.append("\n📦 <b>Заказы:</b>")
        for o in orders:
            lines.append(
                f"  • #{o['id']} · {esc(o.get('agent_name') or '—')} · {esc(o.get('status') or '')}"
            )
    if payments:
        lines.append("\n💵 <b>Платежи:</b>")
        for p in payments:
            amt = p.get("amount") or 0
            lines.append(
                f"  • #{p['id']} · {amt:g} {esc(p.get('currency') or '')} · {esc(p.get('full_name') or '—')}"
            )
    if agents:
        lines.append("\n👤 <b>Клиенты:</b>")
        for a in agents:
            lines.append(f"  • {esc(a.get('name') or '—')}")
    if not (orders or payments or agents):
        lines.append("\nНичего не найдено.")

    # Кнопки на найденные заказы — открыть в один тап, без захода в /myorders
    # и поиска по номеру вручную. Доступ всё равно проверит cb_view_order.
    markup = None
    if orders:
        kb = InlineKeyboardBuilder()
        for o in orders:
            kb.button(
                text=f"📦 Заказ #{o['id']} · {o.get('agent_name') or '—'}",
                callback_data=f"ord_view:{o['id']}",
            )
        kb.adjust(1)
        markup = kb.as_markup()
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=markup)


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
        return await message.answer(user_safe_error(e, "refresh_snapshot"))

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
    for tbl in ("ms_products", "ms_categories", "ms_counterparties", "ms_employees", "ms_stock"):
        lines.append(f"  • {tbl}: <code>{stats.get(tbl, 0)}</code>")
    lines.append("")
    lines.append("<b>Метаданные:</b>")
    for m in stats.get("meta", []):
        last = m.get("last_full_refresh") or m.get("last_refresh") or "—"
        lines.append(f"  • {m['dataset']}: {last} ({m.get('rows_count', 0)} rows)")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    # 🏠 Меню тоже сбрасывает залипшее FSM-состояние — см. cmd_start.
    await state.clear()
    user = call.from_user
    ensure_user(user.id, user.username or "", user.full_name or "", ADMIN_IDS)
    role = get_role(user.id)
    await call.answer()
    if role == "guest":
        return await call.message.answer("⛔ Ваш аккаунт ещё не активирован. Напишите /start.")
    # Welcome + единое меню одним сообщением.
    await call.message.answer(
        get_welcome_text(role, user.first_name or ""),
        parse_mode="HTML",
        reply_markup=get_keyboard_for_role(role),
    )


@router.callback_query(F.data == "ord_my")
async def cb_ord_my(call: CallbackQuery):
    await call.answer()
    from services.database import get_user_orders
    from handlers.orders import my_orders_keyboard

    orders = await get_user_orders(call.from_user.id)
    if not orders:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Создать заказ", callback_data="ord_new")
        kb.button(text="🏠 Меню", callback_data="menu")
        kb.adjust(1)
        return await call.message.answer("📋 У вас пока нет заказов.", reply_markup=kb.as_markup())
    await call.message.answer(
        f"📋 <b>Мои заказы</b> ({len(orders)}):",
        parse_mode="HTML",
        reply_markup=await my_orders_keyboard(orders),
    )


@router.callback_query(F.data == "ord_requests")
async def cb_ord_requests(call: CallbackQuery):
    if not is_boss(call.from_user.id):
        return await call.answer("Нет доступа", show_alert=True)
    await call.answer()
    from services.database import get_pending_requests, get_orders_by_ids
    from handlers.orders import pending_requests_keyboard

    requests = await get_pending_requests()  # async после asyncpg Stage 3
    if not requests:
        return await call.message.answer(
            f"{DIV}\n⏳ <b>Заявки на отгрузку</b>\n\n<i>Нет новых заявок</i>",
            parse_mode="HTML",
        )
    orders_by_id = await get_orders_by_ids([r["order_id"] for r in requests[:10]])
    await call.message.answer(
        f"⏳ <b>Заявки ({len(requests)}):</b>",
        parse_mode="HTML",
        reply_markup=await pending_requests_keyboard(requests, orders_by_id),
    )
