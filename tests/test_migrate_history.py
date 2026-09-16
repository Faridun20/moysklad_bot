"""Перенос истории МойСклад → локальные таблицы (scripts/migrate_history_from_moysklad).

Мокаем ГРАНИЦУ с внешним миром — `ms_get`, то есть HTTP к МойСклад. Всё
остальное настоящее: разбор ответов, сопоставление со справочниками, запись
в реальную БД (`isolated_db`) и сверка. Мок самого переноса доказывал бы
только то, что мок работает.

Главный инвариант, ради которого тесты и написаны: **перенос НЕ ДВИГАЕТ
ОСТАТКИ**. `stock` уже перенесён снимком на сегодня и все исторические
отгрузки в себя включает; повторное списание увело бы склад в минус на весь
оборот компании. Проверяется прямым сравнением до/после.
"""

import asyncio

import pytest

import scripts.migrate_history_from_moysklad as mig


# ─── Фикстуры данных МойСклад ────────────────────────────────────────────────

CP_MS = "cp-uuid-1"
P1_MS = "prod-uuid-1"
P2_MS = "prod-uuid-2"

_MS = "https://api.moysklad.ru/api/remap/1.2"


def _currency(ms_id, name, iso, code, *, default, rate, indirect=False, multiplicity=1):
    """Валюта так, как её отдаёт МС (entity/currency и expand=rate.currency).

    `name` у МС — краткое наименование («сум», «доллар»), НЕ ISO-код: код
    лежит в `isoCode`. Прежние фикстуры подставляли {"name": "USD"} и потому
    не ловили, что скрипт читал ISO из `name`.
    """
    return {
        "meta": {"href": f"{_MS}/entity/currency/{ms_id}", "type": "currency",
                 "mediaType": "application/json"},
        "id": ms_id, "name": name, "fullName": name, "code": code, "isoCode": iso,
        "default": default, "rate": rate, "multiplicity": multiplicity,
        "indirect": indirect, "archived": False, "system": True,
        "rateUpdateType": "manual", "margin": 0.0,
    }


# Учётная валюта аккаунта — доллар; сум заведён с обратным курсом («1 USD =
# 12 700 сум»), как его заводят в аккаунтах с долларовым учётом.
USD_CUR = _currency("cur-usd", "доллар", "USD", "840", default=True, rate=1.0)
UZS_CUR = _currency("cur-uzs", "сум", "UZS", "860", default=False, rate=12700.0, indirect=True)


def _rate(cur: dict, value: float | None = None) -> dict:
    """`rate` документа с раскрытой валютой. `value` МС отдаёт, только если ≠ 1."""
    r: dict = {"currency": dict(cur)}
    if value is not None:
        r["value"] = value
    return r


def _pos(ms_id: str, name: str, qty: float, price_minor: int) -> dict:
    return {
        "quantity": qty,
        "price": price_minor,
        "assortment": {"id": ms_id, "name": name, "uom": {"name": "шт"}},
    }


def _order(ms_id="ord-1", name="00001", sum_minor=300000, payed_minor=0, positions=None):
    return {
        "id": ms_id,
        "name": name,
        "moment": "2026-03-14 09:20:00.000",
        "agent": {"meta": {"href": f"https://x/entity/counterparty/{CP_MS}"}, "name": "ООО Ромашка"},
        "state": {"name": "Согласован"},
        "sum": sum_minor,
        "payedSum": payed_minor,
        "shippedSum": 0,
        "rate": _rate(USD_CUR),
        "description": "",
        "positions": {
            "meta": {"size": len(positions or [])},
            "rows": positions if positions is not None else [_pos(P1_MS, "Труба", 3, 100000)],
        },
    }


def _demand(ms_id="dem-1", name="D001", order_ms_id="ord-1", sum_minor=300000, positions=None):
    doc = {
        "id": ms_id,
        "name": name,
        "moment": "2026-03-15 10:00:00.000",
        "agent": {"meta": {"href": f"https://x/entity/counterparty/{CP_MS}"}, "name": "ООО Ромашка"},
        "sum": sum_minor,
        "rate": _rate(USD_CUR),
        "positions": {
            "meta": {"size": len(positions or [])},
            "rows": positions if positions is not None else [_pos(P1_MS, "Труба", 3, 100000)],
        },
    }
    if order_ms_id:
        doc["customerOrder"] = {"meta": {"href": f"https://x/entity/customerorder/{order_ms_id}"}}
    return doc


def _paymentin(ms_id="pay-1", sum_minor=300000, op=("customerorder", "ord-1")):
    doc = {
        "id": ms_id,
        "name": "P001",
        "moment": "2026-03-16 11:00:00.000",
        "agent": {"meta": {"href": f"https://x/entity/counterparty/{CP_MS}"}, "name": "ООО Ромашка"},
        "sum": sum_minor,
        "rate": _rate(USD_CUR),
        "paymentPurpose": "оплата по счёту",
    }
    if op:
        doc["operations"] = [{"meta": {"href": f"https://x/entity/{op[0]}/{op[1]}", "type": op[0]}}]
    return doc


def _supply(ms_id="sup-1", name="S001", sum_minor=200000, positions=None, agent=CP_MS):
    return {
        "id": ms_id,
        "name": name,
        "moment": "2026-02-01 08:00:00.000",
        "agent": {"meta": {"href": f"https://x/entity/counterparty/{agent}"}, "name": "ООО Ромашка"},
        "sum": sum_minor,
        "rate": _rate(USD_CUR),
        "description": "",
        "positions": {
            "meta": {"size": len(positions or [])},
            "rows": positions if positions is not None else [_pos(P1_MS, "Труба", 4, 50000)],
        },
    }


def _paymentout(ms_id="po-1", sum_minor=200000, agent=CP_MS, op=("supply", "sup-1")):
    doc = {
        "id": ms_id,
        "name": "PO001",
        "moment": "2026-02-02 09:00:00.000",
        "agent": {"meta": {"href": f"https://x/entity/counterparty/{agent}"}, "name": "ООО Ромашка"},
        "sum": sum_minor,
        "rate": _rate(USD_CUR),
        "paymentPurpose": "оплата поставщику",
    }
    if op:
        doc["operations"] = [{"meta": {"href": f"https://x/entity/{op[0]}/{op[1]}", "type": op[0]}}]
    return doc


@pytest.fixture
def ms_api(monkeypatch):
    """Мок HTTP-границы: путь → список строк. Пагинация исполняется настоящая."""
    state = {
        "customerorder": [], "demand": [], "paymentin": [],
        "supply": [], "paymentout": [], "extra": {},
        # Кассовые ордера и возвраты — отдельные сущности МС (см. докстринг
        # скрипта); по умолчанию их нет, тесты подкладывают сами.
        "cashin": [], "cashout": [], "salesreturn": [], "purchasereturn": [],
        "currency": [USD_CUR, UZS_CUR],
        # Сотрудники МС (entity/employee) — авторы документов (`owner`).
        "employee": [],
    }

    async def fake_ms_get(path, params=None):
        params = params or {}
        if path in state["extra"]:
            return {"rows": state["extra"][path], "meta": {"size": len(state["extra"][path])}}
        key = path.split("/")[-1]
        rows = state.get(key, [])
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 100))
        page = rows[offset : offset + limit]
        return {"rows": page, "meta": {"size": len(rows)}}

    monkeypatch.setattr(mig, "ms_get", fake_ms_get)
    return state


@pytest.fixture
def seeded(isolated_db):
    """Справочники, как их оставил первый скрипт переноса."""
    db = isolated_db
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, legacy_ms_id, created_at) "
                 "VALUES (?, 'customer', ?, ?)"),
            ("ООО Ромашка", CP_MS, db.now_str()),
        )
        for ms_id, name in ((P1_MS, "Труба"), (P2_MS, "Уголок")):
            cur.execute(
                db.q("INSERT INTO products (name, unit, legacy_ms_id, created_at) "
                     "VALUES (?, 'шт', ?, ?)"),
                (name, ms_id, db.now_str()),
            )
        # Остаток «на сегодня» — как после первого переноса.
        cur.execute(db.q("SELECT id FROM products WHERE legacy_ms_id = ?"), (P1_MS,))
        pid = cur.fetchone()[0]
        cur.execute(db.q("SELECT id FROM warehouses ORDER BY id LIMIT 1"))
        wid = cur.fetchone()[0]
        cur.execute(
            db.q("INSERT INTO stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)"),
            (pid, wid, 50.0),
        )
        conn.commit()
    return db


def _run(ms_api, *, dry_run=False, supplier_history="ledger", balances=None, returns=None,
         orders_owner=None, owner_map=None):
    orders = asyncio.run(mig.pull_orders())
    demands = asyncio.run(mig.pull_demands())
    payments = asyncio.run(mig.pull_payments())
    supplies = asyncio.run(mig.pull_supplies())
    payments_out = asyncio.run(mig.pull_payments_out())
    currencies = asyncio.run(mig.pull_currencies())
    if returns is None:
        returns = asyncio.run(mig.pull_returns())
    return asyncio.run(
        mig.write_history(
            orders, demands, payments, supplies, payments_out,
            currencies=currencies, dry_run=dry_run, supplier_history=supplier_history,
            balances=balances, returns=returns, orders_owner=orders_owner,
            owner_map=owner_map, employees=asyncio.run(mig.pull_employees()),
        )
    )


def _rows(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return [dict(r) for r in cur.fetchall()]


# ─── Главный инвариант ───────────────────────────────────────────────────────


def test_migration_never_moves_stock(seeded, ms_api):
    """Остаток после переноса истории обязан остаться прежним.

    `stock` перенесён снимком на сегодня и уже учитывает эти отгрузки.
    Если перенос проведёт их через warehouse, склад уедет в минус на весь
    исторический оборот — и заметят это не сразу, а на первой отгрузке,
    которая «вдруг» не проходит по остатку.
    """
    before = _rows(seeded, "SELECT product_id, quantity FROM stock ORDER BY product_id")
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand()]
    ms_api["paymentin"] = []

    stats, _, _ = _run(ms_api)

    assert stats["demands"] == 1, "отгрузка должна быть перенесена"
    after = _rows(seeded, "SELECT product_id, quantity FROM stock ORDER BY product_id")
    assert after == before, f"остаток изменился: {before} → {after}"


# ─── Перенос как таковой ─────────────────────────────────────────────────────


def test_order_with_shipment_and_payment(seeded, ms_api):
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand()]
    ms_api["paymentin"] = [_paymentin()]

    stats, unmatched, problems = _run(ms_api)

    assert problems == []
    assert unmatched.total() == 0
    assert (stats["orders"], stats["demands"], stats["payments"]) == (1, 1, 1)

    order = _rows(seeded, "SELECT * FROM orders")[0]
    assert order["ms_customerorder_id"] == "ord-1"
    assert order["status"] == "shipped"
    assert order["agent_name"] == "ООО Ромашка"
    assert order["currency"] == "USD"
    # agent_id — НАШ id контрагента строкой (конвенция после backfill).
    cp_id = _rows(seeded, "SELECT id FROM counterparties")[0]["id"]
    assert order["agent_id"] == str(cp_id)

    items = _rows(seeded, "SELECT * FROM order_items")
    assert len(items) == 1
    assert items[0]["quantity"] == 3
    assert items[0]["price_cents"] == 100000, "цена МС уже в минорных единицах"

    links = _rows(seeded, "SELECT * FROM order_item_products")
    assert len(links) == 1, "позиция должна быть связана с карточкой товара"

    inv = _rows(seeded, "SELECT * FROM invoices")[0]
    assert inv["type"] == "outgoing"
    assert inv["invoice_number"] == "MS-D-D001", "своя серия номеров, не из счётчика"
    assert inv["invoice_date"] == "2026-03-15"

    shipment = _rows(seeded, "SELECT * FROM order_shipment")[0]
    assert shipment["order_id"] == order["id"]
    assert shipment["invoice_id"] == inv["id"]

    pay = _rows(seeded, "SELECT * FROM payments")[0]
    assert pay["order_id"] == order["id"]
    assert pay["status"] == "confirmed", "деньги в МС уже проведены"
    assert pay["amount_cents"] == 300000
    assert pay["ms_paymentin_id"] == "pay-1"


def test_invoice_counter_untouched(seeded, ms_api):
    """Исторические накладные не съедают номера живой нумерации."""
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand()]
    _run(ms_api)
    assert _rows(seeded, "SELECT * FROM invoice_counters") == []


def test_dry_run_writes_nothing(seeded, ms_api):
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand()]
    ms_api["paymentin"] = [_paymentin()]

    stats, _, _ = _run(ms_api, dry_run=True)

    assert stats["orders"] == 1, "отчёт считается по реальной записи"
    assert _rows(seeded, "SELECT * FROM orders") == []
    assert _rows(seeded, "SELECT * FROM payments") == []
    assert _rows(seeded, "SELECT * FROM invoices") == []


def test_rerun_is_idempotent(seeded, ms_api):
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand()]
    ms_api["paymentin"] = [_paymentin()]

    _run(ms_api)
    _run(ms_api)

    assert len(_rows(seeded, "SELECT * FROM orders")) == 1
    assert len(_rows(seeded, "SELECT * FROM order_items")) == 1
    assert len(_rows(seeded, "SELECT * FROM invoices")) == 1
    assert len(_rows(seeded, "SELECT * FROM invoice_items")) == 1
    assert len(_rows(seeded, "SELECT * FROM payments")) == 1


# ─── Долг против оплаты ──────────────────────────────────────────────────────


def test_unpaid_order_becomes_credit_debt(seeded, ms_api):
    """Недоплаченный заказ обязан попасть в список должников."""
    ms_api["customerorder"] = [_order(sum_minor=300000, payed_minor=100000)]
    ms_api["demand"] = []
    ms_api["paymentin"] = [_paymentin(sum_minor=100000)]

    _run(ms_api)

    debtors = _rows(
        seeded,
        "SELECT * FROM orders WHERE payment_type = 'credit' AND paid_confirmed_at IS NULL",
    )
    assert len(debtors) == 1
    assert debtors[0]["paid_at"] is None


def test_fully_paid_order_is_closed(seeded, ms_api):
    ms_api["customerorder"] = [_order(sum_minor=300000, payed_minor=300000)]
    ms_api["demand"] = []
    ms_api["paymentin"] = [_paymentin()]

    _run(ms_api)

    order = _rows(seeded, "SELECT * FROM orders")[0]
    assert order["payment_type"] == "paid"
    assert order["paid_confirmed_at"] is not None
    assert order["payment_confirmed"] == 1
    assert _rows(
        seeded, "SELECT * FROM orders WHERE payment_type = 'credit' AND paid_confirmed_at IS NULL"
    ) == []


def test_due_date_is_not_invented(seeded, ms_api):
    """В МС срока оплаты нет, и выдумывать его нельзя: подставленная дата
    объявила бы просроченным весь исторический долг разом."""
    ms_api["customerorder"] = [_order(payed_minor=0)]
    _run(ms_api)
    assert _rows(seeded, "SELECT due_date FROM orders")[0]["due_date"] is None


# ─── «Не удалось сопоставить» вместо угадывания ──────────────────────────────


def test_demand_without_order_becomes_a_sale(seeded, ms_api):
    """Отгрузка без заказа-основания СТАНОВИТСЯ продажей, а не выбрасывается.

    В этом аккаунте так оформляют почти все продажи (26 заказов против 421
    отгрузки). Прежнее поведение — «в отчёт и мимо» — теряло 94% истории.
    Догадки тут нет: у отгрузки свой контрагент, дата, позиции и сумма.
    """
    ms_api["demand"] = [_demand(ms_id="dem-x", name="D009", order_ms_id=None)]

    stats, unmatched, _ = _run(ms_api)

    assert stats["orders_from_demand"] == 1
    assert stats["demands"] == 1
    order = _rows(seeded, "SELECT * FROM orders")[0]
    assert order["ms_demand_id"] == "dem-x"
    assert order["ms_customerorder_id"] is None
    assert order["status"] == "shipped"
    assert "продажа по отгрузке D009" in order["comment"]
    assert len(_rows(seeded, "SELECT * FROM order_items")) == 1
    assert len(_rows(seeded, "SELECT * FROM invoices")) == 1
    assert len(_rows(seeded, "SELECT * FROM order_shipment")) == 1
    assert not any("без заказа" in k for k in unmatched.buckets)


def test_sale_from_demand_is_idempotent(seeded, ms_api):
    ms_api["demand"] = [_demand(ms_id="dem-x", name="D009", order_ms_id=None)]
    _run(ms_api)
    _run(ms_api)
    assert len(_rows(seeded, "SELECT * FROM orders")) == 1
    assert len(_rows(seeded, "SELECT * FROM order_items")) == 1
    assert len(_rows(seeded, "SELECT * FROM invoices")) == 1


def test_payment_without_basis_is_allocated_fifo(seeded, ms_api):
    """Платёж без документа-основания гасит долг СВОЕГО контрагента по FIFO.

    Соглашение, а не факт из МС, — поэтому оно помечено в комментарии строки.
    Без него 243 платежа не гасили бы ничего, и клиенты выглядели бы
    должниками на всю сумму отгрузок.
    """
    ms_api["customerorder"] = [_order(sum_minor=300000)]
    ms_api["paymentin"] = [_paymentin(ms_id="pay-x", op=None)]

    stats, _, _ = _run(ms_api)

    assert stats["payments_fifo"] == 1
    assert stats.get("payments_unlinked", 0) == 0
    pay = _rows(seeded, "SELECT * FROM payments")[0]
    order_id = _rows(seeded, "SELECT id FROM orders")[0]["id"]
    assert pay["order_id"] == order_id
    assert "FIFO" in pay["comment"], "происхождение привязки обязано быть видно"


def test_fifo_pays_oldest_order_first(seeded, ms_api):
    """Порядок гашения — от старых заказов к новым."""
    old = _order(ms_id="ord-old", name="001", sum_minor=100000,
                 positions=[_pos(P1_MS, "Труба", 1, 100000)])
    old["moment"] = "2026-01-01 10:00:00.000"
    new = _order(ms_id="ord-new", name="002", sum_minor=100000,
                 positions=[_pos(P1_MS, "Труба", 1, 100000)])
    new["moment"] = "2026-05-01 10:00:00.000"
    ms_api["customerorder"] = [new, old]  # порядок выгрузки намеренно обратный
    ms_api["paymentin"] = [_paymentin(ms_id="pay-x", sum_minor=100000, op=None)]

    _run(ms_api)

    paid = _rows(seeded, "SELECT o.ms_customerorder_id FROM payments p "
                         "JOIN orders o ON o.id = p.order_id")
    assert paid[0]["ms_customerorder_id"] == "ord-old"


def test_fifo_splits_one_payment_across_orders(seeded, ms_api):
    """Платёж больше одного заказа гасит следующий: order_id — одно поле на
    строку, поэтому части пишутся отдельными строками с суффиксом в ms-id."""
    a = _order(ms_id="ord-a", name="001", sum_minor=100000,
               positions=[_pos(P1_MS, "Труба", 1, 100000)])
    a["moment"] = "2026-01-01 10:00:00.000"
    b = _order(ms_id="ord-b", name="002", sum_minor=100000,
               positions=[_pos(P1_MS, "Труба", 1, 100000)])
    b["moment"] = "2026-02-01 10:00:00.000"
    ms_api["customerorder"] = [a, b]
    ms_api["paymentin"] = [_paymentin(ms_id="pay-x", sum_minor=150000, op=None)]

    stats, _, _ = _run(ms_api)

    assert stats["payments_fifo_parts"] == 2
    rows = _rows(seeded, "SELECT amount_cents, ms_paymentin_id FROM payments "
                         "ORDER BY ms_paymentin_id")
    assert [r["amount_cents"] for r in rows] == [100000, 50000]
    assert [r["ms_paymentin_id"] for r in rows] == ["pay-x", "pay-x#2"]
    assert sum(r["amount_cents"] for r in rows) == 150000, "сумма частей = сумме платежа"


def test_split_payment_rerun_does_not_duplicate(seeded, ms_api):
    a = _order(ms_id="ord-a", name="001", sum_minor=100000,
               positions=[_pos(P1_MS, "Труба", 1, 100000)])
    b = _order(ms_id="ord-b", name="002", sum_minor=100000,
               positions=[_pos(P1_MS, "Труба", 1, 100000)])
    b["moment"] = "2026-02-01 10:00:00.000"
    ms_api["customerorder"] = [a, b]
    ms_api["paymentin"] = [_paymentin(ms_id="pay-x", sum_minor=150000, op=None)]

    _run(ms_api)
    _run(ms_api)

    rows = _rows(seeded, "SELECT amount_cents FROM payments")
    assert len(rows) == 2
    assert sum(r["amount_cents"] for r in rows) == 150000


def test_payment_from_unknown_counterparty_is_reported(seeded, ms_api):
    """Контрагента не угадываем: привязать платёж не к чему."""
    ms_api["customerorder"] = [_order()]
    p = _paymentin(ms_id="pay-x", op=None)
    p["agent"] = {"meta": {"href": "https://x/entity/counterparty/cp-UNKNOWN"}, "name": "Кто-то"}
    ms_api["paymentin"] = [p]

    stats, unmatched, _ = _run(ms_api)

    assert stats["payments_unlinked"] == 1
    assert any("контрагента нет в справочнике" in k for k in unmatched.buckets)
    assert _rows(seeded, "SELECT * FROM payments") == []


def test_payment_currency_must_match_the_order(seeded, ms_api):
    """Кросс-валютное гашение запрещено: конверсии при закрытии заказа нет,
    и UZS-платёж закрыл бы USD-заказ по номиналу копеек."""
    ms_api["customerorder"] = [_order(sum_minor=300000)]
    p = _paymentin(ms_id="pay-x", sum_minor=300000, op=None)
    p["rate"] = _rate(UZS_CUR, 12700.0)
    ms_api["paymentin"] = [p]

    stats, unmatched, _ = _run(ms_api)

    assert stats["payments_unlinked"] == 1
    assert any("непогашенных заказов в этой валюте" in k for k in unmatched.buckets)


def test_payment_linked_through_demand(seeded, ms_api):
    """Платёж, привязанный к ОТГРУЗКЕ, выходит на её заказ — это не угадывание,
    связь documented: demand.customerOrder."""
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand()]
    ms_api["paymentin"] = [_paymentin(op=("demand", "dem-1"))]

    stats, _, _ = _run(ms_api)

    assert stats["payments"] == 1
    order_id = _rows(seeded, "SELECT id FROM orders")[0]["id"]
    assert _rows(seeded, "SELECT order_id FROM payments")[0]["order_id"] == order_id


def test_position_without_product_card_is_reported(seeded, ms_api):
    """Позиция без карточки не уходит в накладную: product_id там NOT NULL,
    а привязка наугад — это приход/расход не на тот товар."""
    ms_api["customerorder"] = [
        _order(positions=[_pos("prod-UNKNOWN", "Неизвестный", 1, 5000)])
    ]
    ms_api["demand"] = [
        _demand(positions=[_pos("prod-UNKNOWN", "Неизвестный", 1, 5000)])
    ]

    stats, unmatched, _ = _run(ms_api)

    assert any("без карточки товара" in k for k in unmatched.buckets)
    # Строка заказа сохраняется (название видно), а в накладную не попадает.
    assert len(_rows(seeded, "SELECT * FROM order_items")) == 1
    assert _rows(seeded, "SELECT * FROM order_item_products") == []
    assert _rows(seeded, "SELECT * FROM invoice_items") == []


def test_order_without_counterparty_is_reported(seeded, ms_api):
    o = _order()
    o["agent"] = {"meta": {"href": "https://x/entity/counterparty/cp-UNKNOWN"}, "name": "Кто-то"}
    ms_api["customerorder"] = [o]

    _, unmatched, _ = _run(ms_api)

    assert any("контрагента нет в справочнике" in k for k in unmatched.buckets)
    assert _rows(seeded, "SELECT agent_id FROM orders")[0]["agent_id"] is None


# ─── Структурные ограничения, о которых нельзя молчать ───────────────────────


def test_multiple_demands_per_order_are_all_kept_and_counted(seeded, ms_api):
    """order_shipment — PK по order_id, то есть одна строка на заказ. Накладные
    при этом переносятся все: состав частичных отгрузок не теряется."""
    ms_api["customerorder"] = [_order(sum_minor=600000,
                                      positions=[_pos(P1_MS, "Труба", 6, 100000)])]
    ms_api["demand"] = [
        _demand(ms_id="dem-1", name="D001", positions=[_pos(P1_MS, "Труба", 2, 100000)]),
        _demand(ms_id="dem-2", name="D002", positions=[_pos(P1_MS, "Труба", 4, 100000)]),
    ]

    stats, _, problems = _run(ms_api)

    assert stats["demands"] == 2
    assert stats["multi_demand_orders"] == 1
    assert len(_rows(seeded, "SELECT * FROM invoices")) == 2
    assert len(_rows(seeded, "SELECT * FROM order_shipment")) == 1
    assert problems == [], "две отгрузки в сумме равны заказу — расхождения нет"


# ─── Сверка ──────────────────────────────────────────────────────────────────


def test_overshipment_is_flagged(seeded, ms_api):
    """Отгружено больше, чем заказано — признак привязки не к тому заказу."""
    ms_api["customerorder"] = [_order(sum_minor=100000,
                                      positions=[_pos(P1_MS, "Труба", 1, 100000)])]
    ms_api["demand"] = [_demand(positions=[_pos(P1_MS, "Труба", 5, 100000)])]

    _, _, problems = _run(ms_api)

    assert any("отгружено" in p for p in problems)


def test_overpayment_leaves_remainder_reported_not_written(seeded, ms_api):
    """Денег больше, чем выставлено контрагенту: остаток НЕ пишется никуда и
    уходит в отчёт. Раздать его несуществующим заказам было бы выдумкой."""
    ms_api["customerorder"] = [_order(sum_minor=100000,
                                      positions=[_pos(P1_MS, "Труба", 1, 100000)])]
    ms_api["paymentin"] = [_paymentin(ms_id="pay-x", sum_minor=500000, op=None)]

    stats, unmatched, problems = _run(ms_api)

    assert problems == [], "переплата — не расхождение переноса"
    assert stats["payments_overflow_cents"] == 400000
    assert any("переплата" in k for k in unmatched.buckets)
    assert sum(r["amount_cents"] for r in _rows(seeded, "SELECT amount_cents FROM payments")) \
        == 100000, "записано ровно столько, сколько было на что отнести"


def test_fifo_closes_order_and_marks_it_paid(seeded, ms_api):
    """Полностью покрытый заказ закрывается, недопокрытый остаётся долгом."""
    full = _order(ms_id="ord-a", name="001", sum_minor=100000,
                  positions=[_pos(P1_MS, "Труба", 1, 100000)])
    full["moment"] = "2026-01-01 10:00:00.000"   # старше — гасится первым
    part = _order(ms_id="ord-b", name="002", sum_minor=100000,
                  positions=[_pos(P1_MS, "Труба", 1, 100000)])
    part["moment"] = "2026-02-01 10:00:00.000"
    ms_api["customerorder"] = [full, part]
    ms_api["paymentin"] = [_paymentin(ms_id="pay-x", sum_minor=130000, op=None)]

    _run(ms_api)

    closed = _rows(seeded, "SELECT ms_customerorder_id FROM orders "
                           "WHERE payment_type = 'paid' AND paid_confirmed_at IS NOT NULL")
    debt = _rows(seeded, "SELECT ms_customerorder_id FROM orders "
                         "WHERE payment_type = 'credit' AND paid_confirmed_at IS NULL")
    assert [r["ms_customerorder_id"] for r in closed] == ["ord-a"]
    assert [r["ms_customerorder_id"] for r in debt] == ["ord-b"]


def test_positions_sum_mismatch_with_ms_document_is_flagged(seeded, ms_api):
    """Сумма позиций разошлась с суммой документа МС — значит часть строк
    не приехала (обрезанный MetaArray) или цена разобрана неверно."""
    ms_api["customerorder"] = [_order(sum_minor=999999,
                                      positions=[_pos(P1_MS, "Труба", 1, 100000)])]

    _, _, problems = _run(ms_api)

    assert any("расходится с суммой" in p for p in problems)


def test_truncated_positions_are_refetched(seeded, ms_api):
    """Вложенный positions обрезается на 100 строках БЕЗ ошибки. Хвост обязан
    дотягиваться, иначе заказ молча теряет часть состава."""
    head = [_pos(P1_MS, "Труба", 1, 1000)]
    full = head + [_pos(P2_MS, "Уголок", 2, 2000)]
    o = _order(sum_minor=5000, positions=head)
    o["positions"]["meta"]["size"] = 2  # в документе 2 строки, приехала 1
    ms_api["customerorder"] = [o]
    ms_api["extra"]["entity/customerorder/ord-1/positions"] = full

    stats, _, _ = _run(ms_api)

    assert stats["order_items"] == 2, "хвост позиций должен быть дотянут"


# ─── Закупочная сторона ──────────────────────────────────────────────────────


def test_supply_becomes_incoming_invoice_without_moving_stock(seeded, ms_api):
    """Приход — тот же принцип, что расход: документ переносим, остаток нет.

    Приход уже сидит в снимке `stock`; повторное оприходование удвоило бы
    его на весь закупочный оборот.
    """
    before = _rows(seeded, "SELECT product_id, quantity FROM stock ORDER BY product_id")
    ms_api["supply"] = [_supply()]

    stats, _, problems = _run(ms_api)

    assert stats["supplies"] == 1
    assert problems == []
    inv = _rows(seeded, "SELECT * FROM invoices")[0]
    assert inv["type"] == "incoming"
    assert inv["invoice_number"] == "MS-S-S001", "своя серия для прихода"
    assert len(_rows(seeded, "SELECT * FROM invoice_items")) == 1
    assert _rows(seeded, "SELECT product_id, quantity FROM stock ORDER BY product_id") == before


def test_payment_out_goes_to_supplier_payments_not_payments(seeded, ms_api):
    """Платёж поставщику НЕ должен попадать в `payments`.

    Там деньги ОТ клиентов, и на них считается вся дебиторка: исходящий
    платёж уменьшил бы долг клиента на сумму, выплаченную поставщику.
    """
    ms_api["supply"] = [_supply()]
    ms_api["paymentout"] = [_paymentout()]

    stats, _, _ = _run(ms_api)

    assert stats["payments_out"] == 1
    assert _rows(seeded, "SELECT * FROM payments") == [], "в payments ничего исходящего"
    sp = _rows(seeded, "SELECT * FROM supplier_payments")[0]
    assert sp["amount_cents"] == 200000
    assert sp["ms_paymentout_id"] == "po-1"
    cp_id = _rows(seeded, "SELECT id FROM counterparties")[0]["id"]
    assert sp["counterparty_id"] == cp_id
    # Платёж привязан к поступлению, на которое ссылается в МС.
    inv_id = _rows(seeded, "SELECT id FROM invoices WHERE type = 'incoming'")[0]["id"]
    assert sp["invoice_id"] == inv_id


def test_payment_out_without_supplier_is_reported(seeded, ms_api):
    ms_api["paymentout"] = [_paymentout(ms_id="po-x", agent="cp-UNKNOWN", op=None)]

    stats, unmatched, _ = _run(ms_api)

    assert stats["payments_out_unlinked"] == 1
    assert stats.get("payments_out", 0) == 0
    assert any("поставщика нет в справочнике" in k for k in unmatched.buckets)
    assert _rows(seeded, "SELECT * FROM supplier_payments") == []


def test_supply_and_sales_do_not_collide_on_invoice_numbers(seeded, ms_api):
    """Отгрузка и поступление с ОДИНАКОВЫМ номером документа в МС — законно
    (нумерация там своя на каждый тип), а `invoices.invoice_number` UNIQUE.
    Разные префиксы разводят их."""
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand(name="X1")]
    ms_api["supply"] = [_supply(name="X1")]

    stats, _, _ = _run(ms_api)

    numbers = {r["invoice_number"] for r in _rows(seeded, "SELECT invoice_number FROM invoices")}
    assert numbers == {"MS-D-X1", "MS-S-X1"}
    assert stats["demands"] == 1 and stats["supplies"] == 1


def test_supply_rerun_is_idempotent(seeded, ms_api):
    ms_api["supply"] = [_supply()]
    ms_api["paymentout"] = [_paymentout()]

    _run(ms_api)
    _run(ms_api)

    assert len(_rows(seeded, "SELECT * FROM invoices WHERE type = 'incoming'")) == 1
    assert len(_rows(seeded, "SELECT * FROM invoice_items")) == 1
    assert len(_rows(seeded, "SELECT * FROM supplier_payments")) == 1


def test_supply_positions_mismatch_is_flagged(seeded, ms_api):
    ms_api["supply"] = [_supply(sum_minor=999999,
                                positions=[_pos(P1_MS, "Труба", 1, 50000)])]

    _, _, problems = _run(ms_api)

    assert any("поступление" in p and "расходится" in p for p in problems)


def test_advance_to_supplier_is_not_an_error(seeded, ms_api):
    """Выплачено больше, чем поставлено, — это аванс, а не ошибка переноса.

    Помечать его расхождением значило бы утопить настоящие проблемы в шуме.
    """
    ms_api["supply"] = [_supply(sum_minor=200000,
                                positions=[_pos(P1_MS, "Труба", 4, 50000)])]
    ms_api["paymentout"] = [_paymentout(sum_minor=900000)]

    stats, _, problems = _run(ms_api)

    assert problems == []
    assert stats["payments_out"] == 1


# ─── Диагностика --explain ───────────────────────────────────────────────────


def test_explain_order_shows_positions_and_delta(seeded, ms_api, caplog):
    """--explain печатает позиции заказа и его отгрузок и считает превышение.

    Ради этого он и нужен: «отгружено больше, чем заказано» на итоговых суммах
    не объясняет, ЧТО именно разошлось, — ответ виден только построчно.
    """
    import logging

    ms_api["customerorder"] = [
        _order(name="00003", sum_minor=53000, positions=[_pos(P1_MS, "Труба", 1, 53000)])
    ]
    ms_api["demand"] = [
        _demand(name="D77", positions=[_pos(P1_MS, "Труба", 1, 131850)])
    ]

    with caplog.at_level(logging.INFO, logger="ms_history"):
        rc = asyncio.run(mig.explain_order("00003"))

    assert rc == 0
    text = caplog.text
    assert "ЗАКАЗ 00003" in text
    assert "Труба" in text, "позиции должны печататься построчно"
    assert "ПРЕВЫШЕНИЕ" in text
    assert "788.50" in text, "разница 1318.50 − 530.00"


def test_explain_unknown_order_fails_loudly(seeded, ms_api):
    ms_api["customerorder"] = [_order(name="00001")]
    assert asyncio.run(mig.explain_order("нет-такого")) == 1


def test_explain_writes_nothing(seeded, ms_api):
    ms_api["customerorder"] = [_order(name="00003")]
    ms_api["demand"] = [_demand()]
    asyncio.run(mig.explain_order("00003"))
    assert _rows(seeded, "SELECT * FROM orders") == []
    assert _rows(seeded, "SELECT * FROM invoices") == []


# ─── Валюта и исторический курс (P0-1) ───────────────────────────────────────


def _uzs_sale(ms_id="dem-uzs", name="D-UZS", moment="2026-03-10 12:00:00.000", value=12650.0):
    """Продажа в сумах в аккаунте с долларовым учётом: 10 шт по 12 650.00 сум."""
    d = _demand(ms_id=ms_id, name=name, order_ms_id=None, sum_minor=12650000,
                positions=[_pos(P1_MS, "Труба", 10, 1265000)])
    d["moment"] = moment
    d["rate"] = _rate(UZS_CUR, value)
    return d


def test_uzs_document_keeps_iso_currency_and_its_own_rate(seeded, ms_api):
    """Сумовая отгрузка пишется в UZS с курсом ДОКУМЕНТА, а не как доллары.

    Раньше валюта читалась из `rate.currency.name` («сум» или пусто без
    expand) → подставлялась база USD, и 126 500 сум превращались в 126 500 $.
    Курс `rate.value` игнорировался, пересчёт шёл по сегодняшнему.
    """
    ms_api["demand"] = [_uzs_sale()]
    p = _paymentin(ms_id="pay-uzs", sum_minor=12650000, op=("demand", "dem-uzs"))
    p["rate"] = _rate(UZS_CUR, 12600.0)
    ms_api["paymentin"] = [p]

    stats, _, problems = _run(ms_api)

    assert problems == []
    order = _rows(seeded, "SELECT * FROM orders")[0]
    assert order["currency"] == "UZS"
    # Семантика проекта: 1 сум = fx долларов. Обратный курс МС 12 650 → 1/12 650.
    assert order["fx_rate_to_base"] == pytest.approx(1 / 12650.0, rel=1e-9)
    inv = _rows(seeded, "SELECT * FROM invoices")[0]
    assert inv["currency"] == "UZS"
    assert inv["total_amount_cents"] == 12650000
    pay = _rows(seeded, "SELECT * FROM payments")[0]
    assert (pay["currency"], pay["amount_cents"]) == ("UZS", 12650000)
    assert pay["fx_rate_to_base"] == pytest.approx(1 / 12600.0, rel=1e-9)

    # И пересчёт в доллары даёт ровно то, что было в тот день: 126 500 сум / 12 650.
    from services.database import convert_to_base_at

    assert convert_to_base_at(126500.0, "UZS", order["fx_rate_to_base"]) == pytest.approx(10.0)


def test_currency_is_resolved_by_dictionary_without_expand(seeded, ms_api):
    """Если expand не раскрыл валюту (только meta), ISO берётся из справочника
    по UUID — а не пустая строка, которая раньше означала «база USD»."""
    d = _uzs_sale()
    d["rate"] = {"currency": {"meta": {"href": f"{_MS}/entity/currency/cur-uzs",
                                       "type": "currency"}}, "value": 12650.0}
    ms_api["demand"] = [d]

    _run(ms_api)

    assert _rows(seeded, "SELECT currency FROM invoices")[0]["currency"] == "UZS"


def test_misread_rate_semantics_stops_even_dry_run(seeded, ms_api):
    """Курс, расходящийся со справочником на порядки, — неверно понятая
    семантика (например, МС отдал уже нормированный курс). Не пишем и не
    показываем «успешный» предпросмотр: останавливаемся с объяснением."""
    ms_api["demand"] = [_uzs_sale(value=0.0000790)]

    with pytest.raises(mig.MigrationStop, match="расходится со справочником"):
        _run(ms_api, dry_run=True)
    with pytest.raises(mig.MigrationStop):
        _run(ms_api)
    assert _rows(seeded, "SELECT * FROM orders") == []
    assert _rows(seeded, "SELECT * FROM invoices") == []


def test_unknown_currency_stops(seeded, ms_api):
    d = _uzs_sale()
    d["rate"] = {"currency": {"meta": {"href": f"{_MS}/entity/currency/cur-eur"}}}
    ms_api["demand"] = [d]

    with pytest.raises(mig.MigrationStop, match="не найдена в справочнике"):
        _run(ms_api)
    assert _rows(seeded, "SELECT * FROM orders") == []


def _uzs_account(ms_api):
    """Аккаунт с учётом в сумах: курс документа приведён к сумам, а база
    проекта — доллар, и звено «сум → доллар» берётся из архива курсов."""
    ms_api["currency"] = [
        _currency("cur-uzs", "сум", "UZS", "860", default=True, rate=1.0),
        _currency("cur-usd", "доллар", "USD", "840", default=False, rate=12700.0),
    ]


def test_account_in_uzs_uses_rate_archive_for_base(seeded, ms_api):
    _uzs_account(ms_api)
    uzs = _demand(ms_id="dem-uzs", name="D1", order_ms_id=None, sum_minor=12600000,
                  positions=[_pos(P1_MS, "Труба", 10, 1260000)])
    uzs["moment"] = "2026-03-12 12:00:00.000"
    uzs["rate"] = {"currency": dict(ms_api["currency"][0])}  # учётная: value нет
    usd = _demand(ms_id="dem-usd", name="D2", order_ms_id=None)
    usd["rate"] = {"currency": dict(ms_api["currency"][1]), "value": 12650.0}
    ms_api["demand"] = [uzs, usd]
    with seeded.get_conn() as conn:
        cur = seeded.get_cursor(conn)
        cur.execute(
            seeded.q("INSERT INTO currency_rate_daily (currency_code, rate_date, rate_to_base, "
                     "source, created_at) VALUES ('UZS', '2026-03-10', ?, 'cbu', ?)"),
            (1 / 12600.0, seeded.now_str()),
        )
        conn.commit()

    _run(ms_api)

    fx = {r["ms_demand_id"]: (r["currency"], r["fx_rate_to_base"])
          for r in _rows(seeded, "SELECT ms_demand_id, currency, fx_rate_to_base FROM orders")}
    assert fx["dem-usd"] == ("USD", 1.0)
    assert fx["dem-uzs"][0] == "UZS"
    assert fx["dem-uzs"][1] == pytest.approx(1 / 12600.0), "курс ближайшего раннего дня архива"


def test_account_in_uzs_without_archive_stops(seeded, ms_api):
    _uzs_account(ms_api)
    d = _demand(ms_id="dem-uzs", name="D1", order_ms_id=None)
    d["rate"] = {"currency": dict(ms_api["currency"][0])}
    ms_api["demand"] = [d]

    with pytest.raises(mig.MigrationStop, match="currency_rate_daily"):
        _run(ms_api, dry_run=True)


def test_dry_run_prints_currency_and_month_breakdown(seeded, ms_api, caplog):
    import logging

    ms_api["demand"] = [
        _demand(ms_id="dem-a", name="A", order_ms_id=None),
        _uzs_sale(ms_id="dem-b", name="B", moment="2026-03-31 23:30:00.000"),
    ]
    with caplog.at_level(logging.INFO, logger="ms_history"):
        rc = asyncio.run(mig.main("dry-run"))

    assert rc == 0
    text = caplog.text
    assert "Распределение по валютам" in text
    assert "UZS: 1 на 126 500.00" in text and "USD: 1 на 3 000.00" in text
    # Вечерняя московская отгрузка 31 марта — это уже апрель по Ташкенту.
    assert "2026-04  отгрузки: UZS 1" in text
    assert "2026-03  отгрузки: USD 1" in text
    assert _rows(seeded, "SELECT * FROM orders") == []


def test_dry_run_with_unconfirmed_rate_fails_loudly(seeded, ms_api, caplog):
    import logging

    ms_api["demand"] = [_uzs_sale(value=0.0000790)]
    with caplog.at_level(logging.INFO, logger="ms_history"):
        rc = asyncio.run(mig.main("dry-run"))

    assert rc == 1
    assert "ВАЛЮТА/КУРС НЕ ПОДТВЕРЖДЕНЫ" in caplog.text
    assert "ПЕРЕНОС ОСТАНОВЛЕН" in caplog.text


# ─── Ключ накладной — UUID, а не имя (P0-2) ─────────────────────────────────


def test_duplicate_demand_names_do_not_overwrite_each_other(seeded, ms_api):
    """Две отгрузки с одним именем в МС — две накладные, а не одна перезаписанная."""
    ms_api["demand"] = [
        _demand(ms_id="dem-1", name="D001", order_ms_id=None,
                positions=[_pos(P1_MS, "Труба", 3, 100000)]),
        _demand(ms_id="dem-2", name="D001", order_ms_id=None, sum_minor=40000,
                positions=[_pos(P2_MS, "Уголок", 2, 20000)]),
    ]

    stats, _, _ = _run(ms_api)

    invs = _rows(seeded, "SELECT id, invoice_number, total_amount_cents, comment "
                         "FROM invoices ORDER BY id")
    assert [i["invoice_number"] for i in invs] == ["MS-D-D001", "MS-D-D001-2"]
    assert [i["total_amount_cents"] for i in invs] == [300000, 40000]
    assert all("отгрузка D001" in i["comment"] for i in invs), "исходное имя видно"
    assert len(_rows(seeded, "SELECT * FROM invoice_items")) == 2
    assert stats["invoice_numbers_suffixed"] == 1
    keys = _rows(seeded, "SELECT ms_id, local_id FROM ms_id_map WHERE entity_type = 'demand' "
                         "ORDER BY ms_id")
    assert [(k["ms_id"], k["local_id"]) for k in keys] == [
        ("dem-1", invs[0]["id"]), ("dem-2", invs[1]["id"])
    ]

    _run(ms_api)  # повторный прогон: ни дублей, ни смены номеров
    again = _rows(seeded, "SELECT id, invoice_number FROM invoices ORDER BY id")
    assert again == [{"id": i["id"], "invoice_number": i["invoice_number"]} for i in invs]
    assert len(_rows(seeded, "SELECT * FROM invoice_items")) == 2


def test_duplicate_names_are_counted_for_preview():
    docs = [{"name": "D1", "ms_id": "a"}, {"name": "D1", "ms_id": "b"},
            {"name": "D2", "ms_id": "c"}]
    assert mig.duplicate_names(docs) == {"D1": 2}


def test_invoice_written_by_old_version_is_adopted_not_duplicated(seeded, ms_api):
    """Прежняя версия писала накладную без ms_id_map (ключом был номер).
    Повторный прогон обязан подхватить её, а не завести вторую рядом."""
    with seeded.get_conn() as conn:
        cur = seeded.get_cursor(conn)
        cur.execute(
            seeded.q("INSERT INTO invoices (type, warehouse_id, invoice_number, invoice_date, "
                     "status, currency, total_amount_cents, created_by, created_at) "
                     "VALUES ('outgoing', 1, 'MS-D-D001', '2026-03-15', 'confirmed', 'USD', "
                     "1, 0, '2026-03-15 10:00:00')"),
        )
        conn.commit()
    ms_api["demand"] = [_demand(ms_id="dem-1", name="D001", order_ms_id=None)]

    _run(ms_api)

    invs = _rows(seeded, "SELECT * FROM invoices")
    assert len(invs) == 1
    assert invs[0]["total_amount_cents"] == 300000
    assert _rows(seeded, "SELECT local_id FROM ms_id_map WHERE ms_id = 'dem-1'")[0][
        "local_id"] == invs[0]["id"]


# ─── Время МС — московское (P1-5) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "moment, expected",
    [
        ("2026-03-14 09:20:00.000", "2026-03-14 11:20:00"),
        # Вечер после 22:00 МСК — в Ташкенте уже следующий день…
        ("2026-03-14 22:30:00.000", "2026-03-15 00:30:00"),
        # …и следующий месяц, и следующий год.
        ("2026-03-31 23:30:00.000", "2026-04-01 01:30:00"),
        ("2025-12-31 22:00:00.000", "2026-01-01 00:00:00"),
        # 2012: Москва жила по UTC+4 — сдвиг +1 ч, а не +2. Константа ошиблась бы.
        ("2012-06-01 23:30:00.000", "2012-06-02 00:30:00"),
        ("2026-03-14 09:20", "2026-03-14 11:20:00"),
        ("", ""),
    ],
)
def test_ms_moment_is_moscow_time(moment, expected):
    assert mig._ms_moment_to_local(moment) == expected


def test_evening_document_lands_on_next_day_and_month(seeded, ms_api):
    d = _demand(ms_id="dem-late", name="D-LATE", order_ms_id=None)
    d["moment"] = "2026-03-31 23:30:00.000"
    ms_api["demand"] = [d]
    p = _paymentin(ms_id="pay-late", op=("demand", "dem-late"))
    p["moment"] = "2026-03-31 22:15:00.000"
    ms_api["paymentin"] = [p]

    _run(ms_api)

    assert _rows(seeded, "SELECT invoice_date FROM invoices")[0]["invoice_date"] == "2026-04-01"
    order = _rows(seeded, "SELECT submitted_at, shipped_at FROM orders")[0]
    assert order == {"submitted_at": "2026-04-01 01:30:00", "shipped_at": "2026-04-01 01:30:00"}
    assert _rows(seeded, "SELECT confirmed_at FROM payments")[0]["confirmed_at"] \
        == "2026-04-01 00:15:00"


# ─── Поставщики (P2-6) ───────────────────────────────────────────────────────


def test_counterparty_with_only_purchases_becomes_supplier(seeded, ms_api):
    with seeded.get_conn() as conn:
        cur = seeded.get_cursor(conn)
        cur.execute(
            seeded.q("INSERT INTO counterparties (name, type, legacy_ms_id, created_at) "
                     "VALUES ('Завод', 'customer', 'cp-supplier', ?)"),
            (seeded.now_str(),),
        )
        conn.commit()
    ms_api["demand"] = [_demand(order_ms_id=None)]  # Ромашка покупает…
    ms_api["supply"] = [_supply(), _supply(ms_id="sup-2", name="S2", agent="cp-supplier")]
    ms_api["paymentout"] = [_paymentout(ms_id="po-2", agent="cp-supplier", op=None)]

    stats, _, _ = _run(ms_api)

    types = {r["legacy_ms_id"]: r["type"]
             for r in _rows(seeded, "SELECT legacy_ms_id, type FROM counterparties")}
    assert types == {CP_MS: "customer", "cp-supplier": "supplier"}
    assert stats["counterparties_to_supplier"] == 1
    assert stats["counterparties_both"] == 1, "Ромашка и продаёт, и покупает — остаётся клиентом"


# ─── Историю нельзя отменить: склад по ней не двигался (P0-3) ────────────────


@pytest.fixture
def boss_api(seeded, monkeypatch):
    """WebApp под боссом поверх перенесённой истории. Мокаем только initData."""
    import importlib

    from fastapi.testclient import TestClient

    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    rate_limit.reset()
    seeded.set_role(100, "boss_user", "Boss", "boss")
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "B", "username": "b"},
    )
    return TestClient(server.app)


def _stock(db):
    return _rows(db, "SELECT product_id, quantity FROM stock ORDER BY product_id")


def test_history_map_entities_match_warehouse_guard():
    """Перенос пишет ключи тех сущностей, по которым warehouse узнаёт историю."""
    from services import warehouse

    assert set(mig.INVOICE_ENTITY.values()) == set(warehouse.HISTORY_MAP_ENTITIES)


def test_historical_invoices_cannot_be_cancelled(seeded, ms_api, boss_api):
    """Ни первая, ни вторая отгрузка заказа, ни приход из МС не отменяются.

    Вторая отгрузка в order_shipment не попадает, и прежняя защита «накладная
    заказа — отменяйте заказ» её не ловила: отмена возвращала товар на склад.
    """
    ms_api["customerorder"] = [_order(sum_minor=600000,
                                      positions=[_pos(P1_MS, "Труба", 6, 100000)])]
    ms_api["demand"] = [
        _demand(ms_id="dem-1", name="D001", positions=[_pos(P1_MS, "Труба", 2, 100000)]),
        _demand(ms_id="dem-2", name="D002", positions=[_pos(P1_MS, "Труба", 4, 100000)]),
    ]
    ms_api["supply"] = [_supply()]
    _run(ms_api)
    before = _stock(seeded)

    invoices = _rows(seeded, "SELECT id FROM invoices ORDER BY id")
    assert len(invoices) == 3
    for inv in invoices:
        r = boss_api.post("/api/wh/invoices/cancel",
                          json={"initData": "100", "invoice_id": inv["id"]})
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "historical"
        assert "перенесено из МойСклад" in r.json()["reason"]

    assert _stock(seeded) == before
    assert {r["status"] for r in _rows(seeded, "SELECT status FROM invoices")} == {"confirmed"}


def test_historical_invoice_guard_is_in_warehouse_itself(seeded, ms_api):
    """Запрет в `cancel_invoice_in`, а не только в ручке: отмену зовут и заказ,
    и контейнер. И узнаётся история по ms_id_map, даже если номер поправили."""
    from services import warehouse

    ms_api["demand"] = [_demand(order_ms_id=None)]
    _run(ms_api)
    inv_id = _rows(seeded, "SELECT id FROM invoices")[0]["id"]
    with seeded.get_conn() as conn:
        cur = seeded.get_cursor(conn)
        cur.execute(seeded.q("UPDATE invoices SET invoice_number = 'ПОПРАВЛЕН-1' WHERE id = ?"),
                    (inv_id,))
        conn.commit()
    before = _stock(seeded)

    res = asyncio.run(warehouse.cancel_invoice(inv_id, cancelled_by=100))

    assert res["ok"] is False and res["code"] == "historical"
    assert _stock(seeded) == before


def test_historically_shipped_order_cannot_be_cancelled(seeded, ms_api, boss_api):
    """Заказ, отгруженный в МС, не отменяется: отмена откатила бы накладную и
    вернула товар, которого перенос не списывал. Статус подменяем на approved —
    иначе до проверки не дойти (отмена разрешена только одобренным), а
    нужно доказать, что стережёт именно запрет истории."""
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand()]
    _run(ms_api)
    order_id = _rows(seeded, "SELECT id FROM orders")[0]["id"]
    with seeded.get_conn() as conn:
        cur = seeded.get_cursor(conn)
        cur.execute(seeded.q("UPDATE orders SET status = 'approved' WHERE id = ?"), (order_id,))
        conn.commit()
    before = _stock(seeded)

    r = boss_api.post("/api/orders/cancel",
                      json={"initData": "100", "order_id": order_id, "reason": "ошибка"})

    assert r.status_code == 409
    assert "отгружен ещё в МойСклад" in r.json()["detail"]
    assert _rows(seeded, "SELECT status FROM orders")[0]["status"] == "approved"
    assert _rows(seeded, "SELECT status FROM invoices")[0]["status"] == "confirmed"
    assert _stock(seeded) == before


def test_historical_order_never_shipped_can_still_be_cancelled(seeded, ms_api, boss_api):
    """Заказ покупателя МС без отгрузки склад не двигал нигде; отмена — только
    смена статуса и единственный способ снять его из резерва."""
    ms_api["customerorder"] = [_order()]
    _run(ms_api)
    order_id = _rows(seeded, "SELECT id FROM orders")[0]["id"]
    before = _stock(seeded)

    r = boss_api.post("/api/orders/cancel",
                      json={"initData": "100", "order_id": order_id, "reason": "не актуален"})

    assert r.status_code == 200, r.text
    assert _rows(seeded, "SELECT status FROM orders")[0]["status"] == "cancelled"
    assert _stock(seeded) == before


def test_invoice_list_flags_historical_for_the_ui(seeded, ms_api, boss_api):
    """Фронт прячет «Отменить» по флагу `historical` — живые накладные без него."""
    from services import adb_core, warehouse

    ms_api["demand"] = [_demand(order_ms_id=None)]
    _run(ms_api)

    async def live_incoming():
        pid = await adb_core.fetchval("SELECT id FROM products WHERE legacy_ms_id = $1", P2_MS)
        wid = await adb_core.fetchval("SELECT id FROM warehouses ORDER BY id LIMIT 1")
        return await warehouse.create_invoice(
            invoice_type="incoming", warehouse_id=int(wid),
            items=[{"product_id": pid, "quantity": 1, "price_cents": 100}], created_by=100,
        )

    live = asyncio.run(live_incoming())
    assert live["ok"], live

    r = boss_api.post("/api/wh/invoices", json={"initData": "100"})
    assert r.status_code == 200
    flags = {i["invoice_number"]: i["historical"] for i in r.json()["invoices"]}
    assert flags == {"MS-D-D001": True, live["invoice_number"]: False}


# ─── Подготовка к переносу на чистую базу (сентябрь 2026) ───────────────────
#
# Скрипт писался до разбивки оплаты, долгов поставщикам и CHECK-ограничений.
# Ниже — то, что без правок дало бы выдуманные долги/авансы или уронило бы
# транзакцию на проде.


def _cashin(ms_id="cin-1", sum_minor=300000, op=None, agent=CP_MS):
    doc = _paymentin(ms_id=ms_id, sum_minor=sum_minor, op=op)
    doc["name"] = "ПКО-1"
    doc["agent"] = {"meta": {"href": f"https://x/entity/counterparty/{agent}"}, "name": "ООО Ромашка"}
    return doc


def _cashout(ms_id="cout-1", sum_minor=200000, agent=CP_MS, op=("supply", "sup-1"),
             expense_item=None):
    doc = _paymentout(ms_id=ms_id, sum_minor=sum_minor, agent=agent, op=op)
    doc["name"] = "РКО-1"
    if expense_item:
        doc["expenseItem"] = {"name": expense_item}
    return doc


def _returns_doc(ms_id="sr-1", sum_minor=50000, agent=CP_MS, name="ООО Ромашка"):
    return {"id": ms_id, "name": "R1", "moment": "2026-03-20 10:00:00.000", "sum": sum_minor,
            "agent": {"meta": {"href": f"https://x/entity/counterparty/{agent}"}, "name": name}}


def _add_counterparty(db, name, ms_id):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, legacy_ms_id, created_at) "
                 "VALUES (?, 'customer', ?, ?)"),
            (name, ms_id, db.now_str()),
        )
        conn.commit()


def test_cashin_pays_a_sale_like_a_payment(seeded, ms_api):
    """Приходный кассовый ордер — те же деньги клиента, что и платёж. Без него
    продажа, оплаченная наличными, осталась бы долгом клиента."""
    ms_api["demand"] = [_demand(ms_id="dem-x", name="D9", order_ms_id=None)]
    ms_api["cashin"] = [_cashin(op=None)]

    stats, unmatched, problems = _run(ms_api)

    assert problems == [] and unmatched.total() == 0
    assert stats["payments_in_cashin"] == 1 and stats["payments_fifo"] == 1
    pay = _rows(seeded, "SELECT * FROM payments")[0]
    assert (pay["ms_paymentin_id"], pay["amount_cents"], pay["status"]) == ("cin-1", 300000,
                                                                          "confirmed")
    assert "приходный ордер" in pay["comment"]
    order = _rows(seeded, "SELECT payment_type, paid_confirmed_at FROM orders")[0]
    assert order["payment_type"] == "paid" and order["paid_confirmed_at"]
    assert stats["client_debts_open"] == 0


def test_cashout_to_supplier_goes_to_supplier_payments(seeded, ms_api):
    ms_api["supply"] = [_supply()]
    ms_api["cashout"] = [_cashout()]

    stats, _, _ = _run(ms_api)

    assert stats["payments_out"] == 1 and stats["payments_out_cashout"] == 1
    sp = _rows(seeded, "SELECT * FROM supplier_payments")[0]
    assert sp["ms_paymentout_id"] == "cout-1" and "расходный ордер" in sp["comment"]
    assert _rows(seeded, "SELECT * FROM payments") == []
    assert stats["supplier_debts_open"] == 0 and stats["supplier_advances"] == 0


def test_outgoing_money_to_non_supplier_is_not_a_supplier_payment(seeded, ms_api):
    """Аренда/зарплата — не выплата поставщику: у получателя нет ни одного
    прихода, и вся сумма легла бы в «Поставщикам» строкой «аванс»."""
    _add_counterparty(seeded, "Арендодатель", "cp-rent")
    ms_api["paymentout"] = [_paymentout(ms_id="po-rent", agent="cp-rent", op=None)]
    ms_api["cashout"] = [_cashout(ms_id="co-rent", agent="cp-rent", op=None,
                                  expense_item="Аренда")]

    stats, unmatched, _ = _run(ms_api)

    assert stats["payments_out_not_supplier"] == 2
    assert _rows(seeded, "SELECT * FROM supplier_payments") == []
    bucket = unmatched.buckets["исходящий платёж не поставщику — не перенесён"]
    assert len(bucket) == 2 and any("Аренда" in x for x in bucket)
    assert stats["supplier_advances"] == 0
    types = {r["legacy_ms_id"]: r["type"]
             for r in _rows(seeded, "SELECT legacy_ms_id, type FROM counterparties")}
    assert types["cp-rent"] == "customer", "арендодатель не становится поставщиком"


def test_zero_quantity_positions_are_skipped_and_reported(seeded, ms_api):
    """CHECK `quantity > 0` на проде: нулевая строка уронила бы весь перенос."""
    ms_api["demand"] = [_demand(order_ms_id=None, positions=[
        _pos(P1_MS, "Труба", 3, 100000), _pos(P2_MS, "Уголок", 0, 5000),
    ])]

    stats, unmatched, problems = _run(ms_api)

    assert problems == []
    assert len(_rows(seeded, "SELECT * FROM order_items")) == 1
    assert len(_rows(seeded, "SELECT * FROM invoice_items")) == 1
    assert "позиция с нулевым количеством — не перенесена" in unmatched.buckets


def test_zero_sum_money_documents_are_skipped_and_reported(seeded, ms_api):
    ms_api["demand"] = [_demand(order_ms_id=None)]
    ms_api["paymentin"] = [_paymentin(sum_minor=0, op=None)]
    ms_api["supply"] = [_supply()]
    ms_api["paymentout"] = [_paymentout(sum_minor=0)]

    stats, unmatched, _ = _run(ms_api)

    assert _rows(seeded, "SELECT * FROM payments") == []
    assert _rows(seeded, "SELECT * FROM supplier_payments") == []
    assert stats["payments_zero"] == 1 and stats["payments_out_zero"] == 1
    assert "входящий документ с нулевой суммой — не перенесён" in unmatched.buckets
    assert "исходящий документ с нулевой суммой — не перенесён" in unmatched.buckets


def test_zero_total_order_is_not_an_open_debt(seeded, ms_api):
    ms_api["demand"] = [_demand(order_ms_id=None, sum_minor=0,
                                positions=[_pos(P1_MS, "Труба", 1, 0)])]

    stats, _, _ = _run(ms_api)

    order = _rows(seeded, "SELECT payment_type, paid_confirmed_at FROM orders")[0]
    assert order["payment_type"] == "paid" and order["paid_confirmed_at"]
    assert stats["client_debts_open"] == 0


def test_partially_shipped_order_owes_only_what_was_shipped(seeded, ms_api):
    """Баланс в МС — по отгрузкам. Заказали 6, отгрузили и оплатили 2:
    клиент ничего не должен, а по позициям заказа вышел бы долг за 4 трубы."""
    ms_api["customerorder"] = [_order(sum_minor=600000, positions=[_pos(P1_MS, "Труба", 6, 100000)])]
    ms_api["demand"] = [_demand(sum_minor=200000, positions=[_pos(P1_MS, "Труба", 2, 100000)])]
    ms_api["paymentin"] = [_paymentin(sum_minor=200000)]

    stats, unmatched, problems = _run(ms_api)

    assert problems == [], "недоотгрузка — сведения, а не расхождение"
    assert stats["orders_partially_shipped"] == 1
    assert [r["quantity"] for r in _rows(seeded, "SELECT quantity FROM order_items")] == [2]
    order = _rows(seeded, "SELECT status, payment_type, paid_confirmed_at FROM orders")[0]
    assert order["status"] == "shipped" and order["payment_type"] == "paid"
    assert order["paid_confirmed_at"]
    assert any("отгружен частично" in k for k in unmatched.info)
    assert stats["client_debts_open"] == 0


def test_demand_positions_must_match_demand_sums(seeded, ms_api):
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand(sum_minor=999999)]

    _, _, problems = _run(ms_api)

    assert any("суммой отгрузок в МС" in p for p in problems)


def test_unshipped_ms_order_is_kept_to_ship_and_noted(seeded, ms_api):
    ms_api["customerorder"] = [_order()]

    stats, unmatched, _ = _run(ms_api)

    assert stats["orders_unshipped"] == 1
    assert _rows(seeded, "SELECT status FROM orders")[0]["status"] == "approved"
    notes = [x for k, v in unmatched.info.items() if "без отгрузки" in k for x in v]
    assert len(notes) == 1 and "Согласован" in notes[0]
    assert stats["client_debts_open"] == 1, "виден и в предпросмотре «Долгов»"


def test_settled_mode_closes_supplier_history(seeded, ms_api):
    """`settled`: приходы «уже оплачено», выплаты не пишутся — иначе общие
    выплаты при закрытых приходах легли бы «авансом» (supplier_debts._allocate)."""
    ms_api["supply"] = [_supply(), _supply(ms_id="sup-2", name="S2")]
    ms_api["paymentout"] = [_paymentout(op=None, sum_minor=150000)]

    stats, unmatched, _ = _run(ms_api, supplier_history="settled")

    terms = _rows(seeded, "SELECT payment_type, created_by, created_by_name "
                          "FROM supplier_invoice_terms")
    assert len(terms) == 2
    assert {(t["payment_type"], t["created_by"], t["created_by_name"]) for t in terms} == {
        ("paid", 0, "Перенос из МойСклад")}
    assert _rows(seeded, "SELECT * FROM supplier_payments") == []
    assert stats["payments_out_settled"] == 1 and stats["supplies_settled"] == 2
    assert (stats["supplier_debts_open"], stats["supplier_advances"],
            stats["supplier_debts_overdue"]) == (0, 0, 0)
    assert any("settled" in k for k in unmatched.info)


def test_ledger_mode_previews_debts_and_advances(seeded, ms_api):
    _add_counterparty(seeded, "Завод", "cp-supplier")
    ms_api["supply"] = [_supply(), _supply(ms_id="sup-2", name="S2", agent="cp-supplier")]
    ms_api["paymentout"] = [_paymentout(sum_minor=900000, op=None)]

    stats, unmatched, _ = _run(ms_api)

    # Ромашка: приход 2 000 и выплата 9 000 → аванс 7 000; Завод: приход 2 000
    # без выплаты → долг, просроченный со дня прихода.
    assert stats["supplier_debts_open"] == 1
    assert stats["supplier_debts_overdue"] == 1
    assert stats["supplier_advances"] == 1
    assert any("аванс" in line and "7 000.00" in line for line in unmatched.preview)


def test_switching_supplier_history_mode_on_rerun(seeded, ms_api):
    ms_api["supply"] = [_supply()]
    ms_api["paymentout"] = [_paymentout()]

    def state():
        return (len(_rows(seeded, "SELECT * FROM supplier_payments")),
                len(_rows(seeded, "SELECT * FROM supplier_invoice_terms")))

    _run(ms_api)
    assert state() == (1, 0)
    _run(ms_api, supplier_history="settled")
    assert state() == (0, 1)
    _run(ms_api, supplier_history="settled")
    assert state() == (0, 1)
    _run(ms_api)
    assert state() == (1, 0)


def test_settled_mode_keeps_terms_set_by_a_person(seeded, ms_api):
    ms_api["supply"] = [_supply()]
    _run(ms_api)
    inv_id = _rows(seeded, "SELECT id FROM invoices")[0]["id"]
    with seeded.get_conn() as conn:
        cur = seeded.get_cursor(conn)
        cur.execute(
            seeded.q("INSERT INTO supplier_invoice_terms (invoice_id, payment_type, due_date, "
                     "created_by, created_by_name, created_at) "
                     "VALUES (?, 'credit', '2030-01-01', 100, 'Boss', ?)"),
            (inv_id, seeded.now_str()),
        )
        conn.commit()

    stats, _, _ = _run(ms_api, supplier_history="settled")

    assert stats["supplies_terms_kept"] == 1
    assert _rows(seeded, "SELECT payment_type FROM supplier_invoice_terms")[0][
        "payment_type"] == "credit"
    _run(ms_api)  # ledger не удаляет чужую строку
    assert len(_rows(seeded, "SELECT * FROM supplier_invoice_terms")) == 1


def test_unknown_supplier_history_mode_is_refused(seeded, ms_api):
    with pytest.raises(ValueError):
        _run(ms_api, supplier_history="maybe")


def test_balance_reconciliation_picks_sign_and_lists_mismatches(seeded, ms_api):
    _add_counterparty(seeded, "Бета", "cp-beta")
    beta = _demand(ms_id="dem-b", name="DB", order_ms_id=None, sum_minor=100000,
                   positions=[_pos(P1_MS, "Труба", 1, 100000)])
    beta["agent"] = {"meta": {"href": "https://x/entity/counterparty/cp-beta"}, "name": "Бета"}
    ms_api["demand"] = [_demand(order_ms_id=None), beta]
    # Знак МС здесь «клиент должен — минус»: у нас «мы должны» положительно,
    # то есть клиент-должник отрицателен — совпадает как есть.
    balances = [{"ms_id": CP_MS, "name": "ООО Ромашка", "balance": -300000},
                {"ms_id": "cp-beta", "name": "Бета", "balance": -50000}]

    stats, unmatched, _ = _run(ms_api, balances=balances)

    assert (stats["balance_checked"], stats["balance_mismatches"]) == (2, 1)
    assert "как есть" in unmatched.balance[0]
    assert any("«Бета»" in line for line in unmatched.balance[1:])

    inverted = [dict(b, balance=-b["balance"]) for b in balances]
    stats, unmatched, _ = _run(ms_api, balances=inverted)
    assert stats["balance_mismatches"] == 1 and "обратный" in unmatched.balance[0]


def test_balance_reconciliation_skips_multi_currency_and_missing_report(seeded, ms_api):
    ms_api["demand"] = [_demand(order_ms_id=None), _uzs_sale()]

    stats, unmatched, _ = _run(ms_api, balances=[])
    assert stats["balance_not_compared"] == 1 and stats["balance_checked"] == 0

    stats, unmatched, _ = _run(ms_api, balances=None)
    assert stats["balance_checked"] == 0 and "не выполнена" in unmatched.balance[0]


def test_ms_returns_are_noted_not_migrated(seeded, ms_api):
    ms_api["demand"] = [_demand(order_ms_id=None)]
    ms_api["salesreturn"] = [_returns_doc()]

    stats, unmatched, _ = _run(ms_api)

    assert stats["ms_returns"] == 1
    notes = [x for k, v in unmatched.info.items() if "возвраты" in k for x in v]
    assert notes and "возврат покупателя" in notes[0] and "500.00" in notes[0]
    assert _rows(seeded, "SELECT * FROM returns") == []


def test_apply_requires_explicit_supplier_history():
    with pytest.raises(SystemExit):
        mig._parse_args(["--apply"])
    assert mig._parse_args(["--apply", "--supplier-history", "settled"]).supplier_history == "settled"
    assert mig._parse_args(["--dry-run"]).supplier_history is None
    with pytest.raises(SystemExit):
        mig._parse_args(["--apply", "--supplier-history", "maybe"])


def test_dry_run_reports_app_preview_and_balance_check(seeded, ms_api, caplog):
    import logging

    ms_api["demand"] = [_demand(order_ms_id=None)]
    ms_api["extra"]["report/counterparty"] = [
        {"counterparty": {"meta": {"href": f"https://x/entity/counterparty/{CP_MS}"},
                          "name": "ООО Ромашка"}, "balance": -300000},
    ]
    with caplog.at_level(logging.INFO, logger="ms_history"):
        rc = asyncio.run(mig.main("dry-run"))

    assert rc == 0
    assert "ЧТО ПОКАЖЕТ ПРИЛОЖЕНИЕ" in caplog.text
    assert "сравнено 1, расхождений 0" in caplog.text
    assert _rows(seeded, "SELECT * FROM orders") == []


# ─── Чьи заказы: сотрудники МС → сотрудники бота ─────────────────────────────

OWNER_TG, BOSS_TG = 941599419, 273791555
EMP_OWNER, EMP_OTHER = "emp-farid", "emp-anvar"


def _employee(ms_id, uid, full_name, short_fio, *, archived=False):
    """Сотрудник так, как его отдаёт entity/employee."""
    return {
        "meta": {"href": f"{_MS}/entity/employee/{ms_id}", "type": "employee"},
        "id": ms_id, "uid": uid, "name": short_fio, "fullName": full_name,
        "shortFio": short_fio, "archived": archived,
    }


def _by(doc, emp):
    """Автор документа МС — ссылка на сотрудника, без expand."""
    doc["owner"] = {"meta": {"href": f"{_MS}/entity/employee/{emp}", "type": "employee"}}
    return doc


@pytest.fixture
def two_authors(seeded, ms_api):
    """Два сотрудника МС и два сотрудника бота: владелец (manager) и босс без имени
    в user_roles — ровно как на проде 16.09."""
    import services.database as db

    db.set_role(OWNER_TG, "flext9m", "Фаридун", "manager")
    db.set_role(BOSS_TG, "", "", "boss")
    ms_api["employee"] = [
        _employee(EMP_OWNER, "farid@impeks", "Масуджанов Фаридун", "Масуджанов Ф."),
        _employee(EMP_OTHER, "anvar@impeks", "Анваров Анвар", "Анваров А."),
    ]
    # Заказ МС владельца (отгрузил другой), продажа по отгрузке другого,
    # платёж другого по заказу владельца и платёж владельца по продаже другого.
    ms_api["customerorder"] = [_by(_order(sum_minor=300000), EMP_OWNER)]
    ms_api["demand"] = [
        _by(_demand(), EMP_OTHER),
        _by(_demand(ms_id="dem-2", name="D002", order_ms_id=None), EMP_OTHER),
    ]
    ms_api["paymentin"] = [
        _by(_paymentin(sum_minor=100000), EMP_OTHER),
        _by(_paymentin(ms_id="pay-2", sum_minor=50000, op=("demand", "dem-2")), EMP_OWNER),
    ]
    return seeded


def test_without_owner_flags_history_belongs_to_nobody(two_authors, ms_api):
    stats, _, problems = _run(ms_api)
    assert problems == [] and stats["orders_owner"] == 0
    assert {r["user_id"] for r in _rows(two_authors, "SELECT user_id FROM orders")} == {0}
    assert {(r["user_id"], r["full_name"]) for r in _rows(two_authors, "SELECT * FROM payments")} == {
        (0, "Перенос из МойСклад")
    }


def test_owner_map_splits_orders_payments_and_debts_between_two_ms_employees(two_authors, ms_api):
    """Заказ — на автора заказа МС, продажа по отгрузке — на автора отгрузки,
    платёж — на автора платежа; имя и username — из user_roles. «Долги»
    менеджера — ровно заказы, записанные на него."""
    import services.database as db

    stats, unmatched, problems = _run(
        ms_api, owner_map=[("farid@impeks", OWNER_TG), ("anvar@impeks", BOSS_TG)],
    )
    assert problems == []
    orders = {r["ms_customerorder_id"] or r["ms_demand_id"]: (r["user_id"], r["full_name"])
              for r in _rows(two_authors, "SELECT * FROM orders")}
    assert orders == {"ord-1": (OWNER_TG, "Фаридун"), "dem-2": (BOSS_TG, f"Сотрудник {BOSS_TG}")}
    pays = {r["ms_paymentin_id"]: (r["user_id"], r["username"], r["full_name"])
            for r in _rows(two_authors, "SELECT * FROM payments")}
    assert pays == {"pay-1": (BOSS_TG, "", f"Сотрудник {BOSS_TG}"),
                    "pay-2": (OWNER_TG, "flext9m", "Фаридун")}

    mine = asyncio.run(db.get_open_debts(user_id=OWNER_TG))
    assert [d["ms_customerorder_id"] for d in mine] == ["ord-1"]
    boss = asyncio.run(db.get_open_debts(user_id=BOSS_TG))
    assert [d["ms_demand_id"] for d in boss] == ["dem-2"]

    assert stats["orders_owners"] == 2
    summary = "\n".join(unmatched.owners)
    assert f"{OWNER_TG} «Фаридун» (manager): заказов 1, из них открытых долгов (видит в «Долгах») 1" in summary
    assert f"{BOSS_TG} «Сотрудник {BOSS_TG}» (boss): заказов 1" in summary

    # Ключ — ФИО (регистр, пробелы, ё) или id: тот же результат; повтор идемпотентен.
    _run(ms_api, owner_map=[("  МАСУДЖАНОВ   фаридун ", OWNER_TG), (EMP_OTHER, BOSS_TG)])
    again = {r["ms_customerorder_id"] or r["ms_demand_id"]: r["user_id"]
             for r in _rows(two_authors, "SELECT * FROM orders")}
    assert again == {"ord-1": OWNER_TG, "dem-2": BOSS_TG}
    assert len(_rows(two_authors, "SELECT id FROM payments")) == 2


def test_unmapped_or_missing_author_goes_to_default(two_authors, ms_api):
    """Автор не в карте, автор, которого нет в справочнике сотрудников, и
    документ без автора — всё на `--orders-owner-default`."""
    ms_api["demand"].append(_by(_demand(ms_id="dem-3", name="D003", order_ms_id=None), "emp-fired"))
    ms_api["demand"].append(_demand(ms_id="dem-4", name="D004", order_ms_id=None))  # без owner

    _, _, problems = _run(ms_api, owner_map=[("farid", OWNER_TG)], orders_owner=BOSS_TG)
    assert problems == []
    orders = {r["ms_customerorder_id"] or r["ms_demand_id"]: r["user_id"]
              for r in _rows(two_authors, "SELECT * FROM orders")}
    assert orders == {"ord-1": OWNER_TG, "dem-2": BOSS_TG, "dem-3": BOSS_TG, "dem-4": BOSS_TG}

    # Без default — не в карте значит «Перенос из МойСклад» (user_id 0).
    _run(ms_api, owner_map=[("farid@impeks", OWNER_TG)])
    orders = {r["ms_customerorder_id"] or r["ms_demand_id"]: r["user_id"]
              for r in _rows(two_authors, "SELECT * FROM orders")}
    assert orders == {"ord-1": OWNER_TG, "dem-2": 0, "dem-3": 0, "dem-4": 0}


@pytest.mark.parametrize("owner_map, match", [
    ([("nobody@impeks", OWNER_TG)], "nobody@impeks.*нет такого сотрудника"),
    ([("impeks", OWNER_TG)], "нет такого сотрудника"),  # хвост логина — не ключ
    ([("farid@impeks", OWNER_TG), (EMP_OWNER, BOSS_TG)], "уже назначен"),
])
def test_bad_owner_map_key_stops_before_write(two_authors, ms_api, owner_map, match):
    with pytest.raises(mig.MigrationStop, match=match):
        _run(ms_api, owner_map=owner_map, orders_owner=BOSS_TG)
    assert _rows(two_authors, "SELECT id FROM orders") == []


def test_ambiguous_owner_map_key_stops(two_authors, ms_api):
    ms_api["employee"].append(_employee("emp-farid-2", "farid2@impeks", "Другой", "Масуджанов Ф."))
    with pytest.raises(mig.MigrationStop, match="нескольким сотрудникам"):
        _run(ms_api, owner_map=[("Масуджанов Ф.", OWNER_TG)])
    assert _rows(two_authors, "SELECT id FROM orders") == []


def test_owner_targets_must_be_active_staff_before_any_write(two_authors, ms_api):
    import services.database as db

    with pytest.raises(mig.MigrationStop, match="orders-owner-map «farid@impeks»=777: нет в user_roles"):
        _run(ms_api, owner_map=[("farid@impeks", 777)], orders_owner=BOSS_TG)
    db.set_role(778, "g", "Гость", "guest")
    with pytest.raises(mig.MigrationStop, match="orders-owner-default 778: роль guest"):
        _run(ms_api, owner_map=[("farid@impeks", OWNER_TG)], orders_owner=778)
    asyncio.run(db.deactivate_user(OWNER_TG, by=BOSS_TG))
    with pytest.raises(mig.MigrationStop, match="941599419: manager, деактивирован"):
        _run(ms_api, owner_map=[("farid@impeks", OWNER_TG)], orders_owner=BOSS_TG)
    with pytest.raises(mig.MigrationStop, match="orders-owner-default 941599419: manager, деактивирован"):
        _run(ms_api, orders_owner=OWNER_TG)  # прежний --orders-owner — те же проверки
    assert _rows(two_authors, "SELECT id FROM orders") == [], "остановка — до записи"
    assert _rows(two_authors, "SELECT id FROM payments") == []


def test_dry_run_prints_ms_employee_table_with_targets(two_authors, ms_api, caplog):
    import logging

    ms_api["demand"].append(_demand(ms_id="dem-4", name="D004", order_ms_id=None))  # без owner
    with caplog.at_level(logging.INFO, logger="ms_history"):
        rc = asyncio.run(mig.main("dry-run", owner_map=[("farid@impeks", OWNER_TG)],
                                  orders_owner=BOSS_TG))
    assert rc == 0
    lines = caplog.text.splitlines()
    head = lines.index(next(x for x in lines if "СОТРУДНИКИ МОЙСКЛАД" in x))
    table = "\n".join(lines[head:head + 9])
    farid = next(x for x in lines[head:] if EMP_OWNER in x)
    anvar = next(x for x in lines[head:] if EMP_OTHER in x)
    nobody = next(x for x in lines[head:] if "(без автора)" in x)
    assert "farid@impeks" in farid and "Масуджанов Фаридун" in farid
    # колонки: заказ отгр плат ПКО пост исх РКО
    assert farid.split("→")[0].split()[-7:] == ["1", "0", "1", "0", "0", "0", "0"]
    assert anvar.split("→")[0].split()[-7:] == ["0", "2", "1", "0", "0", "0", "0"]
    assert nobody.split("→")[0].split()[-7:] == ["0", "1", "0", "0", "0", "0", "0"]
    assert f"→ {OWNER_TG} «Фаридун» (manager) — по карте" in farid
    assert f"→ {BOSS_TG} «Сотрудник {BOSS_TG}» (boss) — по умолчанию" in anvar
    assert "заказ  отгр  плат   ПКО  пост   исх   РКО  → на кого" in table
    assert "ЧЬИ ЗАКАЗЫ И ПЛАТЕЖИ" in caplog.text
    assert _rows(two_authors, "SELECT * FROM orders") == []


def test_dry_run_without_map_shows_table_and_bad_map_stops_after_it(two_authors, ms_api, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="ms_history"):
        assert asyncio.run(mig.main("dry-run")) == 0
    anvar = next(x for x in caplog.text.splitlines() if EMP_OTHER in x and "→" in x)
    assert "→ 0 «Перенос из МойСклад» — только руководству (по умолчанию)" in anvar

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ms_history"):
        rc = asyncio.run(mig.main("dry-run", owner_map=[("farid@impek", OWNER_TG)]))
    assert rc == 1
    text = caplog.text
    assert "СОТРУДНИКИ МОЙСКЛАД" in text and "farid@impeks" in text
    assert "«farid@impek»: нет такого сотрудника МС" in text
    assert "ПЕРЕНОС ОСТАНОВЛЕН" in text

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="ms_history"):
        rc = asyncio.run(mig.main("dry-run", owner_map=[("farid@impeks", 777)]))
    assert rc == 1
    assert "777 ✗ нет в user_roles — перенос остановится" in caplog.text
    assert "ПЕРЕНОС ОСТАНОВЛЕН" in caplog.text


def test_employee_directory_unavailable_still_maps_by_id(two_authors, ms_api, monkeypatch):
    """Нет прав на entity/employee — перенос не падает, ключом годится id автора."""
    real = mig.ms_get

    async def no_employees(path, params=None):
        if path == "entity/employee":
            raise RuntimeError("403")
        return await real(path, params)

    monkeypatch.setattr(mig, "ms_get", no_employees)
    assert asyncio.run(mig.pull_employees()) is None
    _, _, problems = _run(ms_api, owner_map=[(EMP_OWNER, OWNER_TG)], orders_owner=BOSS_TG)
    assert problems == []
    orders = {r["ms_customerorder_id"] or r["ms_demand_id"]: r["user_id"]
              for r in _rows(two_authors, "SELECT * FROM orders")}
    assert orders == {"ord-1": OWNER_TG, "dem-2": BOSS_TG}
    with pytest.raises(mig.MigrationStop, match="нет такого сотрудника"):
        _run(ms_api, owner_map=[("farid@impeks", OWNER_TG)])


def test_orders_owner_cli_flags():
    args = mig._parse_args([
        "--apply", "--supplier-history", "ledger",
        "--orders-owner-map", "farid@impeks=941599419",
        "--orders-owner-map", "Масуджанов Фаридун = 941599419",
        "--orders-owner-default", "273791555",
    ])
    assert args.orders_owner_map == [("farid@impeks", 941599419), ("Масуджанов Фаридун", 941599419)]
    assert args.orders_owner_default == 273791555
    legacy = mig._parse_args(["--apply", "--supplier-history", "settled", "--orders-owner", "941599419"])
    assert legacy.orders_owner_default == 941599419 and legacy.orders_owner_map == []
    plain = mig._parse_args(["--dry-run"])
    assert plain.orders_owner_default is None and plain.orders_owner_map == []
    for bad in (["--dry-run", "--orders-owner-map", "farid@impeks"],
                ["--dry-run", "--orders-owner-map", "farid=abc"],
                ["--dry-run", "--orders-owner-map", "=941599419"],
                ["--dry-run", "--orders-owner", "1", "--orders-owner-default", "2"],
                ["--explain", "00001", "--orders-owner-default", "2"]):
        with pytest.raises(SystemExit):
            mig._parse_args(bad)


# ─── Платежи без разбивки ────────────────────────────────────────────────────


def test_migrated_payments_need_no_breakdown(seeded, ms_api):
    """Исторический платёж пишется БЕЗ строки `payment_parts` — и это штатно:
    он `confirmed`, а подтверждённый платёж объясняет деньги заказа и без
    разбивки. Иначе каждая перенесённая продажа висела бы «не оплачено способом»
    (к отгрузке не допускается), а `migrate_payment_breakdown` требовал бы
    вручную разложить сотни чужих платежей."""
    import scripts.migrate_payment_breakdown as mpb
    from services import order_payments

    ms_api["customerorder"] = [_order(sum_minor=300000)]
    ms_api["demand"] = [_demand(), _demand(ms_id="dem-2", name="D002", order_ms_id=None)]
    ms_api["paymentin"] = [
        _paymentin(sum_minor=300000),
        _paymentin(ms_id="pay-2", sum_minor=100000, op=("demand", "dem-2")),
    ]
    _, _, problems = _run(ms_api)
    assert problems == []

    assert _rows(seeded, "SELECT * FROM payment_parts") == []
    assert {r["status"] for r in _rows(seeded, "SELECT status FROM payments")} == {"confirmed"}
    orders = {r["ms_customerorder_id"] or r["ms_demand_id"]: r["id"]
              for r in _rows(seeded, "SELECT id, ms_demand_id, ms_customerorder_id FROM orders")}
    gaps = asyncio.run(order_payments.payment_gap_cents(list(orders.values())))
    assert gaps[orders["ord-1"]] == 0, "оплаченный заказ объяснён платежом без разбивки"
    assert gaps[orders["dem-2"]] == 200000, "частичная оплата — остаток долга, не весь заказ"

    rep = asyncio.run(mpb.report())
    assert rep["unexplained_payments"] == [] and rep["paid_orders_blocked"] == {}
