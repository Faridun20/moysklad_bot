"""
Бот-команды списка «на подтверждении» для ролей склад/бухгалтер.

Раньше сдачи/возвраты подтверждались только из push-уведомления (если оно
потерялось — очередь не найти). Теперь /deposits (бухгалтер) и /returns
(склад) показывают очередь с теми же кнопками подтверждения.
"""

import asyncio


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "U"


class _FakeMessage:
    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


def _all_cbs(answers):
    out = []
    for _t, kw in answers:
        markup = kw.get("reply_markup")
        if not markup:
            continue
        for row in markup.inline_keyboard:
            for b in row:
                if b.callback_data:
                    out.append(b.callback_data)
    return out


def test_pending_deposits_list_for_bookkeeper(isolated_db):
    db = isolated_db
    db.set_role(1, "bk", "Book", "bookkeeper")
    res = db.create_cash_deposit(2, 500.0)  # сдаёт менеджер #2
    assert res["ok"]

    from handlers.deposits import cmd_pending_deposits

    m = _FakeMessage(1)
    asyncio.run(cmd_pending_deposits(m))

    assert any("Сдача" in t for t, _ in m.answers)
    cbs = _all_cbs(m.answers)
    assert any(c.startswith("dep_ok:") for c in cbs)
    assert any(c.startswith("dep_no:") for c in cbs)


def test_pending_deposits_denied_for_manager(isolated_db):
    db = isolated_db
    db.set_role(3, "mgr", "M", "manager")
    from handlers.deposits import cmd_pending_deposits

    m = _FakeMessage(3)
    asyncio.run(cmd_pending_deposits(m))
    assert any("доступ" in t.lower() or "босс" in t.lower() for t, _ in m.answers)


def test_pending_returns_list_for_warehouse(isolated_db):
    db = isolated_db
    db.set_role(1, "wh", "WH", "warehouse_keeper")
    oid = db.create_order(2, "M", "")
    iid = db.add_order_item(oid, "P", "href", 1, "шт", 100.0)
    db.update_order_status(oid, "shipped")
    res = db.create_return(
        oid, "full", "брак партии", [(iid, 1, 100.0)],
        refund_method="cash", created_by=2, force=True,
    )
    assert res["ok"]

    from handlers.returns import cmd_pending_returns

    m = _FakeMessage(1)
    asyncio.run(cmd_pending_returns(m))

    cbs = _all_cbs(m.answers)
    assert any(c.startswith("ret_ok:") for c in cbs)
    assert any(c.startswith("ret_got:") for c in cbs)


def test_pending_lists_empty(isolated_db):
    db = isolated_db
    db.set_role(1, "bk", "Book", "bookkeeper")
    db.set_role(2, "wh", "WH", "warehouse_keeper")
    from handlers.deposits import cmd_pending_deposits
    from handlers.returns import cmd_pending_returns

    md = _FakeMessage(1)
    asyncio.run(cmd_pending_deposits(md))
    assert any("Нет сдач" in t for t, _ in md.answers)

    mr = _FakeMessage(2)
    asyncio.run(cmd_pending_returns(mr))
    assert any("Нет возвратов" in t for t, _ in mr.answers)
