"""Типы, ограничения, сортировка и разовый `scripts/apply_constraints` на
НАСТОЯЩЕМ Postgres (TEST_PG_URL).

SQLite здесь не свидетель: NUMERIC у него сводится к REAL, внешние ключи и
CHECK добавить к существующей таблице нельзя, ICU-коллаций нет, а asyncpg —
единственный драйвер, который кладёт Python float в NUMERIC двоичным хвостом.

Две главные проверки:
* `test_apply_on_legacy_db_with_violators` — база «как на проде»: количества в
  REAL, отрицательный остаток, сирота, чужой статус, лишний индекс. Dry-run ничего
  не меняет; --apply переводит типы без потери знаков, ставит чистое VALID,
  грязное NOT VALID (кроме остатка — его не ставит вовсе) и не падает.
* `test_constraints_hold_for_real_service_flows` — ограничения ставятся на
  пустую базу, а потом через них проходят настоящие сценарии (продажа,
  отгрузка, оплата, возврат с деньгами, сдача, черновик с историей, касса,
  контейнеры). Ограничение, которое ломает рабочий путь, падает здесь, а не на
  проде.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from decimal import Decimal
from urllib.parse import urlparse

import pytest

from tests import test_money_postgres as _pgm

PG_URL = _pgm.PG_URL
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_PG_URL не задан — нужен живой Postgres")

pg_db = _pgm.pg_db

MGR, BOSS = 1, 2


def _run(coro):
    return asyncio.run(coro)


def _one(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(sql, params)
        return cur.fetchone()


def _exec(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(sql, params)
        conn.commit()


def _data_type(db, table, column) -> str:
    return _one(
        db,
        "SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = %s AND column_name = %s",
        (table, column),
    )["data_type"]


def _constraint(db, name):
    row = _one(db, "SELECT convalidated FROM pg_constraint WHERE conname = %s", (name,))
    return None if row is None else bool(row["convalidated"])


# ─── Количества: NUMERIC и точные параметры ──────────────────────────────────


def test_fresh_schema_quantities_are_numeric(pg_db):
    from services.startup_checks import check_schema

    for table, col in (("order_items", "quantity"), ("order_items", "returned_qty"),
                       ("return_items", "qty"), ("container_items", "expected_qty"),
                       ("container_items", "arrived_qty")):
        assert _data_type(pg_db, table, col) == "numeric", (table, col)
    assert check_schema() == []


def test_fractional_return_closes_order_on_postgres(pg_db):
    """1.1 − 0.9 во float = 0.20000000000000007; на NUMERIC такой остаток не
    пролезал в `returned_qty + qty <= quantity`, и заказ не закрывался."""
    db = pg_db
    oid = db.create_order(MGR, "M", "")
    iid = db.add_order_item(oid, "Кабель", "", 1.1, "м", 10.0)
    for st in ("pending", "approved", "shipped"):
        db.update_order_status(oid, st)

    for qty in (0.9, None):
        if qty is None:
            it = _run(db.get_order_items(oid))[0]
            qty = it["quantity"] - it["returned_qty"]
        ret = _run(db.create_return(oid, "partial", "брак", [(iid, qty, 0)], "no_refund", MGR))
        assert ret["ok"], ret
        assert _run(db.mark_return_goods_received(ret["return_id"], BOSS))["ok"]
        res = _run(db.confirm_return(ret["return_id"], BOSS, "Boss"))
        assert res["ok"], res

    assert _run(db.get_order(oid))["status"] == "returned"
    row = _one(db, "SELECT quantity, returned_qty FROM order_items WHERE id = %s", (iid,))
    assert (row["quantity"], row["returned_qty"]) == (Decimal("1.1"), Decimal("1.1"))
    items = _run(db.get_order_items(oid))
    assert isinstance(items[0]["quantity"], float)
    json.dumps(items)


def test_async_float_param_is_stored_exactly(pg_db):
    """asyncpg клал 2.3 в NUMERIC как 2.29999999999999982236…; теперь — «2.3»."""
    from services import container_receipt, warehouse

    pid = _run(container_receipt.create_product("Трос"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    for q in (0.1, 0.2):
        assert _run(warehouse.create_invoice(
            invoice_type="incoming", warehouse_id=wid,
            items=[{"product_id": pid, "quantity": q, "price_cents": None}],
        ))["ok"]
    row = _one(pg_db, "SELECT quantity::text AS q FROM stock WHERE product_id = %s", (pid,))
    assert row["q"] == "0.3"


def test_container_items_come_back_as_floats(pg_db):
    from services import containers

    c = _run(containers.create_container(number="MSKU1234567", created_by=BOSS))
    cid = c["container_id"] if "container_id" in c else c["id"]
    assert _run(containers.add_item(cid, name="Кабель", expected_qty=2.5))["ok"]
    items = _run(containers.list_items(cid))
    assert items[0]["expected_qty"] == 2.5 and items[0]["arrived_qty"] is None
    json.dumps(items)
    listed = _run(containers.list_containers())
    json.dumps(listed, default=str)


# ─── Сортировка и поиск ──────────────────────────────────────────────────────


def test_names_sort_in_russian_on_postgres(pg_db):
    from services import container_receipt, counterparties, startup_checks, warehouse

    names = ["Zeta", "alfa", "Ёлка", "Абрикос", "Елена", "яблоко"]
    for n in names:
        _run(counterparties.create(n))
        _run(container_receipt.create_product(n))
    got = [r["name"] for r in _run(counterparties.search())]
    # Кириллица — по алфавиту без учёта регистра (Ё — буквой после Е, как в
    # алфавите), латиница отдельным блоком и тоже без учёта регистра. Порядок
    # кодов символов дал бы «Zeta, alfa, Ёлка, Абрикос, Елена, яблоко».
    assert got == ["Абрикос", "Елена", "Ёлка", "яблоко", "alfa", "Zeta"], got
    assert [r["name"] for r in _run(warehouse.get_catalog())] == got
    assert [r["name"] for r in _run(counterparties.search("ЁЛ"))] == ["Елена", "Ёлка"]
    assert [r["name"] for r in _run(warehouse.get_stock())] == got
    assert [r["name"] for r in _run(warehouse.search_products("е"))][:2] == ["Елена", "Ёлка"]
    assert startup_checks.check_collation() == []


def test_categories_sort_in_russian_on_postgres(pg_db):
    """DISTINCT + ORDER BY … COLLATE Postgres отвергает — категории через GROUP BY."""
    from services import container_receipt, warehouse

    for n, cat in (("a", "Трубы"), ("b", "Арматура"), ("c", "Трубы"), ("d", "cable")):
        pid = _run(container_receipt.create_product(n))["product_id"]
        _exec(pg_db, "UPDATE products SET category = %s WHERE id = %s", (cat, pid))
    assert [c["name"] for c in _run(warehouse.get_categories())] == ["Арматура", "Трубы", "cable"]


# ─── Разовый скрипт на базе «как на проде» ───────────────────────────────────


LEGACY_QTY = {"order_items", "return_items", "container_items"}


@pytest.fixture
def pg_legacy(monkeypatch):
    """База, где позиции заказа/возврата/контейнера созданы старым DDL (REAL)."""
    import psycopg2

    name = f"t_legacy_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    url = urlparse(PG_URL)._replace(path=f"/{name}").geturl()
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")
    _pgm._drop_async_pool()
    db = _pgm._reload_modules()
    try:
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            for sql in db._table_ddls():
                table = re.search(r"EXISTS\s+(\w+)", sql).group(1)
                if table in LEGACY_QTY or table == "containers":
                    cur.execute(sql.replace("NUMERIC", "REAL") if table in LEGACY_QTY else sql)
            conn.commit()
        db.init_db()
        db.seed_warehouses()
        yield db
    finally:
        _pgm._drop_async_pool()
        pool = getattr(db, "_pg_connection_pool", None)
        if pool is not None:
            pool.closeall()
        monkeypatch.undo()
        _pgm._reload_modules()
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _seed_violators(db):
    now = db.now_str()
    oid = db.create_order(MGR, "M", "")
    db.add_order_item(oid, "Кабель", "", 2.3, "м", 10.0)
    bogus = db.create_order(MGR, "M", "")
    _exec(db, "UPDATE orders SET status = 'ms_legacy' WHERE id = %s", (bogus,))
    _exec(db, "INSERT INTO containers (number, created_by, created_at) VALUES ('C1', 1, %s)", (now,))
    cid = _one(db, "SELECT id FROM containers WHERE number = 'C1'")["id"]
    _exec(db, "INSERT INTO container_items (container_id, name, expected_qty, arrived_qty) "
              "VALUES (%s, 'Трос', 1234.567, 0.1)", (cid,))
    _exec(db, "INSERT INTO products (name, created_at) VALUES ('Минус', %s)", (now,))
    pid = _one(db, "SELECT id FROM products WHERE name = 'Минус'")["id"]
    # Остаток из снимка МС ушёл в минус — как 32 строки на проде.
    _exec(db, "INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (%s, 1, -4)", (pid,))
    # Сирота: строка накладной без накладной.
    _exec(db, "INSERT INTO invoice_items (invoice_id, product_id, quantity) VALUES (9999, %s, 1)",
          (pid,))
    # Лишний индекс, оставшийся на базе после чистки определений.
    _exec(db, "CREATE INDEX IF NOT EXISTS idx_user_roles_role ON user_roles(role)")
    return oid, pid


def test_apply_on_legacy_db_with_violators(pg_legacy):
    import psycopg2

    from scripts import apply_constraints
    from services.startup_checks import check_schema

    db = pg_legacy
    assert _data_type(db, "order_items", "quantity") == "real"
    assert any("тип колонок" in p for p in check_schema()), "сверка старта видит REAL"
    oid, pid = _seed_violators(db)

    # ── dry-run: отчёт есть, база не тронута ──
    dry = apply_constraints.run(dry_run=True)
    assert dry.planned and not dry.applied and not dry.failed
    assert dry.violations["stock_quantity_chk"] == 1
    assert dry.violations["invoice_items_invoice_fk"] == 1
    assert dry.violations["orders_status_chk"] == 1
    assert _data_type(db, "order_items", "quantity") == "real"
    assert _constraint(db, "payments_status_chk") is None
    assert _one(db, "SELECT 1 AS x FROM pg_indexes WHERE indexname = 'idx_user_roles_role'")
    assert apply_constraints.main([]) == 0  # по умолчанию dry-run и не падает

    # ── apply ──
    rep = apply_constraints.run(dry_run=False)
    assert rep.failed == [], rep.failed
    for table, col in (("order_items", "quantity"), ("order_items", "returned_qty"),
                       ("return_items", "qty"), ("container_items", "expected_qty"),
                       ("container_items", "arrived_qty")):
        assert _data_type(db, table, col) == "numeric", (table, col)
    # Знаки не потеряны: ни хвоста float4, ни обрезки до 6 цифр.
    assert _one(db, "SELECT quantity FROM order_items WHERE order_id = %s", (oid,))["quantity"] \
        == Decimal("2.3")
    row = _one(db, "SELECT expected_qty, arrived_qty FROM container_items")
    assert (row["expected_qty"], row["arrived_qty"]) == (Decimal("1234.567"), Decimal("0.1"))

    assert _constraint(db, "payments_status_chk") is True
    assert _constraint(db, "order_items_order_fk") is True
    assert _constraint(db, "orders_status_chk") is False  # нарушитель — NOT VALID
    assert _constraint(db, "invoice_items_invoice_fk") is False
    assert _constraint(db, "stock_quantity_chk") is None  # не ставится вовсе
    assert any("stock_quantity_chk" in s for s in rep.skipped)
    assert not _one(db, "SELECT 1 AS x FROM pg_indexes WHERE indexname = 'idx_user_roles_role'")
    assert rep.remaining == []

    # NOT VALID уже стережёт новые строки.
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _exec(db, "INSERT INTO invoice_items (invoice_id, product_id, quantity) "
                  "VALUES (12345, %s, 1)", (pid,))
    # А приход на отрицательный остаток работает — ради этого остаток и пропущен.
    _exec(db, "UPDATE stock SET quantity = quantity + 1 WHERE product_id = %s", (pid,))

    # ── повтор: делать нечего, ничего не падает ──
    again = apply_constraints.run(dry_run=False)
    assert again.failed == [] and again.applied == []

    # ── данные исправили — повтор довалидирует ──
    _exec(db, "UPDATE stock SET quantity = 0 WHERE product_id = %s", (pid,))
    _exec(db, "DELETE FROM invoice_items WHERE invoice_id = 9999")
    _exec(db, "UPDATE orders SET status = 'cancelled' WHERE status = 'ms_legacy'")
    fixed = apply_constraints.run(dry_run=False)
    assert fixed.failed == [] and fixed.violations == {}
    for name in ("stock_quantity_chk", "invoice_items_invoice_fk", "orders_status_chk"):
        assert _constraint(db, name) is True, name


# ─── Ограничения против рабочих сценариев ────────────────────────────────────


def _service_flows(db, tag: str) -> None:
    """Сквозные сценарии, которые пишут во все таблицы под ограничениями."""
    from services import accounting as acc
    from services import container_receipt, containers, counterparties, warehouse
    from services.order_workflow import approve_shipment_request, return_order_to_draft, submit_order

    # Учёт включён первым: иначе приход не заводит партий, а отгрузка — фиксаций
    # себестоимости, и ограничения cost_batches/sale_costs нечем проверить.
    boss = acc.Actor(BOSS, "Boss", "boss")
    _run(acc.set_enabled(boss, True))
    pid = _run(container_receipt.create_product(f"Кабель {tag}"))["product_id"]
    cp = _run(counterparties.create(f"Клиент {tag}"))["counterparty_id"]
    wid = _run(warehouse.default_warehouse_id())
    assert _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid, counterparty_id=cp,
        items=[{"product_id": pid, "quantity": 10, "price_cents": 5000}],
    ))["ok"]

    # Продажа в долг → одобрение (списание накладной) → отгрузка → оплата.
    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, str(cp), f"Клиент {tag}")
    iid = db.add_order_item(oid, f"Кабель {tag}", "", 2.5, "м", 100.0, product_id=pid)
    sub = _run(submit_order(oid, MGR, "Manager", payment_type="credit", due_date="2030-01-15"))
    assert sub.get("ok"), sub
    ap = _run(approve_shipment_request(sub["req_id"], BOSS, "Boss", None, pdf_delivery="inline"))
    assert ap.get("ok"), ap
    assert _run(db.mark_order_shipped(oid, BOSS, "Boss"))["ok"]
    ok, pay_id = _run(db.mark_order_paid(oid, MGR, "Manager", amount=100.0))
    assert ok, pay_id
    assert _run(db.confirm_payment(pay_id, BOSS, "Boss"))

    # Сдача наличных по этому заказу.
    dep = _run(db.create_cash_deposit(MGR, 50.0))
    if dep.get("ok"):
        assert _run(db.confirm_cash_deposit(dep["deposit_id"], BOSS, "Boss"))["ok"]

    # «Оплата сразу»: разбивка (наличные USD + карта сумами по курсу) до отгрузки,
    # автоплатёж старого образца заменяется, сдача по наличным, отклонение и
    # повторная сдача — пишут payment_parts / cash_deposit_parts / _currency.
    from services import order_payments

    paid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(paid, str(cp), f"Клиент {tag}")
    db.add_order_item(paid, f"Кабель {tag}", "", 2, "м", 100.0, product_id=pid)
    sub3 = _run(submit_order(paid, MGR, "Manager", payment_type="paid"))
    assert sub3.get("ok"), sub3
    assert _run(approve_shipment_request(sub3["req_id"], BOSS, "Boss", None, pdf_delivery="inline"))["ok"]
    assert _run(db.mark_order_shipped(paid, BOSS, "Boss"))["code"] == "payment_required"
    db.add_payment(MGR, "", "Manager", 200.0, "USD", "Оплата по заказу (отгрузка одобрена)", order_id=paid)
    mgr_actor = order_payments.Actor(MGR, "Manager", "manager")
    # Свой курс менеджеру — только рядом с курсом ЦБ (manual_rate_refusal).
    assert db.set_currency_rate("UZS", 1 / 12700, BOSS)[0]
    rec = _run(order_payments.record_payment_parts(paid, mgr_actor, [
        {"method": "cash", "currency": "USD", "amount": "120"},
        {"method": "card", "currency": "UZS", "amount": "1016000", "rate": "12700"},
    ]))
    assert rec["superseded"] and rec["total_cents"] == 20_000, rec
    assert _run(db.mark_order_shipped(paid, BOSS, "Boss"))["ok"]
    card_pid = next(p["payment_id"] for p in rec["parts"] if p["method"] == "card")
    assert _run(db.confirm_payment(card_pid, BOSS, "Boss"))
    dep1 = _run(db.create_cash_deposit(MGR, 50.0))
    assert dep1["ok"] and dep1["parts"], dep1
    assert _run(db.reject_cash_deposit(dep1["deposit_id"], BOSS, "Boss", "пересчитать"))["ok"]
    dep2 = _run(db.create_cash_deposit(MGR, 120.0, currency="USD"))
    assert dep2["ok"] and dep2["parts"], dep2
    assert _run(db.confirm_cash_deposit(dep2["deposit_id"], BOSS, "Boss"))["ok"]
    assert _run(db.get_order(paid))["paid_confirmed_at"]

    # Возврат с выдачей денег (отрицательная сдача) и приходом на склад.
    ret = _run(db.create_return(oid, "partial", "брак", [(iid, 1.2, 0)], "cash", MGR))
    assert ret["ok"], ret
    assert _run(db.mark_return_goods_received(ret["return_id"], BOSS))["ok"]
    conf = _run(db.confirm_return(ret["return_id"], BOSS, "Boss"))
    assert conf["ok"], conf

    # Черновик с историей (заявка возвращена на доработку) удаляется.
    draft = db.create_order(MGR, "Manager", "")
    db.update_order_agent(draft, str(cp), f"Клиент {tag}")
    db.add_order_item(draft, f"Кабель {tag}", "", 1, "м", 100.0, product_id=pid)
    sub2 = _run(submit_order(draft, MGR, "Manager", payment_type="credit", due_date="2030-01-15"))
    assert sub2.get("ok"), sub2
    back = _run(return_order_to_draft(sub2["req_id"], BOSS, "Boss", "поправьте цену", None))
    assert back.get("ok"), back
    assert _run(db.delete_order(draft, MGR)) is True

    # Касса: счёт с начальным остатком, расход, две сверки за день.
    cash = _run(acc.save_account(boss, {"name": f"Касса {tag}", "kind": "cash",
                                        "currency": "USD", "opening": "100"}))["id"]
    _run(acc.record_expense(boss, {"account_id": cash, "amount": "15.50", "note": "такси",
                                   "idempotency_key": f"e-{tag}"}))
    _run(acc.close_day(boss, {"account_id": cash, "counted": "84.50", "idempotency_key": f"c1-{tag}"}))
    _run(acc.close_day(boss, {"account_id": cash, "counted": "84", "note": "мелочь",
                              "idempotency_key": f"c2-{tag}"}))

    # Контейнер: заведён и удалён; второй — принят на склад и принят повторно.
    gone = _run(containers.create_container(number=f"TMPU{tag}0001", created_by=BOSS))
    gone_id = gone.get("container_id") or gone.get("id")
    assert _run(containers.add_item(gone_id, name="Трос", expected_qty=3.5))["ok"]
    assert _run(containers.delete_container(gone_id, user_id=BOSS))["ok"]

    box = _run(containers.create_container(number=f"RCVU{tag}0001", created_by=BOSS))
    box_id = box.get("container_id") or box.get("id")
    added = _run(containers.add_item(box_id, name=f"Кабель {tag}", expected_qty=4, product_id=pid))
    assert added["ok"], added
    assert _run(containers.mark_arrived(box_id, user_id=BOSS))["ok"]
    item_id = _run(containers.list_items(box_id))[0]["id"]
    assert _run(containers.set_arrived_quantities(box_id, {item_id: 4.5}, user_id=BOSS))["ok"]
    first = _run(container_receipt.receive(box_id, user_id=BOSS))
    assert first.get("ok"), first
    assert _run(containers.set_arrived_quantities(box_id, {item_id: 5}, user_id=BOSS))["ok"]
    second = _run(container_receipt.receive(box_id, user_id=BOSS))
    assert second.get("ok"), second

    # Позиции свободным текстом, решённые перед оприходованием: одна — новый
    # товар (карточка заводится), другая — тёзка существующей карточки с «ё» и
    # двойным пробелом (дубль не заводится), обе уходят в приход.
    free = _run(containers.create_container(number=f"FREE{tag}0001", created_by=BOSS))
    free_id = free["container_id"]
    new_item = _run(containers.add_item(free_id, name=f"Ёрш  трубный {tag}", expected_qty=2))["item_id"]
    old_item = _run(containers.add_item(free_id, name=f"кабель {tag}", expected_qty=1))["item_id"]
    assert _run(containers.mark_arrived(free_id, user_id=BOSS))["ok"]
    assert _run(containers.set_arrived_quantities(
        free_id, {new_item: 2, old_item: 1}, user_id=BOSS))["ok"]
    resolved = _run(container_receipt.resolve_items(
        free_id, {new_item: {"new": True}, old_item: {"new": True}}))
    assert resolved["ok"] and len(resolved["created"]) == 1 and len(resolved["existed"]) == 1, resolved
    got = _run(container_receipt.receive(free_id, user_id=BOSS))
    assert got.get("ok") and got["matched"] == 2 and got["unmatched"] == [], got
    assert _one(db, "SELECT COUNT(*) AS n FROM products WHERE replace(lower(name), 'ё', 'е') LIKE %s",
                (f"%ерш%трубный {tag.lower()}%",))["n"] == 1

    # Техника: прибытие менеджером (статус под CHECK, локация тем же UPDATE).
    from services import machines

    made = _run(machines.create_machine(vin=f"PGVIN{tag}", name="CAT 320D", created_by=BOSS))
    arrived = _run(machines.mark_arrived(made["machine_id"], user_id=MGR, location="Сергели"))
    assert arrived["ok"], arrived

    # Техника: заявки менеджера через одобрение (вид/статус/режим под CHECK,
    # FK заявки → машина и → сделка). Бронь отклонена, рассрочка — доработка,
    # повторная отправка, одобрение и поступление; продажа руководства — сразу.
    from services import machine_deal_requests as mdr

    mid = made["machine_id"]
    mgr = {"actor_id": MGR, "actor_name": "Manager", "actor_role": "manager"}
    bs = {"actor_id": BOSS, "actor_name": "Boss", "actor_role": "boss"}
    booking = _run(mdr.submit(mid, kind="reserve", buyer_name="Покупатель", notify=False, **mgr))
    assert booking["ok"] and booking["pending"], booking
    assert _run(mdr.reject(booking["request_id"], reason="клиент не внёс задаток", **bs))["ok"]
    credit = _run(mdr.submit(mid, kind="credit", price_cents=1_200_000, buyer_name="Иванов",
                             buyer_passport="AA1234567", down_payment_cents=0, months=3,
                             notify=False, **mgr))
    assert credit["ok"] and credit["pending"], credit
    rid = credit["request_id"]
    assert _run(mdr.return_for_rework(rid, reason="взнос хотя бы 10%", **bs))["ok"]
    again = _run(mdr.resubmit(rid, down_payment_cents=120_000, notify=False, **mgr))
    assert again["ok"], again
    done = _run(mdr.approve(rid, **bs))
    assert done["ok"] and done["deal_id"] and done["status"] == "on_credit", done
    assert _run(machines.add_receipt(done["deal_id"], 360_000, user_id=BOSS))["ok"]
    other = _run(machines.create_machine(vin=f"PGSALE{tag}", name="JCB 3CX", created_by=BOSS,
                                         status="in_stock"))
    sold = _run(mdr.submit(other["machine_id"], kind="sale", price_cents=500_000,
                           buyer_name="ООО Стройка", **bs))
    assert sold["ok"] and not sold["pending"] and sold["deal_id"], sold
    third = _run(machines.create_machine(vin=f"PGCANC{tag}", name="XCMG", created_by=BOSS,
                                         status="in_stock"))
    gone_req = _run(mdr.submit(third["machine_id"], kind="sale", price_cents=10_000,
                               buyer_name="Передумал", notify=False, **mgr))
    assert _run(mdr.cancel(gone_req["request_id"], **mgr))["ok"]
    assert _run(machines.delete_machine(third["machine_id"], user_id=BOSS))["ok"]


def test_constraints_hold_for_real_service_flows(pg_db):
    from scripts import apply_constraints

    db = pg_db
    empty = apply_constraints.run(dry_run=False)
    assert empty.failed == [] and empty.violations == {} and empty.not_valid == []
    for chk in apply_constraints._checks():
        assert _constraint(db, chk.name) is True, chk.name
    for fk in apply_constraints.FOREIGN_KEYS:
        assert _constraint(db, fk.name) is True, fk.name

    _service_flows(db, "A")
    # Сценарии действительно дошли до таблиц под ограничениями, а не пропустились.
    for table, where in (
        ("order_shipment", "invoice_id IS NOT NULL"), ("return_items", "TRUE"),
        ("return_receipt", "invoice_id IS NOT NULL"), ("cash_deposit_orders", "TRUE"),
        ("cash_deposits", "amount_cents < 0"), ("acc_day_closes", "TRUE"),
        ("container_receipt", "invoice_id IS NOT NULL"), ("sale_costs", "TRUE"),
        ("cost_batches", "TRUE"), ("payment_parts", "split_from IS NOT NULL"),
        ("cash_deposit_parts", "TRUE"), ("cash_deposit_currency", "TRUE"),
        ("machine_deal_requests", "status = 'approved' AND approval_mode = 'boss'"),
        ("machine_deal_requests", "status = 'approved' AND approval_mode = 'auto'"),
        ("machine_deal_requests", "status = 'rejected'"),
    ):
        n = _one(db, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")["n"]
        assert n > 0, table

    after = apply_constraints.run(dry_run=True)
    assert after.violations == {} and after.planned == [], (after.violations, after.planned)


def test_catalog_picker_search_and_name_matching_on_postgres(pg_db):
    """Шторка выбора товара и сверка имён на настоящем Postgres: ICU-сортировка
    с LIMIT-параметром, подзапрос остатка, артикул с NULL, ё = е в шаблоне
    сравнения имён (`_products_by_name`)."""
    from services import container_receipt, containers, warehouse

    bracket = _run(container_receipt.create_product("Ёлочный кронштейн", unit="компл"))["product_id"]
    # Двойной пробел — как в каталоге, приехавшем из МойСклад (create_product его схлопывает).
    _exec(pg_db, "UPDATE products SET name = %s WHERE id = %s", ("Ёлочный  кронштейн", bracket))
    _run(container_receipt.create_product("Абразив"))
    filt = _run(container_receipt.create_product("Фильтр масляный"))["product_id"]
    _exec(pg_db, "UPDATE products SET sku = %s WHERE id = %s", ("320/04133", filt))
    wid = _run(warehouse.default_warehouse_id())
    assert _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid,
        items=[{"product_id": bracket, "quantity": 2.5, "price_cents": None}],
    ))["ok"]

    browse = _run(warehouse.search_products("", 2, browse=True))
    assert [r["name"] for r in browse] == ["Абразив", "Ёлочный  кронштейн"]
    assert browse[1]["quantity"] == 2.5 and isinstance(browse[1]["quantity"], float)
    json.dumps(browse)
    assert [r["product_id"] for r in _run(warehouse.search_products("04133", browse=True))] == [filt]
    assert [r["product_id"] for r in _run(warehouse.search_products("елоч"))] == [bracket]

    assert [p["id"] for p in _run(container_receipt.same_name_products("ЕЛОЧНЫЙ кронштейн"))] == [bracket]
    again = _run(container_receipt.create_product("елочный кронштейн"))
    assert again["existed"] and again["product_id"] == bracket

    cid = _run(containers.create_container(number="PICK0000001", created_by=BOSS))["container_id"]
    item = _run(containers.add_item(cid, name="ёлочный кронштейн", expected_qty=1))["item_id"]
    matches = _run(container_receipt.catalog_matches(_run(containers.list_items(cid))))
    assert [m["product_id"] for m in matches[item]] == [bracket]
