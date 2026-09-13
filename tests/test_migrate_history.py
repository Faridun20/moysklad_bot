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
        "rate": {"currency": {"name": "USD"}},
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
        "rate": {"currency": {"name": "USD"}},
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
        "rate": {"currency": {"name": "USD"}},
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
        "rate": {"currency": {"name": "USD"}},
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
        "rate": {"currency": {"name": "USD"}},
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


def _run(ms_api, *, dry_run=False):
    orders = asyncio.run(mig.pull_orders())
    demands = asyncio.run(mig.pull_demands())
    payments = asyncio.run(mig.pull_payments())
    supplies = asyncio.run(mig.pull_supplies())
    payments_out = asyncio.run(mig.pull_payments_out())
    return asyncio.run(
        mig.write_history(orders, demands, payments, supplies, payments_out, dry_run=dry_run)
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


def test_demand_without_order_is_reported_not_guessed(seeded, ms_api):
    ms_api["customerorder"] = [_order()]
    ms_api["demand"] = [_demand(ms_id="dem-x", name="D009", order_ms_id=None)]

    stats, unmatched, _ = _run(ms_api)

    assert stats.get("demands", 0) == 0, "отгрузка без основания не привязывается наугад"
    assert any("без заказа" in k for k in unmatched.buckets)
    assert _rows(seeded, "SELECT * FROM order_shipment") == []


def test_payment_without_operations_is_reported(seeded, ms_api):
    ms_api["customerorder"] = [_order()]
    ms_api["paymentin"] = [_paymentin(ms_id="pay-x", op=None)]

    stats, unmatched, _ = _run(ms_api)

    assert stats["payments_unlinked"] == 1
    assert stats.get("payments", 0) == 0
    assert any("без документа-основания" in k for k in unmatched.buckets)
    assert _rows(seeded, "SELECT * FROM payments") == []


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


def test_overpayment_is_flagged(seeded, ms_api):
    ms_api["customerorder"] = [_order(sum_minor=100000,
                                      positions=[_pos(P1_MS, "Труба", 1, 100000)])]
    ms_api["paymentin"] = [_paymentin(sum_minor=500000)]

    _, _, problems = _run(ms_api)

    assert any("оплачено" in p for p in problems)


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
