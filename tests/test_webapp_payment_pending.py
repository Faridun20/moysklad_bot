"""
WebApp /api/payments/pending: превью позиций в карточке подтверждения оплаты —
босс видит, ЧТО подтверждает, без открытия заказа.
"""

import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def test_payments_pending_includes_item_preview(isolated_db, monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    boss, mgr = 100, 200
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.update_order_agent(oid, "A-1", "C1")
    db.add_order_item(oid, "Хлеб", "", 2, "шт", 50.0)  # total 100, paid (default)
    db.update_order_status(oid, "shipped")
    db.add_payment(
        user_id=mgr, username="", full_name="Mgr", amount=100.0, currency="USD",
        comment="", order_id=oid,
    )  # pending

    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    client = TestClient(server.app)
    body = client.post("/api/payments/pending", json={"initData": str(boss)}).json()

    row = next(r for r in body["pending"] if r["order_id"] == oid)
    assert row["items_count"] == 1
    assert row["total"] == 100.0
    assert row["pending"] == 100.0
    assert row["items"][0]["name"] == "Хлеб"
    assert row["items"][0]["quantity"] == 2
