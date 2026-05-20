"""
Smoke-тесты WebApp-эндпоинтов одобрения/отклонения заявок на отгрузку.

Покрывают баг, из-за которого кнопка «Одобрить» в WebApp не работала
(раньше слала tg.sendData(), теперь — HTTP на /api/requests/approve).

Через FastAPI TestClient (синхронный — крутит async-эндпоинты сам,
pytest-asyncio не нужен). Замоканы только границы системы:
  - verify_init_data — auth проверяется отдельно в test_auth.py
  - get_notify_bot   — чтобы не звонить в реальный Telegram
  - ms_demand.is_ready=False — чтобы не звонить в реальный МойСклад
БД, роли и переходы статусов — настоящие.
"""

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


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    """Готовит webapp + БД с боссом, менеджером и pending-заявкой.

    Возвращает (TestClient, db, ids-dict, fake_bot).
    """
    import importlib
    import services.roles as roles
    import services.ms_demand as ms_demand
    import webapp.server as server

    importlib.reload(roles)  # сбросить in-memory role-cache между тестами
    db = isolated_db

    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")

    # Заказ менеджера: клиент + позиция, статус pending + заявка
    order_id = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(order_id, "agent-uuid", "Client X")
    db.add_order_item(order_id, "Product A", "", 3, "шт", 100.0)
    db.update_order_status(order_id, "pending")
    req_id = db.create_shipment_request(order_id, mgr_id, "Manager")

    # Границы системы — мок
    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(ms_demand, "is_ready", lambda: False)

    def _fake_verify(init_data: str):
        # initData передаём как строку с telegram id одобряющего
        return {"id": int(init_data), "first_name": "Boss", "username": "boss_user"}

    monkeypatch.setattr(server, "verify_init_data", _fake_verify)

    client = TestClient(server.app)
    ids = {"boss": boss_id, "mgr": mgr_id, "order": order_id, "req": req_id}
    return client, db, ids, fake_bot


def test_approve_request_transitions_status(client_env):
    client, db, ids, fake_bot = client_env

    resp = client.post(
        "/api/requests/approve",
        json={"initData": str(ids["boss"]), "req_id": ids["req"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # Заявка одобрена в БД
    assert db.get_shipment_request(ids["req"])["status"] == "approved"
    # Менеджер уведомлён (notify_order_approved → bot.send_message)
    assert any(kind == "message" for kind, *_ in fake_bot.sent)


def test_reject_request_transitions_status(client_env):
    client, db, ids, fake_bot = client_env

    resp = client.post(
        "/api/requests/reject",
        json={"initData": str(ids["boss"]), "req_id": ids["req"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert db.get_shipment_request(ids["req"])["status"] == "rejected"


def test_double_approve_returns_conflict(client_env):
    client, db, ids, _ = client_env

    first = client.post(
        "/api/requests/approve",
        json={"initData": str(ids["boss"]), "req_id": ids["req"]},
    )
    assert first.status_code == 200
    # Повторный апрув той же заявки — 409 (уже обработана)
    second = client.post(
        "/api/requests/approve",
        json={"initData": str(ids["boss"]), "req_id": ids["req"]},
    )
    assert second.status_code == 409


def test_manager_cannot_approve(client_env, monkeypatch):
    client, db, ids, _ = client_env
    import webapp.server as server

    # Подменяем верификацию: теперь «заходит» менеджер, не босс
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": ids["mgr"], "first_name": "Manager"},
    )
    resp = client.post(
        "/api/requests/approve",
        json={"initData": "x", "req_id": ids["req"]},
    )
    assert resp.status_code == 403
    # Заявка осталась pending
    assert db.get_shipment_request(ids["req"])["status"] == "pending"
