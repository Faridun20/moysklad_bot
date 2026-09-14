"""Приёмка контейнера и склад: целостность при гонках и удалении.

* п.5 — параллельные приёмки одного контейнера не проводят две приходные;
* п.6 — удаление оприходованного контейнера отменяет его приходную накладную
  (и отказывает понятным текстом, если товар уже ушёл);
* п.7 — накладную, привязанную к отгрузке заказа или приёмке контейнера,
  нельзя отменить в обход заказа/контейнера.

БД настоящая (isolated_db), корутины через asyncio.run.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def _stock(pid):
    from services import adb_core

    return float(_run(adb_core.fetchval(
        "SELECT COALESCE(SUM(quantity), 0) FROM stock WHERE product_id = $1", pid
    )) or 0)


def _active_incoming():
    from services import adb_core

    return int(_run(adb_core.fetchval(
        "SELECT COUNT(*) FROM invoices WHERE type = 'incoming' AND status != 'cancelled'"
    )) or 0)


def _arrived_container(qty=8, number="MSKU-1111111"):
    from services import container_receipt, containers

    pid = _run(container_receipt.create_product("Кабель ВВГ 3x2.5"))["product_id"]
    cid = _run(containers.create_container(number=number, created_by=2, creator_name="Boss"))["container_id"]
    item = _run(containers.add_item(cid, name="Кабель ВВГ 3x2.5", expected_qty=qty, product_id=pid))["item_id"]
    assert _run(containers.mark_arrived(cid, user_id=2))["ok"]
    assert _run(containers.set_arrived_quantities(cid, {item: qty}, user_id=2))["ok"]
    return cid, pid


# ─── п.5: параллельная приёмка ────────────────────────────────────────────────


def test_concurrent_receive_books_the_container_once(isolated_db):
    from services import container_receipt

    _setup(isolated_db)
    cid, pid = _arrived_container(qty=8)

    async def many():
        return await asyncio.gather(
            *(container_receipt.receive(cid, user_id=2) for _ in range(4)),
            return_exceptions=True,
        )

    results = _run(many())
    assert all(isinstance(r, dict) and r["ok"] for r in results), results
    # Сколько бы приёмок ни прошло, проведена ОДНА приходная и товар не удвоен.
    assert _stock(pid) == 8
    assert _active_incoming() == 1
    link = _run(container_receipt.get_link(cid))
    from services import adb_core

    status = _run(adb_core.fetchval("SELECT status FROM invoices WHERE id = $1", link["invoice_id"]))
    assert status == "confirmed"


def test_supply_endpoint_replays_same_idempotency_key(isolated_db, monkeypatch):
    _setup(isolated_db)
    cid, pid = _arrived_container(qty=5)
    client = _client(monkeypatch)

    first = _post(client, "/api/containers/supply", 1, container_id=cid, idempotency_key="s-1")
    again = _post(client, "/api/containers/supply", 1, container_id=cid, idempotency_key="s-1")
    assert first.status_code == 200, first.text
    assert again.json() == first.json()
    assert _stock(pid) == 5
    from services import adb_core

    # Повтор с тем же ключом не переоприходовал: в истории одна накладная, без отменённой.
    assert int(_run(adb_core.fetchval("SELECT COUNT(*) FROM invoices WHERE type = 'incoming'"))) == 1
