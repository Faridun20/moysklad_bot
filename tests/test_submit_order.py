"""T2.3 — сабмит заказа: атомарно и с типом оплаты.

Критерий готовности из плана:
  • заказ с payment_type='credit' и due_date сохраняет ОБА поля после сабмита;
  • двойной сабмит даёт одну заявку и понятную ошибку.

Критичный баг (§5.2.3): бот звал create_shipment_request, вообще не трогая
payment_type. Схемный дефолт — 'paid', поэтому заказ в рассрочку, отправленный
из бота, попадал в учёт как оплаченный: он не появлялся ни в «Долгах», ни в
кредит-чеке, ни в утреннем напоминании.
"""

import asyncio
from datetime import date, timedelta

import pytest

from services.order_workflow import submit_order, validate_payment_terms


def _draft(db, uid=1, *, with_items=True, with_agent=True):
    db.set_role(uid, "mgr", "Manager", "manager")
    oid = db.create_order(uid, "Manager", "")
    if with_items:
        db.add_order_item(oid, "Товар", "href", 2, "шт", 500.0)
    if with_agent:
        db.update_order_agent(oid, "A-1", "Клиент")
    return oid


def _order(db, oid):
    return asyncio.run(db.get_order(oid))


def _requests(db, oid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT id, status FROM shipment_requests WHERE order_id = ? ORDER BY id"),
            (oid,),
        )
        return [dict(r) for r in cur.fetchall()]


TOMORROW = (date.today() + timedelta(days=1)).isoformat()


# ─── Критерий 1: credit + due_date сохраняются ──────────────────────────────


def test_credit_submit_persists_payment_type_and_due_date(isolated_db):
    db = isolated_db
    oid = _draft(db)

    res = asyncio.run(
        submit_order(oid, 1, "Manager", payment_type="credit", due_date=TOMORROW)
    )
    assert res["ok"] is True, res

    o = _order(db, oid)
    assert o["payment_type"] == "credit"
    assert o["due_date"] == TOMORROW
    assert o["status"] == "pending"


def test_bot_path_no_longer_loses_credit(isolated_db):
    """Бот передаёт тип оплаты С ЗАКАЗА. Раньше он не передавал ничего, и
    рассрочка превращалась в 'paid'."""
    db = isolated_db
    oid = _draft(db)
    asyncio.run(db.set_order_payment(oid, "credit", TOMORROW))

    o = _order(db, oid)
    res = asyncio.run(
        submit_order(
            oid, 1, "Manager",
            payment_type=o.get("payment_type"),
            due_date=o.get("due_date"),
        )
    )
    assert res["ok"] is True, res
    assert _order(db, oid)["payment_type"] == "credit"


def test_paid_submit_clears_stale_due_date(isolated_db):
    """Заказ перевели из credit обратно в paid — срок долга не должен остаться."""
    db = isolated_db
    oid = _draft(db)
    asyncio.run(db.set_order_payment(oid, "credit", TOMORROW))

    res = asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))
    assert res["ok"] is True
    o = _order(db, oid)
    assert o["payment_type"] == "paid"
    assert o["due_date"] is None


# ─── Критерий 2: двойной сабмит ─────────────────────────────────────────────


def test_double_submit_creates_one_request(isolated_db):
    db = isolated_db
    oid = _draft(db)

    first = asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))
    second = asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))

    assert first["ok"] is True
    assert second["ok"] is False
    assert "уже отправлен" in second["error"]
    assert second.get("status") == "pending"

    reqs = _requests(db, oid)
    assert len(reqs) == 1, f"должна быть ровно одна заявка, а есть {reqs}"


def test_concurrent_submits_produce_one_request(isolated_db):
    """Две «одновременные» отправки одного заказа. CAS на draft пропускает
    только одну; уникальный индекс из T1.8 — страховка на уровне БД."""
    db = isolated_db
    oid = _draft(db)

    async def _both():
        return await asyncio.gather(
            submit_order(oid, 1, "Manager", payment_type="paid"),
            submit_order(oid, 1, "Manager", payment_type="paid"),
            return_exceptions=True,
        )

    results = asyncio.run(_both())
    oks = [r for r in results if isinstance(r, dict) and r.get("ok")]
    assert len(oks) == 1, results
    assert len(_requests(db, oid)) == 1


# ─── submitted_at (§2.11) ───────────────────────────────────────────────────


def test_submitted_at_is_written(isolated_db):
    """Раньше колонка только читалась. Из-за пустого submitted_at
    get_stale_pending_orders судил по created_at, и переотправленный старый
    заказ немедленно объявлялся «зависшей заявкой»."""
    db = isolated_db
    oid = _draft(db)
    asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))
    assert _order(db, oid)["submitted_at"]


def test_resubmitted_order_is_not_stale(isolated_db):
    db = isolated_db
    oid = _draft(db)
    # Заказ создан неделю назад, отправлен — только что.
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET created_at = ? WHERE id = ?"),
            ("2020-01-01 00:00:00", oid),
        )
        conn.commit()
    asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))

    stale = asyncio.run(db.get_stale_pending_orders(hours=48))
    assert all(o["id"] != oid for o in stale), "свежая заявка попала в зависшие"


def test_submitted_at_shares_frame_with_created_at(isolated_db):
    """submitted_at пишется в том же кадре времени, что created_at.

    get_stale_pending_orders сравнивает COALESCE(submitted_at, created_at)
    с порогом, посчитанным через datetime.now() (локальное). UTC здесь
    разъехался бы со смещением TZ и досрочно помечал заявки зависшими.
    """
    db = isolated_db
    oid = _draft(db)
    asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))

    o = _order(db, oid)
    # Оба значения — одного формата и близки во времени (тот же прогон теста).
    assert len(o["submitted_at"]) == len("YYYY-MM-DD HH:MM:SS")
    assert o["submitted_at"][:10] == o["created_at"][:10]


# ─── Guard'ы ────────────────────────────────────────────────────────────────


def test_submit_rejects_empty_order(isolated_db):
    db = isolated_db
    oid = _draft(db, with_items=False)
    res = asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))
    assert res["ok"] is False
    assert "товары" in res["error"].lower()
    assert _order(db, oid)["status"] == "draft"


def test_submit_rejects_order_without_agent(isolated_db):
    db = isolated_db
    oid = _draft(db, with_agent=False)
    res = asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))
    assert res["ok"] is False
    assert "клиент" in res["error"].lower()
    assert _order(db, oid)["status"] == "draft"


def test_submit_rejects_frozen_order(isolated_db):
    db = isolated_db
    oid = _draft(db)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET frozen = 1 WHERE id = ?"), (oid,))
        conn.commit()
    res = asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))
    assert res["ok"] is False
    assert "заморожен" in res["error"].lower()


def test_submit_unknown_order(isolated_db):
    res = asyncio.run(submit_order(999_999, 1, "Manager", payment_type="paid"))
    assert res["ok"] is False
    assert "не найден" in res["error"].lower()


def test_failed_submit_leaves_no_request(isolated_db):
    """Откат должен быть полным: ни заявки, ни смены статуса."""
    db = isolated_db
    oid = _draft(db, with_items=False)
    asyncio.run(submit_order(oid, 1, "Manager", payment_type="paid"))
    assert _requests(db, oid) == []
    assert _order(db, oid)["status"] == "draft"


# ─── Валидация условий оплаты ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "ptype,due,expect_err",
    [
        ("credit", None, "дату возврата"),
        ("credit", "", "дату возврата"),
        ("credit", "не-дата", "формат"),
        ("credit", "2000-01-01", "в прошлом"),
        ("нечто", None, "тип оплаты"),
    ],
)
def test_payment_terms_validation_errors(ptype, due, expect_err):
    _p, _d, err = validate_payment_terms(ptype, due)
    assert err and expect_err in err.lower()


def test_payment_terms_defaults_to_paid():
    ptype, due, err = validate_payment_terms(None, None)
    assert (ptype, due, err) == ("paid", None, None)


def test_submit_rejects_credit_without_due_date(isolated_db):
    db = isolated_db
    oid = _draft(db)
    res = asyncio.run(submit_order(oid, 1, "Manager", payment_type="credit"))
    assert res["ok"] is False
    assert "дату возврата" in res["error"].lower()
    assert _order(db, oid)["status"] == "draft"
