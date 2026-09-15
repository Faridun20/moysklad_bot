"""Личные настройки вида (`user_prefs`) и выключатель «Рабочие действия».

Решение владельца: у руководителя в интерфейсе только «смотреть, решать,
контролировать», работа менеджера — за выключателем в «Меню». Выключатель —
ПРЕДПОЧТЕНИЕ ВИДА: хранится на сервере (WebView теряет localStorage), едет в
/api/me, переключается ручкой с аудитом и НЕ меняет права ни одной ручки.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    ids = {"boss": 100, "admin": 101, "mgr": 200, "keeper": 300}
    db.set_role(ids["boss"], "boss", "Boss", "boss")
    db.set_role(ids["admin"], "admin", "Admin", "admin")
    db.set_role(ids["mgr"], "mgr", "Manager", "manager")
    db.set_role(ids["keeper"], "keeper", "Keeper", "warehouse_keeper")
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: (
            {"id": int(init_data), "first_name": "U", "username": "u"} if init_data else None
        ),
    )
    return TestClient(server.app), db, ids


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_me_carries_prefs_with_defaults_off(env):
    client, _db, ids = env
    for who in ("boss", "admin", "mgr"):
        me = _post(client, "/api/me", ids[who]).json()
        assert me["prefs"] == {"work_actions": False}, who
        # Настройки удаления нет — значит «менеджеру можно» (по умолчанию выкл.).
        assert me["delete_requires_boss"] is False


def test_boss_toggles_work_actions_and_it_persists(env):
    client, db, ids = env
    r = _post(client, "/api/prefs/set", ids["boss"], key="work_actions", value=True)
    assert r.status_code == 200, r.text
    assert r.json()["prefs"]["work_actions"] is True
    # Сохранено на сервере — новая сессия (новый /api/me) видит включённым.
    assert _post(client, "/api/me", ids["boss"]).json()["prefs"]["work_actions"] is True
    # У другого пользователя своё значение.
    assert _post(client, "/api/me", ids["admin"]).json()["prefs"]["work_actions"] is False

    r = _post(client, "/api/prefs/set", ids["boss"], key="work_actions", value=False)
    assert r.json()["prefs"]["work_actions"] is False
    assert _post(client, "/api/me", ids["boss"]).json()["prefs"]["work_actions"] is False

    # Каждое переключение — в аудит.
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            "SELECT user_id, action, details FROM audit_log WHERE action = 'pref_set' ORDER BY id"
        )
        rows = [tuple(r) for r in cur.fetchall()]
    assert rows == [
        (ids["boss"], "pref_set", "work_actions=on"),
        (ids["boss"], "pref_set", "work_actions=off"),
    ]


def test_toggle_is_boss_only_and_validates(env):
    client, _db, ids = env
    assert _post(client, "/api/prefs/set", ids["mgr"], key="work_actions", value=True).status_code == 403
    assert _post(client, "/api/prefs/set", ids["keeper"], key="work_actions", value=True).status_code == 403
    assert _post(client, "/api/prefs/set", ids["admin"], key="work_actions", value=True).status_code == 200
    # Неизвестный ключ и не-булево значение — отказ текстом, в базу не пишется.
    assert _post(client, "/api/prefs/set", ids["boss"], key="role", value=True).status_code == 400
    assert _post(client, "/api/prefs/set", ids["boss"], key="work_actions", value="yes").status_code == 400
    assert _post(client, "/api/me", ids["boss"]).json()["prefs"]["work_actions"] is False
    # Без подписи — 401.
    assert client.post("/api/prefs/set", json={"initData": "", "key": "work_actions", "value": True}).status_code == 401


def test_switch_is_view_only_not_a_permission(env):
    """Выключенные «Рабочие действия» не отбирают у руководителя ни одной ручки,
    включённые — не дают менеджеру руководительских."""
    client, _db, ids = env
    # Выключено (по умолчанию): ручки руководителя по-прежнему отвечают ему.
    assert _post(client, "/api/deposits/on_hand", ids["boss"]).status_code == 200
    assert _post(client, "/api/wh/invoices", ids["boss"], limit=5).status_code == 200
    _post(client, "/api/prefs/set", ids["boss"], key="work_actions", value=True)
    assert _post(client, "/api/deposits/on_hand", ids["boss"]).status_code == 200
    # Менеджеру ручки одобрения закрыты и остаются закрытыми.
    assert _post(client, "/api/orders/requests", ids["mgr"]).status_code == 403


def test_service_rejects_unknown_key(isolated_db):
    from services import user_prefs

    with pytest.raises(ValueError):
        user_prefs.set_pref(1, "anything", True)
    assert user_prefs.get_prefs(1) == {"work_actions": False}
    assert user_prefs.set_pref(1, "work_actions", 1) == {"work_actions": True}


def test_delete_requires_boss_setting_reaches_me(env):
    client, db, ids = env
    db.set_setting("delete_requires_boss", True)
    assert _post(client, "/api/me", ids["mgr"]).json()["delete_requires_boss"] is True
