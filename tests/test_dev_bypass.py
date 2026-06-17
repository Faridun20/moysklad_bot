"""Регресс на локальный обход авторизации WebApp (DEV_AUTH_BYPASS).

Контракт:
  * без флага → пустой initData отвергается (401), как в проде;
  * с флагом и без DATABASE_URL → синтетический dev-юзер пропускается,
    роль берётся из БД (сид-скрипт даёт admin);
  * с флагом, НО при заданном DATABASE_URL (прод/Postgres) → обход
    самоблокируется (401) — предохранитель от случайного включения на проде.
"""

import importlib

from fastapi.testclient import TestClient


def _client(isolated_db):
    """Свежий TestClient с перезагруженным кэшем ролей (как в test_audit_fixes)."""
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    return TestClient(server.app)


def test_no_bypass_empty_initdata_rejected(isolated_db, monkeypatch):
    monkeypatch.delenv("DEV_AUTH_BYPASS", raising=False)
    client = _client(isolated_db)
    resp = client.post("/api/me", json={"initData": ""})
    assert resp.status_code == 401


def test_bypass_active_resolves_dev_user(isolated_db, monkeypatch):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("DEV_USER_ID", "777001")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    isolated_db.set_role(777001, "dev", "Dev", "admin")

    client = _client(isolated_db)
    resp = client.post("/api/me", json={"initData": ""})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == 777001
    assert body["role"] == "admin"  # роль реально читается из БД

    # Ролевой scope тоже проходит (admin видит все заказы; список пуст — ок).
    resp_orders = client.post("/api/orders", json={"initData": ""})
    assert resp_orders.status_code == 200


def test_bypass_self_disables_when_database_url_set(isolated_db, monkeypatch):
    """Предохранитель: даже с флагом обход не работает при DATABASE_URL."""
    monkeypatch.setenv("DEV_AUTH_BYPASS", "1")
    monkeypatch.setenv("DEV_USER_ID", "777002")
    monkeypatch.setenv("DATABASE_URL", "postgres://example/db")
    isolated_db.set_role(777002, "dev", "Dev", "admin")

    client = _client(isolated_db)
    resp = client.post("/api/me", json={"initData": ""})
    assert resp.status_code == 401
