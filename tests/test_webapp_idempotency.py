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


# ─── PR A (Tier 1.1): идемпотентность остальных write-endpoint'ов ──────────────


class _FakeBot:
    async def send_message(self, *a, **k):
        return None


def _setup(server, monkeypatch):
    """Сброс глобального idem-кэша + мок границ (verify_init_data, Telegram-бот)."""
    import services.roles as roles

    importlib.reload(roles)
    server._IDEM_CACHE.clear()
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"},
    )

    # Telegram — внешняя граница: не дёргаем реальный бот (иначе unclosed session).
    async def _fake_bot():
        return _FakeBot()

    monkeypatch.setattr(server, "get_notify_bot", _fake_bot)
    return TestClient(server.app)


def _approved_order(db, mgr):
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, "A-1", "Client X")
    db.add_order_item(oid, "Хлеб", "", 2, "шт", 50.0)
    db.update_order_status(oid, "approved")
    return oid


def test_orders_ship_idempotent_returns_cached(isolated_db, monkeypatch):
    """Повтор ship с тем же ключом → кэшированный 200, не 409 «уже обработан»."""
    import webapp.server as server

    db = isolated_db
    boss, mgr = 100, 200
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = _approved_order(db, mgr)

    client = _setup(server, monkeypatch)
    body = {"initData": str(boss), "order_id": oid, "idempotency_key": "ship-1"}
    r1 = client.post("/api/orders/ship", json=body)
    r2 = client.post("/api/orders/ship", json=body)

    assert r1.status_code == 200, r1.text
    assert r1.json()["ok"] is True
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # второй ответ из кэша


def test_orders_ship_without_key_second_conflicts(isolated_db, monkeypatch):
    """Контроль: без ключа второй ship → 409 (идемпотентность реально меняет
    поведение)."""
    import webapp.server as server

    db = isolated_db
    boss, mgr = 101, 201
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = _approved_order(db, mgr)

    client = _setup(server, monkeypatch)
    body = {"initData": str(boss), "order_id": oid}
    r1 = client.post("/api/orders/ship", json=body)
    r2 = client.post("/api/orders/ship", json=body)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 409


def test_deposits_create_idempotent_single_row(isolated_db, monkeypatch):
    """create_cash_deposit не защищён claim'ом — без идемпотентности два POST
    создали бы две сдачи. С ключом — ровно одна."""
    import webapp.server as server

    db = isolated_db
    mgr = 300
    db.set_role(mgr, "m", "Mgr", "manager")

    client = _setup(server, monkeypatch)
    body = {"initData": str(mgr), "amount": 123.0, "idempotency_key": "dep-1"}
    r1 = client.post("/api/deposits/create", json=body)
    r2 = client.post("/api/deposits/create", json=body)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["deposit_id"] == r2.json()["deposit_id"]
    assert len(asyncio.run(db.get_manager_cash_deposits(mgr))) == 1  # дубль НЕ создан
