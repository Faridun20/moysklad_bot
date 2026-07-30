"""
MS-4 (волна 7) — реконсиляция спрашивает МойСклад батчами.

Было: GET на каждую строку выборки — 1500 запросов в час, 99,9 % из которых
отвечали «документ на месте». Стало: один списочный запрос на 100 id
(`filter=id=…;id=…`), пришедшие в rows живы, остальные удалены.

Цена ошибки здесь максимальная: если счесть «не смогли спросить» за «всё
удалено», реконсиляция отменит живые заказы разом. Поэтому половина тестов —
про предохранители, а не про счастливый путь.

Мокаем ТРАНСПОРТ (aioresponses): реально исполняется сборка URL и разбор ответа.
"""

import asyncio
import re

import pytest
from aioresponses import CallbackResult, aioresponses

import services.moysklad as ms
from tasks import run_ms_reconcile as rec

_ANY_CO = re.compile(r".*entity/customerorder.*")


def _run(coro_factory):
    async def scenario():
        ms._session = None
        try:
            return await coro_factory()
        finally:
            await ms.close_session()

    return asyncio.run(scenario())


def _ids(n: int) -> list[str]:
    return [f"uuid-{i:04d}" for i in range(n)]


# ─── Батчинг ──────────────────────────────────────────────────────────────────


def _echo_alive(calls: list[dict]):
    """Мок МС: отвечает «живы все, о ком спросили», попутно записывая запросы."""

    def cb(url, **kwargs):
        params = kwargs.get("params") or {}
        calls.append(params)
        asked = [a for a in (params.get("filter") or "").split(";") if a]
        return CallbackResult(
            status=200, payload={"rows": [{"id": a.split("=", 1)[1]} for a in asked]}
        )

    return cb


def test_250_ids_take_three_requests():
    """limit=100 — максимум страницы у МойСклад, значит 250 id это ровно три
    запроса вместо двухсот пятидесяти."""
    calls: list[dict] = []

    with aioresponses() as m:
        m.get(_ANY_CO, callback=_echo_alive(calls), repeat=True)
        alive = _run(lambda: rec._alive_ids("customerorder", _ids(250)))

    assert alive is not None and len(alive) == 250
    assert len(calls) == 3
    assert len(calls[0]["filter"].split(";")) == 100


def test_missing_id_is_reported_as_deleted():
    ids = ["alive-1", "gone-2", "alive-3"]

    with aioresponses() as m:
        m.get(_ANY_CO, payload={"rows": [{"id": "alive-1"}, {"id": "alive-3"}]})
        alive = _run(lambda: rec._alive_ids("customerorder", ids))

    assert alive == {"alive-1", "alive-3"}
    assert "gone-2" not in alive


def test_filter_uses_repeated_id_equals():
    """Повторное «=» по одному полю у МойСклад означает ИЛИ — на этом и держится
    весь батчинг."""
    captured: dict = {}

    def cb(url, **kwargs):
        captured.update(kwargs.get("params") or {})
        return CallbackResult(status=200, payload={"rows": []})

    with aioresponses() as m:
        m.get(_ANY_CO, callback=cb)
        _run(lambda: rec._alive_ids("customerorder", ["a", "b"]))

    assert captured["filter"] == "id=a;id=b"
    assert captured["limit"] == rec._BATCH_SIZE


# ─── Предохранители ───────────────────────────────────────────────────────────


def test_network_error_returns_none_not_empty():
    """«Не смогли спросить» ≠ «ничего не осталось». Пустое множество здесь
    означало бы «удалить всё»."""
    with aioresponses() as m:
        m.get(_ANY_CO, exception=OSError("сеть недоступна"))
        assert _run(lambda: rec._alive_ids("customerorder", _ids(3))) is None


def test_empty_answer_on_large_batch_is_treated_as_broken_filter():
    """0 строк на батч из 100 id — это сломанный фильтр или отозванные права,
    а не одновременное удаление сотни документов."""
    with aioresponses() as m:
        m.get(_ANY_CO, payload={"rows": []})
        assert _run(lambda: rec._alive_ids("customerorder", _ids(100))) is None


def test_empty_answer_on_tiny_batch_is_honest_deletion():
    """А вот один-два документа реально могли удалить — тут пустой ответ
    правдоподобен, иначе реконсиляция никогда не сработает на малом парке."""
    with aioresponses() as m:
        m.get(_ANY_CO, payload={"rows": []})
        assert _run(lambda: rec._alive_ids("customerorder", ["only-one"])) == set()


def test_truncated_page_is_inconclusive():
    """meta.size больше числа строк — ответ урезан пагинацией, и недостающие
    живые документы выглядели бы удалёнными."""
    with aioresponses() as m:
        m.get(_ANY_CO, payload={"rows": [{"id": "a"}], "meta": {"size": 5}})
        assert _run(lambda: rec._alive_ids("customerorder", _ids(5))) is None


def test_answer_without_rows_is_inconclusive():
    with aioresponses() as m:
        m.get(_ANY_CO, payload={"meta": {"size": 0}})
        assert _run(lambda: rec._alive_ids("customerorder", _ids(6))) is None


# ─── Проход целиком ───────────────────────────────────────────────────────────


def test_reconcile_flags_only_missing_documents(isolated_db, monkeypatch):
    """Сквозной проход: удалённый заказ уходит в обработчик, живой — нет."""
    db = isolated_db
    alive_order = db.create_order(1, "M", "")
    gone_order = db.create_order(1, "M", "")
    db.set_order_ms_customerorder_id(alive_order, "co-alive")
    db.set_order_ms_customerorder_id(gone_order, "co-gone")

    handled: list[int] = []

    async def _fake_delete(order, co_id):
        handled.append(order["id"])

    monkeypatch.setattr(
        "services.ms_sync_handler.apply_ms_customerorder_delete", _fake_delete
    )

    async def scenario():
        ms._session = None
        try:
            with aioresponses() as m:
                m.get(_ANY_CO, payload={"rows": [{"id": "co-alive"}]}, repeat=True)
                m.get(re.compile(r".*entity/demand.*"), payload={"rows": []}, repeat=True)
                m.get(re.compile(r".*entity/paymentin.*"), payload={"rows": []}, repeat=True)
                return await rec.main()
        finally:
            await ms.close_session()

    code = asyncio.run(scenario())
    assert code == 0
    assert handled == [gone_order]


@pytest.mark.parametrize("size", [1, 99, 100, 101, 200])
def test_batch_never_exceeds_page_limit(size):
    """URL с id длиннее 8 КБ отдаёт HTTP 414 — размер батча не должен зависеть
    от размера выборки."""
    chunks: list[int] = []

    def cb(url, **kwargs):
        chunks.append(len((kwargs.get("params") or {}).get("filter", "").split(";")))
        return CallbackResult(status=200, payload={"rows": [{"id": "x"}]})

    with aioresponses() as m:
        m.get(_ANY_CO, callback=cb, repeat=True)
        _run(lambda: rec._alive_ids("customerorder", _ids(size)))

    assert max(chunks) <= rec._BATCH_SIZE
