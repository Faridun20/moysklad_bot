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
