"""T2.8 — возврат нельзя подтвердить, пока товар не принят.

Критерий готовности: `confirm_return` при `goods_received=0` не меняет
`returned_qty` и не создаёт отрицательную сдачу.

§2.12: `mark_return_goods_received` флаг ставил, но `confirm_return` его
НИКОГДА не проверял. Босс подтверждал возврат → `returned_qty` рос, заказ
уходил в `returned`, а при `refund_method='cash'` деньги выдавались из кассы
(отрицательная сдача) — за товар, который физически мог не приехать.
"""

import asyncio


def _shipped_order(db, uid=1, total=300.0, qty=3):
    db.set_role(uid, "mgr", "Manager", "manager")
    oid = db.create_order(uid, "Manager", "")
    iid = db.add_order_item(oid, "Товар", "href", qty, "шт", total / qty)
    db.update_order_agent(oid, "A-1", "Клиент")
    db.update_order_status(oid, "pending")
    db.update_order_status(oid, "approved")
    db.update_order_status(oid, "shipped")
    return oid, iid


def _pending_return(db, oid, iid, qty=1, amount=100.0, method="debt_reduction"):
    res = asyncio.run(
        db.create_return(
            oid, "partial", "брак", [(iid, qty, amount)], refund_method=method, created_by=1
        )
    )
    assert res.get("ok"), res
    return res["return_id"]


def _returned_qty(db, iid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT returned_qty FROM order_items WHERE id = ?"), (iid,))
        return float(cur.fetchone()[0] or 0)


def _negative_deposits(db):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM cash_deposits WHERE amount_cents < 0"))
        return cur.fetchone()[0]


# ─── Критерий готовности ────────────────────────────────────────────────────


def test_confirm_without_goods_received_is_refused(isolated_db):
    db = isolated_db
    oid, iid = _shipped_order(db)
    rid = _pending_return(db, oid, iid)

    res = asyncio.run(db.confirm_return(rid, 2, "Boss"))

    assert res["ok"] is False
    assert "товар принят" in res["error"]
    assert _returned_qty(db, iid) == 0, "returned_qty не должен измениться"
    assert asyncio.run(db.get_return(rid))["status"] == "pending"
    assert asyncio.run(db.get_order(oid))["status"] == "shipped"


def test_cash_refund_without_goods_received_creates_no_negative_deposit(isolated_db):
    """Самое дорогое последствие: деньги из кассы за непринятый товар."""
    db = isolated_db
    oid, iid = _shipped_order(db)
    rid = _pending_return(db, oid, iid, method="cash")

    res = asyncio.run(db.confirm_return(rid, 2, "Boss"))

    assert res["ok"] is False
    assert _negative_deposits(db) == 0, "выдача из кассы не должна состояться"


# ─── После приёмки всё работает ─────────────────────────────────────────────


def test_confirm_works_after_goods_received(isolated_db):
    db = isolated_db
    oid, iid = _shipped_order(db)
    rid = _pending_return(db, oid, iid)

    assert asyncio.run(db.mark_return_goods_received(rid, 3)).get("ok") is True
    res = asyncio.run(db.confirm_return(rid, 2, "Boss"))

    assert res["ok"] is True, res
    assert _returned_qty(db, iid) == 1
    assert asyncio.run(db.get_return(rid))["status"] == "confirmed"


def test_cash_refund_after_goods_received_pays_out(isolated_db):
    db = isolated_db
    oid, iid = _shipped_order(db)
    rid = _pending_return(db, oid, iid, method="cash")

    asyncio.run(db.mark_return_goods_received(rid, 3))
    res = asyncio.run(db.confirm_return(rid, 2, "Boss"))

    assert res["ok"] is True, res
    assert _negative_deposits(db) == 1


# ─── Сообщения различимы ────────────────────────────────────────────────────


def test_already_processed_has_its_own_message(isolated_db):
    """«Уже обработан» и «товар не принят» — разные причины отказа, иначе
    кладовщик не поймёт, что именно делать."""
    db = isolated_db
    oid, iid = _shipped_order(db)
    rid = _pending_return(db, oid, iid)

    asyncio.run(db.mark_return_goods_received(rid, 3))
    assert asyncio.run(db.confirm_return(rid, 2, "Boss"))["ok"] is True

    second = asyncio.run(db.confirm_return(rid, 2, "Boss"))
    assert second["ok"] is False
    assert "уже обработан" in second["error"].lower()
    assert "товар принят" not in second["error"]


def test_no_silent_bypass_exists(isolated_db):
    """Тихого обхода быть не должно: проверка — часть CAS-UPDATE, а не
    отдельный if, который легко случайно обойти другим путём."""
    import inspect

    from services.database import confirm_return

    src = inspect.getsource(confirm_return)
    assert "goods_received = 1" in src, "guard пропал из UPDATE"
    assert "status = 'pending' AND goods_received = 1" in src
