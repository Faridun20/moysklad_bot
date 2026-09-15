"""
Очередь дел на «Сегодня».

Проверяем не «список собрался», а два свойства, ради которых очередь и
существует: она упорядочена по срочности и роль видит только то, на что может
подействовать. Пункт, ведущий туда, где ручка ответит 403, — тупик, а не
напоминание.

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет.
"""

import asyncio
from datetime import timedelta

from fastapi.testclient import TestClient

import services.roles as roles
from utils.helpers import local_now


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "acc", "Bookkeeper", "bookkeeper")
    db.set_role(4, "wh", "Keeper", "warehouse_keeper")


def _overdue_order(db, user_id=1, days=5):
    """Кредит-заказ, срок оплаты которого уже прошёл."""
    order_id = db.create_order(user_id, "Manager")
    due = (local_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET status = 'approved', payment_type = 'credit', "
                 "due_date = ?, agent_name = 'ООО Ромашка' WHERE id = ?"),
            (due, order_id),
        )
        conn.commit()
    return order_id


def _arrived_container_with_unchecked(db):
    from services import containers

    res = _run(containers.create_container(number="MSKU-1", created_by=2))
    cid = res["container_id"]
    _run(containers.add_item(cid, name="Кабель", expected_qty=100))
    _run(containers.mark_arrived(cid, user_id=2))
    return cid


# ─── Порядок ──────────────────────────────────────────────────────────────────


def test_overdue_money_outranks_everything(isolated_db):
    """Порядок — это и есть ответ на «с чего начать». У просроченного долга срок
    уже нарушен, у несверенного контейнера сроков нет вовсе."""
    from services import work_queue

    db = isolated_db
    _setup(db)
    _overdue_order(db)
    _arrived_container_with_unchecked(db)

    items = _run(work_queue.gather(2, "boss"))
    keys = [i["key"] for i in items]
    assert keys[0] == "overdue_debts"
    assert keys[-1] == "unchecked_containers"


def test_within_one_severity_bigger_number_first(isolated_db):
    from services import work_queue

    db = isolated_db
    _setup(db)
    items = [
        {"key": "a", "count": 1, "severity": "warn"},
        {"key": "b", "count": 9, "severity": "warn"},
        {"key": "c", "count": 3, "severity": "crit"},
    ]
    items.sort(key=lambda it: (work_queue.SEVERITIES.index(it["severity"]), -it["count"]))
    assert [i["key"] for i in items] == ["c", "b", "a"]


def test_empty_queue_is_an_answer_not_an_error(isolated_db):
    from services import work_queue

    db = isolated_db
    _setup(db)
    assert _run(work_queue.gather(2, "boss")) == []


# ─── Роли ─────────────────────────────────────────────────────────────────────


def test_every_item_points_somewhere_the_role_can_open(isolated_db):
    """Счётчик без адреса заставляет искать руками то, о чём сам же сообщил."""
    from services import work_queue

    db = isolated_db
    _setup(db)
    _overdue_order(db)
    _arrived_container_with_unchecked(db)

    for item in _run(work_queue.gather(2, "boss")):
        assert item["screen"], item
        assert item["severity"] in work_queue.SEVERITIES


def test_bookkeeper_sees_only_what_he_can_close(isolated_db):
    """У бухгалтера нет ни долгов, ни контейнеров — только сдачи."""
    from services import work_queue

    db = isolated_db
    _setup(db)
    _overdue_order(db)
    _arrived_container_with_unchecked(db)

    keys = [i["key"] for i in _run(work_queue.gather(3, "bookkeeper"))]
    assert "overdue_debts" not in keys
    assert "unchecked_containers" not in keys
    assert "requests" not in keys


def test_manager_sees_his_own_overdue_not_everyones(isolated_db):
    """Иначе менеджер каждое утро видит чужие просрочки и перестаёт смотреть."""
    from services import work_queue

    db = isolated_db
    _setup(db)
    _overdue_order(db, user_id=1)          # его
    _overdue_order(db, user_id=99)         # чужой

    mine = _run(work_queue.gather(1, "manager"))
    boss = _run(work_queue.gather(2, "boss"))
    mine_n = next(i["count"] for i in mine if i["key"] == "overdue_debts")
    boss_n = next(i["count"] for i in boss if i["key"] == "overdue_debts")
    assert mine_n == 1
    assert boss_n == 2


def _overdue_supplier_invoice(db, day="2020-01-01"):
    """Приход от поставщика с суммой и давно прошедшей датой — долг перед ним."""
    from services import warehouse

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO counterparties (name, type, created_at) VALUES (?, ?, ?)"),
                    ("Shandong Machinery", "supplier", db.now_str()))
        cur.execute(db.q("INSERT INTO products (name, unit, created_at) VALUES (?, ?, ?)"),
                    ("Гидронасос", "шт", db.now_str()))
        conn.commit()
        cur.execute(db.q("SELECT MAX(id) AS id FROM counterparties"))
        sup = int(cur.fetchone()["id"])
        cur.execute(db.q("SELECT MAX(id) AS id FROM products"))
        pid = int(cur.fetchone()["id"])
    res = _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=_run(warehouse.default_warehouse_id()),
        counterparty_id=sup, items=[{"product_id": pid, "quantity": 1, "price_cents": 100_000}],
        invoice_date=day, created_by=2,
    ))
    assert res["ok"], res
    return int(res["invoice_id"])


def test_overdue_supplier_debt_is_a_boss_item(isolated_db):
    """Долг ПЕРЕД поставщиком просрочен так же, как долг клиента, — и стоит
    рядом с ним. Менеджеру пункт не показываем: ручка ответит ему 403."""
    from services import work_queue

    db = isolated_db
    _setup(db)
    _overdue_supplier_invoice(db)

    boss = _run(work_queue.gather(2, "boss"))
    item = next(i for i in boss if i["key"] == "supplier_debts")
    assert item["count"] == 1 and item["severity"] == "crit"
    assert item["screen"] == "money:suppliers"

    mgr_keys = [i["key"] for i in _run(work_queue.gather(1, "manager"))]
    assert "supplier_debts" not in mgr_keys


def test_paid_supplier_invoice_leaves_the_queue(isolated_db):
    from services import supplier_debts, work_queue
    from services.order_payments import Actor

    db = isolated_db
    _setup(db)
    inv = _overdue_supplier_invoice(db)
    _run(supplier_debts.set_terms(Actor(2, "Boss", "boss"), inv, "paid"))

    keys = [i["key"] for i in _run(work_queue.gather(2, "boss"))]
    assert "supplier_debts" not in keys


def test_unchecked_container_counts_once_per_container(isolated_db):
    """Три непосчитанные позиции в одном контейнере — это один контейнер."""
    from services import containers, work_queue

    db = isolated_db
    _setup(db)
    cid = _arrived_container_with_unchecked(db)
    _run(containers.add_item(cid, name="Штекер", expected_qty=5))
    _run(containers.add_item(cid, name="Розетка", expected_qty=7))

    items = _run(work_queue.gather(2, "boss"))
    assert next(i["count"] for i in items if i["key"] == "unchecked_containers") == 1


def test_checked_container_leaves_the_queue(isolated_db):
    from services import containers, work_queue

    db = isolated_db
    _setup(db)
    cid = _arrived_container_with_unchecked(db)
    item_id = _run(containers.list_items(cid))[0]["id"]
    _run(containers.set_arrived_quantities(cid, {item_id: 100}, user_id=2))

    keys = [i["key"] for i in _run(work_queue.gather(2, "boss"))]
    assert "unchecked_containers" not in keys


def test_one_broken_counter_does_not_take_the_queue_down(isolated_db, monkeypatch):
    """Экран с четырьмя пунктами из пяти полезнее, чем ошибка вместо всех пяти."""
    from services import work_queue

    db = isolated_db
    _setup(db)
    _arrived_container_with_unchecked(db)

    async def boom(*a, **kw):
        raise RuntimeError("дебиторка недоступна")

    monkeypatch.setattr(work_queue, "_overdue_debts", boom)
    keys = [i["key"] for i in _run(work_queue.gather(2, "boss"))]
    assert "unchecked_containers" in keys


# ─── Ручка ────────────────────────────────────────────────────────────────────


def _client(monkeypatch):
    import importlib

    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def test_endpoint_answers_the_roles_that_have_no_home(isolated_db, monkeypatch):
    """Раньше «Главная» держалась на /api/home, который кладовщику не отвечает,
    и раздел открывался экраном с ошибкой."""
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    for uid in (2, 3, 4):
        res = client.post("/api/today", json={"initData": str(uid)})
        assert res.status_code == 200, (uid, res.text)
        assert res.json()["ok"] is True


def test_endpoint_total_matches_the_items(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _overdue_order(db)
    body = _client(monkeypatch).post("/api/today", json={"initData": "2"}).json()
    assert body["total"] == sum(i["count"] for i in body["queue"])
    assert body["total"] > 0


def test_boss_decisions_lead_to_one_decisions_screen(isolated_db, monkeypatch):
    """Всё, что ждёт решения руководителя, ведёт в один экран «Решения» — там
    общий бейдж. Менеджер (подтверждает за бухгалтера/кладовщика) — прежнее
    `money:confirm`: его интерфейс не меняли."""
    from services import work_queue

    db = isolated_db
    _setup(db)

    async def counts():
        return {"payments": 2, "deposits": 1, "returns": 3}

    async def pending():
        return [{"id": 1}, {"id": 2}]

    monkeypatch.setattr(db, "count_boss_attention", counts)
    monkeypatch.setattr(db, "get_pending_requests", pending)

    boss = {i["key"]: i for i in _run(work_queue.gather(2, "boss"))}
    for key in ("requests", "payments", "deposits", "returns"):
        assert boss[key]["screen"] == work_queue.DECISIONS_SCREEN == "decisions", key

    mgr = {i["key"]: i for i in _run(work_queue.gather(1, "manager"))}
    assert mgr["deposits"]["screen"] == "money:confirm"
    assert mgr["returns"]["screen"] == "money:confirm"
