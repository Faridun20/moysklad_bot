"""
/api/home: оркестрация ответа (gather независимых источников + фолбэк при сбое
МойСклад). Агрегация аналитики покрыта в test_analytics_parallel через границу;
здесь — что эндпоинт правильно собирает ответ и НЕ отдаёт 500, когда МС упал.

БД/роли настоящие (isolated_db). Коллабораторы МС (get_sales_stats/get_shipments)
подменяем — единица под тестом тут именно api_home, а не аналитика.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def home_env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)  # сброс кэша ролей (reload очищает _role_cache)
    db = isolated_db

    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")

    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)
    return client, db, {"boss": boss_id, "mgr": mgr_id}


def _mock_ms(monkeypatch, sales=None, shipments=None, fail=False):
    import services.moysklad as moysklad

    async def fake_sales(since, until=None):
        if fail:
            raise RuntimeError("MS down")
        return sales or {"total": 0, "count": 0, "clients": 0, "top_products": []}

    async def fake_shipments(since, until=None):
        if fail:
            raise RuntimeError("MS down")
        return shipments or []

    monkeypatch.setattr(moysklad, "get_sales_stats", fake_sales)
    monkeypatch.setattr(moysklad, "get_shipments", fake_shipments)


def test_home_boss_ok(home_env, monkeypatch):
    client, db, ids = home_env
    _mock_ms(
        monkeypatch,
        sales={"total": 500000, "count": 3, "clients": 2, "top_products": []},
        shipments=[
            {"attributes": [{"name": "telegram_full_name", "value": "Иван"}], "sum": 300000},
            {"attributes": [{"name": "telegram_full_name", "value": "Иван"}], "sum": 100000},
            {"sum": 50000},  # без атрибута → "Прочее (вручную в МойСклад)"
        ],
    )
    r = client.post("/api/home", json={"initData": str(ids["boss"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "boss"
    assert body["today"]["revenue"] == 5000.0  # 500000 / 100
    assert body["today"]["scope"] == "company"
    assert "pending_requests" in body
    emp = {e["name"]: e for e in body["top_employees"]}
    assert emp["Иван"]["revenue"] == 4000.0  # (300000 + 100000) / 100
    assert emp["Иван"]["count"] == 2
    assert "Прочее (вручную в МойСклад)" in emp


def test_home_boss_survives_ms_failure(home_env, monkeypatch):
    """Сбой МойСклад → фолбэк (нули + пустой лидерборд), а не 500. Покрывает
    ветку gather(return_exceptions=True) + isinstance-фолбэк для обоих источников."""
    client, db, ids = home_env
    _mock_ms(monkeypatch, fail=True)
    r = client.post("/api/home", json={"initData": str(ids["boss"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["today"]["revenue"] == 0
    assert body["today"]["shipments"] == 0
    assert body["top_employees"] == []


def test_home_manager_is_ms_free(home_env, monkeypatch):
    """Менеджер считает показатели из локальной БД и НЕ ходит в МС: даже при
    падающих МС-моках ответ 200 (если бы путь звал МС — был бы 500)."""
    client, db, ids = home_env
    _mock_ms(monkeypatch, fail=True)
    r = client.post("/api/home", json={"initData": str(ids["mgr"])})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "manager"
    assert body["today"]["scope"] == "personal"
