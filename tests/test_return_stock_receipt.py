"""Подтверждённый возврат возвращает товар на склад.

Раньше остаток по возврату двигал МойСклад документом «Возврат покупателя».
После удаления интеграции `confirm_return` менял returned_qty, статус заказа и
деньги, а товар на складе не появлялся: клиенту вернули деньги, а продать
вернувшийся товар было нельзя — в остатках его нет.

Проверяем: остаток до/после, двойное подтверждение не приходует дважды,
частичный возврат приходует только возвращённое, приход в той же транзакции
(откат прихода откатывает и подтверждение), заказ без списания не приходуется.
"""

from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(isolated_db):
    """Склад: кабель 10 шт. и лампа 10 шт.; менеджер 200, босс 100."""
    from services import container_receipt, warehouse

    db = isolated_db
    db.set_role(100, "boss", "Boss", "boss")
    db.set_role(200, "mgr", "Manager", "manager")
    wid = _run(warehouse.default_warehouse_id())
    pids = {}
    for name in ("Кабель", "Лампа"):
        pid = _run(container_receipt.create_product(name))["product_id"]
        res = _run(warehouse.create_invoice(
            invoice_type="incoming", warehouse_id=wid,
            items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
        ))
        assert res["ok"], res
        pids[name] = pid
    return db, pids


def _shipped_order(db, pids, lines):
    """Заказ отгружен расходной накладной (как после одобрения). lines: [(name, qty)]."""
    from services import order_shipment

    oid = db.create_order(200, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    item_ids = {}
    for name, qty in lines:
        item_ids[name] = db.add_order_item(oid, name, "", qty, "шт", 5.0, product_id=pids[name])
    db.update_order_status(oid, "shipped")
    res = _run(order_shipment.ship_order(
        _run(db.get_order(oid)), _run(db.get_order_items(oid)), user_id=100
    ))
    assert res["ok"], res
    return oid, item_ids


def _return(db, oid, lines, received=True):
    """lines: [(order_item_id, qty)]."""
    res = _run(db.create_return(
        oid, "partial", "брак", [(iid, qty, 0) for iid, qty in lines],
        refund_method="debt_reduction", created_by=200,
    ))
    assert res["ok"], res
    if received:
        assert _run(db.mark_return_goods_received(res["return_id"], 300))["ok"]
    return res["return_id"]


def _stock(db, pid) -> float:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT SUM(quantity) FROM stock WHERE product_id = ?"), (pid,))
        return float(cur.fetchone()[0] or 0)


def _incoming_invoices(db) -> list:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            "SELECT id, comment, total_amount_cents FROM invoices "
            "WHERE type = 'incoming' AND comment LIKE 'Возврат%' ORDER BY id"
        )
        return [tuple(r) for r in cur.fetchall()]


def test_confirmed_return_puts_goods_back(env):
    db, pids = env
    oid, items = _shipped_order(db, pids, [("Кабель", 4)])
    assert _stock(db, pids["Кабель"]) == 6  # 10 − 4 отгружено

    rid = _return(db, oid, [(items["Кабель"], 4)])
    res = _run(db.confirm_return(rid, 100, "Boss"))

    assert res["ok"], res
    assert res["order_status"] == "returned"
    assert res["invoice_number"].startswith("IN-")
    assert _stock(db, pids["Кабель"]) == 10, "возвращённый товар обязан вернуться в остаток"
    invoices = _incoming_invoices(db)
    assert len(invoices) == 1
    # Комментарий — человеку, открывшему список накладных: откуда приход.
    assert invoices[0][1] == f"Возврат по заказу #{oid} (возврат #{rid})"
    # Цену продажи в приход не пишем — она читалась бы как закупочная.
    assert invoices[0][2] == 0


def test_double_confirm_does_not_receive_twice(env):
    db, pids = env
    oid, items = _shipped_order(db, pids, [("Кабель", 4)])
    rid = _return(db, oid, [(items["Кабель"], 2)])

    assert _run(db.confirm_return(rid, 100, "Boss"))["ok"]
    second = _run(db.confirm_return(rid, 100, "Boss"))

    assert second["ok"] is False
    assert _stock(db, pids["Кабель"]) == 8  # 6 + 2, а не 6 + 4
    assert len(_incoming_invoices(db)) == 1


def test_receipt_row_is_a_second_guard(env):
    """Статус вернули в pending руками — CAS пропустит, PK return_receipt нет."""
    db, pids = env
    oid, items = _shipped_order(db, pids, [("Кабель", 4)])
    rid = _return(db, oid, [(items["Кабель"], 2)])
    assert _run(db.confirm_return(rid, 100, "Boss"))["ok"]

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE returns SET status = 'pending' WHERE id = ?"), (rid,))
        cur.execute(db.q("UPDATE order_items SET returned_qty = 0 WHERE order_id = ?"), (oid,))
        conn.commit()

    again = _run(db.confirm_return(rid, 100, "Boss"))
    assert again["ok"] is False
    assert "оприходован" in again["error"]
    assert _stock(db, pids["Кабель"]) == 8


def test_partial_return_receives_only_returned_positions(env):
    db, pids = env
    oid, items = _shipped_order(db, pids, [("Кабель", 4), ("Лампа", 3)])
    assert (_stock(db, pids["Кабель"]), _stock(db, pids["Лампа"])) == (6, 7)

    rid = _return(db, oid, [(items["Лампа"], 1)])
    res = _run(db.confirm_return(rid, 100, "Boss"))

    assert res["ok"], res
    assert res["order_status"] == "partially_returned"
    assert _stock(db, pids["Кабель"]) == 6, "невозвращённая позиция не двигается"
    assert _stock(db, pids["Лампа"]) == 8


def test_failed_receipt_rolls_back_confirmation(env, monkeypatch):
    """Приход и подтверждение — одна транзакция: отказ склада не оставляет
    возврат подтверждённым (деньги без товара — ровно исходный баг)."""
    from services import warehouse

    db, pids = env
    oid, items = _shipped_order(db, pids, [("Кабель", 4)])
    rid = _return(db, oid, [(items["Кабель"], 4)])

    async def boom(txn, **kwargs):
        raise warehouse.InvoiceError("unknown_warehouse", "Склад #1 не найден")

    monkeypatch.setattr(warehouse, "create_invoice_in", boom)
    res = _run(db.confirm_return(rid, 100, "Boss"))

    assert res["ok"] is False
    assert "не оприходован" in res["error"]
    assert _run(db.get_return(rid))["status"] == "pending"
    assert _run(db.get_order(oid))["status"] == "shipped"
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT returned_qty FROM order_items WHERE id = ?"), (items["Кабель"],))
        assert float(cur.fetchone()[0]) == 0
        cur.execute("SELECT COUNT(*) FROM return_receipt")
        assert cur.fetchone()[0] == 0


def test_order_without_writeoff_is_not_received(env):
    """Отгрузка не провелась (накладной нет) — остаток не уменьшался, и приход
    по возврату прибавил бы товар, которого склад не терял."""
    db, pids = env
    oid = db.create_order(200, "Manager", "")
    iid = db.add_order_item(oid, "Кабель", "", 4, "шт", 5.0, product_id=pids["Кабель"])
    db.update_order_status(oid, "shipped")
    rid = _return(db, oid, [(iid, 4)])

    res = _run(db.confirm_return(rid, 100, "Boss"))

    assert res["ok"], res
    assert res["invoice_id"] is None
    assert "не списывался" in res["stock_skipped"]
    assert _stock(db, pids["Кабель"]) == 10
    assert _incoming_invoices(db) == []


def test_moysklad_era_order_is_received(env):
    """Заказ эпохи МойСклад списан там (снимок остатков это учёл) — возврат
    по нему товар на склад возвращает."""
    db, pids = env
    oid = db.create_order(200, "Manager", "")
    iid = db.add_order_item(oid, "Кабель", "", 4, "шт", 5.0, product_id=pids["Кабель"])
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET ms_demand_id = ? WHERE id = ?"), ("ms-demand-1", oid))
        conn.commit()
    rid = _return(db, oid, [(iid, 3)])

    res = _run(db.confirm_return(rid, 100, "Boss"))

    assert res["ok"], res
    assert _stock(db, pids["Кабель"]) == 13


def test_return_goes_back_at_the_cost_it_left_with(env):
    """Учёт себестоимости включён: возврат приходуется по той себестоимости,
    что зафиксировала отгрузка, а не «без себестоимости» — иначе следующая
    продажа возвращённого товара выпала бы из прибыли."""
    from services import container_receipt, warehouse

    db, _ = env
    db.set_setting("accounting_enabled", True)
    wid = _run(warehouse.default_warehouse_id())
    pid = _run(container_receipt.create_product("Фильтр"))["product_id"]
    res = _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": pid, "quantity": 10, "price_cents": 300}],
    ))
    assert res["ok"], res
    oid, items = _shipped_order(db, {"Фильтр": pid}, [("Фильтр", 4)])
    rid = _return(db, oid, [(items["Фильтр"], 2)])
    assert _run(db.confirm_return(rid, 100, "Boss"))["ok"]

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            "SELECT b.unit_price_cents, b.currency, b.quantity FROM cost_batches b "
            "JOIN invoices i ON i.id = b.invoice_id WHERE i.comment LIKE 'Возврат%'"
        )
        rows = [tuple(r) for r in cur.fetchall()]
    assert rows == [(300, "USD", 2.0)]
    assert _stock(db, pid) == 8
