"""
Генерация юридических документов: docxtpl → LibreOffice → PDF.

Отдельный движок от накладных: накладная — простая таблица, которую проще
собрать в HTML, а расписка — бланк юриста (узбекская кириллица, нумерация
пунктов, таблица графика), и его надёжнее держать в Word-шаблоне.

Системные зависимости: `libreoffice-writer` (без него LibreOffice не умеет
открывать .docx вообще) и `fonts-liberation` — см. Dockerfile.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, asdict
from datetime import date, datetime
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

# Тип документа → (файл шаблона, части бланка). Все три — один бланк юриста
# (templates/legal/src, scripts/build_raspiska_ru_uz): обе части, только
# русская или только узбекская. Ключи raspiska_ru и tilxat_uz — прежние: на
# них ссылаются document_templates и generated_documents в проде, а старые
# PDF по ним показываются и печатаются с диска, шаблон им не нужен.
TEMPLATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "raspiska_ru_uz": ("raspiska_ru_uz.docx", ("ru", "uz")),
    "raspiska_ru": ("raspiska_ru.docx", ("ru",)),
    "tilxat_uz": ("tilxat_uz.docx", ("uz",)),
}

# Типы, где личные данные должника (ФИО, паспорт, адрес, телефон) Должник
# пишет ОТ РУКИ, а система подставляет реквизиты кредитора, товар, сумму,
# сроки и график. Сейчас это все типы — бланк один.
HANDWRITTEN_TYPES = frozenset(TEMPLATES)

# Валюта документа — из текста бланка: «общей стоимостью … сум», «… сўм».
# Прежний пункт «стоимость определена в долларах США, уплата в сумах по
# курсу ЦБ» юрист из финального бланка убрал, поэтому сумма вводится в сумах
# и печатается как есть. Долларовая сумма под словом «сум» была бы долгом в
# тысячи раз меньше настоящего.
DOCUMENT_CURRENCY = "UZS"

# Реквизиты, которые печатаются в документе. Пустое поле ушло бы в документ
# дырой посреди фразы «в лице …, действующего на основании …». Должность —
# на языке части: русской расписке узбекская должность не нужна, и наоборот.
_REQUIRED_COMMON = (
    ("tin", "ИНН"),
    ("address", "адрес"),
    ("representative", "представитель (ФИО)"),
)
_REQUIRED_BY_LANG = {
    "ru": (("position", "должность подписанта"),),
    "uz": (("position_uz", "должность подписанта по-узбекски"),),
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
    """Копейки → «25 000 000». Сумы — целые, пробел разделяет тысячи."""
    return money.format_cents(cents, decimals=0, sep=" ")


def _fmt_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def build_schedule(total_cents: int, installments_count: int, start_date: date) -> list[dict]:
    """График платежей. Последний платёж — остаток, чтобы сумма сошлась.

    Делим в ЦЕЛЫХ сумах: тийинов в расчётах нет, а дробный остаток от деления
    обязан достаться последнему платежу целиком — иначе сумма графика
    разойдётся с суммой договора, а это первое, что пересчитает клиент.
    """
    if installments_count < 1:
        raise DocumentError("Число платежей должно быть больше нуля")
    if total_cents <= 0:
        raise DocumentError("Сумма должна быть больше нуля")
    if total_cents % 100:
        raise DocumentError("Сумма в сумах — целым числом, без тийинов")

    base = total_cents // 100 // installments_count * 100
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
    creditor: dict,
    product_name: str,
    total_cents: int,
    start_date: date,
    term_months: int,
    installments_count: int,
    debtor_full_name: str = "",
) -> dict:
    """Контекст для шаблона. Ключи обязаны совпадать с плейсхолдерами docx:
    отсутствующий ключ docxtpl отрисует пустотой, и документ уйдёт с дырой.

    `debtor_full_name` в документ не печатается (Должник вписывает ФИО сам) —
    он нужен имени PDF-файла и списку документов.
    """
    if doc_type not in TEMPLATES:
        raise DocumentError(f"Неизвестный тип документа: {doc_type}")
    if term_months < 1:
        raise DocumentError("Срок должен быть не меньше месяца")

    langs = TEMPLATES[doc_type][1]
    required = _REQUIRED_COMMON + tuple(f for lang in langs for f in _REQUIRED_BY_LANG[lang])
    missing = [label for key, label in required if not creditor.get(key)]
    if missing:
        raise DocumentError(
            "Для расписки заполните в «Реквизитах компании»: " + ", ".join(missing)
        )

    end_date = start_date + relativedelta(months=term_months)
    # Последний платёж не должен выходить за срок договора: график, который
    # заканчивается после окончания расписки, противоречит сам себе.
    last_payment = start_date + relativedelta(months=installments_count)
    if last_payment > end_date:
        raise DocumentError(
            f"Последний платёж ({_fmt_date(last_payment)}) позже срока "
            f"({_fmt_date(end_date)}): платежей больше, чем месяцев срока"
        )
    schedule = build_schedule(total_cents, installments_count, start_date)
    whole = money.from_cents(total_cents)

    return {
        "city": city,
        "city_uz": creditor.get("city_uz") or city,
        "debtor_full_name": debtor_full_name,
        "creditor_name": creditor["name"],
        "creditor_tin": creditor.get("tin", ""),
        "creditor_address": creditor.get("address", ""),
        "creditor_representative": creditor.get("representative", ""),
        **_signatory(creditor),
        "product_name": product_name,
        "total_amount": _money(total_cents),
        # Пропись — на языке своей части: в двуязычном документе обе.
        "total_amount_words_ru": amount_in_words(whole, "ru"),
        "total_amount_words_uz": amount_in_words(whole, "uz"),
        "start_date": _fmt_date(start_date),
        "end_date": _fmt_date(end_date),
        "schedule": schedule,
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


def _reserve_pdf_path(out_dir: Path, doc_type: str, name: str) -> Path:
    """Занять уникальное имя файла PDF и вернуть путь.

    Имя было `{тип}_{ФИО}_{дата}`: второй документ тому же должнику за день
    молча ЗАТИРАЛ первый, и запись первого в generated_documents начинала
    отдавать чужой PDF (подписанная расписка подменялась новой). Теперь в
    имени время до секунды, а совпадение внутри секунды разводит суффикс.
    Файл создаётся через O_EXCL — два параллельных рендера не займут одно имя
    даже в одну и ту же секунду. Имя остаётся читаемым: его видит человек в
    Telegram (read_pdf отдаёт path.name).
    """
    base = f"{doc_type}_{name}_{datetime.now():%Y-%m-%d_%H%M%S}"
    for n in range(1, 1000):
        candidate = out_dir / (f"{base}.pdf" if n == 1 else f"{base}_{n}.pdf")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise DocumentError("Не удалось подобрать имя файла документа")


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
            # Сырой stderr — в лог, человеку — короткий текст. Раньше хвост
            # stderr уезжал в ответ формы: менеджер видел пути /tmp, имя
            # профиля и английскую диагностику LibreOffice, с которой ему
            # делать нечего, а в логе причины не оставалось вовсе.
            tail = (stderr or b"").decode(errors="replace")[-2000:]
            logger.error(
                "LibreOffice не создал PDF (%s, код %s): %s",
                doc_type, proc.returncode, tail.strip() or "без сообщения",
            )
            raise DocumentError(
                "Не удалось сформировать PDF. Попробуйте ещё раз; если повторится — "
                "сообщите администратору."
            )

        name = _safe_name(str(context.get("debtor_full_name") or ""))
        pdf_dst = _reserve_pdf_path(out_dir, doc_type, name)
        shutil.copyfile(pdf_src, pdf_dst)
        logger.info("Документ %s сформирован: %s", doc_type, pdf_dst.name)
        return pdf_dst
