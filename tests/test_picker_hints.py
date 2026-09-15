"""D1 продуктового аудита: подсказка порядка в «Выборе товара»
(`services.warehouse.picker_hints`, ручка `/api/products/picker_hints`).

Клиент задан и уже покупал у нас — его товары по давности последней покупки
(«Недавно у этого клиента»); нет клиента, или у него ещё нет истории (первый
заказ), — частые товары самого МЕНЕДЖЕРА за последние ~30 дней («Часто
заказываемое»). Список — только порядок: полный алфавитный каталог фронт не
теряет (см. `webapp/static/__tests__/product-picker.test.js`), здесь
проверяем ровно то, что отдаёт сервис.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def wh(isolated_db):
    """Схема + склад + три товара. Возвращает services.warehouse."""
    import importlib

    import services.warehouse as warehouse

    importlib.reload(warehouse)

    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO warehouses (name) VALUES (?)"), ("Основной склад",))
        for name in ("Товар А", "Товар Б", "Товар В"):
            cur.execute(
                db.q("INSERT INTO products (name, unit, created_at) VALUES (?, ?, ?)"),
                (name, "шт", db.now_str()),
            )
        conn.commit()
    return warehouse


def _run(coro):
    return asyncio.run(coro)


def _order(db, *, user_id, agent_id=None, status="approved", created_at=None):
    order_id = db.create_order(user_id, "Manager")
    if agent_id is not None:
        db.update_order_agent(order_id, str(agent_id), "Клиент")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status = ?, created_at = ? WHERE id = ?"),
            (status, created_at or db.now_str(), order_id),
        )
        conn.commit()
    return order_id


def _add_item(db, order_id, product_id, *, created_at=None):
    item_id = db.add_order_item(order_id, "Товар", None, 1, "шт", 100, product_id=product_id)
    if created_at:
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(
                db.q("UPDATE order_item_products SET created_at = ? WHERE item_id = ?"),
                (created_at, item_id),
            )
            conn.commit()
    return item_id


def _pids(db, *names):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT id, name FROM products"))
        by_name = {r["name"]: r["id"] for r in cur.fetchall()}
    return tuple(by_name[n] for n in names)


# ─── Клиент с историей: по давности последней покупки ────────────────────────


def test_client_recent_products_first_by_last_purchase(wh, isolated_db):
    db = isolated_db
    a, b, c = _pids(db, "Товар А", "Товар Б", "Товар В")
    o1 = _order(db, user_id=1, agent_id=42, created_at="2030-01-01 10:00:00")
    _add_item(db, o1, a, created_at="2030-01-01 10:00:00")
    o2 = _order(db, user_id=1, agent_id=42, created_at="2030-01-10 10:00:00")
    _add_item(db, o2, b, created_at="2030-01-10 10:00:00")
    # Товар В — у ДРУГОГО клиента, в подсказку этого клиента попасть не должен.
    o3 = _order(db, user_id=1, agent_id=99, created_at="2030-01-15 10:00:00")
    _add_item(db, o3, c, created_at="2030-01-15 10:00:00")

    res = _run(wh.picker_hints(user_id=1, agent_id="42"))
    assert res["kind"] == "client_recent"
    assert res["label"]
    assert res["product_ids"] == [b, a]


def test_client_recent_dedupes_by_product_keeping_latest(wh, isolated_db):
    """Один товар покупали дважды — в подсказке одна запись, по ПОСЛЕДНЕЙ покупке."""
    db = isolated_db
    a, b = _pids(db, "Товар А", "Товар Б")
    o1 = _order(db, user_id=1, agent_id=7, created_at="2030-01-01 10:00:00")
    _add_item(db, o1, a, created_at="2030-01-01 10:00:00")
    o2 = _order(db, user_id=1, agent_id=7, created_at="2030-01-05 10:00:00")
    _add_item(db, o2, b, created_at="2030-01-05 10:00:00")
    o3 = _order(db, user_id=1, agent_id=7, created_at="2030-01-20 10:00:00")
    _add_item(db, o3, a, created_at="2030-01-20 10:00:00")  # А снова, позже Б

    res = _run(wh.picker_hints(user_id=1, agent_id="7"))
    assert res["kind"] == "client_recent"
    assert res["product_ids"] == [a, b]


def test_client_recent_excludes_cancelled_and_rejected_orders(wh, isolated_db):
    db = isolated_db
    a, b = _pids(db, "Товар А", "Товар Б")
    o1 = _order(db, user_id=1, agent_id=5, status="cancelled")
    _add_item(db, o1, a)
    o2 = _order(db, user_id=1, agent_id=5, status="rejected")
    _add_item(db, o2, b)

    res = _run(wh.picker_hints(user_id=1, agent_id="5"))
    # Ни одной ЖИВОЙ покупки у клиента — фолбэк на «частое у менеджера»,
    # а не пустая подсказка с «отменённым» товаром.
    assert res["kind"] in ("manager_frequent", "none")
    assert a not in res["product_ids"]
    assert b not in res["product_ids"]


# ─── Нет клиента / клиент без истории → частое у менеджера ───────────────────


def test_no_agent_falls_back_to_manager_frequent(wh, isolated_db):
    db = isolated_db
    a, b = _pids(db, "Товар А", "Товар Б")
    o1 = _order(db, user_id=1, created_at="2030-01-01 10:00:00")
    _add_item(db, o1, a, created_at="2030-01-01 10:00:00")
    _add_item(db, o1, a, created_at="2030-01-01 10:00:00")  # А заказывали чаще
    o2 = _order(db, user_id=1, created_at="2030-01-02 10:00:00")
    _add_item(db, o2, b, created_at="2030-01-02 10:00:00")

    res = _run(wh.picker_hints(user_id=1, agent_id=None))
    assert res["kind"] == "manager_frequent"
    assert res["product_ids"][0] == a  # чаще → выше


def test_first_time_client_falls_back_to_manager_frequent(wh, isolated_db):
    """agent_id указан, но у ЭТОГО клиента заказов нет — это не «нет клиента»
    (kind=none), а тот же фолбэк на частое у менеджера, что и без клиента."""
    db = isolated_db
    (a,) = _pids(db, "Товар А")
    o1 = _order(db, user_id=1)
    _add_item(db, o1, a)

    res = _run(wh.picker_hints(user_id=1, agent_id="12345"))
    assert res["kind"] == "manager_frequent"
    assert a in res["product_ids"]


def test_no_history_at_all_returns_none(wh, isolated_db):
    res = _run(wh.picker_hints(user_id=999, agent_id=None))
    assert res == {"kind": "none", "label": "", "product_ids": []}


def test_manager_frequent_scoped_to_own_orders(wh, isolated_db):
    """Заказы ДРУГОГО менеджера не подмешиваются в «часто заказываемое»."""
    db = isolated_db
    a, b = _pids(db, "Товар А", "Товар Б")
    o1 = _order(db, user_id=1)
    _add_item(db, o1, a)
    o2 = _order(db, user_id=2)  # другой менеджер
    _add_item(db, o2, b)

    res = _run(wh.picker_hints(user_id=1, agent_id=None))
    assert res["kind"] == "manager_frequent"
    assert res["product_ids"] == [a]


def test_manager_frequent_respects_30_day_window(wh, isolated_db):
    """Заказ старше ~30 дней в «частое» не попадает — иначе разовая закупка
    полугодовой давности маячила бы наверху вечно."""
    import datetime as dt

    db = isolated_db
    a, b = _pids(db, "Товар А", "Товар Б")
    old = (dt.datetime.now() - dt.timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = (dt.datetime.now() - dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    o1 = _order(db, user_id=1, created_at=old)
    _add_item(db, o1, a, created_at=old)
    o2 = _order(db, user_id=1, created_at=fresh)
    _add_item(db, o2, b, created_at=fresh)

    res = _run(wh.picker_hints(user_id=1, agent_id=None))
    assert res["kind"] == "manager_frequent"
    assert res["product_ids"] == [b]


def test_manager_frequent_excludes_cancelled_and_rejected(wh, isolated_db):
    db = isolated_db
    a, b = _pids(db, "Товар А", "Товар Б")
    o1 = _order(db, user_id=1, status="cancelled")
    _add_item(db, o1, a)
    o2 = _order(db, user_id=1, status="draft")
    _add_item(db, o2, b)

    res = _run(wh.picker_hints(user_id=1, agent_id=None))
    assert res["kind"] == "manager_frequent"
    assert res["product_ids"] == [b]


def test_bad_agent_id_is_ignored_not_500(wh, isolated_db):
    """Мусор в agent_id (не число) не должен уронить ручку — просто фолбэк."""
    db = isolated_db
    (a,) = _pids(db, "Товар А")
    o1 = _order(db, user_id=1)
    _add_item(db, o1, a)

    res = _run(wh.picker_hints(user_id=1, agent_id="не-число"))
    assert res["kind"] == "manager_frequent"
