"""Одобрение ↔ склад: копейки автоплатежа, гонка «одобрить против отменить»,
резерв и дайджест «нужна доделка».

* Автоплатёж «оплата сразу» считался суммой float-произведений, а закрытие
  заказа — построчно в копейках. На дробных количествах суммы расходились на
  копейку: платёж «на всю сумму» оставлял долг 0,01, заказ не закрывался.
* Между CAS одобрения и расходной накладной босс успевал отменить заказ:
  отмена не находила накладной (её ещё нет), а списание потом проходило по
  отменённому заказу. Статус теперь перепроверяется под замком строки заказа.
* Процесс, убитый между одобрением и списанием, не оставлял даже `failed_at`,
  и заказ не попадал в дайджест.
* Списанный накладной заказ не резерв: иначе он вычитается из доступного дважды.
"""

from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(isolated_db):
    """Склад с 10 шт. кабеля, менеджер 200, босс 100, клиент."""
    from services import container_receipt, warehouse

    db = isolated_db
    db.set_role(100, "boss", "Boss", "boss")
    db.set_role(200, "mgr", "Manager", "manager")
    pid = _run(container_receipt.create_product("Кабель"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"),
            ("Клиент", "customer", "", db.now_str()),
        )
        conn.commit()

    def request(lines=((2, 5.0),), payment_type="paid"):
        from services.order_workflow import submit_order

        oid = db.create_order(200, "Manager", "")
        db.update_order_agent(oid, "1", "Клиент")
        for qty, price in lines:
            db.add_order_item(oid, "Кабель", "", qty, "шт", price, product_id=pid)
        due = "2030-01-15" if payment_type == "credit" else None
        res = _run(submit_order(oid, 200, "Manager", payment_type=payment_type, due_date=due))
        assert res["ok"], res
        return oid, res["req_id"]

    return db, pid, request


def _stock(db, pid) -> float:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT SUM(quantity) AS q FROM stock WHERE product_id = ?"), (pid,))
        row = cur.fetchone()
    return float((row["q"] if hasattr(row, "keys") else row[0]) or 0)


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        conn.commit()


def _active_outgoing(db) -> int:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(
            "SELECT COUNT(*) AS n FROM invoices WHERE type = 'outgoing' AND status <> 'cancelled'"
        ))
        row = cur.fetchone()
    return int(row["n"] if hasattr(row, "keys") else row[0])


# ─── «Оплата сразу» — в копейках, без автоплатежа ────────────────────────────


def test_paid_order_breakdown_equals_total_in_cents_and_closes_order(env):
    """2 строки × 1,5 шт × 0,33: построчно 50 + 50 = 100 копеек, float-сумма
    давала 0,99. Одобрение платежа больше не создаёт (автоплатежа нет); сумма к
    оплате, которую требует разбивка, совпадает с суммой закрытия заказа."""
    from services import order_payments
    from services.database import confirm_payment, get_order_payment_summary, get_payments_for_order
    from services.order_workflow import approve_shipment_request

    db, _pid, request = env
    oid, req_id = request(lines=((1.5, 0.33), (1.5, 0.33)))
    res = _run(approve_shipment_request(req_id, 100, "Boss", None))
    assert res["ok"], res
    assert _run(get_payments_for_order(oid)) == [], "одобрение деньги не заявляет"
    assert _run(order_payments.payment_gap_cents([oid]))[oid] == 100

    actor = order_payments.Actor(user_id=100, name="Boss", role="boss")
    rec = _run(order_payments.record_payment_parts(
        oid, actor, [{"method": "card", "currency": "USD", "amount": "1"}]))
    payments = _run(get_payments_for_order(oid))
    summary = _run(get_order_payment_summary(oid))
    assert summary["total_cents"] == 100
    assert [p["amount_cents"] for p in payments] == [100] == [rec["total_cents"]]

    assert _run(confirm_payment(payments[0]["id"], 100, "Boss"))
    summary = _run(get_order_payment_summary(oid))
    assert summary["remaining_cents"] == 0, "платёж на всю сумму не оставляет копейку долга"
    assert _run(db.get_order(oid))["paid_confirmed_at"]


# ─── «Одобрить против отменить» ──────────────────────────────────────────────


def test_ship_order_refuses_order_cancelled_after_snapshot(env):
    """`order` пришёл снимком «approved», а в БД заказ уже отменён — списания нет."""
    from services import order_shipment

    db, pid, request = env
    oid, _req = request()
    db.update_order_status(oid, "approved")
    order = _run(db.get_order(oid))
    items = _run(db.get_order_items(oid))
    _exec(db, "UPDATE orders SET status = 'cancelled' WHERE id = ?", (oid,))

    res = _run(order_shipment.ship_order(order, items, user_id=100))
    assert res["ok"] is False and res["code"] == "order_moved", res
    assert _active_outgoing(db) == 0
    assert _stock(db, pid) == 10
    row = _run(order_shipment.get_shipment(oid))
    assert row is None, "отменённый заказ — не «нужна доделка»"


def test_cancel_between_approval_and_writeoff_leaves_stock_intact(env, monkeypatch):
    """Отмена успевает между CAS одобрения и накладной: отмена накладной не
    находит, списание видит под замком статус cancelled и не проводится."""
    from services import order_shipment
    from services.database import get_payments_for_order
    from services.order_workflow import approve_shipment_request, cancel_order_full

    db, pid, request = env
    oid, req_id = request()
    real_ship = order_shipment.ship_order

    async def cancel_first(order, items, *, user_id=None):
        cancelled = await cancel_order_full(order["id"], 100, "Boss", "Клиент передумал")
        assert cancelled["ok"] and cancelled["stock_reverse"].get("skipped") == "no-shipment"
        return await real_ship(order, items, user_id=user_id)

    monkeypatch.setattr(order_shipment, "ship_order", cancel_first)
    res = _run(approve_shipment_request(req_id, 100, "Boss", None))

    assert res["ok"] and res["invoice_id"] is None
    assert "не списаны" in res["demand_line"]
    assert _run(db.get_order(oid))["status"] == "cancelled"
    assert _active_outgoing(db) == 0
    assert _stock(db, pid) == 10, "товар по отменённому заказу не списан"
    assert _run(get_payments_for_order(oid)) == [], "автоплатёж по отменённому заказу не заводится"
    assert all(r["order_id"] != oid for r in _run(order_shipment.list_failed()))


def test_cancel_after_writeoff_returns_stock_once(env):
    from services import order_shipment
    from services.order_workflow import approve_shipment_request, cancel_order_full

    db, pid, request = env
    oid, req_id = request()
    assert _run(approve_shipment_request(req_id, 100, "Boss", None))["ok"]
    assert _stock(db, pid) == 8

    res = _run(cancel_order_full(oid, 100, "Boss", "Клиент передумал"))
    assert res["ok"] and res["stock_reverse"]["ok"] and res["stock_reverse"]["invoice_id"]
    assert _stock(db, pid) == 10
    assert _run(order_shipment.get_shipment(oid))["invoice_id"] is None

    again = _run(order_shipment.cancel_shipment(oid, user_id=100))
    assert again == {"ok": True, "skipped": "no-shipment"}
    assert _stock(db, pid) == 10


# ─── Дайджест: одобрен, но не списан ─────────────────────────────────────────


def test_list_failed_catches_approval_that_never_reached_warehouse(env):
    from datetime import datetime, timedelta

    from services import order_shipment

    db, _pid, request = env
    old = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    def approve_without_shipment(oid, req_id, approved_at):
        _exec(db, "UPDATE orders SET status = 'approved' WHERE id = ?", (oid,))
        _exec(db, "UPDATE shipment_requests SET status = 'approved', approved_at = ? WHERE id = ?",
              (approved_at, req_id))

    stuck, stuck_req = request()
    approve_without_shipment(stuck, stuck_req, old)
    fresh, fresh_req = request()
    approve_without_shipment(fresh, fresh_req, db.now_str())  # ещё в пути
    legacy, legacy_req = request()
    approve_without_shipment(legacy, legacy_req, old)
    _exec(db, "UPDATE orders SET ms_customerorder_id = ? WHERE id = ?", ("ms-uuid", legacy))
    failed_cancelled, _r = request()
    _run(order_shipment._remember_failure(failed_cancelled, "не хватило"))
    _exec(db, "UPDATE orders SET status = 'cancelled' WHERE id = ?", (failed_cancelled,))
    failed, _r = request()
    _exec(db, "UPDATE orders SET status = 'approved' WHERE id = ?", (failed,))
    _run(order_shipment._remember_failure(failed, "не хватило"))

    rows = {r["order_id"]: r for r in _run(order_shipment.list_failed())}
    assert set(rows) == {stuck, failed}
    assert "прервался" in rows[stuck]["error"] and rows[stuck]["agent_name"] == "Клиент"
    assert rows[failed]["error"] == "не хватило"


# ─── Резерв: только не списанное ─────────────────────────────────────────────


def test_catalog_reserve_counts_only_unwritten_off_orders(env):
    from services import order_shipment, warehouse
    from services.order_workflow import approve_shipment_request

    db, pid, request = env
    _oid, req_id = request()  # 2 шт., одобрение списывает накладной
    assert _run(approve_shipment_request(req_id, 100, "Boss", None))["ok"]
    unshipped, _r = request(lines=((3, 5.0),))
    _exec(db, "UPDATE orders SET status = 'approved' WHERE id = ?", (unshipped,))
    _run(order_shipment._remember_failure(unshipped, "не хватило"))

    row = next(p for p in _run(warehouse.get_catalog()) if p["product_id"] == pid)
    assert (row["quantity"], row["reserved"], row["available"]) == (8, 3, 5)
