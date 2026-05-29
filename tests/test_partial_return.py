"""
Частичный возврат в боте: выбор позиций и количеств.

Раньше бот оформлял только полный возврат. Теперь после выбора способа
возврата можно выбрать позиции (с количеством) и вернуть часть заказа.
create_return уже поддерживает partial + per-item; тут проверяем бот-FSM.
"""

import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "Boss"


class _FakeMessage:
    def __init__(self, text="", uid=1):
        self.text = text
        self.from_user = _FakeUser(uid)
        self.html_text = ""
        self.answers = []
        self.edits = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))


class _FakeCall:
    def __init__(self, data, uid=1):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid=uid)
        self.alerts = []

    async def answer(self, text="", **kw):
        self.alerts.append((text, kw))


def _state(uid=1):
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=uid, user_id=uid)
    return FSMContext(storage=storage, key=key)


def _setup_order(db, uid=1):
    db.set_role(uid, "boss", "Boss", "boss")
    oid = db.create_order(2, "Mgr", "")
    i1 = db.add_order_item(oid, "Товар-А", "hrefA", 5, "шт", 100.0)
    i2 = db.add_order_item(oid, "Товар-Б", "hrefB", 3, "шт", 50.0)
    db.update_order_status(oid, "shipped")
    return oid, i1, i2


def test_partial_return_selected_items(isolated_db):
    db = isolated_db
    oid, i1, i2 = _setup_order(db)

    from handlers import returns as R

    st = _state()

    async def scenario():
        # Эмулируем шаг после причины: уже на waiting_refund с order_id+reason.
        await st.set_state(R.ReturnFlow.waiting_refund)
        await st.update_data(order_id=oid, reason="брак части партии")

        # Выбор способа возврата → экран выбора позиций
        call = _FakeCall("ret_rm:cash")
        await R.cb_return_refund(call, st)
        assert await st.get_state() == R.ReturnFlow.choosing_items

        # Берём позицию 0 (Товар-А), вводим 2 из 5
        await R.cb_return_pick_item(_FakeCall("ret_pi:0"), st)
        await R.process_item_qty(_FakeMessage("2"), st)

        # Завершаем частичный возврат
        done = _FakeCall("ret_pdone")
        await R.cb_return_partial_done(done, st, bot=None)
        return done

    asyncio.run(scenario())

    # Создан возврат: только Товар-А ×2 = 200
    rets = asyncio.run(db.get_pending_returns())  # async после asyncpg Stage 5
    assert len(rets) == 1
    assert rets[0]["return_type"] == "partial"
    assert rets[0]["total_amount"] == 200.0
    assert rets[0]["total_amount_cents"] == 20000


def test_full_shortcut_returns_everything(isolated_db):
    db = isolated_db
    oid, i1, i2 = _setup_order(db)
    from handlers import returns as R

    st = _state()

    async def scenario():
        await st.set_state(R.ReturnFlow.waiting_refund)
        await st.update_data(order_id=oid, reason="полный брак")
        await R.cb_return_refund(_FakeCall("ret_rm:cash"), st)
        await R.cb_return_full(_FakeCall("ret_full"), st, bot=None)

    asyncio.run(scenario())

    rets = asyncio.run(db.get_pending_returns())  # async после asyncpg Stage 5
    assert len(rets) == 1
    # 5×100 + 3×50 = 650
    assert rets[0]["total_amount"] == 650.0
    assert rets[0]["return_type"] == "full"


def test_partial_done_requires_selection(isolated_db):
    db = isolated_db
    oid, _i1, _i2 = _setup_order(db)
    from handlers import returns as R

    st = _state()

    async def scenario():
        await st.set_state(R.ReturnFlow.waiting_refund)
        await st.update_data(order_id=oid, reason="причина")
        await R.cb_return_refund(_FakeCall("ret_rm:debt_reduction"), st)
        done = _FakeCall("ret_pdone")
        await R.cb_return_partial_done(done, st, bot=None)
        return done

    done = asyncio.run(scenario())
    # Ничего не выбрано → возврат не создан, показан alert
    assert asyncio.run(db.get_pending_returns()) == []
    assert any("выберите" in (t or "").lower() for t, _ in done.alerts)
