"""T2.6 — отмена заказа откатывает документ в МойСклад из ОБОИХ входов.

Критерий готовности: отмена через `/api/orders/cancel` делает POST реверса в МС.

§5.2.2: реверс `customerorder` делал только бот. Заказ, отменённый из WebApp,
оставался в МойСклад живым документом с резервом товара — навсегда: cron-
реконсиляция такие заказы не подбирает (`get_orders_with_ms_customerorder`
исключает `cancelled`), поэтому рассинхрон уже никто не заметил бы.

Мокаем ТРАНСПОРТ (aioresponses) — сборка URL реально исполняется.
"""

import asyncio
import importlib

import pytest
from aioresponses import aioresponses
from fastapi.testclient import TestClient

import services.ms_demand as ms_demand
import webapp.server as server
from services.moysklad import MS_BASE

CO_ID = "CO-UUID"


@pytest.fixture
def env(isolated_db, monkeypatch):
    db = isolated_db
    boss_id, mgr_id = 700, 701
    db.set_role(boss_id, "boss", "Boss", "boss")
    db.set_role(mgr_id, "mgr", "Mgr", "manager")

    import services.roles as roles

    importlib.reload(roles)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "B", "username": "b"},
    )
    # MS-контекст готов — иначе reverse_customerorder выходит раньше HTTP.
    monkeypatch.setattr(ms_demand, "is_ready", lambda: True)
    return db, {"boss": boss_id, "mgr": mgr_id}


def _approved_order_with_co(db, mgr_id):
    oid = db.create_order(mgr_id, "Mgr", "")
    db.add_order_item(oid, "T", "href", 1, "шт", 100.0)
    db.update_order_agent(oid, "A-1", "Клиент")
    db.update_order_status(oid, "pending")
    db.update_order_status(oid, "approved")
    db.set_order_ms_customerorder_id(oid, CO_ID)
    return oid


def _order(db, oid):
    return asyncio.run(db.get_order(oid))


# ─── Критерий готовности: WebApp делает реверс ──────────────────────────────


def test_webapp_cancel_reverses_customerorder_in_ms(env):
    db, ids = env
    oid = _approved_order_with_co(db, ids["mgr"])
    deleted: list = []

    from services import moysklad

    moysklad._session = None
    try:
        with aioresponses() as m:
            m.delete(
                f"{MS_BASE}/entity/customerorder/{CO_ID}",
                status=200,
                payload={},
                callback=lambda url, **kw: deleted.append(str(url)),
            )
            client = TestClient(server.app)
            r = client.post(
                "/api/orders/cancel",
                json={
                    "initData": str(ids["boss"]),
                    "order_id": oid,
                    "reason": "клиент передумал",
                },
            )
    finally:
        asyncio.run(moysklad.close_session())

    assert r.status_code == 200, r.text
    assert len(deleted) == 1, "реверс customerorder в МС не отправлен"
    assert CO_ID in deleted[0]

    order = _order(db, oid)
    assert order["status"] == "cancelled"
    assert order["ms_cancel_synced_at"], "ms_cancel_synced_at не проставлен"


# ─── Оба входа ведут себя одинаково ─────────────────────────────────────────


def test_bot_and_webapp_use_the_same_code():
    """Оба входа зовут order_workflow.cancel_order_full, а не свои цепочки."""
    import inspect

    import handlers.order_cancel as bot_cancel

    bot_src = inspect.getsource(bot_cancel)
    assert "cancel_order_full" in bot_src
    assert "ms_cancel.reverse_customerorder" not in bot_src, "осталась своя цепочка"

    server_src = inspect.getsource(server.api_orders_cancel)
    assert "cancel_order_full" in server_src


def test_cancel_order_full_reverses_and_marks_synced(env):
    db, ids = env
    from services.order_workflow import cancel_order_full

    oid = _approved_order_with_co(db, ids["mgr"])

    from services import moysklad

    moysklad._session = None
    try:
        with aioresponses() as m:
            m.delete(f"{MS_BASE}/entity/customerorder/{CO_ID}", status=204, payload={})
            res = asyncio.run(cancel_order_full(oid, ids["boss"], "Boss", "причина"))
    finally:
        asyncio.run(moysklad.close_session())

    assert res["ok"] is True
    assert res["ms_reverse"]["ok"] is True
    assert _order(db, oid)["ms_cancel_synced_at"]


# ─── Границы ────────────────────────────────────────────────────────────────


def test_ms_failure_does_not_roll_back_local_cancel(env):
    """Реверс — best-effort: МС ответил 500, но отмена уже применена локально.
    Откатывать подтверждённое оператором действие из-за МС нельзя."""
    db, ids = env
    from services.order_workflow import cancel_order_full

    oid = _approved_order_with_co(db, ids["mgr"])

    from services import moysklad

    moysklad._session = None
    try:
        with aioresponses() as m:
            m.delete(f"{MS_BASE}/entity/customerorder/{CO_ID}", status=500, payload={})
            res = asyncio.run(cancel_order_full(oid, ids["boss"], "Boss", "причина"))
    finally:
        asyncio.run(moysklad.close_session())

    assert res["ok"] is True, "локальная отмена должна состояться"
    assert res["ms_reverse"]["ok"] is False
    order = _order(db, oid)
    assert order["status"] == "cancelled"
    # Флаг НЕ проставлен — значит расхождение видно, а не замаскировано.
    assert not order["ms_cancel_synced_at"]


def test_order_without_ms_document_needs_no_http(env):
    """Заказ не доехал до МС — реверсить нечего, HTTP не дёргаем."""
    db, ids = env
    from services.order_workflow import cancel_order_full

    oid = db.create_order(ids["mgr"], "Mgr", "")
    db.add_order_item(oid, "T", "href", 1, "шт", 100.0)
    db.update_order_agent(oid, "A-1", "Клиент")
    db.update_order_status(oid, "pending")
    db.update_order_status(oid, "approved")

    from services import moysklad

    moysklad._session = None
    try:
        with aioresponses():  # ни одного мока: любой HTTP упадёт
            res = asyncio.run(cancel_order_full(oid, ids["boss"], "Boss", "причина"))
    finally:
        asyncio.run(moysklad.close_session())

    assert res["ok"] is True
    assert res["ms_reverse"].get("skipped") == "no-customerorder"


def test_failed_local_cancel_skips_ms(env):
    """Если локальная отмена не прошла (недопустимый статус) — в МС не лезем."""
    db, ids = env
    from services.order_workflow import cancel_order_full

    oid = _approved_order_with_co(db, ids["mgr"])
    db.update_order_status(oid, "cancelled")  # уже отменён

    from services import moysklad

    moysklad._session = None
    try:
        with aioresponses():  # ни одного мока
            res = asyncio.run(cancel_order_full(oid, ids["boss"], "Boss", "причина"))
    finally:
        asyncio.run(moysklad.close_session())

    assert res["ok"] is False
    assert "ms_reverse" not in res
