"""
Регрессии безопасности — два инварианта, которые легко нечаянно нарушить:

1. Каждый /api/* endpoint обязан проходить через _authorize (initData +
   роль). Забыть вызов на новом endpoint'е — значит открыть его всем.
   Тест перебирает ВСЕ POST-роуты приложения и шлёт мусорный initData:
   ответ обязан быть 401/403, а не 200. Webhook-роуты (секрет в пути,
   не initData) исключены явно.

2. Пользовательский ввод (agent_name/comment и т.п.) экранируется перед
   вставкой в parse_mode=HTML — иначе инъекция разметки/тегов.
"""

import pytest
from fastapi.testclient import TestClient


# ─── 1. Все /api/* требуют валидный initData ─────────────────────────────────


def _api_post_paths(app) -> list[str]:
    """POST-роуты под /api/, кроме webhook'ов (там секрет в пути, не initData)."""
    paths = []
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if "POST" not in methods:
            continue
        if not path.startswith("/api/"):
            continue
        if "{secret}" in path or "webhook" in path:
            continue
        paths.append(path)
    return paths


@pytest.fixture
def client(isolated_db):
    import webapp.server as server
    return TestClient(server.app)


def test_every_api_endpoint_rejects_bad_initdata(client):
    app = client.app
    paths = _api_post_paths(app)
    assert paths, "не нашли ни одного /api/ POST-роута — тест бесполезен"

    failures = []
    for path in paths:
        resp = client.post(path, json={"initData": "garbage-not-a-valid-hmac"})
        # _authorize: 401 (битый initData) или 403 (роль). Любой не-2xx ок,
        # но 200 = endpoint пустил неаутентифицированного → дыра.
        if resp.status_code not in (401, 403):
            failures.append((path, resp.status_code))

    assert not failures, (
        "эндпоинты пустили запрос с мусорным initData (нет _authorize?): "
        f"{failures}"
    )


# ─── 2. Экранирование пользовательского ввода в HTML ─────────────────────────


def test_esc_escapes_html_metacharacters():
    from utils.helpers import esc
    out = esc("<script>alert(1)</script> & <b>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_format_order_escapes_agent_name_and_comment():
    from handlers.orders import format_order
    order = {
        "id": 1,
        "status": "draft",
        "created_at": "2026-05-20 10:00",
        "full_name": "Менеджер",
        "agent_name": "<b>ХАКЕР</b>",
        "comment": "<i>злой</i> & комментарий",
        "currency": "USD",
    }
    text = format_order(order, items=[])
    # Сырые теги пользователя не должны просочиться в HTML-сообщение.
    assert "<b>ХАКЕР</b>" not in text
    assert "<i>злой</i>" not in text
    assert "&lt;b&gt;" in text
    assert "&amp;" in text
