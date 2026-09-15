"""
Расхождения денежного учёта, найденные E2E-покрытием «Денег» и разбором денег.

Каждый тест — один способ, которым сумма молча расходилась с реальностью:
«весь остаток» мимо сдачи, сдача поверх ожидающей оплаты, возврат сумов
«долларами», частичное поступление по рассрочке, пересчёт прошлых денег по
сегодняшнему курсу, потолок суммы в единицах валюты, USD+UZS одним числом в
отчёте продаж и заказ «оплата сразу», пропадавший из долгов.

БД настоящая (isolated_db, SQLite). Переполнение INTEGER на SQLite не
воспроизводится (там целые 64-битные при любом имени типа) — его держат
`tests/test_money_postgres.py` на настоящем Postgres и сторож
`test_no_int4_casts_in_money_sql` ниже.
"""

import asyncio
import importlib
import re
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _order(db, *, total=100.0, qty=1, currency="USD", payment_type="credit",
           status="shipped", uid=1, created_at=None):
    """Заказ сразу в нужном статусе — сценарий начинается после одобрения."""
    oid = db.create_order(uid, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", qty, "шт", total / qty)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type=?, currency=?, due_date=? WHERE id=?"),
            (payment_type, currency, "2030-01-15" if payment_type == "credit" else None, oid),
        )
        if created_at:
            cur.execute(db.q("UPDATE orders SET created_at=? WHERE id=?"), (created_at, oid))
        conn.commit()
    db.update_order_status(oid, status)
    return oid


def _rows(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return [dict(r) for r in cur.fetchall()]


def _client(db, monkeypatch, uid):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})

    import services.notifier as notifier

    async def _recips():
        return []

    async def _send(*_a, **_k):
        return None

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)
    return TestClient(server.app)


# ─── 1–2. «Отметить оплату» и сдача заявляют одни и те же деньги ────────────


def test_mark_full_remaining_subtracts_confirmed_deposit(isolated_db):
    """Заказ 100, сдача 60 подтверждена: «весь остаток» — это 40, а не 100."""
    db = isolated_db
    _setup(db)
    oid = _order(db, total=100.0)
    dep = _run(db.create_cash_deposit(1, 60.0))
    assert dep["ok"] and _run(db.confirm_cash_deposit(dep["deposit_id"], 2, "Boss"))["ok"]

    ok, pid = _run(db.mark_order_paid(oid, 1, "Manager", amount=None))
    assert ok
    assert _rows(db, "SELECT amount_cents FROM payments WHERE id = ?", (pid,)) == [
        {"amount_cents": 4000}
    ]
    # Больше заявлять нечего — второй «весь остаток» отказывает.
    assert _run(db.mark_order_paid(oid, 1, "Manager", amount=None)) == (False, None)


def test_mark_paid_does_not_claim_amount_held_by_pending_deposit(isolated_db):
    """Неподтверждённая сдача уже застолбила часть остатка — как и в FIFO сдачи."""
    db = isolated_db
    _setup(db)
    oid = _order(db, total=100.0)
    assert _run(db.create_cash_deposit(1, 70.0))["ok"]  # pending

    ok, pid = _run(db.mark_order_paid(oid, 1, "Manager", amount=999.0))
    assert ok
    assert _rows(db, "SELECT amount_cents FROM payments WHERE id = ?", (pid,)) == [
        {"amount_cents": 3000}
    ], "переплата срезается до незаявленного остатка"


def test_deposit_is_not_allocated_over_pending_payment(isolated_db):
    """Заказ 200, отмечено 150 (ждёт): сдача 200 ложится на заказ не больше 50."""
    db = isolated_db
    _setup(db)
    first = _order(db, total=200.0, created_at="2026-01-01 09:00:00")
    second = _order(db, total=80.0, created_at="2026-01-02 09:00:00")
    ok, _pid = _run(db.mark_order_paid(first, 1, "Manager", amount=150.0))
    assert ok

    res = _run(db.create_cash_deposit(1, 200.0))
    assert res["ok"]
    alloc = {r["order_id"]: r["amount_allocated_cents"]
             for r in _rows(db, "SELECT order_id, amount_allocated_cents FROM cash_deposit_orders")}
    # FIFO: первому — только незаявленные 50, остаток сдачи идёт второму.
    assert alloc == {first: 5000, second: 8000}

    # Подтвердили и платёж, и сдачу — по заказу собрано ровно 200, не 350.
    _run(db.confirm_all_pending_payments_for_order(first, 2, "Boss"))
    assert _run(db.confirm_cash_deposit(res["deposit_id"], 2, "Boss"))["ok"]
    from services.debts import calc_order_balance

    bal = _run(calc_order_balance(first))
    assert bal.confirmed_cents + bal.deposits_cents == 20000
    assert bal.remaining_cents == 0


def test_claimable_is_one_formula_for_both_paths(isolated_db):
    """mark_paid и распределение сдачи видят один и тот же «можно заявить»."""
    db = isolated_db
    _setup(db)
    oid = _order(db, total=300.0)
    _run(db.mark_order_paid(oid, 1, "Manager", amount=100.0))          # pending 100
    assert _run(db.create_cash_deposit(1, 50.0))["ok"]                  # pending 50
    from services.debts import calc_claimable_cents

    assert _run(calc_claimable_cents([oid])) == {oid: 15000}
    assert _run(db.deposit_remaining_cents_for_orders([oid])) == {oid: 15000}


# ─── 3. Возврат наличными без курса ──────────────────────────────────────────


def _uzs_paid_order_with_cash_return(db, price=1_250_000.0):
    oid = _order(db, total=price, currency="UZS", payment_type="paid")
    pid = db.add_payment(1, "@m", "Manager", price, "UZS", "c", order_id=oid)
    assert _run(db.confirm_payment(pid, 2, "Boss"))
    item = _rows(db, "SELECT id FROM order_items WHERE order_id = ?", (oid,))[0]["id"]
    res = _run(db.create_return(oid, "full", "Брак", [(item, 1, price)], "cash", 1))
    assert res["ok"], res
    assert _run(db.mark_return_goods_received(res["return_id"], 2))["ok"]
    return oid, res["return_id"]


def test_cash_refund_without_rate_is_refused_not_booked_as_base(isolated_db):
    db = isolated_db
    _setup(db)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("DELETE FROM currency_rates WHERE currency_code = 'UZS'"))
        conn.commit()
    db._invalidate_currency_rates_cache()
    oid, rid = _uzs_paid_order_with_cash_return(db)

    res = _run(db.confirm_return(rid, 2, "Boss"))
    assert res["ok"] is False
    assert "курс" in res["error"].lower()
    # Ничего не сдвинулось: ни касса, ни возврат, ни заказ.
    assert _rows(db, "SELECT COUNT(*) AS n FROM cash_deposits")[0]["n"] == 0
    assert _rows(db, "SELECT status FROM returns WHERE id = ?", (rid,))[0]["status"] == "pending"
    assert _rows(db, "SELECT returned_qty FROM order_items WHERE order_id = ?", (oid,))[0][
        "returned_qty"] == 0

    # Курс задали — тот же возврат проходит и выдаёт из кассы доллары по курсу.
    ok, err = db.set_currency_rate("UZS", 0.00008, 2)
    assert ok, err
    assert _run(db.confirm_return(rid, 2, "Boss"))["ok"]
    assert _rows(db, "SELECT amount_cents FROM cash_deposits") == [{"amount_cents": -10000}]


# ─── 4, 6. Рассрочка: частичное поступление и карточка покупателя ────────────


def _credit_deal(price_cents=2_000_000, down=500_000, months=3, buyer="Азиз Рахимов"):
    from services import machines

    m = _run(machines.create_machine(vin="JCB-PART-1", name="JCB 3CX", created_by=2,
                                     price_cents=price_cents, status="in_stock"))
    assert m["ok"], m
    deal = _run(machines.create_deal(m["machine_id"], kind="credit", price_cents=price_cents,
                                     buyer_name=buyer, created_by=2,
                                     down_payment_cents=down, months=months))
    assert deal["ok"], deal
    return deal["deal_id"]


def test_partial_receipt_reduces_machine_debt_everywhere(isolated_db):
    """Взнос 5 000, график 3 × 5 000, внесено 1 500 → должен 13 500, не 15 000."""
    from services import machines, receivables

    db = isolated_db
    _setup(db)
    deal = _credit_deal()
    assert _run(machines.add_receipt(deal, 150_000, user_id=2))["ok"]

    rows = _run(receivables.machine_debt_rows(date.today().isoformat()))
    assert [(r["remaining"], r["next_amount"]) for r in rows] == [(13_500.0, 3_500.0)]

    items = _run(receivables.machine_receivables())
    assert sum(r.amount_cents for r in items) == 1_350_000
    assert sorted(r.amount_cents for r in items) == [350_000, 500_000, 500_000]

    card = _run(receivables.buyer_card("азиз  рахимов"))
    assert card["outstanding"]["by_currency"] == [{"currency": "USD", "total": 13_500.0}]
    progress = card["deals"][0]["progress"]
    # «Всего по рассрочкам» и «осталось» в прогрессе — одно число.
    assert progress["left_cents"] == 1_350_000
    assert progress["paid_cents"] == 650_000


def test_buyer_card_carries_progress_coverage_and_receipts(isolated_db):
    """Фронт карточки покупателя считает «Получено» по progress/covered_cents —
    без них он писал «Получено 0 USD из …»."""
    from services import machines, receivables

    db = isolated_db
    _setup(db)
    deal = _credit_deal()
    first = _rows(db, "SELECT id FROM machine_deal_payments WHERE deal_id = ? AND seq = 1",
                  (deal,))[0]["id"]
    assert _run(machines.pay_installment(first, user_id=2))["ok"]

    card = _run(receivables.buyer_card("Азиз Рахимов"))
    d = card["deals"][0]
    assert d["progress"] == {
        "received_cents": 500_000, "down_payment_cents": 500_000, "paid_cents": 1_000_000,
        "planned_cents": 2_000_000, "left_cents": 1_000_000,
    }
    assert [p["covered_cents"] for p in d["payments"]] == [500_000, 500_000, 0, 0]
    assert [r["amount_cents"] for r in d["receipts"]] == [500_000]
    assert card["outstanding"]["by_currency"] == [{"currency": "USD", "total": 10_000.0}]


# ─── 7. «Деньги → Отчёт»: курс на момент подтверждения ───────────────────────


def test_money_summary_uses_rate_frozen_at_confirmation(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 0.00008, 2)[0]       # 12 500 сум за доллар
    frozen = db.add_payment(1, "@m", "Manager", 1_250_000.0, "UZS", "c")
    assert _run(db.confirm_payment(frozen, 2, "Boss"))
    # Легаси-строка без снимка — пересчитывается по текущему курсу.
    legacy = db.add_payment(1, "@m", "Manager", 500_000.0, "UZS", "c")
    assert _run(db.confirm_payment(legacy, 2, "Boss"))
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET fx_rate_to_base = NULL WHERE id = ?"), (legacy,))
        conn.commit()
    assert db.set_currency_rate("UZS", 0.0001, 2)[0]        # сум укрепился: 10 000

    client = _client(db, monkeypatch, 2)
    body = client.post("/api/money/summary", json={"initData": "2", "period": "month"}).json()
    # 1 250 000 × 0.00008 = 100 (снимок) + 500 000 × 0.0001 = 50 (текущий).
    assert body["base_total"] == 150.0
    assert body["base_partial"] is False
    # Разбивка по валютам та же, внутренний разрез по курсу наружу не уходит.
    assert body["payments"] == [{"currency": "UZS", "total_cents": 175_000_000, "count": 2}]
    assert "payments_by_rate" not in body


# ─── 9. Потолок суммы — в эквиваленте базовой валюты ─────────────────────────


def test_amount_ceiling_is_in_base_currency_equivalent():
    from services import money

    assert money.max_cents_for_rate(1) == money.MAX_BASE_CENTS
    # 12 500 сум за доллар: потолок — 125 000 000 000 сум, а не 10 000 000.
    assert money.max_cents_for_rate(0.00008) == 12_500_000_000_000
    # Курса нет — технический потолок, а не отказ.
    assert money.max_cents_for_rate(None) == money.HARD_MAX_CENTS
    assert money.max_cents_for_rate(0) == money.HARD_MAX_CENTS
    assert money.validate_cents(2_000_000_000, 0.00008)[0] is True    # 20 000 000 сум ≈ $1 600
    assert money.validate_cents(money.MAX_BASE_CENTS + 1)[0] is False  # USD — как раньше
    assert money.HARD_MAX_CENTS < 2**53, "сумма обязана переживать float/JSON без потерь"


def test_uzs_payment_above_old_ceiling_is_accepted(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 0.00008, 2)[0]
    client = _client(db, monkeypatch, 1)

    # 50 000 000 сум ≈ $4 000: прежний потолок «10 000 000 в любой валюте» отказывал.
    r = client.post("/api/payments/send", json={
        "initData": "1", "amount": 50_000_000, "currency": "UZS", "comment": "Экскаватор",
    })
    assert r.status_code == 200, r.text
    # Эквивалент выше 10 000 000 USD — по-прежнему отказ, и текстом про лимит.
    r = client.post("/api/payments/send", json={
        "initData": "1", "amount": 130_000_000_000, "currency": "UZS", "comment": "опечатка",
    })
    assert r.status_code == 400 and "лимит" in r.json()["detail"]
    r = client.post("/api/payments/send", json={
        "initData": "1", "amount": 10_000_001, "currency": "USD", "comment": "опечатка",
    })
    assert r.status_code == 400


def test_uzs_order_mark_paid_above_old_ceiling(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 0.00008, 2)[0]
    oid = _order(db, total=1_500_000_000.0, currency="UZS")  # техника ≈ $120 000
    client = _client(db, monkeypatch, 1)
    r = client.post("/api/orders/mark_paid",
                    json={"initData": "1", "order_id": oid,
                          "parts": [{"method": "bank", "currency": "UZS", "amount": 600_000_000}]})
    assert r.status_code == 200, r.text
    assert _rows(db, "SELECT amount_cents FROM payments") == [{"amount_cents": 60_000_000_000}]


def test_machine_priced_in_uzs_above_old_ceiling(isolated_db):
    from services import machines

    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 0.00008, 2)[0]
    res = _run(machines.create_machine(vin="UZS-PRICE-1", name="Hyundai R220", created_by=2,
                                       price_cents=150_000_000_000, currency="UZS"))
    assert res["ok"], res
    too_big = _run(machines.create_machine(vin="UZS-PRICE-2", name="Опечатка", created_by=2,
                                           price_cents=150_000_000_000, currency="USD"))
    assert too_big["ok"] is False and "лимит" in too_big["error"]


# ─── 10. «Продажи → Отчёт»: валюты не складываются ───────────────────────────


def _outgoing(currency, price_cents, name):
    from services import container_receipt, warehouse

    pid = _run(container_receipt.create_product(name))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(invoice_type="incoming", warehouse_id=wid,
                                  items=[{"product_id": pid, "quantity": 1, "price_cents": None}]))
    res = _run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid, currency=currency,
        items=[{"product_id": pid, "quantity": 1, "price_cents": price_cents}],
    ))
    assert res["ok"], res


def test_sales_report_splits_currencies_and_converts_with_rate(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _outgoing("USD", 100_000, "Кабель")              # 1 000 USD
    _outgoing("UZS", 1_250_000_000, "Автомат")      # 12 500 000 UZS
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("DELETE FROM currency_rates WHERE currency_code = 'UZS'"))
        conn.commit()
    db._invalidate_currency_rates_cache()
    client = _client(db, monkeypatch, 2)

    body = client.post("/api/analytics", json={"initData": "2", "period": "month"}).json()
    # Без курса сумы в итог не входят — и это сказано, а не спрятано в «12 501 000».
    assert body["total"] == 1_000.0
    assert body["base_partial"] is True
    assert body["missing_rates"] == [{"currency": "UZS", "amount": 12_500_000.0}]
    assert body["total_by_currency"] == [
        {"currency": "UZS", "total": 12_500_000.0}, {"currency": "USD", "total": 1_000.0},
    ]
    assert body["avg_check"] == 1_000.0, "средний чек — по отгрузкам, вошедшим в итог"
    by_name = {p["name"]: p["currency"] for p in body["top_products"]}
    assert by_name == {"Кабель": "USD", "Автомат": "UZS"}

    assert db.set_currency_rate("UZS", 0.00008, 2)[0]
    body = client.post("/api/analytics", json={"initData": "2", "period": "month"}).json()
    assert body["total"] == 2_000.0 and body["base_partial"] is False
    assert body["missing_rates"] == [] and body["avg_check"] == 1_000.0
    # Порядок топа — по эквиваленту: 12 500 000 сум = 1 000 USD, не «в 12 500 раз больше».
    assert {c["currency"] for c in body["top_clients"]} == {"USD", "UZS"}


# ─── 11. «Оплата сразу» без подтверждённых денег — это долг ──────────────────


def test_paid_order_with_rejected_auto_payment_is_a_debt(isolated_db, monkeypatch):
    from services import receivables

    db = isolated_db
    _setup(db)
    oid = _order(db, total=250.0, payment_type="paid")
    pid = db.add_payment(1, "", "Manager", 250.0, "USD", "авто", order_id=oid)
    assert _run(db.reject_payment(pid, 2, "Boss"))

    assert [o["id"] for o in _run(db.get_open_debts())] == [oid]
    # «Оплата сразу» причиталась в день заказа — в «к оплате сейчас» он есть.
    assert [o["id"] for o in _run(db.get_open_debts(due_through=date.today().isoformat()))] == [oid]
    assert _run(db.count_boss_attention())["debts"] == 1
    items = _run(receivables.collect())
    assert [(r.ref_id, r.amount_cents) for r in items] == [(oid, 25_000)]

    client = _client(db, monkeypatch, 2)
    body = client.post("/api/debts", json={"initData": "2"}).json()
    assert [(d["id"], d["remaining"]) for d in body["debts"]] == [(oid, 250.0)]
    assert body["totals"]["orders"]["base_total"] == 250.0

    # Менеджер заявляет оплату заново — кнопка на карточке долга работает.
    mgr = _client(db, monkeypatch, 1)
    r = mgr.post("/api/orders/mark_paid", json={
        "initData": "1", "order_id": oid,
        "parts": [{"method": "card", "currency": "USD", "amount": 250}],
    })
    assert r.status_code == 200, r.text
    new_pid = r.json()["payment_id"]
    assert _run(db.confirm_payment(new_pid, 2, "Boss"))
    assert _run(db.get_open_debts()) == []


def test_debts_reminder_lists_paid_order_by_its_order_date(isolated_db):
    """У «оплаты сразу» нет due_date: без debt_due_date такой долг попадал в
    выборку напоминания, но ни в один блок — уходило пустое сообщение."""
    from services.debts import calc_order_balances
    from tasks.run_debts_notify import _format_message

    db = isolated_db
    _setup(db)
    oid = _order(db, total=250.0, payment_type="paid")
    today = date.today().isoformat()
    debts = _run(db.get_open_debts(due_through=today))
    text = _format_message(debts, _run(calc_order_balances([oid])), today, is_boss_view=True)
    assert "Сегодня к оплате (1)" in text and f"#{oid}" in text and "250" in text


def test_paid_order_is_not_a_debt_once_confirmed(isolated_db):
    db = isolated_db
    _setup(db)
    oid = _order(db, total=90.0, payment_type="paid")
    pid = db.add_payment(1, "", "Manager", 90.0, "USD", "авто", order_id=oid)
    # Ждёт подтверждения — деньги ещё не получены, долг виден.
    assert [o["id"] for o in _run(db.get_open_debts())] == [oid]
    assert _run(db.confirm_payment(pid, 2, "Boss"))
    assert _run(db.get_open_debts()) == []
    assert _run(db.count_boss_attention())["debts"] == 0


# ─── 8. Сторож: денежный SQL не приводит к 32-битному целому ─────────────────


def test_no_int4_casts_in_money_sql():
    """`CAST(... AS INTEGER)` на Postgres — 32 бита: строка дороже 21 474 836.47
    роняла «Долги», закрытие заказа и дебиторку. SQLite это не ловит, поэтому
    сторожим сам текст запросов."""
    from services.debts import SUM_ORDER_TOTAL_CENTS

    assert "AS BIGINT" in SUM_ORDER_TOTAL_CENTS
    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"AS\s+(INTEGER|INT|INT4|SMALLINT)\s*\)|::\s*(integer|int4?|smallint)\b",
                         re.IGNORECASE)
    offenders = []
    for base in ("services", "webapp", "tasks", "handlers", "utils", "scripts"):
        for path in (root / base).rglob("*.py"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
    assert offenders == []
