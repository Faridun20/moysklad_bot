"""
Отгрузка без одобрения (решение владельца, сентябрь 2026): менеджер отгружает
заказ сам, руководителю приходит уведомление. На решение руководителя заказ
уходит, только если скидка к прайсу выше порога или долг клиента сверх лимита.

Что держит этот файл (`order_workflow.ship_order_now`, `/api/orders/ship`):
* черновик → shipped одним действием; остаток списан ровно один раз, накладная
  одна, в журнале «оформлена без одобрения»;
* боссам уходит «Заказ #N отгружен» со всеми позициями, суммой, клиентом, кто
  отгрузил, как оплачено / в долг до — без кнопок решения; сбой Telegram
  отгрузку не ломает;
* «оплата сразу» без внесённой оплаты не отгружается и черновик не трогает;
  с разбивкой — оплата и отгрузка вместе;
* скидка выше порога и превышение лимита → отказ `decision_required`, заявка к
  руководителю; до его решения отгрузить нельзя, после — можно;
* двойной клик (два одновременных запроса) не списывает дважды;
* старая заявка без решения не застревает: менеджер отгружает её сам, в
  «Решениях» и очереди руководителя её нет.

БД, роли, склад — настоящие (`isolated_db`); Telegram — заглушка.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

BOSS, BOSS2, MGR, MGR2 = 100, 101, 200, 201


class _Bot:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise RuntimeError("telegram down")
        self.messages.append({"chat_id": chat_id, "text": text, **kw})

    async def send_document(self, chat_id, document, **kw):
        self.documents.append({"chat_id": chat_id, "caption": kw.get("caption")})


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(isolated_db):
    """Два босса, два менеджера, два товара на складе, клиент."""
    import importlib

    import services.roles as roles
    from services import container_receipt, warehouse

    importlib.reload(roles)
    db = isolated_db
    db.set_role(BOSS, "boss", "Boss", "boss")
    db.set_role(BOSS2, "boss2", "Boss Two", "boss")
    db.set_role(MGR, "mgr", "Manager", "manager")
    db.set_role(MGR2, "mgr2", "Manager Two", "manager")

    async def seed():
        wid = await warehouse.default_warehouse_id()
        pids = []
        for name, qty in (("Кабель ВВГ", 10), ("Розетка", 50)):
            pid = (await container_receipt.create_product(name))["product_id"]
            await warehouse.create_invoice(
                invoice_type="incoming", warehouse_id=wid,
                items=[{"product_id": pid, "quantity": qty, "price_cents": None}],
            )
            pids.append(pid)
        return pids

    cable, socket = _run(seed())
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"),
            ("ООО Ромашка", "customer", "", db.now_str()),
        )
        conn.commit()

    def draft(*, uid=MGR, cable_qty=2, cable_price=25.0, socket_qty=3, socket_price=4.5, payment_type="paid"):
        oid = db.create_order(uid, "Manager", "")
        db.update_order_agent(oid, "1", "ООО Ромашка")
        db.add_order_item(oid, "Кабель ВВГ", "", cable_qty, "м", cable_price, product_id=cable)
        if socket_qty:
            db.add_order_item(oid, "Розетка", "", socket_qty, "шт", socket_price, product_id=socket)
        return oid

    return {"db": db, "draft": draft, "cable": cable, "socket": socket}


def _stock(db, pid) -> float:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COALESCE(SUM(quantity), 0) AS q FROM stock WHERE product_id = ?"), (pid,))
        row = cur.fetchone()
    return float(row[0] if not isinstance(row, dict) else row["q"])


def _rows(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return [dict(r) for r in cur.fetchall()]


def _ship(oid, uid=MGR, name="Manager", bot=None, **kw):
    from services.order_workflow import ship_order_now

    async def go():
        res = await ship_order_now(oid, uid, name, bot, **kw)
        task = res.get("notify_task")
        if task is not None:
            await task
        return res

    return _run(go())


def _cash(amount, cur="USD"):
    return {"method": "cash", "currency": cur, "amount": str(amount)}


# ─── Главный путь ────────────────────────────────────────────────────────────


def test_manager_ships_credit_draft_without_approval(env):
    db, cable, socket = env["db"], env["cable"], env["socket"]
    oid = env["draft"]()
    bot = _Bot()

    res = _ship(oid, bot=bot, payment_type="credit", due_date="2099-12-31")

    assert res["ok"], res
    order = _run(db.get_order(oid))
    assert order["status"] == "shipped"
    assert order["payment_type"] == "credit" and order["due_date"] == "2099-12-31"
    assert order["shipped_by"] == MGR and order["shipped_at"]
    # Остаток списан ровно один раз, накладная одна.
    assert _stock(db, cable) == 8 and _stock(db, socket) == 47
    shipments = _rows(db, "SELECT * FROM order_shipment WHERE order_id = ?", (oid,))
    assert len(shipments) == 1 and shipments[0]["invoice_id"]
    assert len(_rows(db, "SELECT id FROM invoices WHERE type = 'outgoing'")) == 1
    # Заявка проведена самим менеджером; в журнале — «без одобрения».
    (req,) = _rows(db, "SELECT * FROM shipment_requests WHERE order_id = ?", (oid,))
    assert req["status"] == "approved" and req["approved_by"] == MGR
    actions = [r["action"] for r in _rows(db, "SELECT action FROM audit_log")]
    assert "shipment_auto_approved" in actions and "shipment_approved" not in actions

    # Боссам — уведомление с позициями, без кнопок решения; себе — нет.
    to_bosses = [m for m in bot.messages if m["chat_id"] in (BOSS, BOSS2)]
    assert {m["chat_id"] for m in to_bosses} == {BOSS, BOSS2}
    text = to_bosses[0]["text"]
    for needle in (
        f"Заказ #{oid} отгружен", "ООО Ромашка", "Отгрузил: <b>Manager</b>",
        "Кабель ВВГ — 2 м × 25 USD = 50 USD", "Розетка — 3 шт × 4,5 USD = 13,5 USD",
        "Итого: 63,5 USD", "В долг до <b>31.12.2099</b>",
    ):
        assert needle in text, (needle, text)
    assert "reply_markup" not in to_bosses[0]
    assert not any(m["chat_id"] == MGR for m in bot.messages)


def test_paid_draft_without_payment_is_refused_and_stays_draft(env):
    db, cable = env["db"], env["cable"]
    oid = env["draft"]()

    res = _ship(oid, payment_type="paid")

    assert not res["ok"] and res["code"] == "payment_required"
    assert res["gap_cents"] == 6350
    assert "сначала введите, как клиент заплатил" in res["error"]
    assert _run(db.get_order(oid))["status"] == "draft"
    assert _rows(db, "SELECT id FROM shipment_requests WHERE order_id = ?", (oid,)) == []
    assert _stock(db, cable) == 10


def test_paid_draft_with_wrong_sum_is_refused_before_anything_moves(env):
    db, cable = env["db"], env["cable"]
    oid = env["draft"]()

    res = _ship(oid, payment_type="paid", parts=[_cash("60")])

    assert not res["ok"] and res["code"] == "short"
    assert _run(db.get_order(oid))["status"] == "draft"
    assert _rows(db, "SELECT id FROM payments WHERE order_id = ?", (oid,)) == []
    assert _stock(db, cable) == 10


def test_paid_draft_ships_together_with_payment(env):
    db, cable = env["db"], env["cable"]
    oid = env["draft"]()
    bot = _Bot()

    res = _ship(oid, bot=bot, payment_type="paid", parts=[_cash("63.50")])

    assert res["ok"], res
    assert _run(db.get_order(oid))["status"] == "shipped"
    assert res["payment"] and res["payment"]["total_cents"] == 6350
    parts = _rows(db, "SELECT method, amount_cents FROM payment_parts WHERE order_id = ?", (oid,))
    assert parts == [{"method": "cash", "amount_cents": 6350}]
    assert _stock(db, cable) == 8
    text = next(m["text"] for m in bot.messages if m["chat_id"] == BOSS)
    assert "Оплата сразу: наличные 63,5 USD" in text


def test_notify_failure_does_not_break_shipment(env):
    db = env["db"]
    oid = env["draft"]()

    res = _ship(oid, bot=_Bot(fail=True), payment_type="credit", due_date="2099-12-31")

    assert res["ok"]
    assert _run(db.get_order(oid))["status"] == "shipped"


def test_insufficient_stock_refuses_and_keeps_draft_editable(env):
    db, cable = env["db"], env["cable"]
    oid = env["draft"](cable_qty=11)

    res = _ship(oid, payment_type="credit", due_date="2099-12-31")

    assert not res["ok"] and res["code"] == "insufficient_stock"
    assert "«Кабель ВВГ»: нужно 11, на складе 10" in res["error"]
    assert _run(db.get_order(oid))["status"] == "draft"
    assert _stock(db, cable) == 10


def test_position_without_catalog_product_is_refused(env):
    db = env["db"]
    oid = env["draft"](socket_qty=0)
    db.add_order_item(oid, "Доставка по городу", "", 1, "усл", 5.0)

    res = _ship(oid, payment_type="credit", due_date="2099-12-31")

    assert not res["ok"] and res["code"] == "unlinked_positions"
    assert "«Доставка по городу»" in res["error"]
    assert _run(db.get_order(oid))["status"] == "draft"


def test_only_author_ships_a_draft(env):
    db = env["db"]
    oid = env["draft"]()

    res = _ship(oid, uid=MGR2, name="Manager Two", payment_type="credit", due_date="2099-12-31")

    assert not res["ok"] and res["http_status"] == 403
    assert _run(db.get_order(oid))["status"] == "draft"


def test_double_click_writes_stock_off_once(env):
    """Два одновременных «Отгрузить» по одному черновику: списание одно."""
    from services.order_workflow import ship_order_now

    db, cable = env["db"], env["cable"]
    oid = env["draft"]()

    async def both():
        return await asyncio.gather(
            ship_order_now(oid, MGR, "Manager", None, payment_type="credit", due_date="2099-12-31"),
            ship_order_now(oid, MGR, "Manager", None, payment_type="credit", due_date="2099-12-31"),
        )

    results = _run(both())
    assert any(r["ok"] for r in results), results
    # Повтор после отгрузки — отказ, остаток тот же.
    again = _ship(oid, payment_type="credit", due_date="2099-12-31")
    assert not again["ok"] and again["code"] == "status"
    assert _run(db.get_order(oid))["status"] == "shipped"
    assert _stock(db, cable) == 8
    assert len(_rows(db, "SELECT id FROM invoices WHERE type = 'outgoing'")) == 1


def test_double_click_with_payment_records_money_once(env):
    from services.order_workflow import ship_order_now

    db, cable = env["db"], env["cable"]
    oid = env["draft"]()

    async def both():
        return await asyncio.gather(*(
            ship_order_now(oid, MGR, "Manager", None, payment_type="paid", parts=[_cash("63.50")])
            for _ in range(2)
        ))

    results = _run(both())
    assert any(r["ok"] for r in results), results
    assert _run(db.get_order(oid))["status"] == "shipped"
    live = _rows(db, "SELECT amount_cents FROM payments WHERE order_id = ? AND status != 'rejected'", (oid,))
    assert sum(r["amount_cents"] for r in live) == 6350
    assert _stock(db, cable) == 8


# ─── Решение руководителя ────────────────────────────────────────────────────


def _raise_price_list(db, pid, price=100.0):
    ok, err = db.set_product_price(str(pid), "Кабель ВВГ", price, None, "USD", updated_by=BOSS)
    assert ok, err


def test_discount_above_threshold_needs_boss_then_manager_ships(env):
    from services.order_workflow import approve_shipment_request, requests_needing_decision, submit_order

    db, cable = env["db"], env["cable"]
    _raise_price_list(db, cable)  # продают по 25 при прайсе 100 — скидка 75%
    oid = env["draft"](socket_qty=0)

    refused = _ship(oid, payment_type="credit", due_date="2099-12-31")
    assert not refused["ok"] and refused["code"] == "decision_required"
    assert "скидка 75% при пороге 15%" in refused["error"]
    assert _run(db.get_order(oid))["status"] == "draft"
    assert _stock(db, cable) == 10

    # Заявка руководителю — и до решения отгрузить нельзя.
    sub = _run(submit_order(oid, MGR, "Manager", payment_type="credit", due_date="2099-12-31"))
    assert sub["ok"]
    still = _ship(oid, payment_type="credit", due_date="2099-12-31")
    assert not still["ok"] and still["code"] == "decision_required"
    assert _run(db.get_order(oid))["status"] == "pending"
    assert [r["order_id"] for r in _run(requests_needing_decision())] == [oid]

    decided = _run(approve_shipment_request(sub["req_id"], BOSS, "Boss", None, discount_ack=True))
    assert decided["ok"], decided
    assert _stock(db, cable) == 8
    shipped = _ship(oid, bot=_Bot())
    assert shipped["ok"], shipped
    assert _run(db.get_order(oid))["status"] == "shipped"
    assert _stock(db, cable) == 8  # одобрение уже списало — второй раз не списывает


def test_credit_over_limit_needs_boss(env):
    db = env["db"]
    db.set_setting("credit_limit_default", 10.0)
    oid = env["draft"]()

    res = _ship(oid, payment_type="credit", due_date="2099-12-31")

    assert not res["ok"] and res["code"] == "decision_required"
    assert "долг клиента станет" in res["error"] and "лимите" in res["error"]
    assert _run(db.get_order(oid))["status"] == "draft"


def test_legacy_pending_request_is_shipped_by_manager_and_leaves_boss_queue(env):
    """Заявка, отправленная «на одобрение» до выката: решение по ней не нужно."""
    from services import work_queue
    from services.order_workflow import requests_needing_decision, submit_order

    db, cable = env["db"], env["cable"]
    oid = env["draft"]()
    sub = _run(submit_order(oid, MGR, "Manager", payment_type="credit", due_date="2099-12-31"))
    assert sub["ok"]
    assert _run(requests_needing_decision()) == []
    queue = _run(work_queue.gather(BOSS, "boss"))
    assert not any(it["key"] == "requests" for it in queue)

    res = _ship(oid, bot=_Bot())

    assert res["ok"], res
    assert _run(db.get_order(oid))["status"] == "shipped"
    assert _stock(db, cable) == 8


def test_boss_ships_pending_manager_order_as_approval(env):
    from services.order_workflow import submit_order

    db = env["db"]
    oid = env["draft"]()
    assert _run(submit_order(oid, MGR, "Manager", payment_type="credit", due_date="2099-12-31"))["ok"]
    bot = _Bot()

    res = _ship(oid, uid=BOSS, name="Boss", bot=bot)

    assert res["ok"], res
    (req,) = _rows(db, "SELECT * FROM shipment_requests WHERE order_id = ?", (oid,))
    assert req["approved_by"] == BOSS
    # Автору — «ваш заказ отгружен», второму боссу — карточка, себе — ничего.
    assert any(m["chat_id"] == MGR and "Ваш заказ" in m["text"] for m in bot.messages)
    assert any(m["chat_id"] == BOSS2 and "отгружен" in m["text"] for m in bot.messages)
    assert not any(m["chat_id"] == BOSS for m in bot.messages)


def test_timeline_says_shipped_without_approval(env):
    from services.order_timeline import build_order_timeline

    oid = env["draft"]()
    assert _ship(oid, payment_type="credit", due_date="2099-12-31")["ok"]

    events = _run(build_order_timeline(oid))
    codes = [e["action"] for e in events]
    assert "shipment_auto_approved" in codes and "order_shipped" in codes
    assert "order_submitted" not in codes and "shipment_requested" not in codes


# ─── HTTP ────────────────────────────────────────────────────────────────────


@pytest.fixture
def client(env, monkeypatch):
    import webapp.server as server

    bot = _Bot()

    async def fake_bot():
        return bot

    monkeypatch.setattr(server, "get_notify_bot", fake_bot)
    monkeypatch.setattr(
        server, "verify_init_data", lambda init: {"id": int(init), "first_name": "U", "username": "u"}
    )
    return TestClient(server.app), bot


def test_http_ship_draft_and_codes(env, client):
    http, _bot = client
    db = env["db"]
    oid = env["draft"]()

    need_pay = http.post("/api/orders/ship", json={"initData": str(MGR), "order_id": oid, "payment_type": "paid"})
    assert need_pay.status_code == 409 and need_pay.json()["code"] == "payment_required"
    assert need_pay.json()["gap_cents"] == 6350

    foreign = http.post("/api/orders/ship", json={
        "initData": str(MGR2), "order_id": oid, "payment_type": "credit", "due_date": "2099-12-31"})
    assert foreign.status_code == 403

    ok = http.post("/api/orders/ship", json={
        "initData": str(MGR), "order_id": oid, "payment_type": "credit", "due_date": "2099-12-31",
        "idempotency_key": "k-1"})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "order_id": oid, "status": "shipped"}
    assert _run(db.get_order(oid))["status"] == "shipped"
    # Повтор тем же ключом — тот же ответ, без второй отгрузки.
    again = http.post("/api/orders/ship", json={
        "initData": str(MGR), "order_id": oid, "payment_type": "credit", "due_date": "2099-12-31",
        "idempotency_key": "k-1"})
    assert again.status_code == 200 and again.json()["ok"] is True
    assert _stock(db, env["cable"]) == 8


def test_http_payment_context_opens_for_draft(env, client):
    http, _bot = client
    oid = env["draft"]()
    ctx = http.post("/api/orders/payment_context", json={"initData": str(MGR), "order_id": oid}).json()
    assert ctx["open"] is True and ctx["with_shipment"] is True
    assert ctx["exact"] is True and ctx["due_cents"] == 6350


def test_http_orders_list_marks_pending_without_decision(env, client):
    from services.order_workflow import submit_order

    http, _bot = client
    db = env["db"]
    _raise_price_list(db, env["socket"])  # розетка по 4.5 при прайсе 100
    plain = env["draft"](socket_qty=0)
    discounted = env["draft"]()
    for oid in (plain, discounted):
        assert _run(submit_order(oid, MGR, "Manager", payment_type="credit", due_date="2099-12-31"))["ok"]

    rows = {o["id"]: o for o in http.post("/api/orders", json={"initData": str(MGR)}).json()["orders"]}
    assert rows[plain]["needs_decision"] is False and rows[plain]["decision_note"] == ""
    assert rows[discounted]["needs_decision"] is True
    assert "скидка 95.5% при пороге 15%" in rows[discounted]["decision_note"]

    boss_page = http.post("/api/orders", json={"initData": str(BOSS), "limit": 50, "offset": 0}).json()
    assert boss_page["pending_count"] == 1
    requests = http.post("/api/orders/requests", json={"initData": str(BOSS)}).json()["requests"]
    assert [r["order_id"] for r in requests] == [discounted]
    assert requests[0]["reasons"][0]["code"] == "discount"


def test_http_decision_required_is_409_with_reasons(env, client):
    http, _bot = client
    db = env["db"]
    _raise_price_list(db, env["cable"])
    oid = env["draft"](socket_qty=0)

    r = http.post("/api/orders/ship", json={
        "initData": str(MGR), "order_id": oid, "payment_type": "credit", "due_date": "2099-12-31"})

    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "decision_required" and body["order_status"] == "draft"
    assert body["reasons"][0]["code"] == "discount"
    assert "отправьте заявку на отгрузку" in body["detail"]
    assert _run(db.get_order(oid))["status"] == "draft"
