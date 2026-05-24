"""
Тесты способов запуска WebApp в Telegram — Menu Button и Reply Keyboard.

Покрываем pure-функции (без сети, без БД):
  * webapp_reply_keyboard(url) → ReplyKeyboardMarkup с web_app
  * webapp_reply_keyboard(None/'') → ReplyKeyboardRemove
  * set_global_menu_button — sync вызовы через FakeBot
"""

import asyncio

import pytest
from aiogram.types import (
    KeyboardButton,
    MenuButtonDefault,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)


# ─── Reply Keyboard ─────────────────────────────────────────────────────────


def test_reply_keyboard_with_url_returns_markup():
    from handlers.start import webapp_reply_keyboard

    kb = webapp_reply_keyboard("https://example.com")
    assert isinstance(kb, ReplyKeyboardMarkup)
    # is_persistent + resize_keyboard включены
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True
    # Один ряд с одной кнопкой «🌐 Открыть»
    assert len(kb.keyboard) == 1
    assert len(kb.keyboard[0]) == 1
    btn = kb.keyboard[0][0]
    assert isinstance(btn, KeyboardButton)
    assert "Открыть" in btn.text
    assert btn.web_app is not None
    assert btn.web_app.url == "https://example.com"


def test_reply_keyboard_none_returns_remove():
    """url=None → ReplyKeyboardRemove (убрать кнопку, ведущую в никуда)."""
    from handlers.start import webapp_reply_keyboard

    assert isinstance(webapp_reply_keyboard(None), ReplyKeyboardRemove)


def test_reply_keyboard_empty_returns_remove():
    """url='' тоже трактуется как «нет URL»."""
    from handlers.start import webapp_reply_keyboard

    assert isinstance(webapp_reply_keyboard(""), ReplyKeyboardRemove)


# ─── Global Menu Button ─────────────────────────────────────────────────────


class _FakeBot:
    """Минимальный stub для set_chat_menu_button. Записывает аргументы для assert'ов."""

    def __init__(self):
        self.calls: list[dict] = []

    async def set_chat_menu_button(self, **kwargs):
        self.calls.append(kwargs)


def test_set_global_menu_button_with_url(monkeypatch):
    """С WEBAPP_URL → ставит MenuButtonWebApp."""
    import bot as bot_module

    monkeypatch.setattr(bot_module, "WEBAPP_URL", "https://my.app")
    fake = _FakeBot()
    asyncio.run(bot_module.set_global_menu_button(fake))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert "menu_button" in call
    assert isinstance(call["menu_button"], MenuButtonWebApp)
    assert call["menu_button"].text == "Открыть"
    assert call["menu_button"].web_app.url == "https://my.app"


def test_set_global_menu_button_without_url_falls_back_to_default(monkeypatch):
    """Без WEBAPP_URL → MenuButtonDefault (сбрасывает старую WebApp-кнопку)."""
    import bot as bot_module

    monkeypatch.setattr(bot_module, "WEBAPP_URL", "")
    fake = _FakeBot()
    asyncio.run(bot_module.set_global_menu_button(fake))
    assert len(fake.calls) == 1
    assert isinstance(fake.calls[0]["menu_button"], MenuButtonDefault)


def test_set_global_menu_button_swallows_telegram_exception(monkeypatch, caplog):
    """Если Telegram вернёт ошибку — функция не должна ронять старт."""
    import bot as bot_module
    import logging

    monkeypatch.setattr(bot_module, "WEBAPP_URL", "https://my.app")

    class _ExplosiveBot:
        async def set_chat_menu_button(self, **kwargs):
            raise RuntimeError("Telegram API down")

    caplog.set_level(logging.WARNING)
    # Не должна raise — функция глотает exception и логирует warning
    asyncio.run(bot_module.set_global_menu_button(_ExplosiveBot()))
    # Warning записан
    assert any("Не удалось установить Menu Button" in r.message for r in caplog.records)


# ─── HTTPS validation в config ──────────────────────────────────────────────


def test_config_warns_on_non_https_webapp_url(monkeypatch, caplog):
    """WEBAPP_URL без https → warning при импорте config (Telegram молча
    игнорирует http:// в WebAppInfo)."""
    import importlib
    import logging

    monkeypatch.setenv("WEBAPP_URL", "http://insecure.example.com")
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake")
    monkeypatch.setenv("MS_TOKEN", "fake")
    monkeypatch.delenv("ADMIN_IDS", raising=False)
    monkeypatch.delenv("BOSS_IDS", raising=False)
    caplog.set_level(logging.WARNING, logger="config")
    import config

    importlib.reload(config)
    msgs = [r.message for r in caplog.records if r.name == "config"]
    assert any("WEBAPP_URL" in m and "HTTPS" in m for m in msgs), msgs


def test_config_allows_https_webapp_url(monkeypatch, caplog):
    import importlib
    import logging

    monkeypatch.setenv("WEBAPP_URL", "https://webapp.example.com")
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake")
    monkeypatch.setenv("MS_TOKEN", "fake")
    caplog.set_level(logging.WARNING, logger="config")
    import config

    importlib.reload(config)
    msgs = [r.message for r in caplog.records if r.name == "config"]
    assert not any("WEBAPP_URL" in m and "HTTPS" in m for m in msgs)


def test_config_allows_localhost_http(monkeypatch, caplog):
    """http://localhost / 127.0.0.1 — для local dev OK, warning не нужен."""
    import importlib
    import logging

    monkeypatch.setenv("WEBAPP_URL", "http://localhost:8080")
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake")
    monkeypatch.setenv("MS_TOKEN", "fake")
    caplog.set_level(logging.WARNING, logger="config")
    import config

    importlib.reload(config)
    msgs = [r.message for r in caplog.records if r.name == "config"]
    assert not any("WEBAPP_URL" in m and "HTTPS" in m for m in msgs)


# Cleanup: после возни с config.reload вернуть в чистое состояние,
# чтобы соседние тесты не подцепили мусорный env.
@pytest.fixture(autouse=True)
def _restore_config(monkeypatch):
    yield
    import importlib

    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")
    monkeypatch.setenv("MS_TOKEN", "fake-ms-token")
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    import config

    importlib.reload(config)
