"""
Раздел «Деньги»: итоги поступлений за период (get_money_totals + /api/money/summary)
и мульти-валютная отправка платежей (/api/payments/send с items).

Реальная БД (isolated_db); Telegram initData и граница Telegram (notifier) мокаются.
Денежное ядро — целые копейки; проверяем суммы в копейках.
"""

import asyncio
import importlib

from fastapi.testclient import TestClient

import services.roles as roles


# ─── helpers ──────────────────────────────────────────────────────────────────


def _confirmed_payment(db, uid, amount, currency, order_id=None):
    pid = db.add_payment(uid, "m", "Mgr", amount, currency, "c", order_id=order_id)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET status='confirmed' WHERE id=?"), (pid,))
        conn.commit()
    return pid


def _credit_shipped(db, uid, total, currency, agent):
    oid = db.create_order(uid, "Mgr", "")
    db.update_order_agent(oid, agent, agent)
    db.add_order_item(oid, "P", "", 1, "шт", total)
    db.update_order_status(oid, "shipped")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET payment_type='credit', currency=? WHERE id=?"),
            (currency, oid),
        )
        conn.commit()
    return oid


def _confirmed_deposit(db, uid, amount, created_at="2026-06-03 10:00:00"):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO cash_deposits (manager_id, amount_cents, deposited_at, "
                "status, created_at) VALUES (?, ?, ?, 'confirmed', ?)"
            ),
            (uid, int(round(amount * 100)), created_at, created_at),
        )
        conn.commit()


def _client(db, monkeypatch, uid, role):
    import webapp.server as server

    importlib.reload(roles)
    db.set_role(uid, "u", "U", role)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})

    import services.notifier as notifier

    sent = []

    async def _recips():
        return [999]

    async def _send(uid_, text, **k):
        sent.append((uid_, text, k.get("reply_markup")))

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)
    return TestClient(server.app), sent


# ─── get_money_totals ───────────────────────────────────────────────────────


def test_get_money_totals_by_currency_excludes_deleted(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    live = _credit_shipped(db, 1, 100.0, "USD", "A-live")
    _confirmed_payment(db, 1, 100.0, "USD", order_id=live)
    _confirmed_payment(db, 1, 50.0, "USD", order_id=None)  # standalone
    _confirmed_payment(db, 1, 70000.0, "UZS", order_id=None)
    phantom = _credit_shipped(db, 1, 30.0, "USD", "A-phantom")
    _confirmed_payment(db, 1, 30.0, "USD", order_id=phantom)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET ms_deleted_at='2026-06-03 00:00:00' WHERE id=?"), (phantom,)
        )
        conn.commit()
    _confirmed_deposit(db, 1, 500.0)

    totals = asyncio.run(db.get_money_totals())
    by = {p["currency"]: p["total_cents"] for p in totals["payments"]}
    # 100 (live) + 50 (standalone) = 150 USD; 30 phantom исключён.
    assert by["USD"] == 15000
    assert by["UZS"] == 7000000
    assert totals["deposits"]["total_cents"] == 50000
    assert totals["deposits"]["count"] == 1


def test_deleted_order_payments_excluded_from_all_money_surfaces(isolated_db):
    """WP-03: платёж по удалённому заказу исключён из ВСЕХ денежных поверхностей
    (итог, касса, отчёт по оплатам, по сотрудникам) — иначе бот и WebApp
    расходятся в сумме. standalone и платёж по живому заказу учитываются."""
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    live = _credit_shipped(db, 1, 100.0, "USD", "A-live")
    _confirmed_payment(db, 1, 100.0, "USD", order_id=live)
    phantom = _credit_shipped(db, 1, 30.0, "USD", "A-phantom")
    _confirmed_payment(db, 1, 30.0, "USD", order_id=phantom)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET ms_deleted_at='2026-06-03 00:00:00' WHERE id=?"), (phantom,)
        )
        conn.commit()

    totals = asyncio.run(db.get_money_totals())
    assert {p["currency"]: p["total_cents"] for p in totals["payments"]}["USD"] == 10000

    cb = db.get_cashbox_stats()  # sync, бот /cashbox
    assert cb["total_cents"] == 10000  # 100, не 130

    rep = asyncio.run(db.get_payments_report())  # бот /payreport
    order_ids = {r["order_id"] for r in rep}
    assert live in order_ids and phantom not in order_ids

    emp = asyncio.run(db.get_summary_by_employee())  # бот /payreport (по сотрудникам)
    assert sum(float(r["total"]) for r in emp) == 100.0


def test_get_money_totals_period_filter(isolated_db):
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    old = _confirmed_payment(db, 1, 100.0, "USD")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET created_at='2000-01-01 00:00:00' WHERE id=?"), (old,))
        conn.commit()
    _confirmed_payment(db, 1, 40.0, "USD")  # свежий (created_at = now)

    totals = asyncio.run(db.get_money_totals(since="2026-01-01 00:00:00"))
    by = {p["currency"]: p["total_cents"] for p in totals["payments"]}
    assert by.get("USD") == 4000  # только свежий, старый вне окна


# ─── /api/money/summary ───────────────────────────────────────────────────────


def test_money_summary_boss(isolated_db, monkeypatch):
    db = isolated_db
    _confirmed_payment(db, 10, 100.0, "USD")
    client, _ = _client(db, monkeypatch, 600, "boss")
    body = client.post("/api/money/summary", json={"initData": "600", "period": "year"}).json()
    by = {p["currency"]: p["total_cents"] for p in body["payments"]}
    assert by.get("USD") == 10000
    assert "period" in body and body["period"]["label"] == "Год"


def test_money_summary_forbidden_for_manager(isolated_db, monkeypatch):
    db = isolated_db
    client, _ = _client(db, monkeypatch, 601, "manager")
    r = client.post("/api/money/summary", json={"initData": "601", "period": "month"})
    assert r.status_code == 403


# ─── /api/payments/send (мульти-валюта) ───────────────────────────────────────


def test_payments_send_batch_creates_multiple(isolated_db, monkeypatch):
    db = isolated_db
    client, sent = _client(db, monkeypatch, 700, "manager")
    r = client.post(
        "/api/payments/send",
        json={
            "initData": "700",
            "items": [{"amount": 100, "currency": "USD"}, {"amount": 5000, "currency": "UZS"}],
            "comment": "аренда",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["payment_ids"]) == 2
    # Одно уведомление боссу с кнопкой на каждый платёж.
    assert len(sent) == 1
    kb = sent[0][2]["inline_keyboard"]
    assert len(kb) == 2
    assert kb[0][0]["callback_data"].startswith("pay_ok:")


def test_payments_send_batch_rejects_bad_amount(isolated_db, monkeypatch):
    db = isolated_db
    client, _ = _client(db, monkeypatch, 701, "manager")
    r = client.post(
        "/api/payments/send",
        json={"initData": "701", "items": [{"amount": -1, "currency": "USD"}], "comment": "x"},
    )
    assert r.status_code == 400


def test_payments_send_batch_requires_comment(isolated_db, monkeypatch):
    db = isolated_db
    client, _ = _client(db, monkeypatch, 702, "manager")
    r = client.post(
        "/api/payments/send",
        json={"initData": "702", "items": [{"amount": 10, "currency": "USD"}], "comment": ""},
    )
    assert r.status_code == 400


def test_payments_send_batch_partial_failure_no_dup_on_retry(isolated_db, monkeypatch):
    """Сбой add_payment в середине батча: уже созданные платежи нельзя откатить
    (построчный автокоммит). Ретрай тем же ключом НЕ создаёт их повторно —
    возвращает сохранённый частичный результат (status='partial')."""
    import pytest

    db = isolated_db
    client, _ = _client(db, monkeypatch, 704, "manager")

    import services.async_db as adb

    real_add = adb.add_payment
    calls = {"n": 0}

    async def _flaky_add(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # второй платёж в батче падает
            raise RuntimeError("boom")
        return await real_add(*a, **k)

    monkeypatch.setattr(adb, "add_payment", _flaky_add)
    payload = {
        "initData": "704",
        "items": [{"amount": 100, "currency": "USD"}, {"amount": 200, "currency": "USD"}],
        "comment": "аренда",
        "idempotency_key": "kp",
    }
    with pytest.raises(RuntimeError):
        client.post("/api/payments/send", json=payload)

    def _count():
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(db.q("SELECT COUNT(*) FROM payments WHERE user_id=?"), (704,))
            return cur.fetchone()[0]

    assert _count() == 1  # первый платёж закоммитился

    monkeypatch.setattr(adb, "add_payment", real_add)  # сбой снят
    r2 = client.post("/api/payments/send", json=payload)  # ретрай тем же ключом
    assert r2.status_code == 200, r2.text
    assert r2.json().get("status") == "partial"
    assert _count() == 1  # дубля нет — вернулся сохранённый частичный результат


def test_money_summary_base_total_includes_negative_deposits(isolated_db, monkeypatch):
    """Нетто-сдачи могут быть отрицательными (cash-возвраты уменьшают кассу) —
    base_total обязан их учесть, а не отбрасывать (фильтр amt != 0, не > 0)."""
    db = isolated_db
    _confirmed_payment(db, 20, 100.0, "USD")  # +100 USD
    _confirmed_deposit(db, 20, -30.0)  # cash-возврат −30 USD
    client, _ = _client(db, monkeypatch, 610, "boss")
    body = client.post("/api/money/summary", json={"initData": "610", "period": "year"}).json()
    assert body["base_total"] == 70.0  # 100 − 30, отрицательная сдача учтена


def test_money_summary_counts_by_confirmation_time(isolated_db, monkeypatch):
    """«Поступления за период» считаются по confirmed_at (когда деньги получены),
    а не created_at. Платёж/сдача, созданные ДО периода, но подтверждённые В нём,
    обязаны попасть в итог (был баг «не считает платежи и сдачу»)."""
    db = isolated_db
    # Платёж создан в прошлом году, подтверждён сегодня.
    pid = db.add_payment(40, "m", "Mgr", 100.0, "USD", "c")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE payments SET status='confirmed', created_at=?, confirmed_at=? WHERE id=?"),
            ("2020-01-01 00:00:00", db.now_str(), pid),
        )
        # Сдача: создана в прошлом году, подтверждена сегодня.
        cur.execute(
            db.q(
                "INSERT INTO cash_deposits (manager_id, amount_cents, status, "
                "created_at, confirmed_at) VALUES (?, ?, 'confirmed', ?, ?)"
            ),
            (40, 5000, "2020-01-01 00:00:00", db.now_str()),
        )
        conn.commit()
    client, _ = _client(db, monkeypatch, 615, "boss")
    # Период «месяц» (с 1-го числа текущего месяца) — created_at в 2020 НЕ попал бы,
    # но confirmed_at сегодня → должно посчитаться.
    body = client.post("/api/money/summary", json={"initData": "615", "period": "month"}).json()
    by = {p["currency"]: p["total_cents"] for p in body["payments"]}
    assert by.get("USD") == 10000          # платёж посчитан по дате подтверждения
    assert body["deposits"]["total_cents"] == 5000  # сдача тоже


def test_money_summary_unrated_currency_not_dropped(isolated_db, monkeypatch):
    """Регресс «Обзор не считает суммы >999»: крупная сумма в валюте БЕЗ курса
    (UZS 5 000 000) не должна молча выпадать из итога — она в missing_rates,
    base_partial=True, а в base_total входит только то, что сконвертировалось."""
    db = isolated_db
    _confirmed_payment(db, 30, 100.0, "USD")          # есть курс (база)
    _confirmed_payment(db, 30, 5_000_000.0, "UZS")    # курса нет → не теряем молча
    client, _ = _client(db, monkeypatch, 611, "boss")
    body = client.post("/api/money/summary", json={"initData": "611", "period": "year"}).json()
    assert body["base_total"] == 100.0          # только USD сконвертирован
    assert body["base_partial"] is True
    miss = {m["currency"]: m["amount"] for m in body["missing_rates"]}
    assert miss.get("UZS") == 5_000_000.0       # крупная сумма видна явно, не потеряна


def test_money_summary_full_total_when_rate_set(isolated_db, monkeypatch):
    """Когда курс задан — UZS входит в единый «≈» итог, missing_rates пуст."""
    db = isolated_db
    db.set_currency_rate("UZS", 0.00008, 1)  # 1 UZS = 0.00008 USD
    db._invalidate_currency_rates_cache()
    _confirmed_payment(db, 31, 100.0, "USD")
    _confirmed_payment(db, 31, 5_000_000.0, "UZS")  # 5M UZS → 400 USD
    client, _ = _client(db, monkeypatch, 612, "boss")
    body = client.post("/api/money/summary", json={"initData": "612", "period": "year"}).json()
    assert body["base_total"] == 500.0       # 100 + 400
    assert body["base_partial"] is False
    assert body["missing_rates"] == []


def test_payments_send_single_idempotent(isolated_db, monkeypatch):
    """WP-22: ретрай одиночного платежа тем же idempotency_key не создаёт дубль
    и не шлёт второе уведомление (раньше идемпотентность была только у batch)."""
    db = isolated_db
    client, sent = _client(db, monkeypatch, 750, "manager")
    payload = {
        "initData": "750", "amount": 100, "currency": "USD",
        "comment": "аренда", "idempotency_key": "single-1",
    }
    r1 = client.post("/api/payments/send", json=payload)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/payments/send", json=payload)  # тот же ключ
    assert r2.status_code == 200, r2.text
    assert r2.json()["payment_id"] == r1.json()["payment_id"]  # тот же платёж
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM payments WHERE user_id=?"), (750,))
        assert cur.fetchone()[0] == 1  # ровно один, дубля нет
    assert len(sent) == 1  # уведомление ушло один раз


def test_payments_send_batch_idempotent(isolated_db, monkeypatch):
    """Ретрай с тем же idempotency_key не создаёт дубль-набор платежей и не шлёт
    второе уведомление — отдаёт сохранённый результат."""
    db = isolated_db
    client, sent = _client(db, monkeypatch, 703, "manager")
    payload = {
        "initData": "703",
        "items": [{"amount": 100, "currency": "USD"}],
        "comment": "аренда",
        "idempotency_key": "k1",
    }
    r1 = client.post("/api/payments/send", json=payload)
    assert r1.status_code == 200, r1.text
    ids1 = r1.json()["payment_ids"]
    r2 = client.post("/api/payments/send", json=payload)  # тот же ключ
    assert r2.status_code == 200, r2.text
    assert r2.json()["payment_ids"] == ids1  # тот же результат, не новый набор

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT COUNT(*) FROM payments WHERE user_id=?"), (703,))
        count = cur.fetchone()[0]
    assert count == 1  # ровно один платёж, дубля нет
    assert len(sent) == 1  # уведомление ушло один раз
