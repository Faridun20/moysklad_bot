"""T2.11 — create_cash_deposit не выедает пул коннектов.

Критерий готовности: сдача по 50 заказам не исчерпывает пул из 10 коннектов.

§2.13: FIFO-расчёт шёл ВНУТРИ `async with adb_core.transaction()` (коннект
захвачен на всё время), а `get_manager_open_orders_for_deposit` делала 4
запроса НА КАЖДЫЙ открытый заказ, и каждый брал свой коннект из того же
пула. При PG_POOL_MAX=10 одновременных сдачах все коннекты держали
транзакции, внутренние запросы ждали свободного, а `asyncpg.Pool.acquire()`
без таймаута ждёт вечно — процесс вставал до рестарта.

Прямо посчитать коннекты Postgres в тестах нельзя (тут SQLite), поэтому
меряем то, что и создаёт давление на пул: сколько РАЗ за одну сдачу берётся
отдельное соединение (каждый вызов adb_core.fetch/fetchval/execute вне
транзакции = одно соединение). Было O(заказов), стало O(1).
"""

import asyncio

import pytest

import services.adb_core as adb_core
import services.database as _db


def _shipped_orders(db, n, uid=1, total=100.0):
    db.set_role(uid, "mgr", "Manager", "manager")
    ids = []
    for _ in range(n):
        oid = db.create_order(uid, "Manager", "")
        db.add_order_item(oid, "Товар", "href", 1, "шт", total)
        db.update_order_agent(oid, "A-1", "Клиент")
        db.update_order_status(oid, "shipped")
        ids.append(oid)
    return ids


@pytest.fixture
def count_acquires(monkeypatch):
    """Считает захваты соединения вне транзакции."""
    calls = {"n": 0}

    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        orig = getattr(adb_core, name)

        def make(orig=orig):
            async def wrapper(*a, **kw):
                calls["n"] += 1
                return await orig(*a, **kw)

            return wrapper

        monkeypatch.setattr(adb_core, name, make())
    return calls


# ─── Критерий готовности ────────────────────────────────────────────────────


def test_deposit_over_50_orders_completes(isolated_db):
    """50 заказов по 100 → сдача 5000 покрывает все, расчёт не виснет."""
    db = isolated_db
    ids = _shipped_orders(db, 50)

    res = asyncio.run(db.create_cash_deposit(1, 5000.0))

    assert res["ok"] is True, res
    assert len(res["allocations"]) == 50
    assert sum(a for _, a in res["allocations"]) == pytest.approx(5000.0)
    assert {oid for oid, _ in res["allocations"]} == set(ids)


def test_connection_acquisitions_do_not_scale_with_orders(isolated_db, count_acquires):
    """Главная проверка: число захватов соединения не растёт с числом заказов.

    До фикса было ~4 запроса на заказ, каждый со своим коннектом.
    """
    db = isolated_db
    _shipped_orders(db, 5)
    count_acquires["n"] = 0
    asyncio.run(db.create_cash_deposit(1, 500.0))
    few = count_acquires["n"]

    db2 = isolated_db
    _shipped_orders(db2, 40, uid=2)
    count_acquires["n"] = 0
    asyncio.run(db2.create_cash_deposit(2, 4000.0))
    many = count_acquires["n"]

    assert many <= few + 2, (
        f"захваты соединения растут с числом заказов: {few} → {many}. "
        "Похоже, вернулся N+1 внутри транзакции."
    )


def test_fifo_computed_before_transaction():
    """Структурная гарантия: расчёт — ДО `async with transaction()`.

    Именно вложенность (захват коннектов изнутри удерживаемой транзакции)
    и приводила к вечному ожиданию пула.
    """
    import inspect

    src = inspect.getsource(_db.create_cash_deposit)
    calc_at = src.index("get_manager_open_orders_for_deposit(manager_id)")
    txn_at = src.index("async with adb_core.transaction()")
    assert calc_at < txn_at, "FIFO-расчёт снова внутри транзакции"


# ─── Корректность распределения не пострадала ───────────────────────────────


def test_fifo_order_is_preserved(isolated_db):
    """Распределяем по возрастанию created_at — старые долги гасятся первыми."""
    db = isolated_db
    ids = _shipped_orders(db, 3, total=100.0)

    res = asyncio.run(db.create_cash_deposit(1, 150.0))

    assert res["ok"] is True
    # Первый заказ покрыт полностью, второй — наполовину, третий не тронут.
    assert res["allocations"] == [(ids[0], 100.0), (ids[1], 50.0)]


def test_partially_covered_order_is_not_double_allocated(isolated_db):
    """Остаток, застолблённый прошлой (ещё pending) сдачей, повторно не раздаём."""
    db = isolated_db
    ids = _shipped_orders(db, 2, total=100.0)

    first = asyncio.run(db.create_cash_deposit(1, 100.0))
    assert first["allocations"] == [(ids[0], 100.0)]

    second = asyncio.run(db.create_cash_deposit(1, 100.0))
    # Первый заказ уже закрыт pending-сдачей → уходим на второй.
    assert second["allocations"] == [(ids[1], 100.0)]


def test_deposit_larger_than_debt_allocates_only_what_is_owed(isolated_db):
    db = isolated_db
    ids = _shipped_orders(db, 2, total=100.0)

    res = asyncio.run(db.create_cash_deposit(1, 1000.0))

    assert res["ok"] is True
    assert res["allocations"] == [(ids[0], 100.0), (ids[1], 100.0)]


def test_manual_allocations_bypass_fifo(isolated_db):
    """Ручной режим оставлен как был — расчёт не запускается."""
    db = isolated_db
    ids = _shipped_orders(db, 3, total=100.0)

    res = asyncio.run(db.create_cash_deposit(1, 50.0, allocations=[(ids[2], 50.0)]))

    assert res["ok"] is True
    assert res["allocations"] == [(ids[2], 50.0)]
