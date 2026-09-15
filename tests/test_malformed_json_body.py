"""Битое тело запроса без initData не должно давать 500 + алерт (аудит, п.4).

Ручки читают `await request.json()` ДО `_authorize` (см. `test_body_size_
limit.py` про потолок размера). Раньше:
  * `{` (не парсится) — `json.JSONDecodeError` улетал в общий
    `_unhandled_exception`: 500 клиенту и алерт админам — хотя это просто
    кривой клиент/скан, а не поломка сервиса;
  * `[]` / `"x"` (валидный JSON, но не объект) — `data.get("initData", "")`
    внутри `_authorize` падал `AttributeError` (у списка/строки нет `.get`) —
    та же дорога в 500 + алерт.

Фикс — централизованно: `_unhandled_exception` отвечает 400 на
`JSONDecodeError` без алерта, `_authorize` отвечает 401 на body не-dict (как
на невалидную подпись) ДО обращения к `.get`. Не переписываем все ручки —
только точку входа.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(isolated_db, monkeypatch):
    import webapp.server as server

    calls: list = []

    async def _fake_report(exc, where="", **kw):
        calls.append((exc, where))

    from services import error_alerts

    monkeypatch.setattr(error_alerts, "report_exception", _fake_report)
    # raise_server_exceptions=False: Starlette's ServerErrorMiddleware always
    # re-raises after a registered Exception/500 handler runs (so servers can
    # log/test clients can opt in) — see tests/test_error_alerts.py for the
    # same idiom already established in this codebase.
    c = TestClient(server.app, raise_server_exceptions=False)
    c.alert_calls = calls
    return c


ENDPOINTS = [
    "/api/me",
    "/api/orders/add_item",
    "/api/orders/payment",
    "/api/metrics",
]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_malformed_json_is_400_without_alert(client, path):
    r = client.post(path, content=b"{", headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    assert client.alert_calls == []


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("body", [b"[]", b'"x"', b"42", b"null"])
def test_non_dict_json_body_is_401_without_alert(client, path, body):
    r = client.post(path, content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 401, r.text
    assert client.alert_calls == []


def test_well_formed_dict_body_still_reaches_authorize(client):
    """Контроль: обычное тело без initData по-прежнему 401 от подписи, а не
    от нашей новой проверки типа — значит, мы не сломали нормальный путь."""
    r = client.post("/api/me", json={"initData": "not-a-real-signature"})
    assert r.status_code == 401
    assert client.alert_calls == []


def test_background_alert_never_spawned_for_bad_json(client):
    """Никаких фоновых задач-алертов не должно ставиться в очередь вовсе —
    не только report_exception не вызван, но и spawn под него."""
    from utils.background import pending

    client.post("/api/me", content=b"{not json", headers={"Content-Type": "application/json"})
    assert not pending()
