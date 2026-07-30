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


# ─── T2.10: валидация суммы и перепроверка роли ─────────────────────────────
#
# §5.2.8: проверка была `amount < 0`, а `float('inf') < 0` — ложь, поэтому
# «inf» и «nan» проходили насквозь. Бесконечный лимит делает ЛЮБОЙ долг
# «свободным»: кредит-чек перестаёт срабатывать вовсе. Плюс это был
# единственный FSM в проекте без перепроверки роли после перехода.

import pytest  # noqa: E402


def _flow_to_amount(db, uid=2, agent="AG-1"):
    """Довести FSM до состояния ожидания суммы."""
    from handlers.credit import cb_limit_set

    state = _FakeState()
    call = _FakeCall(f"lim_set:{agent}", uid=uid)
    asyncio.run(cb_limit_set(call, state))
    return state


@pytest.mark.parametrize("bad", ["inf", "-inf", "Infinity", "nan", "NaN"])
def test_non_finite_limit_is_rejected(isolated_db, bad):
    db = isolated_db
    from handlers.credit import process_limit_amount

    _setup(db)
    state = _flow_to_amount(db)
    msg = _FakeMessage(text=bad, uid=2)
    asyncio.run(process_limit_amount(msg, state))

    assert any("от 0 до" in t or "числом" in t for t, _ in msg.answers), msg.answers
    # Лимит не изменился — остался дефолтным из app_settings.
    assert asyncio.run(db.get_credit_limit("AG-1")) == 2000.0


@pytest.mark.parametrize("bad", ["-5", "10000000", "99999999"])
def test_out_of_range_limit_is_rejected(isolated_db, bad):
    db = isolated_db
    from handlers.credit import process_limit_amount

    _setup(db)
    state = _flow_to_amount(db)
    msg = _FakeMessage(text=bad, uid=2)
    asyncio.run(process_limit_amount(msg, state))

    assert any("от 0 до" in t for t, _ in msg.answers), msg.answers
    assert asyncio.run(db.get_credit_limit("AG-1")) == 2000.0


def test_zero_limit_is_allowed(isolated_db):
    """0 — валидная граница: «в долг не отпускать вообще»."""
    db = isolated_db
    from handlers.credit import process_limit_amount

    _setup(db)
    state = _flow_to_amount(db)
    asyncio.run(process_limit_amount(_FakeMessage(text="0", uid=2), state))
    assert asyncio.run(db.get_credit_limit("AG-1")) == 0.0


def test_role_revoked_between_steps_blocks_write(isolated_db):
    """Роль снята между `lim_set:` и вводом суммы — запись не проходит."""
    db = isolated_db
    from handlers.credit import process_limit_amount

    _setup(db)
    state = _flow_to_amount(db)

    db.set_role(2, "boss", "Boss", "manager")  # понизили
    roles.invalidate_all_roles()

    msg = _FakeMessage(text="5000", uid=2)
    asyncio.run(process_limit_amount(msg, state))

    assert any("доступ" in t.lower() for t, _ in msg.answers), msg.answers
    assert asyncio.run(db.get_credit_limit("AG-1")) == 2000.0


def test_agent_without_orders_is_rejected(isolated_db):
    """Паритет с /api/credit/set: лимит-сирота не заводим."""
    db = isolated_db
    from handlers.credit import process_limit_amount

    _setup(db)
    state = _FakeState()
    asyncio.run(state.update_data(agent_id="GHOST", agent_name="Призрак"))

    msg = _FakeMessage(text="5000", uid=2)
    asyncio.run(process_limit_amount(msg, state))

    assert any("есть заказ" in t for t, _ in msg.answers), msg.answers
    assert asyncio.run(db.get_credit_limit("GHOST")) == 2000.0  # дефолт, записи нет
