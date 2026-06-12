"""WP-01/WP-02: правка состава/агента заказа разрешена только в статусе draft,
и числовые поля (quantity/amount) валидируются (isfinite, >0, потолок).

Реальная БД (isolated_db); граница Telegram (verify_init_data) мокается.
"""

import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _client(db, monkeypatch, uid, role="manager"):
    import webapp.server as server

    importlib.reload(roles)
    db.set_role(uid, "u", "U", role)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _draft(db, uid):
    return db.create_order(uid, "Mgr", "")


def _item_count(db, oid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM order_items WHERE order_id = ?"), (oid,))
        return cur.fetchone()[0]


def test_add_item_blocked_on_non_draft(isolated_db, monkeypatch):
    db = isolated_db
    client = _client(db, monkeypatch, 10)
    oid = _draft(db, 10)
    # draft → ok
    r = client.post(
        "/api/orders/add_item",
        json={"initData": "10", "order_id": oid, "product_name": "P", "quantity": 1, "price": 5},
    )
    assert r.status_code == 200, r.text
    # approved → 409, состав не меняется
    db.update_order_status(oid, "approved")
    r = client.post(
        "/api/orders/add_item",
        json={"initData": "10", "order_id": oid, "product_name": "P2", "quantity": 1, "price": 5},
    )
    assert r.status_code == 409
    assert _item_count(db, oid) == 1  # вторая позиция не добавлена


def test_remove_item_and_set_agent_blocked_on_non_draft(isolated_db, monkeypatch):
    db = isolated_db
    client = _client(db, monkeypatch, 11)
    oid = _draft(db, 11)
    item_id = db.add_order_item(oid, "P", "", 1, "шт", 5)
    db.update_order_status(oid, "shipped")

    r = client.post("/api/orders/remove_item", json={"initData": "11", "item_id": item_id})
    assert r.status_code == 409
    assert _item_count(db, oid) == 1  # не удалено

    r = client.post(
        "/api/orders/set_agent",
        json={"initData": "11", "order_id": oid, "agent_id": "X", "agent_name": "X"},
    )
    assert r.status_code == 409


def test_add_item_rejects_bad_quantity(isolated_db, monkeypatch):
    db = isolated_db
    client = _client(db, monkeypatch, 12)
    oid = _draft(db, 12)
    # negative / zero / non-number / over-cap finite / inf-as-string
    for bad in (-1, 0, "abc", 1e308, "1e309"):
        r = client.post(
            "/api/orders/add_item",
            json={"initData": "12", "order_id": oid, "product_name": "P", "quantity": bad, "price": 5},
        )
        assert r.status_code == 400, f"quantity={bad!r} should be rejected, got {r.status_code}"
    assert _item_count(db, oid) == 0
