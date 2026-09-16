"""
«Счёт» — документ клиенту ДО отгрузки (`services/sales_invoice.py`).

Главный инвариант модуля и главный тест здесь один и тот же: СЧЁТ НИЧЕГО НЕ
ДВИГАЕТ. Ни остатка, ни накладной, ни платежа, ни долга, ни статуса заказа —
это бумага. Снимок склада и денег до и после проверяется построчно
(`test_building_and_printing_the_invoice_moves_nothing`); если однажды счёт
захочется «заодно резервировать товар», этот тест обязан упасть.

Мокаем ГРАНИЦУ: запуск `lp` (CUPS) и отправку документа в Telegram. БД, роли,
сборка HTML и сам PDF — настоящие.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

MGR, BOSS, OTHER_MGR, GUEST = 501, 502, 503, 504


def _run(coro):
    return asyncio.run(coro)


# ─── Сид ──────────────────────────────────────────────────────────────────────


def _order(db, *, uid=MGR, agent=True, items=True) -> int:
    oid = db.create_order(uid, "Менеджер Иван", "Самовывоз со склада")
    if agent:
        db.update_order_agent(oid, "1", "ООО Ромашка")
    if items:
        db.add_order_item(oid, "Болт М8", "", 3, "шт", 250.0)
        db.add_order_item(oid, "Гайка М8", "", 2, "шт", 125.5)
    return oid


@pytest.fixture
def db(isolated_db):
    isolated_db.set_role(MGR, "mgr", "Менеджер Иван", "manager")
    isolated_db.set_role(BOSS, "boss", "Руководитель Пётр", "boss")
    isolated_db.set_role(OTHER_MGR, "mgr2", "Менеджер Олег", "manager")
    isolated_db.set_role(GUEST, "guest", "Гость", "guest")
    with isolated_db.get_conn() as conn:
        cur = isolated_db.get_cursor(conn)
        cur.execute(
            isolated_db.q(
                "INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"
            ),
            ("ООО Ромашка", "customer", "+998 90 123-45-67", isolated_db.now_str()),
        )
        conn.commit()
    isolated_db.set_setting("company_name", "FARID IMPEKS LLC", BOSS)
    isolated_db.set_setting("company_tin", "301234567", BOSS)
    isolated_db.set_setting("company_address", "Ташкент, ул. Складская, 1", BOSS)
    return isolated_db


# ─── Сборка ───────────────────────────────────────────────────────────────────


def test_invoice_is_built_before_any_request_or_shipment(db):
    """Счёт выписывается по ЧЕРНОВИКУ — до заявки и до отгрузки.

    Ради этого всё и делалось: раньше печатную форму давала только отгрузка,
    то есть показать клиенту бумагу можно было лишь после того, как товар уехал.
    """
    from services.sales_invoice import build_sales_invoice

    oid = _order(db)
    assert _run(db.get_order(oid))["status"] == "draft", "ещё черновик — ни заявки, ни отгрузки"

    doc = _run(build_sales_invoice(oid))
    assert doc["number"] == str(oid), "номер счёта — номер заказа"
    assert doc["client_name"] == "ООО Ромашка"
    assert doc["client_phone"] == "+998 90 123-45-67"
    assert [ln["product_name"] for ln in doc["lines"]] == ["Болт М8", "Гайка М8"]
    # 3 × 250.00 + 2 × 125.50 = 1001.00
    assert doc["total_cents"] == 100100
    assert doc["total_words"] == "одна тысяча один"
    assert doc["company"]["company_tin"] == "301234567"


def test_number_and_date_do_not_change_between_two_prints(db):
    """Второй экземпляр — ТОТ ЖЕ документ, а не новый счёт.

    Своей последовательности у счёта нет намеренно: счётчик выдал бы на один
    заказ три номера, которые клиент между собой не свяжет.
    """
    from services.sales_invoice import build_sales_invoice

    oid = _order(db)
    first = _run(build_sales_invoice(oid))
    second = _run(build_sales_invoice(oid))
    assert (first["number"], first["date"]) == (second["number"], second["date"])


def test_invoice_number_never_touches_warehouse_counters(db):
    """Счёт не берёт номер из `invoice_counters`: там нумерация складских
    накладных, и дырки в ней из бумаги, по которой товар не двигался, —
    худшее, что можно сделать с журналом склада."""
    from services.sales_invoice import build_sales_invoice

    oid = _order(db)
    _run(build_sales_invoice(oid))
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute("SELECT COUNT(*) FROM invoice_counters")
        assert cur.fetchone()[0] == 0


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"agent": False}, "no_agent"),
        ({"items": False}, "no_items"),
    ],
)
def test_incomplete_order_is_refused_with_text(db, kwargs, code):
    from services.sales_invoice import SalesInvoiceError, build_sales_invoice

    oid = _order(db, **kwargs)
    with pytest.raises(SalesInvoiceError) as e:
        _run(build_sales_invoice(oid))
    assert e.value.code == code
    assert e.value.message and "Ошибка" not in e.value.message


def test_cancelled_order_gets_no_invoice(db):
    """Бумага с ценами на то, чего не будет, хуже её отсутствия."""
    from services.sales_invoice import SalesInvoiceError, build_sales_invoice

    oid = _order(db)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE orders SET status = 'cancelled' WHERE id = ?"), (oid,))
        conn.commit()
    with pytest.raises(SalesInvoiceError) as e:
        _run(build_sales_invoice(oid))
    assert e.value.code == "bad_status"


# ─── Печатная форма ───────────────────────────────────────────────────────────


def test_html_carries_positions_total_and_requisites(db):
    from services import invoice_pdf
    from services.sales_invoice import build_sales_invoice

    doc = _run(build_sales_invoice(_order(db)))
    html = invoice_pdf.build_sales_invoice_html(doc)
    assert "СЧЁТ НА ОПЛАТУ" in html
    assert "FARID IMPEKS LLC" in html and "301234567" in html
    assert "ООО Ромашка" in html and "+998 90 123-45-67" in html
    assert "Болт М8" in html and "Гайка М8" in html
    assert "1 001,00" in html          # итог цифрами, по-русски
    assert "одна тысяча один" in html  # он же прописью
    assert "Самовывоз со склада" in html


def test_html_escapes_product_and_client_names(db):
    """Товар «Уголок 50<60» без экранирования открывает несуществующий тег и
    ломает разметку документа целиком."""
    from services import invoice_pdf
    from services.sales_invoice import build_sales_invoice

    oid = db.create_order(MGR, "Менеджер Иван", "")
    db.update_order_agent(oid, "1", '<b>ООО</b> "Ромашка"')
    db.add_order_item(oid, "Уголок 50<60", "", 1, "шт", 10.0)
    html = invoice_pdf.build_sales_invoice_html(_run(build_sales_invoice(oid)))
    assert "Уголок 50<60" not in html
    assert "&lt;b&gt;ООО&lt;/b&gt;" in html


def test_render_produces_a_real_pdf(db):
    from services import invoice_pdf
    from services.sales_invoice import build_sales_invoice

    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    pdf = invoice_pdf.render_sales_invoice_pdf(_run(build_sales_invoice(_order(db))))
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


def test_filename_is_safe_for_any_filesystem(db):
    from services import invoice_pdf

    assert invoice_pdf.sales_invoice_filename({"number": "31"}) == "schet-31.pdf"
    assert invoice_pdf.sales_invoice_filename({"number": "../../etc/passwd"}) == "schet-etcpasswd.pdf"


# ─── Ручки ────────────────────────────────────────────────────────────────────


class _FakeBot:
    def __init__(self):
        self.docs: list[dict] = []

    async def send_document(self, chat_id, document, caption=None, **kw):
        self.docs.append({"chat_id": chat_id, "caption": caption})


class _FakeProc:
    """Процесс `lp`, отдающий заранее заданный ответ CUPS."""

    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout, self._stderr = stdout, stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture
def api(db, monkeypatch):
    import importlib

    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    rate_limit.reset()

    bot = _FakeBot()

    async def _fake_get_bot():
        return bot

    monkeypatch.setattr(server, "get_notify_bot", _fake_get_bot)
    monkeypatch.setattr(
        server,
        "verify_init_data",
        lambda init_data: {"id": int(init_data), "first_name": "U", "username": "u"},
    )
    return TestClient(server.app), db, bot


def _call(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def _no_printer(monkeypatch):
    """Контейнер без `cups-client` — штатное состояние, а не сбой."""
    import services.printing as printing

    monkeypatch.setattr(printing.shutil, "which", lambda name: None)


def _with_printer(monkeypatch, proc):
    import services.printing as printing

    calls: list[list[str]] = []

    async def _exec(*argv, **kwargs):
        calls.append(list(argv))
        return proc

    monkeypatch.setattr(printing.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(printing.asyncio, "create_subprocess_exec", _exec)
    return calls


def test_endpoint_returns_the_document_for_its_author(api):
    client, db, _bot = api
    oid = _order(db)
    r = _call(client, "/api/orders/invoice", MGR, order_id=oid)
    assert r.status_code == 200, r.text
    doc = r.json()["invoice"]
    assert doc["number"] == str(oid) and doc["total_cents"] == 100100
    assert len(doc["lines"]) == 2


def test_guest_and_foreign_manager_get_no_invoice(api):
    client, db, _bot = api
    oid = _order(db)
    assert _call(client, "/api/orders/invoice", GUEST, order_id=oid).status_code == 403
    assert _call(client, "/api/orders/invoice", OTHER_MGR, order_id=oid).status_code == 403
    # Руководителю — любой заказ: у него счёт это контроль, а не своя продажа.
    assert _call(client, "/api/orders/invoice", BOSS, order_id=oid).status_code == 200


def test_print_without_cups_refuses_in_plain_words(api, monkeypatch):
    client, db, _bot = api
    _no_printer(monkeypatch)
    oid = _order(db)
    body = _call(client, "/api/orders/invoice/print", MGR, order_id=oid).json()
    assert body["ok"] is False
    assert "Печать не настроена" in body["error"]
    # Кнопку в интерфейсе в этом случае вообще не рисуют.
    assert _call(client, "/api/orders/invoice", MGR, order_id=oid).json()["can_print"] is False


def test_print_sends_the_document_to_the_queue_and_writes_audit(api, monkeypatch):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, _bot = api
    calls = _with_printer(monkeypatch, _FakeProc(0, b"request id is Canon-7 (1 file(s))\n"))
    oid = _order(db)

    body = _call(client, "/api/orders/invoice/print", MGR, order_id=oid).json()
    assert body["ok"] is True and "Отправлено на печать" in body["message"]
    assert calls and calls[0][0] == "lp"
    # Имя задания называет документ: у принтера иначе не понять, чья бумага.
    assert f"Счёт № {oid}" in " ".join(calls[0])

    rows = _run(db.get_audit_log(limit=10))
    printed = [r for r in rows if r["action"] == "sales_invoice_printed"]
    assert len(printed) == 1 and printed[0]["user_id"] == MGR
    assert f"заказу #{oid}" in printed[0]["details"]


def test_print_failure_explains_itself(api, monkeypatch):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, _bot = api
    _with_printer(monkeypatch, _FakeProc(1, b"", b"lp: Error - unknown printer\n"))
    oid = _order(db)
    body = _call(client, "/api/orders/invoice/print", MGR, order_id=oid).json()
    assert body["ok"] is False
    assert "Очередь печати не найдена" in body["error"]
    # Неудачная печать в историю не пишется — бумаги не было.
    rows = _run(db.get_audit_log(limit=10))
    assert not [r for r in rows if r["action"] == "sales_invoice_printed"]


def test_send_delivers_pdf_to_the_person_who_asked(api):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, bot = api
    oid = _order(db)
    r = _call(client, "/api/orders/invoice/send", MGR, order_id=oid)
    assert r.status_code == 200 and r.json()["ok"] is True
    # Именно составителю: счёт обсуждают, и пересылает его человек сам.
    assert [d["chat_id"] for d in bot.docs] == [MGR]
    assert f"Счёт № {oid}" in bot.docs[0]["caption"]
    rows = _run(db.get_audit_log(limit=10))
    assert [r["action"] for r in rows if r["action"] == "sales_invoice_sent"] == ["sales_invoice_sent"]


def test_invoice_for_an_order_without_client_is_refused_with_text(api):
    client, db, _bot = api
    oid = _order(db, agent=False)
    r = _call(client, "/api/orders/invoice", MGR, order_id=oid)
    assert r.status_code == 400
    assert "выберите клиента" in r.json()["detail"].lower()


# ─── Главный инвариант: счёт ничего не двигает ───────────────────────────────


def _snapshot(db) -> dict:
    """Всё, что счёт не имеет права тронуть: склад, документы, деньги, заказ."""
    out = {}
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for table, cols in (
            ("stock", "product_id, warehouse_id, quantity"),
            ("invoices", "id, type, status, total_amount_cents"),
            ("invoice_items", "id, invoice_id, quantity, price_cents"),
            ("invoice_counters", "*"),
            ("payments", "id, order_id, amount_cents, status"),
            ("orders", "id, status, paid_at, paid_confirmed_at"),
            ("stock_writeoffs", "id, invoice_id"),
        ):
            cur.execute(f"SELECT {cols} FROM {table}")
            out[table] = [tuple(r) for r in cur.fetchall()]
    return out


def test_building_and_printing_the_invoice_moves_nothing(api, monkeypatch):
    """Счёт — бумага, а не документ учёта.

    Остаток, накладные, платежи, долг и статус заказа обязаны быть теми же
    ДО и ПОСЛЕ того, как счёт собрали, напечатали и отправили. Этот тест —
    сторож решения: если счёт когда-нибудь захочет «заодно» зарезервировать
    товар или завести платёж, он упадёт.
    """
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    client, db, bot = api
    _with_printer(monkeypatch, _FakeProc(0, b"request id is Canon-1 (1 file(s))\n"))

    # Заказ со СКЛАДСКОЙ историей: остаток заведён обычным приходом, чтобы
    # снимок был не пустым и «ничего не изменилось» что-то значило.
    from services import warehouse

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("INSERT INTO warehouses (name) VALUES (?)"), ("Основной склад",))
        cur.execute(
            db.q("INSERT INTO products (name, sku, unit, created_at) VALUES (?, ?, ?, ?)"),
            ("Болт М8", "B8", "шт", db.now_str()),
        )
        conn.commit()
    res = _run(
        warehouse.create_invoice(
            invoice_type="incoming",
            warehouse_id=1,
            counterparty_id=1,
            items=[{"product_id": 1, "quantity": 10, "price_cents": 100}],
        )
    )
    assert res["ok"], res

    oid = _order(db)
    before = _snapshot(db)

    assert _call(client, "/api/orders/invoice", MGR, order_id=oid).status_code == 200
    assert _call(client, "/api/orders/invoice/print", MGR, order_id=oid).json()["ok"] is True
    assert _call(client, "/api/orders/invoice/send", MGR, order_id=oid).json()["ok"] is True
    # Повтор — тоже ничего не двигает: у бумаги нет идемпотентности, потому что
    # ей нечего защищать.
    assert _call(client, "/api/orders/invoice/print", MGR, order_id=oid).json()["ok"] is True

    assert _snapshot(db) == before, "счёт изменил склад или деньги"
    assert len(bot.docs) == 1, "печать в Telegram ничего не шлёт"

    # И долг клиента не появился: заказ как был черновиком, так и остался.
    assert _run(db.get_open_debts()) == []
