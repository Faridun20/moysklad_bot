"""
Рассрочка по технике: график платежей и напоминания.

Проверяем то, из-за чего рассрочка врёт молча: сумма графика обязана сойтись с
ценой до копейки, платёж не должен выпадать из короткого месяца, взнос не
должен попадать в напоминания, а напоминание — уходить дважды.

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет.
"""

import asyncio
import importlib
from datetime import date

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _machine(vin="A-1", price_cents=2_500_000):
    from services import machines

    res = _run(machines.create_machine(
        vin=vin, name="JCB", created_by=2, price_cents=price_cents
    ))
    assert res["ok"], res
    return res["machine_id"]


def _credit(machine_id, price_cents=2_500_000, down=500_000, months=5):
    from services import machines

    res = _run(machines.create_deal(
        machine_id, kind="credit", price_cents=price_cents, buyer_name="Иванов",
        created_by=2, down_payment_cents=down, months=months,
    ))
    assert res["ok"], res
    return res


# ─── Построение графика ───────────────────────────────────────────────────────


def test_schedule_splits_the_remainder_by_months():
    from services import machines

    rows = machines.build_schedule(2_500_000, 500_000, 5, date(2026, 7, 31))
    assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4, 5]
    assert rows[0]["amount_cents"] == 500_000 and rows[0]["prepaid"] is True
    assert all(r["amount_cents"] == 400_000 for r in rows[1:])


def test_schedule_sums_up_to_the_price_exactly():
    """Копейки от деления уходят в последний платёж: иначе рассрочка закроется
    с хвостом в пару копеек, который никто не поймёт."""
    from services import machines

    rows = machines.build_schedule(1_000_000, 0, 3, date(2026, 1, 15))
    assert sum(r["amount_cents"] for r in rows) == 1_000_000
    assert rows[0]["amount_cents"] == 333_333
    assert rows[-1]["amount_cents"] == 333_334  # хвост здесь


def test_schedule_clamps_to_short_months():
    """Сделка 31 января иначе не имеет февральского платежа вовсе:
    date(2026, 2, 31) не существует."""
    from services import machines

    rows = machines.build_schedule(300_000, 0, 3, date(2026, 1, 31))
    assert [r["due_date"] for r in rows] == ["2026-02-28", "2026-03-31", "2026-04-30"]


def test_schedule_without_down_payment_has_no_zero_row():
    from services import machines

    rows = machines.build_schedule(300_000, 0, 3, date(2026, 5, 10))
    assert [r["seq"] for r in rows] == [1, 2, 3]


def test_installment_conditions_are_validated():
    from services import machines

    assert machines.validate_installment(100_000, 0, 5) == ""
    assert "от 1 до" in machines.validate_installment(100_000, 0, 0)
    assert "от 1 до" in machines.validate_installment(100_000, 0, 999)
    # Взнос во всю цену — это продажа, а не рассрочка: график был бы пустым.
    assert "продажа" in machines.validate_installment(100_000, 100_000, 5)
    assert "отрицательным" in machines.validate_installment(100_000, -1, 5)


# ─── Сделка с графиком ────────────────────────────────────────────────────────


def test_credit_deal_writes_its_schedule(isolated_db):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)

    rows = _run(machines.get_schedule(deal["deal_id"]))
    assert len(rows) == 6              # взнос + 5 платежей
    assert deal["payments"] == 5       # в ответе — только к оплате
    assert rows[0]["paid_at"] is not None   # взнос получен сразу
    assert all(r["paid_at"] is None for r in rows[1:])
    # Дату закрытия считает сервис — вводить её руками значит однажды разойтись
    # с графиком.
    assert deal["due_date"] == rows[-1]["due_date"]


def test_credit_deal_requires_a_term(isolated_db):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    res = _run(machines.create_deal(
        mid, kind="credit", price_cents=100_000, buyer_name="A", created_by=2
    ))
    assert res["ok"] is False
    assert "месяц" in res["error"]
    # Машина осталась непроданной — сделка не прошла целиком.
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "in_transit"


def test_sale_has_no_schedule(isolated_db):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    res = _run(machines.create_deal(
        mid, kind="sale", price_cents=100_000, buyer_name="A", created_by=2
    ))
    assert res["ok"], res
    assert _run(machines.get_schedule(res["deal_id"])) == []


# ─── Отметка платежей ─────────────────────────────────────────────────────────


def test_last_payment_closes_the_deal(isolated_db):
    """Закрывать рассрочку руками после последнего платежа значит однажды
    забыть это сделать."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid, months=2)
    rows = _run(machines.get_schedule(deal["deal_id"]))
    unpaid = [r for r in rows if r["paid_at"] is None]

    first = _run(machines.pay_installment(unpaid[0]["id"], user_id=2))
    assert first["ok"] and first["deal_closed"] is False
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "on_credit"

    last = _run(machines.pay_installment(unpaid[1]["id"], user_id=2))
    assert last["deal_closed"] is True
    assert _run(machines.get_machine(mid, role="boss"))["status"] == "sold"
    assert _run(machines.get_open_credit_deals()) == []


def test_double_paid_is_rejected(isolated_db):
    """Два «оплачен» с двух телефонов не должны разойтись с экраном."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    pid = [r for r in _run(machines.get_schedule(deal["deal_id"])) if r["paid_at"] is None][0]["id"]

    assert _run(machines.pay_installment(pid, user_id=2))["ok"] is True
    second = _run(machines.pay_installment(pid, user_id=2))
    assert second["ok"] is False
    assert second["current"] == "paid"


def test_paid_mark_can_be_taken_back(isolated_db):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    pid = [r for r in _run(machines.get_schedule(deal["deal_id"])) if r["paid_at"] is None][0]["id"]

    _run(machines.pay_installment(pid, user_id=2))
    assert _run(machines.pay_installment(pid, user_id=2, paid=False))["ok"] is True
    rows = {r["id"]: r for r in _run(machines.get_schedule(deal["deal_id"]))}
    assert rows[pid]["paid_at"] is None


def test_down_payment_cannot_be_toggled(isolated_db):
    """Взнос получен в момент сделки — отмечать его нечем и незачем."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    down = _run(machines.get_schedule(deal["deal_id"]))[0]

    res = _run(machines.pay_installment(down["id"], user_id=2))
    assert res["ok"] is False
    assert "взнос" in res["error"].lower()


# ─── Напоминания ──────────────────────────────────────────────────────────────


def _shift_due(db, payment_id, due):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE machine_deal_payments SET due_date = ? WHERE id = ?"),
                    (due, payment_id))
        conn.commit()


def test_due_installments_skips_the_down_payment(isolated_db):
    """Взнос оплачен в день сделки — в напоминания он попадать не должен."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    rows = _run(machines.get_schedule(deal["deal_id"]))
    _shift_due(db, rows[0]["id"], "2020-01-01")   # взнос «просрочен»

    due = _run(machines.due_installments("2026-07-31"))
    assert all(int(p["seq"]) > 0 for p in due)


def test_due_installments_catch_up_after_a_missed_day(isolated_db):
    """`due_date <= today`, а не `= today`: пропущенный прогон (деплой, сбой)
    не должен молча съесть платёж."""
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    first = [r for r in _run(machines.get_schedule(deal["deal_id"])) if r["seq"] == 1][0]
    _shift_due(db, first["id"], "2026-01-15")

    due = _run(machines.due_installments("2026-07-31"))
    assert [p["id"] for p in due] == [first["id"]]
    assert due[0]["machine_name"] == "JCB"
    assert due[0]["buyer_name"] == "Иванов"


def test_notification_is_sent_once(isolated_db):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    first = [r for r in _run(machines.get_schedule(deal["deal_id"])) if r["seq"] == 1][0]
    _shift_due(db, first["id"], "2026-01-15")

    assert _run(machines.mark_installment_notified(first["id"])) is True
    # Второй прогон (ретрай cron'а, второй процесс) не должен прислать дубль.
    assert _run(machines.mark_installment_notified(first["id"])) is False
    assert _run(machines.due_installments("2026-07-31")) == []


def test_paid_and_closed_deals_are_not_reminded(isolated_db):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    first = [r for r in _run(machines.get_schedule(deal["deal_id"])) if r["seq"] == 1][0]
    _shift_due(db, first["id"], "2026-01-15")
    _run(machines.pay_installment(first["id"], user_id=2))

    assert _run(machines.due_installments("2026-07-31")) == []


# ─── Ручки ────────────────────────────────────────────────────────────────────


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_deal_endpoint_builds_the_schedule(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)

    r = _post(client, "/api/machines/deal", 2, machine_id=mid, kind="credit",
              price="25 000", down_payment="5 000", months=5,
              buyer_name="Иванов", idempotency_key="c1")
    assert r.status_code == 200, r.text
    assert r.json()["payments"] == 5
    rows = _run(machines.get_schedule(r.json()["deal_id"]))
    assert sum(x["amount_cents"] for x in rows) == 2_500_000


def test_card_carries_the_schedule(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()
    _credit(mid)
    client = _client(monkeypatch)

    card = _post(client, "/api/machines/card", 2, machine_id=mid).json()
    deal = card["deals"][0]
    assert len(deal["payments"]) == 6
    assert deal["payments"][0]["seq"] == 0


def test_bad_term_is_400(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    mid = _machine()
    client = _client(monkeypatch)

    r = _post(client, "/api/machines/deal", 2, machine_id=mid, kind="credit",
              price="25 000", months=0, buyer_name="A", idempotency_key="c1")
    assert r.status_code == 400
    assert "месяц" in r.json()["detail"]


def test_payment_endpoint_is_boss_only(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    mid = _machine()
    deal = _credit(mid)
    pid = [r for r in _run(machines.get_schedule(deal["deal_id"])) if r["seq"] == 1][0]["id"]
    client = _client(monkeypatch)

    assert _post(client, "/api/machines/payment", 1, payment_id=pid).status_code == 403
    assert _post(client, "/api/machines/payment", 2, payment_id=pid).status_code == 200
    # Повтор — 409: состояние на сервере уже другое, карточку надо перечитать.
    assert _post(client, "/api/machines/payment", 2, payment_id=pid).status_code == 409
