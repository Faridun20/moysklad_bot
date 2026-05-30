"""
Тесты bot-хендлеров заказов — двухшаговое удаление черновика (B2).

Раньше `ord_delete:` сразу переводил черновик в rejected без вопроса —
случайный тап терял заказ. Теперь первый шаг показывает подтверждение,
а удаляет только `ord_delete_yes:`.

Хендлеры aiogram вызываем напрямую с фейковым CallbackQuery и гоняем
корутину через asyncio.run (pytest-asyncio в проекте нет).
"""

import asyncio


class _FakeUser:
    def __init__(self, uid, full_name="Manager"):
        self.id = uid
        self.full_name = full_name


class _FakeMessage:
    def __init__(self):
        self.answers = []  # список (text, kwargs)

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class _FakeCall:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage()
        self.alerts = []

    async def answer(self, text="", **kwargs):
        self.alerts.append((text, kwargs))


def _kb_callbacks(markup):
    """Собрать все callback_data из InlineKeyboardMarkup."""
    out = []
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                out.append(btn.callback_data)
    return out


def test_delete_shows_confirm_and_keeps_draft(isolated_db):
    db = isolated_db
    from handlers.orders import cb_delete_order

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")
    assert asyncio.run(db.get_order(oid))["status"] == "draft"

    call = _FakeCall(f"ord_delete:{oid}", uid=1)
    asyncio.run(cb_delete_order(call))

    # Заказ НЕ удалён — только показан вопрос
    assert asyncio.run(db.get_order(oid))["status"] == "draft"
    assert len(call.message.answers) == 1
    text, kwargs = call.message.answers[0]
    assert "Удалить" in text
    markup = kwargs.get("reply_markup")
    assert markup is not None
    cbs = _kb_callbacks(markup)
    assert f"ord_delete_yes:{oid}" in cbs  # кнопка «Да»
    assert f"ord_view:{oid}" in cbs  # кнопка «Нет, оставить»


def test_delete_yes_actually_deletes(isolated_db):
    db = isolated_db
    from handlers.orders import cb_delete_order_yes

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")

    call = _FakeCall(f"ord_delete_yes:{oid}", uid=1)
    asyncio.run(cb_delete_order_yes(call))

    # Черновик помечен rejected (так реализовано «удаление»)
    assert asyncio.run(db.get_order(oid))["status"] == "rejected"


def test_delete_yes_rejects_non_owner(isolated_db):
    db = isolated_db
    from handlers.orders import cb_delete_order_yes

    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "other", "Other", "manager")
    oid = db.create_order(1, "Manager", "")

    # Чужой пользователь не может удалить
    call = _FakeCall(f"ord_delete_yes:{oid}", uid=2)
    asyncio.run(cb_delete_order_yes(call))

    assert asyncio.run(db.get_order(oid))["status"] == "draft"
