"""
Тесты HMAC-валидации Telegram WebApp initData.

Что важно проверить:
  - Корректная подпись принимается
  - Битая подпись отвергается (constant-time compare)
  - Просроченный auth_date отвергается (защита от replay)
  - Пустой / битый input не падает
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode


# Реальный fake-токен от @BotFather имеет формат `123:AAA...`
FAKE_TOKEN = "12345:AAFakeTokenForTests-_-aBcDeF123456789-_-x"


def _make_init_data(token: str, user: dict, auth_date: int | None = None) -> str:
    """Сгенерировать корректный initData с правильным HMAC.

    Имитирует то, что Telegram отправляет в Telegram.WebApp.initData.
    """
    if auth_date is None:
        auth_date = int(time.time())
    fields = {
        "auth_date": str(auth_date),
        "user": json.dumps(user, separators=(",", ":")),
        "query_id": "AAH-test",
    }
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(fields.items())
    )
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=token.encode(),
        digestmod=hashlib.sha256,
    ).digest()
    real_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    fields["hash"] = real_hash
    return urlencode(fields)


def _reload_auth(monkeypatch):
    """Перезагрузить webapp.auth с фейковым TELEGRAM_TOKEN."""
    monkeypatch.setenv("TELEGRAM_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("MS_TOKEN", "fake-ms")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import importlib
    import config
    import webapp.auth as auth
    importlib.reload(config)
    importlib.reload(auth)
    return auth


def test_valid_signature_accepted(monkeypatch):
    auth = _reload_auth(monkeypatch)
    user = {"id": 12345, "first_name": "Test", "username": "tester"}
    init_data = _make_init_data(FAKE_TOKEN, user)
    result = auth.verify_init_data(init_data)
    assert result is not None
    assert result["id"] == 12345
    assert result["first_name"] == "Test"


def test_invalid_signature_rejected(monkeypatch):
    auth = _reload_auth(monkeypatch)
    user = {"id": 12345}
    init_data = _make_init_data(FAKE_TOKEN, user)
    # Портим hash — даже один символ должен отвергаться
    broken = init_data.replace("hash=", "hash=00")[:len(init_data)]
    result = auth.verify_init_data(broken)
    assert result is None


def test_expired_init_data_rejected(monkeypatch):
    auth = _reload_auth(monkeypatch)
    user = {"id": 12345}
    # auth_date 2 дня назад → старше MAX_INIT_DATA_AGE (24h)
    old_date = int(time.time()) - 2 * 86400
    init_data = _make_init_data(FAKE_TOKEN, user, auth_date=old_date)
    result = auth.verify_init_data(init_data)
    assert result is None


def test_empty_input_returns_none(monkeypatch):
    auth = _reload_auth(monkeypatch)
    assert auth.verify_init_data("") is None
    assert auth.verify_init_data(None) is None


def test_missing_hash_rejected(monkeypatch):
    auth = _reload_auth(monkeypatch)
    # initData без hash-поля
    init_data = urlencode({"auth_date": str(int(time.time())), "user": "{}"})
    assert auth.verify_init_data(init_data) is None


def test_signature_for_different_token_rejected(monkeypatch):
    auth = _reload_auth(monkeypatch)
    user = {"id": 12345}
    # initData подписан другим токеном → должен отвергнуться
    init_data = _make_init_data("99999:DifferentToken", user)
    result = auth.verify_init_data(init_data)
    assert result is None


def test_malformed_user_json_rejected(monkeypatch):
    auth = _reload_auth(monkeypatch)
    # Соберём правильный hash для битого user JSON
    auth_date = str(int(time.time()))
    bad_user = "{not-valid-json"
    fields = {"auth_date": auth_date, "user": bad_user}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hmac.new(
        b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256,
    ).digest()
    real_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256,
    ).hexdigest()
    fields["hash"] = real_hash
    init_data = urlencode(fields)
    # Подпись корректная, но user — невалидный JSON. Должен вернуть None.
    assert auth.verify_init_data(init_data) is None
