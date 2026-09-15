"""
Тесты B5 (WebApp-ручки) — `/api/catalog_import/{template,preview,commit}`.

FastAPI TestClient; мокаем границу verify_init_data + get_notify_bot (файл в
Telegram), БД и роли настоящие — тот же приём, что в `tests/test_warehouse_api.py`.
"""

from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient


class _FakeBot:
    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, document, caption=None):
        self.docs.append({"chat_id": chat_id, "caption": caption})


@pytest.fixture
def api(isolated_db, monkeypatch):
    import importlib

    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    rate_limit.reset()

    db = isolated_db
    ids = {"admin": 1, "boss": 100, "mgr": 200, "guest": 300}
    db.set_role(ids["admin"], "admin_user", "Admin", "admin")
    db.set_role(ids["boss"], "boss_user", "Boss", "boss")
    db.set_role(ids["mgr"], "mgr_user", "Manager", "manager")
    db.set_role(ids["guest"], "guest_user", "Guest", "guest")

    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    ids["bot"] = fake_bot
    return TestClient(server.app), db, ids


def _xlsx_b64(rows: list[list]) -> str:
    from openpyxl import Workbook

    from services.catalog_import import TEMPLATE_HEADERS

    wb = Workbook()
    ws = wb.active
    ws.append(TEMPLATE_HEADERS)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


def test_manager_can_preview_and_commit(api):
    client, db, ids = api
    b64 = _xlsx_b64([["Новый товар", "шт", "", "5", "10"]])

    prev = client.post(
        "/api/catalog_import/preview",
        json={"initData": str(ids["mgr"]), "filename": "f.xlsx", "content_base64": b64},
    )
    assert prev.status_code == 200, prev.text
    assert prev.json()["new"] == 1

    commit = client.post(
        "/api/catalog_import/commit",
        json={"initData": str(ids["mgr"]), "filename": "f.xlsx", "content_base64": b64},
    )
    assert commit.status_code == 200, commit.text
    body = commit.json()
    assert body["ok"] is True
    assert body["created"] == 1

    stock = client.post("/api/wh/stock", json={"initData": str(ids["mgr"])}).json()
    assert {p["name"]: p["quantity"] for p in stock["products"]}["Новый товар"] == 10.0


def test_guest_cannot_import(api):
    client, _db, ids = api
    b64 = _xlsx_b64([["Товар", "шт", "", "", "1"]])
    r = client.post(
        "/api/catalog_import/preview",
        json={"initData": str(ids["guest"]), "filename": "f.xlsx", "content_base64": b64},
    )
    assert r.status_code == 403
    r = client.post(
        "/api/catalog_import/commit",
        json={"initData": str(ids["guest"]), "filename": "f.xlsx", "content_base64": b64},
    )
    assert r.status_code == 403


def test_malformed_file_rejected_before_any_write(api):
    client, db, ids = api
    bad_b64 = base64.b64encode(b"this is not a real xlsx file").decode()
    r = client.post(
        "/api/catalog_import/commit",
        json={"initData": str(ids["mgr"]), "filename": "f.xlsx", "content_base64": bad_b64},
    )
    assert r.status_code == 400
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM products"))
        assert cur.fetchone()[0] == 0


def test_template_downloads_via_telegram(api):
    client, _db, ids = api
    r = client.post("/api/catalog_import/template", json={"initData": str(ids["mgr"])})
    assert r.status_code == 200, r.text
    assert r.json()["sent"] is True
    assert len(ids["bot"].docs) == 1
