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
        assert me["prefs"] == {"work_actions": False, "work_actions_hint_shown": 0, "doc_lang": "ru_uz"}, who
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
    assert user_prefs.get_prefs(1) == {"work_actions": False, "work_actions_hint_shown": 0, "doc_lang": "ru_uz"}
    assert user_prefs.set_pref(1, "work_actions", 1) == {
        "work_actions": True, "work_actions_hint_shown": 0, "doc_lang": "ru_uz",
    }


def test_doc_lang_is_remembered_only_from_the_list(isolated_db):
    """Язык печатных форм — последний выбор человека (любой рабочей роли),
    только `ru_uz`/`ru`/`uz`; мусор не записывается и печать не роняет."""
    from services import user_prefs

    assert user_prefs.doc_lang(7) == "ru_uz"
    user_prefs.remember_doc_lang(7, "uz")
    assert user_prefs.doc_lang(7) == "uz"
    user_prefs.remember_doc_lang(7, "en")
    assert user_prefs.doc_lang(7) == "uz"
    with pytest.raises(ValueError):
        user_prefs.set_pref(7, "doc_lang", "en")
    assert user_prefs.applies_to("doc_lang", "manager")


def test_work_actions_hint_shown_counter(env):
    """D2 продуктового аудита: счётчик показов подсказки «Работаете один?».

    Это int, а не bool — фронт шлёт очередное значение (текущее + 1) каждый
    раз, когда подсказку реально нарисовал; сервер только валидирует диапазон
    и роль (та же, что у `work_actions` — подсказка о нём и есть)."""
    client, db, ids = env
    r = _post(client, "/api/prefs/set", ids["boss"], key="work_actions_hint_shown", value=1)
    assert r.status_code == 200, r.text
    assert r.json()["prefs"]["work_actions_hint_shown"] == 1
    r = _post(client, "/api/prefs/set", ids["boss"], key="work_actions_hint_shown", value=3)
    assert r.json()["prefs"]["work_actions_hint_shown"] == 3
    # Персистентность — новый /api/me видит то же значение.
    assert _post(client, "/api/me", ids["boss"]).json()["prefs"]["work_actions_hint_shown"] == 3
    # Только admin/boss — как у самого work_actions.
    assert _post(
        client, "/api/prefs/set", ids["mgr"], key="work_actions_hint_shown", value=1
    ).status_code == 403
    # Булево на счётчик — отказ (иначе True/False молча стали бы 1/0).
    assert _post(
        client, "/api/prefs/set", ids["boss"], key="work_actions_hint_shown", value=True
    ).status_code == 400
    assert _post(
        client, "/api/prefs/set", ids["boss"], key="work_actions_hint_shown", value="1"
    ).status_code == 400
    # Отрицательное/за потолком — обрезается, не отказ (это телеметрия показа,
    # а не решение пользователя — нет причины ронять запрос фронта из-за гонки).
    r = _post(client, "/api/prefs/set", ids["boss"], key="work_actions_hint_shown", value=-5)
    assert r.json()["prefs"]["work_actions_hint_shown"] == 0
    r = _post(client, "/api/prefs/set", ids["boss"], key="work_actions_hint_shown", value=999999)
    assert r.json()["prefs"]["work_actions_hint_shown"] == 1000
    # Счётчик — не решение, поэтому в аудит не идёт (в отличие от work_actions).
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT action FROM audit_log WHERE action = 'pref_set'")
        assert cur.fetchall() == []


def test_delete_requires_boss_setting_reaches_me(env):
    client, db, ids = env
    db.set_setting("delete_requires_boss", True)
    assert _post(client, "/api/me", ids["mgr"]).json()["delete_requires_boss"] is True
