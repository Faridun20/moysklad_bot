"""T2.5 — идемпотентность денежных ручек живёт в БД, а не в памяти процесса.

Критерий готовности: два параллельных запроса с одним `idempotency_key`
дают один эффект.

Был in-memory кэш с TTL 30 c (`_IDEM_CACHE`). Он не переживал рестарт и не
делился между воркерами uvicorn — то есть на денежных ручках защиты
фактически не было: ретрай клиента после рестарта или запрос, попавший в
другой воркер, проходил как новый. Результат — лишний платёж, лишний
документ в МойСклад, второе уведомление менеджеру.
"""

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

import webapp.server as server


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    db = isolated_db
    boss_id, mgr_id = 900, 901
    db.set_role(boss_id, "boss", "Boss", "boss")
    db.set_role(mgr_id, "mgr", "Mgr", "manager")

    import services.roles as roles

    importlib.reload(roles)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    monkeypatch.setattr(db, "_trigger_ms_paymentin_sync", lambda *a, **k: None)
    return TestClient(server.app), db, {"boss": boss_id, "mgr": mgr_id}


def _credit_order(db, mgr_id, total=1000.0):
    oid = db.create_order(mgr_id, "Mgr", "")
    db.add_order_item(oid, "T", "href", 1, "шт", total)
    db.update_order_agent(oid, "A-1", "Клиент")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type='credit', currency='USD' WHERE id=?"), (oid,)
        )
        conn.commit()
    db.update_order_status(oid, "shipped")
    return oid


def _payments(db, oid):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT id, status FROM payments WHERE order_id = ?"), (oid,))
        return [dict(r) for r in cur.fetchall()]


# ─── Кэша в памяти больше нет ───────────────────────────────────────────────


def test_in_memory_cache_is_gone():
    """Символа быть не должно — иначе кто-то снова начнёт им пользоваться."""
    assert not hasattr(server, "_IDEM_CACHE")
    assert not hasattr(server, "_idem_get")
    assert not hasattr(server, "_idem_set")


# ─── Критерий готовности: один эффект на один ключ ─────────────────────────


def test_mark_paid_same_key_creates_one_payment(client_env):
    client, db, ids = client_env
    oid = _credit_order(db, ids["mgr"])
    body = {
        "initData": str(ids["mgr"]),
        "order_id": oid,
        "amount": 100.0,
        "idempotency_key": "same-key",
    }

    r1 = client.post("/api/orders/mark_paid", json=body)
    r2 = client.post("/api/orders/mark_paid", json=body)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # Второй запрос вернул СОХРАНЁННЫЙ результат первого, а не создал платёж.
    assert r1.json()["payment_id"] == r2.json()["payment_id"]
    assert len(_payments(db, oid)) == 1


def test_mark_paid_distinct_keys_create_two_payments(client_env):
    """Разные ключи — разные операции: идемпотентность не должна склеивать
    две настоящие частичные оплаты."""
    client, db, ids = client_env
    oid = _credit_order(db, ids["mgr"])

    for key in ("key-a", "key-b"):
        r = client.post(
            "/api/orders/mark_paid",
            json={
                "initData": str(ids["mgr"]),
                "order_id": oid,
                "amount": 100.0,
                "idempotency_key": key,
            },
        )
        assert r.status_code == 200, r.text

    assert len(_payments(db, oid)) == 2


def test_confirm_payment_same_key_confirms_once(client_env):
    client, db, ids = client_env
    oid = _credit_order(db, ids["mgr"])
    client.post(
        "/api/orders/mark_paid",
        json={"initData": str(ids["mgr"]), "order_id": oid, "amount": 100.0},
    )
    body = {"initData": str(ids["boss"]), "order_id": oid, "idempotency_key": "confirm-1"}

    r1 = client.post("/api/orders/confirm_payment", json=body)
    r2 = client.post("/api/orders/confirm_payment", json=body)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # Второй вызов вернул тот же счётчик, а не «подтвердил ещё раз».
    assert r1.json()["confirmed_count"] == r2.json()["confirmed_count"] == 1
    assert [p["status"] for p in _payments(db, oid)] == ["confirmed"]


def test_key_is_scoped_per_user(client_env):
    """Один и тот же ключ от РАЗНЫХ пользователей — разные операции."""
    client, db, ids = client_env
    o1 = _credit_order(db, ids["mgr"])
    o2 = _credit_order(db, ids["boss"])

    r1 = client.post(
        "/api/orders/mark_paid",
        json={
            "initData": str(ids["mgr"]),
            "order_id": o1,
            "amount": 10.0,
            "idempotency_key": "shared",
        },
    )
    r2 = client.post(
        "/api/orders/mark_paid",
        json={
            "initData": str(ids["boss"]),
            "order_id": o2,
            "amount": 10.0,
            "idempotency_key": "shared",
        },
    )
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(_payments(db, o1)) == 1
    assert len(_payments(db, o2)) == 1


def test_key_survives_process_restart(client_env):
    """Ключ живёт в БД: «рестарт» процесса (новый TestClient) его не теряет.

    Ровно этот сценарий in-memory кэш не закрывал.
    """
    client, db, ids = client_env
    oid = _credit_order(db, ids["mgr"])
    body = {
        "initData": str(ids["mgr"]),
        "order_id": oid,
        "amount": 100.0,
        "idempotency_key": "survives",
    }

    r1 = client.post("/api/orders/mark_paid", json=body)
    assert r1.status_code == 200, r1.text

    fresh = TestClient(server.app)  # как будто другой воркер / после рестарта
    r2 = fresh.post("/api/orders/mark_paid", json=body)

    assert r2.status_code == 200, r2.text
    assert r1.json()["payment_id"] == r2.json()["payment_id"]
    assert len(_payments(db, oid)) == 1


# ─── Claim/release на уровне БД ─────────────────────────────────────────────


def test_claim_is_atomic(isolated_db):
    """idem_claim столбит ключ ровно один раз — это и есть защита от гонки."""
    db = isolated_db

    async def race():
        return await asyncio.gather(
            db.idem_claim("k", "op", 1),
            db.idem_claim("k", "op", 1),
            db.idem_claim("k", "op", 1),
        )

    results = asyncio.run(race())
    mine = [r for r in results if r is None]
    assert len(mine) == 1, f"ключ застолбили несколько раз: {results}"


def test_release_frees_key_for_retry(isolated_db):
    """Операция не состоялась → ключ освобождён, ретрай тем же ключом работает."""
    db = isolated_db
    assert asyncio.run(db.idem_claim("k", "op", 1)) is None
    asyncio.run(db.idem_release("k"))
    assert asyncio.run(db.idem_claim("k", "op", 1)) is None


def test_stored_result_is_returned_verbatim(isolated_db):
    db = isolated_db
    assert asyncio.run(db.idem_claim("k", "op", 1)) is None
    asyncio.run(db.idem_store("k", {"ok": True, "payment_id": 77}))
    assert asyncio.run(db.idem_claim("k", "op", 1)) == {"ok": True, "payment_id": 77}


def test_in_flight_key_conflicts(client_env):
    """Ключ занят, а результата ещё нет (упало до store) → 409, а не дубль.

    Отказать безопаснее, чем рискнуть вторым платежом.
    """
    client, db, ids = client_env
    oid = _credit_order(db, ids["mgr"])
    asyncio.run(db.idem_claim(f"mark_paid:{ids['mgr']}:in-flight", "mark_paid", ids["mgr"]))

    r = client.post(
        "/api/orders/mark_paid",
        json={
            "initData": str(ids["mgr"]),
            "order_id": oid,
            "amount": 100.0,
            "idempotency_key": "in-flight",
        },
    )
    assert r.status_code == 409, r.text
    assert _payments(db, oid) == []
