"""Юридические документы из WebApp: форма → PDF → Telegram → печать.

Движок (`legal_docs`: docxtpl → LibreOffice) был в проекте, но без единого
входа: ни команды в боте, ни ручки в WebApp — расписку было негде составить.
Здесь — слой между формой и движком:

* **Реквизиты компании** (кредитор) — в `app_settings` (`company_*`), не в
  форме каждый раз: их вводит руководство один раз, форма их только показывает
  и даёт поправить для конкретного документа.
* **Файл PDF хранится** в `DOCUMENTS_DIR` (том `/app/data` на проде), а путь —
  в `generated_documents.file_path`. Печать и повторная отправка читают файл,
  а не пересобирают документ: расписка подписывается один раз, и второй
  рендер с новой датой был бы другим документом.
* **Доставка — тем же приёмом, что у накладной**: PDF уходит в Telegram тому,
  кто составил, с кнопкой «Распечатать» (`prn:doc:<id>`), а из WebApp печатать
  можно прямо кнопкой. Ни доставка, ни печать не откатывают созданный документ.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

from services import adb_core, money
from services.legal_docs import TEMPLATES, DocumentError, build_context, render_pdf
from services.database import now_str

logger = logging.getLogger(__name__)

DOC_TYPES: dict[str, str] = {
    "raspiska_ru": "Расписка (рус.)",
    "tilxat_uz": "Тилхат (ўзб.)",
}

# Ключи реквизитов компании в app_settings и подписи для формы.
COMPANY_FIELDS: tuple[tuple[str, str], ...] = (
    ("company_name", "Название компании"),
    ("company_tin", "ИНН"),
    ("company_address", "Адрес"),
    ("company_representative", "Представитель (ФИО)"),
    ("company_city", "Город"),
)

DEFAULT_PENALTY_RATE = "0.1"
DEFAULT_GRACE_DAYS = 3


def documents_dir() -> Path:
    """Куда класть PDF. На проде — том /app/data (переживает рестарт)."""
    raw = os.environ.get("DOCUMENTS_DIR") or ""
    if raw:
        return Path(raw)
    base = Path(os.environ.get("INVOICE_LOGO_PATH", "")).parent
    if str(base) not in ("", "."):
        return base / "documents"
    return Path(__file__).resolve().parent.parent / "data" / "documents"


def company_requisites() -> dict[str, str]:
    """Реквизиты кредитора из настроек. Пустые строки — не заполнено."""
    from services.database import get_setting

    out = {}
    for key, _label in COMPANY_FIELDS:
        val = get_setting(key, "")
        out[key] = str(val or "")
    if not out["company_name"]:
        out["company_name"] = os.environ.get("COMPANY_NAME", "")
    return out


def save_company_requisites(values: dict[str, Any], by: int) -> dict[str, str]:
    from services.database import set_setting

    saved = {}
    for key, _label in COMPANY_FIELDS:
        if key in values:
            clean = str(values.get(key) or "").strip()[:200]
            set_setting(key, clean, by)
            saved[key] = clean
    return saved


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _parse_date(raw: Any) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        raise DocumentError("Дата начала: нужен формат ГГГГ-ММ-ДД")


def _parse_int(raw: Any, name: str, *, minimum: int = 0) -> int:
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        raise DocumentError(f"{name}: нужно целое число")
    if val < minimum:
        raise DocumentError(f"{name}: не меньше {minimum}")
    return val


def form_to_context(data: dict[str, Any]) -> tuple[dict, dict]:
    """Тело формы → (контекст шаблона, поля для generated_documents).

    Бросает DocumentError с текстом для человека. Реквизиты компании берутся из
    настроек, но поля формы `company_*` их перекрывают — для документа, где
    подписывает другой представитель.
    """
    doc_type = _clean(data.get("doc_type"))
    if doc_type not in TEMPLATES:
        raise DocumentError("Выберите тип документа")

    debtor = {
        "full_name": _clean(data.get("debtor_full_name")),
        "birth_date": _clean(data.get("debtor_birth_date"), 32),
        "passport": _clean(data.get("debtor_passport"), 64),
        "pinfl": _clean(data.get("debtor_pinfl"), 32),
        "address": _clean(data.get("debtor_address"), 300),
        "phone": _clean(data.get("debtor_phone"), 32),
    }
    if not debtor["full_name"]:
        raise DocumentError("ФИО должника обязательно")

    company = company_requisites()
    creditor = {
        "name": _clean(data.get("company_name")) or company["company_name"],
        "tin": _clean(data.get("company_tin"), 32) or company["company_tin"],
        "address": _clean(data.get("company_address"), 300) or company["company_address"],
        "representative": _clean(data.get("company_representative")) or company["company_representative"],
    }
    if not creditor["name"]:
        raise DocumentError("Укажите название компании (кредитора) — в форме или в настройках")
    city = _clean(data.get("city"), 80) or company["company_city"]
    if not city:
        raise DocumentError("Укажите город")

    product_name = _clean(data.get("product_name"), 300)
    if not product_name:
        raise DocumentError("Укажите, что передаётся (товар/техника)")

    try:
        amount = float(str(data.get("total_amount") or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        raise DocumentError("Сумма: нужно число")
    if amount <= 0:
        raise DocumentError("Сумма должна быть больше нуля")
    total_cents = money.to_cents(amount)
    currency = _clean(data.get("currency"), 8).upper() or "USD"

    start_date = _parse_date(data.get("start_date") or date.today().isoformat())
    term_months = _parse_int(data.get("term_months"), "Срок (месяцев)", minimum=1)
    payment_type = _clean(data.get("payment_type")) or "single"
    installments = None
    if payment_type == "installment":
        installments = _parse_int(data.get("installments_count"), "Число платежей", minimum=2)
    penalty_rate = _clean(data.get("penalty_rate"), 16) or DEFAULT_PENALTY_RATE
    grace_days = _parse_int(data.get("grace_days") or DEFAULT_GRACE_DAYS, "Льготные дни")
    witness_name = _clean(data.get("witness_name"))

    context = build_context(
        doc_type=doc_type, city=city, debtor=debtor, creditor=creditor,
        product_name=product_name, total_cents=total_cents, currency=currency,
        start_date=start_date, term_months=term_months, payment_type=payment_type,
        installments_count=installments, penalty_rate=penalty_rate,
        grace_days=grace_days, witness_name=witness_name,
    )
    record = {
        "doc_type": doc_type,
        "counterparty_id": data.get("counterparty_id") or None,
        "client_name": debtor["full_name"],
        "passport_data": debtor["passport"],
        "product_name": product_name,
        "total_amount_cents": total_cents,
        "currency": currency,
        "start_date": start_date.isoformat(),
        "term_months": term_months,
        "payment_type": payment_type,
        "installments_count": installments,
    }
    return context, record


async def _template_id(doc_type: str) -> int | None:
    """id активного шаблона; тип документа хранится через него (своей колонки
    у generated_documents нет). Свежая база без сидинга — досеваем на месте."""
    import asyncio

    sql = "SELECT id FROM document_templates WHERE type = $1 AND is_active = 1 ORDER BY id DESC LIMIT 1"
    row = await adb_core.fetchrow(sql, doc_type)
    if row is None:
        from services.database import seed_document_templates

        await asyncio.to_thread(seed_document_templates)
        row = await adb_core.fetchrow(sql, doc_type)
    return int(row["id"]) if row else None


def print_keyboard(doc_id: int):
    """Кнопка «Распечатать» под документом в Telegram; None — печати нет."""
    from services import printing

    if not printing.is_available():
        return None
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()
    kb.button(text="🖨 Распечатать", callback_data=printing.document_callback(int(doc_id)))
    kb.adjust(1)
    return kb.as_markup()


async def send_to_chat(bot: Any, doc: dict, chat_id: int) -> dict:
    """Отправить PDF документа в чат. Не бросает: {sent, reason}."""
    import asyncio

    if bot is None:
        return {"sent": False, "reason": "Бот недоступен"}
    found = await asyncio.to_thread(read_pdf, doc)
    if found is None:
        return {"sent": False, "reason": "Файл документа не найден — сформируйте заново"}
    pdf, filename = found
    try:
        from aiogram.types import BufferedInputFile

        await bot.send_document(
            chat_id=int(chat_id),
            document=BufferedInputFile(pdf, filename=filename),
            caption=caption_for(doc),
            reply_markup=print_keyboard(int(doc["id"])),
        )
    except Exception:
        logger.exception("Документ #%s не отправлен в чат %s", doc.get("id"), chat_id)
        return {"sent": False, "reason": "Не удалось отправить в Telegram"}
    return {"sent": True, "reason": None}


async def create_document(data: dict[str, Any], *, created_by: int) -> dict:
    """Собрать PDF по форме и записать документ. Возвращает {ok, id, file, ...}
    либо {ok: False, error}. Не бросает."""
    try:
        context, record = form_to_context(data)
    except DocumentError as e:
        return {"ok": False, "error": str(e)}

    out_dir = documents_dir()
    try:
        pdf_path = await render_pdf(record["doc_type"], context, out_dir)
    except DocumentError as e:
        logger.warning("Документ не собран: %s", e)
        return {"ok": False, "error": str(e)}
    except Exception:
        logger.exception("Документ не собран (неожиданная ошибка)")
        return {"ok": False, "error": "Не удалось собрать документ"}

    cp_id = None
    try:
        cp_id = int(record["counterparty_id"]) if record["counterparty_id"] else None
    except (TypeError, ValueError):
        cp_id = None
    template_id = await _template_id(record["doc_type"])
    stamp = now_str()
    sql = (
        "INSERT INTO generated_documents (template_id, counterparty_id, client_name, passport_data, "
        "product_name, total_amount_cents, currency, start_date, term_months, payment_type, "
        "installments_count, file_path, created_by, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)"
    )
    values = (
        template_id, cp_id, record["client_name"], record["passport_data"], record["product_name"],
        record["total_amount_cents"], record["currency"], record["start_date"], record["term_months"],
        record["payment_type"], record["installments_count"], str(pdf_path), created_by, stamp,
    )
    async with adb_core.transaction() as txn:
        if adb_core._use_postgres():
            doc_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            doc_id = await txn.fetchval("SELECT last_insert_rowid()")
    logger.info("Документ #%s (%s) для «%s» сформирован", doc_id, record["doc_type"], record["client_name"])
    return {
        "ok": True, "id": int(doc_id), "doc_type": record["doc_type"],
        "file": str(pdf_path), "filename": pdf_path.name,
        "client_name": record["client_name"], "total_amount_cents": record["total_amount_cents"],
        "currency": record["currency"],
    }


def _label(row: dict) -> str:
    tpl = row.get("doc_type") or ""
    return DOC_TYPES.get(tpl, "Документ")


async def list_documents(limit: int = 50) -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT g.id, g.client_name, g.product_name, g.total_amount_cents, g.currency, g.start_date, "
        "       g.term_months, g.payment_type, g.installments_count, g.file_path, g.created_by, "
        "       g.created_at, t.type AS doc_type "
        "FROM generated_documents g LEFT JOIN document_templates t ON t.id = g.template_id "
        "ORDER BY g.id DESC LIMIT $1",
        int(limit),
    )
    import asyncio

    paths = [str(r.get("file_path") or "") for r in rows]
    exists = await asyncio.to_thread(lambda: {p: bool(p) and Path(p).is_file() for p in set(paths)})
    out = []
    for r in rows:
        d = dict(r)
        d["type_label"] = _label(d)
        d["file_exists"] = exists.get(str(d.get("file_path") or ""), False)
        d.pop("file_path", None)
        out.append(d)
    return out


async def get_document(doc_id: int) -> dict | None:
    row = await adb_core.fetchrow(
        "SELECT g.*, t.type AS doc_type FROM generated_documents g "
        "LEFT JOIN document_templates t ON t.id = g.template_id WHERE g.id = $1",
        int(doc_id),
    )
    return dict(row) if row else None


def read_pdf(doc: dict) -> tuple[bytes, str] | None:
    """Байты PDF и имя файла; None — файла нет (эфемерный диск, ручное удаление)."""
    path = Path(str(doc.get("file_path") or ""))
    if not doc.get("file_path") or not path.is_file():
        return None
    return path.read_bytes(), path.name


def caption_for(doc: dict) -> str:
    amount = money.from_cents(int(doc.get("total_amount_cents") or 0))
    return (
        f"📄 {_label(doc)} — {doc.get('client_name') or ''} · "
        f"{amount:,.2f} {doc.get('currency') or ''}".replace(",", " ")
    )
