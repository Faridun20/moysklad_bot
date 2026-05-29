"""
Тесты bot-хендлеров возвратов (handlers/returns). Фейковые объекты + asyncio.run.
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

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class _FakeChat:
    id = 1


class _FakeMessage:
    def __init__(self, text="", uid=1, bot=None):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.bot = bot or _FakeBot()
        self.chat = _FakeChat()
        self.message_id = 100
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.answers.append(("EDIT:" + text, kwargs))


class _FakeCall:
    def __init__(self, data, uid, bot=None):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid=uid, bot=bot)
        self.alerts = []

    async def answer(self, text="", **kwargs):
        self.alerts.append((text, kwargs))


class _FakeState:
    """Минимальный FSMContext: хранит data и state в памяти."""

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


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 2, "шт", 100.0)  # total 200
    db.update_order_status(oid, "shipped")
    return oid


def test_full_return_flow_creates_and_confirms(isolated_db):
    db = isolated_db
    from handlers.returns import (
        cmd_return,
        process_return_reason,
        cb_return_refund,
        cb_return_full,
        cb_return_confirm,
    )

    oid = _setup(db)
    bot = _FakeBot()
    state = _FakeState()

    # /return <oid>
    asyncio.run(cmd_return(_FakeMessage(text=f"/return {oid}", uid=1, bot=bot), state))
    assert state._data.get("order_id") == oid

    # причина
    asyncio.run(process_return_reason(_FakeMessage(text="брак партии", uid=1, bot=bot), state))
    assert state._data.get("reason") == "брак партии"

    # способ возврата → экран выбора позиций (полный/частичный)
    refund_call = _FakeCall("ret_rm:debt_reduction", uid=1, bot=bot)
    asyncio.run(cb_return_refund(refund_call, state))

    # «📦 Весь заказ» → создаётся полный возврат + уведомление боссу
    full_call = _FakeCall("ret_full", uid=1, bot=bot)
    asyncio.run(cb_return_full(full_call, state, bot))
    pend = asyncio.run(db.get_pending_returns())  # async после asyncpg Stage 5
    assert len(pend) == 1
    return_id = pend[0]["id"]
    assert any(chat == 2 for chat, _, _ in bot.sent)  # боссу ушло

    # босс подтверждает
    bot2 = _FakeBot()
    conf_call = _FakeCall(f"ret_ok:{return_id}", uid=2, bot=bot2)
    asyncio.run(cb_return_confirm(conf_call, bot2))
    assert db.get_order(oid)["status"] == "returned"  # полный возврат


def test_return_blocked_for_non_shipped(isolated_db):
    db = isolated_db
    from handlers.returns import cmd_return

    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")  # draft
    db.add_order_item(oid, "T", "", 1, "шт", 10.0)
    msg = _FakeMessage(text=f"/return {oid}", uid=1)
    asyncio.run(cmd_return(msg, _FakeState()))
    assert any("отгруженных" in t for t, _ in msg.answers)


def test_confirm_return_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.returns import cb_return_confirm

    oid = _setup(db)
    items = db.get_order_items(oid)
    r = asyncio.run(
        db.create_return(
            oid, "full", "x", [(items[0]["id"], 2, 200.0)], refund_method="no_refund", created_by=1
        )
    )
    call = _FakeCall(f"ret_ok:{r['return_id']}", uid=1)  # менеджер не вправе
    asyncio.run(cb_return_confirm(call, _FakeBot()))
    assert any("доступа" in (a[0] or "").lower() for a in call.alerts)
    assert db.get_order(oid)["status"] == "shipped"  # не подтверждён
