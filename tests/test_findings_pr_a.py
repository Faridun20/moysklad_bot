"""
PR A находок: esc agent-name (#26), мульти-валютные итоги *_base (#27),
webapp goods_received (#28).

Реальная БД (isolated_db), мок только границы (Telegram-объекты / verify_init_data).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import services.roles as roles


# ─── фейки бот-объектов ──────────────────────────────────────────────────────


class _FakeUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "U"


class _FakeChat:
    id = 1


class _FakeMessage:
    def __init__(self, uid=1):
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append((text, kw))


class _FakeCall:
    def __init__(self, data, uid):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeMessage(uid)

    async def answer(self, text="", **kw):
        pass


class _FakeState:
    def __init__(self, data):
        self._data = data

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._data = {}


# ─── #26: esc имени контрагента ──────────────────────────────────────────────


def test_agent_pick_escapes_html_in_name(isolated_db):
    from handlers.orders import cb_agent_pick

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "M", "manager")
    oid = db.create_order(1, "M", "")
    state = _FakeState({"agents": [{"id": "a1", "name": "Evil <script> & Co"}]})
    call = _FakeCall(f"agent_pick:{oid}:0", uid=1)

    asyncio.run(cb_agent_pick(call, state))
    txt = call.message.answers[-1][0]
    # Имя экранировано — сырой тег не попал в HTML-сообщение.
    assert "&lt;script&gt;" in txt
    assert "<script>" not in txt


# ─── #27: *_base в сводке ────────────────────────────────────────────────────


def test_payment_summary_base_none_without_rate(isolated_db):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET currency = 'UZS' WHERE id = ?"), (oid,))
        conn.commit()
    db.add_order_item(oid, "P", "", 1, "шт", 1000.0)

    s = asyncio.run(db.get_order_payment_summary(oid))
    assert s["total"] == 1000.0
    assert s["currency"] == "UZS"
    assert s["total_base"] is None  # курс UZS не задан → нет пересчёта


def test_payment_summary_base_set_with_rate(isolated_db):
    db = isolated_db
    db._invalidate_currency_rates_cache()
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET currency = 'UZS' WHERE id = ?"), (oid,))
        conn.commit()
    db.add_order_item(oid, "P", "", 1, "шт", 1000.0)
    db.set_currency_rate("UZS", 0.00008, 1)
    db._invalidate_currency_rates_cache()

    s = asyncio.run(db.get_order_payment_summary(oid))
    assert s["base_currency"] == "USD"
    assert s["total_base"] == pytest.approx(0.08)  # 1000 * 0.00008


# ─── #28: webapp goods_received ──────────────────────────────────────────────


@pytest.fixture
def rclient(isolated_db, monkeypatch):
    import importlib
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    boss, wh, mgr = 100, 300, 200
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(wh, "w", "WH", "warehouse_keeper")
    db.set_role(mgr, "m", "Mgr", "manager")

    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "P", "", 1, "шт", 100.0)
    db.update_order_status(oid, "shipped")

    def _ins_return(status):
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(
                db.q(
                    "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
                    "created_by, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                ),
                (oid, "partial", "x", 5000, mgr, status, db.now_str()),
            )
            rid = cur.lastrowid
            conn.commit()
        return rid

    monkeypatch.setattr(
        server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"}
    )
    client = TestClient(server.app)
    ids = {"boss": boss, "wh": wh, "mgr": mgr}
    return client, db, ids, _ins_return


def test_goods_received_marks_flag(rclient):
    client, db, ids, mk = rclient
    rid = mk("pending")
    r = client.post(
        "/api/returns/goods_received", json={"initData": str(ids["wh"]), "return_id": rid}
    )
    assert r.status_code == 200, r.text
    row = asyncio.run(db.get_return(rid))
    assert row["goods_received"] == 1


def test_goods_received_forbidden_for_manager(rclient):
    client, db, ids, mk = rclient
    rid = mk("pending")
    r = client.post(
        "/api/returns/goods_received", json={"initData": str(ids["mgr"]), "return_id": rid}
    )
    assert r.status_code == 403


def test_goods_received_409_when_not_pending(rclient):
    client, db, ids, mk = rclient
    rid = mk("confirmed")  # не pending → нечего отмечать
    r = client.post(
        "/api/returns/goods_received", json={"initData": str(ids["boss"]), "return_id": rid}
    )
    assert r.status_code == 409
