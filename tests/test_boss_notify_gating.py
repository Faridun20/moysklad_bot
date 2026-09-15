"""
Каждая точка входа, которая раньше пушила боссу карточку по платежу/сдаче/
возврату, теперь проверяет `services.notify_policy.should_notify_now` ПЕРЕД
отправкой: ниже `boss_instant_threshold_usd` — карточка не уходит (событие
остаётся pending и попадёт в вечерний дайджест), выше/равно — уходит как
раньше. Заявки на отгрузку (`ORDER_REQUEST`) режиму не подчиняются —
проверяем, что они всё равно уходят немедленно.

Реальная БД (isolated_db); граница с Telegram мокается.
"""

import asyncio

TOKEN = "123456:AAH-secret-bot-token"


def _run(coro):
    return asyncio.run(coro)


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(chat_id)


# ─── services/notify.py ─────────────────────────────────────────────────────


def test_notify_payment_sent_skips_below_threshold(isolated_db, monkeypatch):
    import services.notify as notify
    import services.notifier as notifier

    isolated_db.set_role(1, "b", "Boss", "boss")
    monkeypatch.setattr(notifier, "get_notify_recipients", lambda: [1])
    bot = _Bot()

    _run(notify.notify_payment_sent(bot, 1, "Иван", "@ivan", 100.0, "USD", "c", confirm_keyboard=None))
    assert bot.sent == []


def test_notify_payment_sent_fires_at_or_above_threshold(isolated_db, monkeypatch):
    import services.notify as notify
    import services.notifier as notifier

    isolated_db.set_role(1, "b", "Boss", "boss")
    monkeypatch.setattr(notifier, "get_notify_recipients", lambda: [1])
    bot = _Bot()

    _run(notify.notify_payment_sent(bot, 1, "Иван", "@ivan", 6000.0, "USD", "c", confirm_keyboard=None))
    assert bot.sent == [1]


def test_notify_shipment_request_always_fires(isolated_db, monkeypatch):
    """Заявка блокирует работу менеджера — порогом не режется."""
    import services.notify as notify
    import services.notifier as notifier

    isolated_db.set_role(1, "b", "Boss", "boss")
    monkeypatch.setattr(notifier, "get_notify_recipients", lambda: [1])
    bot = _Bot()

    _run(notify.notify_shipment_request(bot, "Заявка #1", 1, approve_keyboard=None))
    assert bot.sent == [1]


# ─── handlers/returns.py, handlers/deposits.py ──────────────────────────────


def _shipped_order_with_return(db, mgr, total_per_unit=100.0, qty=2):
    oid = db.create_order(mgr, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", qty, "шт", total_per_unit)
    db.update_order_status(oid, "shipped")
    items = _run(db.get_order_items(oid))
    r = _run(db.create_return(
        oid, "full", "брак", [(items[0]["id"], qty, total_per_unit * qty)],
        refund_method="no_refund", created_by=mgr,
    ))
    return r["return_id"]


def test_return_confirmers_below_threshold_warehouse_notified_boss_not(isolated_db):
    """«Товар получен» — физическая приёмка, не зависит от суммы: склад
    получает карточку ВСЕГДА. Ниже порога — боссу пуш не идёт (в дайджест)."""
    from handlers.returns import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(2, "m", "Manager", "manager")
    db.set_role(3, "w", "Keeper", "warehouse_keeper")
    ret_id = _shipped_order_with_return(db, 2, total_per_unit=100.0, qty=2)  # $200

    bot = _Bot()
    _run(_notify_confirmers(bot, ret_id, 1, 200.0, "no_refund"))
    assert bot.sent == [3]


def test_return_confirmers_below_threshold_manager_fallback_notified_boss_not(isolated_db):
    """Кладовщика нет — менеджер замещает его (ROLE_ALSO_ACTS_AS) и получает
    карточку всегда; боссу ниже порога она не идёт."""
    from handlers.returns import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(2, "m", "Manager", "manager")
    ret_id = _shipped_order_with_return(db, 2, total_per_unit=100.0, qty=2)  # $200

    bot = _Bot()
    _run(_notify_confirmers(bot, ret_id, 1, 200.0, "no_refund"))
    assert bot.sent == [2]


def test_return_confirmers_fires_at_or_above_threshold(isolated_db):
    from handlers.returns import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(2, "m", "Manager", "manager")
    ret_id = _shipped_order_with_return(db, 2, total_per_unit=3000.0, qty=2)  # $6000

    bot = _Bot()
    _run(_notify_confirmers(bot, ret_id, 1, 6000.0, "no_refund"))
    assert sorted(bot.sent) == [1, 2]


def test_return_confirmers_uses_order_currency_for_threshold(isolated_db):
    """Сумма возврата отображается и сравнивается с порогом в валюте ЗАКАЗА,
    а не всегда как USD (аудит, финдинг #5): хардкод "USD" считал 6000 сум
    как $6000 (порог $5000 — «сразу»), хотя реально это ~$0.47 — боссу
    улетала мгновенная карточка по возврату на копейки, вместо того чтобы
    тихо остаться pending и попасть в вечерний дайджест."""
    from handlers.returns import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(2, "m", "Manager", "manager")
    assert db.set_currency_rate("UZS", 1 / 12700, updated_by=1)[0]

    oid = db.create_order(2, "Manager", "")
    assert db.update_order_currency(oid, "UZS", require_draft=True)
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 6000.0)
    db.update_order_status(oid, "shipped")
    items = _run(db.get_order_items(oid))
    r = _run(db.create_return(
        oid, "full", "брак", [(items[0]["id"], 1, 6000.0)],
        refund_method="no_refund", created_by=2,
    ))

    bot = _Bot()
    _run(_notify_confirmers(bot, r["return_id"], oid, 6000.0, "no_refund"))
    # 6000 сум ≈ $0.47 — далеко ниже порога: боссу пуш НЕ идёт, только
    # менеджер-заместитель кладовщика (кладовщика нет — ROLE_ALSO_ACTS_AS).
    assert bot.sent == [2]


def test_return_confirmers_no_recipients_sends_nothing(isolated_db):
    """Нет ни склада, ни его заместителя, сумма ниже порога — рассылать
    некому: функция не падает и ничего не шлёт."""
    from handlers.returns import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    ret_id = _shipped_order_with_return(db, 1, total_per_unit=100.0, qty=2)  # $200

    bot = _Bot()
    _run(_notify_confirmers(bot, ret_id, 1, 200.0, "no_refund"))
    assert bot.sent == []


def test_deposit_confirmers_below_threshold_bookkeeper_notified_boss_not(isolated_db):
    """Бухгалтер сверяет кассу каждый день — получает карточку ВСЕГДА.
    Ниже порога боссу она не идёт (в дайджест)."""
    from handlers.deposits import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(5, "k", "Book", "bookkeeper")

    bot = _Bot()
    _run(_notify_confirmers(bot, 1, "Manager", 300.0, currency="USD"))
    assert bot.sent == [5]


def test_deposit_confirmers_below_threshold_manager_fallback_notified_boss_not(isolated_db):
    """Бухгалтера нет — менеджер замещает его и получает карточку всегда;
    боссу ниже порога она не идёт."""
    from handlers.deposits import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(10, "m", "Manager", "manager")

    bot = _Bot()
    _run(_notify_confirmers(bot, 1, "Manager", 300.0, currency="USD"))
    assert bot.sent == [10]


def test_deposit_confirmers_no_recipients_sends_nothing(isolated_db):
    from handlers.deposits import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")

    bot = _Bot()
    _run(_notify_confirmers(bot, 1, "Manager", 300.0, currency="USD"))
    assert bot.sent == []


def test_deposit_confirmers_fires_at_or_above_threshold(isolated_db):
    from handlers.deposits import _notify_confirmers

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    # Бухгалтера нет — менеджер замещает его (services.roles.ROLE_ALSO_ACTS_AS),
    # поэтому получает карточку сдачи наравне с боссом.
    db.set_role(10, "m", "Manager", "manager")

    bot = _Bot()
    _run(_notify_confirmers(bot, 1, "Manager", 7000.0, currency="USD"))
    assert sorted(bot.sent) == [1, 10]


def test_deposit_confirmers_uses_currency_for_threshold(isolated_db):
    """7 000 000 UZS по курсу 1 USD=12 500 UZS ≈ $560 — ниже порога."""
    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    ok, err = db.set_currency_rate("UZS", 1 / 12_500, updated_by=1)
    assert ok, err
    from handlers.deposits import _notify_confirmers

    bot = _Bot()
    _run(_notify_confirmers(bot, 1, "Manager", 7_000_000.0, currency="UZS"))
    assert bot.sent == []


# ─── webapp/server.py ────────────────────────────────────────────────────────


def test_notify_batch_payments_sends_only_lines_at_or_above_threshold(isolated_db, monkeypatch):
    import webapp.server as server

    isolated_db.set_role(1, "b", "Boss", "boss")
    sent = []

    async def _recips():
        return [1]

    async def _send(uid, text, **kw):
        sent.append((uid, text))

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)

    created = [(1, 100.0, "USD"), (2, 6000.0, "USD")]
    _run(server._notify_batch_payments("Иван", "@ivan", "аренда", created))

    assert len(sent) == 1
    assert "6,000" in sent[0][1] and "100" not in sent[0][1]


def test_notify_batch_payments_sends_nothing_when_all_below_threshold(isolated_db, monkeypatch):
    import webapp.server as server

    isolated_db.set_role(1, "b", "Boss", "boss")
    sent = []

    async def _recips():
        return [1]

    async def _send(uid, text, **kw):
        sent.append((uid, text))

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)

    _run(server._notify_batch_payments("Иван", "@ivan", "аренда", [(1, 100.0, "USD")]))
    assert sent == []


def test_api_payments_send_skips_push_below_threshold(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient

    import services.notifier as notifier
    import webapp.server as server

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(50, "m", "Manager", "manager")
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "M"})
    sent = []

    async def _recips():
        return [1]

    async def _send(uid, text, **kw):
        sent.append((uid, text))

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)

    client = TestClient(server.app)
    r = client.post(
        "/api/payments/send",
        json={"initData": "50", "amount": 100, "currency": "USD", "comment": "аренда"},
    )
    assert r.status_code == 200, r.text
    assert sent == []
    # Платёж всё равно создан pending — не потерян, просто без немедленного пуша.
    payment_id = r.json()["payment_id"]
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT status FROM payments WHERE id = ?"), (payment_id,))
        assert cur.fetchone()[0] == "pending"


def test_api_payments_send_fires_push_at_or_above_threshold(isolated_db, monkeypatch):
    from fastapi.testclient import TestClient

    import services.notifier as notifier
    import webapp.server as server

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(50, "m", "Manager", "manager")
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "M"})
    sent = []

    async def _recips():
        return [1]

    async def _send(uid, text, **kw):
        sent.append((uid, text))

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)

    client = TestClient(server.app)
    r = client.post(
        "/api/payments/send",
        json={"initData": "50", "amount": 9000, "currency": "USD", "comment": "аренда"},
    )
    assert r.status_code == 200, r.text
    assert len(sent) == 1


def test_api_submit_order_always_fires_regardless_of_threshold(isolated_db, monkeypatch):
    """Заявка блокирует работу менеджера — порогом не режется, даже с
    boss_instant_threshold_usd искусственно завышенным."""
    from fastapi.testclient import TestClient

    import services.notifier as notifier
    import webapp.server as server

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_role(50, "m", "Manager", "manager")
    db.set_setting("boss_instant_threshold_usd", 10_000_000)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "M"})
    sent = []

    async def _recips():
        return [1]

    async def _send(uid, text, **kw):
        sent.append((uid, text))

    monkeypatch.setattr(notifier, "aget_notify_recipients", _recips)
    monkeypatch.setattr(notifier, "tg_send_message", _send)

    client = TestClient(server.app)
    oid = db.create_order(50, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 10.0)
    r = client.post("/api/orders/submit", json={"initData": "50", "order_id": oid, "payment_type": "paid"})
    assert r.status_code == 200, r.text
    assert len(sent) == 1
