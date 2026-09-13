"""Размер и время ответа на больших объёмах.

WebApp открывают с телефона по мобильной сети: ответ на 5 000 товаров не
должен весить мегабайты, а на 500 заказов — тянуть позиции всех заказов
целиком. Пороги по байтам жёсткие (это свойство формата, не машины),
по времени — щедрые: ловим секунды, а не миллисекунды.
"""

from __future__ import annotations

import time

from tests.perf._seed import seed_orders, seed_products


def _timed_post(client, path, uid, body=None):
    t = time.perf_counter()
    r = client.post(path, json={"initData": str(uid), **(body or {})})
    return r, time.perf_counter() - t


def test_stock_5000_products_payload(api_env, query_counter):
    client, db, ids = api_env
    seed_products(db, 5000)
    query_counter.reset()
    r, dt = _timed_post(client, "/api/stock", ids["boss"])
    assert r.status_code == 200
    n = len(r.json()["products"])
    assert n >= 5000
    per_product = len(r.content) / n
    print(f"/api/stock: {n} товаров, {len(r.content) / 1024:.0f} КБ, {per_product:.0f} Б/товар, "
          f"{dt * 1000:.0f} мс, {query_counter.total} SQL")
    assert per_product < 300, "формат строки каталога распух"
    assert dt < 5.0
    assert query_counter.total < 20, "каталог собирается батчем"


def test_orders_500_payload(api_env, query_counter):
    client, db, ids = api_env
    seed_orders(db, 500, owner=ids["mgr"], product_id=ids["product"], status="approved", items_per_order=3)
    query_counter.reset()
    r, dt = _timed_post(client, "/api/orders", ids["boss"])
    assert r.status_code == 200
    orders = r.json()["orders"]
    assert len(orders) == 500
    per_order = len(r.content) / len(orders)
    print(f"/api/orders: {len(orders)} заказов, {len(r.content) / 1024:.0f} КБ, {per_order:.0f} Б/заказ, "
          f"{dt * 1000:.0f} мс, {query_counter.total} SQL")
    assert per_order < 2000
    assert dt < 5.0


def test_debts_500_payload(api_env, query_counter):
    client, db, ids = api_env
    seed_orders(db, 500, owner=ids["mgr"], product_id=ids["product"], status="approved")
    query_counter.reset()
    r, dt = _timed_post(client, "/api/debts", ids["boss"], {"mode": "all"})
    assert r.status_code == 200
    debts = r.json()["debts"]
    assert len(debts) == 500
    print(f"/api/debts: {len(debts)} долгов, {len(r.content) / 1024:.0f} КБ, {dt * 1000:.0f} мс, "
          f"{query_counter.total} SQL")
    assert len(r.content) / len(debts) < 1500
    assert dt < 5.0
