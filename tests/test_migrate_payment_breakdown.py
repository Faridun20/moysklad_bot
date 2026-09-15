"""Разовый `scripts/migrate_payment_breakdown` на картине прода (заказ #27).

Заказ «оплата сразу» 12 130 USD отгружен, автоплатёж одобрения pending на всю
сумму, две сдачи по 2 000 USD подтверждены самим менеджером и никуда не
распределены. Dry-run ничего не пишет; --apply раскладывает платёж на строки и
кладёт сдачи на наличные — долг уменьшается ровно на сданное.
"""

from __future__ import annotations

import asyncio

import pytest

MGR = 941599419


def _run(coro):
    return asyncio.run(coro)


def _rows(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return [dict(r) for r in cur.fetchall()]


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        conn.commit()


@pytest.fixture
def prod_like(isolated_db):
    db = isolated_db
    db.set_role(MGR, "owner", "Owner", "manager")
    oid = db.create_order(MGR, "Owner", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Техника", "", 1, "шт", 12130.0)
    _exec(db, "UPDATE orders SET payment_type = 'paid', currency = 'USD' WHERE id = ?", (oid,))
    db.update_order_status(oid, "shipped")
    pid = db.add_payment(MGR, "", "Owner", 12130.0, "USD", f"Оплата по заказу #{oid} (отгрузка одобрена)",
                         order_id=oid)
    deps = []
    for _ in range(2):
        dep = _run(db.create_cash_deposit(MGR, 2000.0))
        assert dep["ok"] and not dep["allocations"] and not dep["parts"]  # «Заказы: —»
        assert _run(db.confirm_cash_deposit(dep["deposit_id"], MGR, "Owner"))["ok"]
        deps.append(dep["deposit_id"])
    return db, oid, pid, deps


def test_report_names_the_problem_rows(prod_like):
    from scripts import migrate_payment_breakdown as m

    db, oid, pid, deps = prod_like
    rep = _run(m.report())
    assert [p["id"] for p in rep["unexplained_payments"]] == [pid]
    assert [d["id"] for d in rep["unallocated_deposits"]] == deps


def test_dry_run_changes_nothing(prod_like):
    from scripts import migrate_payment_breakdown as m

    db, oid, pid, deps = prod_like
    before = _rows(db, "SELECT id, status, amount_cents FROM payments ORDER BY id")
    rc = m.main(["--payment", str(pid), "--parts", "cash:12130",
                 "--allocate-deposit", str(deps[0]), "--allocate-deposit", str(deps[1])])
    assert rc == 0
    assert _rows(db, "SELECT id, status, amount_cents FROM payments ORDER BY id") == before
    assert _rows(db, "SELECT * FROM payment_parts") == []
    assert _rows(db, "SELECT * FROM cash_deposit_parts") == []


def test_apply_breaks_down_payment_and_allocates_deposits(prod_like):
    from scripts import migrate_payment_breakdown as m
    from services.debts import calc_order_balance

    db, oid, pid, deps = prod_like
    rc = m.main(["--apply", "--payment", str(pid), "--parts", "cash:12130",
                 "--allocate-deposit", str(deps[0]), "--allocate-deposit", str(deps[1])])
    assert rc == 0
    assert _rows(db, "SELECT status FROM payments WHERE id = ?", (pid,)) == [{"status": "rejected"}]
    links = _rows(db, "SELECT deposit_id, order_id, amount_cents FROM cash_deposit_parts ORDER BY deposit_id")
    assert links == [{"deposit_id": deps[0], "order_id": oid, "amount_cents": 200_000},
                     {"deposit_id": deps[1], "order_id": oid, "amount_cents": 200_000}]
    bal = _run(calc_order_balance(oid))
    assert (bal.confirmed_cents, bal.pending_cents, bal.remaining_cents) == (400_000, 813_000, 813_000)
    # Повтор не распределяет сдачу второй раз.
    assert m.main(["--apply", "--allocate-deposit", str(deps[0])]) == 1


def test_parts_argument_format():
    from scripts.migrate_payment_breakdown import parse_parts_arg

    assert parse_parts_arg("cash:5000,card:90551000:UZS:12700") == [
        {"method": "cash", "amount": "5000"},
        {"method": "card", "amount": "90551000", "currency": "UZS", "rate": "12700"},
    ]
    with pytest.raises(ValueError):
        parse_parts_arg("cash")
