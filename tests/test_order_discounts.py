"""
Скидка к прайсу по обычному заказу (C2/C5): расчёт, порог, карточки, гейт
одобрения и то, что видит менеджер.

Границы системы мокаются как обычно (Telegram-бот, проверка initData), БД,
роли и переходы статусов — настоящие (`isolated_db`).

Про сид позиций. Заказы здесь собираются СЕРВИСОМ (`db.add_order_item`), а не
через `/api/orders/add_item`: ручка держит `product_prices.sale_price` жёстким
минимумом («Цена ниже минимальной», tests/test_product_prices.py), то есть
через неё позиция дешевле прайса просто не заводится. Скидка по живой базе
берётся из других источников — прайс подняли ПОСЛЕ заказа, позиция приехала
миграцией, цену задали позже; такой случай проверяется отдельно
(`test_discount_appears_when_price_list_is_raised_later`).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from services import order_discounts as od


# ─── Чистый расчёт ───────────────────────────────────────────────────────────


def _items(*rows):
    """rows: (price_cents, qty, product_id)."""
    return [
        {"id": i + 1, "product_name": f"Товар {i + 1}", "price_cents": p,
         "quantity": q, "product_id": pid}
        for i, (p, q, pid) in enumerate(rows)
    ]


def _prices(**by_id):
    """by_id: product_id → (sale_price_cents, currency)."""
    return {k: {"sale_price_cents": v[0], "currency": v[1]} for k, v in by_id.items()}


def test_line_pct_basic_and_edges():
    assert od.line_pct(2_400_000, 2_500_000) == 4.0
    assert od.line_pct(7_000, 10_000) == 30.0
    # Дороже прайса — отрицательная скидка, а не ноль.
    assert od.line_pct(11_000, 10_000) == -10.0
    # Сравнивать не с чем: прайса нет / он нулевой / цены нет.
    assert od.line_pct(1_000, None) is None
    assert od.line_pct(1_000, 0) is None
    assert od.line_pct(None, 10_000) is None


def test_no_reference_price_is_dash_not_zero():
    """Новый товар без прайса: строка без скидки, а не «скидка 0%»."""
    summary = od.summarize(_items((10_000, 1, 7)), {}, "USD", threshold=15)
    line = summary["lines"][0]
    assert line["discount_pct"] is None and line["ref_price_cents"] is None
    assert od.line_label(line, "USD") == ""
    assert summary["avg_pct"] is None
    assert summary["covered_lines"] == 0 and summary["total_lines"] == 1
    assert summary["flagged"] is False
    assert od.summary_label(summary) == ""


def test_price_in_other_currency_is_not_a_reference():
    """Прайс в UZS против заказа в USD: курс тут не помощник — скидки нет."""
    summary = od.summarize(
        _items((10_000, 1, 7)), _prices(**{"7": (100_000_000, "UZS")}), "USD", threshold=15
    )
    assert summary["lines"][0]["ref_price_cents"] is None
    assert summary["covered_lines"] == 0


def test_weighted_average_is_by_money_not_by_lines():
    """Дорогая позиция со скидкой 30% и грошовая без скидки — это ~29.7%,
    а не среднее арифметическое 15%."""
    items = _items((700_000, 1, 1), (10_000, 1, 2))
    prices = _prices(**{"1": (1_000_000, "USD"), "2": (10_000, "USD")})
    summary = od.summarize(items, prices, "USD", threshold=15)
    assert [ln["discount_pct"] for ln in summary["lines"]] == [30.0, 0.0]
    assert summary["avg_pct"] == pytest.approx(29.7, abs=0.05)
    assert summary["max_pct"] == 30.0
    assert summary["covered_lines"] == 2


def test_quantity_weights_the_average():
    """Вес строки — деньги прайса: количество считается."""
    items = _items((900, 10, 1), (500, 1, 2))
    prices = _prices(**{"1": (1_000, "USD"), "2": (1_000, "USD")})
    summary = od.summarize(items, prices, "USD", threshold=60)
    # прайс: 10×1000 + 1×1000 = 11 000; факт: 10×900 + 500 = 9 500 → 13.6%
    assert summary["avg_pct"] == pytest.approx(13.6, abs=0.05)
    assert summary["max_pct"] == 50.0
    assert summary["flagged"] is False, "ни строка, ни средняя порога не достали"


def test_single_line_over_threshold_flags_the_whole_order():
    items = _items((900, 10, 1), (500, 1, 2))
    prices = _prices(**{"1": (1_000, "USD"), "2": (1_000, "USD")})
    summary = od.summarize(items, prices, "USD", threshold=40)
    assert summary["avg_pct"] == pytest.approx(13.6, abs=0.05)
    assert summary["flagged"] is True, "порог пробила одна позиция — этого хватает"


def test_average_over_threshold_flags_even_without_a_single_bad_line():
    items = _items((880, 1, 1), (880, 1, 2))
    prices = _prices(**{"1": (1_000, "USD"), "2": (1_000, "USD")})
    summary = od.summarize(items, prices, "USD", threshold=12)
    assert summary["max_pct"] == 12.0 and summary["avg_pct"] == 12.0
    assert summary["flagged"] is True


def test_threshold_is_inclusive():
    prices = _prices(**{"1": (1_000, "USD")})
    assert od.summarize(_items((900, 1, 1)), prices, "USD", threshold=15)["flagged"] is False
    assert od.summarize(_items((850, 1, 1)), prices, "USD", threshold=15)["flagged"] is True


def test_threshold_zero_disables_flagging():
    prices = _prices(**{"1": (1_000, "USD")})
    summary = od.summarize(_items((100, 1, 1)), prices, "USD", threshold=0)
    assert summary["max_pct"] == 90.0
    assert summary["flagged"] is False
    assert od.pending_note(summary) == "" and od.flag_label(summary) == ""


def test_labels_read_like_the_machine_card():
    prices = _prices(**{"1": (2_500_000, "USD")})
    summary = od.summarize(_items((2_400_000, 1, 1)), prices, "USD", threshold=15)
    assert od.line_label(summary["lines"][0], "USD") == "прайс 25 000 USD · скидка 4%"
    assert od.summary_label(summary) == "Скидка по заказу: скидка 4%"
    assert od.flag_label(summary) == ""
    assert od.pct_label(None) == "—"
    assert od.pct_label(-4.0) == "выше прайса на 4%"
    assert od.pct_label(0) == "по прайсу"


def test_flag_and_pending_note_texts():
    prices = _prices(**{"1": (1_000, "USD")})
    summary = od.summarize(_items((700, 1, 1)), prices, "USD", threshold=15)
    assert "30%" in od.flag_label(summary) and "15%" in od.flag_label(summary)
    note = od.pending_note(summary)
    assert note.startswith("Ждёт одобрения из-за скидки 30%")
    refusal = od.refusal(summary, 42)
    assert refusal["code"] == "discount_approval_required"
    assert refusal["needs_discount_ack"] is True and refusal["ok"] is False
    assert "#42" in refusal["error"]


def test_summary_label_counts_lines_without_price():
    prices = _prices(**{"1": (1_000, "USD")})
    summary = od.summarize(_items((900, 1, 1), (500, 1, 2)), prices, "USD", threshold=15)
    assert "без прайса: 1" in od.summary_label(summary)


def test_threshold_from_settings_and_garbage(isolated_db):
    db = isolated_db
    assert od.threshold_pct() == 15.0, "дефолт из _DEFAULT_SETTINGS"
    db.set_setting("order_discount_requires_approval_pct", 30)
    assert od.threshold_pct() == 30.0
    db.set_setting("order_discount_requires_approval_pct", "не число")
    assert od.threshold_pct() == od.DEFAULT_THRESHOLD_PCT


# ─── Ручки, карточки и гейт одобрения ───────────────────────────────────────


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, *a, **k):
        self.sent.append(("message", a, k))

    async def send_document(self, *a, **k):
        self.sent.append(("document", a, k))


def _order(order_id: int) -> dict:
    from services import async_db as adb

    return asyncio.run(adb.get_order(order_id))


def _product(name: str) -> int:
    from services import container_receipt

    return asyncio.run(container_receipt.create_product(name))["product_id"]


@pytest.fixture
def env(isolated_db, monkeypatch):
    """Босс, менеджер, товар с прайсом 100 USD и заказ по 70 USD (скидка 30%)."""
    import importlib
    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    # Счётчик `/api/orders/submit` живёт в процессе: без сброса тесты этого
    # модуля упираются в 429 друг из-за друга, а не из-за проверяемого.
    rate_limit.reset()
    db = isolated_db

    boss_id, mgr_id = 100, 200
    db.set_role(boss_id, "boss_user", "Boss", "boss")
    db.set_role(mgr_id, "mgr_user", "Manager", "manager")

    pid = _product("Кабель ВВГ")
    db.set_product_price(str(pid), "Кабель ВВГ", 100.0, 60.0, "USD", updated_by=boss_id)

    order_id = db.create_order(mgr_id, "Manager", "")
    db.update_order_agent(order_id, "agent-1", "ООО Ромашка")
    db.add_order_item(order_id, "Кабель ВВГ", "", 3, "м", 70.0, product_id=pid)
    db.update_order_currency(order_id, "USD")

    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    client = TestClient(server.app)
    ids = {"boss": boss_id, "mgr": mgr_id, "order": order_id, "product": pid}
    return client, db, ids, fake_bot


def _submit(client, ids, order_id=None, payment_type="credit"):
    resp = client.post(
        "/api/orders/submit",
        json={
            "initData": str(ids["mgr"]),
            "order_id": order_id or ids["order"],
            "payment_type": payment_type,
            "due_date": "2030-01-15" if payment_type == "credit" else None,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_submit_tells_manager_the_order_waits_for_the_discount(env):
    client, db, ids, _bot = env
    body = _submit(client, ids)
    assert body["discount"]["max_pct"] == 30.0
    assert body["discount"]["flagged"] is True
    assert body["discount_note"].startswith("Ждёт одобрения из-за скидки 30%")


def test_requests_card_shows_price_and_discount_per_line(env):
    client, db, ids, _bot = env
    _submit(client, ids)
    resp = client.post("/api/orders/requests", json={"initData": str(ids["boss"])})
    assert resp.status_code == 200, resp.text
    req = resp.json()["requests"][0]
    assert req["discount"]["avg_pct"] == 30.0 and req["discount"]["flagged"] is True
    assert req["discount"]["threshold_pct"] == 15
    assert req["items"][0]["ref_price"] == 100.0
    assert req["items"][0]["discount_pct"] == 30.0


def test_boss_card_in_bot_carries_the_discount(env):
    """Карточка решения в боте — тот же вид, что у сделки по технике."""
    from handlers.orders import format_request_notify
    from services import async_db as adb

    client, db, ids, _bot = env
    order = asyncio.run(adb.get_order(ids["order"]))
    items = asyncio.run(adb.get_order_items(ids["order"]))
    summary = asyncio.run(od.order_discount(items, "USD"))
    card = format_request_notify(order, items, 1, summary)
    assert "прайс 100 USD · скидка 30%" in card
    assert "Скидка по заказу: скидка 30%" in card
    assert "порог 15%" in card
    # Без сводки карточка остаётся прежней — старый вызов из трёх аргументов.
    assert "скидка" not in format_request_notify(order, items, 1)


def test_approve_requires_explicit_ack_and_then_passes(env):
    client, db, ids, _bot = env
    body = _submit(client, ids)
    req_id = body["req_id"]

    first = client.post(
        "/api/requests/approve", json={"initData": str(ids["boss"]), "req_id": req_id}
    )
    assert first.status_code == 200, first.text
    assert first.json()["needs_discount_ack"] is True
    assert first.json()["discount"]["max_pct"] == 30.0
    assert _order(ids["order"])["status"] == "pending", "без подтверждения заявка не одобряется"

    second = client.post(
        "/api/requests/approve",
        json={"initData": str(ids["boss"]), "req_id": req_id, "discount_ack": True},
    )
    assert second.status_code == 200, second.text
    assert second.json()["ok"] is True
    assert _order(ids["order"])["status"] == "approved"
    actions = [r["action"] for r in asyncio.run(db.get_audit_log(50))]
    assert "discount_approved" in actions, "решение по скидке остаётся в журнале"


def test_discount_below_threshold_approves_in_one_tap(env):
    client, db, ids, _bot = env
    db.set_setting("order_discount_requires_approval_pct", 40)
    body = _submit(client, ids)
    resp = client.post(
        "/api/requests/approve", json={"initData": str(ids["boss"]), "req_id": body["req_id"]}
    )
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert _order(ids["order"])["status"] == "approved"


def test_order_without_reference_price_is_not_flagged(env):
    """Товар без прайса нельзя объявить скидкой: заявка проходит как раньше."""
    client, db, ids, _bot = env
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, "agent-1", "ООО Ромашка")
    db.add_order_item(oid, "Новый товар", "", 1, "шт", 1.0)
    body = _submit(client, ids, order_id=oid, payment_type="paid")
    assert body["discount"]["flagged"] is False
    assert body["discount_note"] == ""
    ok = client.post(
        "/api/requests/approve",
        json={"initData": str(ids["boss"]), "req_id": body["req_id"]},
    )
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_discount_appears_when_price_list_is_raised_later(env):
    """Как скидка возникает на живой базе: позиция заведена по 100 (ручка
    минимум держит), прайс подняли до 200 — заявка ждёт решения."""
    client, db, ids, _bot = env
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, "agent-1", "ООО Ромашка")
    pid = _product("Труба")
    db.set_product_price(str(pid), "Труба", 100.0, None, "USD", updated_by=ids["boss"])
    resp = client.post(
        "/api/orders/add_item",
        json={"initData": str(ids["mgr"]), "order_id": oid, "product_name": "Труба",
              "product_id": pid, "quantity": 1, "price": 100, "currency": "USD"},
    )
    assert resp.status_code == 200, resp.text
    db.set_product_price(str(pid), "Труба", 200.0, None, "USD", updated_by=ids["boss"])
    body = _submit(client, ids, order_id=oid, payment_type="paid")
    assert body["discount"]["max_pct"] == 50.0 and body["discount"]["flagged"] is True


def test_manager_cannot_wave_the_flag_through(env):
    """`discount_ack` — не волшебное слово: ручку одобрения держит роль."""
    client, db, ids, _bot = env
    body = _submit(client, ids)
    resp = client.post(
        "/api/requests/approve",
        json={"initData": str(ids["mgr"]), "req_id": body["req_id"], "discount_ack": True},
    )
    assert resp.status_code == 403
    assert _order(ids["order"])["status"] == "pending"


def test_manager_sees_the_reason_on_his_order(env):
    client, db, ids, _bot = env
    _submit(client, ids)
    resp = client.post("/api/orders", json={"initData": str(ids["mgr"])})
    assert resp.status_code == 200, resp.text
    order = [o for o in resp.json()["orders"] if o["id"] == ids["order"]][0]
    assert order["discount"]["flagged"] is True
    assert order["discount_note"].startswith("Ждёт одобрения из-за скидки 30%")
    assert order["items"][0]["ref_price"] == 100.0
    assert order["items"][0]["discount_pct"] == 30.0
    # Себестоимость менеджеру по-прежнему не видна.
    assert "profit" not in order and "cost_price" not in str(order["items"])


def test_note_disappears_after_approval(env):
    client, db, ids, _bot = env
    body = _submit(client, ids)
    client.post(
        "/api/requests/approve",
        json={"initData": str(ids["boss"]), "req_id": body["req_id"], "discount_ack": True},
    )
    resp = client.post("/api/orders", json={"initData": str(ids["mgr"])})
    order = [o for o in resp.json()["orders"] if o["id"] == ids["order"]][0]
    assert order["discount"]["flagged"] is True, "скидка видна и после одобрения"
    assert order["discount_note"] == "", "но заявка больше ничего не ждёт"


# ─── Два порога сразу: кредит-лимит и скидка ────────────────────────────────


def test_credit_limit_is_asked_first_then_the_discount(env):
    """Заявка пробивает ОБА порога: сначала вопрос про лимит, потом про скидку,
    и только флаги вместе одобряют. Раздельные подтверждения гасили бы друг
    друга — второе нажатие без `override` снова упёрлось бы в лимит."""
    from services.order_workflow import approve_shipment_request

    client, db, ids, bot = env
    # Кредит-заказ выше дефолтного лимита (2000) и со скидкой 30%.
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, "agent-limit", "ООО Должник")
    db.add_order_item(oid, "Кабель ВВГ", "", 50, "м", 70.0, product_id=ids["product"])
    db.update_order_currency(oid, "USD")
    body = _submit(client, ids, order_id=oid, payment_type="credit")
    req_id = body["req_id"]

    first = asyncio.run(approve_shipment_request(req_id, ids["boss"], "Boss", bot))
    assert first["needs_override"] is True and "needs_discount_ack" not in first

    second = asyncio.run(
        approve_shipment_request(req_id, ids["boss"], "Boss", bot, override=True)
    )
    assert second["needs_discount_ack"] is True
    assert _order(oid)["status"] == "pending", "два порога — заявка всё ещё ждёт"

    third = asyncio.run(
        approve_shipment_request(
            req_id, ids["boss"], "Boss", bot, override=True, discount_ack=True
        )
    )
    assert third["ok"] is True
    assert _order(oid)["status"] == "approved"
    actions = [r["action"] for r in asyncio.run(db.get_audit_log(50))]
    assert "credit_override" in actions and "discount_approved" in actions


# ─── Карточка решения в боте: кнопка «Одобрить со скидкой» ──────────────────


class _FakeUser:
    def __init__(self, uid: int, full_name: str = "Boss"):
        self.id = uid
        self.full_name = full_name
        self.username = full_name.lower()


class _FakeChat:
    id = 55


class _FakeBotMessage:
    def __init__(self, text: str, uid: int, bot):
        self.text = text
        self.html_text = text
        self.from_user = _FakeUser(uid)
        self.chat = _FakeChat()
        self.message_id = 9
        self.bot = bot
        self.reply_markup = None
        self.answers: list[tuple] = []
        self.edited: str | None = None

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return _FakeBotMessage(text, self.from_user.id, self.bot)

    async def edit_text(self, text, **kwargs):
        self.edited = text
        self.reply_markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, reply_markup=None):
        self.reply_markup = reply_markup


class _FakeCall:
    def __init__(self, data: str, uid: int, bot, text: str = "🔔 Новая заявка на отгрузку #1"):
        self.data = data
        self.from_user = _FakeUser(uid)
        self.message = _FakeBotMessage(text, uid, bot)
        self.alerts: list[tuple] = []

    async def answer(self, text: str = "", **kwargs):
        self.alerts.append((text, kwargs))


def _kb_callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_bot_offers_the_discount_button_and_the_second_tap_approves(env):
    from handlers.orders import cb_approve_request, cb_approve_request_discount

    client, db, ids, bot = env
    req_id = _submit(client, ids)["req_id"]

    call = _FakeCall(f"req_ok:{req_id}", ids["boss"], bot)
    asyncio.run(cb_approve_request(call, bot))
    assert _order(ids["order"])["status"] == "pending", "первый тап не одобряет"
    assert any("Скидка выше порога" in a[0] for a in call.alerts)
    text, kwargs = call.message.answers[-1]
    assert "Скидка по заказу: скидка 30%" in text and "порог 15%" in text
    assert _kb_callbacks(kwargs["reply_markup"]) == [f"req_dsc:{req_id}"]

    ack = _FakeCall(f"req_dsc:{req_id}", ids["boss"], bot)
    asyncio.run(cb_approve_request_discount(ack, bot))
    assert _order(ids["order"])["status"] == "approved"
    assert "со скидкой" in (ack.message.edited or "")


def test_bot_discount_button_is_boss_only(env):
    """`req_dsc:` — та же роль, что у «Одобрить»: менеджер мимо неё не пройдёт."""
    from handlers.orders import cb_approve_request_discount

    client, db, ids, bot = env
    req_id = _submit(client, ids)["req_id"]

    call = _FakeCall(f"req_dsc:{req_id}", ids["mgr"], bot)
    asyncio.run(cb_approve_request_discount(call, bot))
    assert call.alerts and call.alerts[0][0] == "Нет доступа"
    assert _order(ids["order"])["status"] == "pending"
