"""
Слой дебиторки: «где деньги».

Проверяем то, из-за чего сводка врёт молча: границы корзин просрочки, смешение
валют без курса, попадание просроченного в прогноз будущих поступлений и
подсчёт «в срок».

БД настоящая (isolated_db), корутины через asyncio.run — pytest-asyncio в
проекте нет.
"""

import asyncio
from datetime import date

import services.roles as roles
from services.receivables import Receivable


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _r(amount=100_000, due="2026-07-01", source="order", cur="USD", who="Acme", owner=1):
    return Receivable(source, 1, "t", who, owner, due, amount, cur)


TODAY = date(2026, 8, 1)


# ─── Корзины просрочки ────────────────────────────────────────────────────────


def test_bucket_boundaries_are_exact():
    """Ровно на границе платёж не должен перепрыгивать в соседнюю корзину."""
    from services.receivables import bucket_of

    assert bucket_of("2026-08-01", TODAY) == "not_due"   # срок сегодня
    assert bucket_of("2026-08-05", TODAY) == "not_due"   # впереди
    assert bucket_of("2026-07-31", TODAY) == "overdue_1"
    assert bucket_of("2026-07-02", TODAY) == "overdue_1"   # 30 дней ровно
    assert bucket_of("2026-07-01", TODAY) == "overdue_30"  # 31 день
    assert bucket_of("2026-06-02", TODAY) == "overdue_30"  # 60 ровно
    assert bucket_of("2026-06-01", TODAY) == "overdue_60"  # 61
    assert bucket_of("2026-05-03", TODAY) == "overdue_60"  # 90 ровно
    assert bucket_of("2026-05-02", TODAY) == "overdue_90"  # 91


def test_missing_due_date_is_not_overdue():
    """Заказ без срока просрочить нельзя — срок ему просто не ставили."""
    from services.receivables import bucket_of

    assert bucket_of(None, TODAY) == "not_due"
    assert bucket_of("", TODAY) == "not_due"
    assert bucket_of("кривая дата", TODAY) == "not_due"


def test_aging_splits_and_sums(isolated_db):
    from services.receivables import aging

    res = aging([
        _r(100_000, "2026-05-01"),   # >90
        _r(200_000, "2026-07-31"),   # <30
        _r(300_000, "2026-09-01"),   # впереди
    ], TODAY)
    buckets = {b["key"]: b for b in res["buckets"]}
    assert buckets["overdue_90"]["base_total"] == 1000
    assert buckets["overdue_1"]["base_total"] == 2000
    assert buckets["not_due"]["base_total"] == 3000
    # Просрочка — сумма всех корзин, кроме «срок не наступил».
    assert res["overdue"]["base_total"] == 3000
    assert res["total"]["base_total"] == 6000


def test_empty_bucket_is_present_with_zero(isolated_db):
    """Корзина без строк должна остаться в ответе: исчезнувшая строка читается
    как «данные не посчитали», а не как «здесь ноль»."""
    from services.receivables import AGING_BUCKETS, aging

    res = aging([_r(100_000, "2026-09-01")], TODAY)
    assert [b["key"] for b in res["buckets"]] == list(AGING_BUCKETS)
    assert all(b["count"] == 0 for b in res["buckets"] if b["key"] != "not_due")


# ─── Валюты ───────────────────────────────────────────────────────────────────


def test_unknown_rate_marks_the_total_partial(isolated_db):
    """«5 000 UZS + 200 USD» без курса — не число, а флаг «посчитано не всё»."""
    from services.receivables import aging

    db = isolated_db
    _setup(db)
    res = aging([_r(100_000, "2026-09-01", cur="USD"),
                 _r(500_000_00, "2026-09-01", cur="UZS")], TODAY)
    total = res["total"]
    assert total["partial"] is True
    assert total["base_total"] == 1000            # только USD-часть
    # Валюта без курса из разбивки не пропадает — долг существует и без курса.
    assert {c["currency"] for c in total["by_currency"]} == {"USD", "UZS"}


def test_known_rate_is_converted(isolated_db):
    from services.receivables import aging

    db = isolated_db
    _setup(db)
    db.set_currency_rate("UZS", 0.00008, 2)
    res = aging([_r(100_000, "2026-09-01", cur="USD"),
                 _r(1_000_000_00, "2026-09-01", cur="UZS")], TODAY)
    assert res["total"]["partial"] is False
    assert res["total"]["base_total"] == 1000 + 80


# ─── Итоги по источникам ──────────────────────────────────────────────────────


def test_totals_split_orders_and_machines(isolated_db):
    from services.receivables import totals_by_source

    res = totals_by_source([
        _r(100_000, source="order"),
        _r(300_000, source="machine"),
    ])
    assert res["orders"]["base_total"] == 1000
    assert res["machines"]["base_total"] == 3000
    assert res["all"]["base_total"] == 4000
    assert res["all"]["count"] == 2


# ─── Прогноз ──────────────────────────────────────────────────────────────────


def test_forecast_buckets_by_month(isolated_db):
    from services.receivables import forecast

    res = forecast([
        _r(100_000, "2026-08-10"),
        _r(200_000, "2026-08-25"),
        _r(300_000, "2026-09-05"),
    ], months=3, today=TODAY)
    assert [m["month"] for m in res] == ["2026-08", "2026-09", "2026-10"]
    assert res[0]["base_total"] == 3000
    assert res[1]["base_total"] == 3000
    assert res[2]["count"] == 0


def test_forecast_excludes_overdue(isolated_db):
    """«Должны были заплатить в марте» — это не поступление августа: иначе
    прогноз рисует деньги, которых никто не ждёт."""
    from services.receivables import forecast

    res = forecast([_r(100_000, "2026-03-01"), _r(200_000, "2026-08-20")],
                   months=2, today=TODAY)
    assert res[0]["base_total"] == 2000
    assert res[0]["count"] == 1


def test_forecast_marks_the_machine_share(isolated_db):
    from services.receivables import forecast

    res = forecast([
        _r(100_000, "2026-08-10", source="order"),
        _r(400_000, "2026-08-11", source="machine"),
    ], months=1, today=TODAY)
    assert res[0]["base_total"] == 5000
    assert res[0]["machines"]["base_total"] == 4000


# ─── Группировки ──────────────────────────────────────────────────────────────


def test_top_debtors_sorted_by_amount(isolated_db):
    from services.receivables import by_counterparty

    rows = by_counterparty([
        _r(100_000, who="Малый"),
        _r(900_000, who="Крупный"),
        _r(100_000, who="Крупный"),
    ])
    assert [r["name"] for r in rows] == ["Крупный", "Малый"]
    assert rows[0]["base_total"] == 10000


def test_debtor_row_shows_both_sources(isolated_db):
    from services.receivables import by_counterparty

    rows = by_counterparty([
        _r(100_000, who="Иванов", source="order"),
        _r(100_000, who="Иванов", source="machine"),
    ])
    assert rows[0]["sources"] == ["machine", "order"]


def test_by_owner_ignores_machines(isolated_db):
    """У рассрочки нет менеджера-владельца — приписывать её кому-то нельзя."""
    from services.receivables import by_owner

    rows = by_owner([
        _r(100_000, source="order", owner=1),
        _r(900_000, source="machine", owner=None),
    ])
    assert len(rows) == 1
    assert rows[0]["user_id"] == 1
    assert rows[0]["base_total"] == 1000


# ─── Сбор из БД ───────────────────────────────────────────────────────────────


def _machine_with_credit(db, vin="A-1", price=2_500_000, down=500_000, months=5):
    from services import machines

    m = _run(machines.create_machine(vin=vin, name="JCB", created_by=2, price_cents=price))
    assert m["ok"], m
    deal = _run(machines.create_deal(
        m["machine_id"], kind="credit", price_cents=price, buyer_name="Иванов",
        created_by=2, down_payment_cents=down, months=months,
    ))
    assert deal["ok"], deal
    return m["machine_id"], deal["deal_id"]


def test_collect_includes_unpaid_installments(isolated_db):
    from services.receivables import collect

    db = isolated_db
    _setup(db)
    _machine_with_credit(db)

    items = _run(collect())
    machine_rows = [r for r in items if r.source == "machine"]
    # Взнос оплачен в момент сделки — в дебиторку он не попадает.
    assert len(machine_rows) == 5
    assert sum(r.amount_cents for r in machine_rows) == 2_000_000
    assert machine_rows[0].counterparty == "Иванов"


def test_collect_drops_paid_and_closed(isolated_db):
    from services import machines
    from services.receivables import collect

    db = isolated_db
    _setup(db)
    _mid, deal_id = _machine_with_credit(db, months=2)
    rows = [p for p in _run(machines.get_schedule(deal_id)) if p["seq"] > 0]
    _run(machines.pay_installment(rows[0]["id"], user_id=2))

    assert len([r for r in _run(collect()) if r.source == "machine"]) == 1

    _run(machines.pay_installment(rows[1]["id"], user_id=2))  # закрывает сделку
    assert [r for r in _run(collect()) if r.source == "machine"] == []


def test_manager_scope_excludes_machines(isolated_db):
    """Рассрочки оформляет руководство: менеджеру это не пустой блок, а чужой
    участок."""
    from services.receivables import collect

    db = isolated_db
    _setup(db)
    _machine_with_credit(db)

    items = _run(collect(user_id=1, include_machines=False))
    assert all(r.source == "order" for r in items)


# ─── Дисциплина ───────────────────────────────────────────────────────────────


def _shift(db, payment_id, due=None, paid=None):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        if due is not None:
            cur.execute(db.q("UPDATE machine_deal_payments SET due_date = ? WHERE id = ?"),
                        (due, payment_id))
        if paid is not None:
            cur.execute(db.q("UPDATE machine_deal_payments SET paid_at = ? WHERE id = ?"),
                        (paid, payment_id))
        conn.commit()


def test_collection_counts_on_time_by_date(isolated_db):
    """Платёж в день срока опозданием не считается — сравниваем даты, не время."""
    from services import machines
    from services.receivables import collection_stats

    db = isolated_db
    _setup(db)
    _mid, deal_id = _machine_with_credit(db, months=3)
    rows = [p for p in _run(machines.get_schedule(deal_id)) if p["seq"] > 0]
    _shift(db, rows[0]["id"], due="2026-07-05", paid="2026-07-05 18:00:00")  # в срок
    _shift(db, rows[1]["id"], due="2026-07-10", paid="2026-07-15 09:00:00")  # с опозданием
    _shift(db, rows[2]["id"], due="2026-07-20")                              # не оплачен

    res = _run(collection_stats("2026-07-01", "2026-07-31"))
    assert res["expected_count"] == 3
    assert res["paid_count"] == 2
    assert res["on_time_count"] == 1
    assert res["on_time_share"] == 0.5
    assert res["expected"]["base_total"] > res["collected"]["base_total"]


def test_collection_period_is_respected(isolated_db):
    from services import machines
    from services.receivables import collection_stats

    db = isolated_db
    _setup(db)
    _mid, deal_id = _machine_with_credit(db, months=2)
    rows = [p for p in _run(machines.get_schedule(deal_id)) if p["seq"] > 0]
    _shift(db, rows[0]["id"], due="2026-07-10")
    _shift(db, rows[1]["id"], due="2026-09-10")

    assert _run(collection_stats("2026-07-01", "2026-07-31"))["expected_count"] == 1


def test_laggards_counted_by_repetition(isolated_db):
    """Систематичность важнее размера конкретного платежа."""
    from services import machines
    from services.receivables import collection_stats

    db = isolated_db
    _setup(db)
    _mid, deal_id = _machine_with_credit(db, months=3)
    rows = [p for p in _run(machines.get_schedule(deal_id)) if p["seq"] > 0]
    for r in rows[:2]:
        _shift(db, r["id"], due="2026-07-10", paid="2026-07-20 10:00:00")
    _shift(db, rows[2]["id"], due="2026-07-15", paid="2026-07-15 10:00:00")

    res = _run(collection_stats("2026-07-01", "2026-07-31"))
    assert res["laggards"][0]["name"] == "Иванов"
    assert res["laggards"][0]["late"] == 2
    assert res["laggards"][0]["total"] == 3


def test_no_expected_payments_is_not_a_crash(isolated_db):
    from services.receivables import collection_stats

    db = isolated_db
    _setup(db)
    res = _run(collection_stats("2026-07-01", "2026-07-31"))
    assert res["expected_count"] == 0
    assert res["on_time_share"] is None


# ─── Карточка покупателя ──────────────────────────────────────────────────────


def test_buyer_key_folds_case_and_spaces():
    """Настоящего идентификатора у покупателя нет — иначе один человек выглядит
    как двое должников."""
    from services.receivables import buyer_key

    assert buyer_key("Иванов  П.") == buyer_key("иванов п.")
    assert buyer_key("  Иванов П. ") == buyer_key("Иванов П.")
    assert buyer_key(None) == ""


def test_buyer_card_gathers_all_deals(isolated_db):
    from services.receivables import buyer_card

    db = isolated_db
    _setup(db)
    _machine_with_credit(db, vin="A-1", months=5)
    _machine_with_credit(db, vin="B-2", price=1_200_000, down=200_000, months=2)

    card = _run(buyer_card("иванов"))
    assert len(card["deals"]) == 2
    assert card["outstanding"]["count"] == 7          # 5 + 2 неоплаченных
    assert card["outstanding"]["base_total"] == 30000  # 20 000 + 10 000
    assert card["aging"]["total"]["count"] == 7


def test_buyer_card_of_unknown_person_is_empty(isolated_db):
    from services.receivables import buyer_card

    db = isolated_db
    _setup(db)
    assert _run(buyer_card("Никого")) == {}


def test_next_due_is_batched(isolated_db):
    """Ближайший платёж каждой сделки одним запросом: иначе список рассрочек
    превращается в N+1 на первом же десятке."""
    from services import machines
    from services.receivables import next_due_by_deal

    db = isolated_db
    _setup(db)
    _mid, deal_id = _machine_with_credit(db, months=3)
    rows = [p for p in _run(machines.get_schedule(deal_id)) if p["seq"] > 0]
    _run(machines.pay_installment(rows[0]["id"], user_id=2))

    nxt = _run(next_due_by_deal([deal_id]))
    assert nxt[deal_id]["seq"] == 2
    assert _run(next_due_by_deal([])) == {}
