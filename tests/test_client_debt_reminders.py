"""
Тесты B3 — прямое напоминание клиенту-должнику в Telegram
(`services/client_debt_reminders.py`).

Мокаем ГРАНИЦУ (`services.notifier.tg_send_message` — сама она протестирована
на транспорте в `tests/test_notifier_send.py`), а не пересчитываем остаток —
берём его напрямую из `services.debts.calc_order_balances`, как это делает
`tasks/run_debts_notify.main()`.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from services import client_debt_reminders as cdr
from services.debts import calc_order_balances


def _make_overdue_order(db, *, telegram_id: int | None = 555, due_date="2020-01-01") -> tuple[int, int]:
    """Контрагент (опц. с telegram_id) + отгруженный credit-заказ, просроченный.

    Возвращает (order_id, counterparty_id).
    """
    from services import counterparties

    cp = asyncio.run(counterparties.create("Клиент Тест"))
    cp_id = int(cp["counterparty_id"])
    if telegram_id is not None:
        asyncio.run(counterparties.set_telegram_id(cp_id, telegram_id))

    db.set_role(1, "mgr", "Manager", "manager")
    oid = db.create_order(1, "Manager", "")
    db.add_order_item(oid, "Товар", "href", 1, "шт", 1000.0)
    db.update_order_agent(oid, str(cp_id), "Клиент Тест")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "UPDATE orders SET payment_type='credit', currency='USD', due_date=? WHERE id=?"
            ),
            (due_date, oid),
        )
        conn.commit()
    db.update_order_status(oid, "shipped")
    return oid, cp_id


async def _overdue_and_balances(db, oid):
    debts = await db.get_open_debts()
    debts = [d for d in debts if int(d["id"]) == oid]
    balances = await calc_order_balances([oid])
    return debts, balances


def _enable(db, *, reminders=True, notifications=True):
    db.set_setting("client_debt_reminders_enabled", reminders)
    db.set_setting("client_notifications_enabled", notifications)


def test_message_contains_amount_order_and_due_date(isolated_db, monkeypatch):
    db = isolated_db
    oid, _cp_id = _make_overdue_order(db, due_date="2020-03-15")
    _enable(db)

    sent_texts = []

    async def _fake_send(chat_id, text, **kw):
        sent_texts.append((chat_id, text, kw))
        return True

    monkeypatch.setattr(cdr, "tg_send_message", _fake_send)

    async def scenario():
        debts, balances = await _overdue_and_balances(db, oid)
        return await cdr.send_overdue_client_reminders(debts, balances, "2026-09-15")

    sent = asyncio.run(scenario())
    assert sent == 1
    assert len(sent_texts) == 1
    chat_id, text, kw = sent_texts[0]
    assert chat_id == 555
    assert f"№{oid}" in text
    assert "1 000" in text or "1000" in text  # сумма долга
    assert "15.03.2020" in text  # срок оплаты по-русски
    # Никаких кнопок и ссылок в WebApp — сообщение чисто информационное.
    assert kw.get("reply_markup") is None
    assert "startapp" not in text and "web_app" not in text.lower()


def test_opt_in_default_off_does_not_send(isolated_db, monkeypatch):
    db = isolated_db
    oid, _ = _make_overdue_order(db)
    # НЕ включаем client_debt_reminders_enabled — дефолт False.

    called = False

    async def _fake_send(chat_id, text, **kw):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cdr, "tg_send_message", _fake_send)

    async def scenario():
        debts, balances = await _overdue_and_balances(db, oid)
        return await cdr.send_overdue_client_reminders(debts, balances, "2026-09-15")

    sent = asyncio.run(scenario())
    assert sent == 0
    assert called is False


def test_respects_global_client_notifications_toggle(isolated_db, monkeypatch):
    db = isolated_db
    oid, _ = _make_overdue_order(db)
    _enable(db, reminders=True, notifications=False)

    called = False

    async def _fake_send(chat_id, text, **kw):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cdr, "tg_send_message", _fake_send)

    async def scenario():
        debts, balances = await _overdue_and_balances(db, oid)
        return await cdr.send_overdue_client_reminders(debts, balances, "2026-09-15")

    sent = asyncio.run(scenario())
    assert sent == 0
    assert called is False


def test_no_telegram_id_no_send(isolated_db, monkeypatch):
    db = isolated_db
    oid, _ = _make_overdue_order(db, telegram_id=None)
    _enable(db)

    called = False

    async def _fake_send(chat_id, text, **kw):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cdr, "tg_send_message", _fake_send)

    async def scenario():
        debts, balances = await _overdue_and_balances(db, oid)
        return await cdr.send_overdue_client_reminders(debts, balances, "2026-09-15")

    sent = asyncio.run(scenario())
    assert sent == 0
    assert called is False


def test_rate_limited_to_once_per_day(isolated_db, monkeypatch):
    db = isolated_db
    oid, _ = _make_overdue_order(db)
    _enable(db)

    calls = []

    async def _fake_send(chat_id, text, **kw):
        calls.append(chat_id)
        return True

    monkeypatch.setattr(cdr, "tg_send_message", _fake_send)

    async def scenario():
        debts, balances = await _overdue_and_balances(db, oid)
        first = await cdr.send_overdue_client_reminders(debts, balances, "2026-09-15")
        second = await cdr.send_overdue_client_reminders(debts, balances, "2026-09-15")
        return first, second

    first, second = asyncio.run(scenario())
    assert first == 1
    assert second == 0  # тот же день — уже отправлено
    assert len(calls) == 1


def test_writes_audit_log_on_send(isolated_db, monkeypatch):
    db = isolated_db
    oid, _ = _make_overdue_order(db)
    _enable(db)

    async def _fake_send(chat_id, text, **kw):
        return True

    monkeypatch.setattr(cdr, "tg_send_message", _fake_send)

    async def scenario():
        debts, balances = await _overdue_and_balances(db, oid)
        return await cdr.send_overdue_client_reminders(debts, balances, "2026-09-15")

    asyncio.run(scenario())

    rows = asyncio.run(db.get_audit_log(limit=20))
    matches = [r for r in rows if r["action"] == "client_debt_reminder_sent"]
    assert len(matches) == 1
    assert str(oid) in matches[0]["details"]


# ─── /api/settings/client_debt_reminders (WebApp-ручка переключателя) ────────


def test_settings_endpoint_toggles_and_reaches_me(isolated_db, monkeypatch):
    """Регресс-гард: этот путь исполняет `get_setting`/`set_setting` внутри
    самой ручки (а не только в сервисе) — юнит-тесты сервиса его не покрывают,
    и именно здесь однажды была опечатка в импорте (`get_setting` не
    импортирован), которую поймал только mypy, а не pytest."""
    import importlib

    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    db = isolated_db
    db.set_role(100, "boss", "Boss", "boss")
    db.set_role(200, "mgr", "Manager", "manager")
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)

    me = client.post("/api/me", json={"initData": "200"}).json()
    assert me["client_debt_reminders_enabled"] is False  # default off

    r = client.post(
        "/api/settings/client_debt_reminders",
        json={"initData": "100", "enabled": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["client_debt_reminders_enabled"] is True

    me = client.post("/api/me", json={"initData": "200"}).json()
    assert me["client_debt_reminders_enabled"] is True

    # Менеджеру ручка недоступна.
    forbidden = client.post(
        "/api/settings/client_debt_reminders",
        json={"initData": "200", "enabled": False},
    )
    assert forbidden.status_code == 403
