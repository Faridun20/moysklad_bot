"""
End-to-end: /api/metrics endpoint + metrics middleware.

Проверяем:
  * middleware считает каждый запрос к /api/* (ok / 4xx / 5xx раздельно)
  * /api/metrics требует admin/boss роли (manager → 403)
  * snapshot содержит counters/timings/pool/uptime/version

Мокаем границу: verify_init_data; БД — настоящая.
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_env(isolated_db, monkeypatch):
    import services.metrics as m
    import services.roles as roles
    import webapp.server as server

    # Свежие метрики на каждый тест.
    importlib.reload(m)
    importlib.reload(roles)
    # server держит ссылку на старый модуль — сбрасываем напрямую тоже.
    server_metrics = importlib.import_module("services.metrics")
    server_metrics.reset()

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
    return client, {"boss": boss_id, "mgr": mgr_id}, server_metrics


def test_metrics_endpoint_forbidden_for_manager(client_env):
    client, ids, _ = client_env
    resp = client.post("/api/metrics", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 403


def test_metrics_endpoint_returns_snapshot_for_boss(client_env):
    client, ids, _ = client_env
    resp = client.post("/api/metrics", json={"initData": str(ids["boss"])})
    assert resp.status_code == 200
    data = resp.json()
    # Базовая структура
    for key in ("uptime_sec", "version", "counters", "timings", "pool"):
        assert key in data
    # SQLite — pool пустой
    assert data["pool"] == {}
    # uptime ≥ 0, version — строка
    assert data["uptime_sec"] >= 0
    assert isinstance(data["version"], str)


def test_middleware_counts_ok_requests(client_env):
    client, ids, m = client_env
    # Несколько успешных запросов
    for _ in range(3):
        client.post("/api/metrics", json={"initData": str(ids["boss"])})
    snap = m.snapshot()
    # 4-й запрос показывает счётчик от первых 3 (4-й ещё не закрыт)
    final = client.post("/api/metrics", json={"initData": str(ids["boss"])})
    snap = final.json()
    assert snap["counters"]["/api/metrics.ok"] >= 3
    # И timing записан
    assert snap["timings"]["/api/metrics"]["count"] >= 3


def test_middleware_counts_403_as_4xx(client_env):
    client, ids, m = client_env
    # Manager → 403 от /api/metrics
    client.post("/api/metrics", json={"initData": str(ids["mgr"])})
    # Boss получит snapshot где видно 4xx counter
    resp = client.post("/api/metrics", json={"initData": str(ids["boss"])})
    snap = resp.json()
    assert snap["counters"].get("/api/metrics.4xx", 0) >= 1


def test_middleware_skips_non_api_paths(client_env):
    client, _ids, m = client_env
    client.get("/healthz")
    snap = m.snapshot()
    # /healthz — не /api/*, middleware его не трекает
    assert "/healthz" not in snap["counters"]
    assert "/healthz" not in snap["timings"]
