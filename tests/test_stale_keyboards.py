"""
T3.2 — устаревшие inline-кнопки и дубли черновиков.

Класс баги: после успешного действия клавиатура оставалась в сообщении и
выглядела рабочей. Второй тап либо ничего не делал (сервер идемпотентен), либо
вообще не доходил до хендлера (фильтр по FSM-состоянию больше не совпадает) —
Telegram крутил спиннер до таймаута. Отдельно: «Новый заказ» плодил пустые
черновики на каждое нажатие.

Проверяем:
1. `handlers/_ui` — снятие/замена клавиатуры; косметика не роняет хендлер, если
   Telegram отказал в правке (действие в БД к этому моменту уже выполнено).
2. `get_or_create_draft` — пустой черновик переиспользуется, непустой — нет.
3. Хендлеры гасят отработавший список (товары/клиенты) и не выдают кнопку
   «Товар получен» по уже принятому возврату.

Хендлеры aiogram зовём напрямую с фейковыми объектами через asyncio.run
(pytest-asyncio в проекте нет), БД — настоящая SQLite (isolated_db).
"""

import asyncio

import services.roles as roles

_SENTINEL = "ORIGINAL-MARKUP"


class _FakeUser:
    def __init__(self, uid, full_name="Manager"):
        self.id = uid
        self.full_name = full_name


class _FakeChat:
    id = 55


class _FakeBot:
    def __init__(self, *, fail_send=False):
        self.sent = []
        self.markup_edits = []
        self.fail_send = fail_send

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_send:
            raise RuntimeError("message to reply not found")
        self.sent.append((chat_id, text, kwargs))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markup_edits.append((chat_id, message_id, reply_markup))


class _FakeMessage:
    """Сообщение с отслеживанием клавиатуры.

    `markup` стартует с sentinel — так тест отличает «клавиатуру сняли»
    (None) от «её никто не трогал».
    """

    def __init__(self, text="Карточка", *, uid=1, fail_edit_text=False, bot=None):
        self.text = text
        self.html_text = text
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = 9
        self.bot = bot or _FakeBot()
        self.answers = []
        self.markup = _SENTINEL
        self.edited = None
        self.fail_edit_text = fail_edit_text

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeMessage(text=text, uid=self.from_user.id, bot=self.bot)

    async def edit_text(self, text, **kwargs):
        if self.fail_edit_text:
            raise RuntimeError("message can't be edited")
        self.edited = text
        self.markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, reply_markup=None):
        self.markup = reply_markup


class _FakeCall:
    def __init__(self, data="", uid=1, message=None, bot=None):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = message or _FakeMessage(bot=bot)
        self.alerts = []

    async def answer(self, text="", **kwargs):
        self.alerts.append((text, kwargs))


class _FakeState:
    def __init__(self):
        self._data = {}
        self._state = None

    async def clear(self):
        self._data = {}
        self._state = None

    async def set_state(self, st):
        self._state = st

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)


def _kb_callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ─── handlers/_ui ─────────────────────────────────────────────────────────────


def test_drop_keyboard_removes_markup_and_keeps_text():
    from handlers._ui import drop_keyboard

    call = _FakeCall()
    asyncio.run(drop_keyboard(call))
    assert call.message.markup is None
    assert call.message.edited is None  # текст не тронут


def test_finish_card_appends_note_and_removes_markup():
    from handlers._ui import finish_card

    call = _FakeCall(message=_FakeMessage(text="Заявка #7"))
    asyncio.run(finish_card(call, "✅ Отправлено"))
    assert "Заявка #7" in call.message.edited
    assert "✅ Отправлено" in call.message.edited
    # edit_text без reply_markup — Telegram снимает клавиатуру
    assert call.message.markup is None


def test_finish_card_survives_failed_edit_and_still_drops_keyboard():
    """Сообщение могли удалить/оно слишком старое — падать нельзя: запись в БД
    уже прошла. Минимум — снять клавиатуру."""
    from handlers._ui import finish_card

    call = _FakeCall(message=_FakeMessage(fail_edit_text=True))
    asyncio.run(finish_card(call, "✅ Готово"))
    assert call.message.edited is None
    assert call.message.markup is None


def test_replace_keyboard_sets_new_markup():
    from handlers._ui import replace_keyboard
    from handlers.returns import _confirm_keyboard

    call = _FakeCall(message=_FakeMessage(text="Возврат #3"))
    markup = _confirm_keyboard(3, goods_received=True)
    asyncio.run(replace_keyboard(call, "📦 Товар получен", markup))
    assert "📦 Товар получен" in call.message.edited
    assert call.message.markup is markup


def test_finish_message_without_ids_returns_false():
    """Нет сохранённых id — вызывающий обязан отчитаться сам."""
    from handlers._ui import finish_message

    bot = _FakeBot()
    assert asyncio.run(finish_message(bot, None, None, "note")) is False
    assert bot.sent == []


def test_finish_message_drops_keyboard_and_stamps_reply():
    from handlers._ui import finish_message

    bot = _FakeBot()
    assert asyncio.run(finish_message(bot, 55, 9, "❌ Отклонено")) is True
    assert bot.markup_edits == [(55, 9, None)]
    chat, text, kwargs = bot.sent[0]
    assert (chat, kwargs["reply_to_message_id"]) == (55, 9)
    assert "❌ Отклонено" in text


def test_finish_message_reports_failure_when_card_gone():
    """Карточку удалили → пометку не доставить. False, чтобы вызывающий
    отправил её обычным сообщением (иначе операция пройдёт молча)."""
    from handlers._ui import finish_message

    assert asyncio.run(finish_message(_FakeBot(fail_send=True), 55, 9, "note")) is False


# ─── get_or_create_draft ──────────────────────────────────────────────────────


def test_empty_draft_is_reused(isolated_db):
    db = isolated_db
    first, created_1 = asyncio.run(db.get_or_create_draft(1, "Manager"))
    second, created_2 = asyncio.run(db.get_or_create_draft(1, "Manager"))
    assert (created_1, created_2) == (True, False)
    assert first == second


def test_draft_with_item_is_not_reused(isolated_db):
    db = isolated_db
    oid, _ = asyncio.run(db.get_or_create_draft(1, "Manager"))
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    new_id, created = asyncio.run(db.get_or_create_draft(1, "Manager"))
    assert created is True
    assert new_id != oid


def test_draft_with_agent_is_not_reused(isolated_db):
    """Клиент выбран — пользователь уже вложился в этот черновик."""
    db = isolated_db
    oid, _ = asyncio.run(db.get_or_create_draft(1, "Manager"))
    db.update_order_agent(oid, "AG-1", "Клиент")
    new_id, created = asyncio.run(db.get_or_create_draft(1, "Manager"))
    assert created is True
    assert new_id != oid


def test_comment_forces_new_draft(isolated_db):
    """С комментарием переиспользовать нечего — его нужно записать."""
    db = isolated_db
    oid, _ = asyncio.run(db.get_or_create_draft(1, "Manager"))
    new_id, created = asyncio.run(db.get_or_create_draft(1, "Manager", "срочно"))
    assert created is True
    assert new_id != oid
    assert asyncio.run(db.get_order(new_id))["comment"] == "срочно"


def test_other_users_draft_is_not_reused(isolated_db):
    db = isolated_db
    mine, _ = asyncio.run(db.get_or_create_draft(1, "Manager"))
    theirs, created = asyncio.run(db.get_or_create_draft(2, "Other"))
    assert created is True
    assert theirs != mine


def test_non_draft_order_is_not_reused(isolated_db):
    db = isolated_db
    oid, _ = asyncio.run(db.get_or_create_draft(1, "Manager"))
    db.update_order_status(oid, "approved")
    new_id, created = asyncio.run(db.get_or_create_draft(1, "Manager"))
    assert created is True
    assert new_id != oid


def test_webapp_create_order_reuses_empty_draft(isolated_db, monkeypatch):
    """openOrderEditor(null) зовёт /api/orders/create на каждое открытие
    редактора — выход назад и повторный вход плодили пустые заказы."""
    import importlib

    from fastapi.testclient import TestClient

    import webapp.server as server

    db = isolated_db
    importlib.reload(roles)
    db.set_role(20, "mgr", "Manager", "manager")
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    client = TestClient(server.app)

    first = client.post("/api/orders/create", json={"initData": "20"})
    second = client.post("/api/orders/create", json={"initData": "20"})
    assert first.status_code == second.status_code == 200, second.text
    assert first.json()["order_id"] == second.json()["order_id"]
    assert len(asyncio.run(db.get_user_orders(20, status="draft"))) == 1


def test_deposit_reject_flow_kills_card_buttons_and_stamps_result(isolated_db):
    """Вход в FSM снимает кнопки карточки (подтвердить параллельно с отклонением
    нельзя), а после ввода причины решение помечается на самой карточке."""
    db = isolated_db
    from handlers.deposits import cb_deposit_reject, process_deposit_reject_reason

    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 250.0)
    db.update_order_status(oid, "shipped")
    dep_id = asyncio.run(db.create_cash_deposit(1, 250.0))["deposit_id"]

    bot = _FakeBot()
    state = _FakeState()
    call = _FakeCall(f"dep_no:{dep_id}", uid=2, bot=bot)
    asyncio.run(cb_deposit_reject(call, state))
    assert call.message.markup is None  # кнопки карточки мертвы уже на входе

    asyncio.run(
        process_deposit_reject_reason(_FakeMessage(text="не сходится", uid=2, bot=bot), state, bot)
    )
    assert asyncio.run(db.get_cash_deposit(dep_id))["status"] == "rejected"
    # Пометка — ответом на карточку (чат/message_id взяты из state).
    stamps = [t for chat, t, kw in bot.sent if kw.get("reply_to_message_id") == 9 and chat == 55]
    assert stamps and f"Сдача #{dep_id} отклонена" in stamps[0]


def test_cancel_flow_kills_abort_button_after_reason(isolated_db):
    """Кнопка «❌ Отмена» на промпте гаснет, когда причина принята — иначе она
    отвечает «отмена прервана» уже по отменённому заказу."""
    db = isolated_db
    from handlers.order_cancel import cmd_cancel, process_cancel_reason

    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "AG-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")

    bot = _FakeBot()
    state = _FakeState()
    asyncio.run(cmd_cancel(_FakeMessage(text=f"/cancel {oid}", uid=2, bot=bot), state))
    data = asyncio.run(state.get_data())
    assert (data.get("msg_chat"), data.get("msg_id")) == (55, 9)  # промпт запомнен

    asyncio.run(process_cancel_reason(_FakeMessage(text="клиент отказался", uid=2, bot=bot), state, bot))
    assert asyncio.run(db.get_order(oid))["status"] == "cancelled"
    assert (55, 9, None) in bot.markup_edits  # клавиатуру промпта сняли
