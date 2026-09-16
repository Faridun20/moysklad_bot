"""
Ежедневная сверка кассы (`services/cash_reconciliation.py`, `/api/cash/reconcile*`).

Что здесь проверяется по существу:
  * расхождение считается ПО КАЖДОЙ ВАЛЮТЕ отдельно и валюты друг на друга не
    влияют (доллары и сумы складывать нечем);
  * пересчёт «всё сошлось» тоже ПИШЕТСЯ — иначе не отличить «сверили» от «не
    сверяли»;
  * сверка НИЧЕГО не двигает: ни платежа, ни сдачи, ни долга после неё не
    появляется и не меняется — это наблюдение, а не операция;
  * наличные, которых в системе нет вовсе, целиком уходят в расхождение — ради
    этого случая («менеджер не занёс оплату») всё и затевалось;
  * менеджер видит только свою историю, руководство — всю.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient


# ─── Каркас ──────────────────────────────────────────────────────────────────


@pytest.fixture
def env(isolated_db, monkeypatch):
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    import services.rate_limit as rate_limit

    rate_limit.reset()
    db = isolated_db
    ids = {"boss": 100, "admin": 101, "mgr": 200, "mgr2": 201, "keeper": 300, "guest": 900}
    db.set_role(ids["boss"], "boss", "Boss", "boss")
    db.set_role(ids["admin"], "adm", "Admin", "admin")
    db.set_role(ids["mgr"], "mgr", "Фаридун М.", "manager")
    db.set_role(ids["mgr2"], "mgr2", "Второй", "manager")
    db.set_role(ids["keeper"], "kp", "Кладовщик", "warehouse_keeper")
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: ({"id": int(init_data), "first_name": "U", "username": "u"}
                           if init_data else None),
    )
    return TestClient(server.app), db, ids


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def _actor(uid: int, role: str = "manager", name: str = "Фаридун М."):
    from services.cash_reconciliation import Actor

    return Actor(user_id=uid, name=name, role=role)


def _cash_on_hand(db, mgr: int, currency: str, amount_cents: int) -> int:
    """Положить менеджеру на руки наличные: заказ + pending-платёж + строка разбивки.

    Ровно та конфигурация, которую считает `order_payments.cash_on_hand`
    (наличные, платёж `pending`, живой сдачи нет) — второй формулы «сколько у
    менеджера налички» в тестах заводить нельзя, иначе она разъедется с кодом.
    """
    oid = db.create_order(mgr, "Mgr", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", amount_cents / 100)
    db.update_order_status(oid, "shipped")
    now = db.now_str()
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(
            "INSERT INTO payments (user_id, username, full_name, amount_cents, currency, "
            "comment, status, created_at, order_id) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)"),
            (mgr, "mgr", "Mgr", amount_cents, currency, "нал", now, oid))
        pid = cur.lastrowid
        cur.execute(db.q(
            "INSERT INTO payment_parts (payment_id, order_id, method, currency, amount_cents, "
            "rate_source, order_amount_cents, created_by, created_by_name, created_at) "
            "VALUES (?, ?, 'cash', ?, ?, 'base', ?, ?, ?, ?)"),
            (pid, oid, currency, amount_cents, amount_cents, mgr, "Mgr", now))
        conn.commit()
    return oid


def _rows(db, sql, params=()):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q(sql), params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


# ─── Чистые функции: разбор формы и арифметика разницы ───────────────────────


def test_parse_counts_accepts_zero_and_rejects_garbage():
    from services.cash_reconciliation import CashCountError, parse_counts

    curs = ["USD", "UZS"]
    assert parse_counts({"USD": "1 200,50"}, curs) == {"USD": 120050}
    # Пересчитанный НОЛЬ — законный результат («в кассе пусто»), а не пустое поле.
    assert parse_counts({"USD": "0"}, curs) == {"USD": 0}
    # Пустое поле — «эту валюту не считал», в пересчёт не попадает.
    assert parse_counts([{"currency": "USD", "amount": "5"},
                         {"currency": "UZS", "amount": ""}], curs) == {"USD": 500}
    for bad in ({"USD": "12о"}, {"USD": "-5"}, {"EUR": "10"}, {}, "мусор"):
        with pytest.raises(CashCountError):
            parse_counts(bad, curs)


def test_diff_is_computed_per_currency_independently():
    """Доллары и сумы считаются сами по себе: недостача в одной валюте не
    «покрывается» излишком в другой (складывать их нечем — курс это уже
    переоценка, а не сверка)."""
    from services.cash_reconciliation import build_lines, summarize

    lines = build_lines({"USD": 50000, "UZS": 127000000},
                        {"USD": 60000, "UZS": 120000000})
    by = {ln["currency"]: ln for ln in lines}
    assert by["USD"]["diff_cents"] == -10000       # недостача 100 USD
    assert by["UZS"]["diff_cents"] == 7000000      # излишек 70 000 UZS
    assert summarize(lines)["mismatched_currencies"] == ["USD", "UZS"]
    # Одна валюта сошлась — вторая всё равно остаётся расхождением.
    mixed = build_lines({"USD": 60000, "UZS": 120000000},
                        {"USD": 60000, "UZS": 119000000})
    assert summarize(mixed)["mismatched_currencies"] == ["UZS"]
    assert summarize(build_lines({"USD": 100}, {"USD": 100}))["matched"] is True


def test_cash_absent_from_system_is_entirely_a_discrepancy():
    """Ровно тот случай, ради которого сверка и заводилась: оплату не занесли,
    по системе ноль, а деньги в кармане есть."""
    from services.cash_reconciliation import build_lines

    [line] = build_lines({"USD": 30000}, {})
    assert line["system_cents"] == 0
    assert line["diff_cents"] == 30000


def test_result_message_names_the_exact_discrepancy():
    from services.cash_reconciliation import result_message

    assert result_message([{"currency": "USD", "counted_cents": 100,
                            "system_cents": 100, "diff_cents": 0}]) \
        == "Записан пересчёт, расхождений нет"
    text = result_message([{"currency": "USD", "counted_cents": 40000,
                            "system_cents": 50000, "diff_cents": -10000}])
    assert "не хватает" in text and "100" in text and "USD" in text


# ─── Запись ──────────────────────────────────────────────────────────────────


def test_zero_diff_is_recorded_too(isolated_db):
    """«Записан пересчёт, расхождений нет» — тоже факт: без строки нельзя
    отличить сверку, которая сошлась, от сверки, которой не делали."""
    from services import cash_reconciliation as recon

    db = isolated_db
    _cash_on_hand(db, 200, "USD", 50000)
    res = asyncio.run(recon.record(_actor(200), {"USD": "500"}))
    assert res["matched"] is True
    assert res["message"] == "Записан пересчёт, расхождений нет"
    rows = _rows(db, "SELECT * FROM daily_cash_counts")
    assert len(rows) == 1
    assert (rows[0]["counted_cents"], rows[0]["system_cents"], rows[0]["diff_cents"]) \
        == (50000, 50000, 0)


def test_mismatch_records_diff_and_optional_note(isolated_db):
    from services import cash_reconciliation as recon

    db = isolated_db
    _cash_on_hand(db, 200, "USD", 50000)
    res = asyncio.run(recon.record(_actor(200), {"USD": "400"}, "забыл занести оплату вчера"))
    assert res["matched"] is False
    assert res["lines"][0]["diff_cents"] == -10000
    row = _rows(db, "SELECT * FROM daily_cash_counts")[0]
    assert row["diff_cents"] == -10000
    assert row["note"] == "забыл занести оплату вчера"
    # Примечание НЕ обязательно: расхождение записывается и без объяснения —
    # иначе его перестали бы записывать вовсе.
    assert asyncio.run(recon.record(_actor(200), {"USD": "300"}))["matched"] is False


def test_two_currencies_are_written_as_two_rows(isolated_db):
    from services import cash_reconciliation as recon

    db = isolated_db
    _cash_on_hand(db, 200, "USD", 50000)
    _cash_on_hand(db, 200, "UZS", 120000000)
    res = asyncio.run(recon.record(_actor(200), {"USD": "500", "UZS": "1300000"}))
    assert res["mismatched_currencies"] == ["UZS"]
    rows = {r["currency"]: r for r in _rows(db, "SELECT * FROM daily_cash_counts")}
    assert rows["USD"]["diff_cents"] == 0
    assert rows["UZS"]["diff_cents"] == 10000000
    # Строки одного пересчёта делят ключ — по нему их и склеивает история.
    assert rows["USD"]["request_key"] == rows["UZS"]["request_key"]


def test_reconciliation_moves_no_money(isolated_db):
    """Сверка — наблюдение. Ни платежа, ни сдачи, ни долга она не создаёт и не
    меняет: иначе «объяснил примечанием» стало бы способом закрыть недостачу."""
    from services import cash_reconciliation as recon

    db = isolated_db
    oid = _cash_on_hand(db, 200, "USD", 50000)
    before = {
        "payments": _rows(db, "SELECT * FROM payments ORDER BY id"),
        "parts": _rows(db, "SELECT * FROM payment_parts ORDER BY id"),
        "deposits": _rows(db, "SELECT * FROM cash_deposits ORDER BY id"),
        "order": _rows(db, "SELECT * FROM orders WHERE id = ?", (oid,)),
    }
    asyncio.run(recon.record(_actor(200), {"USD": "100"}, "деньги пропали"))
    after = {
        "payments": _rows(db, "SELECT * FROM payments ORDER BY id"),
        "parts": _rows(db, "SELECT * FROM payment_parts ORDER BY id"),
        "deposits": _rows(db, "SELECT * FROM cash_deposits ORDER BY id"),
        "order": _rows(db, "SELECT * FROM orders WHERE id = ?", (oid,)),
    }
    assert before == after


def test_repeat_with_same_key_does_not_write_second_count(isolated_db):
    from services import cash_reconciliation as recon

    db = isolated_db
    _cash_on_hand(db, 200, "USD", 50000)
    first = asyncio.run(recon.record(_actor(200), {"USD": "500"}, request_key="k-1"))
    again = asyncio.run(recon.record(_actor(200), {"USD": "500"}, request_key="k-1"))
    assert first["repeated"] is False and again["repeated"] is True
    assert len(_rows(db, "SELECT * FROM daily_cash_counts")) == 1
    # Повторный пересчёт БЕЗ ключа — законное второе действие («вечером
    # пересчитали ещё раз»), а не дубль.
    asyncio.run(recon.record(_actor(200), {"USD": "480"}))
    assert len(_rows(db, "SELECT * FROM daily_cash_counts")) == 2


def test_role_without_cash_cannot_record(isolated_db):
    from services.cash_reconciliation import CashCountError, record

    with pytest.raises(CashCountError) as e:
        asyncio.run(record(_actor(900, role="guest"), {"USD": "10"}))
    assert e.value.status == 403
    with pytest.raises(CashCountError):
        asyncio.run(record(_actor(300, role="warehouse_keeper"), {"USD": "10"}))


# ─── История и напоминание ───────────────────────────────────────────────────


def test_history_scopes_and_only_diff(isolated_db):
    from services import cash_reconciliation as recon

    db = isolated_db
    _cash_on_hand(db, 200, "USD", 50000)
    _cash_on_hand(db, 201, "USD", 10000)
    asyncio.run(recon.record(_actor(200), {"USD": "500"}))           # сошлось
    asyncio.run(recon.record(_actor(201, name="Второй"), {"USD": "50"}))  # недостача 50 USD

    mine = asyncio.run(recon.history(user_id=200))
    assert [r["counted_by"] for r in mine] == [200]
    everyone = asyncio.run(recon.history())
    assert sorted(r["counted_by"] for r in everyone) == [200, 201]
    diffs = asyncio.run(recon.history(only_diff=True))
    assert [r["counted_by"] for r in diffs] == [201]
    assert asyncio.run(recon.history(user_id=200, only_diff=True)) == []


def test_reminder_is_off_until_an_hour_is_set(isolated_db):
    """По умолчанию напоминания НЕТ: пустая настройка = «не напоминать».

    Очередь дел тем и ценна, что в ней нет ничего лишнего, — новый пункт не
    должен приезжать всем вместе с выкатом. Экран и запись сверки при этом
    работают всегда, выключатель только про напоминание."""
    from datetime import datetime

    from services import cash_reconciliation as recon

    db = isolated_db
    assert db.get_setting("cash_reconciliation_reminder_time", "") == ""
    assert recon.reminder_time_hhmm() is None
    assert asyncio.run(recon.reminder_due(200, "manager", datetime(2026, 9, 15, 23, 59))) is False
    # Мусор в настройке тоже гасит напоминание, а не подставляет свой час.
    for bad in ("вечером", "25:00", "18", "18:99"):
        db.set_setting("cash_reconciliation_reminder_time", bad)
        assert recon.reminder_time_hhmm() is None, bad


def test_done_today_and_reminder(isolated_db):
    from datetime import datetime

    from services import cash_reconciliation as recon

    db = isolated_db
    db.set_setting("cash_reconciliation_reminder_time", "18:00")
    _cash_on_hand(db, 200, "USD", 50000)
    early = datetime(2026, 9, 15, 9, 0)  # до часа напоминания
    late = datetime(2026, 9, 15, 19, 0)
    assert asyncio.run(recon.reminder_due(200, "manager", early)) is False
    assert asyncio.run(recon.reminder_due(200, "manager", late)) is True
    # Кладовщик сверку не записывает — и напоминания не получает.
    assert asyncio.run(recon.reminder_due(300, "warehouse_keeper", late)) is False
    asyncio.run(recon.record(_actor(200), {"USD": "500"}))
    assert asyncio.run(recon.done_today(200)) is True
    assert asyncio.run(recon.reminder_due(200, "manager", late)) is False
    # Час напоминания — настройка, а не константа.
    db.set_setting("cash_reconciliation_reminder_time", "20:30")
    assert recon.reminder_time_hhmm() == (20, 30)


def test_reminder_shows_even_with_empty_system_cash(isolated_db):
    """Пустой остаток напоминание НЕ прячет: «по системе ноль, а в кармане
    деньги» — как раз то, что сверка обязана поймать."""
    from datetime import datetime

    from services import cash_reconciliation as recon

    isolated_db.set_setting("cash_reconciliation_reminder_time", "18:00")
    assert asyncio.run(recon.reminder_due(200, "manager", datetime(2026, 9, 15, 19, 0))) is True


def test_work_queue_offers_reconciliation(isolated_db, monkeypatch):
    from datetime import datetime

    from services import work_queue

    monkeypatch.setattr("utils.helpers.local_now", lambda: datetime(2026, 9, 15, 19, 0))
    # Выключатель пуст — очередь дел о сверке молчит в любое время суток.
    assert "cash_reconciliation" not in {
        i["key"] for i in asyncio.run(work_queue.gather(200, "manager"))
    }
    isolated_db.set_setting("cash_reconciliation_reminder_time", "18:00")
    items = {i["key"]: i for i in asyncio.run(work_queue.gather(200, "manager"))}
    assert items["cash_reconciliation"]["screen"] == "money:reconcile"
    assert items["cash_reconciliation"]["severity"] == "info"
    assert "cash_reconciliation" not in {
        i["key"] for i in asyncio.run(work_queue.gather(300, "warehouse_keeper"))
    }


# ─── Ручки ───────────────────────────────────────────────────────────────────


def test_api_context_shows_system_amounts(env):
    client, db, ids = env
    _cash_on_hand(db, ids["mgr"], "USD", 50000)
    r = _post(client, "/api/cash/reconcile/context", ids["mgr"])
    assert r.status_code == 200
    body = r.json()
    assert {s["currency"]: s["amount_cents"] for s in body["system"]}["USD"] == 50000
    assert body["done_today"] is False
    assert body["can_see_all"] is False
    assert _post(client, "/api/cash/reconcile/context", ids["boss"]).json()["can_see_all"] is True


def test_api_record_and_history_permissions(env):
    client, db, ids = env
    _cash_on_hand(db, ids["mgr"], "USD", 50000)
    _cash_on_hand(db, ids["mgr2"], "USD", 10000)

    ok = _post(client, "/api/cash/reconcile", ids["mgr"],
               counts=[{"currency": "USD", "amount": "400"}], note="ошибся вчера")
    assert ok.status_code == 200
    assert ok.json()["matched"] is False
    assert _post(client, "/api/cash/reconcile", ids["mgr2"],
                 counts=[{"currency": "USD", "amount": "100"}]).json()["matched"] is True

    mine = _post(client, "/api/cash/reconcile/history", ids["mgr"]).json()
    assert mine["scope"] == "mine"
    assert {r["counted_by"] for r in mine["items"]} == {ids["mgr"]}
    # Менеджер не расширит охват флагом в теле: чужие пересчёты решает роль.
    wide = _post(client, "/api/cash/reconcile/history", ids["mgr"], user_id=ids["mgr2"]).json()
    assert {r["counted_by"] for r in wide["items"]} == {ids["mgr"]}

    boss = _post(client, "/api/cash/reconcile/history", ids["boss"]).json()
    assert boss["scope"] == "all"
    assert {r["counted_by"] for r in boss["items"]} == {ids["mgr"], ids["mgr2"]}
    only = _post(client, "/api/cash/reconcile/history", ids["boss"], only_diff=True).json()
    assert {r["counted_by"] for r in only["items"]} == {ids["mgr"]}


def test_api_rejects_foreign_roles_and_bad_input(env):
    client, db, ids = env
    for uid in (ids["keeper"], ids["guest"]):
        for path in ("/api/cash/reconcile/context", "/api/cash/reconcile",
                     "/api/cash/reconcile/history"):
            assert _post(client, path, uid, counts={"USD": "1"}).status_code == 403
    bad = _post(client, "/api/cash/reconcile", ids["mgr"], counts={"USD": "12о"})
    assert bad.status_code == 400
    assert "числ" in bad.json()["detail"]
    assert _post(client, "/api/cash/reconcile", ids["mgr"], counts={}).status_code == 400


def test_boss_digest_reports_mismatches(isolated_db):
    from services import boss_digest
    from services import cash_reconciliation as recon

    db = isolated_db
    _cash_on_hand(db, 200, "USD", 50000)
    asyncio.run(recon.record(_actor(200), {"USD": "400"}, "забыл занести оплату"))
    data = asyncio.run(boss_digest.gather())
    assert data["cash_counts"]["count"] == 1
    assert boss_digest.is_empty(data) is False
    text = boss_digest.build_text(data)
    assert "Сверка кассы" in text and "недостача" in text and "забыл занести оплату" in text
