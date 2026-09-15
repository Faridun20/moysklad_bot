"""
«Как получены деньги» на НАСТОЯЩЕМ Postgres: гонки и сквозной поток.

На SQLite пишущая транзакция одна, и параллельные разбивка/сдача там всё равно
встают в очередь; на Postgres их сериализует только общий замок строк заказов
(`services.debts.lock_orders`). Приём с «барьером» — как в
`test_txn_races_postgres.py`: подменённый расчёт остатка ждёт второго
участника; при разных замках оба видят один остаток, и деньги заявляются дважды.

Запуск: `TEST_PG_URL=postgresql://… pytest tests/test_order_payments_postgres.py`.
"""

from __future__ import annotations

import asyncio

import pytest

from tests import test_txn_races_postgres as _races

PG_URL = _races.PG_URL
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

pg = _races.pg
pg_db = _races.pg_db
_reload_order_shipment_after = _races._reload_order_shipment_after
MGR, BOSS = _races.MGR, _races.BOSS


def _run(coro):
    return asyncio.run(coro)


def _actor(uid=MGR, role="manager"):
    from services import order_payments

    return order_payments.Actor(user_id=uid, name=f"U{uid}", role=role)


async def _record_or_error(oid, parts, uid=MGR):
    from services import order_payments

    try:
        return await order_payments.record_payment_parts(oid, _actor(uid), await _accounts(parts))
    except order_payments.PaymentError as e:
        return {"ok": False, "code": e.code}


async def _accounts(parts):
    """Карта/перечисление — с тестовой записью справочника (в своём цикле)."""
    from services import pay_accounts
    from tests.conftest import TEST_BANK, TEST_CARD

    out = []
    for p in parts:
        if p["method"] in ("card", "bank") and not p.get("account_id"):
            data = TEST_CARD if p["method"] == "card" else TEST_BANK
            acc = await pay_accounts.create_account(pay_accounts.Actor(0, "Tests", "boss"), data)
            p = {**p, "account_id": acc["account"]["id"]}
        out.append(p)
    return out


def test_parallel_breakdowns_of_one_paid_order_claim_once(pg, monkeypatch):
    db = pg
    oid = _races._order(db, total=12130.0, status="approved", payment_type="paid")
    barrier = _races._barrier_on_claimable(monkeypatch)

    async def both():
        return await asyncio.gather(
            _record_or_error(oid, [{"method": "cash", "currency": "USD", "amount": "12130"}]),
            _record_or_error(oid, [{"method": "card", "currency": "USD", "amount": "12130"}]),
        )

    results = _run(both())
    assert barrier.max_inside == 1, "обе разбивки считали остаток одновременно"
    assert sorted(bool(r.get("ok")) for r in results) == [False, True]
    assert _run(_races._claimed_cents(db, oid)) == 1_213_000


def test_breakdown_and_old_mark_paid_do_not_double_claim(pg, monkeypatch):
    db = pg
    oid = _races._order(db, total=100.0)  # в долг, отгружен
    barrier = _races._barrier_on_claimable(monkeypatch)

    async def both():
        return await asyncio.gather(
            _record_or_error(oid, [{"method": "bank", "currency": "USD", "amount": "100"}]),
            db.mark_order_paid(oid, MGR, "Manager", amount=None),
        )

    rec, (paid_ok, _pid) = _run(both())
    assert barrier.max_inside == 1
    assert _run(_races._claimed_cents(db, oid)) == 10_000
    assert bool(rec.get("ok")) != bool(paid_ok)


def test_parallel_handovers_never_link_one_cash_part_twice(pg):
    from services import adb_core, order_payments

    db = pg
    a = _races._order(db, total=3000.0)
    b = _races._order(db, total=2000.0)
    for oid, amount in ((a, "3000"), (b, "2000")):
        assert _run(order_payments.record_payment_parts(
            oid, _actor(), [{"method": "cash", "currency": "USD", "amount": amount}]))["ok"]

    async def both():
        return await asyncio.gather(db.create_cash_deposit(MGR, 4000.0), db.create_cash_deposit(MGR, 4000.0))

    first, second = _run(both())
    assert first["ok"] and second["ok"]
    linked = _run(adb_core.fetch(
        "SELECT part_id, COUNT(*) AS n, SUM(amount_cents) AS c FROM cash_deposit_parts GROUP BY part_id"))
    assert all(int(r["n"]) == 1 for r in linked), linked
    assert sum(int(r["c"]) for r in linked) == 500_000, "на руках было 5 000 — больше не распределить"
    assert first["unallocated_cents"] + second["unallocated_cents"] == 300_000
    # Суммы строк после деления сходятся с платежами под ними.
    rows = _run(adb_core.fetch(
        "SELECT pp.order_amount_cents, p.amount_cents FROM payment_parts pp JOIN payments p ON p.id = pp.payment_id"))
    assert all(int(r["order_amount_cents"]) == int(r["amount_cents"]) for r in rows)


def test_full_flow_paid_order_split_handover_confirm_on_postgres(pg):
    from services import order_payments
    from services.debts import calc_order_balance

    db = pg
    assert db.set_currency_rate("UZS", 1 / 12700, BOSS)[0]
    oid = _races._order(db, total=12130.0, status="approved", payment_type="paid")
    assert _run(db.mark_order_shipped(oid, BOSS, "Boss"))["code"] == "payment_required"
    from tests.conftest import pay_account_id

    rec = _run(order_payments.record_payment_parts(oid, _actor(), [
        {"method": "cash", "currency": "USD", "amount": "5000"},
        # 7 129.92 → 7 130 копейками пересчёта
        {"method": "card", "currency": "UZS", "amount": "90550000", "account_id": pay_account_id("card")},
    ]))
    assert rec["total_cents"] == 1_213_000
    assert _run(db.mark_order_shipped(oid, BOSS, "Boss"))["ok"]

    dep1 = _run(db.create_cash_deposit(MGR, 3000.0))
    dep2 = _run(db.create_cash_deposit(MGR, 2000.0))
    assert [p["amount_cents"] for p in dep1["parts"] + dep2["parts"]] == [300_000, 200_000]
    for dep in (dep1, dep2):
        assert _run(db.confirm_cash_deposit(dep["deposit_id"], BOSS, "Boss"))["ok"]
    card = next(p["payment_id"] for p in rec["parts"] if p["method"] == "card")
    assert _run(db.confirm_payment(card, BOSS, "Boss"))
    bal = _run(calc_order_balance(oid))
    assert (bal.remaining_cents, bal.confirmed_cents, bal.pending_cents) == (0, 1_213_000, 0)
    assert _run(db.get_order(oid))["paid_confirmed_at"]
