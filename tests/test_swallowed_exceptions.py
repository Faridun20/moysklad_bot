"""T2.12 — значимые исключения больше не проглатываются молча.

§2.16. Пять конкретных мест, не «логирование везде»:
  • CREATE TABLE — любая ошибка логировалась как «таблица уже существует»;
  • _invalidate_role_cache — при поломке роль остаётся закэшированной, и об
    этом не было ни строчки;
  • ms_demand — `skipped` терялся при сбоях, босс не видел, какие позиции
    не попали в МойСклад;
  • bot.py ×3 — `except Exception: pass`.
"""

import asyncio
import inspect
import logging

import pytest
from aioresponses import aioresponses

import bot as bot_module
import services.database as db_module
import services.ms_demand as ms_demand
from services.moysklad import MS_BASE


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


# ─── ms_demand: skipped доезжает до вызывающего ─────────────────────────────


def _prime_ctx(monkeypatch):
    monkeypatch.setitem(ms_demand._CTX, "ready", True)
    monkeypatch.setitem(
        ms_demand._CTX, "org_meta", {"href": f"{MS_BASE}/entity/organization/ORG"}
    )
    monkeypatch.setitem(ms_demand._CTX, "store_meta", {"href": f"{MS_BASE}/entity/store/ST"})
    monkeypatch.setitem(ms_demand._CTX, "attribute_name_meta", None)
    monkeypatch.setitem(ms_demand._CTX, "attribute_uid_meta", None)


ORDER = {"id": 42, "agent_id": "AGENT", "comment": ""}
# Одна позиция с product_href (пройдёт), одна без (попадёт в skipped).
ITEMS_MIXED = [
    {"product_name": "Хороший", "product_href": f"{MS_BASE}/entity/product/P", "quantity": 1, "price_cents": 100},
    {"product_name": "Без привязки", "product_href": "", "quantity": 1, "price_cents": 100},
]
ITEMS_ALL_BAD = [
    {"product_name": "Без привязки 1", "product_href": "", "quantity": 1, "price_cents": 100},
    {"product_name": "Без привязки 2", "product_href": "", "quantity": 1, "price_cents": 100},
]


def _run(factory):
    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            return await factory()
        finally:
            await moysklad.close_session()

    return asyncio.run(scenario())


def test_skipped_returned_when_no_positions_parsed(monkeypatch):
    _prime_ctx(monkeypatch)

    res = _run(
        lambda: ms_demand.create_demand_from_request(ORDER, ITEMS_ALL_BAD, "Менеджер")
    )

    assert res["ok"] is False
    assert res.get("skipped") == ["Без привязки 1", "Без привязки 2"]


def test_skipped_returned_on_http_error(monkeypatch):
    """МС ответил 400 — список непрошедших позиций всё равно должен вернуться."""
    _prime_ctx(monkeypatch)
    sync_id = __import__("services.moysklad", fromlist=["order_sync_id"]).order_sync_id(
        "demand", 42
    )

    def factory():
        with aioresponses() as m:
            m.get(
                f"{MS_BASE}/entity/demand?filter=syncId%3D{sync_id}&limit=1",
                status=200,
                payload={"rows": []},
            )
            m.post(f"{MS_BASE}/entity/demand", status=400, payload={"errors": []})
            return ms_demand.create_demand_from_request(ORDER, ITEMS_MIXED, "Менеджер")

    res = _run(factory)
    assert res["ok"] is False
    assert res.get("skipped") == ["Без привязки"]


def test_skipped_returned_on_exception(monkeypatch):
    """POST упал с сетевой ошибкой — skipped всё равно возвращается."""
    _prime_ctx(monkeypatch)
    from services.moysklad import order_sync_id

    sync_id = order_sync_id("demand", 42)

    def factory():
        with aioresponses() as m:
            m.get(
                f"{MS_BASE}/entity/demand?filter=syncId%3D{sync_id}&limit=1",
                status=200,
                payload={"rows": []},
            )
            m.post(f"{MS_BASE}/entity/demand", exception=RuntimeError("сеть отвалилась"))
            return ms_demand.create_demand_from_request(ORDER, ITEMS_MIXED, "Менеджер")

    res = _run(factory)
    assert res["ok"] is False
    assert res.get("skipped") == ["Без привязки"]


@pytest.mark.parametrize("marker", ['"skipped": skipped'])
def test_all_demand_failure_paths_carry_skipped(marker):
    """Каждая ветка отказа ПОСЛЕ разбора позиций отдаёт skipped."""
    src = inspect.getsource(ms_demand.create_demand_from_request)
    # Три ветки: нет позиций, HTTP-ошибка, исключение.
    assert src.count(marker) >= 3, f"не все ветки отказа возвращают skipped ({src.count(marker)})"


# ─── bot.py: ни одного `except Exception: pass` в названных местах ──────────


def test_bot_has_no_bare_except_pass():
    src = inspect.getsource(bot_module)
    bare = []
    lines = src.splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.strip() == "except Exception:" and lines[i + 1].strip() == "pass":
            bare.append(i + 1)
    assert not bare, f"остались `except Exception: pass` на строках {bare}"
