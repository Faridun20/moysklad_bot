"""
Тесты СЕТЕВОЙ ГРАНИЦЫ для services.notifier.tg_send_message.

Зачем отдельно: остальные тесты мокают саму `tg_send_message` целиком —
поэтому сборка URL/payload внутри неё никогда не исполнялась, и баг
«path в aiohttp base_url» (все уведомления молча падали) пережил CI.

Здесь мокаем ТРАНСПОРТ (aioresponses перехватывает aiohttp на уровне
запроса), а не наш код. Реально исполняется get_tg_session() + сборка
URL и тела. Регресс-гард `test_base_url_has_no_path` краснеет, если
кто-то вернёт path в base_url.
"""

import asyncio

from aioresponses import aioresponses
from yarl import URL

import services.notifier as notifier


def _run(coro):
    """Запустить корутину в свежем loop'е и аккуратно закрыть сессию.

    tg_send_message держит глобальный ClientSession, привязанный к loop'у;
    сбрасываем его до и после, чтобы тесты не делили сессию между loop'ами.
    """

    async def _wrapped():
        notifier._tg_session = None
        try:
            return await coro()
        finally:
            await notifier.close_tg_session()

    return asyncio.run(_wrapped())


def _expected_url() -> str:
    return f"https://api.telegram.org/bot{notifier.TELEGRAM_TOKEN}/sendMessage"


def test_tg_send_message_hits_correct_url_and_payload():
    async def scenario():
        with aioresponses() as m:
            m.post(_expected_url(), payload={"ok": True})
            await notifier.tg_send_message(12345, "привет")

            # Запрос реально ушёл по корректному URL (на старом base_url
            # с path он падал ещё до сети).
            key = ("POST", URL(_expected_url()))
            assert key in m.requests, f"запрос не ушёл; requests={list(m.requests)}"
            call = m.requests[key][0]
            payload = call.kwargs["json"]
            assert payload["chat_id"] == 12345
            assert payload["text"] == "привет"
            assert payload["parse_mode"] == "HTML"
            assert "reply_markup" not in payload

    _run(scenario)


def test_tg_send_message_includes_reply_markup():
    async def scenario():
        with aioresponses() as m:
            m.post(_expected_url(), payload={"ok": True})
            kb = {"inline_keyboard": [[{"text": "ok", "callback_data": "x"}]]}
            await notifier.tg_send_message(7, "t", reply_markup=kb)

            call = m.requests[("POST", URL(_expected_url()))][0]
            assert call.kwargs["json"]["reply_markup"] == kb

    _run(scenario)


def test_tg_send_message_swallows_http_error():
    """best-effort: даже при 400 от Telegram не пробрасываем исключение."""

    async def scenario():
        with aioresponses() as m:
            m.post(_expected_url(), status=400, payload={"ok": False})
            # Не должно бросить — иначе единичный сбой уронил бы хендлер.
            await notifier.tg_send_message(1, "t")

    _run(scenario)


def test_consecutive_failures_escalate_to_error(caplog):
    """N неудач подряд → logger.error (массовый отказ виден сразу)."""
    import logging

    notifier._send_fail_streak = 0
    n = notifier.SEND_FAIL_ALERT_AFTER

    async def scenario():
        with aioresponses() as m:
            for _ in range(n):
                m.post(_expected_url(), status=500, payload={"ok": False})
            with caplog.at_level(logging.WARNING, logger="services.notifier"):
                for _ in range(n):
                    await notifier.tg_send_message(1, "t")

    _run(scenario)
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "после серии неудач не было error-эскалации"
    )


def test_success_resets_failure_streak():
    """Успешная отправка обнуляет счётчик подряд-неудач."""
    notifier._send_fail_streak = 3

    async def scenario():
        with aioresponses() as m:
            m.post(_expected_url(), payload={"ok": True})
            await notifier.tg_send_message(1, "t")

    _run(scenario)
    assert notifier._send_fail_streak == 0


def test_base_url_has_no_path():
    """Регресс-гард на исходный баг: base_url у сессии — только origin.

    aiohttp запрещает path-часть в base_url; если она вернётся, все
    уведомления снова начнут молча падать. Сетевого вызова тут нет.
    """

    async def scenario():
        sess = await notifier.get_tg_session()
        assert sess._base_url is not None
        assert sess._base_url.path in ("", "/"), (
            f"base_url не должен содержать path: {sess._base_url}"
        )

    _run(scenario)
