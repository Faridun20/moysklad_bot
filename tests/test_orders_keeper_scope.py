"""Кладовщик в `/api/orders` видит то, что отгружает.

Регресс, найденный E2E (`test_keeper_marks_approved_order_shipped`): ручка
отдавала кладовщику «свои заказы» (`get_user_orders`), а заказов он не
создаёт — список был пуст всегда, и кнопка «Отгрузить» в WebApp никогда не
появлялась, хотя `/api/orders/ship` ему разрешён.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    db.set_role(200, "m", "Manager", "manager")
    db.set_role(400, "k", "Keeper", "warehouse_keeper")
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"},
    )

    def order(status):
        oid = db.create_order(200, "Manager", "")
        db.update_order_agent(oid, "1", "Клиент")
        db.add_order_item(oid, "Товар", "", 1, "шт", 10.0)
        db.update_order_status(oid, status)
        return oid

    return TestClient(server.app), order


def test_keeper_sees_approved_and_shipped_orders_only(env):
    client, order = env
    draft = order("draft")
    pending = order("pending")
    approved = order("approved")
    shipped = order("shipped")

    r = client.post("/api/orders", json={"initData": "400"})
    assert r.status_code == 200, r.text
    got = {o["id"]: o["status"] for o in r.json()["orders"]}
    assert got == {approved: "approved", shipped: "shipped"}
    assert draft not in got and pending not in got


def test_keeper_orders_carry_no_profit(env):
    """Себестоимость и прибыль — только руководству; кладовщику не отдаём."""
    client, order = env
    order("approved")
    r = client.post("/api/orders", json={"initData": "400"})
    o = r.json()["orders"][0]
    assert "profit" not in o or o.get("profit") is None
    assert r.json().get("role") == "warehouse_keeper"


def test_manager_scope_unchanged(env):
    client, order = env
    order("approved")
    r = client.post("/api/orders", json={"initData": "200"})
    assert r.status_code == 200
    assert len(r.json()["orders"]) == 1


def test_manager_acting_as_keeper_also_sees_others_orders_to_ship(env, isolated_db, monkeypatch):
    """Совмещение ролей (ROLE_ALSO_ACTS_AS): менеджер отгружает за кладовщика,
    поэтому к своим заказам получает чужие одобренные/отгруженные — но не чужие
    черновики и заявки, и без прибыли."""
    client, order = env
    db = isolated_db
    db.set_role(201, "m2", "Manager2", "manager")
    mine_draft = order("draft")

    def other(status):
        oid = db.create_order(201, "Manager2", "")
        db.add_order_item(oid, "Товар", "", 1, "шт", 10.0)
        db.update_order_status(oid, status)
        return oid

    other_draft, other_pending = other("draft"), other("pending")
    other_approved, other_shipped = other("approved"), other("shipped")

    r = client.post("/api/orders", json={"initData": "200"})
    assert r.status_code == 200, r.text
    got = {o["id"] for o in r.json()["orders"]}
    assert got == {mine_draft, other_approved, other_shipped}
    assert other_draft not in got and other_pending not in got
    assert all(o.get("profit") is None for o in r.json()["orders"])

    # И отгрузить чужой одобренный он действительно может. Уведомление автору
    # заказа — граница с Telegram, её подменяем.
    import webapp.server as server

    class _Bot:
        async def send_message(self, *a, **k):
            return None

    async def _bot():
        return _Bot()

    monkeypatch.setattr(server, "get_notify_bot", _bot)
    r = client.post("/api/orders/ship", json={"initData": "200", "order_id": other_approved})
    assert r.status_code == 200, r.text
    assert asyncio.run(db.get_order(other_approved))["status"] == "shipped"
