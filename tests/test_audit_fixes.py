"""
Регрессы на находки аудита: H1 (дубли/сверх-кол-во возвратов),
H2 (возврат только своего заказа + дедлайн для менеджера),
M3 (pending-сдачи не дают двойного распределения остатка).
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


def _shipped_order(db, owner=1, qty=2, price=100.0):
    oid = db.create_order(owner, "M", "")
    db.update_order_agent(oid, "A", "Client")
    db.add_order_item(oid, "Товар", "", qty, "шт", price)
    db.update_order_status(oid, "shipped")
    return oid


# ─── H1 ──────────────────────────────────────────────────────────────────────


def test_duplicate_return_blocked(isolated_db):
    db = isolated_db
    oid = _shipped_order(db)
    items = asyncio.run(db.get_order_items(oid))
    r1 = asyncio.run(
        db.create_return(
            oid,
            "full",
            "брак",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert r1["ok"]
    r2 = asyncio.run(
        db.create_return(
            oid,
            "full",
            "ещё",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert r2["ok"] is False
    assert "уже есть" in r2["error"]


def test_return_qty_clamped_to_available(isolated_db):
    db = isolated_db
    oid = _shipped_order(db, qty=2, price=100.0)
    items = asyncio.run(db.get_order_items(oid))
    # просят 5 при доступных 2 → режется до 2, сумма пересчитывается
    r = asyncio.run(
        db.create_return(
            oid,
            "full",
            "брак",
            [(items[0]["id"], 5, 500.0)],
            refund_method="no_refund",
            created_by=1,
        )
    )
    assert r["ok"] is True
    assert r["total_amount"] == 200.0


# ─── H2 (DB-уровень: дедлайн через force) ────────────────────────────────────


def test_manager_return_blocked_after_deadline(isolated_db):
    db = isolated_db
    oid = _shipped_order(db)
    # отгружен давно
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET shipped_at = ? WHERE id = ?"), ("2000-01-01 00:00:00", oid)
        )
        conn.commit()
    items = asyncio.run(db.get_order_items(oid))
    # менеджер (force=False) — за дедлайном, нельзя
    r_mgr = asyncio.run(
        db.create_return(
            oid,
            "full",
            "x",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=1,
            force=False,
        )
    )
    assert r_mgr["ok"] is False
    # начальство (force=True) — можно
    r_boss = asyncio.run(
        db.create_return(
            oid,
            "full",
            "x",
            [(items[0]["id"], 2, 200.0)],
            refund_method="no_refund",
            created_by=2,
            force=True,
        )
    )
    assert r_boss["ok"] is True


# ─── M3 ──────────────────────────────────────────────────────────────────────


def test_pending_deposit_not_double_allocated(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    _shipped_order(db, owner=1, qty=1, price=250.0)
    d1 = asyncio.run(db.create_cash_deposit(1, 250.0))
    assert d1["ok"] and asyncio.run(db.get_cash_deposit_orders(d1["deposit_id"]))
    # после первой pending-сдачи остаток заказа исчерпан — вторая ничего не берёт
    assert asyncio.run(db.get_manager_open_orders_for_deposit(1)) == []
    d2 = asyncio.run(db.create_cash_deposit(1, 250.0))
    assert d2["ok"]
    assert asyncio.run(db.get_cash_deposit_orders(d2["deposit_id"])) == []


# ─── H2 (webapp: чужой заказ) ────────────────────────────────────────────────


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    db.set_role(200, "a", "MgrA", "manager")
    db.set_role(201, "b", "MgrB", "manager")
    oid = _shipped_order(db, owner=200, qty=1, price=100.0)

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, oid


def test_manager_cannot_return_foreign_order(client_env):
    client, db, oid = client_env
    resp = client.post(
        "/api/returns/create",
        json={
            "initData": "201",
            "order_id": oid,
            "reason": "норм причина",
            "refund_method": "cash",
        },
    )
    assert resp.status_code == 403


# ─── R1 (деактивация мгновенна, минуя кэш ролей) ─────────────────────────────


def test_deactivated_user_blocked_immediately(client_env):
    """R1: после deactivate_user пользователь получает 403 СРАЗУ, без ожидания
    TTL кэша ролей — _authorize проверяет deactivated_at прямым read из БД.
    Прогреваем кэш активной ролью до деактивации, чтобы поймать stale-cache."""
    client, db, _ = client_env

    # Прогрев кэша ролей (как если бы webapp уже видел активного юзера).
    from services.roles import cached_role

    assert cached_role(200) == "manager"

    # Деактивируем (как из бот-процесса — webapp-кэш НЕ инвалидируется).
    assert asyncio.run(db.deactivate_user(200, by=1)) is True

    # Эндпоинт со скоупом по себе (allowed_roles=None) — всё равно 403.
    resp = client.post("/api/payments/history", json={"initData": "200"})
    assert resp.status_code == 403
    # И ролевой эндпоинт тоже.
    resp2 = client.post("/api/orders", json={"initData": "200"})
    assert resp2.status_code == 403
    # WP-21: /api/me раньше обходил гейт деактивации (звал verify_init_data
    # напрямую) → отдавал 200. Теперь тоже 403.
    resp3 = client.post("/api/me", json={"initData": "200"})
    assert resp3.status_code == 403


# ─── R2 (DB-идемпотентность денежных create) ─────────────────────────────────


def test_return_create_idempotent_same_key(client_env, monkeypatch):
    """R2: два POST /api/returns/create с одним idempotency_key создают РОВНО
    один возврат (DB-claim), даже если in-mem кэш «забыл» (эмулируем сбросом)."""
    client, db, oid = client_env

    # Гасим внешние уведомления (Telegram) — не часть теста.
    import webapp.server as server

    async def _noop_bot():
        class _B:
            async def send_message(self, *a, **k):
                pass
        return _B()
    monkeypatch.setattr(server, "get_notify_bot", _noop_bot)

    body = {
        "initData": "200", "order_id": oid, "reason": "брак партии",
        "refund_method": "debt_reduction", "idempotency_key": "FIXED-KEY-1",
    }
    r1 = client.post("/api/returns/create", json=body)
    assert r1.status_code == 200, r1.text
    rid = r1.json()["return_id"]

    # Эмулируем потерю in-memory кэша (рестарт/другой воркер).
    server._IDEM_CACHE.clear()

    r2 = client.post("/api/returns/create", json=body)
    assert r2.status_code == 200, r2.text
    # Тот же return_id — повтор не создал второй возврат.
    assert r2.json()["return_id"] == rid

    # В БД ровно один pending-возврат по заказу.
    pend = [x for x in asyncio.run(db.get_pending_returns()) if x["order_id"] == oid]
    assert len(pend) == 1


# ─── Перф: кэш флага деактивации (TTL 30с) с мгновенной инвалидацией ──────────


def test_deact_cache_invalidated_on_deactivate_same_process(isolated_db):
    """`_authorize` зовёт cached_is_deactivated на каждый запрос — это кэш с TTL,
    а не SELECT каждый раз. Но deactivate_user должен инвалидировать кэш в своём
    процессе, чтобы блок наступал мгновенно (не ждать TTL)."""
    import services.roles as roles

    db = isolated_db
    db.set_role(300, "u", "U", "manager")

    # Прогреваем деакт-кэш как «активен» (False закэширован).
    assert roles.cached_is_deactivated(300) is False

    # Деактивируем — _invalidate_role_cache внутри сбрасывает и деакт-кэш.
    assert asyncio.run(db.deactivate_user(300, by=1)) is True

    # Кэш сброшен → следующий вызов читает БД заново и видит деактивацию.
    assert roles.cached_is_deactivated(300) is True

    # Реактивация так же мгновенно снимает блок.
    assert asyncio.run(db.reactivate_user(300, by=1)) is True
    assert roles.cached_is_deactivated(300) is False
