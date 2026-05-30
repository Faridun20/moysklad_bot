"""
Контракт с МойСклад: реверс «Заказа покупателя» (DELETE entity/customerorder)
при отмене заказа.

Мокаем ТРАНСПОРТ (aioresponses) — реально исполняется сборка URL. БД настоящая
(isolated_db). _CTX праймим как после init_demand_context().

Проверяем: gate без контекста, no-op без customerorder, идемпотентность,
успешный DELETE (ставит ms_cancel_synced_at), и что HTTP-ошибка не ломает
бизнес-флоу (synced не ставится, best-effort).
"""

import asyncio

from aioresponses import aioresponses

import services.ms_cancel as ms_cancel
import services.ms_demand as ms_demand
from services.moysklad import MS_BASE


def _prime_ctx(monkeypatch):
    monkeypatch.setitem(ms_demand._CTX, "ready", True)
    monkeypatch.setitem(ms_demand._CTX, "org_meta", {"href": f"{MS_BASE}/entity/organization/ORG"})
    monkeypatch.setitem(ms_demand._CTX, "store_meta", {"href": f"{MS_BASE}/entity/store/STORE"})


def _make_cancelled_order(db, co_id="CO-1"):
    db.set_role(1, "b", "Boss", "boss")
    oid = db.create_order(1, "Boss", "")
    db.update_order_agent(oid, "AGENT-UUID", "Client")
    db.add_order_item(oid, "Товар", f"{MS_BASE}/entity/product/PROD", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    if co_id:
        db.set_order_ms_customerorder_id(oid, co_id)
    return oid


def test_reverse_noop_without_context(isolated_db, monkeypatch):
    monkeypatch.setitem(ms_demand._CTX, "ready", False)
    oid = _make_cancelled_order(isolated_db)
    res = asyncio.run(ms_cancel.reverse_customerorder(oid))
    assert res["ok"] is False
    assert "context" in res["reason"].lower() or "контекст" in res["reason"].lower()


def test_reverse_skipped_without_customerorder(isolated_db, monkeypatch):
    _prime_ctx(monkeypatch)
    oid = _make_cancelled_order(isolated_db, co_id=None)  # нет ms_customerorder_id
    res = asyncio.run(ms_cancel.reverse_customerorder(oid))
    assert res["ok"] is True
    assert res.get("skipped") == "no-customerorder"


def test_reverse_idempotent_when_already_synced(isolated_db, monkeypatch):
    _prime_ctx(monkeypatch)
    db = isolated_db
    oid = _make_cancelled_order(db)
    db.set_order_ms_cancel_synced(oid)
    # DELETE не мокаем: если код попытается слать — aioresponses упадёт.
    res = asyncio.run(ms_cancel.reverse_customerorder(oid))
    assert res["ok"] is True
    assert res.get("skipped") == "already-synced"


def test_reverse_success_deletes_and_marks_synced(isolated_db, monkeypatch):
    _prime_ctx(monkeypatch)
    db = isolated_db
    oid = _make_cancelled_order(db, co_id="CO-XYZ")

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:
                m.delete(f"{MS_BASE}/entity/customerorder/CO-XYZ", status=200, payload={})
                return await ms_cancel.reverse_customerorder(oid)
        finally:
            await moysklad.close_session()

    res = asyncio.run(scenario())
    assert res["ok"] is True and res.get("ms_id") == "CO-XYZ", res
    assert asyncio.run(db.get_order(oid))["ms_cancel_synced_at"] is not None


def test_reverse_http_error_is_best_effort(isolated_db, monkeypatch):
    _prime_ctx(monkeypatch)
    db = isolated_db
    oid = _make_cancelled_order(db, co_id="CO-LOCKED")

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:
                # 409: к заказу привязан документ — МС не даёт удалить.
                m.delete(f"{MS_BASE}/entity/customerorder/CO-LOCKED", status=409, payload={"errors": []})
                return await ms_cancel.reverse_customerorder(oid)
        finally:
            await moysklad.close_session()

    res = asyncio.run(scenario())
    assert res["ok"] is False
    # synced НЕ ставим — best-effort, отмена в БД при этом не затронута.
    assert asyncio.run(db.get_order(oid))["ms_cancel_synced_at"] is None
