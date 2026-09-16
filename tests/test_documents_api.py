"""Юридические документы и печать из WebApp.

Движок PDF (LibreOffice) и принтер (CUPS) — границы с внешним миром, их
подменяем: рендер пишет файл-заглушку, печать отвечает готовым PrintResult.
Всё остальное — форма → контекст, запись в generated_documents, отправка в
Telegram, права по ролям — настоящее.
"""

from __future__ import annotations

import asyncio
import importlib
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from services.printing import PrintResult

HAS_SOFFICE = shutil.which("soffice") is not None


class _Bot:
    def __init__(self) -> None:
        self.documents: list[dict] = []

    async def send_document(self, chat_id, document, caption=None, reply_markup=None):
        self.documents.append({"chat_id": chat_id, "caption": caption, "markup": reply_markup})


@pytest.fixture
def api(isolated_db, monkeypatch, tmp_path):
    import services.rate_limit as rate_limit
    import services.roles as roles
    import webapp.server as server
    from services import documents, legal_docs

    importlib.reload(roles)
    rate_limit.reset()
    db = isolated_db
    ids = {"boss": 100, "mgr": 200, "keeper": 400}
    db.set_role(ids["boss"], "boss", "Boss", "boss")
    db.set_role(ids["mgr"], "mgr", "Manager", "manager")
    db.set_role(ids["keeper"], "keeper", "Keeper", "warehouse_keeper")

    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path / "docs"))
    # ИНН и адрес кредитора печатаются в каждом виде расписки и обязательны.
    # Название компании не ставим: его подставляет либо тест, либо запасное
    # название проекта (test_creditor_falls_back_to_project_company_name).
    for key, value in SIGNATORY.items():
        db.set_setting(key, value, ids["boss"])
    monkeypatch.setattr(
        server, "verify_init_data",
        lambda s: {"id": int(s), "first_name": "U", "username": "u"} if str(s).isdigit() else None,
    )
    bot = _Bot()

    async def _get_bot():
        return bot

    monkeypatch.setattr(server, "get_notify_bot", _get_bot)

    # Граница: LibreOffice. Файл-заглушка с сигнатурой PDF и контекстом внутри,
    # чтобы тест мог проверить, ЧТО попало в документ.
    def _write_fake(doc_type, context, out_dir):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{doc_type}_{context['debtor_full_name'].replace(' ', '_')}.pdf"
        path.write_bytes(b"%PDF-1.4\n" + repr(context).encode("utf-8"))
        return path

    async def fake_render(doc_type, context, out_dir, template_override=None):
        return await asyncio.to_thread(_write_fake, doc_type, context, out_dir)

    monkeypatch.setattr(documents, "render_pdf", fake_render)
    monkeypatch.setattr(legal_docs, "render_pdf", fake_render)
    return TestClient(server.app), db, ids, bot


SIGNATORY = {
    "company_tin": "123456789", "company_address": "Ташкент, ул. Навои 1",
    "company_city": "Ташкент", "company_city_uz": "Тошкент",
}

FORM = {
    "doc_type": "raspiska_ru",
    "debtor_full_name": "Иванов Иван Иванович",
    "product_name": "Экскаватор JCB 3CX",
    "total_amount": "25000000",
    "start_date": "2026-09-14",
    "term_months": "6",
    "installments_count": "6",
    "city": "Ташкент",
}

# Поля прежней формы: бланк их не печатает, старый клиент ещё может прислать.
LEGACY_FIELDS = {
    "debtor_passport": "AA 1234567", "debtor_address": "Ташкент", "debtor_phone": "+998901112233",
    "debtor_pinfl": "123", "debtor_birth_date": "1990-01-01", "currency": "USD",
    "payment_type": "single", "penalty_rate": "0.5", "grace_days": "5", "witness_name": "Каримов",
    "company_representative": "Посторонний", "company_name": "Чужое ООО",
}


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_types_and_company_requisites(api):
    client, _db, ids, _bot = api
    r = _post(client, "/api/docs/types", ids["mgr"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["types"] == [
        {"key": "raspiska_ru_uz", "label": "Расписка RU+UZ"},
        {"key": "raspiska_ru", "label": "Расписка (рус.)"},
        {"key": "tilxat_uz", "label": "Тилхат (ўзб.)"},
    ]
    # Руководитель в системе есть — менеджер реквизиты не правит.
    assert body["can_edit_company"] is False
    assert "Меняет руководитель" in body["company_edit_hint"]
    assert "can_print" in body
    # Форма по группам, у каждого поля — пример заполнения.
    groups = [g["title"] for g in body["company_form"]]
    assert groups == ["Компания", "Банк", "Подписи", "Счёт на оплату", "Расписка"]
    fields = {f["key"]: f for g in body["company_form"] for f in g["fields"]}
    assert all(f["placeholder"] for f in fields.values())
    assert "company_position" not in fields and "company_representative" not in fields

    # Реквизиты задаёт руководство; менеджеру при живом руководителе — 403.
    r = _post(client, "/api/docs/company/set", ids["mgr"], company={"company_name": "X"})
    assert r.status_code == 403
    assert "руководитель" in r.json()["detail"]
    r = _post(client, "/api/docs/company/set", ids["boss"], company={
        "company_name": "ООО Ромашка", "company_tin": "123456789", "company_city": "Ташкент",
    })
    assert r.status_code == 200, r.text
    assert r.json()["company"]["company_name"] == "ООО Ромашка"
    r = _post(client, "/api/docs/types", ids["boss"])
    assert r.json()["company"]["company_city"] == "Ташкент" and r.json()["can_edit_company"] is True


def test_create_stores_record_and_sends_pdf(api):
    client, db, ids, bot = api
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})

    r = _post(client, "/api/docs/create", ids["mgr"], **FORM)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["sent"] is True
    doc_id = body["id"]

    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT * FROM generated_documents WHERE id = ?"), (doc_id,))
        row = dict(cur.fetchone())
    assert row["client_name"] == "Иванов Иван Иванович"
    # Сумма — в сумах, как в тексте бланка («… сум»).
    assert row["total_amount_cents"] == 2_500_000_000 and row["currency"] == "UZS"
    assert row["payment_type"] == "installment" and row["installments_count"] == 6
    assert row["passport_data"] == "", "паспорт Должник пишет от руки"
    assert row["created_by"] == ids["mgr"]
    assert Path(row["file_path"]).is_file()
    # В документ ушли реквизиты компании из настроек и график на 6 платежей.
    content = Path(row["file_path"]).read_bytes().decode("utf-8")
    assert "ООО Ромашка" in content and "'number': 6" in content

    # PDF ушёл составителю.
    assert [d["chat_id"] for d in bot.documents] == [ids["mgr"]]
    assert bot.documents[0]["caption"] == "📄 Расписка (рус.) — Иванов Иван Иванович · 25 000 000 UZS"

    r = _post(client, "/api/docs/list", ids["boss"])
    docs = r.json()["documents"]
    assert len(docs) == 1 and docs[0]["type_label"] == "Расписка (рус.)" and docs[0]["file_exists"]
    assert "file_path" not in docs[0], "путь на диске наружу не отдаём"


def test_create_validation_errors_are_400_with_text(api):
    client, _db, ids, _bot = api
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})
    bad = dict(FORM, term_months="3")  # 6 платежей за 3 месяца
    r = _post(client, "/api/docs/create", ids["mgr"], **bad)
    assert r.status_code == 400
    assert "позже срока" in r.json()["detail"]

    r = _post(client, "/api/docs/create", ids["mgr"], **dict(FORM, debtor_full_name=""))
    assert r.status_code == 400 and "ФИО" in r.json()["detail"]

    r = _post(client, "/api/docs/create", ids["keeper"], **FORM)
    assert r.status_code == 403


def test_creditor_falls_back_to_project_company_name(api):
    """Реквизиты не заполнены — берём то же название, что печатает накладная.

    Менеджер читал отказ «укажите название компании» как «впишите компанию
    КЛИЕНТА» и вставал в тупик, когда товар берёт физлицо. Название компании
    в проекте одно (`invoice_pdf.COMPANY_NAME`), и спрашивать его второй раз
    ради расписки незачем.
    """
    from services import documents, invoice_pdf

    client, _db, ids, _bot = api
    r = _post(client, "/api/docs/create", ids["mgr"], **FORM)
    assert r.status_code == 200, r.text
    assert documents.company_requisites()["company_name"] == invoice_pdf.COMPANY_NAME


def test_create_is_refused_when_company_name_is_empty_everywhere(api, monkeypatch):
    """Отказ остаётся там, где имени кредитора нет ВООБЩЕ — и говорит, чьё оно."""
    from services import documents

    monkeypatch.setattr(documents, "company_requisites", lambda: dict.fromkeys(
        (k for k, _ in documents.COMPANY_FIELDS), ""
    ))
    client, _db, ids, _bot = api
    r = _post(client, "/api/docs/create", ids["mgr"], **FORM)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "кредитор" in detail.lower() and "физлицо" in detail.lower()


def test_payment_type_follows_the_number_of_payments(api):
    """График строится по ЧИСЛУ платежей, а не по забытому переключателю.

    Раньше рядом стояли «Порядок оплаты» и «Число платежей»: менеджер вписывал
    шесть платежей, оставлял «Разовый платёж» — и расписка молча выходила с
    одной строкой на всю сумму и остатком 0 (жалоба с площадки). Переключателя
    больше нет, присланный старым клиентом `payment_type` ни на что не влияет.
    """
    from services import documents

    _client, _db, _ids, _bot = api

    ctx, record = documents.form_to_context(dict(FORM, payment_type="single", installments_count="6"))
    assert record["payment_type"] == "installment"
    assert record["installments_count"] == 6
    assert len(ctx["schedule"]) == 6
    # Остаток нулевой ТОЛЬКО у последнего платежа — иначе график бессмысленен.
    assert ctx["schedule"][-1]["balance"] == "0"
    assert ctx["schedule"][0]["balance"] != "0"

    # Один платёж и пустое поле — разовый; старый `installment` не мешает.
    for count in ("1", ""):
        _ctx, rec = documents.form_to_context(dict(FORM, payment_type="installment", installments_count=count))
        assert rec["payment_type"] == "single" and rec["installments_count"] is None
        assert len(_ctx["schedule"]) == 1


def test_legacy_form_fields_are_ignored(api):
    """Поля прежней формы (паспорт, валюта, пеня, свидетель, подписант) бланк
    не печатает: запрос с ними не падает, и в документ они не попадают."""
    client, db, ids, _bot = api
    r = _post(client, "/api/docs/create", ids["mgr"], **FORM, **LEGACY_FIELDS)
    assert r.status_code == 200, r.text
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT * FROM generated_documents WHERE id = ?"), (r.json()["id"],))
        row = dict(cur.fetchone())
    assert (row["currency"], row["passport_data"], row["payment_type"]) == ("UZS", "", "installment")
    content = Path(row["file_path"]).read_bytes().decode("utf-8")
    for leaked in ("AA 1234567", "Каримов", "Посторонний", "Чужое ООО", "penalty", "witness", "passport"):
        assert leaked not in content, leaked
    assert "Ташкент, ул. Навои 1" in content


@pytest.mark.parametrize(
    "amount,msg",
    [("0", "больше нуля"), ("abc", "введите число"), ("1500,50", "тийинов"), ("NaN", "введите число"),
     ("1e20", "слишком большая")],
)
def test_amount_in_sums_is_validated(api, amount, msg):
    from services import documents

    with pytest.raises(documents.DocumentError, match=msg):
        documents.form_to_context(dict(FORM, total_amount=amount))


def test_amount_accepts_spaces_between_thousands(api):
    from services import documents

    _ctx, record = documents.form_to_context(dict(FORM, total_amount="12 500 000"))
    assert record["total_amount_cents"] == 1_250_000_000
    assert _ctx["total_amount"] == "12 500 000"


def test_send_and_print(api, monkeypatch):
    from services import printing

    client, _db, ids, bot = api
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})
    doc_id = _post(client, "/api/docs/create", ids["mgr"], **FORM).json()["id"]
    bot.documents.clear()

    r = _post(client, "/api/docs/send", ids["boss"], doc_id=doc_id)
    assert r.status_code == 200 and r.json()["ok"]
    assert [d["chat_id"] for d in bot.documents] == [ids["boss"]]

    # Без cups-client печать честно отказывает, а не падает.
    monkeypatch.setattr(printing, "is_available", lambda: False)
    r = _post(client, "/api/docs/print", ids["mgr"], doc_id=doc_id)
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "не настроена" in r.json()["error"]

    printed: list[dict] = []

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append({"size": len(pdf_bytes), "filename": filename, "label": label})
        return PrintResult(True, job="Canon-42")

    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)
    r = _post(client, "/api/docs/print", ids["mgr"], doc_id=doc_id)
    assert r.json()["ok"] is True and "Canon-42" in r.json()["message"]
    assert printed and printed[0]["filename"].endswith(".pdf") and "Иванов" in printed[0]["label"]

    r = _post(client, "/api/docs/print", ids["mgr"], doc_id=999)
    assert r.status_code == 404


def test_missing_file_is_reported_not_crashed(api):
    client, db, ids, _bot = api
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})
    doc_id = _post(client, "/api/docs/create", ids["mgr"], **FORM).json()["id"]
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("SELECT file_path FROM generated_documents WHERE id = ?"), (doc_id,))
        Path(cur.fetchone()["file_path"]).unlink()
    r = _post(client, "/api/docs/send", ids["mgr"], doc_id=doc_id)
    assert r.status_code == 200 and r.json()["ok"] is False and "не найден" in r.json()["error"]
    assert _post(client, "/api/docs/list", ids["mgr"]).json()["documents"][0]["file_exists"] is False


def test_bot_print_callback_handles_documents(api, monkeypatch):
    """Кнопка «Распечатать» под документом в Telegram — prn:doc:<id>."""
    from handlers import printing as h
    from services import printing

    client, _db, ids, _bot = api
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})
    doc_id = _post(client, "/api/docs/create", ids["mgr"], **FORM).json()["id"]

    printed = []

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append(filename)
        return PrintResult(True, job="7")

    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)
    reports = []

    class _Msg:
        async def reply(self, text, **kw):
            reports.append(text)

    class _Call:
        data = printing.document_callback(doc_id)
        message = _Msg()

        class from_user:
            id = ids["boss"]
            full_name = "Boss"

        async def answer(self, *a, **k):
            pass

    asyncio.run(h.cb_print(_Call()))
    assert printed and printed[0].endswith(".pdf")
    assert reports and "печать" in reports[-1].lower()


def test_manager_sees_and_touches_only_own_documents(api, monkeypatch):
    """IDOR: в расписке паспорт и адрес должника. Менеджер — только свои
    документы (список, повторная отправка, печать, кнопка в боте); чужой id
    отвечает как несуществующий. Руководство — все."""
    from handlers import printing as h
    from services import printing

    client, db, ids, bot = api
    other = 300
    db.set_role(other, "mgr2", "Manager2", "manager")
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})
    mine = _post(client, "/api/docs/create", ids["mgr"], **FORM).json()["id"]
    theirs = _post(client, "/api/docs/create", other, **dict(FORM, debtor_full_name="Петров Пётр")).json()["id"]
    bot.documents.clear()

    listed = [d["id"] for d in _post(client, "/api/docs/list", ids["mgr"]).json()["documents"]]
    assert listed == [mine]
    boss_listed = {d["id"] for d in _post(client, "/api/docs/list", ids["boss"]).json()["documents"]}
    assert boss_listed == {mine, theirs}

    r = _post(client, "/api/docs/send", ids["mgr"], doc_id=theirs)
    assert r.status_code == 404
    assert bot.documents == [], "чужой документ не ушёл в чат"

    printed: list[str] = []

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append(label)
        return PrintResult(True, job="1")

    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)
    assert _post(client, "/api/docs/print", ids["mgr"], doc_id=theirs).status_code == 404
    assert printed == []

    # Свой — можно; руководству — любой.
    assert _post(client, "/api/docs/send", ids["mgr"], doc_id=mine).json()["ok"] is True
    assert _post(client, "/api/docs/print", ids["boss"], doc_id=theirs).json()["ok"] is True
    assert _post(client, "/api/docs/send", ids["boss"], doc_id=theirs).json()["ok"] is True

    # Бот: подделанный callback prn:doc:<чужой id> от менеджера не печатает.
    printed.clear()
    reports = []

    class _Msg:
        async def reply(self, text, **kw):
            reports.append(text)

    class _Call:
        data = printing.document_callback(theirs)
        message = _Msg()

        class from_user:
            id = ids["mgr"]
            full_name = "Mgr"

        async def answer(self, *a, **k):
            pass

    asyncio.run(h.cb_print(_Call()))
    assert printed == [] and reports and "не найден" in reports[-1]


@pytest.mark.skipif(not HAS_SOFFICE, reason="нет LibreOffice (в образе он есть)")
def test_real_render_produces_pdf(isolated_db, tmp_path, monkeypatch):
    """Настоящий LibreOffice: форма → PDF с реквизитами кредитора и суммой."""
    from pypdf import PdfReader

    from services import documents

    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path))
    isolated_db.set_setting("company_name", "ООО Ромашка", 1)
    for key, value in SIGNATORY.items():
        isolated_db.set_setting(key, value, 1)
    res = asyncio.run(documents.create_document(FORM, created_by=100))
    assert res["ok"], res
    text = " ".join(" ".join(p.extract_text() for p in PdfReader(res["file"]).pages).split())
    assert "ООО Ромашка" in text and "25 000 000 (двадцать пять миллионов) сум" in text
    assert "Иванов" not in text, "ФИО Должник вписывает от руки"


def test_invoice_print_endpoint(api, monkeypatch):
    from services import printing, warehouse
    from services import container_receipt

    client, _db, ids, _bot = api
    pid = asyncio.run(container_receipt.create_product("Болт"))["product_id"]
    wid = asyncio.run(warehouse.default_warehouse_id())
    inv = asyncio.run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=wid, items=[{"product_id": pid, "quantity": 3, "price_cents": None}],
    ))
    monkeypatch.setattr(printing, "is_available", lambda: True)
    r = _post(client, "/api/wh/invoices", ids["mgr"])
    assert r.json()["can_print"] is True

    printed = []

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append(label)
        return PrintResult(False, error="Принтер не принимает задания")

    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)
    pytest.importorskip("weasyprint", reason="нет weasyprint")
    r = _post(client, "/api/wh/invoices/print", ids["mgr"], invoice_id=inv["invoice_id"])
    assert r.status_code == 200
    assert r.json()["ok"] is False and "Принтер" in r.json()["error"]
    assert printed and inv["invoice_number"] in printed[0]
    assert _post(client, "/api/wh/invoices/print", ids["keeper"], invoice_id=inv["invoice_id"]).status_code == 403


@pytest.mark.parametrize("doc_type", ["raspiska_ru_uz", "raspiska_ru", "tilxat_uz"])
def test_every_type_needs_only_tin_and_address_not_a_signatory(api, doc_type):
    """Жалоба владельца: «зачем должность, если расписку пишет физлицо».
    Ни один вид не спрашивает должность и представителя; без ИНН и адреса —
    отказ, который называет, ЧТО и ГДЕ заполнить."""
    from services import documents

    client, db, ids, _bot = api
    form = {**FORM, "doc_type": doc_type}
    for key in ("company_tin", "company_address"):
        db.set_setting(key, "", ids["boss"])
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})
    r = _post(client, "/api/docs/create", ids["mgr"], **form)
    assert r.status_code == 400
    assert r.json()["detail"] == (
        "Заполните ИНН и юридический адрес в Настройки → Реквизиты компании — без этого не выписать расписку"
    )

    _post(client, "/api/docs/company/set", ids["boss"], company={
        "company_name": "ООО Ромашка", "company_tin": "123456789", "company_address": "Ташкент, ул. Навои 1",
    })
    r = _post(client, "/api/docs/create", ids["mgr"], **form)
    assert r.status_code == 200, r.text
    doc = asyncio.run(documents.get_document(r.json()["id"]))
    assert doc["doc_type"] == doc_type
    data, _name = documents.read_pdf(doc)
    body = data.decode("utf-8", "replace")
    assert "'creditor_tin': '123456789'" in body
    assert "position" not in body and "representative" not in body and "basis" not in body
    assert "'city_uz': 'Тошкент'" in body


def test_old_documents_stay_listed_sent_and_printed(api, monkeypatch, tmp_path):
    """Документы, составленные по СТАРЫМ шаблонам (долларовые, с паспортом),
    остались в generated_documents проды. Шаблон им не нужен: список, повторная
    отправка и печать читают готовый PDF с диска."""
    from services import documents, printing
    from services.database import seed_document_templates

    client, db, ids, bot = api
    seed_document_templates()
    pdf = tmp_path / "raspiska_ru_Старый_2026-01-10.pdf"
    pdf.write_bytes(b"%PDF-1.4\nold")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        for doc_type in ("raspiska_ru", "tilxat_uz"):
            cur.execute(db.q("SELECT id FROM document_templates WHERE type = ?"), (doc_type,))
            tpl_id = cur.fetchone()["id"]
            cur.execute(db.q(
                "INSERT INTO generated_documents (template_id, client_name, passport_data, product_name, "
                "total_amount_cents, currency, start_date, term_months, payment_type, installments_count, "
                "file_path, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            ), (tpl_id, f"Старый {doc_type}", "AA1", "Кран", 2_500_050, "USD", "2026-01-10", 6,
                "installment", 6, str(pdf), ids["mgr"], db.now_str()))
        conn.commit()

    docs = _post(client, "/api/docs/list", ids["mgr"]).json()["documents"]
    assert [(d["type_label"], d["currency"], d["file_exists"]) for d in docs] == [
        ("Тилхат (ўзб.)", "USD", True), ("Расписка (рус.)", "USD", True),
    ]
    bot.documents.clear()
    assert _post(client, "/api/docs/send", ids["mgr"], doc_id=docs[0]["id"]).json()["ok"] is True
    assert bot.documents[0]["caption"] == "📄 Тилхат (ўзб.) — Старый tilxat_uz · 25 000,50 USD"

    printed: list[int] = []

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append(len(pdf_bytes))
        return PrintResult(True, job="3")

    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)
    assert _post(client, "/api/docs/print", ids["mgr"], doc_id=docs[1]["id"]).json()["ok"] is True
    assert printed == [len(b"%PDF-1.4\nold")]
    assert documents.caption_for({"doc_type": "raspiska_ru", "client_name": "X",
                                  "total_amount_cents": 2_500_000, "currency": "USD"}).endswith("25 000 USD")
