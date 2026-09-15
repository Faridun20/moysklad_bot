"""
Журнал действий в WebApp (C1): `utils.audit_labels.translate_action`,
`services.database.get_audit_log_page` (пагинация/фильтры) и ручка
`/api/audit_log` (только admin/boss — как handlers/audit.py, но не только
admin), плюс `/api/orders/timeline` (C3: свой заказ — менеджеру, любой —
руководству).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

ADMIN, BOSS, MGR, MGR2, KEEPER, GUEST = 1, 2, 3, 4, 5, 6


def _run(coro):
    return asyncio.run(coro)


# ─── translate_action ────────────────────────────────────────────────────────


def test_translate_action_known_code():
    from utils.audit_labels import translate_action

    assert translate_action("payment_sent") == "Платёж внесён"
    assert translate_action("order_cancelled") == "Заказ отменён"


def test_translate_action_unknown_code_falls_back_to_raw():
    from utils.audit_labels import translate_action

    assert translate_action("some_future_action_xyz") == "some_future_action_xyz"
    assert translate_action(None) == "—"


# ─── get_audit_log_page ──────────────────────────────────────────────────────


@pytest.fixture
def db(isolated_db):
    isolated_db.set_role(ADMIN, "admin", "Админ Алла", "admin")
    isolated_db.set_role(BOSS, "boss", "Босс Борис", "boss")
    isolated_db.set_role(MGR, "mgr", "Менеджер Иван", "manager")
    isolated_db.set_role(GUEST, "guest", "Гость Глеб", "guest")
    return isolated_db


def test_get_audit_log_page_paginates_and_counts_total(db):
    from services.database import add_audit_log, get_audit_log_page

    for i in range(7):
        add_audit_log(MGR, "Менеджер Иван", "manager", "payment_sent", f"Платёж №{i}")

    page1, total = _run(get_audit_log_page(limit=3, offset=0))
    assert total == 7
    assert len(page1) == 3
    page2, total2 = _run(get_audit_log_page(limit=3, offset=3))
    assert total2 == 7
    assert len(page2) == 3
    # Новее сверху, страницы не пересекаются.
    ids1 = {r["id"] for r in page1}
    ids2 = {r["id"] for r in page2}
    assert not (ids1 & ids2)


def test_get_audit_log_page_filters_by_user(db):
    from services.database import add_audit_log, get_audit_log_page

    add_audit_log(MGR, "Менеджер Иван", "manager", "payment_sent", "A")
    add_audit_log(BOSS, "Босс Борис", "boss", "credit_override", "B")

    rows, total = _run(get_audit_log_page(limit=50, offset=0, user_id=MGR))
    assert total == 1
    assert rows[0]["user_id"] == MGR


def test_get_audit_log_page_filters_by_date_range(db):
    from services.database import get_audit_log_page

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO audit_log (user_id, full_name, role, action, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (MGR, "Менеджер Иван", "manager", "payment_sent", "старое", "2026-01-01 09:00:00"),
        )
        cur.execute(
            db.q(
                "INSERT INTO audit_log (user_id, full_name, role, action, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (MGR, "Менеджер Иван", "manager", "payment_sent", "в диапазоне", "2026-01-10 09:00:00"),
        )
        cur.execute(
            db.q(
                "INSERT INTO audit_log (user_id, full_name, role, action, details, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (MGR, "Менеджер Иван", "manager", "payment_sent", "новое", "2026-02-01 09:00:00"),
        )
        conn.commit()

    rows, total = _run(get_audit_log_page(
        limit=50, offset=0, date_from="2026-01-05", date_to="2026-01-15",
    ))
    assert total == 1
    assert rows[0]["details"] == "в диапазоне"


# ─── /api/audit_log ───────────────────────────────────────────────────────────


@pytest.fixture
def client(db, monkeypatch):
    import webapp.server as server

    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app)


def test_boss_can_open_audit_log(client, db):
    from services.database import add_audit_log

    add_audit_log(MGR, "Менеджер Иван", "manager", "payment_sent", "Платёж №1")
    r = client.post("/api/audit_log", json={"initData": str(BOSS), "limit": 20})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["entries"][0]["action_label"] == "Платёж внесён"
    assert body["entries"][0]["details"] == "Платёж №1"
    assert any(u["user_id"] == MGR for u in body["users"])


def test_admin_can_open_audit_log(client, db):
    r = client.post("/api/audit_log", json={"initData": str(ADMIN), "limit": 20})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("uid", [MGR, KEEPER, GUEST])
def test_non_boss_roles_cannot_open_audit_log(client, db, uid):
    if uid == KEEPER:
        db.set_role(KEEPER, "keeper", "Кладовщик Олег", "warehouse_keeper")
    r = client.post("/api/audit_log", json={"initData": str(uid), "limit": 20})
    assert r.status_code == 403


def test_audit_log_unknown_action_code_not_hidden(client, db):
    from services.database import add_audit_log

    add_audit_log(MGR, "Менеджер Иван", "manager", "totally_new_code_v2", "что-то новое")
    r = client.post("/api/audit_log", json={"initData": str(BOSS), "limit": 20})
    body = r.json()
    entry = next(e for e in body["entries"] if e["action"] == "totally_new_code_v2")
    assert entry["action_label"] == "totally_new_code_v2"  # fallback — как есть


# ─── /api/orders/timeline ────────────────────────────────────────────────────


def _order(db, uid=MGR):
    oid = db.create_order(uid, "Менеджер Иван", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    return oid


def test_manager_can_see_own_order_timeline(client, db):
    oid = _order(db, uid=MGR)
    r = client.post("/api/orders/timeline", json={"initData": str(MGR), "order_id": oid})
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert events and events[0]["action"] == "order_created"


def test_manager_cannot_see_other_managers_order_timeline(client, db):
    db.set_role(MGR2, "mgr2", "Менеджер Пётр", "manager")
    oid = _order(db, uid=MGR2)
    r = client.post("/api/orders/timeline", json={"initData": str(MGR), "order_id": oid})
    assert r.status_code == 403


def test_boss_and_admin_can_see_any_order_timeline(client, db):
    oid = _order(db, uid=MGR)
    for uid in (BOSS, ADMIN):
        r = client.post("/api/orders/timeline", json={"initData": str(uid), "order_id": oid})
        assert r.status_code == 200, r.text


def test_guest_cannot_see_order_timeline(client, db):
    oid = _order(db, uid=MGR)
    r = client.post("/api/orders/timeline", json={"initData": str(GUEST), "order_id": oid})
    assert r.status_code == 403


def test_order_timeline_missing_order_is_404(client, db):
    r = client.post("/api/orders/timeline", json={"initData": str(BOSS), "order_id": 999999})
    assert r.status_code == 404


def test_keeper_can_see_timeline_of_order_to_ship(client, db):
    """Кладовщик видит на карточке чужой approved/shipped заказ («к отгрузке» —
    `/api/orders` scope='to_ship') — «История» на ней обязана открываться, а не
    отвечать 403 (регрессия: обходчик `test_click_everything[keeper]` поймал её,
    когда кнопка была на каждой карточке, а ручка знала только про admin/boss/
    владельца)."""
    db.set_role(KEEPER, "keeper", "Кладовщик Олег", "warehouse_keeper")
    oid = _order(db, uid=MGR)
    db.update_order_status(oid, "approved")
    r = client.post("/api/orders/timeline", json={"initData": str(KEEPER), "order_id": oid})
    assert r.status_code == 200, r.text


def test_keeper_cannot_see_timeline_of_unrelated_draft_order(client, db):
    """Черновик чужого заказа кладовщику не «к отгрузке» — доступа нет."""
    db.set_role(KEEPER, "keeper", "Кладовщик Олег", "warehouse_keeper")
    oid = _order(db, uid=MGR)
    r = client.post("/api/orders/timeline", json={"initData": str(KEEPER), "order_id": oid})
    assert r.status_code == 403
