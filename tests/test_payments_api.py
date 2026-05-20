"""
Smoke-тесты уведомлений о подтверждении/отклонении оплаты через WebApp.

Покрывают Bug 1: api_confirm_payment подтверждал платежи, но НЕ уведомлял
менеджера (в отличие от bot-флоу). Тест проверяет, что после confirm/reject
владельцу каждого pending-платежа уходит сообщение.

Замокан tg_send_message (граница с Telegram). БД и переходы — настоящие.
"""

import pytest

from fastapi.testclient import TestClient


@pytest.fixture
def pay_env(isolated_db, monkeypatch):
    """Босс + менеджер + credit-заказ с двумя pending-платежами.

    Возвращает (client, db, ids, sent) где sent — список (chat_id, text)
    перехваченных уведомлений."""
    import importlib
    import services.roles as roles
    import services.notifier as notifier
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db

    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")

    order_id = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(order_id, "agent-uuid", "Client X")
    db.add_order_item(order_id, "Product A", "", 4, "шт", 100.0)
    db.set_order_payment(order_id, "credit", "2026-12-31")
    db.update_order_status(order_id, "approved")
    # Два pending-платежа от менеджера по этому заказу
    db.add_payment(mgr_id, "@mgr", "Manager", 100.0, "USD", "первый", order_id=order_id)
    db.add_payment(mgr_id, "@mgr", "Manager", 300.0, "USD", "второй", order_id=order_id)

    sent: list[tuple] = []

    async def _fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text))

    monkeypatch.setattr(notifier, "tg_send_message", _fake_send)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "Boss"},
    )

    client = TestClient(server.app)
    ids = {"boss": boss_id, "mgr": mgr_id, "order": order_id}
    return client, db, ids, sent


def test_confirm_payment_notifies_manager(pay_env):
    client, db, ids, sent = pay_env

    resp = client.post(
        "/api/orders/confirm_payment",
        json={"initData": str(ids["boss"]), "order_id": ids["order"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["confirmed_count"] == 2

    # Оба платежа подтверждены в БД
    statuses = [p["status"] for p in db.get_payments_for_order(ids["order"])]
    assert statuses == ["confirmed", "confirmed"]

    # Менеджер получил уведомление по каждому платежу
    assert len(sent) == 2
    assert all(chat_id == ids["mgr"] for chat_id, _ in sent)
    assert all("принят" in text.lower() for _, text in sent)


def test_reject_payment_notifies_manager(pay_env):
    client, db, ids, sent = pay_env

    resp = client.post(
        "/api/orders/reject_payment",
        json={"initData": str(ids["boss"]), "order_id": ids["order"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["rejected_count"] == 2

    statuses = [p["status"] for p in db.get_payments_for_order(ids["order"])]
    assert statuses == ["rejected", "rejected"]

    assert len(sent) == 2
    assert all(chat_id == ids["mgr"] for chat_id, _ in sent)
    assert all("отклон" in text.lower() for _, text in sent)


def test_confirm_with_no_pending_sends_nothing(pay_env):
    client, db, ids, sent = pay_env

    # Сначала подтверждаем — pending не остаётся
    client.post(
        "/api/orders/confirm_payment",
        json={"initData": str(ids["boss"]), "order_id": ids["order"]},
    )
    sent.clear()
    # Повторный confirm: подтверждать нечего → 0 уведомлений
    resp = client.post(
        "/api/orders/confirm_payment",
        json={"initData": str(ids["boss"]), "order_id": ids["order"]},
    )
    assert resp.status_code == 200
    assert resp.json()["confirmed_count"] == 0
    assert sent == []


def test_manager_cannot_confirm_payment(pay_env, monkeypatch):
    client, db, ids, sent = pay_env
    import webapp.server as server

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": ids["mgr"], "first_name": "Manager"},
    )
    resp = client.post(
        "/api/orders/confirm_payment",
        json={"initData": "x", "order_id": ids["order"]},
    )
    assert resp.status_code == 403
    # Платежи остались pending
    statuses = [p["status"] for p in db.get_payments_for_order(ids["order"])]
    assert statuses == ["pending", "pending"]


# ─── /api/payments/pending — surface для подтверждения paid-заказов ─────────


@pytest.fixture
def paid_env(isolated_db, monkeypatch):
    """Босс + менеджер + ОДОБРЕННЫЙ paid-заказ с pending-платежом.

    Покрывает баг «нет возможности подтвердить оплату в WebApp» для
    paid-заказов (credit видны в /api/debts, paid — нет)."""
    import importlib
    import services.roles as roles
    import services.notifier as notifier
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db

    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")

    order_id = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(order_id, "agent-uuid", "Client X")
    db.add_order_item(order_id, "Product A", "", 2, "шт", 150.0)
    # payment_type='paid' по умолчанию; явно одобряем заказ
    db.update_order_status(order_id, "approved")
    # Авто-платёж, который создаётся при одобрении paid-заказа
    db.add_payment(
        mgr_id,
        "",
        "Manager",
        300.0,
        "USD",
        "Оплата по заказу (отгрузка одобрена)",
        order_id=order_id,
    )

    async def _fake_send(chat_id, text, **kwargs):
        pass

    monkeypatch.setattr(notifier, "tg_send_message", _fake_send)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U"},
    )

    client = TestClient(server.app)
    ids = {"boss": boss_id, "mgr": mgr_id, "order": order_id}
    return client, db, ids


def test_pending_lists_paid_order_for_boss(paid_env):
    client, db, ids = paid_env
    resp = client.post(
        "/api/payments/pending",
        json={"initData": str(ids["boss"])},
    )
    assert resp.status_code == 200, resp.text
    pending = resp.json()["pending"]
    assert len(pending) == 1
    item = pending[0]
    assert item["order_id"] == ids["order"]
    assert item["pending"] == 300.0
    assert item["agent_name"] == "Client X"


def test_pending_forbidden_for_manager(paid_env, monkeypatch):
    client, db, ids = paid_env
    import webapp.server as server

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": ids["mgr"], "first_name": "Manager"},
    )
    resp = client.post("/api/payments/pending", json={"initData": "x"})
    assert resp.status_code == 403


def test_pending_clears_after_confirm(paid_env):
    client, db, ids = paid_env

    # Подтверждаем оплату — заказ должен уйти из списка «на подтверждение»
    ok = client.post(
        "/api/orders/confirm_payment",
        json={"initData": str(ids["boss"]), "order_id": ids["order"]},
    )
    assert ok.status_code == 200
    assert ok.json()["confirmed_count"] == 1

    resp = client.post(
        "/api/payments/pending",
        json={"initData": str(ids["boss"])},
    )
    assert resp.status_code == 200
    assert resp.json()["pending"] == []
