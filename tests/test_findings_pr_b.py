"""
PR B находок: энфорс кредит-лимита + override (#29), resubmit-diff (#30).

Реальная БД (isolated_db). approve_shipment_request зовём напрямую с фейк-ботом
и ms_demand.is_ready=False (без МойСклад).
"""

import asyncio

import services.roles as roles


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, *a, **k):
        self.sent.append(("m", a, k))

    async def send_document(self, *a, **k):
        self.sent.append(("d", a, k))


def _credit_order(db, total: float, agent: str = "A-1", status: str = "pending") -> int:
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, agent, "Client")
    db.add_order_item(oid, "Товар A", "", 1, "шт", total)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type='credit', due_date='2099-01-01' WHERE id=?"),
            (oid,),
        )
        conn.commit()
    db.update_order_status(oid, status)
    return oid


# ─── #29: энфорс кредит-лимита ───────────────────────────────────────────────


def test_approve_over_limit_requires_override(isolated_db, monkeypatch):
    import services.ms_demand as ms_demand
    from services.order_workflow import approve_shipment_request

    monkeypatch.setattr(ms_demand, "is_ready", lambda: False)
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(2, "b", "Boss", "boss")
    oid = _credit_order(db, 3000.0)  # лимит по умолчанию 2000 → превышение
    req_id = db.create_shipment_request(oid, 1, "M")

    res = asyncio.run(approve_shipment_request(req_id, 2, "Boss", _FakeBot(), override=False))
    assert res["ok"] is False
    assert res.get("needs_override") is True
    assert res["over"]["over_limit"] is True
    # Не одобрено — заявка и заказ остались pending.
    assert db.get_shipment_request(req_id)["status"] == "pending"
    assert asyncio.run(db.get_order(oid))["status"] == "pending"


def test_approve_with_override_sets_flag(isolated_db, monkeypatch):
    import services.ms_demand as ms_demand
    from services.order_workflow import approve_shipment_request

    monkeypatch.setattr(ms_demand, "is_ready", lambda: False)
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(2, "b", "Boss", "boss")
    oid = _credit_order(db, 3000.0)
    req_id = db.create_shipment_request(oid, 1, "M")

    res = asyncio.run(approve_shipment_request(req_id, 2, "Boss", _FakeBot(), override=True))
    assert res["ok"] is True
    o = asyncio.run(db.get_order(oid))
    assert o["status"] == "approved"
    assert o["credit_limit_override"] == 1
    assert o["credit_limit_override_by"] == 2


def test_approve_under_limit_proceeds(isolated_db, monkeypatch):
    import services.ms_demand as ms_demand
    from services.order_workflow import approve_shipment_request

    monkeypatch.setattr(ms_demand, "is_ready", lambda: False)
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(2, "b", "Boss", "boss")
    oid = _credit_order(db, 500.0)  # под лимитом 2000
    req_id = db.create_shipment_request(oid, 1, "M")

    res = asyncio.run(approve_shipment_request(req_id, 2, "Boss", _FakeBot(), override=False))
    assert res["ok"] is True
    assert asyncio.run(db.get_order(oid))["status"] == "approved"


def test_approve_no_false_override_from_double_count(isolated_db, monkeypatch):
    """Регресс: заказ-одиночка ПОД лимитом не должен требовать override. Раньше
    энфорс считал заказ дважды (он уже pending → в current_debt, плюс +total) →
    1500 при лимите 2000 давал projected=3000 и ложное «превышение»."""
    import services.ms_demand as ms_demand
    from services.order_workflow import approve_shipment_request

    monkeypatch.setattr(ms_demand, "is_ready", lambda: False)
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(2, "b", "Boss", "boss")
    oid = _credit_order(db, 1500.0)  # один заказ, под лимитом 2000
    req_id = db.create_shipment_request(oid, 1, "M")

    res = asyncio.run(approve_shipment_request(req_id, 2, "Boss", _FakeBot(), override=False))
    assert res.get("needs_override") is not True
    assert res["ok"] is True
    assert asyncio.run(db.get_order(oid))["status"] == "approved"


def test_approve_over_limit_from_existing_debt(isolated_db, monkeypatch):
    """Реальное превышение за счёт ДРУГОГО открытого заказа всё ещё требует
    override (фикс двойного счёта не должен ослабить энфорс)."""
    import services.ms_demand as ms_demand
    from services.order_workflow import approve_shipment_request

    monkeypatch.setattr(ms_demand, "is_ready", lambda: False)
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(2, "b", "Boss", "boss")
    _credit_order(db, 1500.0, agent="A-1", status="approved")  # уже в долге
    oid = _credit_order(db, 1000.0, agent="A-1")  # +1000 → 2500 > 2000
    req_id = db.create_shipment_request(oid, 1, "M")

    res = asyncio.run(approve_shipment_request(req_id, 2, "Boss", _FakeBot(), override=False))
    assert res.get("needs_override") is True
    assert res["over"]["over_limit"] is True


# ─── #30: resubmit-diff ──────────────────────────────────────────────────────


def test_reject_stores_snapshot_and_diff(isolated_db):
    from services.order_workflow import resubmit_diff_line

    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(1, "m", "M", "manager")
    db.set_role(2, "b", "Boss", "boss")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A-1", "Client")
    db.add_order_item(oid, "Товар A", "", 1, "шт", 100.0)
    db.update_order_status(oid, "pending")

    asyncio.run(db.reject_order_to_draft(oid, 2, "Boss", "доработай"))
    snap = asyncio.run(db.get_last_reject_snapshot(oid))
    assert snap is not None
    assert snap["total"] == 100.0
    assert len(snap["items"]) == 1

    # Менеджер добавил позицию и переотправляет.
    db.add_order_item(oid, "Товар B", "", 1, "шт", 150.0)
    items = asyncio.run(db.get_order_items(oid))
    line = asyncio.run(resubmit_diff_line(oid, items))
    assert "+1 поз" in line
    assert "+150" in line


def test_resubmit_diff_empty_without_reject(isolated_db):
    from services.order_workflow import resubmit_diff_line

    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    items = asyncio.run(db.get_order_items(oid))
    assert asyncio.run(resubmit_diff_line(oid, items)) == ""
