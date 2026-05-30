"""
Сдача наличных (cash deposit): покрытие заказа считается в КОПЕЙКАХ, без
float-эпсилона +0.01.

Раньше confirm_cash_deposit закрывал заказ при
`confirmed + 0.01 >= total` — т.е. заказ, недоплаченный на 1 копейку,
ошибочно становился «оплачен». Теперь сравнение точное в копейках.
"""

import asyncio


def _shipped_order(db, mgr, total):
    db.set_role(mgr, "m", "Mgr", "manager")
    oid = db.create_order(mgr, "Mgr", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    db.update_order_status(oid, "shipped")
    return oid


def test_cash_deposit_closes_on_exact_cents_coverage(isolated_db):
    db = isolated_db
    mgr = 5001
    oid = _shipped_order(db, mgr, 100.0)  # 10000 коп.
    res = asyncio.run(db.create_cash_deposit(mgr, 100.0, allocations=[(oid, 100.0)]))
    assert res["ok"], res
    cres = asyncio.run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))
    assert oid in cres["closed_orders"]
    assert asyncio.run(db.get_order(oid))["payment_confirmed"] == 1


def test_cash_deposit_not_closed_when_one_cent_short(isolated_db):
    db = isolated_db
    mgr = 5002
    oid = _shipped_order(db, mgr, 100.0)  # 10000 коп.
    res = asyncio.run(db.create_cash_deposit(mgr, 99.99, allocations=[(oid, 99.99)]))  # 9999 коп.
    assert res["ok"], res
    cres = asyncio.run(db.confirm_cash_deposit(res["deposit_id"], 1, "Boss"))
    # 1 копейка не покрыта → заказ НЕ закрываем (раньше +0.01 эпсилон закрывал).
    assert oid not in cres["closed_orders"]
    assert asyncio.run(db.get_order(oid))["payment_confirmed"] == 0


def test_open_orders_for_deposit_remaining_in_cents(isolated_db):
    db = isolated_db
    mgr = 5003
    oid = _shipped_order(db, mgr, 100.0)
    # частично покрыли pending-сдачей на 30.00
    asyncio.run(db.create_cash_deposit(mgr, 30.0, allocations=[(oid, 30.0)]))
    rows = {o["id"]: o for o in asyncio.run(db.get_manager_open_orders_for_deposit(mgr))}
    assert oid in rows
    assert rows[oid]["remaining"] == 70.0  # 10000 − 3000 = 7000 коп.
