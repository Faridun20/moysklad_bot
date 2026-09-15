"""
Кредиторка: «сколько мы должны поставщикам» (services/supplier_debts.py).

Проверяем то, из-за чего этот учёт врёт молча: долг, посчитанный не по той
накладной; выплата, потерянная при отмене прихода; аванс, принятый за
переплату-ошибку; курс, поставленный мимо допуска ЦБ; и права — сумма прихода
это себестоимость, и менеджер её видеть не должен.

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет.
"""

import asyncio

import pytest

import services.roles as roles
from tests.conftest import pay_account_id


def _run(coro):
    return asyncio.run(coro)


def _rows(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        return [dict(r) for r in cur.fetchall()]


def _actor(uid=2, role="boss", name="Boss"):
    from services.order_payments import Actor

    return Actor(user_id=uid, name=name, role=role)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _supplier(db, name="Shandong Machinery"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, created_at) VALUES (?, ?, ?)"),
            (name, "supplier", db.now_str()),
        )
        conn.commit()
    return int(_rows(db, "SELECT MAX(id) AS id FROM counterparties")[0]["id"])


def _product(db, name="Экскаватор JCB"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO products (name, unit, created_at) VALUES (?, ?, ?)"),
            (name, "шт", db.now_str()),
        )
        conn.commit()
    return int(_rows(db, "SELECT MAX(id) AS id FROM products")[0]["id"])


def _incoming(db, supplier_id, *, qty=2, price_cents=500_000, currency="USD", day=None):
    """Приход на склад через тот же сервис, что и форма накладной."""
    from services import warehouse

    pid = _product(db, f"Товар {db.now_str()}{qty}{price_cents}")
    res = _run(warehouse.create_invoice(
        invoice_type="incoming",
        warehouse_id=_run(warehouse.default_warehouse_id()),
        counterparty_id=supplier_id,
        items=[{"product_id": pid, "quantity": qty, "price_cents": price_cents}],
        currency=currency,
        invoice_date=day,
        created_by=2,
    ))
    assert res["ok"], res
    return int(res["invoice_id"])


def _parts(method="bank", amount=1000, currency="USD", **extra):
    row = {"method": method, "currency": currency, "amount": amount, **extra}
    if method in ("card", "bank") and "account_id" not in row:
        row["account_id"] = pay_account_id(method)
    return [row]


# ─── Долг появляется сам ─────────────────────────────────────────────────────


def test_incoming_invoice_with_supplier_becomes_a_debt(isolated_db):
    """Приход с контрагентом и суммой — это долг: отдельной отметки не нужно."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=2, price_cents=500_000)  # 2 × 5 000 = 10 000 USD

    led = _run(supplier_debts.ledger())
    assert [d.invoice_id for d in led.debts] == [inv]
    assert led.debts[0].remaining_cents == 1_000_000
    assert led.debts[0].currency == "USD"
    assert led.debts[0].supplier_id == sup


def test_invoice_without_supplier_is_not_a_debt(isolated_db):
    """Приход без контрагента долгом быть не может — его не к кому отнести."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    _incoming(db, None)
    assert _run(supplier_debts.ledger()).debts == []


def test_marked_paid_invoice_drops_out_of_debts(isolated_db):
    """«Уже оплачено» убирает приход из долгов и не выдумывает выплату."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup)

    _run(supplier_debts.set_terms(_actor(), inv, "paid"))
    assert _run(supplier_debts.ledger()).debts == []
    assert _rows(db, "SELECT * FROM supplier_payments") == []


def test_terms_set_due_date_and_audit(isolated_db):
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup)

    _run(supplier_debts.set_terms(_actor(), inv, "credit", "2026-12-31"))
    led = _run(supplier_debts.ledger())
    assert led.debts[0].due_date == "2026-12-31"
    actions = [a["action"] for a in _rows(db, "SELECT action FROM audit_log")]
    assert "supplier_terms_set" in actions


def test_terms_can_be_changed_back_and_forth(isolated_db):
    """Условия переписываются, а не дублируются: строка одна на накладную."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup)

    _run(supplier_debts.set_terms(_actor(), inv, "paid"))
    _run(supplier_debts.set_terms(_actor(), inv, "credit", "2027-01-31"))
    rows = _rows(db, "SELECT payment_type, due_date FROM supplier_invoice_terms")
    assert len(rows) == 1 and rows[0]["payment_type"] == "credit"
    assert _run(supplier_debts.ledger()).debts[0].due_date == "2027-01-31"
    # «Уже оплачено» срок не хранит — по смыслу его нет.
    _run(supplier_debts.set_terms(_actor(), inv, "paid", "2027-01-31"))
    assert _rows(db, "SELECT due_date FROM supplier_invoice_terms")[0]["due_date"] is None


def test_due_date_defaults_to_invoice_date(isolated_db):
    """Срок не задан — долг стареет со дня прихода, а не «никогда»."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    _incoming(db, sup, day="2026-01-15")
    assert _run(supplier_debts.ledger()).debts[0].due_date == "2026-01-15"


def test_cancelled_invoice_is_not_a_debt(isolated_db):
    from services import supplier_debts, warehouse

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup)
    assert _run(warehouse.cancel_invoice(inv, 2))["ok"]
    assert _run(supplier_debts.ledger()).debts == []


def test_unpriced_receipt_is_reported_not_silently_zero(isolated_db):
    """Контейнер посчитали, цену не вписали: «долга нет» — неправильный ответ."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup, price_cents=None)

    led = _run(supplier_debts.ledger())
    assert led.debts == []
    assert [u["invoice_id"] for u in led.unpriced] == [inv]


# ─── Выплата гасит долг ──────────────────────────────────────────────────────


def test_payment_reduces_balance(isolated_db):
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=1_000_000)  # 10 000 USD

    res = _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "invoice_id": inv, "parts": _parts("bank", 4000),
    }))
    assert res["ok"] and res["total_cents"] == 400_000
    led = _run(supplier_debts.ledger())
    assert led.debts[0].remaining_cents == 600_000
    assert led.debts[0].direct_cents == 400_000


def test_payment_writes_supplier_payments_not_payments(isolated_db):
    """Исходящие деньги в `payments` не попадают: там дебиторка клиентов."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup)
    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "invoice_id": inv, "parts": _parts("cash", 100),
    }))
    assert _rows(db, "SELECT * FROM payments") == []
    rows = _rows(db, "SELECT * FROM supplier_payments")
    assert len(rows) == 1 and int(rows[0]["counterparty_id"]) == sup
    part = _rows(db, "SELECT * FROM supplier_payment_parts WHERE payment_id = ?",
                 (rows[0]["id"],))[0]
    assert part["method"] == "cash" and part["created_by"] == 2


def test_payment_over_invoice_remainder_is_refused(isolated_db):
    """Больше остатка по накладной — опечатка, а не аванс."""
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=100_000)  # 1 000 USD

    with pytest.raises(PaymentError) as e:
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": sup, "invoice_id": inv, "parts": _parts("bank", 1500),
        }))
    assert e.value.code == "over"
    assert _rows(db, "SELECT * FROM supplier_payments") == []


def test_general_payment_over_balance_becomes_advance(isolated_db):
    """Аванс поставщику — обычная практика, а не ошибка ввода."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    _incoming(db, sup, qty=1, price_cents=100_000)  # долг 1 000 USD

    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "parts": _parts("bank", 1500), "currency": "USD",
    }))
    led = _run(supplier_debts.ledger())
    assert led.debts[0].remaining_cents == 0
    assert led.advances == {(sup, "USD"): 50_000}


def test_general_payment_pays_oldest_first(isolated_db):
    """Выплата без привязки гасит долги от старых к новым — как сдача наличных."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    old = _incoming(db, sup, qty=1, price_cents=100_000, day="2026-01-10")
    new = _incoming(db, sup, qty=1, price_cents=100_000, day="2026-03-10")

    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "parts": _parts("cash", 1000), "currency": "USD",
    }))
    rest = {d.invoice_id: d.remaining_cents for d in _run(supplier_debts.ledger()).debts}
    assert rest[old] == 0
    assert rest[new] == 100_000


def test_payment_on_foreign_invoice_is_refused(isolated_db):
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    a, b = _supplier(db, "A"), _supplier(db, "B")
    inv = _incoming(db, a)
    with pytest.raises(PaymentError):
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": b, "invoice_id": inv, "parts": _parts("cash", 10),
        }))


def test_payment_on_paid_invoice_is_refused(isolated_db):
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup)
    _run(supplier_debts.set_terms(_actor(), inv, "paid"))
    with pytest.raises(PaymentError) as e:
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": sup, "invoice_id": inv, "parts": _parts("cash", 10),
        }))
    assert e.value.code == "already_paid"


def test_payment_on_unpriced_invoice_is_refused_with_reason(isolated_db):
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup, price_cents=None)
    with pytest.raises(PaymentError) as e:
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": sup, "invoice_id": inv, "parts": _parts("cash", 10),
        }))
    assert e.value.code == "no_amount"


def test_cancelling_invoice_keeps_the_money_as_advance(isolated_db):
    """Отмена прихода не стирает выплату: деньги ушли, и они становятся авансом."""
    from services import supplier_debts, warehouse

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=100_000)
    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "invoice_id": inv, "parts": _parts("bank", 400),
    }))
    assert _run(warehouse.cancel_invoice(inv, 2))["ok"]
    led = _run(supplier_debts.ledger())
    assert led.debts == []
    assert led.advances == {(sup, "USD"): 40_000}


# ─── Валюта и курс ───────────────────────────────────────────────────────────


def test_uzs_payment_against_usd_debt_converts_by_cbu(isolated_db):
    """Сумами гасят долларовый приход: сумма долга — пересчитанная."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 1 / 12000, 2)[0]
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=100_000)  # 1 000 USD

    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "invoice_id": inv,
        "parts": _parts("cash", 6_000_000, currency="UZS"),
    }))
    led = _run(supplier_debts.ledger())
    assert led.debts[0].remaining_cents == 50_000      # 500 USD осталось
    row = _rows(db, "SELECT * FROM supplier_payments")[0]
    assert row["currency"] == "UZS"                     # деньги ушли в сумах
    part = _rows(db, "SELECT * FROM supplier_payment_parts WHERE payment_id = ?", (row["id"],))[0]
    assert part["debt_currency"] == "USD" and int(part["debt_amount_cents"]) == 50_000


def test_manual_rate_beyond_deviation_is_refused(isolated_db):
    """Свой курс не дальше допуска от ЦБ — тот же рубеж, что у оплаты заказа."""
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 1 / 12000, 2)[0]
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=100_000)

    with pytest.raises(PaymentError) as e:
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": sup, "invoice_id": inv,
            "parts": _parts("cash", 6_000_000, currency="UZS", rate="30000"),
        }))
    assert e.value.code == "manual_rate"


def test_manual_rate_inside_deviation_is_accepted(isolated_db):
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 1 / 12000, 2)[0]
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=100_000)
    res = _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "invoice_id": inv,
        "parts": _parts("cash", 6_000_000, currency="UZS", rate="12500"),
    }))
    assert res["payments"][0]["rate_source"] == "manual"


def test_fx_rate_snapshot_is_written(isolated_db):
    """Курс к базовой валюте замораживается — итог по прошлым выплатам не «плывёт»."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    assert db.set_currency_rate("UZS", 1 / 12000, 2)[0]
    sup = _supplier(db)
    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "parts": _parts("cash", 1_200_000, currency="UZS"),
        "currency": "UZS",
    }))
    row = _rows(db, "SELECT * FROM supplier_payments")[0]
    assert round(1 / float(row["fx_rate_to_base"])) == 12000


# ─── Форма ───────────────────────────────────────────────────────────────────


def test_card_payment_requires_account(isolated_db):
    """С какой карты ушли деньги — обязательное поле, как и «куда пришли»."""
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    with pytest.raises(PaymentError) as e:
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": sup, "currency": "USD",
            "parts": [{"method": "card", "currency": "USD", "amount": 10}],
        }))
    assert e.value.code == "account_required"


def test_source_label_says_money_left_the_card(isolated_db):
    from services import pay_accounts, supplier_debts

    db = isolated_db
    _setup(db)
    acc_id = pay_account_id("card")
    acc = _run(pay_accounts.get_account(acc_id))
    label = supplier_debts.payment_label("card", 100_000, "USD", acc)
    assert label.startswith("с карты •••• 1234")
    # А то же самое поступление подписывается «на карту» — один и тот же счёт
    # в двух лентах обязан называться одинаково.
    assert pay_accounts.destination_label("card", acc).endswith("•••• 1234 (Фаридун М.)")


def test_future_payment_date_is_refused(isolated_db):
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    with pytest.raises(PaymentError):
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": sup, "currency": "USD", "parts": _parts("cash", 10),
            "paid_at": "2099-01-01",
        }))


def test_unknown_supplier_is_refused(isolated_db):
    from services import supplier_debts
    from services.order_payments import PaymentError

    db = isolated_db
    _setup(db)
    with pytest.raises(PaymentError) as e:
        _run(supplier_debts.record_payment(_actor(), {
            "supplier_id": 4242, "currency": "USD", "parts": _parts("cash", 10),
        }))
    assert e.value.status == 404


def test_audit_log_written(isolated_db):
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": sup, "currency": "USD", "parts": _parts("cash", 10), "note": "по счёту 42",
    }))
    entry = _rows(db, "SELECT details FROM audit_log WHERE action = ?",
                  ("supplier_payment_recorded",))
    assert entry and "по счёту 42" in entry[0]["details"]


# ─── Сводка экрана ───────────────────────────────────────────────────────────


def test_overview_does_not_mix_currencies(isolated_db):
    """5 000 UZS + 200 USD в одно число не складываем — по валютам и с флагом."""
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    _incoming(db, sup, qty=1, price_cents=20_000, currency="USD")
    _incoming(db, sup, qty=1, price_cents=500_000, currency="UZS")

    data = _run(supplier_debts.overview())
    curs = {b["currency"] for b in data["total"]["by_currency"]}
    assert curs == {"USD", "UZS"}
    # Курса UZS нет — итог в базовой считается частичным, а не врёт полным.
    assert data["total"]["partial"] is True


def test_overview_groups_by_supplier_and_lists_payments(isolated_db):
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    a = _supplier(db, "Альфа")
    b = _supplier(db, "Бета")
    _incoming(db, a, qty=1, price_cents=300_000)
    _incoming(db, b, qty=1, price_cents=100_000)
    _run(supplier_debts.record_payment(_actor(), {
        "supplier_id": a, "currency": "USD", "parts": _parts("bank", 500),
    }))

    data = _run(supplier_debts.overview())
    names = [s["supplier_name"] for s in data["suppliers"]]
    assert names[0] == "Альфа"          # больший долг сверху
    assert len(data["payments"]) == 1
    assert data["payments"][0]["account_label"].startswith("со счёта")


def test_overview_reports_aging_buckets(isolated_db):
    from services import supplier_debts

    db = isolated_db
    _setup(db)
    sup = _supplier(db)
    _incoming(db, sup, qty=1, price_cents=100_000, day="2020-01-01")
    data = _run(supplier_debts.overview())
    overdue = data["aging"]["overdue"]
    assert overdue["count"] == 1
    assert data["debts"][0]["state"] == "overdue"
    assert data["debts"][0]["days"] > 1000


# ─── Чистая раскладка (без БД) ───────────────────────────────────────────────


def test_allocate_is_pure_and_keeps_currency_apart():
    """Выплата в другой валюте чужую накладную не гасит — пересчитать её нечем."""
    from services.supplier_debts import _allocate

    invoices = [{
        "id": 1, "invoice_number": "IN-1", "invoice_date": "2026-01-01", "currency": "USD",
        "total_amount_cents": 100_000, "counterparty_id": 7, "supplier_name": "S",
        "payment_type": None, "due_date": None, "container_id": None, "comment": None,
    }]
    payments = [{
        "id": 1, "counterparty_id": 7, "invoice_id": 1, "currency": "UZS",
        "amount_cents": 1_000_000, "debt_currency": None, "debt_amount_cents": None,
        "supplier_name": "S",
    }]
    led = _allocate(invoices, payments)
    assert led.debts[0].remaining_cents == 100_000     # долг в USD не тронут
    assert led.advances == {(7, "UZS"): 1_000_000}      # сумы лежат авансом


# ─── Ручки и права ───────────────────────────────────────────────────────────


@pytest.fixture
def client(isolated_db, monkeypatch):
    import importlib

    from fastapi.testclient import TestClient

    import webapp.server as server

    _setup(isolated_db)
    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, uid, path, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_manager_cannot_see_supplier_debts(isolated_db, client):
    """Сумма прихода — закупочная цена: менеджеру экран не отвечает вовсе."""
    assert _post(client, 1, "/api/suppliers/debts").status_code == 403
    assert _post(client, 1, "/api/suppliers/payment", supplier_id=1,
                 parts=_parts("cash", 10)).status_code == 403
    assert _post(client, 1, "/api/suppliers/terms", invoice_id=1,
                 payment_type="paid").status_code == 403


def test_boss_sees_supplier_debts(isolated_db, client):
    db = isolated_db
    sup = _supplier(db)
    _incoming(db, sup, qty=1, price_cents=100_000)
    r = _post(client, 2, "/api/suppliers/debts")
    assert r.status_code == 200
    body = r.json()
    assert body["total"]["by_currency"] == [{"currency": "USD", "total": 1000.0}]
    assert body["debts"][0]["supplier_name"] == "Shandong Machinery"


def test_payment_is_idempotent(isolated_db, client):
    """Дважды отправленная форма записывает одну выплату, а не две."""
    db = isolated_db
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=100_000)
    body = {"supplier_id": sup, "invoice_id": inv, "parts": _parts("bank", 400),
            "idempotency_key": "form-1"}
    r1 = _post(client, 2, "/api/suppliers/payment", **body)
    r2 = _post(client, 2, "/api/suppliers/payment", **body)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["payments"] == r2.json()["payments"]
    assert len(_rows(db, "SELECT id FROM supplier_payments")) == 1


def test_payment_refusal_returns_code_and_text(isolated_db, client):
    db = isolated_db
    sup = _supplier(db)
    inv = _incoming(db, sup, qty=1, price_cents=100_000)
    r = _post(client, 2, "/api/suppliers/payment", supplier_id=sup, invoice_id=inv,
              parts=_parts("bank", 5000), idempotency_key="form-2")
    assert r.status_code == 409 and r.json()["code"] == "over"
    assert "Аванс" in r.json()["detail"]


def test_payment_without_parts_is_refused(isolated_db, client):
    r = _post(client, 2, "/api/suppliers/payment", supplier_id=1, parts=[])
    assert r.status_code == 400


def test_invoice_form_can_mark_the_receipt_paid(isolated_db, client):
    """«Уже оплачено» приходит прямо из формы накладной — долга не появляется."""
    from services import supplier_debts

    db = isolated_db
    sup = _supplier(db)
    pid = _product(db, "Гидронасос")
    r = _post(client, 2, "/api/wh/invoices/create", type="incoming", counterparty_id=sup,
              items=[{"product_id": pid, "quantity": 1, "price_cents": 100_000}],
              supplier_payment_type="paid", idempotency_key="inv-1")
    assert r.status_code == 200, r.text
    assert r.json()["supplier_terms"]["payment_type"] == "paid"
    assert _run(supplier_debts.ledger()).debts == []


def test_invoice_form_can_set_the_due_date(isolated_db, client):
    """Срок оплаты поставщику задают там же, где заводят приход."""
    from services import supplier_debts

    db = isolated_db
    sup = _supplier(db)
    pid = _product(db, "Рукав РВД")
    r = _post(client, 2, "/api/wh/invoices/create", type="incoming", counterparty_id=sup,
              items=[{"product_id": pid, "quantity": 1, "price_cents": 100_000}],
              supplier_payment_type="credit", supplier_due_date="2027-05-20",
              idempotency_key="inv-3")
    assert r.status_code == 200, r.text
    led = _run(supplier_debts.ledger())
    assert led.debts[0].due_date == "2027-05-20"
    assert led.debts[0].remaining_cents == 100_000


def test_invoice_form_bad_due_date_does_not_undo_the_invoice(isolated_db, client):
    """Отказ в условиях — предупреждение, а не откат: накладная проведена,
    остаток уже уехал."""
    db = isolated_db
    sup = _supplier(db)
    pid = _product(db, "Шланг")
    r = _post(client, 2, "/api/wh/invoices/create", type="incoming", counterparty_id=sup,
              items=[{"product_id": pid, "quantity": 1, "price_cents": 100_000}],
              supplier_payment_type="credit", supplier_due_date="20 мая",
              idempotency_key="inv-4")
    assert r.status_code == 200, r.text
    assert "supplier_terms_warning" in r.json()
    assert len(_rows(db, "SELECT id FROM invoices WHERE type = 'incoming'")) == 1


def test_invoice_form_from_manager_cannot_mark_paid(isolated_db, client):
    """Менеджер не видит суммы прихода — и отмечать её оплаченной не может."""
    db = isolated_db
    sup = _supplier(db)
    pid = _product(db, "Фильтр")
    r = _post(client, 1, "/api/wh/invoices/create", type="incoming", counterparty_id=sup,
              items=[{"product_id": pid, "quantity": 1, "price_cents": 100_000}],
              supplier_payment_type="paid", idempotency_key="inv-2")
    assert r.status_code == 200, r.text
    assert "supplier_terms" not in r.json()
    assert _rows(db, "SELECT * FROM supplier_invoice_terms") == []
