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
                "INSERT INTO cash_deposits (manager_id, amount, amount_cents, deposited_at, "
                "status, created_at) VALUES (?, ?, ?, ?, 'confirmed', ?)"
            ),
            (uid, amount, int(round(amount * 100)), created_at, created_at),
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
