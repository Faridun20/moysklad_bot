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
    assert [t["key"] for t in body["types"]] == ["raspiska_ru", "tilxat_uz"]
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


def test_create_without_company_requisites_is_refused(api):
    client, _db, ids, _bot = api
    r = _post(client, "/api/docs/create", ids["mgr"], **FORM)
    assert r.status_code == 400
    assert "компани" in r.json()["detail"].lower()


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
