"""/pay в боте: двойной тап по валюте создавал два платежа (п.9 аудита).

aiogram обрабатывает апдейты параллельно: оба колбэка `pay_cur:*` проходили
фильтр состояния и читали данные раньше, чем первый успевал сбросить FSM.
Моделируем это хранилищем, которое уступает управление на каждом обращении
(как Redis в проде), и зовём хендлер дважды одновременно.
"""

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import services.roles as roles


class _YieldingStorage(MemoryStorage):
    async def get_data(self, key):
        await asyncio.sleep(0)
        return await super().get_data(key)

    async def set_state(self, key, state=None):
        await asyncio.sleep(0)
        return await super().set_state(key, state)

    async def set_data(self, key, data):
        await asyncio.sleep(0)
        return await super().set_data(key, data)


class _User:
    id = 1
    full_name = "Manager"
    username = "mgr"


class _Chat:
    id = 1


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _Message:
    def __init__(self):
        self.chat = _Chat()
        self.message_id = 555
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)

    async def edit_text(self, text, **kw):
        self.answers.append("EDIT:" + text)


class _Call:
    def __init__(self, data, message):
        self.data = data
        self.from_user = _User()
        self.message = message
        self.alerts = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


def _payments(db):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT amount_cents, currency FROM payments WHERE user_id = ?"), (1,))
        return [tuple(r) for r in cur.fetchall()]


def _prepared_state():
    from handlers.payments import PaymentState

    state = FSMContext(_YieldingStorage(), StorageKey(bot_id=42, chat_id=1, user_id=1))

    async def prep():
        await state.set_state(PaymentState.waiting_for_currency)
        await state.update_data(amount=1500.0, comment="аренда")

    asyncio.run(prep())
    return state


def test_double_tap_on_currency_creates_one_payment(isolated_db):
    from handlers.payments import process_currency

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    state = _prepared_state()
    message = _Message()
    bot = _Bot()
    calls = [_Call("pay_cur:USD", message), _Call("pay_cur:USD", message)]

    async def both():
        await asyncio.gather(*(process_currency(c, state, bot) for c in calls))

    asyncio.run(both())
    assert _payments(db) == [(150000, "USD")]
    assert sum(1 for c in calls if any("уже" in a for a in c.alerts)) == 1


def test_forged_currency_is_refused(isolated_db):
    from handlers.payments import process_currency

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    state = _prepared_state()
    call = _Call("pay_cur:BTC", _Message())
    asyncio.run(process_currency(call, state, _Bot()))
    assert _payments(db) == []
    assert call.alerts == ["Неизвестная валюта"]
