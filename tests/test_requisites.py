"""Реквизиты для документов: «Настройки → Реквизиты компании», покупатель,
товарная накладная поверх складской накладной.

Жалоба владельца: реквизиты было не найти (он работает менеджером, а форма
жила у руководства) и форма спрашивала «должность подписанта». Здесь держим:
новые ключи засеяны; форма сохраняет и отдаёт; менеджер правит, пока
руководителя в системе нет, и получает 403, когда он есть; документ говорит,
ЧЕГО не хватает; ИНН/адрес клиента пишутся и доходят до бумаги.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

BOSS, MGR, KEEPER = 100, 200, 300


def _run(coro):
    return asyncio.run(coro)


def _client(monkeypatch):
    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server

    importlib.reload(roles)
    rate_limit.reset()
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"} if str(s).isdigit() else None,
    )
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


@pytest.fixture
def solo(isolated_db, monkeypatch):
    """Как у владельца сейчас: менеджер есть, руководителя в системе нет."""
    isolated_db.set_role(MGR, "mgr", "Фаридун", "manager")
    isolated_db.set_role(KEEPER, "keeper", "Кладовщик", "warehouse_keeper")
    return _client(monkeypatch), isolated_db


@pytest.fixture
def with_boss(solo):
    client, db = solo
    db.set_role(BOSS, "boss", "Руководитель", "boss")
    return client, db


# ─── Сидинг ──────────────────────────────────────────────────────────────────


def test_new_requisite_keys_are_seeded_without_overwriting(isolated_db):
    from services import requisites

    isolated_db.set_setting("company_tin", "123", 1)
    isolated_db.seed_app_settings()
    with isolated_db.get_conn() as conn:
        cur = isolated_db.get_cursor(conn)
        cur.execute("SELECT key, value FROM app_settings")
        rows = {r[0]: r[1] for r in cur.fetchall()}
    for field in requisites.COMPANY_FIELDS:
        assert field.key in rows, field.key
    assert rows["invoice_valid_days"] == "3"
    assert rows["company_tin"] == '"123"', "сидинг не перетирает заполненное"
    assert requisites.company_requisites()["invoice_valid_days"] == "3"


def test_every_field_has_a_human_label_and_example():
    from services import requisites

    groups = {key for key, _title in requisites.GROUPS}
    for f in requisites.COMPANY_FIELDS:
        assert f.label and f.placeholder and f.short, f.key
        assert f.group in groups, f.key
    keys = [f.key for f in requisites.COMPANY_FIELDS]
    for needed in ("company_name", "company_tin", "company_oked", "company_bank_account", "company_bank_name",
                   "company_bank_mfo", "company_address", "company_phone", "company_director",
                   "company_chief_accountant", "company_release_by", "invoice_valid_days"):
        assert needed in keys
    assert not [k for k in keys if "position" in k or "representative" in k or "poa" in k]


# ─── Кто правит ──────────────────────────────────────────────────────────────


def test_manager_edits_requisites_when_there_is_no_boss(solo):
    client, db = solo
    body = _post(client, "/api/docs/types", MGR).json()
    assert body["can_edit_company"] is True and body["company_edit_hint"] is None

    r = _post(client, "/api/docs/company/set", MGR, company={
        "company_name": "ООО «FARID IMPEKS»", "company_tin": "301234567", "company_oked": "46690",
        "company_bank_account": "20208000900123456001", "company_bank_name": "Капиталбанк",
        "company_bank_mfo": "01088", "company_director": "Масуджанов Фаридун", "invoice_valid_days": "5",
    })
    assert r.status_code == 200, r.text
    company = _post(client, "/api/docs/types", MGR).json()["company"]
    assert company["company_oked"] == "46690" and company["company_director"] == "Масуджанов Фаридун"
    assert company["invoice_valid_days"] == "5"
    rows = _run(db.get_audit_log(limit=5))
    audit = [row for row in rows if row["action"] == "company_requisites"]
    assert audit and "руководителя в системе нет" in audit[0]["details"]


def test_manager_is_refused_when_a_boss_exists(with_boss):
    client, db = with_boss
    body = _post(client, "/api/docs/types", MGR).json()
    assert body["can_edit_company"] is False
    assert "Руководитель" in body["company_edit_hint"]
    r = _post(client, "/api/docs/company/set", MGR, company={"company_tin": "999"})
    assert r.status_code == 403
    assert db.get_setting("company_tin", "") == ""
    assert _post(client, "/api/docs/company/set", BOSS, company={"company_tin": "301234567"}).status_code == 200
    assert _post(client, "/api/docs/company/set", KEEPER, company={"company_tin": "1"}).status_code == 403


@pytest.mark.parametrize("days", ["три", "0", "100"])
def test_invalid_validity_days_are_refused_whole(solo, days):
    client, db = solo
    r = _post(client, "/api/docs/company/set", MGR, company={"company_tin": "301234567", "invoice_valid_days": days})
    assert r.status_code == 400 and "Срок оплаты счёта" in r.json()["detail"]
    assert db.get_setting("company_tin", "") == "", "половину формы не сохраняем"


def test_missing_message_names_fields_and_place():
    from services import requisites

    company = {"company_name": "X", "company_tin": "1", "company_address": "A"}
    with pytest.raises(requisites.RequisitesMissing) as e:
        requisites.require(company, "sales_invoice")
    assert e.value.keys == ["company_bank_account", "company_bank_name", "company_bank_mfo"]
    assert e.value.message == (
        "Заполните расчётный счёт, банк и МФО в Настройки → Реквизиты компании — "
        "без этого не выписать счёт на оплату"
    )


# ─── Покупатель ──────────────────────────────────────────────────────────────


def _customer(db, name="Каримов Алишер", phone="") -> int:
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)"),
            (name, "customer", phone, db.now_str()),
        )
        conn.commit()
        cur.execute(db.q("SELECT id FROM counterparties WHERE name = ?"), (name,))
        return int(cur.fetchone()[0])


def test_manager_fills_client_tin_address_phone_from_the_card(solo):
    client, db = solo
    cp = _customer(db)
    detail = _post(client, "/api/clients/detail", MGR, agent_id=str(cp)).json()
    assert detail["requisites"] == {"tin": "", "address": "", "editable": True}

    # ПИНФЛ переписывают с паспорта группами — пробелы между цифрами снимаем.
    r = _post(client, "/api/clients/requisites/set", MGR, agent_id=str(cp),
              tin="3012 3456 7890 12", address="Самарканд, Регистан 1", phone="+998 90 123-45-67")
    assert r.status_code == 200, r.text
    assert r.json()["requisites"]["tin"] == "30123456789012"
    detail = _post(client, "/api/clients/detail", MGR, agent_id=str(cp)).json()
    assert detail["requisites"]["tin"] == "30123456789012"
    assert detail["requisites"]["address"] == "Самарканд, Регистан 1"
    assert detail["phone"] == "+998 90 123-45-67"

    r = _post(client, "/api/clients/requisites/set", MGR, agent_id=str(cp), tin="12345", address="")
    assert r.status_code == 400 and "ИНН — 9 цифр, ПИНФЛ — 14 цифр" in r.json()["detail"]
    assert _post(client, "/api/clients/requisites/set", KEEPER, agent_id=str(cp), tin="").status_code == 403


def test_new_counterparty_can_get_tin_and_address_right_away(solo):
    from services import requisites

    client, _db = solo
    r = _post(client, "/api/wh/counterparties/create", MGR, name="ООО Новый", tin="301234567", address="Бухара")
    assert r.status_code == 200, r.text
    saved = _run(requisites.counterparty_requisites(r.json()["counterparty_id"]))
    assert saved == {"tin": "301234567", "address": "Бухара"}
    bad = _post(client, "/api/wh/counterparties/create", MGR, name="ООО Кривой", tin="12")
    assert bad.status_code == 400


# ─── Товарная накладная поверх складской ─────────────────────────────────────


def _outgoing(db, *, with_order: bool, counterparty_id: int | None = None) -> dict:
    from services import container_receipt, warehouse

    pid = _run(container_receipt.create_product("Болт М8"))["product_id"]
    wid = _run(warehouse.default_warehouse_id())
    _run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid, items=[{"product_id": pid, "quantity": 10, "price_cents": None}],
    ))
    res = _run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=wid, counterparty_id=counterparty_id,
        items=[{"product_id": pid, "quantity": 2, "price_cents": 50_000}],
    ))
    assert res["ok"], res
    if with_order:
        oid = db.create_order(MGR, "Фаридун", "")
        with db.get_conn() as conn:
            cur = db.get_cursor(conn)
            cur.execute(db.q("UPDATE orders SET created_at = ? WHERE id = ?"), ("2026-09-16 10:00:00", oid))
            cur.execute(
                db.q("INSERT INTO order_shipment (order_id, invoice_id, shipped_at) VALUES (?, ?, ?)"),
                (oid, res["invoice_id"], db.now_str()),
            )
            conn.commit()
        res["order_id"] = oid
    return res


def test_prepare_invoice_adds_company_buyer_and_order_basis(solo):
    from services import invoice_pdf, requisites, warehouse, waybill

    _client_, db = solo
    cp = _customer(db, phone="+998 90 000-00-01")
    _run(requisites.set_counterparty_requisites(cp, tin="", address="Самарканд, Регистан 1"))
    db.set_setting("company_address", "Ташкент, Амира Темура 107Б", 1)
    inv = _outgoing(db, with_order=True, counterparty_id=cp)

    prepared = _run(waybill.prepare_invoice(_run(warehouse.get_invoice(inv["invoice_id"])), "ru"))
    assert prepared["basis"] == {"order_id": inv["order_id"], "date": "2026-09-16 10:00:00"}
    assert prepared["buyer"]["address"] == "Самарканд, Регистан 1"
    assert prepared["buyer"]["phone"] == "+998 90 000-00-01"
    html = invoice_pdf.build_invoice_html(prepared)
    assert f"Основание: Счёт на оплату № {inv['order_id']} от «16» сентября 2026 г." in html
    assert "Ташкент, Амира Темура 107Б" in html and inv["invoice_number"] in html

    standalone = _outgoing(db, with_order=False, counterparty_id=cp)
    prepared = _run(waybill.prepare_invoice(_run(warehouse.get_invoice(standalone["invoice_id"])), "ru"))
    assert prepared.get("basis") is None
    assert "Основание" not in invoice_pdf.build_invoice_html(prepared)


def test_waybill_print_and_send_take_the_language_and_remember_it(solo, monkeypatch):
    from services import invoice_pdf, printing, user_prefs
    from services.printing import PrintResult

    client, db = solo
    cp = _customer(db)
    inv = _outgoing(db, with_order=True, counterparty_id=cp)
    seen: list[str] = []
    monkeypatch.setattr(invoice_pdf, "render_invoice_pdf", lambda i: seen.append(i["doc_lang"]) or b"%PDF-1.4")
    monkeypatch.setattr(printing, "is_available", lambda: True)

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        return PrintResult(True, job="q-1")

    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)

    assert _post(client, "/api/wh/invoices", MGR).json()["doc_lang"] == "ru_uz"
    r = _post(client, "/api/wh/invoices/print", MGR, invoice_id=inv["invoice_id"], lang="ru")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen == ["ru"]
    assert user_prefs.doc_lang(MGR) == "ru"
    assert _post(client, "/api/wh/invoices", MGR).json()["doc_lang"] == "ru"
    # Без языка — последний выбранный.
    _post(client, "/api/wh/invoices/print", MGR, invoice_id=inv["invoice_id"])
    assert seen == ["ru", "ru"]
    assert _post(client, "/api/wh/invoices/print", MGR, invoice_id=inv["invoice_id"], lang="xx").status_code == 400


def test_bot_print_button_uses_the_language_in_the_callback(solo, monkeypatch):
    from handlers import printing as h
    from services import invoice_pdf, printing, user_prefs

    _client_, db = solo
    inv = _outgoing(db, with_order=False)
    seen: list[str] = []
    monkeypatch.setattr(invoice_pdf, "render_invoice_pdf", lambda i: seen.append(i["doc_lang"]) or b"%PDF-1.4")

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        return printing.PrintResult(True, job="q-2")

    monkeypatch.setattr(h.printing, "print_pdf_bytes", fake_print)
    monkeypatch.setattr(h, "can_view_stock", lambda uid: True)

    class _Msg:
        def __init__(self):
            self.replies: list[str] = []

        async def reply(self, text, **kw):
            self.replies.append(text)

    class _User:
        id = MGR
        full_name = "Фаридун"

    class _Call:
        def __init__(self, data):
            self.data = data
            self.from_user = _User()
            self.message = _Msg()

        async def answer(self, *a, **k):
            return None

    _run(h.cb_print(_Call(printing.invoice_callback(inv["invoice_id"], "uz"))))
    assert seen == ["uz"] and user_prefs.doc_lang(MGR) == "uz"
    # Старая кнопка без языка — последний выбранный.
    _run(h.cb_print(_Call(printing.invoice_callback(inv["invoice_id"]))))
    assert seen == ["uz", "uz"]
