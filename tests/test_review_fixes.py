"""
Регрессы на баги, найденные при код-ревью:

1. Кнопка «💳 Долги» в меню была мертва: `cb_debts_my` звал
   `cmd_debts(callback.message)`, а тело читало `message.from_user.id` —
   у `callback.message` автор это БОТ, role-check молча проваливался.
   Теперь user_id берётся из `callback.from_user.id`.

2. Возврат наличными писал строку в `cash_deposits` без `amount_cents`
   (нарушение dual-write копеек).
"""

import asyncio



class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "U"


class _FakeMessage:
    def __init__(self, uid):
        self.from_user = _FakeUser(uid)
        self.answers: list[str] = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class _FakeCall:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        # author of callback.message = бот (id, которому роль не задана)
        self.message = _FakeMessage(uid=999999)
        self.alerts: list[str] = []

    async def answer(self, text="", **kw):
        self.alerts.append(text)


def _credit_debt(db, mgr, agent="A"):
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, agent, f"Client {agent}")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    asyncio.run(db.set_order_payment(oid, "credit", "2099-12-31"))
    return oid


def test_refund_cash_writes_amount_cents(isolated_db):
    db = isolated_db
    boss = 1
    db.set_role(boss, "b", "Boss", "boss")
    oid = db.create_order(2, "Mgr", "")
    db.add_order_item(oid, "Товар", "href", 1, "шт", 49.99)  # 4999 коп.
    db.update_order_status(oid, "shipped")

    oitems = asyncio.run(db.get_order_items(oid))
    items = [(oitems[0]["id"], 1, 49.99)]
    res = asyncio.run(db.create_return(oid, "full", "брак", items, "cash", boss))
    assert res["ok"], res

    asyncio.run(db.mark_return_goods_received(res["return_id"], boss))
    cres = asyncio.run(db.confirm_return(res["return_id"], boss, "Boss"))
    assert cres["ok"], cres

    deps = asyncio.run(db.get_manager_cash_deposits(2))  # order.user_id = 2; async (Stage 9)
    refunds = [d for d in deps if (d.get("notes") or "").startswith("refund")]
    assert len(refunds) == 1
    d = refunds[0]
    assert d["amount"] < 0  # выдача из кассы
    assert d["amount_cents"] == -4999  # dual-write копеек присутствует и отриц.
