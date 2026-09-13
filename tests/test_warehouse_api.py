"""WebApp API локального складского учёта: права, идемпотентность, отказы.

FastAPI TestClient; мокаем только границу verify_init_data — БД, роли и
движение остатков настоящие. Права по ТЗ: создание — менеджер и выше,
отмена — только босс/админ.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient


class _FakeBot:
    """Граница с Telegram. Собираем отправленные документы."""

    def __init__(self):
        self.docs = []

    async def send_document(self, chat_id, document, caption=None):
        self.docs.append({"chat_id": chat_id, "caption": caption})


@pytest.fixture
def api(isolated_db, monkeypatch):
    import importlib

    import services.rate_limit as rate_limit
    import services.roles as roles
    import services.warehouse as warehouse
    import webapp.server as server

    importlib.reload(roles)
    importlib.reload(warehouse)
    # Корзины лимитера живут в памяти процесса и между тестами не сбрасываются;
    # создание накладной лимитировано 30/мин на юзера, и модуль целиком
    # выбирает лимит за одного менеджера. Чистим, как в других API-тестах.
    rate_limit.reset()

    db = isolated_db
    ids = {"admin": 1, "boss": 100, "mgr": 200, "guest": 300}
    db.set_role(ids["admin"], "admin_user", "Admin", "admin")
    db.set_role(ids["boss"], "boss_user", "Boss", "boss")
    db.set_role(ids["mgr"], "mgr_user", "Manager", "manager")
    db.set_role(ids["guest"], "guest_user", "Guest", "guest")

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO warehouses (name) VALUES (?)"), ("Основной склад",))
        for name, sku in (("Болт М8", "B8"), ("Гайка М8", "G8")):
            cur.execute(
                db.q("INSERT INTO products (name, sku, unit, created_at) VALUES (?, ?, ?, ?)"),
                (name, sku, "шт", db.now_str()),
            )
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, created_at) VALUES (?, ?, ?)"),
            ("ООО Ромашка", "customer", db.now_str()),
        )
        conn.commit()

    fake_bot = _FakeBot()

    async def _fake_get_bot():
        return fake_bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    ids["bot"] = fake_bot
    return TestClient(server.app), db, ids


def _incoming(client, uid, product_id=1, qty=10, price=100, **extra):
    body = {
        "initData": str(uid),
        "type": "incoming",
        "warehouse_id": 1,
        "items": [{"product_id": product_id, "quantity": qty, "price_cents": price}],
    }
    body.update(extra)
    return client.post("/api/wh/invoices/create", json=body)


def _outgoing(client, uid, product_id=1, qty=1, price=25000, **extra):
    body = {
        "initData": str(uid),
        "type": "outgoing",
        "warehouse_id": 1,
        "counterparty_id": 1,
        "items": [{"product_id": product_id, "quantity": qty, "price_cents": price}],
    }
    body.update(extra)
    return client.post("/api/wh/invoices/create", json=body)


# ─── Права ────────────────────────────────────────────────────────────────────


def test_manager_can_create_invoice(api):
    client, _db, ids = api
    r = _incoming(client, ids["mgr"])
    assert r.status_code == 200, r.text
    assert r.json()["invoice_number"].startswith("IN-")


def test_guest_cannot_create_invoice(api):
    client, _db, ids = api
    assert _incoming(client, ids["guest"]).status_code == 403


def test_manager_cannot_cancel_invoice(api):
    """Отмена — только босс/админ: она двигает остатки назад и правит историю."""
    client, _db, ids = api
    inv = _incoming(client, ids["mgr"]).json()
    r = client.post(
        "/api/wh/invoices/cancel",
        json={"initData": str(ids["mgr"]), "invoice_id": inv["invoice_id"]},
    )
    assert r.status_code == 403


def test_boss_can_cancel_invoice(api):
    client, _db, ids = api
    inv = _incoming(client, ids["mgr"]).json()
    r = client.post(
        "/api/wh/invoices/cancel",
        json={"initData": str(ids["boss"]), "invoice_id": inv["invoice_id"]},
    )
    assert r.status_code == 200, r.text
    stock = client.post("/api/wh/stock", json={"initData": str(ids["mgr"])}).json()
    assert {p["name"]: p["quantity"] for p in stock["products"]}["Болт М8"] == 0.0


def test_guest_cannot_read_stock(api):
    client, _db, ids = api
    assert client.post("/api/wh/stock", json={"initData": str(ids["guest"])}).status_code == 403


# ─── Валидация запроса ────────────────────────────────────────────────────────


def test_bad_type_rejected(api):
    client, _db, ids = api
    r = client.post(
        "/api/wh/invoices/create",
        json={"initData": str(ids["mgr"]), "type": "transfer", "items": [{}]},
    )
    assert r.status_code == 400


def test_empty_items_rejected(api):
    client, _db, ids = api
    r = client.post(
        "/api/wh/invoices/create",
        json={"initData": str(ids["mgr"]), "type": "incoming", "items": []},
    )
    assert r.status_code == 400


def test_outgoing_requires_counterparty(api):
    client, _db, ids = api
    r = client.post(
        "/api/wh/invoices/create",
        json={
            "initData": str(ids["mgr"]),
            "type": "outgoing",
            "warehouse_id": 1,
            "items": [{"product_id": 1, "quantity": 1, "price_cents": 100}],
        },
    )
    assert r.status_code == 400


def test_insufficient_stock_returns_409_with_code(api):
    """Отказ по остатку — 409 с машиночитаемым кодом, а не 500."""
    client, _db, ids = api
    r = _outgoing(client, ids["mgr"], qty=5)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "insufficient_stock"


# ─── Идемпотентность ──────────────────────────────────────────────────────────


def test_repeated_create_with_same_key_does_not_double_stock(api):
    """Повторная отправка формы не списывает товар дважды.

    Без ключа идемпотентности дрогнувшая связь или второй тап по «Сохранить»
    провели бы вторую накладную — и остаток уехал бы на две отгрузки.
    """
    client, _db, ids = api
    _incoming(client, ids["mgr"], qty=10)

    first = _outgoing(client, ids["mgr"], qty=3, idempotency_key="form-abc")
    assert first.status_code == 200, first.text
    second = _outgoing(client, ids["mgr"], qty=3, idempotency_key="form-abc")
    assert second.status_code == 200, second.text
    assert second.json()["invoice_id"] == first.json()["invoice_id"]

    stock = client.post("/api/wh/stock", json={"initData": str(ids["mgr"])}).json()
    assert {p["name"]: p["quantity"] for p in stock["products"]}["Болт М8"] == 7.0

    invoices = client.post(
        "/api/wh/invoices", json={"initData": str(ids["mgr"]), "type": "outgoing"}
    ).json()
    assert len(invoices["invoices"]) == 1


def test_different_keys_create_separate_invoices(api):
    client, _db, ids = api
    _incoming(client, ids["mgr"], qty=10)
    a = _outgoing(client, ids["mgr"], qty=2, idempotency_key="k1")
    b = _outgoing(client, ids["mgr"], qty=2, idempotency_key="k2")
    assert a.json()["invoice_id"] != b.json()["invoice_id"]
    stock = client.post("/api/wh/stock", json={"initData": str(ids["mgr"])}).json()
    assert {p["name"]: p["quantity"] for p in stock["products"]}["Болт М8"] == 6.0


def test_business_refusal_is_replayed_under_same_key(api):
    """Отказ по остатку тоже сохраняется под ключом: ретрай той же формы
    отдаёт тот же ответ, а не пробует списать ещё раз."""
    client, _db, ids = api
    first = _outgoing(client, ids["mgr"], qty=5, idempotency_key="nope")
    assert first.status_code == 409
    second = _outgoing(client, ids["mgr"], qty=5, idempotency_key="nope")
    assert second.json()["code"] == "insufficient_stock"


# ─── Чтения ───────────────────────────────────────────────────────────────────


def test_stock_lists_products_with_zero(api):
    client, _db, ids = api
    _incoming(client, ids["mgr"], product_id=1, qty=4)
    body = client.post("/api/wh/stock", json={"initData": str(ids["mgr"])}).json()
    by_name = {p["name"]: p["quantity"] for p in body["products"]}
    assert by_name == {"Болт М8": 4.0, "Гайка М8": 0.0}

    only_pos = client.post(
        "/api/wh/stock", json={"initData": str(ids["mgr"]), "only_positive": True}
    ).json()
    assert [p["name"] for p in only_pos["products"]] == ["Болт М8"]


def test_invoice_get_returns_items(api):
    client, _db, ids = api
    inv = _incoming(client, ids["mgr"]).json()
    body = client.post(
        "/api/wh/invoices/get",
        json={"initData": str(ids["mgr"]), "invoice_id": inv["invoice_id"]},
    ).json()
    assert body["invoice"]["items"][0]["product_name"] == "Болт М8"


def test_invoice_get_404(api):
    client, _db, ids = api
    r = client.post(
        "/api/wh/invoices/get", json={"initData": str(ids["mgr"]), "invoice_id": 9999}
    )
    assert r.status_code == 404


@pytest.mark.parametrize("query", ["Ромашк", "ромашк", "РОМАШК"])
def test_counterparties_search_is_case_insensitive_for_cyrillic(api, query):
    """Регистр запроса не влияет — и на SQLite тоже.

    Встроенный SQLite LOWER() ASCII-only и «ромашк» не нашло бы «Ромашка»;
    adb_core переопределяет функцию Unicode-aware, поэтому прод и локалка
    отвечают одинаково. Сторож именно на кириллице: с латиницей расхождение
    не воспроизводится и прошло бы незамеченным.
    """
    client, _db, ids = api
    body = client.post(
        "/api/wh/counterparties", json={"initData": str(ids["mgr"]), "search": query}
    ).json()
    assert [c["name"] for c in body["counterparties"]] == ["ООО Ромашка"]

    empty = client.post(
        "/api/wh/counterparties", json={"initData": str(ids["mgr"]), "search": "неттакого"}
    ).json()
    assert empty["counterparties"] == []


def test_cancel_twice_returns_409(api):
    client, _db, ids = api
    inv = _incoming(client, ids["mgr"]).json()
    body = {"initData": str(ids["boss"]), "invoice_id": inv["invoice_id"]}
    assert client.post("/api/wh/invoices/cancel", json=body).status_code == 200
    second = client.post("/api/wh/invoices/cancel", json=body)
    assert second.status_code == 409
    assert second.json()["code"] == "already_cancelled"


def test_create_writes_audit_log(api):
    client, db, ids = api
    _incoming(client, ids["mgr"])
    rows = asyncio.run(db.get_audit_log(limit=10))
    assert any(r["action"] == "wh_invoice_create" for r in rows)


# ─── PDF расходной накладной ─────────────────────────────────────────────────


def _link_telegram(db, counterparty_id=1, telegram_id=555):
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE counterparties SET telegram_id = ? WHERE id = ?"),
            (telegram_id, counterparty_id),
        )
        conn.commit()


def test_outgoing_sends_pdf_to_linked_client(api):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, ids = api
    _link_telegram(db)
    _incoming(client, ids["mgr"], qty=10)

    r = _outgoing(client, ids["mgr"], qty=2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pdf_sent"] is True
    assert "pdf_warning" not in body

    bot = ids["bot"]
    assert len(bot.docs) == 1
    assert bot.docs[0]["chat_id"] == 555

    inv = client.post(
        "/api/wh/invoices/get",
        json={"initData": str(ids["mgr"]), "invoice_id": body["invoice_id"]},
    ).json()["invoice"]
    assert inv["telegram_sent"] == 1


def test_outgoing_without_telegram_id_saves_and_warns(api):
    """Ключевой случай ТЗ: накладная проведена, остатки списаны, PDF не ушёл."""
    client, _db, ids = api
    _incoming(client, ids["mgr"], qty=10)

    r = _outgoing(client, ids["mgr"], qty=2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["pdf_sent"] is False
    assert "вручную" in body["pdf_warning"]
    assert ids["bot"].docs == []

    stock = client.post("/api/wh/stock", json={"initData": str(ids["mgr"])}).json()
    assert {p["name"]: p["quantity"] for p in stock["products"]}["Болт М8"] == 8.0


def test_incoming_does_not_send_pdf(api):
    client, db, ids = api
    _link_telegram(db)
    r = _incoming(client, ids["mgr"])
    assert "pdf_sent" not in r.json()
    assert ids["bot"].docs == []


def test_pdf_send_failure_does_not_roll_back_invoice(api, monkeypatch):
    """Telegram лёг — накладная всё равно остаётся проведённой."""
    import services.invoice_delivery as d

    async def _boom(*a, **k):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(d, "deliver_invoice_pdf", _boom)

    client, db, ids = api
    _link_telegram(db)
    _incoming(client, ids["mgr"], qty=10)
    r = _outgoing(client, ids["mgr"], qty=2)
    assert r.status_code == 200, r.text
    assert r.json()["pdf_sent"] is False

    stock = client.post("/api/wh/stock", json={"initData": str(ids["mgr"])}).json()
    assert {p["name"]: p["quantity"] for p in stock["products"]}["Болт М8"] == 8.0


def test_manual_send_after_linking_telegram(api):
    """Сценарий из ТЗ: привязали telegram_id позже — отправляем кнопкой."""
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, ids = api
    _incoming(client, ids["mgr"], qty=10)
    out = _outgoing(client, ids["mgr"], qty=2).json()
    assert out["pdf_sent"] is False

    _link_telegram(db)
    r = client.post(
        "/api/wh/invoices/send",
        json={"initData": str(ids["mgr"]), "invoice_id": out["invoice_id"]},
    )
    assert r.status_code == 200, r.text
    assert len(ids["bot"].docs) == 1


def test_manual_send_twice_blocked_without_force(api):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, ids = api
    _link_telegram(db)
    _incoming(client, ids["mgr"], qty=10)
    out = _outgoing(client, ids["mgr"], qty=2).json()
    assert out["pdf_sent"] is True

    body = {"initData": str(ids["mgr"]), "invoice_id": out["invoice_id"]}
    again = client.post("/api/wh/invoices/send", json=body)
    assert again.status_code == 409
    assert again.json()["code"] == "already_sent"

    forced = client.post("/api/wh/invoices/send", json={**body, "force": True})
    assert forced.status_code == 200
    assert len(ids["bot"].docs) == 2


def test_manual_send_unknown_invoice(api):
    client, _db, ids = api
    r = client.post(
        "/api/wh/invoices/send", json={"initData": str(ids["mgr"]), "invoice_id": 999}
    )
    assert r.status_code == 404


def test_retry_with_same_key_does_not_resend_pdf(api):
    """Повторная отправка формы не шлёт клиенту второй экземпляр документа."""
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, ids = api
    _link_telegram(db)
    _incoming(client, ids["mgr"], qty=10)

    first = _outgoing(client, ids["mgr"], qty=2, idempotency_key="form-1")
    second = _outgoing(client, ids["mgr"], qty=2, idempotency_key="form-1")
    assert first.status_code == 200, (first.status_code, first.json())
    assert first.json()["invoice_id"] == second.json()["invoice_id"]
    assert len(ids["bot"].docs) == 1
