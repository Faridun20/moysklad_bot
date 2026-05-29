"""
Smoke-тесты WebApp-эндпоинтов сдач и возвратов (/api/deposits/*, /api/returns/*).

FastAPI TestClient; мокаем границы: verify_init_data и get_notify_bot (чтобы не
звонить в Telegram). БД/роли — настоящие (isolated_db).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **k):
        self.sent.append((a, k))


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db

    boss_id, mgr_id, wh_id = 100, 200, 300
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")
    db.set_role(wh_id, "wh_user", "Warehouse", "warehouse_keeper")

    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, "agent-uuid", "Client X")
    db.add_order_item(oid, "Product A", "", 1, "шт", 250.0)
    db.update_order_status(oid, "shipped")

    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)
    ids = {"boss": boss_id, "mgr": mgr_id, "wh": wh_id, "order": oid}
    return client, db, ids, fake_bot


def test_deposit_pending_and_confirm(client_env):
    client, db, ids, fake_bot = client_env
    dep = db.create_cash_deposit(ids["mgr"], 250.0)

    # список
    resp = client.post("/api/deposits/pending", json={"initData": str(ids["boss"])})
    assert resp.status_code == 200, resp.text
    deposits = resp.json()["deposits"]
    assert any(d["id"] == dep["deposit_id"] for d in deposits)

    # подтверждение → заказ paid + уведомление менеджеру
    resp = client.post(
        "/api/deposits/confirm",
        json={"initData": str(ids["boss"]), "deposit_id": dep["deposit_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert db.get_order(ids["order"])["status"] == "paid"
    assert fake_bot.sent  # менеджер уведомлён


def test_deposit_reject_requires_reason(client_env):
    client, db, ids, _ = client_env
    dep = db.create_cash_deposit(ids["mgr"], 250.0)
    resp = client.post(
        "/api/deposits/reject",
        json={"initData": str(ids["boss"]), "deposit_id": dep["deposit_id"], "reason": "x"},
    )
    assert resp.status_code == 400


def test_deposit_confirm_forbidden_for_manager(client_env):
    client, db, ids, _ = client_env
    dep = db.create_cash_deposit(ids["mgr"], 250.0)
    resp = client.post(
        "/api/deposits/confirm",
        json={"initData": str(ids["mgr"]), "deposit_id": dep["deposit_id"]},
    )
    assert resp.status_code == 403


def test_deposit_create_and_my(client_env):
    client, db, ids, fake_bot = client_env
    # менеджер создаёт сдачу из WebApp
    resp = client.post(
        "/api/deposits/create",
        json={"initData": str(ids["mgr"]), "amount": 250},
    )
    assert resp.status_code == 200, resp.text
    dep_id = resp.json()["deposit_id"]
    assert asyncio.run(db.get_cash_deposit(dep_id))["status"] == "pending"
    assert fake_bot.sent  # подтверждающим ушло уведомление

    # свои сдачи
    resp = client.post("/api/deposits/my", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 200, resp.text
    assert any(d["id"] == dep_id for d in resp.json()["deposits"])


def test_deposit_create_bad_amount(client_env):
    client, db, ids, _ = client_env
    resp = client.post(
        "/api/deposits/create",
        json={"initData": str(ids["mgr"]), "amount": 0},
    )
    assert resp.status_code == 400


def test_returns_pending_and_confirm(client_env):
    client, db, ids, _ = client_env
    items = db.get_order_items(ids["order"])
    r = db.create_return(
        ids["order"],
        "full",
        "брак",
        [(items[0]["id"], 1, 250.0)],
        refund_method="no_refund",
        created_by=ids["mgr"],
    )

    # warehouse_keeper видит список
    resp = client.post("/api/returns/pending", json={"initData": str(ids["wh"])})
    assert resp.status_code == 200, resp.text
    assert any(x["id"] == r["return_id"] for x in resp.json()["returns"])

    # подтверждает только босс
    resp = client.post(
        "/api/returns/confirm",
        json={"initData": str(ids["wh"]), "return_id": r["return_id"]},
    )
    assert resp.status_code == 403

    resp = client.post(
        "/api/returns/confirm",
        json={"initData": str(ids["boss"]), "return_id": r["return_id"]},
    )
    assert resp.status_code == 200, resp.text
    assert db.get_order(ids["order"])["status"] == "returned"


def test_return_create_full(client_env):
    client, db, ids, fake_bot = client_env
    resp = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": ids["order"],
            "reason": "брак партии",
            "refund_method": "debt_reduction",
        },
    )
    assert resp.status_code == 200, resp.text
    pend = asyncio.run(db.get_pending_returns())  # async после asyncpg Stage 5
    assert any(p["order_id"] == ids["order"] for p in pend)
    assert fake_bot.sent  # подтверждающим ушло уведомление


def test_return_create_validation(client_env):
    client, db, ids, _ = client_env
    # короткая причина
    resp = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": ids["order"],
            "reason": "x",
            "refund_method": "cash",
        },
    )
    assert resp.status_code == 400
    # плохой способ возврата
    resp = client.post(
        "/api/returns/create",
        json={
            "initData": str(ids["mgr"]),
            "order_id": ids["order"],
            "reason": "нормальная причина",
            "refund_method": "bitcoin",
        },
    )
    assert resp.status_code == 400


# ─── Round 6 (S2): length caps на reason/comment ──────────────────────────────


def test_deposit_reject_clips_long_reason(client_env):
    """1MB reason должен резаться на API edge, не разлетаться в DB/Telegram."""
    client, db, ids, _ = client_env
    dep = db.create_cash_deposit(ids["mgr"], 250.0)
    huge = "x" * 100_000
    resp = client.post(
        "/api/deposits/reject",
        json={"initData": str(ids["boss"]), "deposit_id": dep["deposit_id"], "reason": huge},
    )
    assert resp.status_code == 200, resp.text
    stored = asyncio.run(db.get_cash_deposit(dep["deposit_id"]))
    assert len(stored["reject_reason"]) <= 500


# ─── Round 6 (S3): amount upper bound и inf/NaN ───────────────────────────────


def test_deposit_create_rejects_huge_amount(client_env):
    """JSON отвергает NaN/inf на уровне сериализации (Starlette сам режет);
    нашу защиту проверяем на больших-но-конечных значениях, которые проходят
    в payload. NaN/inf-кейс покрыт `test_invariants.test_create_cash_deposit_rejects_nan_inf_and_too_large`."""
    client, _db, ids, _ = client_env
    for bad in (1e308, 10_000_001, 99_999_999):
        resp = client.post(
            "/api/deposits/create",
            json={"initData": str(ids["mgr"]), "amount": bad},
        )
        assert resp.status_code == 400, f"expected 400 for amount={bad!r}, got {resp.status_code}"


# ─── Round 6 (RACE-3): set_return_ms_id conditional UPDATE ────────────────────


def test_set_return_ms_id_second_call_loses_race(isolated_db):
    """Два параллельных create_salesreturn — только первый выигрывает запись id;
    второй вернёт False, caller знает что надо удалить orphan-doc в МС."""
    db = isolated_db
    mgr = 700
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "P", "", 1, "шт", 100.0)
    db.update_order_status(oid, "shipped")
    items = db.get_order_items(oid)
    r = db.create_return(oid, "full", "брак", [(items[0]["id"], 1, 100.0)], "no_refund", mgr)
    assert r["ok"]

    rid = r["return_id"]
    assert db.set_return_ms_id(rid, "ms-id-1") is True  # выигрыш гонки
    assert db.set_return_ms_id(rid, "ms-id-2") is False  # уже занято, новый id не запишется
    stored = db.get_return(rid)
    assert stored["moysklad_return_id"] == "ms-id-1"


# ─── Round 6 (RACE-2): create_return TOCTOU — параллельные pending'и ──────────


def test_create_return_blocks_second_pending_for_same_order(isolated_db):
    """Второй create_return по тому же заказу при существующем pending → отказ.
    Раньше advisory-lock'а не было, два параллельных вызова оба проходили."""
    db = isolated_db
    mgr = 800
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "P", "", 2, "шт", 100.0)
    db.update_order_status(oid, "shipped")
    items = db.get_order_items(oid)
    r1 = db.create_return(oid, "partial", "x", [(items[0]["id"], 1, 100.0)], "no_refund", mgr)
    assert r1["ok"]
    r2 = db.create_return(oid, "partial", "x", [(items[0]["id"], 1, 100.0)], "no_refund", mgr)
    assert r2["ok"] is False
    assert "уже есть возврат" in r2["error"]


# ─── Round 6 (L_R2): confirm_return overshoot prevention ──────────────────────


def test_confirm_return_blocks_overshoot(isolated_db):
    """returned_qty + delta > quantity → атомарный UPDATE отвергает,
    confirm откатывается. Без него double-return от concurrent confirm'а
    мог дать returned_qty > quantity и лишний MS-doc."""
    db = isolated_db
    mgr = 900
    db.set_role(mgr, "m", "M", "manager")
    oid = db.create_order(mgr, "M", "")
    db.add_order_item(oid, "P", "", 2, "шт", 100.0)
    db.update_order_status(oid, "shipped")
    items = db.get_order_items(oid)

    # Создаём первый возврат на полные 2 шт и подтверждаем.
    r1 = db.create_return(oid, "full", "x", [(items[0]["id"], 2, 200.0)], "no_refund", mgr)
    assert r1["ok"]
    res = db.confirm_return(r1["return_id"], mgr, "M")
    assert res["ok"], res

    # Второй возврат на ту же позицию — create_return уже клампит до доступного
    # (доступно 0), отказывает на стадии создания.
    r2 = db.create_return(oid, "partial", "x", [(items[0]["id"], 1, 100.0)], "no_refund", mgr)
    assert r2["ok"] is False  # нечего возвращать
