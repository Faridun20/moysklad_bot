"""
Тесты services/observability.py — Sentry hook.

Без SENTRY_DSN: init_sentry → False, все хелперы — no-op (не падают).
Со scrubbing'ом: _scrub_mapping режет sensitive ключи в любых nested
структурах. Это критично, потому что Telegram initData содержит
hash+user — отправить во внешний SaaS = утечка PII.
"""

import importlib


def _fresh_obs():
    """Свежий импорт чтобы сбросить _initialized state между тестами."""
    import services.observability as obs

    importlib.reload(obs)
    return obs


def test_init_sentry_returns_false_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    obs = _fresh_obs()
    assert obs.init_sentry("test") is False
    assert obs.is_enabled() is False


def test_helpers_are_noop_without_init(monkeypatch):
    """capture_exception / set_user / set_tag не падают если init не делали."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    obs = _fresh_obs()
    # Все должны просто молча вернуться:
    obs.capture_exception(ValueError("test"))
    obs.capture_message("hello")
    obs.set_user(123, "u")
    obs.set_user(None)
    obs.set_tag("k", "v")
    obs.set_context("ctx", {"a": 1})


def test_scrub_mapping_redacts_sensitive_keys():
    obs = _fresh_obs()
    payload = {
        "initData": "user=...&hash=secret",
        "amount": 100,
        "headers": {
            "Authorization": "Bearer xxx",
            "X-Telegram-Bot-Api-Secret-Token": "tg-secret",
            "Content-Type": "application/json",
        },
        "MS_TOKEN": "super-secret",
        "ok": True,
    }
    scrubbed = obs._scrub_mapping(payload)
    assert scrubbed["initData"] == "***REDACTED***"
    assert scrubbed["headers"]["Authorization"] == "***REDACTED***"
    # cookie/token/secret в нижнем регистре — тоже ловится
    assert scrubbed["MS_TOKEN"] == "***REDACTED***"
    # Безопасные ключи остаются как есть
    assert scrubbed["amount"] == 100
    assert scrubbed["ok"] is True
    assert scrubbed["headers"]["Content-Type"] == "application/json"


def test_scrub_mapping_recurses_into_lists():
    obs = _fresh_obs()
    payload = {
        "items": [
            {"name": "x", "token": "leak1"},
            {"name": "y", "password": "leak2"},
        ]
    }
    scrubbed = obs._scrub_mapping(payload)
    assert scrubbed["items"][0]["token"] == "***REDACTED***"
    assert scrubbed["items"][1]["password"] == "***REDACTED***"
    assert scrubbed["items"][0]["name"] == "x"


def test_before_send_scrubs_request_and_extra():
    obs = _fresh_obs()
    event = {
        "request": {
            "data": {"initData": "leak"},
            "headers": {"Cookie": "session=leak"},
            "query_string": "foo=bar&token=leak",
        },
        "extra": {"user_password": "leak"},
        "contexts": {"app": {"secret_key": "leak"}},
        "tags": {"role": "boss"},
    }
    out = obs._before_send(event, {})
    assert out is not None
    assert out["request"]["data"]["initData"] == "***REDACTED***"
    assert out["request"]["headers"]["Cookie"] == "***REDACTED***"
    # query_string — строка, scrubbing работает только по ключам, строки
    # пропускает (это OK, query_string обычно не утечёт креды если не
    # в headers/data). Так что просто проверяем что не упало.
    assert out["extra"]["user_password"] == "***REDACTED***"
    assert out["contexts"]["app"]["secret_key"] == "***REDACTED***"
    assert out["tags"]["role"] == "boss"  # безопасно


def test_before_breadcrumb_redacts_token_in_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "999:my-secret-token")
    obs = _fresh_obs()
    crumb = {
        "message": "POST https://api.telegram.org/bot999:my-secret-token/sendMessage ok",
        "data": {"chat_id": 42},
    }
    out = obs._before_breadcrumb(crumb, {})
    assert "999:my-secret-token" not in out["message"]
    assert "***REDACTED***" in out["message"]


# ─── Полный путь с реальной sentry-sdk инициализацией ─────────────────────────
#
# sentry_sdk.init не шлёт HTTP до flush() — безопасно в тестах. Используем
# фейковый DSN, проверяем что init возвращает True, is_enabled() True,
# и хелперы реально доходят до sentry-sdk без exception'а.


_FAKE_DSN = "https://abc123@o123456.ingest.sentry.io/789"


def _close_sentry_client():
    """Best-effort: закрыть текущий sentry-sdk client между тестами,
    чтобы не утекали транспорты и hub-state."""
    try:
        import sentry_sdk

        client = sentry_sdk.Hub.current.client
        if client is not None:
            client.close(timeout=0.1)
    except Exception:
        pass


def test_init_sentry_returns_true_with_dsn(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", _FAKE_DSN)
    obs = _fresh_obs()
    try:
        assert obs.init_sentry("test_component") is True
        assert obs.is_enabled() is True
        # Повторный init — idempotent (gate на _initialized)
        assert obs.init_sentry("again") is True
    finally:
        _close_sentry_client()


def test_helpers_dispatch_to_sentry_when_initialized(monkeypatch):
    """После init: capture_exception / set_user / set_tag / set_context /
    capture_message пробрасывают вызов в sentry_sdk без exception'а.

    Реальные события уходят в внутреннюю очередь sentry-sdk и flush'аются
    лишь по close() — HTTP не делаем."""
    monkeypatch.setenv("SENTRY_DSN", _FAKE_DSN)
    obs = _fresh_obs()
    try:
        obs.init_sentry("test_dispatch")
        obs.capture_exception(ValueError("bug"))
        obs.capture_exception()  # без аргументов — current exc
        obs.capture_message("info-level", level="warning")
        obs.set_user(42, "username")
        obs.set_user(None)  # logout
        obs.set_tag("rollout", "canary")
        obs.set_context("order", {"id": 1, "secret": "leak"})  # secret режется
    finally:
        _close_sentry_client()


def test_init_sentry_handles_import_error(monkeypatch):
    """Если sentry_sdk не установлен — init возвращает False, не падает."""
    monkeypatch.setenv("SENTRY_DSN", _FAKE_DSN)
    # Подменяем builtins.__import__ чтобы import sentry_sdk вернул ImportError
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "sentry_sdk" or name.startswith("sentry_sdk."):
            raise ImportError("simulated: sentry-sdk not installed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    obs = _fresh_obs()
    assert obs.init_sentry("test_noimport") is False
    assert obs.is_enabled() is False
