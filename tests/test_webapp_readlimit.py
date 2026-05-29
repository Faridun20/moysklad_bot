"""
Read-эндпоинты WebApp теперь проходят через _authorize: сохранена ролевая
политика и добавлен per-user rate limit (раньше они инлайнили verify_init_data
без лимита).

Проверяем на эндпоинтах, которые НЕ ходят в МойСклад (orders / orders/requests /
payments/history) — auth/limit отрабатывает до любой бизнес-логики.
Границу (verify_init_data) мокаем; БД/роли настоящие (isolated_db).
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server
    from services import rate_limit

    importlib.reload(roles)
    rate_limit.reset()  # in-memory buckets живут между тестами — чистим
    db = isolated_db

    boss, mgr, book, guest = 100, 200, 400, 500
    db.set_role(boss, "b", "Boss", "boss")
    db.set_role(mgr, "m", "Mgr", "manager")
    db.set_role(book, "k", "Book", "bookkeeper")
    # guest (500) — роль не задаём, get_role вернёт 'guest'

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)
    return client, db, {"boss": boss, "mgr": mgr, "book": book, "guest": guest}


def test_orders_requests_forbidden_for_manager(client_env):
    client, _db, ids = client_env
    r = client.post("/api/orders/requests", json={"initData": str(ids["mgr"])})
    assert r.status_code == 403


def test_orders_requests_ok_for_boss(client_env):
    client, _db, ids = client_env
    r = client.post("/api/orders/requests", json={"initData": str(ids["boss"])})
    assert r.status_code == 200, r.text


def test_orders_allows_any_valid_user(client_env):
    # allowed_roles=None: бухгалтер/гость получают свой (пустой) список, не 403.
    client, _db, ids = client_env
    for uid in (ids["book"], ids["guest"]):
        r = client.post("/api/orders", json={"initData": str(uid)})
        assert r.status_code == 200, r.text


def test_payments_history_allows_any_valid_user(client_env):
    client, _db, ids = client_env
    r = client.post("/api/payments/history", json={"initData": str(ids["book"])})
    assert r.status_code == 200, r.text


def test_read_endpoint_is_rate_limited(client_env):
    client, _db, ids = client_env
    from services import rate_limit

    uid = ids["mgr"]
    # Заполняем bucket ровно как это делает эндпоинт (scope/max/window).
    for _ in range(120):
        assert rate_limit.acquire("api_orders", uid, 120, 60.0)
    # Следующий запрос через эндпоинт упирается в лимит → 429.
    r = client.post("/api/orders", json={"initData": str(uid)})
    assert r.status_code == 429
