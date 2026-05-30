"""
Тесты «вернуть заявку на доработку» (reject→draft) + freeze + unfreeze — #22.

Подключение ранее мёртвой фичи reject_order_to_draft к UI: босс возвращает
заявку в черновик с причиной, после reject_max_cycles=3 заказ замораживается,
переотправка блокируется до разморозки админом.

Конвенция CLAUDE.md: настоящая БД (isolated_db), мок только границы — Telegram
(_FakeBot) и get_notify_bot/ms_demand. Сервис + БД + переходы статусов — реальные.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


class _FakeBot:
    """Заглушка aiogram.Bot — копит вызовы вместо реальной отправки."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, *a, **k):
        self.sent.append(("message", a, k))

    async def send_document(self, *a, **k):
        self.sent.append(("document", a, k))


def _mk_pending_order_with_request(db, mgr_id: int, agent: str = "A-1"):
    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, agent, "Client")
    db.add_order_item(oid, "Product", "", 2, "шт", 100.0)
    db.update_order_status(oid, "pending")
    req_id = db.create_shipment_request(oid, mgr_id, "Manager")
    return oid, req_id


# ─── Сервис: цикл reject→draft→freeze ────────────────────────────────────────


def test_return_order_to_draft_resolves_request_and_notifies(isolated_db):
    db = isolated_db
    mgr_id = 200
    db.set_role(mgr_id, "mgr", "Manager", "manager")
    oid, req_id = _mk_pending_order_with_request(db, mgr_id)

    bot = _FakeBot()
    from services.order_workflow import return_order_to_draft

    res = asyncio.run(return_order_to_draft(req_id, 100, "Boss", "доработай позиции", bot))
    assert res["ok"] is True
    assert res["frozen"] is False
    assert res["rejection_count"] == 1

    order = asyncio.run(db.get_order(oid))
    assert order["status"] == "draft"
    assert order["rejection_comment"] == "доработай позиции"

    # Заявка ушла из pending-очереди со статусом 'returned'.
    assert db.get_shipment_request(req_id)["status"] == "returned"
    pending_ids = {r["id"] for r in asyncio.run(db.get_pending_requests())}
    assert req_id not in pending_ids

    # Менеджер уведомлён.
    assert any(kind == "message" for kind, *_ in bot.sent)


def test_freeze_after_three_returns(isolated_db):
    db = isolated_db
    mgr_id = 201
    db.set_role(mgr_id, "mgr", "Manager", "manager")
    oid, req_id = _mk_pending_order_with_request(db, mgr_id, agent="A-2")

    bot = _FakeBot()
    from services.order_workflow import return_order_to_draft

    # Циклы 1–2: не заморожен. Между циклами возвращаем pending + новая заявка.
    for i in range(2):
        res = asyncio.run(return_order_to_draft(req_id, 100, "Boss", f"правка {i}", bot))
        assert res["ok"] is True and res["frozen"] is False
        db.update_order_status(oid, "pending")
        req_id = db.create_shipment_request(oid, mgr_id, "Manager")

    # Цикл 3: заморозка (reject_max_cycles=3).
    res3 = asyncio.run(return_order_to_draft(req_id, 100, "Boss", "третий", bot))
    assert res3["rejection_count"] == 3
    assert res3["frozen"] is True
    assert asyncio.run(db.get_order(oid))["frozen"] == 1


def test_return_to_draft_only_from_pending(isolated_db):
    db = isolated_db
    mgr_id = 202
    db.set_role(mgr_id, "mgr", "Manager", "manager")
    oid, req_id = _mk_pending_order_with_request(db, mgr_id, agent="A-3")

    bot = _FakeBot()
    from services.order_workflow import return_order_to_draft

    asyncio.run(return_order_to_draft(req_id, 100, "Boss", "правка", bot))
    # Заказ уже в draft → повторная попытка по той же заявке отвергается.
    res = asyncio.run(return_order_to_draft(req_id, 100, "Boss", "ещё раз", bot))
    assert res["ok"] is False


# ─── unfreeze + список замороженных ──────────────────────────────────────────


def test_unfreeze_clears_flag_and_counter(isolated_db):
    db = isolated_db
    mgr_id = 203
    db.set_role(mgr_id, "mgr", "Manager", "manager")
    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, "A-4", "Client")
    db.add_order_item(oid, "Product", "", 1, "шт", 50.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status='draft', frozen=1, rejection_count=3 WHERE id=?"),
            (oid,),
        )
        conn.commit()

    res = asyncio.run(db.unfreeze_order(oid, 1, "Admin"))
    assert res["ok"] is True
    order = asyncio.run(db.get_order(oid))
    assert order["frozen"] == 0
    assert order["rejection_count"] == 0
    # Повторная разморозка незаморожённого — ошибка.
    assert asyncio.run(db.unfreeze_order(oid, 1, "Admin"))["ok"] is False


def test_get_frozen_orders_lists_only_frozen(isolated_db):
    db = isolated_db
    db.set_role(300, "mgr", "Manager", "manager")
    a = db.create_order(300, "M", "")
    b = db.create_order(300, "M", "")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET frozen=1 WHERE id=?"), (a,))
        conn.commit()
    ids = {o["id"] for o in asyncio.run(db.get_frozen_orders())}
    assert a in ids
    assert b not in ids


# ─── WebApp endpoints ────────────────────────────────────────────────────────


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    """webapp + БД с боссом/менеджером/админом и pending-заявкой."""
    import importlib
    import services.roles as roles
    import services.ms_demand as ms_demand
    import webapp.server as server

    importlib.reload(roles)  # сбросить in-memory role-cache между тестами
    db = isolated_db

    boss_id, mgr_id, admin_id = 100, 200, 500
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")
    db.set_role(admin_id, "admin_user", "Admin", "admin")

    order_id = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(order_id, "agent-uuid", "Client X")
    db.add_order_item(order_id, "Product A", "", 3, "шт", 100.0)
    db.update_order_status(order_id, "pending")
    req_id = db.create_shipment_request(order_id, mgr_id, "Manager")

    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(ms_demand, "is_ready", lambda: False)

    _by_id = {
        boss_id: ("Boss", "boss_user"),
        mgr_id: ("Manager", "mgr_user"),
        admin_id: ("Admin", "admin_user"),
    }

    def _fake_verify(init_data: str):
        uid = int(init_data)
        first, uname = _by_id.get(uid, ("User", "user"))
        return {"id": uid, "first_name": first, "username": uname}

    monkeypatch.setattr(server, "verify_init_data", _fake_verify)

    client = TestClient(server.app)
    ids = {"boss": boss_id, "mgr": mgr_id, "admin": admin_id, "order": order_id, "req": req_id}
    return client, db, ids, fake_bot


def test_api_return_to_draft_happy(client_env):
    client, db, ids, fake_bot = client_env
    resp = client.post(
        "/api/requests/return_to_draft",
        json={"initData": str(ids["boss"]), "req_id": ids["req"], "comment": "доработай позиции"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["frozen"] is False
    assert db.get_shipment_request(ids["req"])["status"] == "returned"
    assert asyncio.run(db.get_order(ids["order"]))["status"] == "draft"
    assert any(kind == "message" for kind, *_ in fake_bot.sent)


def test_api_return_to_draft_requires_reason(client_env):
    client, db, ids, _ = client_env
    resp = client.post(
        "/api/requests/return_to_draft",
        json={"initData": str(ids["boss"]), "req_id": ids["req"], "comment": "x"},
    )
    assert resp.status_code == 400
    # Заявка осталась pending.
    assert db.get_shipment_request(ids["req"])["status"] == "pending"


def test_api_manager_cannot_return_to_draft(client_env):
    client, db, ids, _ = client_env
    resp = client.post(
        "/api/requests/return_to_draft",
        json={"initData": str(ids["mgr"]), "req_id": ids["req"], "comment": "доработай"},
    )
    assert resp.status_code == 403
    assert db.get_shipment_request(ids["req"])["status"] == "pending"


def test_api_frozen_order_cannot_be_submitted(client_env):
    client, db, ids, _ = client_env
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status='draft', frozen=1 WHERE id=?"), (ids["order"],)
        )
        conn.commit()
    resp = client.post(
        "/api/orders/submit",
        json={"initData": str(ids["mgr"]), "order_id": ids["order"]},
    )
    assert resp.status_code == 409, resp.text


def test_api_unfreeze_admin_only(client_env):
    client, db, ids, _ = client_env
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status='draft', frozen=1, rejection_count=3 WHERE id=?"),
            (ids["order"],),
        )
        conn.commit()

    # Менеджер и босс — не админ → 403.
    assert (
        client.post(
            "/api/orders/unfreeze",
            json={"initData": str(ids["mgr"]), "order_id": ids["order"]},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/orders/unfreeze",
            json={"initData": str(ids["boss"]), "order_id": ids["order"]},
        ).status_code
        == 403
    )

    # Админ — ок, флаг снят.
    ok = client.post(
        "/api/orders/unfreeze",
        json={"initData": str(ids["admin"]), "order_id": ids["order"]},
    )
    assert ok.status_code == 200, ok.text
    assert asyncio.run(db.get_order(ids["order"]))["frozen"] == 0
