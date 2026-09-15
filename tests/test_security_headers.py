"""Заголовки безопасности WebApp-ответов (аудит, финдинг #8).

X-Content-Type-Options и Referrer-Policy — стандартная гигиена. CSP —
совместимый с Telegram WebApp: скрипт telegram-web-app.js грузится с
telegram.org (index.html), JS-шаблоны (helpers.js/app.js) вставляют
`style="…"` в innerHTML — нужен `style-src 'unsafe-inline'`, а превью фото
(canvas.toDataURL/URL.createObjectURL, webapp/static/app.js) даёт `img src=
data:/blob:`.

БЕЗ `X-Frame-Options: DENY` — Telegram-клиенты (web.telegram.org) открывают
WebApp в iframe, DENY сломал бы вход целиком. Изоляция от постороннего
встраивания — через CSP `frame-ancestors` (заменяет X-Frame-Options и
позволяет точечно разрешить нужные хосты).

`script-src` тоже с `'unsafe-inline'` — без него молча отваливаются все
`onclick="…"` в разметке, которую те же шаблоны вставляют через innerHTML
(их сотни): CSP расценивает атрибут-обработчик как инлайн-скрипт наравне с
`<script>`, а браузер просто не выполняет клик, без ошибки в JS. Поймано
живым e2e (test_hanging_request_ends_with_retry_instead_of_endless_spinner:
кнопка «Повторить» переставала работать), не юнитом на сам заголовок —
дальше в файле только это и добавили как отдельную проверку.
Настоящего инлайн-<script> в проекте нет — unsafe-inline здесь не открывает
внедрение постороннего <script src=…>, script-src по-прежнему это блокирует.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(isolated_db):
    import webapp.server as server

    return TestClient(server.app)


def _headers(resp):
    return {k.lower(): v for k, v in resp.headers.items()}


def test_index_page_has_nosniff_and_referrer_policy(client):
    r = client.get("/")
    assert r.status_code == 200
    h = _headers(r)
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_index_page_never_sets_x_frame_options_deny(client):
    """Telegram встраивает WebApp в iframe — X-Frame-Options: DENY сломал бы
    вход целиком. Изоляция — через CSP frame-ancestors, не через этот
    заголовок."""
    r = client.get("/")
    h = _headers(r)
    assert h.get("x-frame-options") != "DENY"


def test_index_page_csp_allows_telegram_script_and_frame_embedding(client):
    r = client.get("/")
    h = _headers(r)
    csp = h.get("content-security-policy")
    assert csp, "CSP-заголовок обязан быть на главной странице"
    assert "https://telegram.org" in csp
    assert "frame-ancestors" in csp
    assert "https://web.telegram.org" in csp
    assert "https://*.telegram.org" in csp
    # frame-ancestors — НЕ x-frame-options: страница по-прежнему встраиваема.
    assert "'none'" not in csp.split("frame-ancestors", 1)[1].split(";", 1)[0]


def test_csp_allows_inline_onclick_handlers_injected_by_frontend_js(client):
    """helpers.js/app.js вставляют `onclick="…"` в innerHTML (JS-шаблоны) —
    без 'unsafe-inline' в script-src такие обработчики не выполнялись бы:
    браузер молча глотает клик, без исключения (см. докстринг файла)."""
    r = client.get("/")
    csp = _headers(r).get("content-security-policy", "")
    script_src = next((d for d in csp.split(";") if d.strip().startswith("script-src")), "")
    assert "'unsafe-inline'" in script_src


def test_csp_allows_inline_styles_injected_by_frontend_js(client):
    """helpers.js/app.js вставляют `style="…"` в innerHTML (JS-шаблоны) —
    без 'unsafe-inline' в style-src эти узлы просто не красились бы."""
    r = client.get("/")
    csp = _headers(r).get("content-security-policy", "")
    style_src = next((d for d in csp.split(";") if d.strip().startswith("style-src")), "")
    assert "'unsafe-inline'" in style_src


def test_csp_allows_data_and_blob_images(client):
    """Превью фото (base64 canvas.toDataURL) и proxy-фото техники
    (URL.createObjectURL) — img src идёт data:/blob:, не с сервера."""
    r = client.get("/")
    csp = _headers(r).get("content-security-policy", "")
    img_src = next((d for d in csp.split(";") if d.strip().startswith("img-src")), "")
    assert "data:" in img_src
    assert "blob:" in img_src


def test_api_response_also_carries_security_headers(client):
    """Заголовки — не только у главной страницы: тот же ответ и на /api/*."""
    r = client.post("/api/me", json={"initData": "not-a-real-signature"})
    assert r.status_code == 401
    h = _headers(r)
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert h.get("content-security-policy")


def test_static_asset_also_carries_headers(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200
    h = _headers(r)
    assert h.get("x-content-type-options") == "nosniff"
