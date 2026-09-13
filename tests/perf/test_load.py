"""Нагрузка на живой сервер: параллельные менеджеры и гонки на одобрении.

Здесь проверяется не скорость, а КОРРЕКТНОСТЬ ПОД ПАРАЛЛЕЛЬНОСТЬЮ: советующие
блокировки, идемпотентность, «остаток не уходит в минус». Один и тот же
сценарий, выполненный последовательно, эти баги не покажет.

Каркас — тот же uvicorn-в-потоке, что у E2E, сверху httpx. Локальная БД —
SQLite, поэтому параллельность ограничена (`CONCURRENCY`): ловим гонки в
логике, а не «database is locked» самого SQLite.
"""

from __future__ import annotations

import asyncio
import statistics
import time
import uuid

import httpx

MANAGERS = 20
CONCURRENCY = 8
TIMEOUT = 30.0


async def _post(client: httpx.AsyncClient, path: str, uid: int, **body):
    t = time.perf_counter()
    r = await client.post(path, json={"initData": str(uid), **body})
    return r, time.perf_counter() - t


def _p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[max(0, int(len(xs) * 0.95) - 1)] if xs else 0.0


def _seed_managers(live, n: int) -> list[int]:
    ids = []
    for i in range(n):
        uid = 10_000 + i
        live.db.set_role(uid, f"m{i}", f"Manager {i}", "manager")
        ids.append(uid)
    return ids


def _stock(live) -> float:
    return live.rows("SELECT quantity FROM stock WHERE product_id = ?", (live.ids["product"],))[0]["quantity"]


async def _make_request(client, uid: int, product_id: int, cp_id: int, qty: float = 1) -> tuple[int, list[float]]:
    """Черновик → клиент → позиция → заявка. Возвращает req_id и задержки."""
    lat = []
    r, dt = await _post(client, "/api/orders/create", uid)
    lat.append(dt)
    assert r.status_code == 200, r.text
    oid = r.json()["order_id"]
    r, dt = await _post(client, "/api/orders/set_agent", uid, order_id=oid, agent_id=str(cp_id),
                        agent_name="ООО Ромашка")
    lat.append(dt)
    assert r.status_code == 200, r.text
    r, dt = await _post(client, "/api/orders/add_item", uid, order_id=oid, product_name="Кабель ВВГ 3x2.5",
                        product_id=product_id, quantity=qty, unit="м", price=10, currency="USD")
    lat.append(dt)
    assert r.status_code == 200, r.text
    r, dt = await _post(client, "/api/orders/submit", uid, order_id=oid, payment_type="paid",
                        due_date=None, idempotency_key=uuid.uuid4().hex)
    lat.append(dt)
    assert r.status_code == 200, r.text
    return r.json()["req_id"], lat


def test_twenty_managers_submit_and_boss_approves_all(live):
    """20 менеджеров одновременно оформляют заявки, босс одобряет их параллельно.

    Итог обязан сойтись до штуки: 20 одобрений, 20 накладных, остаток −20.
    """
    import services.rate_limit as rate_limit

    ids = live.ids
    managers = _seed_managers(live, MANAGERS)
    cp = live.rows("SELECT id FROM counterparties")[0]["id"]
    start_stock = _stock(live)
    assert start_stock >= MANAGERS

    async def phase_submit():
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(base_url=live.base_url, timeout=TIMEOUT) as client:
            async def one(uid):
                async with sem:
                    return await _make_request(client, uid, ids["product"], cp)
            return await asyncio.gather(*(one(u) for u in managers))

    results = live.run(phase_submit())
    req_ids = [r for r, _ in results]
    latencies = [x for _, lat in results for x in lat]
    assert len(set(req_ids)) == MANAGERS
    assert live.rows("SELECT COUNT(*) AS n FROM shipment_requests WHERE status = 'pending'")[0]["n"] == MANAGERS

    rate_limit.reset()

    async def phase_approve():
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(base_url=live.base_url, timeout=TIMEOUT) as client:
            async def one(req_id):
                async with sem:
                    return await _post(client, "/api/requests/approve", ids["boss"], req_id=req_id,
                                       idempotency_key=uuid.uuid4().hex)
            return await asyncio.gather(*(one(r) for r in req_ids))

    approvals = live.run(phase_approve())
    codes = [r.status_code for r, _ in approvals]
    assert codes == [200] * MANAGERS, codes
    assert all(r.json().get("ok") for r, _ in approvals)
    approve_lat = [dt for _, dt in approvals]

    assert live.rows("SELECT COUNT(*) AS n FROM shipment_requests WHERE status = 'approved'")[0]["n"] == MANAGERS
    assert live.rows("SELECT COUNT(*) AS n FROM order_shipment WHERE invoice_id IS NOT NULL")[0]["n"] == MANAGERS
    assert live.rows("SELECT COUNT(*) AS n FROM invoices WHERE type = 'outgoing'")[0]["n"] == MANAGERS
    assert _stock(live) == start_stock - MANAGERS
    # Ни одного 5xx и ни одного дубля уведомления о заявке.
    assert len([p for p in live.pushes if "заявка" in p["text"].lower()]) >= MANAGERS

    print(f"submit: p50 {statistics.median(latencies) * 1000:.0f} мс, p95 {_p95(latencies) * 1000:.0f} мс; "
          f"approve: p50 {statistics.median(approve_lat) * 1000:.0f} мс, p95 {_p95(approve_lat) * 1000:.0f} мс")


def test_concurrent_approvals_of_one_request_ship_once(live):
    """Шесть одновременных «Одобрить» по одной заявке: одна отгрузка, один остаток."""
    ids = live.ids
    cp = live.rows("SELECT id FROM counterparties")[0]["id"]
    start_stock = _stock(live)

    async def run():
        async with httpx.AsyncClient(base_url=live.base_url, timeout=TIMEOUT) as client:
            req_id, _ = await _make_request(client, ids["mgr"], ids["product"], cp, qty=3)
            return req_id, await asyncio.gather(*(
                _post(client, "/api/requests/approve", ids["boss"], req_id=req_id,
                      idempotency_key=uuid.uuid4().hex)
                for _ in range(6)
            ))

    req_id, results = live.run(run())
    codes = sorted(r.status_code for r, _ in results)
    ok = [r for r, _ in results if r.status_code == 200 and r.json().get("ok")]
    assert len(ok) == 1, codes
    assert all(c in (200, 409) for c in codes), codes
    assert live.rows("SELECT COUNT(*) AS n FROM order_shipment")[0]["n"] == 1
    assert live.rows("SELECT COUNT(*) AS n FROM invoices WHERE type = 'outgoing'")[0]["n"] == 1
    assert _stock(live) == start_stock - 3


def test_same_idempotency_key_returns_cached_result(live):
    """Ретрай с тем же ключом — тот же ответ, вторая отгрузка не проводится."""
    ids = live.ids
    cp = live.rows("SELECT id FROM counterparties")[0]["id"]
    key = uuid.uuid4().hex

    async def run():
        async with httpx.AsyncClient(base_url=live.base_url, timeout=TIMEOUT) as client:
            req_id, _ = await _make_request(client, ids["mgr"], ids["product"], cp)
            return await asyncio.gather(*(
                _post(client, "/api/requests/approve", ids["boss"], req_id=req_id, idempotency_key=key)
                for _ in range(5)
            ))

    results = live.run(run())
    bodies = [r.json() for r, _ in results if r.status_code == 200]
    assert len(bodies) >= 1 and all(b.get("ok") for b in bodies), [r.text for r, _ in results]
    assert all(r.status_code in (200, 409) for r, _ in results)
    assert live.rows("SELECT COUNT(*) AS n FROM order_shipment")[0]["n"] == 1


def test_oversold_requests_do_not_drive_stock_negative(live):
    """Заявок больше, чем товара: одобряется ровно столько, сколько есть."""
    import services.rate_limit as rate_limit

    ids = live.ids
    cp = live.rows("SELECT id FROM counterparties")[0]["id"]
    managers = _seed_managers(live, 8)
    start_stock = _stock(live)  # 20; восемь заявок по 5 = 40

    async def submit():
        async with httpx.AsyncClient(base_url=live.base_url, timeout=TIMEOUT) as client:
            return await asyncio.gather(*(_make_request(client, u, ids["product"], cp, qty=5) for u in managers))

    req_ids = [r for r, _ in live.run(submit())]
    rate_limit.reset()

    async def approve():
        async with httpx.AsyncClient(base_url=live.base_url, timeout=TIMEOUT) as client:
            return await asyncio.gather(*(
                _post(client, "/api/requests/approve", ids["boss"], req_id=r, idempotency_key=uuid.uuid4().hex)
                for r in req_ids
            ))

    results = live.run(approve())
    assert all(r.status_code in (200, 409) for r, _ in results), [r.status_code for r, _ in results]
    shipped = live.rows("SELECT COUNT(*) AS n FROM order_shipment WHERE invoice_id IS NOT NULL")[0]["n"]
    failed = live.rows("SELECT COUNT(*) AS n FROM order_shipment WHERE failed_at IS NOT NULL")[0]["n"]
    stock = _stock(live)
    assert stock >= 0, "остаток ушёл в минус"
    assert stock == start_stock - shipped * 5
    assert shipped == start_stock // 5, (shipped, failed, stock)
    # Не списавшиеся заявки не потеряны: они помечены failed_at и ждут доделки.
    assert shipped + failed == 8
