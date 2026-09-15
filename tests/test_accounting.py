"""
Бухгалтерия, этап 1 (`services/accounting.py`, `/api/acc/*`).

Что стережём:
* выключатель: пока бухгалтерия выключена, новые ручки отказывают, а старый
  поток «Отметить оплату» работает как раньше;
* «Получил деньги» по долгу частями в двух валютах: курс менеджера и курс ЦБ
  хранятся оба, долг гасится обычным платежом в валюте заказа по выбранному
  курсу, в кассе остаются именно сумы;
* остатки считаются только по действующим документам — отклонённый старой
  кнопкой платёж выводит документ из остатков сам;
* расход, перевод, обмен (равенство сторон в базовой валюте), сверка;
* отмена с причиной (без физического удаления), сторно подтверждённого
  платежа, идемпотентность записи и права.

БД настоящая (isolated_db), граница с Telegram — заглушка отправки.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

BOSS, MGR, MGR2, BOOK = 100, 200, 300, 500
CBU_UZS = 12650.5  # сум за доллар по ЦБ на сегодня


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(isolated_db, monkeypatch):
    import importlib

    import services.machines as machines
    import services.notifier as notifier
    import services.roles as roles
    import webapp.server as server
    from services import accounting as acc

    importlib.reload(roles)
    importlib.reload(machines)
    db = isolated_db
    roles.invalidate_all_roles()
    db.set_role(BOSS, "boss", "Boss", "boss")
    db.set_role(MGR, "mgr", "Manager", "manager")
    db.set_role(MGR2, "mgr2", "Manager2", "manager")
    db.set_role(BOOK, "book", "Book", "bookkeeper")
    db.set_currency_rate("UZS", 1 / CBU_UZS, BOSS)
    db.set_currency_rate_daily("UZS", acc.today_str(), 1 / CBU_UZS, "cbu")

    sent: list[tuple] = []

    async def _fake_send(chat_id, text, **kwargs):
        sent.append((chat_id, text))

    monkeypatch.setattr(notifier, "tg_send_message", _fake_send)
    monkeypatch.setattr(
        server, "verify_init_data", lambda init_data: {"id": int(init_data), "first_name": "U"}
    )
    client = TestClient(server.app)

    class Env:
        pass

    e = Env()
    e.db, e.acc, e.client, e.sent = db, acc, client, sent
    return e


def actor(acc, uid):
    role = {BOSS: "boss", MGR: "manager", MGR2: "manager", BOOK: "bookkeeper"}[uid]
    return acc.Actor(user_id=uid, name=f"U{uid}", role=role)


def make_order(db, owner=MGR, *, currency="USD", qty=10, price=100.0, status="approved"):
    oid = db.create_order(owner, "Manager", "")
    db.update_order_agent(oid, "cp-1", "ООО Ромашка")
    db.add_order_item(oid, "Товар", "", qty, "шт", price)
    _run(db.set_order_payment(oid, "credit", "2030-01-01"))
    if currency != "USD":
        _run(_set_currency(oid, currency))
    db.update_order_status(oid, status)
    return oid


async def _set_currency(oid, currency):
    from services import adb_core

    await adb_core.execute("UPDATE orders SET currency = $1 WHERE id = $2", currency, oid)


def enable(e):
    _run(e.acc.set_enabled(actor(e.acc, BOSS), True))


def account(e, name, kind, currency, opening=None, **extra):
    data = {"name": name, "kind": kind, "currency": currency, **extra}
    if opening is not None:
        data["opening"] = opening
    return _run(e.acc.save_account(actor(e.acc, BOSS), data))["id"]


def balances(e):
    return {a["name"]: a["balance_cents"] for a in _run(e.acc.balances())["accounts"]}


def post(e, path, uid, **body):
    return e.client.post(path, json={"initData": str(uid), **body})


# ─── Выключатель ─────────────────────────────────────────────────────────────


def test_disabled_by_default_new_endpoints_refuse_and_old_flow_untouched(env):
    e = env
    assert _run(e.acc.is_enabled()) is False
    me = post(e, "/api/me", MGR).json()
    assert me["accounting_enabled"] is False
    for path in ("/api/acc/balances", "/api/acc/accounts", "/api/acc/journal",
                 "/api/acc/receipt_targets"):
        r = post(e, path, MGR)
        assert r.status_code == 409, (path, r.text)
        assert "выключена" in r.json()["detail"]
    r = post(e, "/api/acc/receipt", MGR, order_id=1, lines=[], idempotency_key="k")
    assert r.status_code == 409

    # Без бухгалтерии оплата вносится разбивкой «как получены деньги» →
    # pending-платёж в валюте заказа, журнал денег не трогается.
    oid = make_order(e.db)
    r = post(e, "/api/orders/mark_paid", MGR, order_id=oid,
             parts=[{"method": "bank", "currency": "USD", "amount": 300}], idempotency_key="old-1")
    assert r.status_code == 200, r.text
    pays = _run(e.db.get_payments_for_order(oid))
    assert [(p["amount_cents"], p["status"]) for p in pays] == [(30000, "pending")]
    # Ни одной строки журнала старый поток не пишет.
    assert e.db.get_setting("accounting_enabled", False) is False
    from services import adb_core

    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM acc_docs")) == 0


def test_only_boss_toggles(env):
    e = env
    r = post(e, "/api/acc/settings", MGR, enabled=True)
    assert r.status_code == 403
    r = post(e, "/api/acc/settings", BOSS, enabled=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is True
    assert body["start_date"] == e.acc.today_str()
    assert post(e, "/api/me", MGR).json()["accounting_enabled"] is True
    assert post(e, "/api/acc/state", MGR).json()["can_manage"] is False


# ─── Справочник счетов ───────────────────────────────────────────────────────


def test_accounts_opening_balance_and_rules(env):
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD", opening="1500")
    card = account(e, "Humo Али", "card", "UZS", opening="2 000 000", bank="Humo",
                   card_last4="1234", holder="Али")
    assert balances(e) == {"Касса USD": 150000, "Humo Али": 200000000}
    listed = {a["id"]: a for a in _run(e.acc.list_accounts())}
    assert listed[card]["card_last4"] == "1234" and listed[card]["holder"] == "Али"
    assert listed[cash]["opening_cents"] == 150000

    # Менеджер счета не заводит.
    r = post(e, "/api/acc/accounts/save", MGR, name="X", kind="cash", currency="USD")
    assert r.status_code == 403
    with pytest.raises(e.acc.AccountingError, match="4"):
        account(e, "Visa", "card", "USD", card_last4="12")
    with pytest.raises(e.acc.AccountingError, match="Тип"):
        account(e, "Сейф", "safe", "USD")

    # Правка начального остатка: старый документ отменяется с причиной, новый проводится.
    _run(e.acc.save_account(actor(e.acc, BOSS), {
        "account_id": cash, "name": "Касса USD", "kind": "cash", "currency": "USD", "opening": "1000",
    }))
    assert balances(e)["Касса USD"] == 100000
    from services import adb_core

    kinds = _run(adb_core.fetch("SELECT kind, status, void_reason FROM acc_docs ORDER BY id"))
    assert [(k["kind"], k["status"]) for k in kinds] == [
        ("opening", "void"), ("opening", "posted"), ("opening", "posted")]
    assert "изменён" in kinds[0]["void_reason"]

    # Валюту счёта с операциями не меняют.
    with pytest.raises(e.acc.AccountingError, match="Валюту"):
        _run(e.acc.save_account(actor(e.acc, BOSS), {
            "account_id": cash, "name": "Касса", "kind": "cash", "currency": "UZS"}))

    # Архивный счёт не виден в «Деньги сейчас» и не принимает операции.
    _run(e.acc.set_archived(actor(e.acc, BOSS), card, True))
    assert "Humo Али" not in balances(e)
    with pytest.raises(e.acc.AccountingError, match="архиве"):
        _run(e.acc.record_expense(actor(e.acc, MGR), {
            "account_id": card, "amount": "1000", "note": "такси", "idempotency_key": "x"}))


# ─── «Получил деньги» ────────────────────────────────────────────────────────


def test_receipt_two_currencies_manual_rate_closes_usd_debt(env):
    """Долг 1 000 USD: 400 USD наличными + 7 620 000 сум на карту по курсу
    менеджера 12 700 → платёж 1 000 USD, долг закрывается подтверждением."""
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD")
    card = account(e, "Humo Али", "card", "UZS")
    oid = make_order(e.db)
    res = _run(e.acc.record_receipt(actor(e.acc, MGR), {
        "order_id": oid,
        "lines": [{"account_id": cash, "amount": "400"},
                  {"account_id": card, "amount": "7 620 000"}],
        "rates": {"UZS": "12700"},
        "note": "частями",
        "idempotency_key": "r-1",
    }))
    assert res["ok"] and res["payment_status"] == "pending"
    assert res["credited_cents"] == 100000 and res["target_currency"] == "USD"
    assert res["claimable_cents"] == 0

    pays = _run(e.db.get_payments_for_order(oid))
    assert [(p["amount_cents"], p["currency"], p["status"]) for p in pays] == [(100000, "USD", "pending")]
    # В кассе — именно та валюта, что получили.
    assert balances(e) == {"Касса USD": 40000, "Humo Али": 762000000}

    from services import adb_core

    entries = _run(adb_core.fetch("SELECT * FROM acc_entries ORDER BY id"))
    uzs = entries[1]
    assert uzs["currency"] == "UZS" and uzs["rate"] == "12700"
    assert uzs["rate_source"] == "manual" and Decimal(uzs["cbu_rate"]) == Decimal("12650.5")
    assert uzs["amount_base_cents"] == 60000 and uzs["target_cents"] == 60000
    assert entries[0]["rate_source"] == "base"

    # Подтверждение — обычный путь босса: долг закрыт.
    assert _run(e.db.confirm_all_pending_payments_for_order(oid, BOSS, "Boss")) == 1
    from services.debts import calc_order_balance

    assert _run(calc_order_balance(oid)).remaining_cents == 0
    order = _run(adb_core.fetchrow("SELECT paid_confirmed_at FROM orders WHERE id = $1", oid))
    assert order["paid_confirmed_at"] is not None


def test_receipt_defaults_to_cbu_and_boss_is_confirmed_at_once(env):
    e = env
    enable(e)
    card = account(e, "Uzcard", "card", "UZS")
    oid = make_order(e.db, qty=1, price=100.0)
    res = _run(e.acc.record_receipt(actor(e.acc, BOSS), {
        "order_id": oid, "lines": [{"account_id": card, "amount": "632525"}],
        "idempotency_key": "r-cbu",
    }))
    # 632 525 / 12 650.5 = 50.00 USD
    assert res["credited_cents"] == 5000
    assert res["payment_status"] == "confirmed"
    from services import adb_core

    entry = _run(adb_core.fetchrow("SELECT rate, rate_source FROM acc_entries"))
    assert entry == {"rate": "12650.5", "rate_source": "cbu"}
    assert res["remaining_cents"] == 5000


def test_uzs_order_paid_in_dollars_by_manager_rate(env):
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD")
    oid = make_order(e.db, currency="UZS", qty=1, price=12_700_000)
    res = _run(e.acc.record_receipt(actor(e.acc, MGR), {
        "order_id": oid, "lines": [{"account_id": cash, "amount": "500"}],
        "rates": {"UZS": {"rate": "12800"}}, "idempotency_key": "uzs-1",
    }))
    assert res["target_currency"] == "UZS"
    assert res["credited_cents"] == 640_000_000  # 500 × 12 800 сум
    pays = _run(e.db.get_payments_for_order(oid))
    assert pays[0]["currency"] == "UZS" and pays[0]["amount_cents"] == 640_000_000


def test_overpayment_refused_and_rounding_tolerated(env):
    e = env
    enable(e)
    card = account(e, "Humo", "card", "UZS")
    oid = make_order(e.db, qty=1, price=999.99)
    with pytest.raises(e.acc.AccountingError, match="больше остатка"):
        _run(e.acc.record_receipt(actor(e.acc, MGR), {
            "order_id": oid, "lines": [{"account_id": card, "amount": "13 000 000"}],
            "rates": {"UZS": "12700"}, "idempotency_key": "over"}))
    from services import adb_core

    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM acc_docs")) == 0
    assert _run(e.db.get_payments_for_order(oid)) == []
    # Круглая сумма на копейку больше долга — принимается, зачёт по остатку.
    res = _run(e.acc.record_receipt(actor(e.acc, MGR), {
        "order_id": oid, "lines": [{"account_id": card, "amount": "12 700 000"}],
        "rates": {"UZS": "12700"}, "idempotency_key": "round"}))
    assert res["credited_cents"] == 99999


def test_receipt_permissions_and_guards(env):
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD")
    foreign = make_order(e.db, owner=MGR2)
    with pytest.raises(e.acc.AccountingError) as ei:
        _run(e.acc.record_receipt(actor(e.acc, MGR), {
            "order_id": foreign, "lines": [{"account_id": cash, "amount": "10"}], "idempotency_key": "p"}))
    assert ei.value.status == 403
    draft = make_order(e.db, status="draft")
    with pytest.raises(e.acc.AccountingError, match="не одобрен"):
        _run(e.acc.record_receipt(actor(e.acc, MGR), {
            "order_id": draft, "lines": [{"account_id": cash, "amount": "10"}], "idempotency_key": "d"}))
    oid = make_order(e.db)
    card = account(e, "Humo", "card", "UZS")
    with pytest.raises(e.acc.AccountingError, match="Курс UZS"):
        _run(e.acc.record_receipt(actor(e.acc, MGR), {
            "order_id": oid, "lines": [{"account_id": card, "amount": "10000"}],
            "rates": {"UZS": "abc"}, "idempotency_key": "bad-rate"}))


def test_receipt_is_idempotent(env):
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD")
    oid = make_order(e.db)
    body = {"order_id": oid, "lines": [{"account_id": cash, "amount": "100"}], "idempotency_key": "same"}
    first = _run(e.acc.record_receipt(actor(e.acc, MGR), dict(body)))
    second = _run(e.acc.record_receipt(actor(e.acc, MGR), dict(body)))
    assert second["repeated"] is True and second["doc_id"] == first["doc_id"]
    assert len(_run(e.db.get_payments_for_order(oid))) == 1
    assert balances(e)["Касса USD"] == 10000
    # Через API — то же, а боссу ушёл один пуш с кнопками подтверждения.
    body["idempotency_key"] = "api-same"
    r1 = post(e, "/api/acc/receipt", MGR, **body).json()
    r2 = post(e, "/api/acc/receipt", MGR, **body).json()
    assert r1["doc_id"] == r2["doc_id"] and r2["repeated"] is True
    pushes = [t for _, t in e.sent if "подтверждение оплаты" in t.lower()]
    assert len(pushes) == 1
    assert balances(e)["Касса USD"] == 20000


def test_payment_rejected_in_old_flow_removes_doc_from_balances(env):
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD")
    oid = make_order(e.db)
    res = _run(e.acc.record_receipt(actor(e.acc, MGR), {
        "order_id": oid, "lines": [{"account_id": cash, "amount": "250"}], "idempotency_key": "rej"}))
    assert balances(e)["Касса USD"] == 25000
    assert _run(e.db.reject_all_pending_payments_for_order(oid, BOSS, "Boss")) == 1
    assert balances(e)["Касса USD"] == 0
    docs = _run(e.acc.journal(actor(e.acc, BOSS), {}))["docs"]
    assert docs[0]["id"] == res["doc_id"] and docs[0]["state"] == "payment_rejected"


def test_machine_installment_receipt_and_void(env):
    from services import adb_core, machines

    e = env
    enable(e)
    card = account(e, "Humo", "card", "UZS")
    m = _run(machines.create_machine(vin="VIN-1", name="JCB", created_by=BOSS, price_cents=2_500_000))
    deal = _run(machines.create_deal(
        m["machine_id"], kind="credit", price_cents=2_500_000, buyer_name="Иванов",
        created_by=BOSS, down_payment_cents=500_000, months=4))
    assert deal["ok"], deal
    deal_id = deal["deal_id"]
    targets = _run(e.acc.receipt_targets(actor(e.acc, BOSS)))
    assert targets["deals"][0]["remaining_cents"] == 2_000_000
    # Рассрочки ведёт менеджер (решение владельца): видит и записывает сам.
    assert [d["deal_id"] for d in _run(e.acc.receipt_targets(actor(e.acc, MGR)))["deals"]] == [deal_id]
    mine = _run(e.acc.record_receipt(actor(e.acc, MGR), {
        "deal_id": deal_id, "lines": [{"account_id": card, "amount": "12700"}],
        "rates": {"UZS": "12700"}, "idempotency_key": "m"}))
    assert mine["credited_cents"] == 100
    # Стирает деньги по рассрочке руководитель — как удаление поступления в ручке.
    with pytest.raises(e.acc.AccountingError):
        _run(e.acc.void_doc(actor(e.acc, MGR), {"doc_id": mine["doc_id"], "reason": "проверка"}))
    _run(e.acc.void_doc(actor(e.acc, BOSS), {"doc_id": mine["doc_id"], "reason": "проверка"}))

    res = _run(e.acc.record_receipt(actor(e.acc, BOSS), {
        "deal_id": deal_id, "lines": [{"account_id": card, "amount": "63 500 000"}],
        "rates": {"UZS": "12700"}, "idempotency_key": "deal-1"}))
    assert res["credited_cents"] == 500_000 and res["deal_closed"] is False
    receipts = _run(adb_core.fetch("SELECT id, amount_cents FROM machine_payment_receipts"))
    assert receipts == [{"id": res["machine_receipt_id"], "amount_cents": 500_000}]
    assert _run(e.acc.receipt_targets(actor(e.acc, BOSS)))["deals"][0]["remaining_cents"] == 1_500_000

    _run(e.acc.void_doc(actor(e.acc, BOSS), {"doc_id": res["doc_id"], "reason": "ошибка суммы"}))
    assert _run(adb_core.fetch("SELECT id FROM machine_payment_receipts")) == []
    assert balances(e)["Humo"] == 0


# ─── Расход, перевод, обмен ──────────────────────────────────────────────────


def test_expense_requires_note_and_manager_sees_only_own(env):
    e = env
    enable(e)
    cash = account(e, "Касса UZS", "cash", "UZS", opening="1000000")
    with pytest.raises(e.acc.AccountingError, match="на что"):
        _run(e.acc.record_expense(actor(e.acc, MGR), {
            "account_id": cash, "amount": "50000", "idempotency_key": "e0"}))
    _run(e.acc.record_expense(actor(e.acc, MGR), {
        "account_id": cash, "amount": "50 000", "note": "Болты и скотч", "category": "Хозтовары",
        "idempotency_key": "e1"}))
    _run(e.acc.record_expense(actor(e.acc, MGR2), {
        "account_id": cash, "amount": "20000", "note": "Такси", "idempotency_key": "e2"}))
    assert balances(e)["Касса UZS"] == 100_000_000 - 7_000_000
    mine = _run(e.acc.journal(actor(e.acc, MGR), {}))["docs"]
    assert [d["note"] for d in mine] == ["Болты и скотч"]
    everything = _run(e.acc.journal(actor(e.acc, BOSS), {"kind": "expense"}))["docs"]
    assert {d["note"] for d in everything} == {"Болты и скотч", "Такси"}
    entry = everything[-1]["entries"][0]
    assert entry["direction"] == "out" and entry["rate_source"] == "cbu"


def test_transfer_and_exchange(env):
    from services import adb_core

    e = env
    enable(e)
    mgr_cash = account(e, "Наличные Али", "cash", "USD", opening="1000")
    office = account(e, "Касса офис", "cash", "USD")
    uzs = account(e, "Касса UZS", "cash", "UZS")
    # Сдача наличных «менеджер → касса».
    t = _run(e.acc.record_transfer(actor(e.acc, MGR), {
        "from_account_id": mgr_cash, "to_account_id": office, "amount": "600", "idempotency_key": "t1"}))
    assert t["kind"] == "transfer"
    with pytest.raises(e.acc.AccountingError, match="совпадают"):
        _run(e.acc.record_transfer(actor(e.acc, MGR), {
            "from_account_id": office, "to_account_id": office, "amount": "1", "idempotency_key": "t2"}))
    # Обмен 100 USD → 1 275 000 сум по факту.
    x = _run(e.acc.record_transfer(actor(e.acc, BOSS), {
        "from_account_id": office, "to_account_id": uzs, "amount": "100", "amount_in": "1 275 000",
        "idempotency_key": "x1"}))
    assert x["kind"] == "exchange"
    assert balances(e) == {"Наличные Али": 40000, "Касса офис": 50000, "Касса UZS": 127_500_000}
    rows = _run(adb_core.fetch(
        "SELECT direction, currency, rate, rate_source, cbu_rate, amount_base_cents FROM acc_entries "
        "WHERE doc_id = $1 ORDER BY id", x["doc_id"]))
    assert rows[0]["amount_base_cents"] == rows[1]["amount_base_cents"] == 10000
    assert rows[1]["rate"] == "12750" and rows[1]["rate_source"] == "manual"
    assert Decimal(rows[1]["cbu_rate"]) == Decimal("12650.5")
    with pytest.raises(e.acc.AccountingError, match="получили"):
        _run(e.acc.record_transfer(actor(e.acc, BOSS), {
            "from_account_id": office, "to_account_id": uzs, "amount": "1", "idempotency_key": "x2"}))


def test_balances_total_in_base_currency(env):
    e = env
    enable(e)
    account(e, "Касса USD", "cash", "USD", opening="100")
    account(e, "Humo", "card", "UZS", opening="1 265 050")
    res = _run(e.acc.balances())
    assert res["total_base_cents"] == 20000 and res["partial"] is False
    assert res["base_currency"] == "USD"


# ─── Закрыть день ────────────────────────────────────────────────────────────


def test_close_day_records_difference(env):
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD", opening="1000")
    with pytest.raises(e.acc.AccountingError, match="причину"):
        _run(e.acc.close_day(actor(e.acc, MGR), {
            "account_id": cash, "counted": "990", "idempotency_key": "c0"}))
    res = _run(e.acc.close_day(actor(e.acc, MGR), {
        "account_id": cash, "counted": "990", "note": "не нашли 10", "idempotency_key": "c1"}))
    assert (res["expected_cents"], res["counted_cents"], res["diff_cents"]) == (100000, 99000, -1000)
    assert balances(e)["Касса USD"] == 99000
    again = _run(e.acc.close_day(actor(e.acc, MGR), {
        "account_id": cash, "counted": "990", "note": "не нашли 10", "idempotency_key": "c1"}))
    assert again["repeated"] and again["doc_id"] == res["doc_id"]
    # Сошлось — закрытие всё равно записано, без движения.
    ok = _run(e.acc.close_day(actor(e.acc, MGR), {
        "account_id": cash, "counted": "990", "idempotency_key": "c2"}))
    assert ok["diff_cents"] == 0
    assert balances(e)["Касса USD"] == 99000
    b = next(a for a in _run(e.acc.balances())["accounts"] if a["id"] == cash)
    assert b["last_close_date"] == e.acc.today_str()
    assert b["today_out_cents"] == 1000


# ─── Отмена ──────────────────────────────────────────────────────────────────


def test_void_rules(env):
    from services import adb_core

    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD", opening="500")
    exp = _run(e.acc.record_expense(actor(e.acc, MGR), {
        "account_id": cash, "amount": "20", "note": "обед", "idempotency_key": "v1"}))
    with pytest.raises(e.acc.AccountingError, match="причину"):
        _run(e.acc.void_doc(actor(e.acc, MGR), {"doc_id": exp["doc_id"], "reason": ""}))
    with pytest.raises(e.acc.AccountingError) as ei:
        _run(e.acc.void_doc(actor(e.acc, MGR2), {"doc_id": exp["doc_id"], "reason": "чужое"}))
    assert ei.value.status == 403
    _run(e.acc.void_doc(actor(e.acc, MGR), {"doc_id": exp["doc_id"], "reason": "задвоил"}))
    assert balances(e)["Касса USD"] == 50000
    row = _run(adb_core.fetchrow("SELECT status, void_reason, voided_by FROM acc_docs WHERE id = $1",
                                 exp["doc_id"]))
    assert row == {"status": "void", "void_reason": "задвоил", "voided_by": MGR}
    # Документ не удалён, строки на месте.
    assert _run(adb_core.fetchval("SELECT COUNT(*) FROM acc_entries WHERE doc_id = $1", exp["doc_id"])) == 1

    # Отмена поступления с pending-платежом отклоняет платёж.
    oid = make_order(e.db)
    rec = _run(e.acc.record_receipt(actor(e.acc, MGR), {
        "order_id": oid, "lines": [{"account_id": cash, "amount": "100"}], "idempotency_key": "v2"}))
    _run(e.acc.void_doc(actor(e.acc, MGR), {"doc_id": rec["doc_id"], "reason": "не тот заказ"}))
    assert _run(e.db.get_payments_for_order(oid))[0]["status"] == "rejected"


def test_boss_reverses_confirmed_receipt_and_reopens_order(env):
    from services import adb_core
    from services.debts import calc_order_balance

    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD")
    oid = make_order(e.db, qty=1, price=300)
    rec = _run(e.acc.record_receipt(actor(e.acc, BOSS), {
        "order_id": oid, "lines": [{"account_id": cash, "amount": "300"}], "idempotency_key": "b1"}))
    assert rec["payment_status"] == "confirmed" and rec["remaining_cents"] == 0
    assert _run(adb_core.fetchval("SELECT paid_confirmed_at FROM orders WHERE id = $1", oid))
    _run(e.acc.void_doc(actor(e.acc, BOSS), {"doc_id": rec["doc_id"], "reason": "ошибка"}))
    assert _run(e.db.get_payments_for_order(oid))[0]["status"] == "rejected"
    assert _run(adb_core.fetchval("SELECT paid_confirmed_at FROM orders WHERE id = $1", oid)) is None
    assert _run(calc_order_balance(oid)).remaining_cents == 30000
    assert balances(e)["Касса USD"] == 0


def test_manager_cannot_void_confirmed_receipt(env):
    e = env
    enable(e)
    cash = account(e, "Касса USD", "cash", "USD")
    oid = make_order(e.db)
    rec = _run(e.acc.record_receipt(actor(e.acc, MGR), {
        "order_id": oid, "lines": [{"account_id": cash, "amount": "100"}], "idempotency_key": "mc"}))
    _run(e.db.confirm_all_pending_payments_for_order(oid, BOSS, "Boss"))
    with pytest.raises(e.acc.AccountingError, match="подтверждён"):
        _run(e.acc.void_doc(actor(e.acc, MGR), {"doc_id": rec["doc_id"], "reason": "ошибка"}))


# ─── API: роли и формат ──────────────────────────────────────────────────────


def test_api_flow_and_roles(env):
    e = env
    post(e, "/api/acc/settings", BOSS, enabled=True)
    r = post(e, "/api/acc/accounts/save", BOSS, name="Касса USD", kind="cash", currency="USD", opening="10")
    assert r.status_code == 200, r.text
    acc_id = r.json()["account"]["id"]
    assert post(e, "/api/acc/accounts", MGR).json()["accounts"][0]["id"] == acc_id
    r = post(e, "/api/acc/expense", MGR, account_id=acc_id, amount="3", note="вода")
    assert r.status_code == 400  # без ключа идемпотентности денежная запись не принимается
    r = post(e, "/api/acc/expense", MGR, account_id=acc_id, amount="3", note="вода", idempotency_key="a1")
    assert r.status_code == 200, r.text
    bal = post(e, "/api/acc/balances", MGR).json()
    assert bal["accounts"][0]["balance_cents"] == 700 and bal["can_manage"] is False
    doc = post(e, "/api/acc/doc", BOSS, doc_id=r.json()["doc_id"]).json()
    assert doc["kind"] == "expense" and doc["can_void"] is True
    # Чужой документ менеджеру «не найден», а не показан.
    assert post(e, "/api/acc/doc", MGR2, doc_id=r.json()["doc_id"]).status_code == 404
    rates = post(e, "/api/acc/rates", MGR).json()
    assert rates["rates"]["UZS"]["cbu"] == "12650.5"
    # Бухгалтер видит всё; гость — никуда.
    assert post(e, "/api/acc/journal", BOOK).status_code == 200
    e.db.set_role(999, "g", "Guest", "guest")
    assert post(e, "/api/acc/balances", 999).status_code == 403
