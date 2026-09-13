"""Бенчмарки чистых функций и денежного ядра (pytest-benchmark).

Порогов по времени нет — см. conftest. Каждый бенчмарк проверяет результат
на правильность, чтобы измерялась работа, а не пустой проход. Отдельно —
тесты МАСШТАБИРОВАНИЯ: та же функция на 10× данных не должна работать
дольше, чем в ~25× (линейный рост с запасом на шум; квадратичный дал бы 100×).
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import date, timedelta

import pytest

from tests.perf._seed import seed_orders, seed_payments, seed_products

pytestmark = pytest.mark.perf


def _receivables(n: int, today: date):
    from services.receivables import Receivable

    rnd = random.Random(42)
    out = []
    for i in range(n):
        due = today + timedelta(days=rnd.randint(-120, 120))
        out.append(Receivable(
            source="order" if i % 3 else "machine", ref_id=i, title=f"#{i}",
            counterparty=f"Клиент {i % 200}", owner_id=(i % 7) or None,
            due_date=due.isoformat(), amount_cents=rnd.randint(1_000, 5_000_000),
            currency="USD" if i % 5 else "UZS",
        ))
    return out


def test_bench_receivables_aging(benchmark):
    from services import receivables

    today = date(2026, 9, 13)
    items = _receivables(20_000, today)
    res = benchmark(receivables.aging, items, today)
    assert [b["key"] for b in res["buckets"]] == list(receivables.AGING_BUCKETS)


def test_bench_receivables_forecast_and_top(benchmark):
    from services import receivables

    today = date(2026, 9, 13)
    items = _receivables(20_000, today)

    def run():
        return (receivables.forecast(items, months=6, today=today),
                receivables.by_counterparty(items, limit=10),
                receivables.by_owner(items, limit=10))

    fc, by_cp, by_owner = benchmark(run)
    assert len(fc) == 6 and len(by_cp) == 10 and by_owner


def test_bench_installment_schedule_and_allocation(benchmark):
    from services import machines

    def run():
        total = 0
        for k in range(500):
            sched = machines.build_schedule(2_400_000 + k, 400_000, 36, date(2026, 1, 31))
            rows = machines.allocate_receipts(sched, 1_234_567)
            total += sum(r["amount_cents"] for r in rows)
        return total

    assert benchmark(run) > 0


def test_bench_money_conversions(benchmark):
    from services import money

    vals = [round(random.Random(1).uniform(0.01, 99_999), 2) for _ in range(50_000)]

    def run():
        return sum(money.to_cents(v) for v in vals)

    assert benchmark(run) > 0


def test_bench_order_balances_batch(benchmark, api_env):
    """Остатки по 500 заказам батчем — денежное ядро на настоящей БД."""
    from services import debts

    _client, db, ids = api_env
    oids = seed_orders(db, 500, owner=ids["mgr"], product_id=ids["product"], status="shipped")
    seed_payments(db, oids[::2], status="confirmed", amount_cents=1500)

    res = benchmark(lambda: asyncio.run(debts.calc_order_balances(oids)))
    assert len(res) == 500
    assert all(b.total_cents == 3000 for b in res.values())


def test_bench_catalog_5000(benchmark, api_env):
    from services import warehouse

    _client, db, _ids = api_env
    seed_products(db, 5000)
    rows = benchmark(lambda: asyncio.run(warehouse.get_catalog()))
    assert len(rows) >= 5000


# ─── Масштабирование: не квадратично ─────────────────────────────────────────


def _timeit(fn, repeats=3) -> float:
    best = float("inf")
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def test_receivables_aggregations_scale_linearly():
    from services import receivables

    today = date(2026, 9, 13)
    small, large = _receivables(2_000, today), _receivables(20_000, today)

    def work(items):
        receivables.aging(items, today)
        receivables.forecast(items, months=6, today=today)
        receivables.by_counterparty(items, limit=10)

    t_small = _timeit(lambda: work(small))
    t_large = _timeit(lambda: work(large))
    assert t_large < t_small * 25 + 0.05, f"{t_small:.4f}s → {t_large:.4f}s на 10× данных"


def test_order_balances_scale_linearly(api_env):
    from services import debts

    _client, db, ids = api_env
    small = seed_orders(db, 50, owner=ids["mgr"], product_id=ids["product"], status="shipped")
    t_small = _timeit(lambda: asyncio.run(debts.calc_order_balances(small)), repeats=2)
    large = small + seed_orders(db, 450, owner=ids["mgr"], product_id=ids["product"], status="shipped")
    t_large = _timeit(lambda: asyncio.run(debts.calc_order_balances(large)), repeats=2)
    assert t_large < t_small * 25 + 0.2, f"{t_small:.4f}s (50) → {t_large:.4f}s (500)"
