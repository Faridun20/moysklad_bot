"""
Тесты per-user permission overrides (PR #44 / tech debt #3c).

Покрываем foundation: caching, грант/реврок/ресет, fallback к role-default,
admin неубиваем env-переменной, endpoint authz, неизвестные коды.
"""

import importlib

import pytest
from fastapi.testclient import TestClient


def _reset_roles_state(monkeypatch=None):
    """Перезагрузить services.roles с свежим _perm_cache.
    Иначе кэш в памяти между тестами утечёт overrides от соседнего теста."""
    import services.roles as roles

    importlib.reload(roles)
    roles.invalidate_all_user_permissions()
    roles.invalidate_all_roles()
    return roles


# ─── Юнит: has_permission ────────────────────────────────────────────────────


def test_has_permission_unknown_code_returns_false(isolated_db):
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    assert roles.has_permission(100, "nonexistent_code") is False


def test_has_permission_role_default_for_manager(isolated_db):
    """Manager → has view_stock (по ROLE_DEFAULTS), но не has manage_payments."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    assert roles.has_permission(100, "view_stock") is True
    assert roles.has_permission(100, "create_orders") is True
    assert roles.has_permission(100, "manage_payments") is False
    assert roles.has_permission(100, "manage_users") is False


def test_has_permission_admin_via_db_role(isolated_db):
    """Юзер с role='admin' в БД имеет ВСЁ."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "a", "Admin", "admin")
    assert roles.has_permission(100, "manage_users") is True
    assert roles.has_permission(100, "change_settings") is True


def test_has_permission_admin_via_env(isolated_db, monkeypatch):
    """ADMIN_IDS из env даёт всё, даже если БД-роль обычная."""
    isolated_db.set_role(100, "m", "M", "manager")
    monkeypatch.setattr("services.roles.ADMIN_IDS", [100])
    roles = _reset_roles_state()
    # Перечитываем ADMIN_IDS после reload
    roles.ADMIN_IDS = [100]
    assert roles.has_permission(100, "manage_users") is True


def test_grant_overrides_role_default_false(isolated_db):
    """Manager НЕ имеет manage_payments по дефолту → grant → True."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    assert roles.has_permission(100, "manage_payments") is False
    assert roles.grant_permission(100, "manage_payments", granted_by=999) is True
    assert roles.has_permission(100, "manage_payments") is True


def test_revoke_overrides_role_default_true(isolated_db):
    """Manager имеет view_stock по дефолту → revoke → False."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    assert roles.has_permission(100, "view_stock") is True
    assert roles.revoke_permission(100, "view_stock", revoked_by=999) is True
    assert roles.has_permission(100, "view_stock") is False


def test_reset_falls_back_to_role_default(isolated_db):
    """После grant + reset → юзер снова на role-default'е."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    roles.grant_permission(100, "manage_payments", granted_by=999)
    assert roles.has_permission(100, "manage_payments") is True
    assert roles.reset_permission(100, "manage_payments") is True
    # Возврат к role-default (False для manager)
    assert roles.has_permission(100, "manage_payments") is False


def test_grant_returns_false_for_unknown_code(isolated_db):
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    assert roles.grant_permission(100, "fly_to_mars", granted_by=999) is False


def test_cache_invalidated_after_grant(isolated_db):
    """grant_permission должен инвалидировать кэш сразу."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    # Заполняем кэш отсутствием permissions
    assert roles.has_permission(100, "manage_payments") is False
    # grant — инвалидирует кэш
    roles.grant_permission(100, "manage_payments", granted_by=999)
    # Следующий вызов перечитывает БД — видит grant
    assert roles.has_permission(100, "manage_payments") is True


def test_list_permissions_shows_sources(isolated_db):
    """list_permissions_for_user различает 'role'/'override'/'default' для UI."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "m", "M", "manager")
    roles.grant_permission(100, "manage_payments", granted_by=999)  # override
    out = roles.list_permissions_for_user(100)
    # view_stock — по умолчанию роли
    assert out["view_stock"]["granted"] is True
    assert out["view_stock"]["source"] == "role"
    # manage_payments — override
    assert out["manage_payments"]["granted"] is True
    assert out["manage_payments"]["source"] == "override"
    # manage_users — нет ни у роли, ни override
    assert out["manage_users"]["granted"] is False
    assert out["manage_users"]["source"] == "default"


def test_list_permissions_admin_marks_all_as_admin(isolated_db):
    """admin → каждый код source='admin'."""
    roles = _reset_roles_state()
    isolated_db.set_role(100, "a", "Admin", "admin")
    out = roles.list_permissions_for_user(100)
    assert all(p["granted"] is True for p in out.values())
    assert all(p["source"] == "admin" for p in out.values())


# ─── E2E endpoints ────────────────────────────────────────────────────────────


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    roles.invalidate_all_user_permissions()
    roles.invalidate_all_roles()

    db = isolated_db
    admin_id, boss_id, mgr_id = 99, 100, 200
    db.set_role(admin_id, "a", "Admin", "admin")
    db.set_role(boss_id, "b", "Boss", "boss")
    db.set_role(mgr_id, "m", "Manager", "manager")

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {
            "id": int(init_data),
            "first_name": "U",
            "last_name": "",
            "username": "u",
        },
    )
    return TestClient(server.app), db, {"admin": admin_id, "boss": boss_id, "mgr": mgr_id}


def test_list_endpoint_admin_only(client_env):
    """Boss и manager не могут читать каталог — только admin."""
    client, _db, ids = client_env
    for uid in (ids["boss"], ids["mgr"]):
        resp = client.post("/api/permissions/list", json={"initData": str(uid)})
        assert resp.status_code == 403
    resp = client.post("/api/permissions/list", json={"initData": str(ids["admin"])})
    assert resp.status_code == 200
    data = resp.json()
    assert "permissions" in data
    assert "view_stock" in data["permissions"]


def test_user_endpoint_returns_per_user_report(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/permissions/user",
        json={"initData": str(ids["admin"]), "user_id": ids["mgr"]},
    )
    assert resp.status_code == 200
    perms = resp.json()["permissions"]
    # Manager — view_stock=True (role), manage_payments=False (default)
    assert perms["view_stock"]["granted"] is True
    assert perms["manage_payments"]["granted"] is False


def test_grant_endpoint_flow(client_env):
    """grant → user perms reflects; revoke → False; reset → fallback."""
    client, db, ids = client_env
    # grant manage_payments менеджеру
    resp = client.post(
        "/api/permissions/grant",
        json={
            "initData": str(ids["admin"]),
            "user_id": ids["mgr"],
            "permission_code": "manage_payments",
            "action": "grant",
        },
    )
    assert resp.status_code == 200, resp.text
    # user/list возвращает override=True
    resp = client.post(
        "/api/permissions/user",
        json={"initData": str(ids["admin"]), "user_id": ids["mgr"]},
    )
    perms = resp.json()["permissions"]
    assert perms["manage_payments"]["granted"] is True
    assert perms["manage_payments"]["source"] == "override"

    # revoke — explicit deny даже на role-default'е (view_stock у manager)
    resp = client.post(
        "/api/permissions/grant",
        json={
            "initData": str(ids["admin"]),
            "user_id": ids["mgr"],
            "permission_code": "view_stock",
            "action": "revoke",
        },
    )
    assert resp.status_code == 200
    resp = client.post(
        "/api/permissions/user",
        json={"initData": str(ids["admin"]), "user_id": ids["mgr"]},
    )
    perms = resp.json()["permissions"]
    assert perms["view_stock"]["granted"] is False
    assert perms["view_stock"]["source"] == "override"

    # reset view_stock → возвращается к role default
    resp = client.post(
        "/api/permissions/grant",
        json={
            "initData": str(ids["admin"]),
            "user_id": ids["mgr"],
            "permission_code": "view_stock",
            "action": "reset",
        },
    )
    assert resp.status_code == 200
    resp = client.post(
        "/api/permissions/user",
        json={"initData": str(ids["admin"]), "user_id": ids["mgr"]},
    )
    perms = resp.json()["permissions"]
    assert perms["view_stock"]["granted"] is True
    assert perms["view_stock"]["source"] == "role"


def test_grant_writes_audit_log(client_env):
    client, db, ids = client_env
    client.post(
        "/api/permissions/grant",
        json={
            "initData": str(ids["admin"]),
            "user_id": ids["mgr"],
            "permission_code": "manage_payments",
            "action": "grant",
        },
    )
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT action, details FROM audit_log WHERE action = ?"), ("permission_grant",))
        rows = [dict(r) for r in cur.fetchall()]
    assert len(rows) == 1
    assert "manage_payments" in rows[0]["details"]


def test_grant_endpoint_403_for_boss(client_env):
    """Даже boss не управляет permissions — только admin."""
    client, _db, ids = client_env
    resp = client.post(
        "/api/permissions/grant",
        json={
            "initData": str(ids["boss"]),
            "user_id": ids["mgr"],
            "permission_code": "manage_payments",
            "action": "grant",
        },
    )
    assert resp.status_code == 403


def test_grant_endpoint_400_for_unknown_action(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/permissions/grant",
        json={
            "initData": str(ids["admin"]),
            "user_id": ids["mgr"],
            "permission_code": "view_stock",
            "action": "wipe",
        },
    )
    assert resp.status_code == 400


def test_grant_endpoint_400_for_unknown_code(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/permissions/grant",
        json={
            "initData": str(ids["admin"]),
            "user_id": ids["mgr"],
            "permission_code": "fly_to_mars",
            "action": "grant",
        },
    )
    assert resp.status_code == 400
