"""N+1 на горячих ручках: число SQL-запросов не должно расти с числом строк.

Проверка масштабированием, а не «ровно N запросов»: одна и та же ручка
меряется на маленькой и на большой базе, и разница обязана уложиться в
константу. Абсолютное число запросов — не инвариант (рефакторинг законно
меняет его на ±2), а вот линейный рост — ровно тот баг, который здесь ловят.
`SLACK` оставляет место под запросы, зависящие от РОЛЕЙ и КЭША, но не от
данных (роль пользователя, настройки, курс валюты).
"""

from __future__ import annotations

import pytest

from tests.perf._seed import seed_counterparties, seed_orders, seed_payments, seed_products

SMALL = 6
LARGE = 60
SLACK = 4


def _count(client, qc, path: str, uid: int, body: dict | None = None) -> int:
    qc.reset()
    r = client.post(path, json={"initData": str(uid), **(body or {})})
    assert r.status_code == 200, (path, r.status_code, r.text[:200])
    return qc.total


def _scaled(api_env, qc, path, uid, seed_fn, body=None):
    """Запросов на LARGE строках ≤ запросов на SMALL + SLACK."""
    client, db, ids = api_env
    seed_fn(db, ids, SMALL)
    small = _count(client, qc, path, uid, body)
    seed_fn(db, ids, LARGE - SMALL)
    large = _count(client, qc, path, uid, body)
    assert large <= small + SLACK, (
        f"{path}: {small} запросов на {SMALL} строках → {large} на {LARGE}: N+1 "
        f"(последние: {qc.statements[-6:]})"
    )
    return small, large


def _orders(status="approved", **kw):
    def seed(db, ids, n):
        seed_orders(db, n, owner=ids["mgr"], product_id=ids["product"], status=status, **kw)
    return seed


# ─── Заказы ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["boss", "mgr", "keeper"])
def test_orders_list_is_batched(api_env, query_counter, who):
    ids = api_env[2]
    _scaled(api_env, query_counter, "/api/orders", ids[who], _orders())


def test_pending_requests_are_batched(api_env, query_counter):
    """Экран заявок босса: кредит-контекст (долг + лимит) по РАЗНЫМ контрагентам — батчем."""
    ids = api_env[2]

    def seed(db, i, n):
        for cp in seed_counterparties(db, n):
            seed_orders(db, 1, owner=i["mgr"], product_id=i["product"], agent_id=str(cp),
                        status="pending", with_request=True)

    _scaled(api_env, query_counter, "/api/orders/requests", ids["boss"], seed)


# ─── Деньги ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["boss", "mgr"])
def test_debts_are_batched(api_env, query_counter, who):
    ids = api_env[2]

    def seed(db, i, n):
        oids = seed_orders(db, n, owner=i["mgr"], product_id=i["product"], status="approved")
        seed_payments(db, oids[: n // 2], status="confirmed")
        seed_payments(db, oids[n // 2:], status="pending")

    _scaled(api_env, query_counter, "/api/debts", ids[who], seed, {"mode": "all"})


def test_payments_pending_is_batched(api_env, query_counter):
    ids = api_env[2]

    def seed(db, i, n):
        oids = seed_orders(db, n, owner=i["mgr"], product_id=i["product"],
                           status="approved", payment_type="paid", due_date=None)
        seed_payments(db, oids, status="pending")

    _scaled(api_env, query_counter, "/api/payments/pending", ids["boss"], seed)


def test_clients_overview_is_batched(api_env, query_counter):
    """Обзор клиентов: долг по каждому — батчем, не по одному контрагенту."""
    ids = api_env[2]

    def seed(db, i, n):
        # n разных контрагентов, у каждого по заказу.
        for cp in seed_counterparties(db, n):
            seed_orders(db, 1, owner=i["mgr"], product_id=i["product"], agent_id=str(cp), status="approved")

    _scaled(api_env, query_counter, "/api/clients/overview", ids["boss"], seed)


# ─── Главная и очередь ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/api/today", "/api/home"])
def test_home_and_today_do_not_scale_with_orders(api_env, query_counter, path):
    ids = api_env[2]

    def seed(db, i, n):
        seed_orders(db, n, owner=i["mgr"], product_id=i["product"],
                    status="pending", with_request=True, due_date="2020-01-01")

    _scaled(api_env, query_counter, path, ids["boss"], seed)


# ─── Склад ───────────────────────────────────────────────────────────────────


def test_stock_catalog_is_batched(api_env, query_counter):
    ids = api_env[2]

    def seed(db, i, n):
        seed_products(db, n)

    _scaled(api_env, query_counter, "/api/stock", ids["boss"], seed)


def test_wh_invoices_list_is_batched(api_env, query_counter):
    from services import warehouse
    from tests.liveserver import run_async

    ids = api_env[2]

    def seed(db, i, n):
        for _ in range(n):
            run_async(warehouse.create_invoice(
                invoice_type="incoming", warehouse_id=i["warehouse"],
                items=[{"product_id": i["product"], "quantity": 1, "price_cents": None}],
            ))

    _scaled(api_env, query_counter, "/api/wh/invoices", ids["boss"], seed, {"limit": 100})


# ─── Аналитика ───────────────────────────────────────────────────────────────


def test_analytics_does_not_scale_with_orders(api_env, query_counter):
    ids = api_env[2]
    _scaled(api_env, query_counter, "/api/analytics", ids["boss"], _orders(status="shipped"),
            {"period": "month"})


def test_money_summary_does_not_scale_with_orders(api_env, query_counter):
    ids = api_env[2]

    def seed(db, i, n):
        oids = seed_orders(db, n, owner=i["mgr"], product_id=i["product"], status="shipped")
        seed_payments(db, oids, status="confirmed")

    _scaled(api_env, query_counter, "/api/money/summary", ids["boss"], seed, {"period": "month"})
