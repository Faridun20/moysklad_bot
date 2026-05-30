"""
Идемпотентность денежных WebApp-эндпоинтов.

mark_paid по частичной сумме без ключа мог при double-click создать два
платежа. Теперь фронт шлёт idempotency_key, а сервер дедуплицирует повтор.

Мокаем границы: verify_init_data и _notify_bosses_payment_pending (чтобы не
звонить в Telegram). БД/роли настоящие (isolated_db).
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db

    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")

    oid = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(oid, "agent-uuid", "Client X")
    db.add_order_item(oid, "Product A", "", 1, "шт", 250.0)
    db.update_order_status(oid, "shipped")
    asyncio.run(db.set_order_payment(oid, "credit", "2099-12-31"))

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(server, "_notify_bosses_payment_pending", _noop)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    server._IDEM_CACHE.clear()  # чистим глобальный кэш между тестами
    client = TestClient(server.app)
    return client, db, {"boss": boss_id, "mgr": mgr_id, "order": oid}


def test_mark_paid_idempotent_double_click(client_env):
    client, db, ids = client_env
    body = {
        "initData": str(ids["mgr"]),
        "order_id": ids["order"],
        "amount": 100,
        "idempotency_key": "K1",
    }
    r1 = client.post("/api/orders/mark_paid", json=body)
    assert r1.status_code == 200, r1.text
    pid1 = r1.json()["payment_id"]

    # Повтор с тем же ключом → тот же результат, второго платежа нет.
    r2 = client.post("/api/orders/mark_paid", json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json()["payment_id"] == pid1
    assert len(asyncio.run(db.get_payments_for_order(ids["order"]))) == 1


def test_mark_paid_distinct_keys_create_two(client_env):
    client, db, ids = client_env
    base = {"initData": str(ids["mgr"]), "order_id": ids["order"], "amount": 50}
    client.post("/api/orders/mark_paid", json={**base, "idempotency_key": "A"})
    client.post("/api/orders/mark_paid", json={**base, "idempotency_key": "B"})
    # Разные ключи = разные намерения → два платежа.
    assert len(asyncio.run(db.get_payments_for_order(ids["order"]))) == 2
