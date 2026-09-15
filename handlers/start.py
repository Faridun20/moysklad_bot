"""
Общие хэндлеры: /start, меню, управление ролями
"""

import logging
from aiogram import Bot, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardRemove,
    BotCommand,
    BotCommandScopeChat,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
# cached_role вместо database.get_role: per-process кэш ролей (TTL 60с) —
# /start и команды не делают синхронный SELECT к БД на каждый вызов
# (конвенция CLAUDE.md). Значение идентично get_role (guest для деактивированных).
from handlers._ui import webapp_keyboard
from services.roles import cached_role as get_role
from config import ADMIN_IDS, WEBAPP_URL
from services.database import (
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
    Inline-меню: вход в WebApp + то, чего в WebApp нет.

    T3.3: заказы, остатки, долги, аналитика, очереди сдач и возвратов из бота
    вырезаны — их кнопки вели в удалённые экраны. Осталось: WebApp (там вся
    работа), отгрузки (аналога в WebApp нет) и админский ряд —
    пользователи/аудит (эндпоинтов под них тоже нет).

    Решения (одобрить заявку, подтвердить платёж/сдачу/возврат) в меню не
    нужны: они приходят кнопками прямо в push-уведомлении.
    """
    if role == "guest":
        return None

    kb = InlineKeyboardBuilder()
    rows: list[int] = []

    if WEBAPP_URL and WEBAPP_URL.startswith("https://"):
        kb.button(text="🌐 Открыть WebApp", web_app=WebAppInfo(url=WEBAPP_URL))
        rows += [1]

    # Отгрузки — единственный список, которого нет в WebApp.
    if role in ("admin", "boss", "manager", "warehouse_keeper"):
        kb.button(text="🚚 Отгрузки", callback_data="sh:today")
        rows += [1]

    # Менеджер: быстрая отправка платежа в кассу (не привязанного к заказу) —
    # три касания в чате против захода в WebApp.
    if role == "manager":
        kb.button(text="💵 Отправить платёж", callback_data="pay_start")
        rows += [1]

    # Админский ряд — управление пользователями и аудит.
    if role == "admin":
        kb.button(text="👥 Пользователи", callback_data="users_list")
        kb.button(text="📋 Аудит", callback_data="al:today")
        rows += [2]

    if not rows:
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
#
# T3.3: список сокращён до того, что бот реально делает. Всё, что переехало в
# WebApp (заказы, остатки, долги, аналитика, сдачи, возвраты, лимиты, курсы,
# цены), из автокомплита убрано — иначе Telegram предлагает команды, которых
# больше нет. Набравшему по памяти отвечает `cmd_retired` подсказкой.
_COMMANDS_MANAGER = [
    BotCommand(command="start", description="🏠 Главное меню"),
    BotCommand(command="pay", description="💵 Отправить платёж"),
    BotCommand(command="find", description="🔍 Поиск (заказ/платёж/клиент)"),
    # Техника: раздел в WebApp, в боте — быстрый взгляд и моточасы с площадки,
    # где открыть WebApp дольше, чем набрать два числа.
    BotCommand(command="machines", description="🚜 Машины"),
    BotCommand(command="hours", description="⏱ Моточасы: /hours 12 15200"),
    # Печать доступна тем же ролям, что получают печатную форму
    # (can_view_stock: admin/boss/manager) — значит и в автокомплите у них.
    BotCommand(command="printer", description="🖨 Статус принтера"),
]
_COMMANDS_BOSS = _COMMANDS_MANAGER + [
    # «Выкатилось ли» — вопрос того, кто просил правку, поэтому у босса тоже.
    BotCommand(command="version", description="📦 Версия на проде"),
    BotCommand(command="machine_deals", description="💳 Рассрочки по технике"),
    BotCommand(command="ship", description="🚚 Отгрузить заказ"),
    BotCommand(command="shipments", description="🚚 Последние отгрузки"),
    BotCommand(command="cancel", description="🚫 Отменить заказ"),
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
    BotCommand(command="ship", description="🚚 Отгрузить заказ"),
    BotCommand(command="shipments", description="🚚 Последние отгрузки"),
]
_COMMANDS_BOOKKEEPER = [
    BotCommand(command="start", description="🏠 Главное меню"),
]


async def set_commands_for_user(bot: Bot, chat_id: int, role: str) -> None:
    """Установить per-chat список /-команд под роль. Telegram кэширует
    эти команды у клиента — после /start пользователь сразу увидит
    свой набор в автокомплите.
    """
    by_role = {
        "admin": _COMMANDS_ADMIN,
        "boss": _COMMANDS_BOSS,
        "manager": _COMMANDS_MANAGER,
        "warehouse_keeper": _COMMANDS_WAREHOUSE,
        "bookkeeper": _COMMANDS_BOOKKEEPER,
    }
    if role in by_role:
        # Совмещение ролей: менеджеру пока достаются и команды кладовщика
        # (/ship) — services.roles.ROLE_ALSO_ACTS_AS. Дубли по имени команды
        # выкидываем: Telegram отвергает список с повтором целиком.
        from services.roles import effective_roles

        commands, seen = [], set()
        for r in effective_roles(role):
            for c in by_role.get(r, []):
                if c.command not in seen:
                    seen.add(c.command)
                    commands.append(c)
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
        "manager": (
            "Создавайте заказы и вносите оплату (наличные / карта / счёт) — всё в WebApp; "
            "«оплату сразу» без внесённой оплаты не отгрузить. "
            "Отгрузка — /ship; сдачи и приёмку возвратов подтверждайте кнопкой в уведомлении."
        ),
        "warehouse_keeper": "Отгрузка — /ship; приёмку возврата подтверждайте кнопкой в уведомлении.",
        "bookkeeper": "Сдачи налички подтверждайте кнопкой прямо в уведомлении.",
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

    # Welcome + единое меню одним сообщением (раньше было двумя: текст
    # отдельно, «⚡ Быстрые действия» с кнопками — отдельно).
    # Если у роли нет меню (employee/гость уже отсеян) — снимаем возможную
    # устаревшую reply-кнопку «🌐 Открыть» (PR #47) тем же сообщением.
    # T3.3: сводка за месяц для менеджера больше не досылается вторым
    # сообщением — она и так первое, что показывает главная WebApp
    # (/api/home), а бот-аналитика вырезана вместе с handlers/analytics.
    inline_markup = get_keyboard_for_role(role)
    await message.answer(
        get_welcome_text(role, user.first_name or ""),
        parse_mode="HTML",
        reply_markup=inline_markup or ReplyKeyboardRemove(),
    )


@router.message(Command("find"))
async def cmd_find(message: Message):
    """Глобальный поиск: /find <текст> — заказы, платежи, клиенты.

    Менеджер видит только свои заказы/платежи; начальство — все.
    Клиенты (контрагенты) видны всем.
    """
    from services import async_db as adb
    from services import counterparties as cp_service
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
    agents = await cp_service.search(query, 10)

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


# ─── Снятые команды: подсказка вместо тишины (T3.3) ──────────────────────────


# Команда → куда идти в WebApp. Набравший старую команду по памяти должен
# понять, что бот не сломался, а операция переехала.
_RETIRED_COMMANDS = {
    "neworder": "Заказы → «➕ Новый заказ»",
    "myorders": "Заказы",
    "orders": "Заявки",
    "stock": "Каталог",
    "categories": "Каталог",
    "debts": "Финансы → Долги",
    "deposit": "Финансы → Касса → «Сдать наличные»",
    "my_deposits": "Финансы → Касса",
    "deposits": "Финансы → Касса → «Сдачи на подтверждении»",
    "return": "Заказы → заказ → «Оформить возврат»",
    "returns": "Финансы → Касса → «Возвраты»",
    "limit": "Финансы → Клиенты → клиент → «Кредитный лимит»",
    "rates": "Финансы → Клиенты → «Курсы валют»",
    "prices": "Каталог → товар → «Цена»",
    "analytics": "Аналитика",
    "cashbox": "Финансы → Касса",
    "reports": "Аналитика",
    "payreport": "Финансы → История платежей",
    # Техника: карточка, фото и сделки переехали в WebApp.
    "newmachine": "Заказы → Техника → «Завести машину»",
    "sell": "Заказы → Техника → машина → «Продажа»",
    "credit": "Заказы → Техника → машина → «Рассрочка»",
}


# Команды, которых больше НЕ СУЩЕСТВУЕТ: они обслуживали интеграцию с
# МойСклад (синхронизация сотрудников и платежей, снапшот справочников).
# Учёт полностью локальный, синхронизировать не с чем. Отвечаем отдельно от
# `_RETIRED_COMMANDS`: там операция переехала в WebApp, здесь — исчезла, и
# отправлять человека искать её в интерфейсе было бы враньём.
_REMOVED_COMMANDS = ("syncms", "msstaff", "refresh", "snapshot", "sync_payments")


@router.message(Command(*_REMOVED_COMMANDS))
async def cmd_removed(message: Message):
    """Ответ на команду, удалённую вместе с интеграцией МойСклад."""
    await message.answer(
        "🗄 Эта команда убрана: учёт ведётся полностью у нас, "
        "синхронизировать с МойСклад больше нечего.\n\n"
        "<i>Остатки, накладные и справочники — в WebApp.</i>",
        parse_mode="HTML",
        reply_markup=webapp_keyboard(),
    )


@router.message(Command(*_RETIRED_COMMANDS))
async def cmd_retired(message: Message):
    """Ответ на команду, вырезанную в T3.3.

    Молча игнорировать нельзя: пользователь с мышечной памятью решит, что бот
    сломался, и будет писать админу. Отвечаем, где теперь эта операция, и
    даём кнопку входа — Menu Button слева от поля ввода замечают не все.
    """
    command = (message.text or "").lstrip("/").split()[0].split("@")[0].lower()
    where = _RETIRED_COMMANDS.get(command, "")
    where_line = f"\n📍 Экран: <b>{where}</b>" if where else ""
    await message.answer(
        f"🌐 Эта операция теперь в WebApp.{where_line}\n\n"
        f"<i>Открыть: кнопка ниже или «Открыть» слева от поля ввода.</i>",
        parse_mode="HTML",
        reply_markup=webapp_keyboard(),
    )
