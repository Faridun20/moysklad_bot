"""
Гонки денежных и складских операций на НАСТОЯЩЕМ Postgres.

На SQLite пишущая транзакция одна (`BEGIN IMMEDIATE`), и два пути с РАЗНЫМИ
замками там всё равно выстраиваются в очередь — баг «сдача и отметка оплаты
одного заказа проходят обе» виден только на Postgres, где advisory-lock по
менеджеру и `FOR UPDATE` заказа друг друга не ждут.

Приём: «барьер» внутри транзакции. Подменённый расчёт остатка ждёт, пока до
него дойдут ОБЕ транзакции (или истечёт секунда). С разными замками обе доходят
одновременно и видят один и тот же остаток — тест падает. С общим замком вторая
стоит на `FOR UPDATE`, первая после секунды считает, пишет и коммитит, и
вторая видит уже заявленное.

Запуск как у `test_money_postgres.py`: `TEST_PG_URL=postgresql://… pytest
tests/test_txn_races_postgres.py`. Без переменной — пропуск.
"""

from __future__ import annotations

import asyncio
import importlib
import os

import pytest

from tests import test_money_postgres as _pg

PG_URL = _pg.PG_URL
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

MGR, BOSS = 1, 2


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def pg(pg_db):
    """База Postgres + модули, которые копируют USE_POSTGRES на импорте
    (`order_shipment`). После теста pg_db вернёт database к SQLite, и
    order_shipment обязан вернуться вместе с ним — иначе следующие SQLite-тесты
    получат FOR UPDATE."""
    import services.order_shipment as order_shipment

    importlib.reload(order_shipment)
    yield pg_db


@pytest.fixture(autouse=True)
def _reload_order_shipment_after():
    yield
    import services.order_shipment as order_shipment

    importlib.reload(order_shipment)


# pg_db — своя база на каждый тест (CREATE DATABASE … / DROP … WITH (FORCE)).
pg_db = _pg.pg_db


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        conn.commit()


def _order(db, *, total=100.0, status="shipped", payment_type="credit", owner=MGR):
    oid = db.create_order(owner, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    _exec(
        db,
        "UPDATE orders SET payment_type = ?, due_date = ?, currency = 'USD' WHERE id = ?",
        (payment_type, "2030-01-15" if payment_type == "credit" else None, oid),
    )
    db.update_order_status(oid, status)
    return oid


class _Barrier:
    """Держит каждого вошедшего, пока не войдут `parties` участников (или не
    истечёт `timeout`). Одновременно дошедшие до расчёта — признак разных
    замков; под общим замком второй сюда не попадает до коммита первого."""

    def __init__(self, parties: int = 2, timeout: float = 1.0):
        self.parties = parties
        self.timeout = timeout
        self.entered = 0
        self.max_inside = 0
        self._inside = 0
        self._event: asyncio.Event | None = None

    async def wait(self) -> None:
        if self._event is None:
            self._event = asyncio.Event()
        self.entered += 1
        self._inside += 1
        self.max_inside = max(self.max_inside, self._inside)
        if self.entered >= self.parties:
            self._event.set()
        try:
            await asyncio.wait_for(self._event.wait(), self.timeout)
        except TimeoutError:
            pass
        finally:
            self._inside -= 1


def _barrier_on_claimable(monkeypatch) -> _Barrier:
    """Барьер в `calc_claimable_cents`, только для вызовов ВНУТРИ транзакции."""
    import services.debts as debts

    barrier = _Barrier()
    real = debts.calc_claimable_cents

    async def gated(order_ids, conn=None):
        if conn is not None:
            await barrier.wait()
        return await real(order_ids, conn=conn)

    monkeypatch.setattr(debts, "calc_claimable_cents", gated)
    return barrier


async def _claimed_cents(db, oid) -> int:
    from services import adb_core

    pays = await adb_core.fetchval(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE order_id = $1 "
        "AND status IN ('pending', 'confirmed')",
        oid,
    )
    deps = await adb_core.fetchval(
        "SELECT COALESCE(SUM(cdo.amount_allocated_cents), 0) FROM cash_deposit_orders cdo "
        "JOIN cash_deposits d ON d.id = cdo.deposit_id "
        "WHERE cdo.order_id = $1 AND d.status IN ('pending', 'confirmed')",
        oid,
    )
    return int(pays or 0) + int(deps or 0)


# ─── 1. Одни деньги не заявляются дважды ─────────────────────────────────────


def test_parallel_deposit_and_mark_paid_do_not_claim_the_same_money(pg, monkeypatch):
    db = pg
    oid = _order(db, total=100.0)
    barrier = _barrier_on_claimable(monkeypatch)

    async def both():
        return await asyncio.gather(
            db.create_cash_deposit(MGR, 100.0),
            db.mark_order_paid(oid, MGR, "Manager", amount=None),
        )

    deposit, (paid_ok, _pid) = _run(both())
    assert barrier.max_inside == 1, "обе транзакции считали остаток одновременно — замки разные"
    assert _run(_claimed_cents(db, oid)) == 10_000, (deposit, paid_ok)
    # Ровно один из двух путей получил деньги заказа.
    assert bool(deposit["allocations"]) != bool(paid_ok)


def test_parallel_deposits_of_one_manager_still_do_not_overlap(pg, monkeypatch):
    """Advisory-lock по менеджеру снят — две его сдачи сериализует тот же замок строк."""
    db = pg
    oid = _order(db, total=100.0)
    _barrier_on_claimable(monkeypatch)

    async def both():
        return await asyncio.gather(db.create_cash_deposit(MGR, 80.0), db.create_cash_deposit(MGR, 80.0))

    _run(both())
    assert _run(_claimed_cents(db, oid)) == 10_000


# ─── 2. Подтверждение платежа — одна транзакция ──────────────────────────────


def test_confirm_payment_rolls_back_when_close_fails_on_postgres(pg, monkeypatch):
    db = pg
    oid = _order(db, total=100.0)
    ok, pid = _run(db.mark_order_paid(oid, MGR, "Manager", amount=100.0))
    assert ok

    async def boom(*a, **kw):
        raise RuntimeError("сбой")

    real = db._close_order_if_covered_locked
    monkeypatch.setattr(db, "_close_order_if_covered_locked", boom)
    with pytest.raises(RuntimeError):
        _run(db.confirm_payment(pid, BOSS, "Boss"))
    assert _run(db.get_payment(pid))["status"] == "pending"
    monkeypatch.setattr(db, "_close_order_if_covered_locked", real)
    assert _run(db.confirm_payment(pid, BOSS, "Boss"))
    assert _run(db.get_order(oid))["paid_confirmed_at"] is not None


# ─── 4. Отмена заказа: статус и склад вместе ─────────────────────────────────


def test_cancel_crash_after_stock_reversal_rolls_back_on_postgres(pg, monkeypatch):
    from services import container_receipt, order_shipment, warehouse
    from services.order_workflow import approve_shipment_request, cancel_order_full, submit_order

    db = pg
    db.set_role(100, "boss2", "Boss", "boss")
    db.set_role(200, "mgr2", "Manager", "manager")
    _exec(db, "INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)",
          ("Клиент", "customer", "", db.now_str()))
    pid = _run(container_receipt.create_product("Кабель"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(invoice_type="incoming", warehouse_id=wid,
                                  items=[{"product_id": pid, "quantity": 10, "price_cents": None}]))
    oid = db.create_order(200, "Manager", "")
    db.update_order_agent(oid, "1", "Клиент")
    db.add_order_item(oid, "Кабель", "", 2, "шт", 5.0, product_id=pid)
    sub = _run(submit_order(oid, 200, "Manager", payment_type="credit", due_date="2030-01-15"))
    assert _run(approve_shipment_request(sub["req_id"], 100, "Boss", None))["ok"]

    def stock():
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute("SELECT SUM(quantity) AS q FROM stock WHERE product_id = %s", (pid,))
            return float(cur.fetchone()["q"] or 0)

    assert stock() == 8
    real = order_shipment.cancel_shipment_locked

    async def reverse_then_crash(txn, order_id, *, user_id=None):
        await real(txn, order_id, user_id=user_id)
        raise RuntimeError("убит посередине")

    monkeypatch.setattr(order_shipment, "cancel_shipment_locked", reverse_then_crash)
    with pytest.raises(RuntimeError):
        _run(cancel_order_full(oid, 100, "Boss", "Клиент передумал"))
    assert _run(db.get_order(oid))["status"] == "approved" and stock() == 8
    monkeypatch.setattr(order_shipment, "cancel_shipment_locked", real)
    assert _run(cancel_order_full(oid, 100, "Boss", "Клиент передумал"))["ok"]
    assert stock() == 10


# ─── 5. «Отгрузить» без одобрения: двойное нажатие ───────────────────────────


def test_parallel_ship_without_approval_writes_off_and_takes_money_once(pg):
    """Два одновременных «Внести оплату и отгрузить» по черновику на Postgres:
    одна заявка, одна накладная, остаток списан один раз, оплата записана один
    раз, заказ отгружен."""
    from services import container_receipt, warehouse
    from services.order_workflow import ship_order_now

    db = pg
    db.set_role(200, "mgr2", "Manager", "manager")
    _exec(db, "INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)",
          ("Клиент", "customer", "", db.now_str()))
    pid = _run(container_receipt.create_product("Кабель"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(invoice_type="incoming", warehouse_id=wid,
                                  items=[{"product_id": pid, "quantity": 10, "price_cents": None}]))
    oid = db.create_order(200, "Manager", "")
    db.update_order_agent(oid, "1", "Клиент")
    db.add_order_item(oid, "Кабель", "", 2, "шт", 5.0, product_id=pid)
    cash = [{"method": "cash", "currency": "USD", "amount": "10"}]

    async def both():
        return await asyncio.gather(*(
            ship_order_now(oid, 200, "Manager", None, actor_role="manager", payment_type="paid", parts=cash)
            for _ in range(2)
        ))

    results = _run(both())
    assert any(r["ok"] for r in results), results

    def one(sql):
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(sql, (oid,) if "%s" in sql else ())
            return cur.fetchone()

    assert _run(db.get_order(oid))["status"] == "shipped"
    assert float(one(f"SELECT SUM(quantity) AS q FROM stock WHERE product_id = {int(pid)}")["q"]) == 8
    assert int(one("SELECT COUNT(*) AS c FROM shipment_requests WHERE order_id = %s")["c"]) == 1
    assert int(one("SELECT COUNT(*) AS c FROM invoices WHERE type = 'outgoing'")["c"]) == 1
    paid = one("SELECT COALESCE(SUM(amount_cents), 0) AS c FROM payments WHERE order_id = %s "
               "AND status IN ('pending', 'confirmed')")
    assert int(paid["c"]) == 1000


# ─── 6. Закрыть день: два пересчёта одной кассы ──────────────────────────────


def test_parallel_close_day_does_not_double_the_difference(pg, monkeypatch):
    from services import accounting as acc

    boss = acc.Actor(BOSS, "Boss", "boss")
    _run(acc.set_enabled(boss, True))
    cash = _run(acc.save_account(boss, {"name": "Касса", "kind": "cash", "currency": "USD",
                                        "opening": "100"}))["id"]
    barrier = _Barrier()
    real = acc.account_balance

    async def gated(account_id, conn=None):
        if conn is not None:
            await barrier.wait()
        return await real(account_id, conn=conn)

    monkeypatch.setattr(acc, "account_balance", gated)

    async def both():
        return await asyncio.gather(*(
            acc.close_day(boss, {"account_id": cash, "counted": "90", "note": "недостача",
                                 "idempotency_key": key})
            for key in ("c-1", "c-2")
        ))

    first, second = _run(both())
    assert barrier.max_inside == 1
    assert sorted([first["diff_cents"], second["diff_cents"]]) == [-1000, 0]
    monkeypatch.setattr(acc, "account_balance", real)
    balance = {a["id"]: a["balance_cents"] for a in _run(acc.balances())["accounts"]}[cash]
    assert balance == 9000, "остаток обязан совпасть с пересчётом, а не уехать на вторую разницу"


# ─── 10. Черновик: удаление и правка против сабмита ─────────────────────────


async def _hold_order_and_submit(oid: int):
    """Чужая транзакция: FOR UPDATE заказа и перевод в pending — как submit_order."""
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    tr = conn.transaction()
    await tr.start()
    await conn.execute("SELECT id FROM orders WHERE id = $1 FOR UPDATE", oid)
    await conn.execute("UPDATE orders SET status = 'pending' WHERE id = $1", oid)
    return conn, tr


def test_delete_order_waits_for_submit_and_does_not_delete_pending(pg):
    db = pg
    oid = db.create_order(MGR, "Manager", "")
    db.add_order_item(oid, "Товар", "", 1, "шт", 10.0)

    async def scenario():
        conn, tr = await _hold_order_and_submit(oid)
        try:
            task = asyncio.create_task(db.delete_order(oid, MGR))
            await asyncio.sleep(0.5)
            assert not task.done(), "удаление обязано ждать сабмит на замке строки"
            await tr.commit()
            return await task
        finally:
            await conn.close()

    assert _run(scenario()) is False
    order = _run(db.get_order(oid))
    assert order and order["status"] == "pending"
    assert len(_run(db.get_order_items(oid))) == 1


def test_add_item_waits_for_submit_and_is_refused(pg):
    db = pg
    oid = db.create_order(MGR, "Manager", "")

    async def scenario():
        conn, tr = await _hold_order_and_submit(oid)
        try:
            task = asyncio.create_task(asyncio.to_thread(
                db.add_order_item, oid, "Лишнее", "", 1, "шт", 5.0, require_draft=True
            ))
            await asyncio.sleep(0.5)
            assert not task.done()
            await tr.commit()
            return await task
        finally:
            await conn.close()

    assert _run(scenario()) is None
    assert _run(db.get_order_items(oid)) == []


# ─── 7–9. Тяжёлые запросы: тот же SQL на Postgres ───────────────────────────


def test_orders_page_matches_reference_on_postgres(pg):
    from webapp.server import _paginate_orders

    db = pg
    statuses = ["draft", "pending", "approved", "shipped"]
    for i in range(14):
        oid = db.create_order(MGR, "Manager", "")
        db.update_order_status(oid, statuses[i % 4])
        _exec(db, "UPDATE orders SET created_at = ? WHERE id = ?", (f"2026-09-{(i % 6) + 1:02d} 10:00:00", oid))
    everything = _run(db.get_all_orders())
    for case in (dict(statuses=["approved", "pending"], date_from="2026-09-02", date_to="2026-09-05"),
                 dict(statuses=[], date_from="", date_to="")):
        ref, meta = _paginate_orders(everything, limit=3, offset=2, **case)
        rows, total, pending = _run(db.get_orders_page(scope="all", limit=3, offset=2, **case))
        assert [r["id"] for r in rows] == [r["id"] for r in ref]
        assert (total, pending) == (meta["total"], meta["pending_count"])


def test_orders_list_survives_more_ids_than_asyncpg_parameter_limit(pg):
    """33 000 заказов: `IN (все id)` упирался в предел asyncpg — 32 767
    параметров — и /api/orders руководства падал целиком."""
    db = pg
    _exec(db, "INSERT INTO orders (user_id, full_name, status, created_at, updated_at) "
              "SELECT 1, 'M', 'approved', '2026-01-01 00:00:00', '2026-01-01 00:00:00' "
              "FROM generate_series(1, 33000)")
    ids = [o["id"] for o in _run(db.get_all_orders())]
    assert len(ids) == 33000
    _run(db.get_order_items_by_ids(ids))  # без пачек — InterfaceError про 32767 аргументов
    rows, total, _pending = _run(db.get_orders_page(scope="all", limit=50, offset=32990))
    assert total == 33000 and len(rows) == 10
    # Аналитика менеджеров за такой период тоже шла тремя IN по всем id.
    perf = _run(db.get_manager_performance("2025-01-01 00:00:00", "2027-01-01 00:00:00"))
    assert perf[0]["orders_count"] == 33000 and perf[0]["approved"] == 33000


def test_sales_stats_aggregates_beyond_list_limit_on_postgres(pg):
    from services import warehouse

    db = pg
    assert db.set_currency_rate("UZS", 0.0001, BOSS)[0]
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("INSERT INTO products (name, unit, created_at) VALUES ('Кабель', 'шт', %s) RETURNING id",
                    (db.now_str(),))
        pid = cur.fetchone()["id"]
        cur.execute("INSERT INTO products (name, unit, created_at) VALUES ('Труба', 'шт', %s) RETURNING id",
                    (db.now_str(),))
        pid2 = cur.fetchone()["id"]
        for n in range(1100):
            currency = "UZS" if n % 4 == 0 else "USD"
            cur.execute(
                "INSERT INTO invoices (type, warehouse_id, invoice_number, invoice_date, status, currency, "
                "total_amount_cents, created_at) VALUES ('outgoing', 1, %s, %s, 'confirmed', %s, 1500, %s) "
                "RETURNING id",
                (f"P-{n}", f"2026-07-{(n % 30) + 1:02d}", currency, db.now_str()),
            )
            inv = cur.fetchone()["id"]
            cur.execute("INSERT INTO invoice_items (invoice_id, product_id, quantity, price_cents) "
                        "VALUES (%s, %s, 1.5, 999)", (inv, pid if n % 3 else pid2))
        conn.commit()

    stats = _run(warehouse.sales_stats("2026-07-01", "2026-07-31"))
    assert stats["count"] == 1100
    assert stats["by_currency"] == {"USD": 825 * 1500, "UZS": 275 * 1500}
    assert stats["base_total"] == 825 * 1500 + 41  # 412 500 × 0.0001 = 41.25 → 41
    names = [(n, d["currency"]) for n, d in stats["top_products"]]
    # USD впереди UZS при любой сумме: 1 копейка сума — это 0.0001 цента.
    assert names[:2] == [("Кабель", "USD"), ("Труба", "USD")]
    # 1.5 × 999 = 1498.5 → 1499 за строку (HALF_UP), как money.mul_qty.
    assert stats["top_products"][0][1]["sum"] == 550 * 1499  # 550 долларовых строк «Кабель»
    assert sum(_run(warehouse.shipment_counts_by_day("2026-07-01", "2026-07-31")).values()) == 1100


def test_manager_performance_and_money_screens_on_postgres(pg):
    db = pg
    oid = _order(db, total=100.0)
    ok, pid = _run(db.mark_order_paid(oid, MGR, "Manager", amount=40.0))
    assert _run(db.confirm_payment(pid, BOSS, "Boss"))
    _run(db.mark_order_paid(oid, MGR, "Manager", amount=10.0))
    perf = _run(db.get_manager_performance("2000-01-01 00:00:00", "2999-01-01 00:00:00"))
    assert perf[0]["revenue"] == 100.0 and perf[0]["debt"] == 60.0
    hist = _run(db.get_cash_history(80, since="2000-01-01 00:00:00", until="2999-01-01 00:00:00"))
    assert sorted(h["status"] for h in hist if h["kind"] == "payment") == ["confirmed", "pending"]
    totals = _run(db.get_money_totals("2000-01-01 00:00:00", "2999-01-01 00:00:00"))
    assert totals["payments"][0]["total_cents"] == 4000


# ─── 10–11. Ключи идемпотентности и разовые backfill'ы на Postgres ───────────


def test_idem_reclaim_and_backfill_flags_on_postgres(pg):
    db = pg
    _exec(db, "INSERT INTO idempotency_keys (key, operation, user_id, result, created_at, expires_at) "
              "VALUES (?, 'mark_paid', 1, NULL, '2000-01-01 00:00:00', '2999-01-01 00:00:00')", ("k",))
    assert _run(db.idem_claim("k", "mark_paid", 1)) == {}
    assert _run(db.idem_claim("k", "mark_paid", 1, reclaim_after_s=600)) is None
    assert _run(db.idem_claim("k", "mark_paid", 1, reclaim_after_s=600)) == {}, "свежий ключ не отдаётся"

    assert all(v != "skipped" for v in db.run_backfills().values())
    assert set(db.run_backfills().values()) == {"skipped"}
