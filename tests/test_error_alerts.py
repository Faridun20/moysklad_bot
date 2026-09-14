"""Глобальные обработчики ошибок (п.11 аудита).

Проверяем: WebApp не отдаёт клиенту `str(e)`, пишет трассу и шлёт алерт
админам; бот отвечает человеку вместо тишины и не отвечает в business-чат;
алерты дросселируются. Отправка в Telegram подменена на границе
(`notifier.tg_send_message`).
"""

import asyncio
import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def sent(monkeypatch):
    import config
    from services import error_alerts, notifier

    error_alerts.reset()
    monkeypatch.setattr(config, "ADMIN_IDS", [7001, 7002], raising=False)
    box: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, **kw):
        box.append((chat_id, text))
        return True

    monkeypatch.setattr(notifier, "tg_send_message", fake_send)
    yield box
    error_alerts.reset()


@pytest.fixture
def boom_path():
    """Временная ручка, которая падает. Убирается после теста: иначе
    test_security_regression, перебирающий все /api-роуты, получил бы 500."""
    import webapp.server as server
    from fastapi import Request

    path = "/api/__boom_test__"

    @server.app.post(path)
    async def _boom(request: Request):  # noqa: ARG001
        from config import TELEGRAM_TOKEN

        raise RuntimeError(f"SELECT secret FROM users WHERE token='{TELEGRAM_TOKEN}'")

    yield path
    server.app.router.routes[:] = [
        r for r in server.app.router.routes if getattr(r, "path", None) != path
    ]


def test_webapp_hides_exception_text_and_alerts_admins(isolated_db, sent, caplog, boom_path):
    import webapp.server as server
    from utils import background

    from config import TELEGRAM_TOKEN

    path = boom_path
    with TestClient(server.app, raise_server_exceptions=False) as client, caplog.at_level(logging.ERROR):
        r = client.post(path, json={})

        # Алерт уходит фоном — дожидаемся его в петле клиента.
        async def _drain():
            await asyncio.gather(*background.pending())

        client.portal.call(_drain)  # type: ignore[union-attr]
    assert r.status_code == 500
    assert "SELECT" not in r.text and "secret" not in r.text
    assert r.json()["detail"].startswith("Что-то пошло не так")

    assert any(rec.exc_info and "SELECT secret" in str(rec.exc_info[1]) for rec in caplog.records), \
        "трасса обязана попасть в лог"
    assert sorted(chat for chat, _ in sent) == [7001, 7002]
    text = sent[0][1]
    assert "RuntimeError" in text and path in text
    assert TELEGRAM_TOKEN not in text, "токен из текста ошибки не утекает в чат"


def test_alerts_are_throttled_per_error(sent, monkeypatch):
    from services import error_alerts

    def boom():
        raise ValueError("сломалось")

    clock = [1000.0]
    monkeypatch.setattr(error_alerts.time, "monotonic", lambda: clock[0])

    async def fire():
        try:
            boom()
        except ValueError as e:
            return await error_alerts.report_exception(e, where="bot message", user_id=5)

    assert asyncio.run(fire()) is True
    for _ in range(5):
        clock[0] += 10
        assert asyncio.run(fire()) is False
    assert len(sent) == 2  # по одному на админа, повторы проглочены

    clock[0] += error_alerts.ALERT_INTERVAL_SEC
    assert asyncio.run(fire()) is True
    assert len(sent) == 4
    assert "Повторялась ещё 5 раз" in sent[-1][1]

    # Другая ошибка из другого места дросселем первой не глушится.
    async def other():
        try:
            raise KeyError("x")
        except KeyError as e:
            return await error_alerts.report_exception(e, where="webapp POST /api/x")

    assert asyncio.run(other()) is True


class _User:
    id = 42


class _Msg:
    def __init__(self):
        self.from_user = _User()
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


class _Cb:
    def __init__(self):
        self.from_user = _User()
        self.alerts = []

    async def answer(self, text="", **kw):
        self.alerts.append((text, kw))


class _Update:
    def __init__(self, message=None, callback_query=None, business_message=None):
        self.message = message
        self.callback_query = callback_query
        self.business_message = business_message
        self.edited_message = None


class _Event:
    def __init__(self, update, exc):
        self.update = update
        self.exception = exc


def _exc():
    try:
        raise RuntimeError("хендлер упал")
    except RuntimeError as e:
        return e


def test_bot_error_answers_message_and_callback(sent):
    from handlers.errors import on_error
    from services import error_alerts

    msg = _Msg()
    assert asyncio.run(on_error(_Event(_Update(message=msg), _exc()))) is True
    assert msg.answers and msg.answers[0].startswith("Что-то пошло не так")

    error_alerts.reset()
    cb = _Cb()
    asyncio.run(on_error(_Event(_Update(callback_query=cb), _exc())))
    assert cb.alerts and cb.alerts[0][1].get("show_alert") is True
    assert any("bot" in text for _, text in sent)


def test_bot_error_never_replies_into_business_chat(sent):
    """Business-апдейт — переписка менеджера с клиентом: туда бот не пишет."""
    from handlers.errors import on_error

    business = _Msg()
    asyncio.run(on_error(_Event(_Update(business_message=business), _exc())))
    assert business.answers == []
    assert sent, "админам всё равно сообщили"


def test_dispatcher_has_error_handler():
    """register_routers (его зовут и polling-, и webhook-путь) вешает dp.errors.
    Настоящий Dispatcher не берём: роутеры модулей цепляются к нему навсегда,
    и соседние тесты уже не смогут собрать свой."""
    import bot as bot_module
    from handlers.errors import on_error

    class _Observer:
        def __init__(self):
            self.handlers = []

        def register(self, fn):
            self.handlers.append(fn)

    class _Dp:
        def __init__(self):
            self.errors = _Observer()
            self.routers = []

        def include_router(self, r):
            self.routers.append(r)

    dp = _Dp()
    bot_module.register_routers(dp)
    assert dp.errors.handlers == [on_error]
    assert dp.routers
