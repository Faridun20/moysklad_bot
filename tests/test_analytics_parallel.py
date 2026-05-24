"""
Аналитика: позиции отгрузок тянутся ПАРАЛЛЕЛЬНО (asyncio.gather), а не
последовательным циклом из 15 HTTP-вызовов. Мокаем транспорт (aioresponses) —
реально исполняется сборка URL/параметров get_shipments + get_shipment_positions
и агрегация top_products по ВСЕМ отгрузкам. См. CLAUDE.md: мок границы с внешним
миром, не своего кода.
"""

import asyncio
import re
from datetime import datetime

from aioresponses import aioresponses

import services.moysklad as moysklad
from services.moysklad import MS_BASE, get_sales_stats, invalidate_ms_cache


def _reset():
    """Чистое состояние МС-слоя: новая сессия, пустой TTL-кэш, закрытый breaker."""
    moysklad._session = None
    invalidate_ms_cache()
    moysklad._circuit.record_success()  # сбросить breaker, если открыл предыдущий тест


# entity/demand?<query>  — список отгрузок (get_shipments). Сразу за demand идёт "?".
_DEMAND_LIST_RE = re.compile(re.escape(f"{MS_BASE}/entity/demand") + r"\?.*")


def _positions_re(demand_id: str):
    # entity/demand/<id>/positions?<query> — позиции конкретной отгрузки.
    return re.compile(re.escape(f"{MS_BASE}/entity/demand/{demand_id}/positions") + r".*")


def test_sales_stats_aggregates_positions_across_shipments():
    _reset()
    since = datetime(2031, 3, 1, 0, 0, 0)
    until = datetime(2031, 3, 2, 0, 0, 0)

    shipments = {
        "rows": [
            {
                "meta": {"href": f"{MS_BASE}/entity/demand/DMD-A"},
                "sum": 100000,
                "agent": {"name": "Клиент-1"},
            },
            {
                "meta": {"href": f"{MS_BASE}/entity/demand/DMD-B"},
                "sum": 50000,
                "agent": {"name": "Клиент-2"},
            },
        ]
    }
    pos_a = [{"assortment": {"name": "Товар-X"}, "quantity": 2, "price": 5000}]
    pos_b = [
        {"assortment": {"name": "Товар-Y"}, "quantity": 1, "price": 8000},
        {"assortment": {"name": "Товар-X"}, "quantity": 1, "price": 5000},
    ]

    async def scenario():
        try:
            with aioresponses() as m:
                m.get(_DEMAND_LIST_RE, status=200, payload=shipments)
                m.get(_positions_re("DMD-A"), status=200, payload=pos_a)
                m.get(_positions_re("DMD-B"), status=200, payload=pos_b)
                return await get_sales_stats(since, until)
        finally:
            await moysklad.close_session()

    stats = asyncio.run(scenario())

    assert stats["count"] == 2
    assert stats["total"] == 150000  # суммы из самих отгрузок
    assert stats["clients"] == 2
    # Агрегация по ОБЕИМ отгрузкам — доказывает, что gather собрал все результаты,
    # а не только первый/последний.
    top = dict(stats["top_products"])
    assert top["Товар-X"]["sum"] == 15000  # 2*5000 (A) + 1*5000 (B)
    assert top["Товар-X"]["qty"] == 3
    assert top["Товар-Y"]["sum"] == 8000
    # Порядок — по убыванию суммы.
    assert stats["top_products"][0][0] == "Товар-X"


def test_sales_stats_skips_failed_position_fetch():
    """Ошибка по одной отгрузке (return_exceptions) не валит весь подсчёт —
    эквивалент прежнего per-item try/except: pass. 400 не ретраится и не трогает
    circuit breaker."""
    _reset()
    since = datetime(2031, 4, 1, 0, 0, 0)
    until = datetime(2031, 4, 2, 0, 0, 0)

    shipments = {
        "rows": [
            {
                "meta": {"href": f"{MS_BASE}/entity/demand/DMD-OK"},
                "sum": 30000,
                "agent": {"name": "Клиент-1"},
            },
            {
                "meta": {"href": f"{MS_BASE}/entity/demand/DMD-FAIL"},
                "sum": 20000,
                "agent": {"name": "Клиент-1"},
            },
        ]
    }
    pos_ok = [{"assortment": {"name": "Товар-Z"}, "quantity": 4, "price": 1000}]

    async def scenario():
        try:
            with aioresponses() as m:
                m.get(_DEMAND_LIST_RE, status=200, payload=shipments)
                m.get(_positions_re("DMD-OK"), status=200, payload=pos_ok)
                m.get(_positions_re("DMD-FAIL"), status=400)  # 400 → не ретраится
                return await get_sales_stats(since, until)
        finally:
            await moysklad.close_session()

    stats = asyncio.run(scenario())

    assert stats["count"] == 2
    assert stats["total"] == 50000  # суммы берутся из отгрузок, не из позиций
    top = dict(stats["top_products"])
    assert top["Товар-Z"]["sum"] == 4000  # учтена только успешная отгрузка
    assert len(top) == 1


# ─── pagination /positions (>100 строк) ───────────────────────────────────


def test_get_shipment_positions_paginates_over_100_rows():
    """До фикса get_shipment_positions делал один запрос limit=100 и молча
    терял хвост для demand'ов >100 строк (большие B2B-заказы). После фикса —
    цикл по offset; ВСЕ строки попадают в результат."""
    _reset()
    from services.moysklad import get_shipment_positions

    # Первая страница ровно 100 → loop должен запросить ещё (offset=100).
    page_1 = [
        {"assortment": {"name": f"Товар-{i}"}, "quantity": 1, "price": 100}
        for i in range(100)
    ]
    # Вторая страница 30 → меньше limit → break.
    page_2 = [
        {"assortment": {"name": f"Товар-{i}"}, "quantity": 1, "price": 100}
        for i in range(100, 130)
    ]

    async def scenario():
        try:
            with aioresponses() as m:
                # aioresponses matches по точному URL; query-params включаются.
                # Используем regex чтобы поймать обе страницы и отдать по очереди.
                m.get(_positions_re("DMD-BIG"), status=200, payload={"rows": page_1})
                m.get(_positions_re("DMD-BIG"), status=200, payload={"rows": page_2})
                return await get_shipment_positions("DMD-BIG")
        finally:
            await moysklad.close_session()

    rows = asyncio.run(scenario())
    assert len(rows) == 130
    # Сохранён порядок — первая страница вначале.
    assert rows[0]["assortment"]["name"] == "Товар-0"
    assert rows[-1]["assortment"]["name"] == "Товар-129"


def test_get_shipment_positions_single_short_page():
    """Demand с <100 позициями: один запрос, break сразу. Регресс-тест что
    pagination loop не делает лишних HTTP когда хвоста нет."""
    _reset()
    from services.moysklad import get_shipment_positions

    page = [{"assortment": {"name": "Solo"}, "quantity": 5, "price": 10}]

    async def scenario():
        try:
            with aioresponses() as m:
                m.get(_positions_re("DMD-SMALL"), status=200, payload={"rows": page})
                return await get_shipment_positions("DMD-SMALL")
        finally:
            await moysklad.close_session()

    rows = asyncio.run(scenario())
    assert len(rows) == 1
    assert rows[0]["assortment"]["name"] == "Solo"


# ─── cross-loop Semaphore safety (Fix #8) ─────────────────────────────────


def test_positions_semaphore_survives_multiple_asyncio_run_with_contention():
    """С capacity=8 и 9+ конкурентными вызовами семафор enqueue'ит waiter →
    bind к loop'у. До Fix #8 (module-level Semaphore(4)) второй asyncio.run
    падал бы 'bound to a different event loop'. После Fix #8 каждый loop
    получает свой Semaphore через _get_positions_semaphore() helper."""
    _reset()
    from services.moysklad import get_shipment_positions

    # 10 распинутых demand'ов — гарантированно >8 cap → waiter enqueue'ится.
    demand_ids = [f"DMD-CROSS-{i}" for i in range(10)]
    pos = [{"assortment": {"name": "X"}, "quantity": 1, "price": 1}]

    async def fan_out():
        # Для aioresponses: один регистр на URL — повторно вызвать в новом
        # `with aioresponses()` блоке. Каждый scenario ставит свежие моки.
        with aioresponses() as m:
            for did in demand_ids:
                m.get(_positions_re(did), status=200, payload={"rows": pos})
            return await asyncio.gather(
                *(get_shipment_positions(did) for did in demand_ids)
            )

    async def scenario():
        try:
            return await fan_out()
        finally:
            await moysklad.close_session()

    # Первый цикл — semaphore связывается с loop A.
    _reset()
    results_a = asyncio.run(scenario())
    assert len(results_a) == 10 and all(len(r) == 1 for r in results_a)

    # Второй цикл — НОВЫЙ loop B. Если бы _POSITIONS_CONCURRENCY был
    # module-level и забинден к loop A — здесь словили бы RuntimeError.
    _reset()
    results_b = asyncio.run(scenario())
    assert len(results_b) == 10 and all(len(r) == 1 for r in results_b)
