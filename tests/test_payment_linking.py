"""
Тесты link_payment_to_order + get_unlinked_payments + endpoints.

Сценарии:
  * валидация: missing payment/order, payment уже привязан, deleted order
  * happy path для pending-платежа — только UPDATE order_id, без триггеров
  * happy path для уже-confirmed платежа — UPDATE + audit + maybe_close +
    MS sync trigger (mocked)
  * race: два параллельных link'а к разным order'ам — второй проигрывает
  * get_unlinked_payments фильтрует по status='confirmed' AND order_id IS NULL
  * authz: manager → 403 на /api/payments/link
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient


# ─── Юнит-уровень (на isolated_db) ─────────────────────────────────────────────


def _setup_order(db, manager_id: int = 100, total: float = 100.0):
    """approved-заказ с одной позицией на total."""
    db.set_role(manager_id, "mgr", "Manager", "manager")
    oid = db.create_order(manager_id, "Manager", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    db.update_order_status(oid, "approved")
    return oid


def test_link_rejects_missing_args(isolated_db):
    db = isolated_db
    res = asyncio.run(db.link_payment_to_order(0, 1, linked_by=99))
    assert res["ok"] is False
    assert "обязательны" in res["error"]


def test_link_rejects_missing_payment(isolated_db):
    db = isolated_db
    oid = _setup_order(db)
    res = asyncio.run(db.link_payment_to_order(999, oid, linked_by=99))
    assert res["ok"] is False
    assert "Платёж не найден" in res["error"]


def test_link_rejects_missing_order(isolated_db):
    db = isolated_db
    db.set_role(100, "m", "M", "manager")
    pid = db.add_payment(100, "@m", "M", 50.0, "USD", "x")  # без order_id
    res = asyncio.run(db.link_payment_to_order(pid, 999, linked_by=99))
    assert res["ok"] is False
    assert "Заказ не найден" in res["error"]


def test_link_rejects_already_linked_payment(isolated_db):
    """Платёж уже имеет order_id → отказ (нет «переноса»)."""
    db = isolated_db
    oid1 = _setup_order(db)
    oid2 = _setup_order(db, manager_id=101)
    pid = db.add_payment(100, "@m", "M", 50.0, "USD", "x", order_id=oid1)
    res = asyncio.run(db.link_payment_to_order(pid, oid2, linked_by=99))
    assert res["ok"] is False
    assert "уже привязан" in res["error"]


def test_link_pending_payment_happy_path(isolated_db):
    """Pending платёж → UPDATE order_id, без MS sync (платёж ещё не confirmed)."""
    db = isolated_db
    oid = _setup_order(db)
    pid = db.add_payment(100, "@m", "M", 50.0, "USD", "x")  # standalone
    assert asyncio.run(db.get_payment(pid))["order_id"] is None
    res = asyncio.run(db.link_payment_to_order(pid, oid, linked_by=99, linked_name="Boss"))
    assert res["ok"] is True
    assert res["ms_sync_triggered"] is False  # pending → не sync'аем
    assert res["order_closed"] is False
    assert asyncio.run(db.get_payment(pid))["order_id"] == oid


def test_link_confirmed_payment_triggers_ms_sync(isolated_db, monkeypatch):
    """Confirmed standalone payment → link → MS sync ставится в очередь."""
    db = isolated_db
    oid = _setup_order(db, total=100.0)
    pid = db.add_payment(100, "@m", "M", 50.0, "USD", "x")
    # Подтверждаем БЕЗ order_id — это standalone confirmed
    asyncio.run(db.confirm_payment(pid, 99, "Boss"))
    assert asyncio.run(db.get_payment(pid))["status"] == "confirmed"

    sync_called = []
    monkeypatch.setattr(
        db, "_trigger_ms_paymentin_sync", lambda pid_: sync_called.append(pid_)
    )

    res = asyncio.run(db.link_payment_to_order(pid, oid, linked_by=99, linked_name="Boss"))
    assert res["ok"] is True
    assert res["ms_sync_triggered"] is True
    assert sync_called == [pid]
    # Заказ ещё не закрыт (50 < 100)
    assert res["order_closed"] is False


def test_link_confirmed_payment_closes_order_if_total_reached(isolated_db, monkeypatch):
    """Confirmed payment на полную сумму → link → order закрывается."""
    db = isolated_db
    oid = _setup_order(db, total=100.0)
    pid = db.add_payment(100, "@m", "M", 100.0, "USD", "x")
    asyncio.run(db.confirm_payment(pid, 99, "Boss"))

    monkeypatch.setattr(db, "_trigger_ms_paymentin_sync", lambda _: None)
    res = asyncio.run(db.link_payment_to_order(pid, oid, linked_by=99, linked_name="Boss"))
    assert res["ok"] is True
    assert res["order_closed"] is True
    assert asyncio.run(db.get_order(oid)).get("paid_confirmed_at") is not None


def test_link_rejects_currency_mismatch(isolated_db):
    """WP-04: нельзя привязать платёж в чужой валюте к заказу — закрытие считает
    копейки в валюте заказа, кросс-валюта без конверсии ложно закрывала заказ."""
    db = isolated_db
    oid = _setup_order(db, total=100.0)  # currency NULL → база USD
    pid = db.add_payment(100, "@m", "M", 5_000_000.0, "UZS", "x")  # standalone UZS
    res = asyncio.run(db.link_payment_to_order(pid, oid, linked_by=99))
    assert res["ok"] is False
    assert "не совпадает" in res["error"]
    assert asyncio.run(db.get_payment(pid))["order_id"] is None  # не привязан


def test_close_ignores_foreign_currency_payment(isolated_db, monkeypatch):
    """WP-04: confirmed-платёж в валюте, отличной от валюты заказа, НЕ закрывает
    заказ (раньше 5 000 000 UZS в копейках перекрывали USD-заказ)."""
    db = isolated_db
    monkeypatch.setattr(db, "_trigger_ms_paymentin_sync", lambda _: None)
    oid = _setup_order(db, total=100.0)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET currency='USD' WHERE id=?"), (oid,))
        conn.commit()
    # Чужая валюта, привязана к заказу и подтверждена — закрывать не должна.
    p_uzs = db.add_payment(100, "@m", "M", 5_000_000.0, "UZS", "x", order_id=oid)
    asyncio.run(db.confirm_payment(p_uzs, 99, "Boss"))
    assert asyncio.run(db.get_order(oid)).get("paid_confirmed_at") is None
    # Платёж в валюте заказа на полную сумму — закрывает.
    p_usd = db.add_payment(100, "@m", "M", 100.0, "USD", "x", order_id=oid)
    asyncio.run(db.confirm_payment(p_usd, 99, "Boss"))
    assert asyncio.run(db.get_order(oid)).get("paid_confirmed_at") is not None


def test_link_writes_audit_log(isolated_db):
    db = isolated_db
    oid = _setup_order(db)
    pid = db.add_payment(100, "@m", "M", 50.0, "USD", "x")
    asyncio.run(db.link_payment_to_order(pid, oid, linked_by=99, linked_name="Boss"))
    # audit_log проверяем по action
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("SELECT action, details FROM audit_log WHERE action = ?"),
            ("payment_linked_to_order",),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert f"#{pid}" in row["details"]
    assert f"#{oid}" in row["details"]


def test_link_race_second_call_loses(isolated_db, monkeypatch):
    """После первого link order_id заполнен → второй UPDATE-WHERE-NULL не сработает."""
    db = isolated_db
    oid1 = _setup_order(db)
    oid2 = _setup_order(db, manager_id=101)
    pid = db.add_payment(100, "@m", "M", 50.0, "USD", "x")

    # Симулируем race: вручную ставим order_id (как «первый победитель»)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET order_id = ? WHERE id = ?"), (oid1, pid))
        conn.commit()
    # Сбрасываем check-кэш (link сначала get_payment видит уже-linked)
    res = asyncio.run(db.link_payment_to_order(pid, oid2, linked_by=99))
    assert res["ok"] is False
    assert "уже привязан" in res["error"]


def test_get_unlinked_payments_filters_by_status_and_order(isolated_db):
    """Только confirmed payments WHERE order_id IS NULL — pending не отдаём."""
    db = isolated_db
    db.set_role(100, "m", "M", "manager")
    oid = _setup_order(db)
    p_pending = db.add_payment(100, "@m", "M", 50.0, "USD", "pending standalone")
    p_confirmed_standalone = db.add_payment(100, "@m", "M", 60.0, "USD", "confirmed standalone")
    asyncio.run(db.confirm_payment(p_confirmed_standalone, 99, "Boss"))
    p_confirmed_linked = db.add_payment(100, "@m", "M", 70.0, "USD", "linked", order_id=oid)
    asyncio.run(db.confirm_payment(p_confirmed_linked, 99, "Boss"))

    unlinked = asyncio.run(db.get_unlinked_payments())  # async после asyncpg Stage 2
    ids = {p["id"] for p in unlinked}
    assert p_confirmed_standalone in ids
    assert p_pending not in ids  # pending не показываем
    assert p_confirmed_linked not in ids  # уже привязан — не показываем


def test_get_unlinked_payments_respects_limit(isolated_db):
    db = isolated_db
    db.set_role(100, "m", "M", "manager")
    pids = []
    for i in range(5):
        pid = db.add_payment(100, "@m", "M", 10.0 + i, "USD", f"p{i}")
        asyncio.run(db.confirm_payment(pid, 99, "Boss"))
        pids.append(pid)
    out = asyncio.run(db.get_unlinked_payments(limit=3))  # async после asyncpg Stage 2
    assert len(out) == 3
    # ORDER BY id DESC — последние созданные первыми
    assert out[0]["id"] == pids[-1]


# ─── E2E endpoints ─────────────────────────────────────────────────────────────


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    boss_id, mgr_id, book_id = 100, 200, 300
    db.set_role(boss_id, "boss", "Boss", "boss")
    db.set_role(mgr_id, "mgr", "Manager", "manager")
    db.set_role(book_id, "book", "Bookkeeper", "bookkeeper")

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {
            "id": int(init_data),
            "first_name": "U",
            "last_name": "X",
            "username": "u",
        },
    )
    return TestClient(server.app), db, {"boss": boss_id, "mgr": mgr_id, "book": book_id}


def test_unlinked_endpoint_open_to_bookkeeper(client_env):
    client, db, ids = client_env
    # Создаём confirmed standalone
    pid = db.add_payment(ids["mgr"], "@m", "M", 50.0, "USD", "x")
    asyncio.run(db.confirm_payment(pid, ids["boss"], "Boss"))
    resp = client.post("/api/payments/unlinked", json={"initData": str(ids["book"])})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert any(p["id"] == pid for p in data["payments"])


def test_link_endpoint_forbidden_for_manager(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/payments/link",
        json={"initData": str(ids["mgr"]), "payment_id": 1, "order_id": 2},
    )
    assert resp.status_code == 403


def test_link_endpoint_happy_path(client_env, monkeypatch):
    """Boss линкует confirmed payment → 200 + DB обновлена."""
    import services.database as db_mod

    monkeypatch.setattr(db_mod, "_trigger_ms_paymentin_sync", lambda _: None)
    client, db, ids = client_env
    oid = _setup_order(db, manager_id=ids["mgr"], total=100.0)
    pid = db.add_payment(ids["mgr"], "@m", "M", 100.0, "USD", "x")
    asyncio.run(db.confirm_payment(pid, ids["boss"], "Boss"))

    resp = client.post(
        "/api/payments/link",
        json={"initData": str(ids["boss"]), "payment_id": pid, "order_id": oid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["ms_sync_triggered"] is True
    assert body["order_closed"] is True
    assert asyncio.run(db.get_payment(pid))["order_id"] == oid


def test_link_endpoint_409_on_already_linked(client_env):
    """Конфликт-вариант возвращает 409, не 400 — UI может различать."""
    client, db, ids = client_env
    oid1 = _setup_order(db, manager_id=ids["mgr"])
    oid2 = _setup_order(db, manager_id=201)
    pid = db.add_payment(ids["mgr"], "@m", "M", 50.0, "USD", "x", order_id=oid1)
    resp = client.post(
        "/api/payments/link",
        json={"initData": str(ids["boss"]), "payment_id": pid, "order_id": oid2},
    )
    assert resp.status_code == 409


def test_link_endpoint_400_on_invalid_input(client_env):
    client, _db, ids = client_env
    resp = client.post(
        "/api/payments/link",
        json={"initData": str(ids["boss"]), "payment_id": "not-int", "order_id": 1},
    )
    assert resp.status_code == 400
