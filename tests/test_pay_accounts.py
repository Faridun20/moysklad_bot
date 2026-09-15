"""
«Куда поступили деньги» (services/pay_accounts.py) — карты и счета, на которые
клиент платит картой и перечислением. SQLite, настоящая БД.

Что держит этот файл:
* форма: 4 цифры карты (полный номер отвергается, не обрезается), владелец,
  20 цифр расчётного счёта, ИНН/МФО, валюта по коду в номере;
* подписи «на карту •••• 1234 (Фаридун М.)» / «на счёт ООО … (…6789)» — одни
  на все места показа (карточки, пуш руководителю, дайджест, аудит, лента);
* оплата картой/перечислением без записи — 400 с текстом, наличные как были,
  старые строки без записи законны;
* справочник: тёзка не заводится (`existed`), архив не предлагается, но на
  старых платежах остаётся; правка номера у записи с деньгами — отказ;
* права: завести — менеджер и руководство; править/архив — руководство,
  менеджер только без руководителя (с пометкой в аудите);
* рассрочка: то же «куда» у поступления картой/перечислением;
* справочник не зависит от выключателя бухгалтерии.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import TEST_BANK, TEST_CARD, pay_account_id
from tests import test_order_payments as _op
from tests.test_order_payments import BOSS, MGR, MGR2, _order, _post, _rows

# Фикстуры разбивки оплаты (роли, курс ЦБ, клиент с перехватом пушей) — те же.
db = _op.db
client = _op.client

KEEPER = 7


def _run(coro):
    return asyncio.run(coro)


def _pa():
    from services import pay_accounts

    return pay_accounts


def _validate(kind, **data):
    return _pa().validate(kind, data, allowed_currencies=["USD", "UZS"], default_currency="USD")


def _card_row(amount, account_id=None, cur="USD"):
    row = {"method": "card", "currency": cur, "amount": str(amount)}
    if account_id is not None:
        row["account_id"] = account_id
    return row


# ─── Чистые функции ──────────────────────────────────────────────────────────


def test_card_form_keeps_only_last4_and_refuses_full_number():
    pa = _pa()
    got = _validate("card", holder="  Фаридун   М. ", card_last4="1234", bank="Kapitalbank")
    assert (got["holder"], got["card_last4"], got["currency"]) == ("Фаридун М.", "1234", "USD")
    assert got["name"] == "Карта •••• 1234 · Фаридун М."
    assert _validate("card", holder="Али", card_last4="•••• 5678")["card_last4"] == "5678"
    for raw, text in (("8600 1234 5678 1234", "полный номер карты не храним"), ("123", "ровно 4"),
                      ("12a4", "ровно 4"), ("", "ровно 4")):
        with pytest.raises(pa.AccountError) as e:
            _validate("card", holder="Али", card_last4=raw)
        assert text in e.value.message, raw
    with pytest.raises(pa.AccountError) as e:
        _validate("card", holder=" ", card_last4="1234")
    assert "владельца карты" in e.value.message


def test_bank_form_needs_company_and_20_digits_currency_from_number():
    pa = _pa()
    got = _validate("bank", holder="ООО Farid Impeks", account_number="2020 8000 9001 1223 6789",
                    mfo="01158", company_tin="301234567", currency="USD")
    assert got["account_number"] == "20208000900112236789"
    assert got["currency"] == "UZS", "код 000 в номере — сумовой счёт, что бы ни пришло в форме"
    assert _validate("bank", holder="ООО", account_number="20208840900112236789")["currency"] == "USD"
    for data, text in (
        ({"holder": "", "account_number": "20208840900112236789"}, "фирму"),
        ({"holder": "ООО", "account_number": "2020884090011223678"}, "20 цифр"),
        ({"holder": "ООО", "account_number": "2020884090011223678X"}, "20 цифр"),
        ({"holder": "ООО", "account_number": "20208840900112236789", "mfo": "1"}, "МФО"),
        ({"holder": "ООО", "account_number": "20208840900112236789", "company_tin": "12345"}, "ИНН"),
    ):
        with pytest.raises(pa.AccountError) as e:
            _validate("bank", **data)
        assert text in e.value.message, data


def test_destination_labels_are_the_same_everywhere():
    pa = _pa()
    from services.order_payments import part_label

    card = {"kind": "card", "card_last4": "1234", "holder": "Фаридун М.", "name": "x"}
    bank = {"kind": "bank", "holder": "ООО Farid Impeks", "account_number": "20208840900112236789"}
    assert pa.destination_label("card", card) == "на карту •••• 1234 (Фаридун М.)"
    assert pa.destination_label("bank", bank) == "на счёт ООО Farid Impeks (…6789)"
    # Записи бухгалтерии без реквизитов подписываются названием.
    assert pa.destination_label("card", {"kind": "card", "name": "Humo Али"}) == "на карту «Humo Али»"
    assert pa.destination_label("bank", {"kind": "bank", "name": "Kapitalbank USD"}) == "на счёт Kapitalbank USD"
    assert pa.destination_label("card", None) is None
    assert part_label("card", 713_000, "USD", card) == "на карту •••• 1234 (Фаридун М.) · 7 130 USD"
    assert part_label("bank", 100, "USD", bank) == "на счёт ООО Farid Impeks (…6789) · 1 USD"
    assert part_label("card", 713_000, "USD") == "на карту 7 130 USD", "старая строка без записи"
    assert part_label("cash", 500_000, "USD", card) == "наличные 5 000 USD", "у наличных «куда» нет"


def test_parse_parts_requires_account_for_card_and_bank_only():
    from services.order_payments import PaymentError, parse_parts

    rows = parse_parts([{"method": "cash", "currency": "USD", "amount": "1", "account_id": 5},
                        _card_row(2, account_id="7")])
    assert [r.account_id for r in rows] == [None, 7]
    with pytest.raises(PaymentError) as e:
        parse_parts([{"method": "cash", "currency": "USD", "amount": "1"}, _card_row(2)])
    assert e.value.code == "account_required" and "Строка 2" in e.value.message
    assert "последние 4 цифры и владелец" in e.value.message
    with pytest.raises(PaymentError) as e:
        parse_parts([{"method": "bank", "currency": "USD", "amount": "1"}])
    assert "фирма и номер счёта" in e.value.message
    with pytest.raises(PaymentError) as e:
        parse_parts([_card_row(1, account_id="abc")])
    assert e.value.code == "account_invalid"
    # Разовый перенос старых отметок: «куда» не восстановить — не требуется.
    assert parse_parts([_card_row(1)], require_account=False)[0].account_id is None


# ─── Справочник ──────────────────────────────────────────────────────────────


def test_same_card_is_not_created_twice_and_archived_twin_is_refused(db):
    pa = _pa()
    mgr = pa.Actor(MGR, "Manager", "manager")
    first = _run(pa.create_account(mgr, TEST_CARD))
    again = _run(pa.create_account(mgr, {**TEST_CARD, "holder": "фаридун  м"}))
    assert first["existed"] is False and again["existed"] is True
    assert again["account"]["id"] == first["account"]["id"]
    other = _run(pa.create_account(mgr, {**TEST_CARD, "holder": "Али"}))
    assert other["existed"] is False, "та же четвёрка цифр у другого владельца — другая карта"
    bank = _run(pa.create_account(mgr, TEST_BANK))
    assert _run(pa.create_account(mgr, {**TEST_BANK, "holder": "Другое имя"}))["account"]["id"] == bank["account"]["id"]

    _run(pa.set_archived(pa.Actor(BOSS, "Boss", "boss"), first["account"]["id"], True))
    with pytest.raises(pa.AccountError) as e:
        _run(pa.create_account(mgr, TEST_CARD))
    assert e.value.status == 409 and "в архив" in e.value.message
    assert [a["id"] for a in _run(pa.list_accounts()) if a["kind"] == "card"] == [other["account"]["id"]]
    with pytest.raises(pa.AccountError) as e:
        _run(pa.create_account(pa.Actor(KEEPER, "Keeper", "warehouse_keeper"), TEST_CARD))
    assert e.value.status == 403
    audit = _rows(db, "SELECT details FROM audit_log WHERE action = 'pay_account_created'")
    assert any("на карту •••• 1234 (Фаридун М.)" in a["details"] for a in audit)


def test_full_card_number_never_reaches_the_database(db):
    pa = _pa()
    with pytest.raises(pa.AccountError):
        _run(pa.create_account(pa.Actor(MGR, "M", "manager"),
                               {"kind": "card", "holder": "Али", "card_last4": "8600123456781234"}))
    assert _rows(db, "SELECT COUNT(*) AS n FROM acc_accounts")[0]["n"] == 0


def test_account_with_money_keeps_its_number_but_holder_can_be_fixed(db):
    pa = _pa()
    from services import order_payments

    boss = pa.Actor(BOSS, "Boss", "boss")
    acc_id = pay_account_id("card")
    oid = _order(db, total=100.0)
    _run(order_payments.record_payment_parts(oid, order_payments.Actor(MGR, "M", "manager"),
                                             [_card_row(100, acc_id)]))
    with pytest.raises(pa.AccountError) as e:
        _run(pa.update_account(boss, acc_id, {"card_last4": "9999"}))
    assert e.value.code == "in_use"
    fixed = _run(pa.update_account(boss, acc_id, {"holder": "Фаридун Масуджанов"}))
    assert fixed["account"]["label"] == "на карту •••• 1234 (Фаридун Масуджанов)"
    part = _run(order_payments.parts_for_orders([oid]))[oid][0]
    assert part["account_label"] == "на карту •••• 1234 (Фаридун Масуджанов)"


# ─── Оплата заказа ───────────────────────────────────────────────────────────


def test_api_card_without_account_is_400_cash_is_unaffected(db, client):
    oid = _order(db, total=12130.0)
    r = client.post("/api/orders/payment", json={
        "initData": str(MGR), "order_id": oid, "idempotency_key": "k1",
        "parts": [{"method": "cash", "currency": "USD", "amount": 5000}, _card_row(7130)],
    })
    assert r.status_code == 400 and r.json()["code"] == "account_required", r.text
    assert "Строка 2: укажите, на какую карту пришли деньги" in r.json()["detail"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM payments")[0]["n"] == 0
    # Ключ освобождён: та же форма с картой проходит.
    card = pay_account_id("card")
    r = client.post("/api/orders/payment", json={
        "initData": str(MGR), "order_id": oid, "idempotency_key": "k1",
        "parts": [{"method": "cash", "currency": "USD", "amount": 5000}, _card_row(7130, card)],
    })
    assert r.status_code == 200, r.text
    assert [p.get("account_label") for p in r.json()["parts"]] == [None, "на карту •••• 1234 (Фаридун М.)"]
    links = _rows(db, "SELECT pp.method, ppa.account_id FROM payment_parts pp "
                      "LEFT JOIN payment_part_accounts ppa ON ppa.part_id = pp.id ORDER BY pp.id")
    assert links == [{"method": "cash", "account_id": None}, {"method": "card", "account_id": card}]
    # Повтор с тем же ключом — тот же результат, вторых ссылок нет.
    again = client.post("/api/orders/payment", json={
        "initData": str(MGR), "order_id": oid, "idempotency_key": "k1",
        "parts": [{"method": "cash", "currency": "USD", "amount": 5000}, _card_row(7130, card)],
    })
    assert again.json()["payments"] == r.json()["payments"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM payment_part_accounts")[0]["n"] == 1
    # Пуш руководителю называет карту — по ней он сверяет банк.
    pushes = [p for p in client.pushes if "pay_ok" in str(p[2])]
    assert len(pushes) == 1 and "на карту •••• 1234 (Фаридун М.) · 7 130 USD" in pushes[0][1]
    audit = _rows(db, "SELECT details FROM audit_log WHERE action = 'order_payment_recorded'")
    assert "на карту •••• 1234 (Фаридун М.) · 7 130 USD" in audit[0]["details"]


def test_wrong_kind_missing_or_archived_account_is_refused(db):
    pa = _pa()
    from services import order_payments

    actor = order_payments.Actor(MGR, "M", "manager")
    oid = _order(db, total=100.0, payment_type="credit", status="shipped")
    bank = pay_account_id("bank")
    card = pay_account_id("card")
    for row, code, text in (
        (_card_row(10, bank), "account_invalid", "выберите карту"),
        ({"method": "bank", "currency": "USD", "amount": "10", "account_id": card}, "account_invalid", "выберите счёт"),
        (_card_row(10, 99_999), "account_invalid", "не найдены"),
    ):
        with pytest.raises(order_payments.PaymentError) as e:
            _run(order_payments.record_payment_parts(oid, actor, [row]))
        assert e.value.code == code and text in e.value.message, row
    _run(order_payments.record_payment_parts(oid, actor, [_card_row(10, card)]))
    _run(pa.set_archived(pa.Actor(BOSS, "B", "boss"), card, True))
    with pytest.raises(order_payments.PaymentError) as e:
        _run(order_payments.record_payment_parts(oid, actor, [_card_row(10, card)]))
    assert e.value.code == "account_archived"
    # Архивная карта на старом платеже показывается как была.
    part = _run(order_payments.parts_for_orders([oid]))[oid][0]
    assert part["account_label"] == "на карту •••• 1234 (Фаридун М.)" and part["account"]["archived"] is True


def test_old_rows_without_account_stay_valid_and_labelled_as_before(db):
    from services import order_payments

    oid = _order(db, total=100.0, payment_type="credit", status="shipped")
    _run(order_payments.record_payment_parts(oid, order_payments.Actor(MGR, "M", "manager"),
                                             [_card_row(100)], require_account=False))
    part = _run(order_payments.parts_for_orders([oid]))[oid][0]
    assert part["account_id"] is None and part["account_label"] is None
    assert _run(order_payments.parts_by_payment([part["payment_id"]]))[part["payment_id"]]["label"] == "на карту 100 USD"


def test_breakdown_label_reaches_debts_decisions_history_and_digest(db, client):
    from services import boss_digest, order_payments

    oid = _order(db, total=150.0, payment_type="credit", status="shipped")
    bank = pay_account_id("bank")
    _run(order_payments.record_payment_parts(oid, order_payments.Actor(MGR, "M", "manager"), [
        {"method": "bank", "currency": "USD", "amount": "150", "account_id": bank}]))
    label = "на счёт ООО Farid Impeks (…6789)"
    debts = _post(client, BOSS, "/api/debts").json()["debts"]
    assert [p["account_label"] for d in debts if d["id"] == oid for p in d["parts"]] == [label]
    history = _post(client, BOSS, "/api/cash/history", period="month").json()["history"]
    assert [h["account_label"] for h in history if h["kind"] == "payment"] == [label]
    orders = {o["id"]: o for o in _post(client, BOSS, "/api/orders").json()["orders"]}
    assert orders[oid]["payment_parts"][0]["account_label"] == label
    data = _run(boss_digest.gather())
    assert any(label in line for line in data["payments"]["lines"]), data["payments"]


def test_paid_orders_awaiting_confirmation_carry_account(db, client):
    from services import order_payments

    oid = _order(db, total=100.0)
    card = pay_account_id("card")
    _run(order_payments.record_payment_parts(oid, order_payments.Actor(MGR, "M", "manager"),
                                             [_card_row(100, card)]))
    pending = _post(client, BOSS, "/api/payments/pending").json()["pending"]
    assert pending[0]["parts"][0]["account_label"] == "на карту •••• 1234 (Фаридун М.)"


# ─── Ручки справочника и права ───────────────────────────────────────────────


def test_api_list_create_and_last_used_without_accounting(db, client):
    assert db.get_setting("accounting_enabled", False) is False
    r = _post(client, MGR, "/api/pay_accounts/create", kind="card", holder="Фаридун М.", card_last4="1234",
              idempotency_key="c1")
    assert r.status_code == 200 and r.json()["existed"] is False, r.text
    card = r.json()["account"]
    assert card["label"] == "на карту •••• 1234 (Фаридун М.)"
    dup = _post(client, MGR, "/api/pay_accounts/create", kind="card", holder="Фаридун М.", card_last4="1234",
                idempotency_key="c2")
    assert dup.json()["existed"] is True and dup.json()["account"]["id"] == card["id"]
    bad = _post(client, MGR, "/api/pay_accounts/create", kind="bank", holder="ООО", account_number="123")
    assert bad.status_code == 400 and "20 цифр" in bad.json()["detail"]
    assert _post(client, KEEPER, "/api/pay_accounts/create", kind="card", holder="A",
                 card_last4="1111").status_code == 403

    listed = _post(client, MGR, "/api/pay_accounts").json()
    assert [a["id"] for a in listed["accounts"]] == [card["id"]]
    assert listed["last_used"] == {"card": None, "bank": None}
    assert listed["can_add"] is True and listed["can_manage"] is False
    oid = _order(db, total=100.0)
    assert _post(client, MGR, "/api/orders/payment", order_id=oid, idempotency_key="p",
                 parts=[_card_row(100, card["id"])]).status_code == 200
    assert _post(client, MGR, "/api/pay_accounts").json()["last_used"]["card"] == card["id"]
    assert _post(client, MGR2, "/api/pay_accounts").json()["last_used"]["card"] is None, "выбор — свой у каждого"
    ctx = _post(client, MGR, "/api/orders/payment_context", order_id=_order(db, total=5.0)).json()
    assert ctx["pay_accounts"]["last_used"]["card"] == card["id"]


def test_manage_rights_boss_always_manager_only_without_boss(db, client):
    card = pay_account_id("card")
    r = _post(client, MGR, "/api/pay_accounts/update", account_id=card, holder="Другой")
    assert r.status_code == 403 and "руководитель" in r.json()["detail"]
    assert _post(client, MGR, "/api/pay_accounts/archive", account_id=card).status_code == 403
    assert _post(client, MGR, "/api/pay_accounts", include_archived=True).json()["can_manage"] is False
    r = _post(client, BOSS, "/api/pay_accounts/archive", account_id=card)
    assert r.status_code == 200 and r.json()["account"]["archived"] is True
    assert _post(client, MGR, "/api/pay_accounts").json()["accounts"] == []
    assert [a["id"] for a in _post(client, BOSS, "/api/pay_accounts", include_archived=True).json()["accounts"]] == [card]

    # Руководителя нет — менеджер правит и возвращает из архива сам, с пометкой.
    assert _run(db.deactivate_user(BOSS, 0))
    import services.roles as roles

    roles.invalidate_all_roles()
    listed = _post(client, MGR, "/api/pay_accounts", include_archived=True).json()
    assert listed["can_manage"] is True and "руководителя в системе нет" in listed["manage_hint"]
    r = _post(client, MGR, "/api/pay_accounts/archive", account_id=card, archived=False)
    assert r.status_code == 200 and r.json()["account"]["archived"] is False
    r = _post(client, MGR, "/api/pay_accounts/update", account_id=card, holder="Фаридун Масуджанов")
    assert r.status_code == 200 and r.json()["account"]["holder"] == "Фаридун Масуджанов"
    notes = [a["details"] for a in _rows(db, "SELECT details FROM audit_log WHERE action IN "
                                             "('pay_account_updated', 'pay_account_archived')")]
    assert any("руководителя в системе нет" in n for n in notes)


# ─── Рассрочка ───────────────────────────────────────────────────────────────


def test_machine_receipt_by_card_points_to_account_and_delete_cleans_link(db):
    pa = _pa()
    from services import machines

    made = _run(machines.create_machine(vin="PAYACC1", name="CAT", created_by=BOSS, status="in_stock"))
    deal = _run(machines.create_deal(made["machine_id"], kind="credit", price_cents=1_000_000, buyer_name="Азиз",
                                     buyer_passport="AA1234567", months=2, down_payment_cents=0,
                                     created_by=BOSS))
    assert deal.get("ok"), deal
    deal_id = deal["deal_id"]
    card = pay_account_id("card")
    assert "выберите карту" in _run(machines.add_receipt(deal_id, 100, user_id=MGR, method="card",
                                                 account_id=pay_account_id("bank")))["error"]
    assert _run(machines.add_receipt(deal_id, 100_000, user_id=MGR, method="card", account_id=card))["ok"]
    receipts = _run(machines.list_receipts(deal_id))
    assert receipts[0]["account_label"] == "на карту •••• 1234 (Фаридун М.)"
    audit = _rows(db, "SELECT details FROM audit_log WHERE action = 'machine_receipt_added'")
    assert "на карту •••• 1234 (Фаридун М.)" in audit[0]["details"]
    assert _run(pa.last_used(MGR))["card"] == card
    assert _run(machines.delete_receipt(receipts[0]["id"], user_id=BOSS))["ok"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM machine_receipt_accounts")[0]["n"] == 0
