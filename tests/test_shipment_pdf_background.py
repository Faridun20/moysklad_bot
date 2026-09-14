"""Печатная форма после одобрения — фоном, а не в ответе.

Одобрение заявки = CAS статуса + расходная накладная (транзакция) + PDF +
две отправки в Telegram. Раньше босс ждал всё это в одном вызове: рендер
weasyprint (сотни миллисекунд CPU) и два обращения к Bot API стояли на
критическом пути ответа. Теперь PDF собирается и рассылается фоновой
задачей ПОСЛЕ того, как одобрение и отгрузка уже состоялись; вызывающий
получает `pdf_task` и может его дождаться (тесты) или не ждать (ручка).
"""

from __future__ import annotations

import asyncio

import pytest


class _Bot:
    def __init__(self, *, fail_send: bool = False) -> None:
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.fail_send = fail_send

    async def send_message(self, chat_id, text, **kw):
        self.messages.append({"chat_id": chat_id, "text": text})

    async def send_document(self, chat_id, document, **kw):
        if self.fail_send:
            raise RuntimeError("telegram down")
        self.documents.append({"chat_id": chat_id, "caption": kw.get("caption")})


@pytest.fixture
def approved_env(isolated_db):
    """Заявка менеджера 200 на клиента с товаром на складе, босс 100."""
    from services import container_receipt, warehouse

    db = isolated_db
    db.set_role(100, "boss", "Boss", "boss")
    db.set_role(200, "mgr", "Manager", "manager")
    pid = asyncio.run(container_receipt.create_product("Кабель"))["product_id"]
    wid = asyncio.run(warehouse.default_warehouse_id())
    asyncio.run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"),
            ("Клиент", "customer", "", db.now_str()),
        )
        conn.commit()

    def make_request():
        from services.order_workflow import submit_order

        oid = db.create_order(200, "Manager", "")
        db.update_order_agent(oid, "1", "Клиент")
        db.add_order_item(oid, "Кабель", "", 2, "шт", 5.0, product_id=pid)
        res = asyncio.run(submit_order(oid, 200, "Manager", payment_type="paid", due_date=None))
        assert res["ok"], res
        return oid, res["req_id"]

    return db, make_request


def test_background_mode_answers_before_pdf_and_delivers_later(approved_env):
    from services.order_workflow import approve_shipment_request

    db, make_request = approved_env
    oid, req_id = make_request()
    bot = _Bot()

    async def run():
        res = await approve_shipment_request(req_id, 100, "Boss", bot)
        # Ответ пришёл: одобрено, накладная есть, PDF ещё не отправлен.
        assert res["ok"] and res["invoice_id"]
        assert res["pdf_task"] is not None and not res["pdf_task"].done()
        assert bot.documents == []
        assert "придёт следом" in res["demand_line"]
        delivered = await res["pdf_task"]
        return res, delivered

    res, delivered = asyncio.run(run())
    assert delivered == {"built": True, "sent_to": [200, 100]}
    assert [d["chat_id"] for d in bot.documents] == [200, 100]
    assert all(f"#{req_id}" in d["caption"] for d in bot.documents)
    # Отгрузка состоялась независимо от PDF.
    assert asyncio.run(db.get_order(oid))["status"] == "approved"


def test_inline_mode_still_sends_within_call(approved_env):
    from services.order_workflow import approve_shipment_request

    _db, make_request = approved_env
    _oid, req_id = make_request()
    bot = _Bot()
    res = asyncio.run(approve_shipment_request(req_id, 100, "Boss", bot, pdf_delivery="inline"))
    assert res["ok"] and res["pdf_task"] is None
    assert [d["chat_id"] for d in bot.documents] == [200, 100]
    assert "печатная форма ниже" in res["demand_line"]


def test_pdf_failure_in_background_does_not_touch_approval(approved_env, monkeypatch):
    """Telegram лёг — одобрение и накладная на месте, задача не бросает."""
    from services import order_workflow

    db, make_request = approved_env
    oid, req_id = make_request()
    bot = _Bot(fail_send=True)

    async def run():
        res = await order_workflow.approve_shipment_request(req_id, 100, "Boss", bot)
        return res, await res["pdf_task"]

    res, delivered = asyncio.run(run())
    assert res["ok"]
    assert delivered == {"built": True, "sent_to": []}
    assert asyncio.run(db.get_order(oid))["status"] == "approved"
    from services import order_shipment

    ship = asyncio.run(order_shipment.get_shipment(oid))
    assert ship and ship["invoice_id"]


def test_render_failure_is_logged_not_raised(approved_env, monkeypatch):
    from services import invoice_pdf, order_workflow

    def boom(_invoice):
        raise RuntimeError("weasyprint missing")

    monkeypatch.setattr(invoice_pdf, "render_invoice_pdf", boom)
    _db, make_request = approved_env
    _oid, req_id = make_request()
    bot = _Bot()

    async def run():
        res = await order_workflow.approve_shipment_request(req_id, 100, "Boss", bot)
        return res, await res["pdf_task"]

    res, delivered = asyncio.run(run())
    assert res["ok"]
    assert delivered == {"built": False, "sent_to": []}
    assert bot.documents == []


def test_same_person_gets_one_pdf(approved_env):
    """Босс одобрил свою же заявку — один документ, не два."""
    from services.order_workflow import approve_shipment_request

    db, make_request = approved_env
    db.set_role(200, "mgr", "Manager", "boss")
    _oid, req_id = make_request()
    bot = _Bot()

    async def run():
        res = await approve_shipment_request(req_id, 200, "Manager", bot)
        return await res["pdf_task"]

    delivered = asyncio.run(run())
    assert delivered["sent_to"] == [200]
    assert len(bot.documents) == 1


def test_background_spawn_keeps_reference_and_logs_failure(caplog):
    from utils import background

    async def ok():
        return 1

    async def bad():
        raise ValueError("boom")

    async def run():
        t1 = background.spawn(ok(), name="ok")
        t2 = background.spawn(bad(), name="bad")
        assert t1 in background.pending() or t1.done()
        await asyncio.gather(t1, t2, return_exceptions=True)
        await asyncio.sleep(0)
        return t1.result()

    assert asyncio.run(run()) == 1
    assert any("Фоновая задача bad упала" in r.getMessage() for r in caplog.records)
    assert not background.pending()
