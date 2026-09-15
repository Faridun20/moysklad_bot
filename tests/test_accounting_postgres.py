"""
Бухгалтерия на НАСТОЯЩЕМ Postgres.

Остальные тесты бухгалтерии идут на SQLite, а три вещи там не проверить:
`FOR UPDATE` на заказе/сделке, ожидание второй транзакции на UNIQUE-индексе
ключа идемпотентности (параллельный двойной тап) и типы выражений в
`EFFECTIVE_SQL`/агрегатах остатков (BIGINT/NUMERIC из asyncpg).

Запуск как у `test_money_postgres.py`: `TEST_PG_URL=postgresql://… pytest
tests/test_accounting_postgres.py`. Без переменной — пропуск; каждая проверка
получает свою базу.
"""

from __future__ import annotations

import asyncio

import pytest

from tests import test_money_postgres as _pg

PG_URL = _pg.PG_URL
# Фикстура своей базы на каждый тест — та же, что у денежных PG-тестов.
pg_db = _pg.pg_db

pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

MGR, BOSS = 1, 2


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    from services import accounting as acc

    db.set_currency_rate("UZS", 1 / 12650.5, BOSS)
    db.set_currency_rate_daily("UZS", acc.today_str(), 1 / 12650.5, "cbu")
    boss = acc.Actor(BOSS, "Boss", "boss")
    _run(acc.set_enabled(boss, True))
    cash = _run(acc.save_account(boss, {"name": "Касса USD", "kind": "cash", "currency": "USD",
                                        "opening": "100"}))["id"]
    card = _run(acc.save_account(boss, {"name": "Humo", "kind": "card", "currency": "UZS"}))["id"]
    return acc, boss, acc.Actor(MGR, "Manager", "manager"), cash, card


def _order(db, total=1000.0):
    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    _run(db.set_order_payment(oid, "credit", "2030-01-15"))
    db.update_order_status(oid, "approved")
    return oid


def test_receipt_two_currencies_balances_and_reversal_on_postgres(pg_db):
    from services.debts import calc_order_balance

    db = pg_db
    assert db.USE_POSTGRES
    acc, boss, mgr, cash, card = _setup(db)
    oid = _order(db)

    res = _run(acc.record_receipt(mgr, {
        "order_id": oid, "idempotency_key": "pg-1", "rates": {"UZS": "12700"},
        "lines": [{"account_id": cash, "amount": "400"}, {"account_id": card, "amount": "7620000"}],
    }))
    assert res["credited_cents"] == 100000 and res["payment_status"] == "pending"
    assert _run(db.confirm_all_pending_payments_for_order(oid, BOSS, "Boss")) == 1
    assert _run(calc_order_balance(oid)).remaining_cents == 0

    bal = {a["name"]: a["balance_cents"] for a in _run(acc.balances())["accounts"]}
    assert bal == {"Касса USD": 50000, "Humo": 762_000_000}

    _run(acc.void_doc(boss, {"doc_id": res["doc_id"], "reason": "ошибка"}))
    assert _run(calc_order_balance(oid)).remaining_cents == 100000
    assert _run(db.get_order(oid))["paid_confirmed_at"] is None
    bal = {a["name"]: a["balance_cents"] for a in _run(acc.balances())["accounts"]}
    assert bal == {"Касса USD": 10000, "Humo": 0}


def test_parallel_double_tap_writes_one_document(pg_db):
    db = pg_db
    acc, boss, mgr, cash, card = _setup(db)
    oid = _order(db)
    body = {"order_id": oid, "idempotency_key": "tap", "lines": [{"account_id": cash, "amount": "10"}]}

    async def both():
        return await asyncio.gather(
            acc.record_receipt(mgr, dict(body)), acc.record_receipt(mgr, dict(body))
        )

    first, second = _run(both())
    assert first["doc_id"] == second["doc_id"]
    assert sorted([first["repeated"], second["repeated"]]) == [False, True]
    assert len(_run(db.get_payments_for_order(oid))) == 1


def test_expense_exchange_close_day_and_journal_on_postgres(pg_db):
    db = pg_db
    acc, boss, mgr, cash, card = _setup(db)
    _run(acc.record_expense(mgr, {"account_id": cash, "amount": "15.50", "note": "такси",
                                  "idempotency_key": "e"}))
    x = _run(acc.record_transfer(boss, {"from_account_id": cash, "to_account_id": card, "amount": "50",
                                        "amount_in": "637500", "idempotency_key": "x"}))
    assert x["kind"] == "exchange"
    res = _run(acc.close_day(boss, {"account_id": cash, "counted": "34", "note": "мелочь",
                                    "idempotency_key": "c"}))
    assert (res["expected_cents"], res["diff_cents"]) == (3450, -50)
    state = _run(acc.balances())
    assert {a["name"]: a["balance_cents"] for a in state["accounts"]} == {"Касса USD": 3400, "Humo": 63_750_000}
    assert state["total_base_cents"] > 0
    docs = _run(acc.journal(boss, {"since": acc.today_str(), "kind": "transfer"}))["docs"]
    assert [d["kind"] for d in docs] == ["exchange"]
    assert _run(acc.journal(mgr, {}))["docs"][0]["note"] == "такси"


def test_machine_receipt_on_postgres(pg_db):
    from services import machines

    db = pg_db
    acc, boss, mgr, cash, card = _setup(db)
    m = _run(machines.create_machine(vin="PG-1", name="JCB", created_by=BOSS, price_cents=1_000_000))
    deal = _run(machines.create_deal(m["machine_id"], kind="credit", price_cents=1_000_000,
                                     buyer_name="Иванов", created_by=BOSS, down_payment_cents=200_000,
                                     months=2))
    res = _run(acc.record_receipt(boss, {"deal_id": deal["deal_id"], "idempotency_key": "m",
                                         "lines": [{"account_id": cash, "amount": "8000"}]}))
    assert res["credited_cents"] == 800_000 and res["deal_closed"] is True
