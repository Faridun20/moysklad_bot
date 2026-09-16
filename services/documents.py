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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from services import adb_core, money
from services.legal_docs import DOCUMENT_CURRENCY, TEMPLATES, DocumentError, build_context, render_pdf
from services.database import now_str

logger = logging.getLogger(__name__)

DOC_TYPES: dict[str, str] = {
    "raspiska_ru_uz": "Расписка RU+UZ",
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
    # Подписант в расписке: «в лице …, действующего на основании …».
    ("company_position", "Должность подписанта (например: Директор)"),
    ("company_representative_gen", "Подписант в родительном падеже (директора Иванова Ивана Ивановича)"),
    ("company_position_uz", "Лавозими — должность по-узбекски (Директор)"),
    ("company_poa_number", "Доверенность № (пусто — действует на основании Устава)"),
    ("company_poa_date", "Дата доверенности (ДД.ММ.ГГГГ)"),
    ("company_city_uz", "Город по-узбекски (Тошкент)"),
)

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
        # Тот же источник, что у накладной: название компании — одно на проект,
        # и требовать вписать его заново только ради расписки незачем.
        from services.invoice_pdf import COMPANY_NAME

        out["company_name"] = COMPANY_NAME
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


def _parse_sum(raw: Any) -> int:
    """Сумма в сумах из формы → копейки (так деньги хранятся везде в проекте).

    Сумы — целые: «12 500 000» и «12500000» одно и то же, а тийины в
    расписке превратили бы пропись в «… 50/100» и график — в дроби.
    """
    text = str(raw or "").replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise DocumentError("Сумма: введите число")
    if not value.is_finite():
        raise DocumentError("Сумма: введите число")
    if value <= 0:
        raise DocumentError("Сумма должна быть больше нуля")
    if value != value.to_integral_value():
        raise DocumentError("Сумма в сумах — целым числом, без тийинов")
    cents = money.to_cents(value)
    if cents > money.HARD_MAX_CENTS:
        raise DocumentError("Сумма слишком большая — проверьте, не лишние ли нули")
    return cents


def form_to_context(data: dict[str, Any]) -> tuple[dict, dict]:
    """Тело формы → (контекст шаблона, поля для generated_documents).

    Бросает DocumentError с текстом для человека. Поля формы — только те, что
    попадают в документ или в список документов. Прежние поля (паспорт,
    адрес, телефон должника, валюта, пеня, льготные дни, свидетель, порядок
    оплаты, подписант из формы) бланк не печатает: старые клиенты API их ещё
    присылают — они молча игнорируются, а не валят создание документа.
    """
    doc_type = _clean(data.get("doc_type"))
    if doc_type not in TEMPLATES:
        raise DocumentError("Выберите тип документа")

    # ФИО в документ не печатается (Должник впишет сам), но без него документ
    # в списке и в подписи к PDF в Telegram не опознать.
    debtor_full_name = _clean(data.get("debtor_full_name"))
    if not debtor_full_name:
        raise DocumentError("Укажите ФИО должника")

    company = company_requisites()
    creditor = {
        "name": company["company_name"],
        "tin": company["company_tin"],
        "address": company["company_address"],
        "representative": company["company_representative"],
        "representative_gen": company["company_representative_gen"],
        "position": company["company_position"],
        "position_uz": company["company_position_uz"],
        "poa_number": company["company_poa_number"],
        "poa_date": company["company_poa_date"],
    }
    if not creditor["name"]:
        # Формулировка важна: менеджеры читали это как «впишите компанию
        # КЛИЕНТА» и вставали в тупик, когда товар берёт физлицо. Компания
        # тут наша, а должником может быть кто угодно — ему компания не нужна.
        raise DocumentError(
            "Не заполнены реквизиты вашей компании (кредитора) — "
            "откройте «Реквизиты компании». К должнику это не относится: "
            "им может быть и физлицо без компании."
        )
    city = _clean(data.get("city"), 80) or company["company_city"]
    if not city:
        raise DocumentError("Укажите город")
    # Узбекское название — только к городу из реквизитов: для другого города
    # в форме оно было бы чужим («Тошкент» при городе «Самарканд»).
    if city == company["company_city"]:
        creditor["city_uz"] = company["company_city_uz"]

    product_name = _clean(data.get("product_name"), 300)
    if not product_name:
        raise DocumentError("Укажите, что передаётся (товар/техника)")

    total_cents = _parse_sum(data.get("total_amount"))
    start_date = _parse_date(data.get("start_date") or date.today().isoformat())
    term_months = _parse_int(data.get("term_months"), "Срок (месяцев)", minimum=1)
    # Порядок оплаты ВЫВОДИТСЯ из числа платежей и больше ниоткуда. Было
    # второе поле «Разовый / Рассрочка», и они противоречили друг другу:
    # менеджер вписывал 6 платежей, забывал переключить — и расписка молча
    # выходила с одной строкой графика на всю сумму. Присланный старым
    # клиентом `payment_type` игнорируется.
    raw_count = str(data.get("installments_count") or "").strip()
    count = _parse_int(raw_count, "Число платежей", minimum=1) if raw_count else 1
    payment_type = "installment" if count >= 2 else "single"

    context = build_context(
        doc_type=doc_type, city=city, creditor=creditor, product_name=product_name,
        total_cents=total_cents, start_date=start_date, term_months=term_months,
        installments_count=count, debtor_full_name=debtor_full_name,
    )
    record = {
        "doc_type": doc_type,
        "counterparty_id": data.get("counterparty_id") or None,
        "client_name": debtor_full_name,
        # Паспорт Должник пишет от руки — в базу системе класть нечего.
        "passport_data": "",
        "product_name": product_name,
        "total_amount_cents": total_cents,
        "currency": DOCUMENT_CURRENCY,
        "start_date": start_date.isoformat(),
        "term_months": term_months,
        "payment_type": payment_type,
        "installments_count": count if payment_type == "installment" else None,
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
        return {"sent": False, "reason": "Бот сейчас недоступен — попробуйте позже"}
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
        return {"sent": False, "reason": "Не удалось отправить в Telegram — попробуйте ещё раз"}
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
        return {"ok": False, "error": "Не удалось собрать документ — проверьте поля формы и повторите"}

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


# Кто видит ЛЮБОЙ документ. Остальные роли — только составленные собой: в
# расписке паспортные данные и адрес должника, и менеджеру чужие клиенты не
# нужны ни в списке, ни в повторной отправке, ни на принтере.
DOC_ADMIN_ROLES = ("admin", "boss")


def can_access(doc: dict, user_id: int, role: str) -> bool:
    """Документ доступен руководству целиком, остальным — только свой."""
    if role in DOC_ADMIN_ROLES:
        return True
    try:
        return int(doc.get("created_by") or 0) == int(user_id)
    except (TypeError, ValueError):
        return False


async def list_documents(limit: int = 50, *, created_by: int | None = None) -> list[dict]:
    """Последние документы; `created_by` — только составленные этим человеком."""
    where, args = "", [int(limit)]
    if created_by is not None:
        where, args = "WHERE g.created_by = $2 ", [int(limit), int(created_by)]
    rows = await adb_core.fetch(
        "SELECT g.id, g.client_name, g.product_name, g.total_amount_cents, g.currency, g.start_date, "
        "       g.term_months, g.payment_type, g.installments_count, g.file_path, g.created_by, "
        "       g.created_at, t.type AS doc_type "
        "FROM generated_documents g LEFT JOIN document_templates t ON t.id = g.template_id "
        + where + "ORDER BY g.id DESC LIMIT $1",
        *args,
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
    cents = int(doc.get("total_amount_cents") or 0)
    # Сумы целые — «12 500 000 UZS», без «.00»; у старых долларовых
    # документов с центами дробная часть остаётся.
    amount = money.format_cents(cents, decimals=0 if cents % 100 == 0 else 2, sep=" ")
    return f"📄 {_label(doc)} — {doc.get('client_name') or ''} · {amount} {doc.get('currency') or ''}"
