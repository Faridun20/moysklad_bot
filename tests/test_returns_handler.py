"""
Тесты bot-хендлеров возвратов (handlers/returns). Фейковые объекты + asyncio.run.

T3.3: оформление возврата ушло в WebApp (тесты — test_partial_return_api.py),
в боте остались кнопки push-карточки: приёмка товара и подтверждение.
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


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 2, "шт", 100.0)  # total 200
    db.update_order_status(oid, "shipped")
    return oid


def test_confirm_return_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.returns import cb_return_confirm

    oid = _setup(db)
    items = asyncio.run(db.get_order_items(oid))
    r = asyncio.run(
        db.create_return(
            oid, "full", "x", [(items[0]["id"], 2, 200.0)], refund_method="no_refund", created_by=1
        )
    )
    call = _FakeCall(f"ret_ok:{r['return_id']}", uid=1)  # менеджер не вправе
    asyncio.run(cb_return_confirm(call, _FakeBot()))
    assert any("доступа" in (a[0] or "").lower() for a in call.alerts)
    assert asyncio.run(db.get_order(oid))["status"] == "shipped"  # не подтверждён


def test_goods_received_then_confirm_closes_order(isolated_db):
    """Путь кнопок push-карточки: «📦 Товар получен» → «✅ Подтвердить возврат».

    После T2.8 подтверждение без приёмки отклоняется, поэтому проверяем связку,
    а не каждую кнопку отдельно: это и есть весь бот-путь возврата после T3.3.
    """
    db = isolated_db
    from handlers.returns import cb_return_confirm, cb_return_goods_received

    oid = _setup(db)
    items = asyncio.run(db.get_order_items(oid))
    r = asyncio.run(
        db.create_return(
            oid,
            "full",
            "брак",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    ret_id = r["return_id"]

    # Склад отмечает приёмку — кнопка «Товар получен» из уведомления.
    got_call = _FakeCall(f"ret_got:{ret_id}", uid=2)
    asyncio.run(cb_return_goods_received(got_call))
    assert asyncio.run(db.get_return(ret_id))["goods_received"]

    # Босс подтверждает — заказ уходит в returned.
    conf_call = _FakeCall(f"ret_ok:{ret_id}", uid=2)
    asyncio.run(cb_return_confirm(conf_call, _FakeBot()))
    assert asyncio.run(db.get_order(oid))["status"] == "returned"
