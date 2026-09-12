"""
PR C находок: деактивация пользователя (#32).

Блок про snapshot (#31) удалён вместе с самим снапшотом справочников
МойСклад — зеркалить больше нечего.

Реальная БД (isolated_db).
"""

import asyncio

from fastapi.testclient import TestClient

import services.roles as roles






# ─── #32: деактивация пользователя ───────────────────────────────────────────


def test_deactivated_user_loses_role(isolated_db):
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(5, "u", "U", "boss")
    assert db.get_role(5) == "boss"

    assert asyncio.run(db.deactivate_user(5, 1)) is True
    assert db.get_role(5) == "guest"  # права сняты
    assert asyncio.run(db.deactivate_user(5, 1)) is False  # идемпотентно

    assert asyncio.run(db.reactivate_user(5, 1)) is True
    assert db.get_role(5) == "boss"  # роль восстановлена


def test_notify_recipients_excludes_deactivated(isolated_db):
    from services.notifier import get_notify_recipients

    db = isolated_db
    db.set_role(10, "a", "A", "boss")
    db.set_role(11, "b", "B", "boss")
    asyncio.run(db.deactivate_user(11, 1))
    rec = get_notify_recipients()
    assert 10 in rec
    assert 11 not in rec


def test_api_users_deactivate(isolated_db, monkeypatch):
    import importlib
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    admin, target, mgr = 100, 50, 200
    db.set_role(admin, "a", "Admin", "admin")
    db.set_role(target, "t", "T", "boss")
    db.set_role(mgr, "m", "M", "manager")
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    client = TestClient(server.app)

    # Менеджер не вправе.
    assert (
        client.post(
            "/api/users/deactivate",
            json={"initData": str(mgr), "user_id": target, "action": "deactivate"},
        ).status_code
        == 403
    )
    # Админ деактивирует.
    assert (
        client.post(
            "/api/users/deactivate",
            json={"initData": str(admin), "user_id": target, "action": "deactivate"},
        ).status_code
        == 200
    )
    assert db.get_role(target) == "guest"
    # Себя — нельзя.
    assert (
        client.post(
            "/api/users/deactivate",
            json={"initData": str(admin), "user_id": admin, "action": "deactivate"},
        ).status_code
        == 400
    )
    # Реактивация.
    assert (
        client.post(
            "/api/users/deactivate",
            json={"initData": str(admin), "user_id": target, "action": "reactivate"},
        ).status_code
        == 200
    )
    assert db.get_role(target) == "boss"


# (Тесты архива аудита удалены вместе с Drive-интеграцией.)
