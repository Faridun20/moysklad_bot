"""
Тесты bot-хендлеров отмены заказа (handlers/order_cancel). Фейковые объекты +
asyncio.run; adb идёт в настоящую SQLite (isolated_db).
"""

import asyncio

import services.roles as roles


class _FakeUser:
    def __init__(self, uid, full_name="User"):
        self.id = uid
        self.full_name = full_name


class _FakeBot:
    def __init__(self):
        self.sent = []
        self.markup_edits = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markup_edits.append((chat_id, message_id, reply_markup))


class _FakeChat:
    id = 42


class _FakeMessage:
    def __init__(self, text="", uid=1):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = 7
        self.answers = []

    # aiogram отдаёт отправленное сообщение — cmd_cancel запоминает его
    # chat/message_id, чтобы потом погасить кнопку «Отмена» (T3.2).
    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeMessage(text=text, uid=self.from_user.id)


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


def _setup(db, status="approved"):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "AG-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, status)
    return oid


def test_cancel_approved_order_notifies_creator(isolated_db):
    db = isolated_db
    from handlers.order_cancel import cmd_cancel, process_cancel_reason

    oid = _setup(db, "approved")
    state = _FakeState()
    bot = _FakeBot()
    asyncio.run(cmd_cancel(_FakeMessage(text=f"/cancel {oid}", uid=2), state))
    assert state._data.get("order_id") == oid

    asyncio.run(process_cancel_reason(_FakeMessage(text="клиент отказался", uid=2), state, bot))
    assert asyncio.run(db.get_order(oid))["status"] == "cancelled"
    # создателю (1) ушло уведомление
    assert any(chat == 1 for chat, _, _ in bot.sent)


def test_cancel_blocked_for_shipped(isolated_db):
    db = isolated_db
    from handlers.order_cancel import cmd_cancel

    oid = _setup(db, "shipped")
    msg = _FakeMessage(text=f"/cancel {oid}", uid=2)
    asyncio.run(cmd_cancel(msg, _FakeState()))
    assert any("approved" in t for t, _ in msg.answers)
    assert asyncio.run(db.get_order(oid))["status"] == "shipped"


def test_cancel_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.order_cancel import cmd_cancel

    oid = _setup(db, "approved")
    msg = _FakeMessage(text=f"/cancel {oid}", uid=1)  # менеджер
    asyncio.run(cmd_cancel(msg, _FakeState()))
    assert any("босс" in t.lower() for t, _ in msg.answers)
    assert asyncio.run(db.get_order(oid))["status"] == "approved"
