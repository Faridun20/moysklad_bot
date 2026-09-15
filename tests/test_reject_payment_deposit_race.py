"""
Гонка «отклонить платёж ↔ сдать наличные» (аудит денег, сентябрь 2026).

Раньше `reject_payment` проверял «наличные уже в сдаче» ВНЕ транзакции и без
замка заказа: сдача, созданная между проверкой и UPDATE, уносила деньги
отклонённого платежа, а подтверждение сдачи молча пропускало строку (rc=0) и
подтверждало сдачу с деньгами, которых по учёту нет. Теперь проверка и CAS —
одной транзакцией под `lock_orders`, а `confirm_deposit_parts_locked`
отказывает, если платёж строки уже не ожидающий.
"""

from __future__ import annotations

import asyncio

import pytest

from tests import test_txn_races_postgres as _races

PG_URL = _races.PG_URL
pg = _races.pg
pg_db = _races.pg_db
_reload_order_shipment_after = _races._reload_order_shipment_after
MGR, BOSS = _races.MGR, _races.BOSS


def _run(coro):
    return asyncio.run(coro)


def _cash_record(oid, amount="100"):
    from services import order_payments

    actor = order_payments.Actor(user_id=MGR, name="Manager", role="manager")
    return _run(order_payments.record_payment_parts(
        oid, actor, [{"method": "cash", "currency": "USD", "amount": amount}]
    ))


async def _rejected_in_live_deposit() -> int:
    from services import adb_core

    return int(await adb_core.fetchval(
        "SELECT COUNT(*) FROM cash_deposit_parts cdp "
        "JOIN payment_parts pp ON pp.id = cdp.part_id JOIN payments p ON p.id = pp.payment_id "
        "JOIN cash_deposits d ON d.id = cdp.deposit_id "
        "WHERE p.status = 'rejected' AND d.status IN ('pending', 'confirmed')"
    ) or 0)


def test_confirm_deposit_refuses_part_whose_payment_is_no_longer_pending(isolated_db):
    import services.roles as roles

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(MGR, "mgr", "Manager", "manager")
    db.set_role(BOSS, "boss", "Boss", "boss")
    oid = _races._order(db, total=100.0)
    rec = _cash_record(oid)
    dep = _run(db.create_cash_deposit(MGR, 100.0))
    assert dep["ok"] and dep["parts"]
    # Состояние, которое оставляла гонка: платёж строки отклонён мимо сдачи.
    _races._exec(db, "UPDATE payments SET status = 'rejected' WHERE id = ?", (rec["payment_id"],))

    res = _run(db.confirm_cash_deposit(dep["deposit_id"], BOSS, "Boss"))
    assert res["ok"] is False and res["code"] == "deposit_part_not_pending", res
    assert "отклоните сдачу" in res["error"]
    # Откат целиком: сдача ждёт, заказ не закрыт.
    assert _run(db.get_cash_deposit(dep["deposit_id"]))["status"] == "pending"
    assert not _run(db.get_order(oid)).get("paid_confirmed_at")


def test_reject_payment_in_live_deposit_is_refused(isolated_db):
    import services.roles as roles

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(MGR, "mgr", "Manager", "manager")
    db.set_role(BOSS, "boss", "Boss", "boss")
    oid = _races._order(db, total=100.0)
    rec = _cash_record(oid)
    assert _run(db.create_cash_deposit(MGR, 100.0))["ok"]
    assert _run(db.reject_payment(rec["payment_id"], BOSS, "Boss")) is False
    assert _run(db.get_payment(rec["payment_id"]))["status"] == "pending"


@pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")
def test_reject_payment_and_cash_deposit_race_never_leaves_rejected_cash_in_deposit(pg, monkeypatch):
    """Отклонение держит замок заказа, пока сдача пытается забрать те же наличные:
    сдача ждёт коммита и перечитывает строки под замком — отклонённое не берёт."""
    from services import order_payments

    db = pg
    oid = _races._order(db, total=100.0)
    rec = _cash_record(oid)
    pid = rec["payment_id"]
    real_check = order_payments.payment_in_active_deposit
    in_check = asyncio.Event()

    async def slow_check(payment_id, conn=None):
        res = await real_check(payment_id, conn)
        in_check.set()
        # Окно, в которое при старом коде сдача успевала закоммитить свои строки.
        await asyncio.sleep(0.5)
        return res

    monkeypatch.setattr(order_payments, "payment_in_active_deposit", slow_check)

    async def deposit_after_check():
        await in_check.wait()
        return await db.create_cash_deposit(MGR, 100.0)

    async def both():
        return await asyncio.gather(db.reject_payment(pid, BOSS, "Boss"), deposit_after_check())

    rejected, dep = _run(both())
    monkeypatch.setattr(order_payments, "payment_in_active_deposit", real_check)

    assert rejected is True
    assert dep["ok"] and dep["parts"] == [], dep  # отклонённые наличные сдача не взяла
    assert _run(_rejected_in_live_deposit()) == 0
    assert _run(db.get_payment(pid))["status"] == "rejected"
    # Подтверждение сдачи ничего чужого не подтверждает и заказ не закрывает.
    res = _run(db.confirm_cash_deposit(dep["deposit_id"], BOSS, "Boss"))
    assert res["ok"] and res["closed_orders"] == []
    assert _run(db.get_payment(pid))["status"] == "rejected"
    assert not _run(db.get_order(oid)).get("paid_confirmed_at")
