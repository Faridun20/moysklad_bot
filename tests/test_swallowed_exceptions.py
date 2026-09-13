"""T2.12 — значимые исключения больше не проглатываются молча.

§2.16. Конкретные места, не «логирование везде»:
  • CREATE TABLE — любая ошибка логировалась как «таблица уже существует»;
  • _invalidate_role_cache — при поломке роль остаётся закэшированной, и об
    этом не было ни строчки;
  • bot.py ×3 — `except Exception: pass`.
"""

import inspect
import logging


import bot as bot_module
import services.database as db_module


# ─── _invalidate_role_cache ─────────────────────────────────────────────────


def test_role_cache_failure_is_logged(isolated_db, monkeypatch, caplog):
    """Сброс кэша упал → WARNING. Раньше — тишина, а роль оставалась старой."""
    import services.roles as roles

    def _boom(_uid):
        raise RuntimeError("кэш недоступен")

    monkeypatch.setattr(roles, "invalidate_role", _boom)

    with caplog.at_level(logging.WARNING):
        isolated_db._invalidate_role_cache(12345)

    assert any(
        "кэш роли" in r.message or "кэш роли" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_role_cache_failure_does_not_break_write(isolated_db, monkeypatch):
    """Кэш мягкий: ошибка сброса не должна валить саму запись роли."""
    import services.roles as roles

    monkeypatch.setattr(
        roles, "invalidate_role", lambda _uid: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert isolated_db.set_role(999, "u", "U", "manager") is True


def test_role_cache_no_bare_pass():
    src = inspect.getsource(db_module._invalidate_role_cache)
    assert "logger.warning" in src
    assert "\n        pass" not in src


# ─── CREATE TABLE ───────────────────────────────────────────────────────────


def test_create_table_failure_is_logged_as_error(isolated_db, monkeypatch, caplog):
    """Настоящая ошибка DDL логируется как ошибка, а не как «уже существует».

    CREATE TABLE IF NOT EXISTS на существующей таблице не бросает вовсе,
    поэтому исключение здесь — всегда реальная проблема (нет прав, кривой тип).
    """
    real_cursor = db_module.get_cursor

    class _BoomCursor:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, *a, **kw):
            raise RuntimeError("permission denied for schema public")

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(db_module, "get_cursor", lambda conn: _BoomCursor(real_cursor(conn)))

    with caplog.at_level(logging.ERROR):
        db_module._create_tables()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert msgs, "провал CREATE TABLE не залогирован на ERROR"
    assert any("схема неполная" in m for m in msgs), msgs


def test_create_table_no_longer_claims_already_exists():
    src = inspect.getsource(db_module._create_tables)
    assert "Таблица уже существует" not in src, "вернулась маскировка ошибки"
    assert "logger.exception" in src


# ─── bot.py: ни одного `except Exception: pass` в названных местах ──────────


def test_bot_has_no_bare_except_pass():
    src = inspect.getsource(bot_module)
    bare = []
    lines = src.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.strip() == "except Exception:" and lines[i + 1].strip() == "pass":
            bare.append(i + 1)
    assert not bare, f"остались `except Exception: pass` на строках {bare}"
