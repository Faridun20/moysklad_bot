"""
«Как получены деньги» (services/order_payments.py) — SQLite, настоящая БД.

Что держит этот файл:
* разбивка: копейки, пересчёт USD/UZS по курсу, допуск округления, «оплата
  сразу» = ровно сумма, «в долг» — любая часть;
* отгрузка «оплаты сразу» без разбивки отказывает СЕРВЕРОМ (и в боте);
* долг: отгружен без денег, потом оплачен разбивкой;
* сдача наличных FIFO по строкам разбивки: несколько заказов, деление строки,
  валюты USD/UZS отдельно, «Заказы: #N» на карточке, подтверждение сдачи
  подтверждает платежи и закрывает заказ, отклонение возвращает «на руки»;
* корень прод-бага «Заказы: —»: автоплатёж одобрения заявлял всю сумму, и сдаче
  было не на что лечь — воспроизведение и починка;
* идемпотентность и двойной сабмит; «Долги»: подписи чисел во всех состояниях.

Postgres-гонки — tests/test_order_payments_postgres.py.
"""

from __future__ import annotations

import asyncio
import importlib
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import services.roles as roles

MGR, BOSS, MGR2 = 1, 2, 3


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(isolated_db):
    roles.invalidate_all_roles()
    isolated_db.set_role(MGR, "mgr", "Manager", "manager")
    isolated_db.set_role(BOSS, "boss", "Boss", "boss")
    isolated_db.set_role(MGR2, "mgr2", "Manager2", "manager")
    # Курс ЦБ: 12 700 сум за доллар (currency_rates хранит «1 UZS = X USD»).
    assert isolated_db.set_currency_rate("UZS", 1 / 12700, BOSS)[0]
    return isolated_db


def _order(db, *, total=12130.0, currency="USD", payment_type="paid", status="approved", uid=MGR,
           created_at=None):
    oid = db.create_order(uid, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
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


def _actor(uid=MGR, role="manager"):
    from services import order_payments

    return order_payments.Actor(user_id=uid, name=f"U{uid}", role=role)


def _record(oid, parts, uid=MGR, role="manager", key=None):
    from services import order_payments

    return _run(order_payments.record_payment_parts(oid, _actor(uid, role), parts, idem_key=key))


def _cash(amount, cur="USD"):
    return {"method": "cash", "currency": cur, "amount": str(amount)}


def _card(amount, cur="USD", rate=None):
    d = {"method": "card", "currency": cur, "amount": str(amount)}
    if rate:
        d["rate"] = rate
    return d


def _balance(oid):
    from services.debts import calc_order_balance

    return _run(calc_order_balance(oid))


# ─── Чистые функции: копейки, курс, допуск ───────────────────────────────────


def test_parse_parts_validates_every_row_with_its_number():
    from services.order_payments import PaymentError, parse_parts

    rows = parse_parts([_cash("5 000"), _card("7130,50")])
    assert [(r.method, r.currency, r.amount_cents) for r in rows] == [
        ("cash", "USD", 500_000), ("card", "USD", 713_050)]
    for bad, text in (
        ([], "хотя бы одну"),
        ([{"method": "crypto", "currency": "USD", "amount": 1}], "Строка 1: выберите способ"),
        ([_cash(1), {"method": "bank", "currency": "EUR", "amount": 1}], "Строка 2: валюта EUR"),
        ([_cash(0)], "больше нуля"),
        ([_cash("abc")], "больше нуля"),
        ([_card(1, "UZS", rate="-3")], "курс"),
        ([_cash(1)] * 11, "Не больше 10"),
    ):
        with pytest.raises(PaymentError) as e:
            parse_parts(bad)
        assert text in e.value.message


def test_conversion_rounds_half_up_to_cents_both_directions():
    from services.order_payments import convert_to_order

    q = Decimal("12700")
    assert convert_to_order(90_551_000_00, "UZS", "USD", "USD", q) == 713_000  # 90 551 000 сум = 7 130 $
    assert convert_to_order(6_350, "UZS", "USD", "USD", q) == 1                # 63.50 сум = 0.005 $ → 0.01
    assert convert_to_order(6_349, "UZS", "USD", "USD", q) == 0
    assert convert_to_order(100_000, "USD", "UZS", "USD", q) == 1_270_000_000  # 1 000 $ = 12.7 млн сум
    assert convert_to_order(123, "USD", "USD", "USD", None) == 123


def test_compute_uses_cbu_by_default_and_marks_manual_rate():
    from services.order_payments import compute_parts, parse_parts

    cbu = {"UZS": Decimal("12700.00")}
    same, by_cbu, manual = compute_parts(
        parse_parts([_cash(5000), _card(1_270_000, "UZS"), _card(1_265_000, "UZS", rate="12650")]),
        "USD", "USD", cbu,
    )
    assert (same.rate_source, same.order_amount_cents) == ("same", 500_000)
    assert (by_cbu.rate_source, by_cbu.rate, by_cbu.order_amount_cents) == ("cbu", Decimal("12700.00"), 10_000)
    assert (manual.rate_source, manual.rate, manual.cbu_rate, manual.order_amount_cents) == (
        "manual", Decimal("12650.0000"), Decimal("12700.00"), 10_000)
    # Сумовой заказ, оплаченный долларами: курс — у валюты заказа.
    (usd_for_uzs,) = compute_parts(parse_parts([_cash(1000)]), "UZS", "USD", cbu)
    assert (usd_for_uzs.rate, usd_for_uzs.order_rate, usd_for_uzs.order_amount_cents) == (
        None, Decimal("12700.00"), 1_270_000_000)


def test_settle_exact_for_paid_absorbs_rounding_only_when_converted():
    from services.order_payments import PaymentError, compute_parts, parse_parts, settle_parts

    cbu = {"UZS": Decimal("12700")}
    due = 1_213_000
    # 5 000 $ + 90 550 000 сум = 12 129.92 $ — копейки пересчёта берёт строка в сумах.
    calcs = compute_parts(parse_parts([_cash(5000), _card(90_550_000, "UZS")]), "USD", "USD", cbu)
    settled = settle_parts(calcs, due, exact=True, order_currency="USD", base="USD")
    assert [c.order_amount_cents for c in settled] == [500_000, 713_000]
    assert sum(c.order_amount_cents for c in settled) == due

    same = compute_parts(parse_parts([_cash(5000), _card("7129.99")]), "USD", "USD", cbu)
    with pytest.raises(PaymentError) as e:
        settle_parts(same, due, exact=True, order_currency="USD", base="USD")
    assert e.value.code == "short" and "Не хватает 0.01 USD" in e.value.message

    with pytest.raises(PaymentError) as e:
        settle_parts(compute_parts(parse_parts([_cash(12131)]), "USD", "USD", cbu), due,
                     exact=True, order_currency="USD", base="USD")
    assert e.value.code == "over"

    # «В долг»: часть законна и не подгоняется к остатку.
    part = settle_parts(compute_parts(parse_parts([_cash(100)]), "USD", "USD", cbu), due,
                        exact=False, order_currency="USD", base="USD")
    assert [c.order_amount_cents for c in part] == [10_000]


# ─── «Оплата сразу»: разбивка до отгрузки ────────────────────────────────────


def test_paid_order_is_not_shipped_until_breakdown_covers_total(db):
    oid = _order(db)
    refused = _run(db.mark_order_shipped(oid, BOSS, "Boss"))
    assert refused["ok"] is False and refused["code"] == "payment_required"
    assert "12 130 USD" in refused["error"]

    rec = _record(oid, [_cash(5000), _card(7130)])
    assert rec["total_cents"] == 1_213_000 and len(rec["parts"]) == 2
    assert _run(db.mark_order_shipped(oid, BOSS, "Boss"))["ok"]
    assert _run(db.get_order(oid))["status"] == "shipped"

    pays = _run(db.get_payments_for_order(oid))
    assert sorted((p["amount_cents"], p["status"]) for p in pays) == [(500_000, "pending"), (713_000, "pending")]
    audit = _rows(db, "SELECT details FROM audit_log WHERE action = 'order_payment_recorded'")
    assert "наличные 5 000 USD" in audit[0]["details"] and "на карту 7 130 USD" in audit[0]["details"]


def test_paid_order_partial_breakdown_is_refused_with_clear_text(db):
    from services.order_payments import PaymentError

    oid = _order(db)
    with pytest.raises(PaymentError) as e:
        _record(oid, [_cash(5000)])
    assert e.value.code == "short"
    assert "Не хватает 7 130 USD" in e.value.message and "в долг" in e.value.message
    assert _run(db.get_payments_for_order(oid)) == []


def test_breakdown_only_by_owner_or_management(db):
    from services.order_payments import PaymentError

    oid = _order(db)
    with pytest.raises(PaymentError) as e:
        _record(oid, [_cash(12130)], uid=MGR2)
    assert e.value.status == 403
    assert _record(oid, [_cash(12130)], uid=BOSS, role="boss")["ok"]


def test_uzs_order_paid_in_usd_and_uzs_order_amounts_are_in_order_currency(db):
    oid = _order(db, total=25_400_000.0, currency="UZS")  # 2 000 $
    rec = _record(oid, [_cash(1000), _card(12_700_000, "UZS")])
    assert rec["total_cents"] == 2_540_000_000
    part = _rows(db, "SELECT currency, amount_cents, order_amount_cents, order_rate, rate_source "
                     "FROM payment_parts WHERE method = 'cash'")[0]
    assert part == {"currency": "USD", "amount_cents": 100_000, "order_amount_cents": 1_270_000_000,
                    "order_rate": "12700", "rate_source": "cbu"}
    assert {p["currency"] for p in _run(db.get_payments_for_order(oid))} == {"UZS"}


def test_legacy_auto_payment_is_superseded_by_breakdown(db):
    """Заказ одобрен ДО выката: висит автоплатёж без способа на всю сумму."""
    oid = _order(db, status="shipped")
    auto = db.add_payment(MGR, "", "Manager", 12130.0, "USD",
                          f"Оплата по заказу #{oid} (отгрузка одобрена)", order_id=oid)
    rec = _record(oid, [_cash(5000), _card(7130)])
    assert rec["superseded"] == [auto]
    assert _run(db.get_payment(auto))["status"] == "rejected"
    assert _balance(oid).pending_cents == 1_213_000


def test_approval_no_longer_creates_auto_payment_and_tells_manager(db, monkeypatch):
    from services.order_workflow import approve_shipment_request, submit_order

    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар без карточки", "", 1, "шт", 100.0)
    sub = _run(submit_order(oid, MGR, "Manager", payment_type="paid"))

    class Bot:
        sent: list = []

        async def send_message(self, chat_id, text, **kw):
            Bot.sent.append((chat_id, text))

    res = _run(approve_shipment_request(sub["req_id"], BOSS, "Boss", Bot(), pdf_delivery="inline"))
    assert res["ok"], res
    assert _run(db.get_payments_for_order(oid)) == []
    assert "перед отгрузкой внесите" in res["demand_line"]


def test_cancel_rejects_pending_breakdown_but_not_accepted_money(db):
    oid = _order(db)
    rec = _record(oid, [_card(12130)])
    res = _run(db.cancel_order(oid, BOSS, "Boss", "клиент передумал"))
    assert res["ok"], res
    assert _run(db.get_payment(rec["payment_id"]))["status"] == "rejected"

    oid2 = _order(db)
    rec2 = _record(oid2, [_card(12130)])
    assert _run(db.confirm_payment(rec2["payment_id"], BOSS, "Boss"))
    res2 = _run(db.cancel_order(oid2, BOSS, "Boss", "клиент передумал"))
    assert res2["ok"] is False and "подтверждена оплата 12 130 USD" in res2["error"]


# ─── Долг: отгружен без денег, оплачен разбивкой ─────────────────────────────


def test_credit_order_ships_without_money_then_is_paid_by_breakdown(db):
    oid = _order(db, total=1000.0, payment_type="credit")
    assert _run(db.mark_order_shipped(oid, BOSS, "Boss"))["ok"]

    first = _record(oid, [_card(300)])
    assert first["total_cents"] == 30_000
    second = _record(oid, [_cash(200), _card(3_810_000, "UZS")])  # 200 + 300 $
    assert second["total_cents"] == 50_000
    from services.order_payments import PaymentError

    with pytest.raises(PaymentError) as e:
        _record(oid, [_cash(201)])
    assert e.value.code == "over"

    for pid in [first["payment_id"], *[p["payment_id"] for p in second["parts"] if p["method"] == "card"]]:
        assert _run(db.confirm_payment(pid, BOSS, "Boss"))
    bal = _balance(oid)
    assert (bal.confirmed_cents, bal.pending_cents, bal.remaining_cents) == (60_000, 20_000, 40_000)


def test_cash_part_is_not_confirmed_by_the_payment_button(db):
    oid = _order(db, total=100.0, payment_type="credit", status="shipped")
    rec = _record(oid, [_cash(100)])
    assert _run(db.confirm_payment(rec["payment_id"], BOSS, "Boss")) is False
    assert _run(db.confirm_all_pending_payments_for_order(oid, BOSS, "Boss")) == 0
    assert _run(db.get_payment(rec["payment_id"]))["status"] == "pending"


# ─── Сдача наличных по строкам разбивки ──────────────────────────────────────


def test_prod_bug_reproduced_old_auto_payment_left_nothing_for_the_deposit(db):
    """Прод: заказ #27 «оплата сразу» 12 130, автоплатёж pending на всю сумму,
    две сдачи по 2 000 — cash_deposit_orders пуст, «Заказы: —»."""
    oid = _order(db, status="shipped")
    db.add_payment(MGR, "", "Manager", 12130.0, "USD", f"Оплата по заказу #{oid} (отгрузка одобрена)",
                   order_id=oid)
    before = _run(db.create_cash_deposit(MGR, 2000.0))
    assert before["ok"] and before["allocations"] == [] and before["parts"] == []
    assert before["unallocated_cents"] == 200_000

    # Починка: менеджер вносит, как получил, — следующая сдача ложится на заказ.
    _record(oid, [_cash(12130)])
    after = _run(db.create_cash_deposit(MGR, 2000.0))
    assert [(p["order_id"], p["amount_cents"]) for p in after["parts"]] == [(oid, 200_000)]
    from services import order_payments

    view = _run(order_payments.deposit_orders_view([after["deposit_id"]]))[after["deposit_id"]]
    assert [(v["order_id"], v["amount_cents"], v["currency"]) for v in view] == [(oid, 200_000, "USD")]


def test_handover_fifo_across_orders_splits_a_part_and_keeps_currencies_apart(db):
    from services import order_payments

    old = _order(db, total=3000.0, status="shipped", created_at="2026-09-01 10:00:00")
    new = _order(db, total=4000.0, status="shipped", created_at="2026-09-02 10:00:00")
    uzs = _order(db, total=12_700_000.0, currency="UZS", status="shipped")
    _record(old, [_cash(3000)])
    _record(new, [_cash(2000), _card(2000)])
    _record(uzs, [_cash(12_700_000, "UZS")])
    _record(_order(db, total=50.0, uid=MGR2, status="shipped"), [_cash(50)], uid=MGR2)  # чужие наличные

    on_hand = order_payments.cash_on_hand_summary(_run(order_payments.cash_on_hand(MGR)))
    assert on_hand["by_currency"] == [{"currency": "USD", "amount_cents": 500_000},
                                      {"currency": "UZS", "amount_cents": 1_270_000_000}]

    dep = _run(db.create_cash_deposit(MGR, 4000.0))  # 3 000 старого + 1 000 из 2 000 нового
    assert [(p["order_id"], p["amount_cents"]) for p in dep["parts"]] == [(old, 300_000), (new, 100_000)]
    assert dep["unallocated_cents"] == 0
    parts_new = _rows(db, "SELECT amount_cents, order_amount_cents, split_from FROM payment_parts "
                          "WHERE order_id = ? AND method = 'cash' ORDER BY id", (new,))
    assert [(r["amount_cents"], r["split_from"] is not None) for r in parts_new] == [(100_000, False), (100_000, True)]
    assert sum(p["amount_cents"] for p in _run(db.get_payments_for_order(new))) == 400_000, "делёж не меняет сумму"

    # Сумы сдаются отдельной сдачей в UZS и в долларовую не попадают.
    dep_uzs = _run(db.create_cash_deposit(MGR, 12_700_000.0, currency="UZS"))
    assert [(p["order_id"], p["amount_cents"]) for p in dep_uzs["parts"]] == [(uzs, 1_270_000_000)]
    assert _run(order_payments.deposit_currency([dep["deposit_id"], dep_uzs["deposit_id"]])) == {
        dep["deposit_id"]: "USD", dep_uzs["deposit_id"]: "UZS"}

    # Ручной выбор: только новый заказ.
    manual = _run(db.create_cash_deposit(MGR, 1000.0, order_ids=[new]))
    assert [(p["order_id"], p["amount_cents"]) for p in manual["parts"]] == [(new, 100_000)]
    assert _run(order_payments.cash_on_hand(MGR)) == []


def test_confirming_handover_confirms_cash_payments_and_closes_order(db):
    oid = _order(db, status="shipped")
    rec = _record(oid, [_cash(5000), _card(7130)])
    dep = _run(db.create_cash_deposit(MGR, 5000.0))
    assert _balance(oid).remaining_cents == 1_213_000

    res = _run(db.confirm_cash_deposit(dep["deposit_id"], BOSS, "Boss"))
    assert res["ok"] and res["closed_orders"] == [] and res["self_confirmed"] is False
    bal = _balance(oid)
    assert (bal.confirmed_cents, bal.pending_cents, bal.remaining_cents) == (500_000, 713_000, 713_000)

    card = next(p["payment_id"] for p in rec["parts"] if p["method"] == "card")
    assert _run(db.confirm_payment(card, BOSS, "Boss"))
    order = _run(db.get_order(oid))
    assert order["paid_confirmed_at"] and _balance(oid).remaining_cents == 0
    assert _run(db.get_open_debts()) == []


def test_handover_confirmed_last_closes_order(db):
    oid = _order(db, status="shipped")
    rec = _record(oid, [_cash(5000), _card(7130)])
    assert _run(db.confirm_payment(rec["parts"][1]["payment_id"], BOSS, "Boss"))
    dep = _run(db.create_cash_deposit(MGR, 5000.0))
    res = _run(db.confirm_cash_deposit(dep["deposit_id"], MGR, "Manager"))  # сам — бухгалтера нет
    assert res["closed_orders"] == [oid] and res["self_confirmed"] is True
    order = _run(db.get_order(oid))
    # Закрытие — как у подтверждения платежа: отметка оплаты, статус не прыгает.
    assert order["status"] == "shipped" and order["paid_confirmed_at"]
    audit = _rows(db, "SELECT details FROM audit_log WHERE action = 'cash_deposit_confirmed'")[-1]["details"]
    assert f"#{oid}" in audit and "подтверждено самим сдающим" in audit


def test_rejected_handover_puts_cash_back_on_hand(db):
    from services import order_payments

    oid = _order(db, status="shipped")
    rec = _record(oid, [_cash(12130)])
    dep = _run(db.create_cash_deposit(MGR, 12130.0))
    assert _run(order_payments.cash_on_hand(MGR)) == []
    # Наличные в сдаче не отклоняются отдельной кнопкой — только вместе со сдачей.
    assert _run(db.reject_payment(rec["payment_id"], BOSS, "Boss")) is False
    assert _run(db.reject_cash_deposit(dep["deposit_id"], BOSS, "Boss", "не сошлось"))["ok"]
    assert len(_run(order_payments.cash_on_hand(MGR))) == 1
    again = _run(db.create_cash_deposit(MGR, 12130.0))
    assert [p["order_id"] for p in again["parts"]] == [oid]


def test_money_totals_count_cash_once_and_deposits_in_their_currency(db):
    oid = _order(db, total=200.0, status="shipped")
    rec = _record(oid, [_cash(100), _card(100)])
    assert _run(db.confirm_payment(rec["parts"][1]["payment_id"], BOSS, "Boss"))
    dep = _run(db.create_cash_deposit(MGR, 100.0))
    assert _run(db.confirm_cash_deposit(dep["deposit_id"], BOSS, "Boss"))["ok"]
    uzs = _order(db, total=1_270_000.0, currency="UZS", status="shipped", payment_type="credit")
    _record(uzs, [_cash(1_270_000, "UZS")])
    dep_uzs = _run(db.create_cash_deposit(MGR, 1_270_000.0, currency="UZS"))
    assert _run(db.confirm_cash_deposit(dep_uzs["deposit_id"], BOSS, "Boss"))["ok"]

    totals = _run(db.get_money_totals())
    assert totals["payments"] == [{"currency": "USD", "total_cents": 10_000, "count": 1}]  # только карта
    assert sorted((d["currency"], d["total_cents"]) for d in totals["deposits"]["by_currency"]) == [
        ("USD", 10_000), ("UZS", 127_000_000)]
    assert totals["deposits"]["total_cents"] == 10_000


# ─── HTTP: ручки, идемпотентность, «Долги» ───────────────────────────────────


@pytest.fixture
def client(db, monkeypatch):
    import services.notifier as notifier
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    pushes: list = []

    async def _recips():
        return [BOSS]

    async def _send(uid, text, **kw):
        pushes.append((uid, text, kw))

    class _Bot:
        async def send_message(self, *a, **k):
            return None

    async def _bot():
        return _Bot()

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)
    monkeypatch.setattr(server, "get_notify_bot", _bot)
    c = TestClient(server.app)
    c.pushes = pushes
    return c


def _post(client, uid, path, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_api_double_submit_with_same_key_records_once(db, client):
    oid = _order(db)
    body = {"order_id": oid, "parts": [_cash(5000), _card(7130)], "idempotency_key": "form-1"}
    r1 = _post(client, MGR, "/api/orders/payment", **body)
    r2 = _post(client, MGR, "/api/orders/payment", **body)
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    assert r1.json()["payments"] == r2.json()["payments"]
    assert len(_rows(db, "SELECT id FROM payment_parts")) == 2
    # Карта ушла подтверждающим с кнопками, наличные — нет.
    cards = [p for p in client.pushes if "pay_ok" in str(p[2])]
    assert len(cards) == 1 and "на карту 7 130 USD" in cards[0][1]


def test_api_refusal_releases_key_and_returns_code(db, client):
    oid = _order(db)
    r = _post(client, MGR, "/api/orders/payment", order_id=oid, parts=[_cash(1)], idempotency_key="k")
    assert r.status_code == 400 and r.json()["code"] == "short"
    r = _post(client, MGR, "/api/orders/payment", order_id=oid, parts=[_cash(12130)], idempotency_key="k")
    assert r.status_code == 200, r.text


def test_api_ship_answers_payment_required_code(db, client):
    oid = _order(db)
    r = _post(client, BOSS, "/api/orders/ship", order_id=oid)
    assert r.status_code == 409 and r.json()["code"] == "payment_required"
    ctx = _post(client, MGR, "/api/orders/payment_context", order_id=oid).json()
    assert (ctx["due_cents"], ctx["exact"], ctx["currency"], ctx["cbu"]["UZS"]) == (1_213_000, True, "USD", "12700")
    orders = {o["id"]: o for o in _post(client, MGR, "/api/orders").json()["orders"]}
    assert orders[oid]["needs_payment"] is True and orders[oid]["payment_gap"] == 12130.0


def test_api_deposit_double_submit_and_card_shows_orders(db, client):
    oid = _order(db, status="shipped")
    _record(oid, [_cash(5000), _card(7130)])
    on_hand = _post(client, MGR, "/api/deposits/on_hand").json()
    assert on_hand["orders"][0]["order_id"] == oid and on_hand["orders"][0]["amount"] == 5000.0
    body = {"amount": 5000, "currency": "USD", "idempotency_key": "dep-1"}
    r1 = _post(client, MGR, "/api/deposits/create", **body)
    r2 = _post(client, MGR, "/api/deposits/create", **body)
    assert r1.json()["deposit_id"] == r2.json()["deposit_id"]
    assert len(_rows(db, "SELECT id FROM cash_deposits")) == 1
    pending = _post(client, MGR, "/api/deposits/pending").json()["deposits"]
    assert [(o["order_id"], o["amount_allocated"], o["currency"]) for o in pending[0]["orders"]] == [(oid, 5000.0, "USD")]
    assert pending[0]["is_own"] is True and pending[0]["currency"] == "USD"


def test_api_debts_wording_numbers_in_every_state(db, client):
    # 1) Вся оплата ждёт: после подтверждения долг 0 (прод-случай «Ждёт 12к / Осталось 0»).
    full = _order(db, status="shipped")
    _record(full, [_cash(5000), _card(7130)])
    # 2) Частично: 3 000 из 10 000 ждут, после подтверждения — 7 000.
    part = _order(db, total=10000.0, payment_type="credit", status="shipped")
    _record(part, [_card(3000)])
    # 3) Сумовой заказ, оплаченный долларами.
    uzs = _order(db, total=25_400_000.0, currency="UZS", payment_type="credit", status="shipped")
    _record(uzs, [_cash(1000)])
    # 4) Возврат после оплаты: ждущее больше долга.
    ret = _order(db, total=1000.0, payment_type="credit", status="shipped")
    _record(ret, [_card(1000)])
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO returns (order_id, return_type, reason, total_amount_cents, refund_method, "
                         "created_by, status, created_at) VALUES (?, 'partial', 'брак', 40000, 'debt_reduction', ?, "
                         "'confirmed', ?)"), (ret, BOSS, db.now_str()))
        conn.commit()

    body = _post(client, MGR, "/api/debts").json()
    d = {x["id"]: x for x in body["debts"]}
    assert (d[full]["pending"], d[full]["remaining_after_pending"], d[full]["pending_cash"],
            d[full]["pending_confirmable"]) == (12130.0, 0.0, 5000.0, 7130.0)
    assert d[full]["state"] == "awaiting_confirmation"
    assert [p["state"] for p in d[full]["parts"]] == ["on_hand", "awaiting_bank"]
    assert (d[part]["pending"], d[part]["remaining_after_pending"], d[part]["claimable"]) == (3000.0, 7000.0, 7000.0)
    assert (d[uzs]["pending"], d[uzs]["remaining_after_pending"], d[uzs]["currency"]) == (12_700_000.0, 12_700_000.0, "UZS")
    assert (d[ret]["remaining"], d[ret]["pending"], d[ret]["overpending"], d[ret]["remaining_after_pending"]) == (
        600.0, 1000.0, 400.0, 0.0)
    # Руководитель есть — менеджеру кнопки нет, экран называет, кто подтвердит.
    assert body["can_confirm"] is False and body["confirm_hint"] == "подтвердит Boss"
    # Руководителя нет (как на проде сейчас): менеджер подтверждает сам, и это сказано.
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE user_roles SET role = 'guest' WHERE user_id = ?"), (BOSS,))
        conn.commit()
    roles.invalidate_all_roles()
    alone = _post(client, MGR, "/api/debts").json()
    assert alone["can_confirm"] is True
    assert "руководителя и бухгалтера в системе нет" in alone["confirm_hint"]


def test_api_mark_paid_requires_method(db, client):
    oid = _order(db, payment_type="credit", status="shipped")
    r = _post(client, MGR, "/api/orders/mark_paid", order_id=oid, amount=10)
    assert r.status_code == 400
    r = _post(client, MGR, "/api/orders/mark_paid", order_id=oid, parts=[_cash(10)])
    assert r.status_code == 200 and r.json()["payment_id"]


def test_bot_ship_command_follows_the_same_rule(db, monkeypatch):
    """Бот и WebApp не расходятся: /ship «оплаты сразу» без разбивки — отказ со ссылкой в WebApp."""
    from handlers import order_ship

    oid = _order(db)
    answers: list = []

    class Msg:
        text = f"/ship {oid}"

        class from_user:
            id = BOSS
            full_name = "Boss"

        async def answer(self, text, **kw):
            answers.append(text)

    importlib.reload(roles)
    monkeypatch.setattr(order_ship, "can_confirm_shipment", lambda uid: True)
    _run(order_ship.cmd_ship(Msg(), bot=None))
    assert "сначала введите, как клиент заплатил" in answers[0]
    assert _run(db.get_order(oid))["status"] == "approved"


def test_bot_pay_ok_refuses_cash(db, monkeypatch):
    from handlers import payments as hp

    oid = _order(db, payment_type="credit", status="shipped")
    rec = _record(oid, [_cash(100)])
    answers: list = []

    class Call:
        data = f"pay_ok:{rec['payment_id']}"

        class from_user:
            id = BOSS
            full_name = "Boss"

        async def answer(self, text=None, **kw):
            answers.append(text)

    monkeypatch.setattr(hp, "is_admin", lambda uid: True)
    _run(hp.confirm_pay(Call(), bot=None))
    assert "сдачей в кассу" in answers[0]
    assert _run(db.get_payment(rec["payment_id"]))["status"] == "pending"


# ─── Баги из сценариев (agent/scenario-tests) ────────────────────────────────


def test_cancel_voids_legacy_pending_payment_and_confirm_refuses_cancelled_order(db, client):
    """Отмена одобренной «оплаты сразу» снимала только… ничего: автоплатёж
    оставался pending, и «Подтвердить» по отменённому заказу засчитывал деньги."""
    oid = _order(db)
    legacy = db.add_payment(MGR, "", "Manager", 12130.0, "USD",
                            f"Оплата по заказу #{oid} (отгрузка одобрена)", order_id=oid)
    res = _run(db.cancel_order(oid, BOSS, "Boss", "передумал"))
    assert res["ok"], res
    assert _run(db.get_payment(legacy))["status"] == "rejected"
    r = _post(client, BOSS, "/api/orders/confirm_payment", order_id=oid)
    assert r.status_code == 200 and r.json()["confirmed_count"] == 0
    assert _rows(db, "SELECT 1 FROM payments WHERE order_id = ? AND status = 'confirmed'", (oid,)) == []

    # Рубеж в самом подтверждении: платёж, оставшийся pending у отменённого заказа.
    stale = db.add_payment(MGR, "", "Manager", 1.0, "USD", "старый", order_id=oid)
    assert _run(db.confirm_payment(stale, BOSS, "Boss")) is False


def test_cancel_refuses_when_money_already_confirmed(db):
    oid = _order(db, payment_type="credit")
    rec = _record(oid, [_card(100)])
    assert _run(db.confirm_payment(rec["payment_id"], BOSS, "Boss"))
    res = _run(db.cancel_order(oid, BOSS, "Boss", "передумал"))
    assert res["ok"] is False and res["code"] == "money_received"
    assert "подтверждена оплата 100 USD" in res["error"] and "возврат" in res["error"]
    assert _run(db.get_order(oid))["status"] == "approved"


def test_ship_refuses_when_write_off_failed_and_retries_when_stock_arrives(db, client):
    from services import container_receipt, order_shipment, warehouse

    pid = _run(container_receipt.create_product("Кабель"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    assert _run(warehouse.create_invoice(invoice_type="incoming", warehouse_id=wid,
                                         items=[{"product_id": pid, "quantity": 5, "price_cents": None}]))["ok"]
    oid = db.create_order(MGR, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Кабель", "", 7, "м", 10.0, product_id=pid)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET payment_type='credit', due_date='2030-01-15', currency='USD' WHERE id=?"), (oid,))
        conn.commit()
    db.update_order_status(oid, "approved")
    order = _run(db.get_order(oid))
    failed = _run(order_shipment.ship_order(order, _run(db.get_order_items(oid)), user_id=BOSS))
    assert failed["ok"] is False and _run(order_shipment.get_shipment(oid))["failed_at"]

    r = _post(client, BOSS, "/api/orders/ship", order_id=oid)
    assert r.status_code == 409 and "не списан" in r.json()["detail"]
    assert _run(db.get_order(oid))["status"] == "approved"

    # Довезли товар — повторное «Отгрузить» списывает и отгружает.
    assert _run(warehouse.create_invoice(invoice_type="incoming", warehouse_id=wid,
                                         items=[{"product_id": pid, "quantity": 5, "price_cents": None}]))["ok"]
    r = _post(client, BOSS, "/api/orders/ship", order_id=oid)
    assert r.status_code == 200, r.text
    assert _run(order_shipment.get_shipment(oid))["invoice_id"]
    assert _run(db.get_order(oid))["status"] == "shipped"


def test_debt_reduction_return_on_paid_order_is_refused_cash_is_fine(db):
    oid = _order(db, total=500.0, payment_type="credit", status="shipped")
    item = _run(db.get_order_items(oid))[0]["id"]
    rec = _record(oid, [_card(500)])
    assert _run(db.confirm_payment(rec["payment_id"], BOSS, "Boss"))

    res = _run(db.create_return(oid, "partial", "брак", [(item, 0.4, 0)], "debt_reduction", MGR))
    assert res["ok"] is False and res["code"] == "no_debt_to_reduce"
    assert "Долга по заказу нет" in res["error"] and "Наличными" in res["error"]
    assert _run(db.create_return(oid, "partial", "брак", [(item, 0.4, 0)], "cash", MGR))["ok"]


def test_debt_reduction_return_within_debt_still_works_and_is_rechecked_on_confirm(db):
    oid = _order(db, total=500.0, payment_type="credit", status="shipped")
    item = _run(db.get_order_items(oid))[0]["id"]
    res = _run(db.create_return(oid, "partial", "брак", [(item, 0.4, 0)], "debt_reduction", MGR))
    assert res["ok"], res  # 200 из 500 долга
    # Клиент доплатил всё до подтверждения возврата — подтверждать «в счёт долга» нечего.
    rec = _record(oid, [_card(500)])
    assert _run(db.confirm_payment(rec["payment_id"], BOSS, "Boss"))
    assert _run(db.mark_return_goods_received(res["return_id"], BOSS))["ok"]
    conf = _run(db.confirm_return(res["return_id"], BOSS, "Boss"))
    assert conf["ok"] is False and "Долга по заказу нет" in conf["error"]
