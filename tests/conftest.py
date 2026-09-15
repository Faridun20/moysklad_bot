"""
Pytest fixtures.

Что важно про тесты в этом проекте:
- Используем SQLite (DB_PATH в /tmp), Postgres в CI не нужен.
- TELEGRAM_TOKEN — заведомо фейковый, реального бота не дёргаем.
"""

import os

import pytest

# Заглушка секрета на случай запуска без env (локально / pre-commit hook):
# config.py требует TELEGRAM_TOKEN уже на импорте, а часть тест-модулей
# импортируют services на этапе сборки — до фикстур. setdefault не перетирает
# реальные значения из CI.
os.environ.setdefault("TELEGRAM_TOKEN", "0:fake-token-for-tests")


def _aioresponses_aiohttp_314_shim() -> None:
    """Научить aioresponses 0.7.9 собирать ответ под aiohttp 3.14.

    В aiohttp 3.14 у `ClientResponse.__init__` появился обязательный
    keyword-only `stream_writer`, а aioresponses (последний релиз 0.7.9) его
    не передаёт: ЛЮБОЙ замоканный ответ падает TypeError. Хуже того, падение
    тихое — `tg_send_message` best-effort глотает исключение, и тесты «запрос
    ушёл по нужному URL» оставались зелёными, хотя ответа код не получал.
    Исправление ждёт своего релиза (pnuckowski/aioresponses#288, #292) —
    делаем то же, что и оно: подставляем заглушку, от которой ответ читает
    только `output_size`.

    Самоустраняется: aiohttp без `stream_writer` или aioresponses, который
    уже передаёт его сам, — ничего не трогаем. Выйдет исправленный релиз —
    функцию можно удалить вместе с вызовом.

    Ограничение (то же, что в 0.7.9 на aiohttp 3.14 без #292): тело мока
    больше 64 КБ упрётся в flow control StreamReader. Наши моки — килобайты.
    """
    import inspect
    from unittest.mock import Mock

    try:
        import aioresponses.core as ar_core
        from aiohttp import ClientResponse
    except ImportError:
        return
    if "stream_writer" not in inspect.signature(ClientResponse.__init__).parameters:
        return
    try:
        if "stream_writer" in inspect.getsource(ar_core):
            return
    except (OSError, TypeError):
        pass

    class _ClientResponse(ClientResponse):
        def __init__(self, *args, stream_writer=None, **kwargs):
            if stream_writer is None:
                stream_writer = Mock(output_size=0)
            super().__init__(*args, stream_writer=stream_writer, **kwargs)

    # _build_response берёт имя из глобалов модуля в момент вызова, поэтому
    # порядок импорта тест-модулей не важен.
    ar_core.ClientResponse = _ClientResponse  # type: ignore[misc]


_aioresponses_aiohttp_314_shim()


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """Свежая SQLite-БД на каждый тест, чтобы тесты не влияли друг на друга.

    Возвращает модуль services.database с инициализированной схемой.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Telegram-токен заглушка — нужен для импорта config
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")

    # Перезагружаем модули чтобы перечитали env var DB_PATH
    import importlib
    import config
    import services.database as db

    importlib.reload(config)
    importlib.reload(db)

    db.init_db()
    # Склад по умолчанию. На проде его сеет `run_backfills` (через
    # `tasks/migrate`), в тестах — фикстура: без единой строки в `warehouses`
    # любая накладная отвергается «склад не найден», и половина сценариев
    # падала бы на инфраструктуре, а не на проверяемом поведении.
    db.seed_warehouses()
    return db


# ─── Карты и счета «куда поступили» (services/pay_accounts.py) ───────────────
# Карта и перечисление в разбивке оплаты обязаны указать запись справочника.
# Тесты, которым неважно, ЧЬЯ карта, берут эти две: заводятся через сервис
# (тот же путь, что у формы), повтор отдаёт уже заведённую.

TEST_CARD = {"kind": "card", "holder": "Фаридун М.", "card_last4": "1234", "bank": "Kapitalbank"}
TEST_BANK = {"kind": "bank", "holder": "ООО Farid Impeks", "account_number": "20208840900112236789",
             "bank": "Kapitalbank", "mfo": "01158"}


def pay_account_id(kind: str = "card", run=None, **override) -> int:
    import asyncio

    from services import pay_accounts

    data = {**(TEST_CARD if kind == "card" else TEST_BANK), **override}
    res = (run or asyncio.run)(pay_accounts.create_account(pay_accounts.Actor(0, "Tests", "boss"), data))
    return int(res["account"]["id"])


def with_pay_accounts(parts: list[dict], run=None) -> list[dict]:
    """Строкам карта/перечисление без `account_id` — тестовая карта/счёт."""
    out = []
    for p in parts:
        if p.get("method") in ("card", "bank") and not p.get("account_id"):
            p = {**p, "account_id": pay_account_id(p["method"], run)}
        out.append(p)
    return out
