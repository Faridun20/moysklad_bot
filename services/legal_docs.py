"""
Генерация юридических документов: docxtpl → LibreOffice → PDF.

Отдельный движок от накладных: накладная — простая таблица, которую проще
собрать в HTML, а расписка требует точного формата (Times New Roman 12pt,
узбекская кириллица, нумерация пунктов), и его надёжнее держать в Word-шаблоне.

Системные зависимости: `libreoffice-writer` (без него LibreOffice не умеет
открывать .docx вообще) и `fonts-liberation` — см. Dockerfile.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path

from dateutil.relativedelta import relativedelta

from services import money
from services.numerals import amount_in_words

logger = logging.getLogger(__name__)

SOFFICE = shutil.which("soffice") or "/usr/bin/soffice"
CONVERT_TIMEOUT = 120

# Каталог шаблонов в репозитории. Путь в БД (document_templates.file_path)
# может указывать и на том /app/data — тогда используется он.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "legal"

# Тип документа → (файл шаблона, язык). Язык нужен не только прописи: в
# документ подставляется целое предложение о порядке оплаты, и по-русски
# оно должно быть по-русски.
TEMPLATES: dict[str, tuple[str, str]] = {
    "raspiska_ru_uz": ("raspiska_ru_uz.docx", "ru"),
    "raspiska_ru": ("raspiska_ru.docx", "ru"),
    "tilxat_uz": ("tilxat_uz.docx", "uz"),
}

# Двуязычная расписка юриста (templates/legal/src, scripts/build_raspiska_ru_uz):
# данные должника и сумму Должник пишет ОТ РУКИ, система подставляет только
# реквизиты кредитора, товар, сроки и график. Стоимость в ней зашита в
# долларах США («подлежит уплате в сумах по курсу ЦБ»), пеня и условия
# досрочного взыскания — тоже в тексте, полями формы не меняются.
HANDWRITTEN_TYPES = frozenset({"raspiska_ru_uz"})

# Реквизиты, которые печатаются в двуязычной расписке. Пустое поле ушло бы в
# документ дырой посреди фразы «в лице …, действующего на основании …».
_HANDWRITTEN_REQUIRED = (
    ("tin", "ИНН"),
    ("address", "адрес"),
    ("representative", "представитель (ФИО)"),
    ("position", "должность подписанта"),
    ("position_uz", "должность подписанта по-узбекски"),
)

# Пункт о порядке оплаты — на языке документа. В черновике эта строка была
# зашита по-узбекски для обоих шаблонов, и русская расписка получала бы
# узбекскую фразу в середине текста.
_PAYMENT_CLAUSE = {
    ("ru", "single"): "Долг погашается единовременным платежом в полном размере.",
    ("ru", "installment"): "Долг погашается в рассрочку согласно графику платежей.",
    ("uz", "single"): "Қарз бир марталик тўлов тартибида тўлиқ миқдорда тўланади.",
    ("uz", "installment"): "Қарз бўлиб-бўлиб тўлаш тартибида тўланади.",
}


class DocumentError(Exception):
    """Документ не может быть сформирован. Текст — для показа менеджеру."""


@dataclass(frozen=True)
class PaymentRow:
    number: int
    date: str
    amount: str
    balance: str


def _money(cents: int) -> str:
    """Копейки → «25 000.00». Пробел как разделитель тысяч — как в документах."""
    return money.format_cents(cents, decimals=2, sep=" ")


def _fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def build_schedule(total_cents: int, installments_count: int, start_date: date) -> list[dict]:
    """График платежей. Последний платёж — остаток, чтобы сумма сошлась.

    Делим в копейках, а не в Decimal-рублях: копейки от деления обязаны
    достаться последнему платежу целиком, иначе сумма графика разойдётся с
    суммой договора — а это первое, что пересчитает клиент.
    """
    if installments_count < 1:
        raise DocumentError("Число платежей должно быть больше нуля")
    if total_cents <= 0:
        raise DocumentError("Сумма должна быть больше нуля")

    base = total_cents // installments_count
    rows, paid = [], 0
    for n in range(1, installments_count + 1):
        amount = base if n < installments_count else total_cents - paid
        paid += amount
        rows.append(
            asdict(
                PaymentRow(
                    number=n,
                    # Платёж n — через n месяцев от старта. relativedelta, а не
                    # timedelta: у месяцев разная длина, и 31 января + 1 месяц
                    # это 28 февраля, а не 3 марта.
                    date=_fmt_date(start_date + relativedelta(months=n)),
                    amount=_money(amount),
                    balance=_money(total_cents - paid),
                )
            )
        )

    if paid != total_cents:  # pragma: no cover — арифметическая страховка
        raise DocumentError(f"График не сходится: {paid} != {total_cents}")
    return rows


def build_context(
    *,
    doc_type: str,
    city: str,
    debtor: dict,
    creditor: dict,
    product_name: str,
    total_cents: int,
    currency: str,
    start_date: date,
    term_months: int,
    payment_type: str,
    installments_count: int | None,
    penalty_rate: str,
    grace_days: int,
    witness_name: str = "",
) -> dict:
    """Контекст для шаблона. Ключи обязаны совпадать с плейсхолдерами docx:
    отсутствующий ключ docxtpl отрисует пустотой, и документ уйдёт с дырой."""
    if doc_type not in TEMPLATES:
        raise DocumentError(f"Неизвестный тип документа: {doc_type}")
    if payment_type not in ("single", "installment"):
        raise DocumentError(f"Неизвестный тип оплаты: {payment_type}")
    if term_months < 1:
        raise DocumentError("Срок должен быть не меньше месяца")

    lang = TEMPLATES[doc_type][1]
    if doc_type in HANDWRITTEN_TYPES:
        if currency != "USD":
            raise DocumentError("Расписка RU+UZ составляется только в долларах США")
        missing = [label for key, label in _HANDWRITTEN_REQUIRED if not creditor.get(key)]
        if missing:
            raise DocumentError(
                "Для расписки RU+UZ заполните в «Реквизитах компании»: " + ", ".join(missing)
            )

    if payment_type == "single":
        count = 1
    else:
        if not installments_count or installments_count < 2:
            raise DocumentError("Для рассрочки нужно не меньше двух платежей")
        count = installments_count

    end_date = start_date + relativedelta(months=term_months)
    # Последний платёж не должен выходить за срок договора: график, который
    # заканчивается после окончания расписки, противоречит сам себе.
    last_payment = start_date + relativedelta(months=count)
    if last_payment > end_date:
        raise DocumentError(
            f"Последний платёж ({_fmt_date(last_payment)}) позже срока "
            f"({_fmt_date(end_date)}): платежей больше, чем месяцев срока"
        )

    return {
        "city": city,
        "document_date": _fmt_date(date.today()),
        "debtor_full_name": debtor["full_name"],
        "debtor_birth_date": debtor.get("birth_date", ""),
        "debtor_passport": debtor.get("passport", ""),
        "debtor_pinfl": debtor.get("pinfl", ""),
        "debtor_address": debtor.get("address", ""),
        "debtor_phone": debtor.get("phone", ""),
        "creditor_name": creditor["name"],
        "creditor_tin": creditor.get("tin", ""),
        "creditor_address": creditor.get("address", ""),
        "creditor_representative": creditor.get("representative", ""),
        **_signatory(creditor),
        "city_uz": creditor.get("city_uz") or city,
        "product_name": product_name,
        "total_amount": _money(total_cents),
        "total_amount_words": amount_in_words(money.from_cents(total_cents), lang),
        "currency": currency,
        "start_date": _fmt_date(start_date),
        "end_date": _fmt_date(end_date),
        "payment_clause": _PAYMENT_CLAUSE[(lang, payment_type)],
        "schedule": build_schedule(total_cents, count, start_date),
        # Пеня вставляется как УСЛОВИЕ, а не посчитанное число: на момент
        # подписания просрочки ещё нет, и сумма пени неизвестна.
        "penalty_rate": penalty_rate,
        "grace_days": grace_days,
        "witness_name": witness_name,
    }


def _signatory(creditor: dict) -> dict:
    """Подписант кредитора для фраз «в лице …» и «на основании …».

    «В лице» требует родительного падежа («директора Иванова И. И.»), а
    склонять ФИО программно — значит однажды просклонять неправильно. Поэтому
    форма берётся из реквизитов как есть; не заполнена — должность и ФИО в
    именительном: грамматически хуже, но без выдуманных окончаний.
    Основание — доверенность, если указан её номер, иначе Устав.
    """
    position = creditor.get("position", "")
    representative = creditor.get("representative", "")
    poa_number = creditor.get("poa_number", "")
    poa_date = creditor.get("poa_date", "")
    if poa_number:
        date_ru = f" от {poa_date}" if poa_date else ""
        date_uz = f"{poa_date} йилдаги " if poa_date else ""
        basis_ru = f"доверенности № {poa_number}{date_ru}"
        basis_uz = f"{date_uz}№ {poa_number} ишончнома"
    else:
        basis_ru, basis_uz = "Устава", "Устав"
    return {
        "creditor_position": position,
        "creditor_position_uz": creditor.get("position_uz", ""),
        "creditor_representative_gen": (
            creditor.get("representative_gen") or f"{position} {representative}".strip()
        ),
        "creditor_basis_ru": basis_ru,
        "creditor_basis_uz": basis_uz,
    }


def template_path(doc_type: str, override: str | None = None) -> Path:
    """Путь к .docx шаблону. override — из document_templates.file_path."""
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = TEMPLATES_DIR.parent.parent / p
        return p
    if doc_type not in TEMPLATES:
        raise DocumentError(f"Неизвестный тип документа: {doc_type}")
    return TEMPLATES_DIR / TEMPLATES[doc_type][0]


def _safe_name(value: str) -> str:
    keep = "".join(c for c in value if c.isalnum() or c in " -_").strip()
    return keep.replace(" ", "_") or "document"


def fill_template(tpl: Path, context: dict, dst: Path) -> None:
    """Заполнить .docx-шаблон значениями контекста.

    autoescape обязателен: без него «&» или «<» в ФИО должника ломают XML
    документа (LibreOffice его не откроет), а разметка WordprocessingML в поле
    формы прошла бы внутрь как есть. Вынесено из `render_pdf` ради теста —
    LibreOffice для проверки экранирования не нужен, а закрытая в замыкании
    функция тестируется только через него.
    """
    from docxtpl import DocxTemplate

    doc = DocxTemplate(str(tpl))
    doc.render(context, autoescape=True)
    doc.save(str(dst))


async def render_pdf(doc_type: str, context: dict, out_dir: Path,
                     template_override: str | None = None) -> Path:
    """Заполнить шаблон и сконвертировать в PDF. Возвращает путь к PDF."""
    tpl = template_path(doc_type, template_override)
    if not tpl.is_file():
        raise DocumentError(f"Шаблон не найден: {tpl}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        docx_path = tmp / "document.docx"
        # docxtpl синхронный и читает/пишет файлы — уводим с event loop.
        await asyncio.to_thread(fill_template, tpl, context, docx_path)

        # -env:UserInstallation обязателен: без него параллельные вызовы
        # soffice дерутся за общий профиль пользователя и виснут.
        proc = await asyncio.create_subprocess_exec(
            SOFFICE,
            f"-env:UserInstallation=file://{tmp}/profile",
            "--headless", "--norestore",
            "--convert-to", "pdf",
            "--outdir", str(tmp),
            str(docx_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), CONVERT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise DocumentError("LibreOffice не ответил за отведённое время")

        pdf_src = tmp / "document.pdf"
        # ПРОВЕРЯЕМ ФАЙЛ, а не код возврата: LibreOffice выходит с нулём даже
        # когда не смог открыть источник («Error: source file could not be
        # loaded» при отсутствующем libreoffice-writer). Код возврата тут
        # ничего не гарантирует.
        if not pdf_src.is_file():
            tail = (stderr or b"").decode(errors="replace")[-300:]
            raise DocumentError(f"PDF не создан. LibreOffice: {tail or 'без сообщения'}")

        stamp = date.today().isoformat()
        name = _safe_name(str(context.get("debtor_full_name") or ""))
        pdf_dst = out_dir / f"{doc_type}_{name}_{stamp}.pdf"
        shutil.copy2(pdf_src, pdf_dst)
        logger.info("Документ %s сформирован: %s", doc_type, pdf_dst.name)
        return pdf_dst
