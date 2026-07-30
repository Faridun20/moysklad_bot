"""T2.4 — approve не создаёт второй документ в МойСклад.

Критерий готовности: повторный approve после краша между POST и записью id
находит СУЩЕСТВУЮЩИЙ документ, а не создаёт второй.

Три эшелона защиты:
  1. guard в approve — не постим, если ms_customerorder_id/ms_demand_id уже есть;
  2. детерминированный syncId + pre-search — даже если зашли параллельно,
     МойСклад отдаёт тот же документ;
  3. условный UPDATE ссылки — повторная запись не перетирает первый документ,
     иначе он становится сиротой, а товар уже зарезервирован/списан.

Мокаем ТРАНСПОРТ (aioresponses), а не свой код: сборка URL и payload реально
исполняется.
"""

import asyncio

import pytest
from aioresponses import aioresponses

import services.ms_customerorder as ms_customerorder
import services.ms_demand as ms_demand
from services.moysklad import MS_BASE, order_sync_id


# ─── Детерминированность ключа ──────────────────────────────────────────────


def test_sync_id_is_stable_and_entity_scoped():
    a = order_sync_id("customerorder", 42)
    assert a == order_sync_id("customerorder", 42), "ключ должен быть стабильным"
    assert a != order_sync_id("demand", 42), "разные сущности — разные ключи"
    assert a != order_sync_id("customerorder", 43), "разные заказы — разные ключи"


# ─── Условный UPDATE ссылок ─────────────────────────────────────────────────


def _order(db, uid=1):
    db.set_role(uid, "m", "M", "manager")
    oid = db.create_order(uid, "M", "")
    db.add_order_item(oid, "T", "href", 1, "шт", 100.0)
    db.update_order_agent(oid, "A-1", "Клиент")
    return oid


def test_customerorder_id_is_written_once(isolated_db):
    db = isolated_db
    oid = _order(db)

    assert db.set_order_ms_customerorder_id(oid, "CO-FIRST") is True
    # Повторная запись (второй approve) НЕ должна перетереть первый документ.
    assert db.set_order_ms_customerorder_id(oid, "CO-SECOND") is False
    assert asyncio.run(db.get_order(oid))["ms_customerorder_id"] == "CO-FIRST"


def test_demand_id_is_written_once(isolated_db):
    db = isolated_db
    oid = _order(db)

    assert db.set_order_ms_demand_id(oid, "D-FIRST") is True
    assert db.set_order_ms_demand_id(oid, "D-SECOND") is False
    assert asyncio.run(db.get_order(oid))["ms_demand_id"] == "D-FIRST"


# ─── syncId в payload + подхват существующего документа ────────────────────


def _prime_ctx(monkeypatch):
    monkeypatch.setitem(ms_demand._CTX, "ready", True)
    monkeypatch.setitem(
        ms_demand._CTX, "org_meta", {"href": f"{MS_BASE}/entity/organization/ORG"}
    )
    monkeypatch.setitem(ms_demand._CTX, "store_meta", {"href": f"{MS_BASE}/entity/store/ST"})
    monkeypatch.setitem(ms_demand._CTX, "attribute_name_meta", None)
    monkeypatch.setitem(ms_demand._CTX, "attribute_uid_meta", None)


ORDER = {"id": 42, "agent_id": "AGENT-UUID", "comment": ""}
ITEMS = [
    {
        "product_name": "Товар",
        "product_href": f"{MS_BASE}/entity/product/PROD",
        "quantity": 1,
        "price_cents": 10000,
    }
]


def _run(coro_factory):
    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            return await coro_factory()
        finally:
            await moysklad.close_session()

    return asyncio.run(scenario())


@pytest.mark.parametrize("entity", ["customerorder", "demand"])
def test_payload_carries_deterministic_sync_id(monkeypatch, entity):
    _prime_ctx(monkeypatch)
    captured: dict = {}
    expected = order_sync_id(entity, 42)

    async def factory():
        with aioresponses() as m:
            # pre-search: документа с таким syncId ещё нет
            m.get(
                f"{MS_BASE}/entity/{entity}?filter=syncId%3D{expected}&limit=1",
                status=200,
                payload={"rows": []},
            )

            def _cb(url, **kwargs):
                captured["payload"] = kwargs["json"]

            m.post(
                f"{MS_BASE}/entity/{entity}",
                status=200,
                payload={"id": "NEW-ID", "name": "x"},
                callback=_cb,
            )
            if entity == "customerorder":
                return await ms_customerorder.create_customerorder_from_request(
                    ORDER, ITEMS, "Менеджер", telegram_user_id=7
                )
            return await ms_demand.create_demand_from_request(
                ORDER, ITEMS, "Менеджер", telegram_user_id=7
            )

    res = _run(factory)
    assert res["ok"] is True, res
    assert captured["payload"]["syncId"] == expected


@pytest.mark.parametrize("entity", ["customerorder", "demand"])
def test_existing_document_is_adopted_not_duplicated(monkeypatch, entity):
    """Краш между POST и записью id: документ в МС уже есть. Повторная попытка
    должна ПОДХВАТИТЬ его, а не создать второй (иначе двойной резерв/списание)."""
    _prime_ctx(monkeypatch)
    sync_id = order_sync_id(entity, 42)
    posted: list = []

    async def factory():
        with aioresponses() as m:
            m.get(
                f"{MS_BASE}/entity/{entity}?filter=syncId%3D{sync_id}&limit=1",
                status=200,
                payload={"rows": [{"id": "EXISTING-DOC"}]},
            )
            # Если код всё-таки решит постить — зафиксируем это.
            m.post(
                f"{MS_BASE}/entity/{entity}",
                status=200,
                payload={"id": "DUPLICATE"},
                callback=lambda url, **kw: posted.append(url),
            )
            if entity == "customerorder":
                return await ms_customerorder.create_customerorder_from_request(
                    ORDER, ITEMS, "Менеджер", telegram_user_id=7
                )
            return await ms_demand.create_demand_from_request(
                ORDER, ITEMS, "Менеджер", telegram_user_id=7
            )

    res = _run(factory)
    assert res["ok"] is True, res
    assert res.get("adopted") is True
    key = "customerorder_id" if entity == "customerorder" else "demand_id"
    assert res[key] == "EXISTING-DOC"
    assert posted == [], "второй документ не должен создаваться"


def test_pre_search_failure_does_not_block_creation(monkeypatch):
    """Поиск — best-effort: если МС не ответил на GET, апрув всё равно идёт.
    Лучше дубль (его отсечёт syncId на стороне МС), чем сорванное одобрение."""
    _prime_ctx(monkeypatch)
    sync_id = order_sync_id("customerorder", 42)

    async def factory():
        with aioresponses() as m:
            m.get(
                f"{MS_BASE}/entity/customerorder?filter=syncId%3D{sync_id}&limit=1",
                status=500,
                payload={"errors": []},
            )
            m.post(
                f"{MS_BASE}/entity/customerorder",
                status=200,
                payload={"id": "CO-NEW", "name": "x"},
            )
            return await ms_customerorder.create_customerorder_from_request(
                ORDER, ITEMS, "Менеджер", telegram_user_id=7
            )

    res = _run(factory)
    assert res["ok"] is True, res
    assert res["customerorder_id"] == "CO-NEW"


# ─── Guard в approve ────────────────────────────────────────────────────────


def test_approve_skips_ms_when_documents_already_linked(isolated_db, monkeypatch):
    """Заказ уже имеет ссылки на документы — повторный approve не должен
    ходить в МойСклад вовсе."""
    db = isolated_db
    from services import order_workflow

    oid = _order(db)
    db.update_order_status(oid, "pending")
    rid = db.create_shipment_request(oid, 1, "M", "")
    db.set_order_ms_customerorder_id(oid, "CO-EXISTING")
    db.set_order_ms_demand_id(oid, "D-EXISTING")

    calls: list = []

    async def _boom_co(*a, **k):
        calls.append("customerorder")
        return {"ok": False}

    async def _boom_demand(*a, **k):
        calls.append("demand")
        return {"ok": False}

    monkeypatch.setattr(ms_customerorder, "create_customerorder_from_request", _boom_co)
    monkeypatch.setattr(ms_demand, "create_demand_from_request", _boom_demand)
    monkeypatch.setattr(ms_demand, "is_ready", lambda: True)

    res = asyncio.run(order_workflow.approve_shipment_request(rid, 2, "Boss", None))

    assert res.get("ok") is True, res
    assert calls == [], f"approve полез в МС при уже созданных документах: {calls}"
    order = asyncio.run(db.get_order(oid))
    assert order["ms_customerorder_id"] == "CO-EXISTING"
    assert order["ms_demand_id"] == "D-EXISTING"
