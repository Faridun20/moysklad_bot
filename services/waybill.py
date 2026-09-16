"""Товарная накладная: данные для печати поверх складской накладной.

`warehouse.get_invoice` отдаёт шапку и позиции, а бланку владельца нужно
больше: реквизиты нашей компании (грузоотправитель), адрес и телефон клиента
(грузополучатель) и ОСНОВАНИЕ — «Счёт на оплату № {заказ} от {даты заказа}»,
если накладная выписана отгрузкой заказа (`order_shipment`). Накладная без
заказа (руководство оформило отгрузку прямо на складе) основания не печатает.

Всё здесь — ЧТЕНИЕ и best-effort: накладная сопровождает уже проведённую
отгрузку, поэтому сбой чтения реквизитов не отменяет бумагу, а оставляет в ней
черту для записи от руки. Рендер — один (`invoice_pdf.render_invoice_pdf`).
"""

from __future__ import annotations

import asyncio
import logging

from services import requisites
from services.invoice_pdf import DEFAULT_DOC_LANG, invoice_filename, normalize_lang

logger = logging.getLogger(__name__)


async def order_basis(invoice_id: int) -> dict | None:
    """{order_id, date} заказа, чьей отгрузкой выписана накладная; иначе None."""
    from services import adb_core

    row = await adb_core.fetchrow(
        "SELECT s.order_id, o.created_at FROM order_shipment s "
        "JOIN orders o ON o.id = s.order_id WHERE s.invoice_id = $1",
        int(invoice_id),
    )
    if not row:
        return None
    return {"order_id": int(row["order_id"]), "date": str(row.get("created_at") or "")}


async def prepare_invoice(invoice: dict, lang: str | None = None) -> dict:
    """Накладная + всё, что печатает бланк. Исходный словарь не меняется."""
    out = dict(invoice)
    out["doc_lang"] = normalize_lang(lang) or normalize_lang(invoice.get("doc_lang")) or DEFAULT_DOC_LANG
    try:
        out["company"] = await asyncio.to_thread(requisites.company_requisites)
    except Exception:
        logger.warning("Накладная #%s: реквизиты компании не прочитаны", invoice.get("id"), exc_info=True)
        out["company"] = {}
    if invoice.get("type") != "outgoing":
        return out
    buyer = {"tin": "", "address": "", "phone": ""}
    try:
        cp_id = invoice.get("counterparty_id")
        if cp_id:
            from services import counterparties as cp_service

            cp = await cp_service.get(cp_id)
            buyer.update(await requisites.counterparty_requisites(cp_id))
            buyer["phone"] = str((cp or {}).get("phone") or "")
        if invoice.get("id"):
            out["basis"] = await order_basis(int(invoice["id"]))
    except Exception:
        logger.warning("Накладная #%s: данные клиента не прочитаны", invoice.get("id"), exc_info=True)
    out["buyer"] = buyer
    return out


async def render(invoice: dict, lang: str | None = None) -> tuple[bytes, str]:
    """(PDF, имя файла). WeasyPrint синхронный и тяжёлый — в поток. Бросает
    исключение рендера: вызывающий решает, как о нём сказать человеку."""
    from services import invoice_pdf

    prepared = await prepare_invoice(invoice, lang)
    pdf = await asyncio.to_thread(invoice_pdf.render_invoice_pdf, prepared)
    return pdf, invoice_filename(invoice)
