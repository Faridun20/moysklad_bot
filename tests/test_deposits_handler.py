"""
Тесты bot-хендлеров сдачи наличных (handlers/deposits).

Хендлеры aiogram вызываем напрямую с фейковыми объектами, гоняем через
asyncio.run (pytest-asyncio в проекте нет). adb-вызовы идут в настоящую
SQLite (isolated_db).
"""

import asyncio

import services.roles as roles


class _FakeUser:
    def __init__(self, uid, full_name="User"):
        self.id = uid
        self.full_name = full_name


class _FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, text, kwargs)

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
    """manager (1) + boss (2) + отгруженный заказ менеджера на 250."""
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    oid = db.create_order(1, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 250.0)
    db.update_order_status(oid, "shipped")
    return oid


def test_cmd_deposit_creates_and_notifies_boss(isolated_db):
    db = isolated_db
    from handlers.deposits import cmd_deposit

    _setup(db)
    bot = _FakeBot()
    msg = _FakeMessage(text="/deposit 250", uid=1, bot=bot)
    asyncio.run(cmd_deposit(msg))

    # Сдача создана (pending).
    deps = asyncio.run(db.get_manager_cash_deposits(1))
    assert len(deps) == 1 and deps[0]["status"] == "pending"
    # Боссу ушло уведомление с кнопками.
    assert any(chat == 2 for chat, _, _ in bot.sent)
    boss_msg = next(kw for chat, _, kw in bot.sent if chat == 2)
    assert boss_msg.get("reply_markup") is not None
    # Менеджеру — подтверждение создания.
    assert any("создана" in t for t, _ in msg.answers)


def test_deposit_bad_amount(isolated_db):
    db = isolated_db
    from handlers.deposits import cmd_deposit

    _setup(db)
    msg = _FakeMessage(text="/deposit ноль", uid=1)
    asyncio.run(cmd_deposit(msg))
    assert any("положительным" in t for t, _ in msg.answers)


def test_cb_confirm_closes_order_and_notifies_manager(isolated_db):
    db = isolated_db
    from handlers.deposits import cb_deposit_confirm

    oid = _setup(db)
    res = asyncio.run(db.create_cash_deposit(1, 250.0))
    bot = _FakeBot()
    call = _FakeCall(f"dep_ok:{res['deposit_id']}", uid=2, bot=bot)
    asyncio.run(cb_deposit_confirm(call, bot))

    assert asyncio.run(db.get_order(oid))["status"] == "paid"
    # Менеджеру (1) ушло «подтверждена».
    assert any(chat == 1 and "подтверждена" in text for chat, text, _ in bot.sent)


def test_cb_confirm_denied_for_manager(isolated_db):
    db = isolated_db
    from handlers.deposits import cb_deposit_confirm

    _setup(db)
    res = asyncio.run(db.create_cash_deposit(1, 250.0))
    bot = _FakeBot()
    call = _FakeCall(f"dep_ok:{res['deposit_id']}", uid=1, bot=bot)  # менеджер не вправе
    asyncio.run(cb_deposit_confirm(call, bot))

    assert any("доступа" in (a[0] or "").lower() for a in call.alerts)
    deps = asyncio.run(db.get_manager_cash_deposits(1))
    assert deps[0]["status"] == "pending"  # не подтверждено
