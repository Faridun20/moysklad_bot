"""Потолок размера тела запроса (п.10 аудита).

Ручки читают `request.json()` до `_authorize`, поэтому тело любого размера от
кого угодно ложилось в память. Проверяем: большое тело — 413 ещё до ручки (и с
Content-Length, и chunked без него), а законная загрузка фото (base64 до 5 МБ)
проходит.
"""

import base64

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(isolated_db, monkeypatch):
    import webapp.server as server

    monkeypatch.setattr(server, "verify_init_data", lambda s: None)  # всем 401 — до ручки дошли
    return TestClient(server.app)


def test_oversized_body_with_content_length_is_413(client):
    import webapp.server as server

    body = b'{"initData": "x", "pad": "' + b"a" * (server.MAX_BODY_BYTES + 10) + b'"}'
    r = client.post("/api/orders/add_item", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.json()["detail"].startswith("Слишком большой запрос")


def test_oversized_chunked_body_without_length_is_413(client):
    import webapp.server as server

    chunk = b"a" * (1024 * 1024)

    def gen():
        yield b'{"initData": "x", "pad": "'
        for _ in range(server.MAX_BODY_BYTES // len(chunk) + 2):
            yield chunk
        yield b'"}'

    r = client.post("/api/orders/add_item", content=gen(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_photo_sized_body_passes_the_limit(client):
    """Фото на 5 МБ в base64 (~6,7 МБ JSON) доходит до ручки: там 401 от
    подписи initData, а не 413 от потолка."""
    blob = b"\xff\xd8\xff" + b"\0" * (5 * 1024 * 1024 - 3)
    data_url = "data:image/jpeg;base64," + base64.b64encode(blob).decode()
    r = client.post("/api/products/photo_upload", json={"initData": "x", "data_url": data_url, "product_id": 1})
    assert r.status_code == 401, r.text


def test_small_requests_unaffected(client):
    r = client.post("/api/orders/add_item", json={"initData": "x"})
    assert r.status_code == 401
    assert client.get("/healthz").status_code == 200
