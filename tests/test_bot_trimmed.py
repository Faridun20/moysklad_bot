"""
T3.3 — бот урезан до того, чего нет в WebApp.

Проверяем не «функция удалена» (это увидел бы и grep), а что после среза бот
остался связным:

1. Снятая команда отвечает подсказкой с экраном WebApp — молчание пользователь
   читает как «бот сломался».
2. Роутеры собираются, и ни один хендлер не потерялся: решения по
   push-карточкам (заявка/платёж/сдача/возврат/долг), ops- и admin-команды на
   месте, а вырезанные callback'и никем не обрабатываются — кнопка на них
   висела бы без ответа.
3. Меню не предлагает удалённых экранов и /-автокомплит не обещает снятых
   команд.
"""

import asyncio

from aiogram.filters import Command
from aiogram.types import BotCommand
from aiogram.utils.magic_filter import MagicFilter

import services.roles as roles


class _FakeUser:
    def __init__(self, uid, full_name="Boss"):
        self.id = uid
        self.full_name = full_name
        self.username = f"u{uid}"
        self.first_name = full_name.split()[0]


class _FakeChat:
    id = 55


class _FakeMessage:
    def __init__(self, text="", uid=1):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = 9
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeMessage(text=text, uid=self.from_user.id)


_HANDLERS_CACHE: dict[str, list] = {}


def _handlers(kind: str) -> list:
    """Хендлеры бота по типу события ("message" / "callback_query").

    Кэшируем: aiogram запрещает подключать один и тот же Router ко второму
    Dispatcher'у («is already attached»), поэтому register_routers за прогон
    вызываем один раз.
    """
    if not _HANDLERS_CACHE:
        from aiogram import Dispatcher

        import bot as bot_module

        dp = Dispatcher()
        bot_module.register_routers(dp)
        for router in [dp, *dp.sub_routers]:
            for name, observer in router.observers.items():
                _HANDLERS_CACHE.setdefault(name, []).extend(observer.handlers)
    return _HANDLERS_CACHE.get(kind, [])


def _registered_commands() -> set[str]:
    """Команды, на которые в боте есть хендлер."""
    cmds: set[str] = set()
    for handler in _handlers("message"):
        for f in handler.filters or ():
            if isinstance(f.callback, Command):
                cmds.update(str(c) for c in f.callback.commands)
    return cmds


class _Probe:
    """Утиный CallbackQuery: фильтрам нужен только .data."""

    def __init__(self, data: str):
        self.data = data


def _has_callback_handler(data: str) -> bool:
    """Есть ли хендлер, который примет такой callback_data.

    Прогоняем настоящие F.data-фильтры, а не сравниваем их repr: у magic-filter
    в repr нет константы, и проверка «есть строка в тексте фильтров» молча
    врала бы. State-фильтры пропускаем — они про FSM, а не про регистрацию;
    хендлеры вообще без magic-фильтра не считаем (иначе совпадёт что угодно).
    """
    for handler in _handlers("callback_query"):
        # aiogram кладёт в filters bound-метод MagicFilter.resolve, а не сам
        # фильтр — узнаём его по владельцу и зовём как обычный предикат.
        magic = [
            f.callback
            for f in (handler.filters or ())
            if isinstance(getattr(f.callback, "__self__", None), MagicFilter)
        ]
        if not magic:
            continue
        if all(bool(m(_Probe(data))) for m in magic):
            return True
    return False


# ─── 1. Снятые команды ────────────────────────────────────────────────────────


def test_retired_command_answers_with_webapp_hint():
    from handlers.start import cmd_retired

    msg = _FakeMessage(text="/neworder")
    asyncio.run(cmd_retired(msg))
    text, kwargs = msg.answers[0]
    assert "WebApp" in text
    assert "Новый заказ" in text  # подсказали конкретный экран
    assert kwargs.get("parse_mode") == "HTML"


def test_retired_command_survives_bot_suffix_and_args():
    """Telegram присылает /deposit@my_bot в группах и /deposit 500 с аргументом."""
    from handlers.start import cmd_retired

    for text in ("/deposit@sklad_bot", "/deposit 500"):
        msg = _FakeMessage(text=text)
        asyncio.run(cmd_retired(msg))
        answer = msg.answers[0][0]
        assert "Касса" in answer, text


def test_retired_commands_have_no_own_handlers():
    """Подсказка не должна конкурировать с живым хендлером той же команды."""
    from handlers.start import _RETIRED_COMMANDS

    registered = _registered_commands()
    # Сама подсказка зарегистрирована на все снятые команды — это ожидаемо;
    # проверяем, что НИКАКОЙ другой хендлер их не объявляет.
    duplicates = []
    for handler in _handlers("message"):
        for f in handler.filters or ():
            if not isinstance(f.callback, Command):
                continue
            names = {str(c) for c in f.callback.commands}
            if names & set(_RETIRED_COMMANDS) and names != set(_RETIRED_COMMANDS):
                duplicates.append(sorted(names & set(_RETIRED_COMMANDS)))
    assert duplicates == [], duplicates
    assert set(_RETIRED_COMMANDS) <= registered


# ─── 2. Что осталось / что ушло ───────────────────────────────────────────────


def test_surviving_commands_are_registered():
    """Ops/admin-команды без аналога в WebApp никуда не делись."""
    registered = _registered_commands()
    for cmd in (
        "start", "pay", "find", "ship", "cancel", "shipments", "sync_payments",
        "frozen", "users", "addrole", "deactivate", "reactivate", "audit", "log",
        "refresh", "snapshot", "syncms", "msstaff",
    ):
        assert cmd in registered, cmd


def test_decision_callbacks_survive():
    """Решения приходят кнопками в push-уведомлении — они и есть смысл бота."""
    for cb in (
        "req_ok:7", "req_no:7", "req_draft:7", "req_ovr:7",  # заявка на отгрузку
        "pay_ok:7", "pay_no:7",  # платёж
        "dep_ok:7", "dep_no:7",  # сдача наличных
        "ret_ok:7", "ret_got:7",  # возврат: подтверждение и приёмка
        "unfreeze:7", "ord_view:7",  # разморозка, карточка заказа из /find
        "menu", "pay_start", "sh:today", "users_list", "al:today",  # меню
    ):
        assert _has_callback_handler(cb), cb


def test_webapp_duplicated_callbacks_are_gone():
    """Кнопки удалённых экранов не должны иметь обработчиков — а их producer'ы
    вырезаны вместе с ними (иначе тап висел бы без ответа)."""
    for cb in (
        "ord_new", "ord_my", "ord_requests",  # заказы и заявки
        "ord_add:1", "ord_submit:1", "ord_delete:1", "ord_agent:1", "ord_cur:1",
        "cat_pick:all:1", "prod_pick:1:0", "agent_pick:1:0",  # каталог МС
        "debts_my", "debt_paid:1",  # долги
        "dep_pending", "ret_pending",  # очереди сдач и возвратов
        "sp:0", "cats:0", "ci:0",  # остатки и категории
        "an:sales:month", "anp:sales", "pr:today",  # аналитика и отчёт платежей
        "rate_set:UZS", "price_set:p1", "lim_set:AG-1",  # курсы, цены, лимиты
        "req_view:1",  # просмотр заявки из списка — список вырезан
    ):
        assert not _has_callback_handler(cb), cb


def test_no_dangling_callback_buttons_in_bot_code():
    """Каждая callback-кнопка, которую бот или cron рисует, должна иметь
    обработчик. Ловит забытый producer после среза экрана."""
    import re
    from pathlib import Path

    produced: set[str] = set()
    for path in [
        *Path("handlers").glob("*.py"),
        *Path("tasks").glob("*.py"),
        *Path("utils").glob("*.py"),
    ]:
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r'callback_data[=:]\s*f?"([^"{]*)', src):
            if m.group(1):
                produced.add(m.group(1))
    # Обрезанный f-string («ord_view:») дополняем id — фильтру нужен полный data.
    dangling = sorted(
        cb for cb in produced if not _has_callback_handler(cb + "1" if cb.endswith(":") else cb)
    )
    assert dangling == [], dangling


# ─── 3. Меню и автокомплит ────────────────────────────────────────────────────


def test_menu_offers_webapp_and_no_removed_screens():
    from handlers.start import get_keyboard_for_role

    markup = get_keyboard_for_role("manager")
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert "ord_my" not in callbacks and "debts_my" not in callbacks
    assert "sp:0" not in callbacks and "analytics" not in callbacks
    assert "pay_start" in callbacks  # отправка платежа осталась
    assert "sh:today" in callbacks  # отгрузок в WebApp нет


def test_guest_menu_is_empty():
    from handlers.start import get_keyboard_for_role

    assert get_keyboard_for_role("guest") is None


def test_autocomplete_does_not_promise_removed_commands():
    """set_my_commands обещает только то, на что есть хендлер."""
    from handlers.start import (
        _COMMANDS_ADMIN,
        _COMMANDS_BOOKKEEPER,
        _COMMANDS_BOSS,
        _COMMANDS_MANAGER,
        _COMMANDS_WAREHOUSE,
        _RETIRED_COMMANDS,
    )

    for commands in (
        _COMMANDS_MANAGER, _COMMANDS_BOSS, _COMMANDS_ADMIN,
        _COMMANDS_WAREHOUSE, _COMMANDS_BOOKKEEPER,
    ):
        for c in commands:
            assert isinstance(c, BotCommand)
            assert c.command not in _RETIRED_COMMANDS, c.command


def test_start_still_greets_manager(isolated_db, monkeypatch):
    """/start не должен упасть после снятия сводки менеджера (её слал
    вырезанный handlers/analytics)."""
    import handlers.start as start

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(7, "mgr", "Manager", "manager")

    calls = {"menu_button": 0, "commands": 0, "sync": 0}

    class _Bot:
        async def set_chat_menu_button(self, **kwargs):
            calls["menu_button"] += 1

        async def set_my_commands(self, **kwargs):
            calls["commands"] += 1

    async def _fake_sync(*a, **k):
        calls["sync"] += 1
        return {"status": "ok"}

    monkeypatch.setattr("services.ms_sync.sync_manager", _fake_sync)

    class _State:
        async def clear(self):
            pass

    msg = _FakeMessage(text="/start", uid=7)
    msg.bot = _Bot()
    asyncio.run(start.cmd_start(msg, _State()))

    assert len(msg.answers) == 1  # приветствие одним сообщением, без досылки сводки
    assert "Привет" in msg.answers[0][0]
    assert calls["commands"] == 1
