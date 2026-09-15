"""E2E: заголовки безопасности (финдинг #8, аудит) не ломают загрузку WebApp.

Юнит-проверки значений — `tests/test_security_headers.py` (TestClient).
Здесь — настоящий Chromium ПРИМЕНЯЕТ CSP по-настоящему: если style-src/
img-src/script-src собраны неверно, страница либо не отрисуется (инлайн-
style="…" из JS-шаблонов не покрасится), либо в консоли будут "Refused to…".
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_app_loads_and_navigates_with_real_security_headers(open_app, e2e):
    page = open_app(e2e.ids["boss"])

    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    # Перезагрузка — под живыми заголовками (не моком) — и снимок Response,
    # чтобы проверить сами значения заголовков той же навигации.
    resp = page.reload()
    page.wait_for_selector("#bottom-nav .nav-item, .error-card, .empty-state-title", state="attached")

    assert resp is not None and resp.ok
    headers = {k.lower(): v for k, v in resp.headers.items()}
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    csp = headers.get("content-security-policy", "")
    assert "telegram.org" in csp
    assert "frame-ancestors" in csp and "web.telegram.org" in csp
    assert "x-frame-options" not in headers

    # Навигация реально построилась (роль — boss) — CSP ничего не заблокировал
    # ни в telegram-web-app.js (перехвачен на network-уровне, но URL и,
    # значит, CSP-проверка — настоящие), ни в собственных скриптах/стилях.
    assert page.locator("#bottom-nav .nav-item").count() > 0

    # Надёжный сигнал про style-src/img-src/script-src сразу: Chromium пишет
    # "Refused to apply inline style..." / "Refused to load the script..." в
    # консоль КАЖДЫЙ раз, когда CSP реально блокирует ресурс — а именно этим
    # приложение и пользуется (style="…" из JS-шаблонов, data:/blob: фото).
    csp_violations = [m for m in console_errors if "Content Security Policy" in m or "Refused to" in m]
    assert csp_violations == [], csp_violations
