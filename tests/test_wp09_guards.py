"""WP-09: mark_order_paid принимает оплату только по активному кредит-заказу;
delete_order не удаляет черновик, по которому есть платежи (orphan-защита)."""

import asyncio


def _credit_order(db, uid, total=100.0, status=None):
    db.set_role(uid, "m", "Mgr", "manager")
    oid = db.create_order(uid, "Mgr", "")
    db.add_order_item(oid, "P", "", 1, "шт", total)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type='credit', due_date='2099-01-01' WHERE id=?"),
            (oid,),
        )
        conn.commit()
    if status:
        db.update_order_status(oid, status)
    return oid


def test_mark_paid_rejects_non_active_status(isolated_db):
    db = isolated_db
    oid = _credit_order(db, 10)  # draft
    ok, pid = asyncio.run(db.mark_order_paid(oid, 10, "Mgr"))
    assert ok is False and pid is None  # draft — отказ
    # shipped → разрешено
    db.update_order_status(oid, "shipped")
    ok2, pid2 = asyncio.run(db.mark_order_paid(oid, 10, "Mgr", amount=50.0))
    assert ok2 is True and pid2 is not None


def test_delete_order_blocked_when_payments_exist(isolated_db):
    db = isolated_db
    oid = _credit_order(db, 11)  # draft
    db.add_payment(11, "@m", "Mgr", 50.0, "USD", "x", order_id=oid)  # легаси orphan-риск
    assert asyncio.run(db.delete_order(oid, 11)) is False  # не удаляем
    # черновик без платежей удаляется штатно
    oid2 = db.create_order(11, "Mgr", "")
    assert asyncio.run(db.delete_order(oid2, 11)) is True
