"""
Ручки денег: дебиторка, прогноз, дисциплина, карточка покупателя.

Главное, что здесь проверяется — объём роли. Менеджер не должен увидеть ни
чужих заказов, ни рассрочек (их оформляет руководство), а разрез по менеджерам
управленческий и ему не адресован.

БД настоящая (isolated_db), корутины через asyncio.run.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "acc", "Bookkeeper", "bookkeeper")


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def _credit(db, vin="A-1", price=2_500_000, down=500_000, months=5, buyer="Иванов"):
    from services import machines

    m = _run(machines.create_machine(vin=vin, name="JCB", created_by=2, price_cents=price))
    assert m["ok"], m
    deal = _run(machines.create_deal(
        m["machine_id"], kind="credit", price_cents=price, buyer_name=buyer,
        created_by=2, down_payment_cents=down, months=months,
    ))
    assert deal["ok"], deal
    return deal["deal_id"]


def _credit_order(db, uid=1, agent="Acme", total=1000.0, due="2020-01-01"):
    """Заказ в кредит с наступившим сроком — просроченный долг."""
    oid = db.create_order(uid, "Manager", "")
    db.update_order_agent(oid, "AG-1", agent)
    db.add_order_item(oid, "Товар", "", 1, "шт", total)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(
            "UPDATE orders SET payment_type = 'credit', due_date = ?, currency = 'USD' WHERE id = ?"
        ), (due, oid))
        conn.commit()
    return oid


# ─── Дебиторка ────────────────────────────────────────────────────────────────


def test_receivables_joins_both_streams(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit_order(db)
    _credit(db)

    body = _post(_client(monkeypatch), "/api/money/receivables", 2).json()
    assert body["totals"]["orders"]["base_total"] == 1000
    assert body["totals"]["machines"]["base_total"] == 20000
    assert body["totals"]["all"]["base_total"] == 21000
    assert body["scope"] == "company"


def test_receivables_aging_has_every_bucket(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit_order(db)

    body = _post(_client(monkeypatch), "/api/money/receivables", 2).json()
    keys = [b["key"] for b in body["aging"]["buckets"]]
    assert keys == ["overdue_90", "overdue_60", "overdue_30", "overdue_1", "not_due"]
    # Заказ со сроком 2020 года — глубокая просрочка.
    assert body["aging"]["buckets"][0]["count"] == 1


def test_manager_sees_neither_machines_nor_owner_split(isolated_db, monkeypatch):
    """Рассрочки — участок руководства, а разрез по менеджерам управленческий."""
    db = isolated_db
    _setup(db)
    _credit_order(db, uid=1)
    _credit(db)

    body = _post(_client(monkeypatch), "/api/money/receivables", 1).json()
    assert body["scope"] == "personal"
    assert body["totals"]["machines"]["count"] == 0
    assert "by_owner" not in body


def test_owner_split_carries_names(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit_order(db, uid=1)

    body = _post(_client(monkeypatch), "/api/money/receivables", 2).json()
    assert body["by_owner"][0]["user_id"] == 1
    assert body["by_owner"][0]["name"] == "Manager"


def test_receivables_denied_to_bookkeeper(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    assert _post(_client(monkeypatch), "/api/money/receivables", 3).status_code == 403


# ─── Прогноз ──────────────────────────────────────────────────────────────────


def test_forecast_returns_requested_months(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit(db, months=5)

    body = _post(_client(monkeypatch), "/api/money/forecast", 2, months=3).json()
    assert len(body["months"]) == 3
    assert sum(m["count"] for m in body["months"]) > 0


def test_forecast_months_are_clamped(isolated_db, monkeypatch):
    """Запрос на 999 месяцев не должен строить 999 корзин."""
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)

    assert len(_post(client, "/api/money/forecast", 2, months=999).json()["months"]) == 12
    assert len(_post(client, "/api/money/forecast", 2, months=0).json()["months"]) == 1
    assert _post(client, "/api/money/forecast", 2, months="много").status_code == 400


def test_forecast_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    assert _post(_client(monkeypatch), "/api/money/forecast", 1).status_code == 403


# ─── Дисциплина ───────────────────────────────────────────────────────────────


def test_discipline_reports_period_and_shares(isolated_db, monkeypatch):
    from datetime import date

    from services import machines

    db = isolated_db
    _setup(db)
    deal_id = _credit(db, months=2)
    rows = [p for p in _run(machines.get_schedule(deal_id)) if p["seq"] > 0]
    # График строится вперёд, а дисциплина смотрит назад: чтобы платежи попали
    # в период, сдвигаем сроки в прошлое.
    past = date.today().replace(day=1).isoformat()
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for p in rows:
            cur.execute(db.q("UPDATE machine_deal_payments SET due_date = ? WHERE id = ?"),
                        (past, p["id"]))
        conn.commit()
    _run(machines.pay_installment(rows[0]["id"], user_id=2))

    body = _post(_client(monkeypatch), "/api/money/discipline", 2, period="month").json()
    assert body["ok"] is True
    assert body["period"]["label"]
    assert body["expected_count"] == 2
    assert body["paid_count"] == 1
    assert body["expected"]["base_total"] > body["collected"]["base_total"]


def test_discipline_ignores_future_schedule(isolated_db, monkeypatch):
    """График строится вперёд; «собрано за июль» не должно включать сентябрь."""
    db = isolated_db
    _setup(db)
    _credit(db, months=5)

    body = _post(_client(monkeypatch), "/api/money/discipline", 2, period="month").json()
    assert body["expected_count"] == 0
    assert body["on_time_share"] is None


def test_discipline_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    assert _post(_client(monkeypatch), "/api/money/discipline", 1).status_code == 403


# ─── Долги с техникой ─────────────────────────────────────────────────────────


def test_debts_carry_machine_rows_and_totals(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit_order(db)
    _credit(db)

    body = _post(_client(monkeypatch), "/api/debts", 2).json()
    assert len(body["machine_debts"]) == 1
    row = body["machine_debts"][0]
    assert row["buyer_name"] == "Иванов"
    assert row["remaining"] == 20000
    assert row["next_amount"] == 4000
    assert row["state"] in ("overdue", "due_today", "upcoming")
    assert body["totals"]["all"]["base_total"] == 21000


def test_debts_hide_machines_from_manager(isolated_db, monkeypatch):
    """Менеджеру это не пустой блок, а чужой участок."""
    db = isolated_db
    _setup(db)
    _credit_order(db, uid=1)
    _credit(db)

    body = _post(_client(monkeypatch), "/api/debts", 1).json()
    assert body["machine_debts"] == []
    assert body["totals"] is None


def test_closed_installment_leaves_the_debts_screen(isolated_db, monkeypatch):
    from services import machines

    db = isolated_db
    _setup(db)
    deal_id = _credit(db, months=2)
    for p in [x for x in _run(machines.get_schedule(deal_id)) if x["seq"] > 0]:
        _run(machines.pay_installment(p["id"], user_id=2))

    body = _post(_client(monkeypatch), "/api/debts", 2).json()
    assert body["machine_debts"] == []


# ─── Карточка покупателя ──────────────────────────────────────────────────────


def test_buyer_card_gathers_deals(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit(db, vin="A-1", months=5)
    _credit(db, vin="B-2", price=1_200_000, down=200_000, months=2)

    body = _post(_client(monkeypatch), "/api/machines/buyer", 2, buyer="иванов").json()
    assert len(body["deals"]) == 2
    assert body["outstanding"]["base_total"] == 30000
    assert body["deals"][0]["payments"]


def test_buyer_card_404_and_400(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)

    assert _post(client, "/api/machines/buyer", 2, buyer="Никого").status_code == 404
    assert _post(client, "/api/machines/buyer", 2, buyer="  ").status_code == 400


def test_buyer_card_is_boss_only(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit(db)
    assert _post(_client(monkeypatch), "/api/machines/buyer", 1,
                 buyer="Иванов").status_code == 403
