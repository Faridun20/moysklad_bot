"""A2 — сидинг предопределённых пользователей из config.

`_load_predefined_users` зовётся из `init_db` и раздаёт роли по спискам
ADMIN_IDS / BOSS_IDS / MANAGER_IDS. Раньше MANAGER_IDS тянулась отдельным
`try: __import__("config").MANAGER_IDS except Exception: MANAGER_IDS = []`,
хотя config.py определяет её в обеих ветках. Такой except маскировал бы
реальную ошибку импорта конфига под безобидное «менеджеров нет».

Тестов на функцию не было вовсе — фиксируем контракт.
"""

import services.database as _db_module


def _roles(db) -> dict[int, str]:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT user_id, role FROM user_roles")
        return {r[0]: r[1] for r in cur.fetchall()}


def _seed(db, monkeypatch, *, admin=(), boss=(), manager=()):
    import config

    monkeypatch.setattr(config, "ADMIN_IDS", list(admin), raising=False)
    monkeypatch.setattr(config, "BOSS_IDS", list(boss), raising=False)
    monkeypatch.setattr(config, "MANAGER_IDS", list(manager), raising=False)
    db._load_predefined_users()


def test_manager_ids_are_seeded(isolated_db, monkeypatch):
    """MANAGER_IDS доезжает до user_roles — то, что маскировал старый except."""
    db = isolated_db
    _seed(db, monkeypatch, manager=[7001, 7002])
    roles = _roles(db)
    assert roles.get(7001) == "manager"
    assert roles.get(7002) == "manager"


def test_all_three_lists_are_seeded(isolated_db, monkeypatch):
    db = isolated_db
    _seed(db, monkeypatch, admin=[8001], boss=[8002], manager=[8003])
    roles = _roles(db)
    assert roles.get(8001) == "admin"
    assert roles.get(8002) == "boss"
    assert roles.get(8003) == "manager"


def test_seeding_is_idempotent_and_keeps_existing_role(isolated_db, monkeypatch):
    """ON CONFLICT DO NOTHING: повторный прогон не трогает уже заведённого
    пользователя, даже если его роль успели поменять вручную."""
    db = isolated_db
    _seed(db, monkeypatch, manager=[9001])
    db.set_role(9001, "u", "U", "boss")  # админ повысил вручную
    _seed(db, monkeypatch, manager=[9001])  # повторный старт сервиса
    assert _roles(db).get(9001) == "boss"


def test_broken_config_is_not_silently_swallowed_as_empty(isolated_db, monkeypatch):
    """Если config сломан, функция не должна тихо отработать «как будто
    пользователей нет» — она логирует ошибку и никого не заводит.

    Раньше отсутствие MANAGER_IDS давало ровно этот тихий сценарий, но
    только для менеджеров; теперь имя резолвится вместе с остальными.
    """
    db = isolated_db
    import config

    monkeypatch.delattr(config, "MANAGER_IDS", raising=False)
    monkeypatch.setattr(config, "ADMIN_IDS", [9100], raising=False)
    monkeypatch.setattr(config, "BOSS_IDS", [], raising=False)

    db._load_predefined_users()  # не бросает наружу (внешний try/except)

    # Никого не завели: ImportError случился ДО записи, а не «просто без
    # менеджеров». Это и есть разница с прежним поведением.
    assert 9100 not in _roles(db)


def test_module_has_no_dynamic_config_import(isolated_db):
    """Обёртка __import__("config") снята — импорт статический."""
    import inspect

    src = inspect.getsource(_db_module._load_predefined_users)
    # Именно вызов, а не слово: в комментарии рядом объясняется, почему
    # обёртки больше нет.
    assert '__import__("config")' not in src
    assert "from config import ADMIN_IDS, BOSS_IDS, MANAGER_IDS" in src
