"""WP-05: взаимоучёт платежей и сдач + валюта сдачи.

- заказ, покрытый наполовину платежом + наполовину сдачей, закрывается;
- get_agent_current_debt вычитает подтверждённые аллокации сдач;
- сдача (базовая валюта) не распределяется на заказ в иной валюте.

Реальная БД (isolated_db); MS-sync платежей мокается (без сети).
"""

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _no_ms_sync(isolated_db, monkeypatch):
    monkeypatch.setattr(isolated_db, "_trigger_ms_paymentin_sync", lambda _: None)


def _shipped(db, manager_id, total, currency=None, agent="A1"):
    db.set_role(manager_id, "m", "Mgr", "manager")
    oid = db.create_order(manager_id, "Mgr", "")
    db.update_order_agent(oid, agent, agent)
    db.add_order_item(oid, "P", "", 1, "шт", total)
    db.update_order_status(oid, "shipped")
    if currency:
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(db.q("UPDATE orders SET currency=? WHERE id=?"), (currency, oid))
            conn.commit()
    return oid


def test_order_closed_by_payment_plus_deposit(isolated_db):
    db = isolated_db
    oid = _shipped(db, 100, 100.0)
    # Платёж 50 — заказ ещё не закрыт.
    pid = db.add_payment(100, "@m", "Mgr", 50.0, "USD", "x", order_id=oid)
    asyncio.run(db.confirm_payment(pid, 99, "Boss"))
    assert asyncio.run(db.get_order(oid)).get("paid_confirmed_at") is None
    # Сдача 50, распределённая на заказ, добивает покрытие → закрытие.
    dep = asyncio.run(db.create_cash_deposit(100, 50.0, allocations=[(oid, 50.0)]))
    res = asyncio.run(db.confirm_cash_deposit(dep["deposit_id"], 99, "Boss"))
    assert oid in res["closed_orders"]
    closed = asyncio.run(db.get_order(oid))
    assert closed["status"] == "paid" and closed["payment_confirmed"] == 1


def test_debt_subtracts_confirmed_deposit(isolated_db):
    db = isolated_db
    oid = _shipped(db, 100, 100.0, agent="AG")
    # Раньше долг был 100 (сдача не вычиталась). Теперь 100 − 30 = 70.
    dep = asyncio.run(db.create_cash_deposit(100, 30.0, allocations=[(oid, 30.0)]))
    asyncio.run(db.confirm_cash_deposit(dep["deposit_id"], 99, "Boss"))
    # Заказ не закрыт (30 < 100), всё ещё открытый долг.
    assert asyncio.run(db.get_order(oid)).get("paid_confirmed_at") is None
    assert asyncio.run(db.get_agent_current_debt("AG")) == 70.0


def test_deposit_skips_foreign_currency_order(isolated_db):
    db = isolated_db
    _shipped(db, 100, 100.0, currency="UZS")  # не базовая валюта
    base_oid = _shipped(db, 100, 50.0)  # базовая (NULL→USD)
    open_orders = asyncio.run(db.get_manager_open_orders_for_deposit(100))
    ids = {o["id"] for o in open_orders}
    assert base_oid in ids
    # UZS-заказ исключён — сдача в базовой валюте его не покрывает.
    assert len(ids) == 1
