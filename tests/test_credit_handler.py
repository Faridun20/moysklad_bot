"""
Тесты bot-хендлеров кредитных лимитов (handlers/credit). Фейковые объекты +
asyncio.run; adb идёт в настоящую SQLite (isolated_db).
"""

import asyncio

import services.roles as roles


class _FakeUser:
    def __init__(self, uid, full_name="User"):
        self.id = uid
        self.full_name = full_name


class _FakeMessage:
    def __init__(self, text="", uid=1):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class _FakeCall:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid=uid)
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


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "AG-1", "ООО Ромашка")
    db.add_order_item(oid, "Товар", "", 3, "шт", 100.0)  # 300
    db.update_order_status(oid, "shipped")
    return oid


def test_limit_overview_lists_agents(isolated_db):
    db = isolated_db
    from handlers.credit import cmd_limit

    _setup(db)
    msg = _FakeMessage(text="/limit", uid=2)
    asyncio.run(cmd_limit(msg))
    text = msg.answers[0][0]
    assert "Ромашка" in text
    # есть клавиатура для редактирования
    assert msg.answers[0][1].get("reply_markup") is not None


def test_set_limit_via_flow(isolated_db):
    db = isolated_db
    from handlers.credit import cb_limit_set, process_limit_amount

    _setup(db)
    state = _FakeState()
    call = _FakeCall("lim_set:AG-1", uid=2)
    asyncio.run(cb_limit_set(call, state))
    assert state._data.get("agent_id") == "AG-1"

    asyncio.run(process_limit_amount(_FakeMessage(text="5000", uid=2), state))
    assert asyncio.run(db.get_credit_limit("AG-1")) == 5000.0


def test_limit_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.credit import cmd_limit

    _setup(db)
    msg = _FakeMessage(text="/limit", uid=1)  # менеджер
    asyncio.run(cmd_limit(msg))
    assert any("доступ" in t.lower() for t, _ in msg.answers)
