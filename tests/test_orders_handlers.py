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


def _items_count(db, oid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM order_items WHERE order_id = ?"), (oid,))
        return cur.fetchone()[0]


def test_delete_yes_actually_deletes(isolated_db):
    """T2.7: удаление из бота физически сносит заказ и его позиции.

    Раньше бот просто ставил status='rejected' и оставлял строку, хотя в
    аудит писал «Удалён черновик». Из-за этого 'rejected' означал две разные
    вещи (отклонён боссом / удалён менеджером), и «удалённые» черновики
    продолжали попадать в выборки аналитики (§5.2.1).
    """
    db = isolated_db
    from handlers.orders import cb_delete_order_yes

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")
    db.add_order_item(oid, "Товар", "href", 1, "шт", 100.0)
    assert _items_count(db, oid) == 1

    call = _FakeCall(f"ord_delete_yes:{oid}", uid=1)
    asyncio.run(cb_delete_order_yes(call))

    assert asyncio.run(db.get_order(oid)) is None, "строка заказа должна быть удалена"
    assert _items_count(db, oid) == 0, "позиции должны удалиться каскадом"


def test_delete_yes_refuses_when_payments_exist(isolated_db):
    """Паритет с WebApp: черновик с платежами не удаляем — orphan-платёж потом
    подтвердился бы в кассу против несуществующего заказа."""
    db = isolated_db
    from handlers.orders import cb_delete_order_yes

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")
    db.add_order_item(oid, "Товар", "href", 1, "шт", 100.0)
    db.add_payment(1, "u", "Manager", 50.0, "USD", "аванс", order_id=oid)

    call = _FakeCall(f"ord_delete_yes:{oid}", uid=1)
    asyncio.run(cb_delete_order_yes(call))

    assert asyncio.run(db.get_order(oid)) is not None, "заказ с платежами не удаляем"
    assert _items_count(db, oid) == 1
    assert "Не удалось удалить" in " ".join(t for t, _ in call.message.answers)


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
