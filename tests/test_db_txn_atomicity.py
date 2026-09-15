"""
Транзакционная корректность денежных и складских операций — логика на SQLite.

Гонки как таковые проверяет `tests/test_txn_races_postgres.py` (на SQLite
пишущая транзакция одна, `BEGIN IMMEDIATE`, и разные замки не видны). Здесь —
то, что от драйвера не зависит:

* подтверждение платежа, отмена заказа, возврат заявки на доработку и закрытие
  рассрочки — ОДНОЙ транзакцией: сбой посередине не оставляет полусостояния;
* ручное распределение сдачи и автоплатёж одобрения считают «можно заявить»
  той же формулой, что отметка оплаты;
* правка черновика перепроверяет статус в транзакции записи;
* ключ идемпотентности: результат пишется вместе с операцией, отказ проверок
  освобождает ключ, брошенный ключ переиспользуется;
* разовые backfill'ы не повторяются на каждом старте.

БД настоящая (isolated_db), подменяется только граница с Telegram и точки
внедрения сбоя.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        conn.commit()


def _credit_order(db, *, total=100.0, status="shipped", owner=1, payment_type="credit"):
    oid = db.create_order(owner, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    _exec(
        db,
        "UPDATE orders SET payment_type = ?, due_date = ?, currency = 'USD' WHERE id = ?",
        (payment_type, "2030-01-15" if payment_type == "credit" else None, oid),
    )
    db.update_order_status(oid, status)
    return oid


# ─── confirm_payment: одна транзакция ────────────────────────────────────────


def test_confirm_payment_failure_before_close_leaves_payment_pending(isolated_db, monkeypatch):
    """Сбой на закрытии заказа откатывает и CAS платежа: раньше платёж
    оставался confirmed, заказ — открытым, а повтор отвечал «уже подтверждён»."""
    db = isolated_db
    db.set_role(1, "m", "Manager", "manager")
    oid = _credit_order(db)
    ok, pid = _run(db.mark_order_paid(oid, 1, "Manager", amount=100.0))
    assert ok

    real = db._close_order_if_covered_locked

    async def boom(*a, **kw):
        raise RuntimeError("сбой между CAS платежа и закрытием заказа")

    monkeypatch.setattr(db, "_close_order_if_covered_locked", boom)
    with pytest.raises(RuntimeError):
        _run(db.confirm_payment(pid, 2, "Boss"))
    pay = _run(db.get_payment(pid))
    assert pay["status"] == "pending" and pay["fx_rate_to_base"] is None
    assert _run(db.get_order(oid))["paid_confirmed_at"] is None

    monkeypatch.setattr(db, "_close_order_if_covered_locked", real)
    assert _run(db.confirm_payment(pid, 2, "Boss")) is True
    pay = _run(db.get_payment(pid))
    assert pay["status"] == "confirmed" and pay["fx_rate_to_base"] is not None
    assert _run(db.get_order(oid))["paid_confirmed_at"] is not None
    assert _run(db.confirm_payment(pid, 2, "Boss")) is False


# ─── «Можно заявить»: ручная сдача и автоплатёж ─────────────────────────────


def test_manual_deposit_allocation_cannot_exceed_claimable(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "Manager", "manager")
    oid = _credit_order(db, total=100.0)
    assert _run(db.mark_order_paid(oid, 1, "Manager", amount=60.0))[0]

    res = _run(db.create_cash_deposit(1, 50.0, allocations=[(oid, 50.0)]))
    assert res["ok"] is False and "40.00" in res["error"]
    assert _run(db.get_manager_cash_deposits(1)) == []

    assert _run(db.create_cash_deposit(1, 40.0, allocations=[(oid, 40.0)]))["ok"]


# ─── Отмена заказа: статус и склад вместе ────────────────────────────────────


@pytest.fixture
def shipped_env(isolated_db):
    """Склад 10 шт., заявка одобрена — 2 шт. списаны накладной."""
    from services import container_receipt, warehouse
    from services.order_workflow import approve_shipment_request, submit_order

    db = isolated_db
    db.set_role(100, "boss", "Boss", "boss")
    db.set_role(200, "mgr", "Manager", "manager")
    _exec(db, "INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)",
          ("Клиент", "customer", "", db.now_str()))
    pid = _run(container_receipt.create_product("Кабель"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))
    oid = db.create_order(200, "Manager", "")
    db.update_order_agent(oid, "1", "Клиент")
    db.add_order_item(oid, "Кабель", "", 2, "шт", 5.0, product_id=pid)
    sub = _run(submit_order(oid, 200, "Manager", payment_type="credit", due_date="2030-01-15"))
    assert _run(approve_shipment_request(sub["req_id"], 100, "Boss", None))["ok"]

    def stock() -> float:
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(db.q("SELECT SUM(quantity) AS q FROM stock WHERE product_id = ?"), (pid,))
            row = cur.fetchone()
        return float((row["q"] if hasattr(row, "keys") else row[0]) or 0)

    return db, oid, stock


def test_cancel_refused_by_warehouse_changes_nothing(shipped_env, monkeypatch):
    from services import order_shipment, warehouse
    from services.order_workflow import cancel_order_full

    db, oid, stock = shipped_env
    assert stock() == 8

    async def refuse(txn, invoice_id, user_id):
        raise warehouse.InvoiceError("locked", "Накладная закрыта периодом")

    monkeypatch.setattr(warehouse, "cancel_invoice_in", refuse)
    res = _run(cancel_order_full(oid, 100, "Boss", "Клиент передумал"))
    assert res["ok"] is False and "Накладная закрыта периодом" in res["error"]
    assert _run(db.get_order(oid))["status"] == "approved", "отмена без возврата остатка не проходит"
    assert stock() == 8
    assert _run(order_shipment.get_shipment(oid))["invoice_id"]


def test_crash_after_stock_reversal_rolls_back_the_whole_cancel(shipped_env, monkeypatch):
    """Раньше статус коммитился первым, склад — потом: сбой между ними оставлял
    отменённый заказ со списанным навсегда товаром."""
    from services import order_shipment
    from services.order_workflow import cancel_order_full

    db, oid, stock = shipped_env
    real = order_shipment.cancel_shipment_locked

    async def reverse_then_crash(txn, order_id, *, user_id=None):
        await real(txn, order_id, user_id=user_id)
        raise RuntimeError("процесс убит посередине отмены")

    monkeypatch.setattr(order_shipment, "cancel_shipment_locked", reverse_then_crash)
    with pytest.raises(RuntimeError):
        _run(cancel_order_full(oid, 100, "Boss", "Клиент передумал"))
    assert _run(db.get_order(oid))["status"] == "approved"
    assert stock() == 8
    assert _run(order_shipment.get_shipment(oid))["invoice_id"], "накладная не отменена наполовину"

    monkeypatch.setattr(order_shipment, "cancel_shipment_locked", real)
    res = _run(cancel_order_full(oid, 100, "Boss", "Клиент передумал"))
    assert res["ok"] and res["stock_reverse"]["invoice_id"]
    assert _run(db.get_order(oid))["status"] == "cancelled"
    assert stock() == 10
    again = _run(cancel_order_full(oid, 100, "Boss", "ещё раз"))
    assert again["ok"] is False and stock() == 10


# ─── Возврат на доработку: заказ и заявка вместе ─────────────────────────────


def test_return_to_draft_keeps_order_pending_when_request_already_decided(isolated_db, monkeypatch):
    import services.async_db as adb
    from services.order_workflow import return_order_to_draft

    db = isolated_db
    db.set_role(200, "mgr", "Manager", "manager")
    oid = db.create_order(200, "Manager", "")
    db.update_order_agent(oid, "A-1", "Client")
    db.add_order_item(oid, "Product", "", 2, "шт", 100.0)
    db.update_order_status(oid, "pending")
    req_id = db.create_shipment_request(oid, 200, "Manager")

    # Второй босс решил заявку между чтением и записью: ручка видит её ещё
    # pending. Раньше заказ уезжал в draft одним коммитом, а заявка
    # помечалась другим — здесь CAS заявки откатывает и заказ.
    snapshot = db.get_shipment_request(req_id)
    _exec(db, "UPDATE shipment_requests SET status = 'rejected' WHERE id = ?", (req_id,))

    async def stale_request(rid):
        return snapshot

    monkeypatch.setattr(adb, "get_shipment_request", stale_request, raising=False)
    res = _run(return_order_to_draft(req_id, 100, "Boss", "доработай", None))
    assert res["ok"] is False and "уже обработана" in res["error"]
    order = _run(db.get_order(oid))
    assert order["status"] == "pending" and int(order["rejection_count"] or 0) == 0
    assert _run(db.get_last_reject_snapshot(oid)) is None, "снимок не пишется без решения"


def test_return_to_draft_writes_order_request_and_snapshot_together(isolated_db):
    from services.order_workflow import return_order_to_draft

    db = isolated_db
    db.set_role(200, "mgr", "Manager", "manager")
    oid = db.create_order(200, "Manager", "")
    db.update_order_agent(oid, "A-1", "Client")
    db.add_order_item(oid, "Product", "", 2, "шт", 100.0)
    db.update_order_status(oid, "pending")
    req_id = db.create_shipment_request(oid, 200, "Manager")

    res = _run(return_order_to_draft(req_id, 100, "Boss", "доработай", None))
    assert res["ok"] and res["rejection_count"] == 1
    assert _run(db.get_order(oid))["status"] == "draft"
    assert db.get_shipment_request(req_id)["status"] == "returned"
    snap = _run(db.get_last_reject_snapshot(oid))
    assert snap["total"] == 200.0 and snap["items"][0]["price"] == 100.0


# ─── Правка черновика: статус в транзакции записи ────────────────────────────


def test_item_edits_recheck_draft_inside_write(isolated_db, monkeypatch):
    """Ручка проверяет черновик отдельным чтением; сабмит между ним и записью
    раньше получал позицию в уже отправленный заказ."""
    import services.async_db as adb
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    db.set_role(200, "m", "Manager", "manager")
    monkeypatch.setattr(
        server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U", "username": "u"}
    )
    oid = db.create_order(200, "Manager", "")
    db.update_order_agent(oid, "1", "Клиент")
    item = db.add_order_item(oid, "Товар", "", 1, "шт", 10.0)
    draft_snapshot = _run(db.get_order(oid))
    db.update_order_status(oid, "pending")  # «сабмит успел»

    async def stale_order(order_id):
        return dict(draft_snapshot)

    monkeypatch.setattr(adb, "get_order", stale_order, raising=False)
    client = TestClient(server.app)
    r = client.post("/api/orders/add_item", json={
        "initData": "200", "order_id": oid, "product_name": "Лишнее", "quantity": 1, "price": 5,
    })
    assert r.status_code == 409
    r = client.post("/api/orders/remove_item", json={"initData": "200", "item_id": item})
    assert r.status_code == 409
    r = client.post("/api/orders/set_agent", json={
        "initData": "200", "order_id": oid, "agent_id": "2", "agent_name": "Другой",
    })
    assert r.status_code == 409
    items = _run(db.get_order_items(oid))
    assert [it["product_name"] for it in items] == ["Товар"]
    assert _run(db.get_order(oid))["agent_name"] == "Клиент"

    assert db.add_order_item(oid, "x", "", 1, "шт", 1.0, require_draft=True) is None
    assert db.update_order_currency(oid, "UZS", require_draft=True) is False


# ─── Рассрочка: закрытие в транзакции поступления ────────────────────────────


def test_installment_closure_commits_with_the_receipt(isolated_db, monkeypatch):
    import services.machines as machines

    importlib.reload(machines)
    import services.roles as roles

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(2, "boss", "Boss", "boss")
    mid = _run(machines.create_machine(vin="VIN-1", name="JCB", created_by=2, price_cents=300_000))[
        "machine_id"
    ]
    deal = _run(machines.create_deal(
        mid, kind="credit", price_cents=300_000, buyer_name="Иванов",
        created_by=2, down_payment_cents=100_000, months=2,
    ))
    assert deal["ok"], deal
    deal_id = deal["deal_id"]
    assert _run(machines.add_receipt(deal_id, 100_000, user_id=2))["deal_closed"] is False

    async def audit_down(*a, **kw):
        raise RuntimeError("процесс умер после коммита поступления")

    real_audit = machines._audit
    monkeypatch.setattr(machines, "_audit", audit_down)
    # Аудит после коммита best-effort: деньги записаны — ответ успешный.
    assert _run(machines.add_receipt(deal_id, 100_000, user_id=2)) == {"ok": True, "deal_closed": True}
    # Закрытие уже в той же транзакции: сделка закрыта, машина продана,
    # и лишнее поступление сверх цены больше не принимается.
    head = _run(machines.adb_core.fetchrow("SELECT closed_at FROM machine_deals WHERE id = $1", deal_id))
    assert head["closed_at"] is not None
    status = _run(machines.adb_core.fetchval("SELECT status FROM machines WHERE id = $1", mid))
    assert status == "sold"
    assert _run(machines.add_receipt(deal_id, 50_000, user_id=2))["ok"] is False

    # Удаление последнего поступления открывает сделку той же транзакцией.
    # Не monkeypatch.undo(): он снял бы и DB_PATH фикстуры.
    monkeypatch.setattr(machines, "_audit", real_audit)
    last = _run(machines.list_receipts(deal_id))[0]
    res = _run(machines.delete_receipt(last["id"], user_id=2))
    assert res["deal_reopened"] is True
    head = _run(machines.adb_core.fetchrow("SELECT closed_at FROM machine_deals WHERE id = $1", deal_id))
    assert head["closed_at"] is None
    assert _run(machines.adb_core.fetchval("SELECT status FROM machines WHERE id = $1", mid)) == "on_credit"


# ─── Идемпотентность ─────────────────────────────────────────────────────────


@pytest.fixture
def api(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    db.set_role(200, "m", "Manager", "manager")
    db.set_role(100, "b", "Boss", "boss")
    monkeypatch.setattr(
        server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U", "username": "u"}
    )

    async def _no_notify(*a, **kw):
        return None

    monkeypatch.setattr(server, "_notify_bosses_payment_pending", _no_notify)
    return db, server, TestClient(server.app, raise_server_exceptions=False)


def test_rejected_mark_paid_releases_its_key(api):
    db, _server, client = api
    body = {"initData": "200", "idempotency_key": "k-404", "parts": [{"method": "card", "currency": "USD", "amount": 10}]}
    r = client.post("/api/orders/mark_paid", json={**body, "order_id": 999_999})
    assert r.status_code == 404
    oid = _credit_order(db, owner=200)
    r = client.post("/api/orders/mark_paid", json={**body, "order_id": oid})
    assert r.status_code == 200, r.text  # раньше — сутки «Запрос уже обрабатывается»


def test_mark_paid_result_is_stored_with_the_payment(api, monkeypatch):
    """Процесс умер после коммита платежа, до записи ответа: ретрай получает
    записанный платёж, а не 409 и не второй платёж."""
    db, server, client = api
    oid = _credit_order(db, owner=200)

    async def crash(*a, **kw):
        raise RuntimeError("упали после коммита")

    monkeypatch.setattr(server, "_notify_bosses_payment_pending", crash)
    body = {"initData": "200", "order_id": oid, "parts": [{"method": "card", "currency": "USD", "amount": 10}], "idempotency_key": "k-crash"}
    assert client.post("/api/orders/mark_paid", json=body).status_code == 500
    r = client.post("/api/orders/mark_paid", json=body)
    assert r.status_code == 200, r.text
    pays = _run(db.get_payments_for_order(oid))
    assert len(pays) == 1 and r.json()["ok"] and r.json()["payment_id"] == pays[0]["id"]


def test_abandoned_key_is_reclaimed_only_for_atomic_operations(api):
    db, _server, client = api
    oid = _credit_order(db, owner=200)
    old = "2000-01-01 00:00:00"
    for key, op in (("mark_paid:200:k-old", "mark_paid"), ("confirm_payment:100:k-old", "confirm_payment")):
        _exec(
            db,
            "INSERT INTO idempotency_keys (key, operation, user_id, result, created_at, expires_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (key, op, 200, old, "2999-01-01 00:00:00"),
        )
    r = client.post("/api/orders/mark_paid", json={
        "initData": "200", "order_id": oid, "parts": [{"method": "card", "currency": "USD", "amount": 10}], "idempotency_key": "k-old",
    })
    assert r.status_code == 200, r.text
    # Не атомарная операция: пустой ключ мог скрывать проведённое — только 409.
    r = client.post("/api/orders/confirm_payment", json={
        "initData": "100", "order_id": oid, "idempotency_key": "k-old",
    })
    assert r.status_code == 409


def test_rejected_return_create_releases_its_key(api):
    db, _server, client = api
    theirs = _credit_order(db, owner=100)
    body = {"initData": "200", "reason": "брак товара", "refund_method": "no_refund",
            "idempotency_key": "k-ret"}
    r = client.post("/api/returns/create", json={**body, "order_id": theirs})
    assert r.status_code == 403
    mine = _credit_order(db, owner=200)
    r = client.post("/api/returns/create", json={**body, "order_id": mine})
    assert r.status_code == 200, r.text


# ─── Разовые backfill'ы ──────────────────────────────────────────────────────


def test_one_time_backfills_do_not_rerun_on_every_start(isolated_db):
    db = isolated_db
    first = db.run_backfills()
    assert all(v != "skipped" for v in first.values())

    # Заказ с paid_at без строк payments появился ПОСЛЕ разовой миграции
    # (так выглядит перенесённый из истории). Деплой не должен его «закрыть».
    oid = _credit_order(db)
    _exec(db, "UPDATE orders SET paid_at = ? WHERE id = ?", ("2026-01-01 10:00:00", oid))
    second = db.run_backfills()
    assert set(second.values()) == {"skipped"}
    assert _run(db.get_order(oid))["paid_confirmed_at"] is None

    forced = db.run_backfills(rerun=("legacy_paid_confirmed",))
    assert forced["legacy_paid_confirmed"]["orders"] == 1
    assert forced["local_identifiers"] == "skipped"


def test_migrate_refuses_unknown_backfill_name(isolated_db):
    from tasks import migrate

    assert migrate.main(["--rerun-backfill", "local_identifers"]) == 2


# ─── Тяжёлые запросы ─────────────────────────────────────────────────────────


def test_orders_page_in_sql_matches_python_reference(isolated_db):
    """SQL-страница обязана совпадать с эталонной нарезкой `_paginate_orders`."""
    from webapp.server import _paginate_orders

    db = isolated_db
    statuses = ["draft", "pending", "approved", "shipped", "paid"]
    for i in range(23):
        oid = db.create_order(200 if i % 3 else 201, "Manager", "")
        db.update_order_status(oid, statuses[i % len(statuses)])
        _exec(db, "UPDATE orders SET created_at = ? WHERE id = ?",
              (f"2026-09-{(i % 9) + 1:02d} 1{i % 10}:00:00", oid))

    everything = _run(db.get_all_orders())
    cases = [
        dict(statuses=[], date_from="", date_to=""),
        dict(statuses=["approved", "shipped"], date_from="", date_to=""),
        dict(statuses=[], date_from="2026-09-03", date_to="2026-09-05"),
        dict(statuses=["pending"], date_from="2026-09-02", date_to=""),
    ]
    for case in cases:
        for offset in (0, 4, 20):
            ref, meta = _paginate_orders(everything, limit=4, offset=offset, **case)
            rows, total, pending = _run(db.get_orders_page(scope="all", limit=4, offset=offset, **case))
            assert [o["id"] for o in rows] == [o["id"] for o in ref], case
            assert (total, pending) == (meta["total"], meta["pending_count"])

    mine, total, _ = _run(db.get_orders_page(scope="user", user_id=201, limit=50, offset=0))
    assert total == len(mine) and {o["user_id"] for o in mine} == {201}
    keeper, _, pending = _run(db.get_orders_page(scope="to_ship", limit=50, offset=0))
    assert {o["status"] for o in keeper} == {"approved", "shipped"} and pending == 0


def test_order_items_batch_is_chunked(isolated_db, monkeypatch):
    db = isolated_db
    ids = []
    for i in range(5):
        oid = db.create_order(1, "M", "")
        db.add_order_item(oid, f"T{i}", "", 1, "шт", 1.0)
        ids.append(oid)
    monkeypatch.setattr(db, "_IN_CHUNK", 2)
    got = _run(db.get_order_items_by_ids(ids))
    assert sorted(got) == sorted(ids)


def test_sales_stats_count_every_shipment_beyond_the_list_limit(isolated_db):
    """1 005 отгрузок за период: раньше итоги считались по списку из 1 000."""
    from services import warehouse

    db = isolated_db
    assert db.set_currency_rate("UZS", 0.0001, 1)[0]
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO products (name, unit, created_at) VALUES (?, ?, ?)"),
                    ("Кабель", "шт", db.now_str()))
        pid = cur.lastrowid
        cur.execute(db.q("INSERT INTO counterparties (name, type, created_at) VALUES (?, ?, ?)"),
                    ("Клиент", "customer", db.now_str()))
        cp = cur.lastrowid
        for n in range(1005):
            cur_code = "UZS" if n % 5 == 0 else "USD"
            cur.execute(
                db.q("INSERT INTO invoices (type, counterparty_id, warehouse_id, invoice_number, "
                     "invoice_date, status, currency, total_amount_cents, created_at) "
                     "VALUES ('outgoing', ?, 1, ?, ?, 'confirmed', ?, ?, ?)"),
                (cp if n % 2 else None, f"T-{n}", f"2026-08-{(n % 28) + 1:02d}", cur_code, 1000,
                 db.now_str()),
            )
            cur.execute(
                db.q("INSERT INTO invoice_items (invoice_id, product_id, quantity, price_cents) "
                     "VALUES (?, ?, 1, 1000)"),
                (cur.lastrowid, pid),
            )
        conn.commit()

    stats = _run(warehouse.sales_stats("2026-08-01", "2026-08-31"))
    assert stats["count"] == 1005
    assert stats["by_currency"] == {"USD": 804 * 1000, "UZS": 201 * 1000}
    # UZS пересчитываются один раз на валюту: 201 000 × 0.0001 = 20.1 → 20.
    assert stats["base_total"] == 804_000 + 20
    assert stats["base_count"] == 1005 and stats["clients"] == 1
    top = stats["top_products"]
    assert [(name, d["currency"], d["sum"]) for name, d in top] == [
        ("Кабель", "USD", 804_000), ("Кабель", "UZS", 201_000),
    ]
    days = _run(warehouse.shipment_counts_by_day("2026-08-01", "2026-08-31"))
    assert sum(days.values()) == 1005
