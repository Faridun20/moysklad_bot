"""
Контракт с МойСклад: документ «Возврат покупателя» (entity/salesreturn).

Мокаем ТРАНСПОРТ (aioresponses) — реально исполняется сборка URL/payload.
БД настоящая (isolated_db). _CTX праймим как после init_demand_context().

Проверяем: gate без контекста, форму payload (org/agent/store/positions/
demand-link, цена в минорных единицах), сохранение moysklad_return_id и
идемпотентность повторного вызова.
"""

import asyncio

from aioresponses import aioresponses

import services.ms_demand as ms_demand
import services.ms_returns as ms_returns
from services.moysklad import MS_BASE


def _prime_ctx(monkeypatch):
    monkeypatch.setitem(ms_demand._CTX, "ready", True)
    monkeypatch.setitem(ms_demand._CTX, "org_meta", {"href": f"{MS_BASE}/entity/organization/ORG"})
    monkeypatch.setitem(ms_demand._CTX, "store_meta", {"href": f"{MS_BASE}/entity/store/STORE"})


def _make_confirmed_return(db, with_demand=True):
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "AGENT-UUID", "Client")
    db.add_order_item(oid, "Товар", f"{MS_BASE}/entity/product/PROD-UUID", 2, "шт", 150.0)
    db.update_order_status(oid, "shipped")
    if with_demand:
        db.set_order_ms_demand_id(oid, "DEMAND-OLD")
    items = db.get_order_items(oid)
    r = db.create_return(
        oid,
        "full",
        "брак партии",
        [(items[0]["id"], 2, 300.0)],
        refund_method="no_refund",
        created_by=1,
    )
    return oid, r["return_id"]


def test_salesreturn_noop_without_context(isolated_db, monkeypatch):
    monkeypatch.setitem(ms_demand._CTX, "ready", False)
    _oid, rid = _make_confirmed_return(isolated_db)
    res = asyncio.run(ms_returns.create_salesreturn(rid))
    assert res["ok"] is False
    assert "context" in res["reason"].lower() or "контекст" in res["reason"].lower()


def test_salesreturn_builds_payload_and_saves_id(isolated_db, monkeypatch):
    _prime_ctx(monkeypatch)
    db = isolated_db
    _oid, rid = _make_confirmed_return(db, with_demand=True)
    captured = {}

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:

                def _cb(url, **kwargs):
                    captured["payload"] = kwargs["json"]

                m.post(
                    f"{MS_BASE}/entity/salesreturn",
                    status=200,
                    payload={"id": "SR-NEW", "name": "Возврат"},
                    callback=_cb,
                )
                return await ms_returns.create_salesreturn(rid)
        finally:
            await moysklad.close_session()

    res = asyncio.run(scenario())
    assert res["ok"] is True and res["ms_id"] == "SR-NEW", res
    assert db.get_return(rid)["moysklad_return_id"] == "SR-NEW"

    p = captured["payload"]
    assert p["organization"]["meta"]["type"] == "organization"
    assert p["store"]["meta"]["type"] == "store"
    assert p["agent"]["meta"]["href"].endswith("/entity/counterparty/AGENT-UUID")
    assert p["demand"]["meta"]["href"].endswith("/entity/demand/DEMAND-OLD")
    pos = p["positions"][0]
    assert pos["price"] == 15000  # 150.0 → минорные единицы
    assert pos["quantity"] == 2
    assert pos["assortment"]["meta"]["type"] == "product"
    # Кастомных атрибутов быть не должно (meta привязана к demand → HTTP 400).
    assert "attributes" not in p


def test_salesreturn_idempotent_when_already_synced(isolated_db, monkeypatch):
    _prime_ctx(monkeypatch)
    db = isolated_db
    _oid, rid = _make_confirmed_return(db)
    db.set_return_ms_id(rid, "SR-EXISTING")
    # Не мокаем POST: если код попытается слать — упадёт. Значит не должен.
    res = asyncio.run(ms_returns.create_salesreturn(rid))
    assert res["ok"] is True
    assert res.get("skipped") == "already-synced"
