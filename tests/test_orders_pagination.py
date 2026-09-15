"""`/api/orders` страницами и понятный 401.

Список отдавал все заказы роли разом, с позициями: у руководства за год это
мегабайты на каждый вход во вкладку по мобильной сети. Фильтры статуса и
периода обязаны применяться ДО нарезки — иначе «Показать ещё» листал бы
нефильтрованный список.

401 — чаще всего не подделка, а подпись initData старше часа: текст видит
человек, поэтому по-русски и с действием.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

# ─── Чистая функция нарезки ──────────────────────────────────────────────────


def _o(oid: int, status: str = "approved", day: str = "2026-09-10") -> dict:
    return {"id": oid, "status": status, "created_at": f"{day} 12:00:00"}


def test_paginate_slices_after_filters_and_reports_next_offset():
    from webapp.server import _paginate_orders

    orders = [_o(i, "pending" if i % 2 else "approved") for i in range(10, 0, -1)]
    page, meta = _paginate_orders(
        orders, limit=2, offset=0, statuses=["approved"], date_from="", date_to=""
    )
    assert [o["id"] for o in page] == [10, 8]
    assert meta == {
        "total": 5, "offset": 0, "limit": 2, "has_more": True, "next_offset": 2,
        # Счётчик заявок — по ВСЕМ заказам роли, не по фильтру и не по странице.
        "pending_count": 5,
    }
    page, meta = _paginate_orders(
        orders, limit=2, offset=4, statuses=["approved"], date_from="", date_to=""
    )
    assert [o["id"] for o in page] == [2]
    assert meta["has_more"] is False and meta["next_offset"] == 5


def test_paginate_period_bounds_are_inclusive_days():
    from webapp.server import _paginate_orders

    orders = [_o(1, day="2026-09-15"), _o(2, day="2026-09-14"), _o(3, day="2026-09-08"), _o(4, day="")]
    page, meta = _paginate_orders(
        orders, limit=50, offset=0, statuses=[], date_from="2026-09-09", date_to="2026-09-15"
    )
    assert [o["id"] for o in page] == [1, 2]
    assert meta["total"] == 2
    page, _ = _paginate_orders(
        orders, limit=50, offset=0, statuses=[], date_from="2026-09-15", date_to="2026-09-15"
    )
    assert [o["id"] for o in page] == [1]


def test_page_params_validate_and_clamp():
    from fastapi import HTTPException

    from webapp.server import _orders_page_params

    assert _orders_page_params({}) is None, "без limit — прежний ответ целиком"
    p = _orders_page_params({"limit": 10_000, "offset": -3})
    assert p is not None and p["limit"] == 200 and p["offset"] == 0
    for bad in ({"limit": "x"}, {"limit": 5, "statuses": "pending"}, {"limit": 5, "date_from": "15.09.2026"}):
        with pytest.raises(HTTPException) as exc:
            _orders_page_params(bad)
        assert exc.value.status_code == 400


# ─── Ручка ───────────────────────────────────────────────────────────────────


@pytest.fixture
def env(isolated_db, monkeypatch):
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    db.set_role(100, "b", "Boss", "boss")
    db.set_role(200, "m", "Manager", "manager")
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"} if s.isdigit() else None,
    )

    def order(status: str, user: int = 200, day: str | None = None) -> int:
        oid = db.create_order(user, "Manager", "")
        db.update_order_agent(oid, "1", "Клиент")
        db.add_order_item(oid, "Товар", "", 1, "шт", 10.0)
        db.update_order_status(oid, status)
        if day:
            with db.get_conn() as conn:
                cur = conn.cursor()
                cur.execute(db.q("UPDATE orders SET created_at = ? WHERE id = ?"), (f"{day} 09:00:00", oid))
                conn.commit()
        return oid

    return TestClient(server.app), order


def test_boss_pages_through_filtered_orders(env):
    client, order = env
    approved = [order("approved", day=f"2026-09-{d:02d}") for d in range(1, 6)]
    order("pending", day="2026-09-03")
    order("shipped", day="2026-09-04")

    body = {"initData": "100", "limit": 2, "statuses": ["approved"]}
    r1 = client.post("/api/orders", json={**body, "offset": 0}).json()
    r2 = client.post("/api/orders", json={**body, "offset": r1["next_offset"]}).json()
    r3 = client.post("/api/orders", json={**body, "offset": r2["next_offset"]}).json()
    got = [o["id"] for o in r1["orders"] + r2["orders"] + r3["orders"]]
    assert got == list(reversed(approved)), "свежие первыми, без повторов и пропусков"
    assert (r1["total"], r1["has_more"], r3["has_more"]) == (5, True, False)
    assert r1["pending_count"] == 1
    # Позиции и прибыль — только у заказов страницы, но в прежнем формате.
    assert r1["orders"][0]["items"][0]["name"] == "Товар" and "profit" in r1["orders"][0]


def test_period_filter_works_together_with_pages(env):
    client, order = env
    order("approved", day="2026-08-31")
    inside = [order("approved", day="2026-09-02"), order("shipped", day="2026-09-03")]
    r = client.post("/api/orders", json={
        "initData": "100", "limit": 1, "offset": 0, "date_from": "2026-09-01", "date_to": "2026-09-30",
    }).json()
    assert r["total"] == 2 and r["has_more"] is True
    assert r["orders"][0]["id"] == inside[1]


def test_manager_scope_is_kept_with_pages(env):
    client, order = env
    mine = order("approved", user=200)
    order("approved", user=100)
    r = client.post("/api/orders", json={"initData": "200", "limit": 50}).json()
    assert [o["id"] for o in r["orders"]] == [mine]
    assert r["total"] == 1


def test_without_limit_response_is_unchanged(env):
    client, order = env
    order("approved")
    r = client.post("/api/orders", json={"initData": "100"}).json()
    assert len(r["orders"]) == 1
    assert "total" not in r and "has_more" not in r


def test_expired_init_data_gets_russian_401(env):
    client, _order = env
    r = client.post("/api/orders", json={"initData": "expired"})
    assert r.status_code == 401
    detail = r.json()["detail"]
    assert "Сессия истекла" in detail and "Invalid" not in detail
