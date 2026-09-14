"""Безопасность, п.1: единица позиции заказа (`unit`) — ввод другого человека.

Раньше `/api/orders/add_item` клал `unit` в БД как есть и любой длины, а фронт
выводил его без экранирования (stored-XSS в сессии руководства). Фронт теперь
экранирует (`app-load.smoke.test.js`), сервер режет разметку и длину.
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


def _unit_of(db, item_id):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT unit FROM order_items WHERE id = ?"), (item_id,))
        return cur.fetchone()[0]


def _add(client, uid, oid, unit):
    body = {"initData": str(uid), "order_id": oid, "product_name": "P", "quantity": 1, "price": 5}
    if unit is not None:
        body["unit"] = unit
    r = client.post("/api/orders/add_item", json=body)
    assert r.status_code == 200, r.text
    return r.json()["item_id"]


def test_add_item_strips_markup_and_caps_unit(isolated_db, monkeypatch):
    db = isolated_db
    client = _client(db, monkeypatch, 21)
    oid = db.create_order(21, "Mgr", "")

    evil = '<img src=x onerror="alert(document.cookie)">'
    stored = _unit_of(db, _add(client, 21, oid, evil))
    assert "<" not in stored and ">" not in stored and '"' not in stored
    assert len(stored) <= 16

    assert len(_unit_of(db, _add(client, 21, oid, "к" * 500))) == 16


def test_add_item_keeps_normal_units(isolated_db, monkeypatch):
    db = isolated_db
    client = _client(db, monkeypatch, 22)
    oid = db.create_order(22, "Mgr", "")
    for unit in ("шт", "кг", "м²", "уп.", "л/мин"):
        assert _unit_of(db, _add(client, 22, oid, unit)) == unit
    # Пусто / не прислали / одни запрещённые символы — дефолт «шт».
    assert _unit_of(db, _add(client, 22, oid, None)) == "шт"
    assert _unit_of(db, _add(client, 22, oid, "   ")) == "шт"
    assert _unit_of(db, _add(client, 22, oid, "<>")) == "шт"
