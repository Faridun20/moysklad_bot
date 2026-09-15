"""Кэш статики: версионированный ассет — на год и immutable, остальное — с
проверкой свежести.

Прежний `max-age=86400` на всё подряд: WebView раз в сутки перекачивал app.js
того же коммита, а неверсионированный запрос держался сутки даже после деплоя.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import webapp.server as server

    return TestClient(server.app), server


def test_versioned_asset_is_immutable_for_a_year(client):
    c, server = client
    for asset in ("app.js", "helpers.js", "style.css"):
        r = c.get(f"/static/{asset}?v={server.APP_VERSION}")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_unversioned_asset_revalidates(client):
    c, _server = client
    r = c.get("/static/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"


def test_foreign_version_is_not_pinned(client):
    """Старая вкладка после деплоя просит ?v=<старый SHA> и получает НОВЫЙ файл —
    закрепить его на год под старым URL значит отравить кэш на откат."""
    c, _server = client
    r = c.get("/static/app.js?v=deadbeef-not-this-build")
    assert r.headers["cache-control"] == "no-cache"


def test_not_modified_keeps_the_policy(client):
    c, server = client
    url = f"/static/app.js?v={server.APP_VERSION}"
    etag = c.get(url).headers["etag"]
    r = c.get(url, headers={"If-None-Match": etag})
    assert r.status_code == 304
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_index_html_is_never_cached_blindly(client):
    c, server = client
    root = c.get("/")
    assert root.headers["cache-control"].startswith("no-cache")
    raw = c.get(f"/static/index.html?v={server.APP_VERSION}")
    assert raw.headers["cache-control"] == "no-cache"


def test_index_references_every_local_asset_with_current_version(client):
    """immutable безопасен, только пока КАЖДЫЙ локальный ассет в index.html
    идёт с ?v=<текущая версия>: без версии новый деплой не сменит URL."""
    c, server = client
    html = c.get("/").text
    refs = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    assert refs, "в index.html нет ссылок на /static"
    for ref in refs:
        assert ref.endswith(f"?v={server.APP_VERSION}"), ref
