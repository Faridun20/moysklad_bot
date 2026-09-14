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


FORM = {
    "doc_type": "raspiska_ru",
    "debtor_full_name": "Иванов Иван Иванович",
    "debtor_passport": "AA 1234567",
    "product_name": "Экскаватор JCB 3CX",
    "total_amount": "25000",
    "currency": "USD",
    "start_date": "2026-09-14",
    "term_months": "6",
    "payment_type": "installment",
    "installments_count": "6",
    "city": "Ташкент",
}


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_types_and_company_requisites(api):
    client, _db, ids, _bot = api
    r = _post(client, "/api/docs/types", ids["mgr"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["key"] for t in body["types"]] == ["raspiska_ru_uz", "raspiska_ru", "tilxat_uz"]
    assert body["handwritten_types"] == ["raspiska_ru_uz"]
    assert body["can_edit_company"] is False
    assert "can_print" in body

    # Реквизиты задаёт руководство; менеджеру — 403.
    r = _post(client, "/api/docs/company/set", ids["mgr"], company={"company_name": "X"})
    assert r.status_code == 403
    r = _post(client, "/api/docs/company/set", ids["boss"], company={
        "company_name": "ООО Ромашка", "company_tin": "123456789", "company_city": "Ташкент",
        "company_representative": "Петров П.П.",
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
    assert row["total_amount_cents"] == 2_500_000 and row["currency"] == "USD"
    assert row["payment_type"] == "installment" and row["installments_count"] == 6
    assert row["created_by"] == ids["mgr"]
    assert Path(row["file_path"]).is_file()
    # В документ ушли реквизиты компании из настроек и график на 6 платежей.
    content = Path(row["file_path"]).read_bytes().decode("utf-8")
    assert "ООО Ромашка" in content and "'number': 6" in content

    # PDF ушёл составителю.
    assert [d["chat_id"] for d in bot.documents] == [ids["mgr"]]
    assert "Расписка" in bot.documents[0]["caption"] and "Иванов" in bot.documents[0]["caption"]

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
    одной строкой на всю сумму и остатком 0 (жалоба с площадки).
    """
    from services import documents

    _client, _db, _ids, _bot = api
    bare = {k: v for k, v in FORM.items() if k != "payment_type"}

    # Ровно тот случай, на который жаловались: шесть платежей вписаны,
    # переключатель остался на «Разовый платёж».
    ctx, record = documents.form_to_context(dict(bare, payment_type="single", installments_count="6"))
    assert record["payment_type"] == "installment"
    assert record["installments_count"] == 6
    assert len(ctx["schedule"]) == 6
    # Остаток нулевой ТОЛЬКО у последнего платежа — иначе график бессмысленен.
    assert ctx["schedule"][-1]["balance"].startswith("0")
    assert not ctx["schedule"][0]["balance"].startswith("0")

    # Форма без переключателя вообще (новая) — тот же результат.
    _ctx, rec = documents.form_to_context(dict(bare, installments_count="6"))
    assert rec["payment_type"] == "installment"

    # Один платёж и пустое поле — разовый, без графика.
    for count in ("1", ""):
        _ctx, rec = documents.form_to_context(dict(bare, installments_count=count))
        assert rec["payment_type"] == "single" and rec["installments_count"] is None

    # Явная рассрочка с одним платежом — противоречие, а не тихий разовый.
    with pytest.raises(documents.DocumentError):
        documents.form_to_context(dict(FORM, payment_type="installment", installments_count="1"))


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


@pytest.mark.skipif(not HAS_SOFFICE, reason="нет LibreOffice (в образе он есть)")
def test_real_render_produces_pdf(isolated_db, tmp_path, monkeypatch):
    """Настоящий LibreOffice: форма → PDF с текстом должника."""
    from pypdf import PdfReader

    from services import documents

    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path))
    isolated_db.set_setting("company_name", "ООО Ромашка", 1)
    res = asyncio.run(documents.create_document(FORM, created_by=100))
    assert res["ok"], res
    text = " ".join(" ".join(p.extract_text() for p in PdfReader(res["file"]).pages).split())
    assert "Иванов Иван Иванович" in text and "ООО Ромашка" in text


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


def test_ru_uz_uses_signatory_from_requisites(api):
    """Расписка RU+UZ берёт подписанта и основание из реквизитов; без них —
    понятный отказ с перечнем того, что заполнить, а не документ с дырами."""
    client, _db, ids, _bot = api
    form = {**FORM, "doc_type": "raspiska_ru_uz"}
    _post(client, "/api/docs/company/set", ids["boss"], company={"company_name": "ООО Ромашка"})
    r = _post(client, "/api/docs/create", ids["mgr"], **form)
    assert r.status_code == 400
    assert "Реквизитах компании" in r.json()["detail"] and "должность подписанта" in r.json()["detail"]

    _post(client, "/api/docs/company/set", ids["boss"], company={
        "company_name": "ООО Ромашка", "company_tin": "123456789", "company_address": "Ташкент",
        "company_representative": "Петров Пётр", "company_position": "Директор",
        "company_position_uz": "Директор", "company_representative_gen": "директора Петрова Петра",
        "company_poa_number": "7", "company_poa_date": "01.09.2026",
        "company_city": "Ташкент", "company_city_uz": "Тошкент",
    })
    r = _post(client, "/api/docs/create", ids["mgr"], **form)
    assert r.status_code == 200, r.text
    pdf = _bot.documents[-1]["document"] if _bot.documents and "document" in _bot.documents[-1] else None
    from services import documents
    import asyncio
    doc = asyncio.run(documents.get_document(r.json()["id"]))
    data, _name = documents.read_pdf(doc)
    body = data.decode("utf-8", "replace")
    assert "директора Петрова Петра" in body
    assert "доверенности № 7 от 01.09.2026" in body
    assert "'city_uz': 'Тошкент'" in body

    r = _post(client, "/api/docs/create", ids["mgr"], **{**form, "currency": "UZS"})
    assert r.status_code == 400 and "долларах США" in r.json()["detail"]
    del pdf
