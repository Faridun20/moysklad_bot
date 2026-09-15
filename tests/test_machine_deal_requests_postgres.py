"""Гонки заявок на сделки по технике на НАСТОЯЩЕМ Postgres (TEST_PG_URL).

SQLite здесь не свидетель: пишущая транзакция у него одна (`BEGIN IMMEDIATE`),
и две «одновременные» заявки просто встают в очередь. На Postgres от второй
сделки на ту же машину держат `FOR UPDATE` строки машины/заявки и частичный
UNIQUE одной живой заявки — их и проверяем параллельными корутинами на пуле.
"""

from __future__ import annotations

import asyncio

import pytest

from tests import test_money_postgres as _pgm

PG_URL = _pgm.PG_URL
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

pg_db = _pgm.pg_db

MGR, BOSS, MGR2 = 1, 2, 3


@pytest.fixture
def quiet(monkeypatch):
    import services.notifier as notifier

    async def fake(chat_id, text, *, parse_mode="HTML", reply_markup=None):
        return True

    monkeypatch.setattr(notifier, "tg_send_message", fake)


def _machine(tag: str) -> int:
    from services import machines

    res = asyncio.run(machines.create_machine(vin=f"RACE{tag}", name="CAT 320", created_by=BOSS,
                                              price_cents=3_000_000, status="in_stock"))
    assert res["ok"], res
    return res["machine_id"]


def _count(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(sql, params)
        return cur.fetchone()["n"]


def test_two_managers_submit_on_one_machine_at_once(pg_db, quiet):
    from services import machine_deal_requests as mdr

    pg_db.set_role(MGR2, "mgr2", "Manager Two", "manager")
    mid = _machine("A")

    async def both():
        async def one(uid, buyer):
            return await mdr.submit(mid, kind="sale", actor_id=uid, actor_name=buyer,
                                    actor_role="manager", price_cents=2_900_000, buyer_name=buyer)

        return await asyncio.gather(*(one(MGR if i % 2 else MGR2, f"Покупатель {i}")
                                      for i in range(8)))

    results = asyncio.run(both())
    ok = [r for r in results if r["ok"]]
    assert len(ok) == 1, results
    assert all(r.get("current") == "pending_request" for r in results if not r["ok"]), results
    assert _count(pg_db, "SELECT COUNT(*) AS n FROM machine_deal_requests") == 1


def test_parallel_approve_and_reject_decide_once(pg_db, quiet):
    from services import machine_deal_requests as mdr

    mid = _machine("B")
    rid = asyncio.run(mdr.submit(mid, kind="credit", actor_id=MGR, actor_name="M", actor_role="manager",
                                 price_cents=2_400_000, buyer_name="Иванов", buyer_passport="AA1",
                                 down_payment_cents=0, months=6))["request_id"]
    boss = {"actor_id": BOSS, "actor_name": "Boss", "actor_role": "boss"}

    async def race():
        return await asyncio.gather(
            mdr.approve(rid, **boss), mdr.approve(rid, **boss),
            mdr.reject(rid, reason="нет", **boss), mdr.approve(rid, **boss),
        )

    results = asyncio.run(race())
    assert sum(1 for r in results if r["ok"]) == 1, results
    deals = _count(pg_db, "SELECT COUNT(*) AS n FROM machine_deals")
    status = _count(pg_db, "SELECT COUNT(*) AS n FROM machines WHERE id = %s AND status = 'on_credit'", (mid,))
    decided = [r for r in results if r["ok"]][0]["request_status"]
    if decided == "approved":
        assert deals == 1 and status == 1
        assert _count(pg_db, "SELECT COUNT(*) AS n FROM machine_deal_payments") == 6
    else:
        assert deals == 0 and status == 0


def test_boss_direct_sale_races_manager_request(pg_db, quiet):
    """Руководитель продаёт напрямую, пока менеджер отправляет заявку на ту же
    машину: либо сделка руководителя (и заявка — отказ), либо заявка (и прямая
    продажа — отказ). Двух итогов не бывает."""
    from services import machine_deal_requests as mdr

    mid = _machine("C")

    async def race():
        return await asyncio.gather(
            mdr.submit(mid, kind="sale", actor_id=BOSS, actor_name="Boss", actor_role="boss",
                       price_cents=3_000_000, buyer_name="Прямой"),
            mdr.submit(mid, kind="sale", actor_id=MGR, actor_name="M", actor_role="manager",
                       price_cents=2_800_000, buyer_name="Через заявку"),
        )

    boss_res, mgr_res = asyncio.run(race())
    assert boss_res["ok"] != mgr_res["ok"], (boss_res, mgr_res)
    assert _count(pg_db, "SELECT COUNT(*) AS n FROM machine_deal_requests "
                         "WHERE status IN ('pending', 'approved')") == 1
