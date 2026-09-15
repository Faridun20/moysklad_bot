"""
Аудит денег (сентябрь 2026): свой курс и кто подтверждает деньги.

* свой курс в разбивке оплаты и в документах бухгалтерии — не дальше
  `manual_rate_max_deviation_pct` от ЦБ; без курса ЦБ — только руководству
  («12 130 сум по курсу 1» закрывали долларовый заказ на 12 130 USD);
* подтверждение сдачи/платежа — СЕРВИСНЫЙ рубеж (`order_payments.confirm_rights`):
  менеджер (бухгалтер лишь совмещением ролей) подтверждает свои и чужие деньги
  ТОЛЬКО пока активных admin/boss/bookkeeper нет; то же для HTTP и кнопок бота;
* пометка «руководителя/бухгалтера в системе нет» — из фактического наличия
  подтверждающих, а не из роли.
"""

from __future__ import annotations

import asyncio

import pytest

import services.roles as roles
from tests import test_order_payments as _op
from tests.test_order_payments import (
    BOSS,
    MGR,
    MGR2,
    _card,
    _cash,
    _order,
    _post,
    _record,
    _rows,
    _run,
)

# Фикстуры разбивки оплаты — те же, что в test_order_payments.
db = _op.db
client = _op.client


def _demote_boss(db):
    """Руководителя в системе нет — как на проде сейчас."""
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE user_roles SET role = 'guest' WHERE user_id = ?"), (BOSS,))
        conn.commit()
    roles.invalidate_all_roles()


# ─── 1a. Свой курс ───────────────────────────────────────────────────────────


def test_manual_rate_far_from_cbu_is_refused_for_everyone(db):
    from services.order_payments import PaymentError, compute_parts, parse_parts

    cbu = {"UZS": __import__("decimal").Decimal("12700")}
    for role in ("manager", "boss", None):
        with pytest.raises(PaymentError) as e:
            compute_parts(parse_parts([_card(12130, "UZS", rate="1")]), "USD", "USD", cbu, role=role)
        assert e.value.code == "manual_rate"
        assert "отличается от курса ЦБ 12700" in e.value.message and "10%" in e.value.message
    # В пределах допуска — законно (курс в обменнике чуть другой).
    (ok,) = compute_parts(parse_parts([_card(1_397_000, "UZS", rate="13970")]), "USD", "USD", cbu,
                          role="manager")
    assert ok.rate_source == "manual" and ok.order_amount_cents == 10_000
    with pytest.raises(PaymentError):
        compute_parts(parse_parts([_card(1_400_000, "UZS", rate="14000")]), "USD", "USD", cbu,
                      role="manager")
    # Допуск — настройка.
    db.set_setting("manual_rate_max_deviation_pct", 20, BOSS)
    (wide,) = compute_parts(parse_parts([_card(1_400_000, "UZS", rate="14000")]), "USD", "USD", cbu,
                            role="manager")
    assert wide.rate_source == "manual"


def test_manual_rate_without_cbu_only_for_boss(db):
    from services.order_payments import PaymentError, compute_parts, parse_parts

    rows = parse_parts([_card(1_300_000, "UZS", rate="13000")])
    with pytest.raises(PaymentError) as e:
        compute_parts(rows, "USD", "USD", {"UZS": None}, role="manager")
    assert "только руководитель" in e.value.message
    for role in ("admin", "boss"):
        (calc,) = compute_parts(rows, "USD", "USD", {"UZS": None}, role=role)
        assert calc.rate_source == "manual" and calc.order_amount_cents == 10_000


def test_api_payment_with_pocket_change_rate_is_refused(db, client):
    oid = _order(db, total=12130.0, status="approved")
    r = _post(client, MGR, "/api/orders/payment", order_id=oid,
              parts=[{"method": "cash", "currency": "UZS", "amount": "12130", "rate": "1"}])
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "manual_rate" and "курса ЦБ" in r.json()["detail"]
    assert _rows(db, "SELECT id FROM payment_parts") == []
    # Ship всё ещё требует оплату.
    assert _post(client, MGR, "/api/orders/ship", order_id=oid).status_code == 409


def test_accounting_resolve_rates_applies_the_same_guard(db, monkeypatch):
    from decimal import Decimal

    from services import accounting

    async def _cbu(currencies, day=None):
        return {c: Decimal("12700") for c in currencies}

    monkeypatch.setattr(accounting, "cbu_quotes", _cbu)
    day = accounting.today_str()
    with pytest.raises(accounting.AccountingError) as e:
        _run(accounting.resolve_rates({"UZS"}, {"UZS": "1"}, day, "manager"))
    assert "отличается от курса ЦБ" in e.value.args[0]
    with pytest.raises(accounting.AccountingError):
        _run(accounting.resolve_rates({"UZS"}, {"UZS": "1"}, day, "boss"))
    ok = _run(accounting.resolve_rates({"UZS"}, {"UZS": "12800"}, day, "manager"))
    assert ok["UZS"].source == "manual"

    async def _no_cbu(currencies, day=None):
        return {c: None for c in currencies}

    monkeypatch.setattr(accounting, "cbu_quotes", _no_cbu)
    with pytest.raises(accounting.AccountingError) as e:
        _run(accounting.resolve_rates({"UZS"}, {"UZS": "12800"}, day, "manager"))
    assert "только руководитель" in e.value.args[0]
    assert _run(accounting.resolve_rates({"UZS"}, {"UZS": "12800"}, day, "boss"))["UZS"].source == "manual"


# ─── 1b. Кто подтверждает ────────────────────────────────────────────────────


def test_manager_cannot_self_confirm_deposit_while_boss_active(db, client):
    oid = _order(db, total=50.0, payment_type="credit", status="shipped")
    _record(oid, [_cash(50)])
    dep = _post(client, MGR, "/api/deposits/create", amount=50, currency="USD").json()
    r = _post(client, MGR, "/api/deposits/confirm", deposit_id=dep["deposit_id"])
    assert r.status_code == 403, r.text
    assert "Boss" in r.json()["detail"]
    # Другой менеджер — тоже нет.
    r2 = _post(client, MGR2, "/api/deposits/confirm", deposit_id=dep["deposit_id"])
    assert r2.status_code == 403
    assert _run(db.get_cash_deposit(dep["deposit_id"]))["status"] == "pending"
    # Сервисный слой отказывает сам, без HTTP.
    res = _run(db.confirm_cash_deposit(dep["deposit_id"], MGR, "Manager"))
    assert res["ok"] is False and res["code"] == "confirm_forbidden" and res["status"] == 403
    # Руководитель подтверждает; пометки «сам» нет.
    ok = _post(client, BOSS, "/api/deposits/confirm", deposit_id=dep["deposit_id"])
    assert ok.status_code == 200 and ok.json()["self_confirmed"] is False and not ok.json()["self_note"]


def test_no_boss_manager_confirms_and_note_says_so(db, client):
    _demote_boss(db)
    oid = _order(db, total=50.0, payment_type="credit", status="shipped")
    _record(oid, [_cash(50)])
    dep = _post(client, MGR, "/api/deposits/create", amount=50, currency="USD").json()
    r = _post(client, MGR, "/api/deposits/confirm", deposit_id=dep["deposit_id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["self_confirmed"] is True and body["approval_mode"] == "no_boss"
    assert "руководителя/бухгалтера в системе нет" in body["self_note"]
    audit = _rows(db, "SELECT details FROM audit_log WHERE action = 'cash_deposit_confirmed'")[-1]["details"]
    assert "подтверждено самим сдающим — руководителя/бухгалтера в системе нет" in audit

    # Чужая сдача без руководителя — можно, но пометка не «сам».
    oid2 = _order(db, total=30.0, payment_type="credit", status="shipped", uid=MGR2)
    _record(oid2, [_cash(30)], uid=MGR2)
    dep2 = _post(client, MGR2, "/api/deposits/create", amount=30, currency="USD").json()
    r2 = _post(client, MGR, "/api/deposits/confirm", deposit_id=dep2["deposit_id"])
    assert r2.status_code == 200 and r2.json()["self_confirmed"] is False
    assert "в системе нет" in r2.json()["self_note"]


def test_boss_own_deposit_note_does_not_claim_no_boss(db, client):
    oid = _order(db, total=40.0, payment_type="credit", status="shipped", uid=BOSS)
    _record(oid, [_cash(40)], uid=BOSS, role="boss")
    dep = _run(db.create_cash_deposit(BOSS, 40.0))
    res = _run(db.confirm_cash_deposit(dep["deposit_id"], BOSS, "Boss"))
    assert res["ok"] and res["self_note"] == "внёс и подтвердил руководитель"
    audit = _rows(db, "SELECT details FROM audit_log WHERE action = 'cash_deposit_confirmed'")[-1]["details"]
    assert "в системе нет" not in audit


def test_manager_cannot_confirm_card_payment_while_boss_active(db, client):
    from services.order_payments import PaymentError

    oid = _order(db, total=1000.0, payment_type="credit", status="shipped")
    rec = _record(oid, [_card(1000)])
    r = _post(client, MGR, "/api/orders/confirm_payment", order_id=oid)
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "confirm_forbidden"
    assert _run(db.get_payment(rec["payment_id"]))["status"] == "pending"
    assert _post(client, MGR2, "/api/orders/confirm_payment", order_id=oid).status_code == 403
    with pytest.raises(PaymentError):
        _run(db.confirm_payment(rec["payment_id"], MGR, "Manager"))
    assert _run(db.get_payment(rec["payment_id"]))["status"] == "pending"

    ok = _post(client, BOSS, "/api/orders/confirm_payment", order_id=oid)
    assert ok.status_code == 200 and ok.json()["confirmed_count"] == 1 and ok.json()["self_note"] is None

    # Без руководителя — менеджер подтверждает сам, пометка про отсутствие.
    oid2 = _order(db, total=500.0, payment_type="credit", status="shipped")
    _record(oid2, [_card(500)])
    _demote_boss(db)
    r2 = _post(client, MGR, "/api/orders/confirm_payment", order_id=oid2)
    assert r2.status_code == 200 and r2.json()["confirmed_count"] == 1
    assert "руководителя/бухгалтера в системе нет" in r2.json()["self_note"]


def test_bookkeeper_does_not_confirm_own_money_when_other_confirmer_exists(db):
    from services import order_payments

    db.set_role(7, "bk", "Bookkeeper", "bookkeeper")
    roles.invalidate_all_roles()
    rights = _run(order_payments.confirm_rights(7, [7]))
    assert rights["allowed"] is False and "Boss" in rights["error"]
    assert _run(order_payments.confirm_rights(7, [MGR]))["allowed"] is True
    assert _run(order_payments.confirm_rights(BOSS, [BOSS]))["allowed"] is True
    # Деактивированный руководитель подтверждающим не считается.
    _run(db.deactivate_user(BOSS, 7))
    assert _run(order_payments.confirm_rights(7, [7]))["allowed"] is True
    mgr = _run(order_payments.confirm_rights(MGR, [MGR]))
    assert mgr["allowed"] is False  # бухгалтер-то есть


def test_bot_dep_ok_refuses_manager_while_boss_active(db):
    from handlers.deposits import cb_deposit_confirm
    from tests.test_deposits_handler import _FakeBot, _FakeCall

    oid = _order(db, total=250.0, payment_type="credit", status="shipped")
    _record(oid, [_cash(250)])
    dep = _run(db.create_cash_deposit(MGR, 250.0))
    bot = _FakeBot()
    call = _FakeCall(f"dep_ok:{dep['deposit_id']}", uid=MGR, bot=bot)
    asyncio.run(cb_deposit_confirm(call, bot))
    assert call.alerts and "Boss" in call.alerts[0][0]
    assert _run(db.get_cash_deposit(dep["deposit_id"]))["status"] == "pending"
    assert not call.message.answers and not bot.sent


def test_bot_dep_ok_no_boss_self_note(db):
    from handlers.deposits import cb_deposit_confirm
    from tests.test_deposits_handler import _FakeBot, _FakeCall

    _demote_boss(db)
    oid = _order(db, total=250.0, payment_type="credit", status="shipped")
    _record(oid, [_cash(250)])
    dep = _run(db.create_cash_deposit(MGR, 250.0))
    bot = _FakeBot()
    call = _FakeCall(f"dep_ok:{dep['deposit_id']}", uid=MGR, bot=bot)
    asyncio.run(cb_deposit_confirm(call, bot))
    assert _run(db.get_cash_deposit(dep["deposit_id"]))["status"] == "confirmed"
    assert any("руководителя/бухгалтера в системе нет" in t for t, _ in call.message.answers)


def test_bot_pay_ok_shows_refusal_and_keeps_card(db, monkeypatch):
    import handlers.payments as hp
    from services import order_payments
    from tests.test_deposits_handler import _FakeBot, _FakeCall

    db.set_role(9, "adm", "Admin", "admin")
    roles.invalidate_all_roles()
    oid = _order(db, total=100.0, payment_type="credit", status="shipped")
    rec = _record(oid, [_card(100)])

    async def _refuse(*a, **k):
        raise order_payments.PaymentError("Подтверждает Boss", status=403, code="confirm_forbidden")

    monkeypatch.setattr(hp.adb, "confirm_payment", _refuse)
    bot = _FakeBot()
    call = _FakeCall(f"pay_ok:{rec['payment_id']}", uid=9, bot=bot)
    asyncio.run(hp.confirm_pay(call, bot))
    assert call.alerts and "Подтверждает Boss" in call.alerts[-1][0]
    assert not call.message.answers
    assert _run(db.get_payment(rec["payment_id"]))["status"] == "pending"
