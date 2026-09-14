"""Юридические документы: пропись, график платежей, сборка контекста, PDF.

Пропись и график проверяются подробно: это денежные поля подписываемого
документа. Сам рендер — один smoke на каждый язык, он пропускается там, где
нет LibreOffice (в CI его нет, в образе есть).
"""

import shutil
from datetime import date
from decimal import Decimal

import pytest

from services import legal_docs as ld
from services.numerals import amount_in_words, ru_number, uz_number

HAS_SOFFICE = shutil.which("soffice") is not None


# ─── Пропись ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "ноль"), (1, "один"), (2, "два"), (11, "одиннадцать"),
        (14, "четырнадцать"), (21, "двадцать один"), (100, "сто"),
        (101, "сто один"), (200, "двести"), (999, "девятьсот девяносто девять"),
        # Тысяча женского рода, миллион мужского — классическая ошибка прописи.
        (1000, "одна тысяча"), (2000, "две тысячи"), (5000, "пять тысяч"),
        (1_000_000, "один миллион"), (2_000_000, "два миллиона"),
        (5_000_000, "пять миллионов"),
        # 11-14 берут форму «тысяч», а не «тысячи» — исключение из правила.
        (11_000, "одиннадцать тысяч"), (12_000, "двенадцать тысяч"),
        (21_000, "двадцать одна тысяча"), (22_000, "двадцать две тысячи"),
        (25_000, "двадцать пять тысяч"),
        (1234, "одна тысяча двести тридцать четыре"),
    ],
)
def test_ru_number(n, expected):
    assert ru_number(n) == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "нол"), (1, "бир"), (5, "беш"), (10, "ўн"), (11, "ўн бир"),
        (21, "йигирма бир"), (90, "тўқсон"),
        # «юз» и «минг» без «бир» — так говорят.
        (100, "юз"), (101, "юз бир"), (200, "икки юз"),
        (1000, "минг"), (1001, "минг бир"), (2000, "икки минг"),
        (25_000, "йигирма беш минг"),
        (1234, "минг икки юз ўттиз тўрт"),
        (1_000_000, "бир миллион"),
    ],
)
def test_uz_number(n, expected):
    assert uz_number(n) == expected


def test_amount_in_words_appends_cents_as_fraction():
    """Копейки — дробью «50/100»: принятая в договорах форма, не требующая
    согласования с валютой. У целой суммы хвоста нет."""
    assert amount_in_words(Decimal("25000"), "ru") == "двадцать пять тысяч"
    assert amount_in_words(Decimal("25000.50"), "ru") == "двадцать пять тысяч 50/100"
    assert amount_in_words(Decimal("25000.05"), "uz") == "йигирма беш минг 05/100"


def test_amount_in_words_rejects_unknown_language():
    with pytest.raises(ValueError):
        amount_in_words(Decimal("1"), "en")


# ─── График платежей ──────────────────────────────────────────────────────────


def test_schedule_sums_exactly_to_total():
    """Сумма графика обязана сойтись с суммой договора до копейки.

    25 000.00 / 6 не делится нацело: копейки от деления уходят в ПОСЛЕДНИЙ
    платёж. Клиент сложит колонку — она должна дать ровно сумму договора.
    """
    rows = ld.build_schedule(2_500_000, 6, date(2026, 9, 12))
    assert len(rows) == 6
    total = sum(int(r["amount"].replace(" ", "").replace(".", "")) for r in rows)
    assert total == 2_500_000
    assert rows[-1]["balance"] == "0.00"
    # Все платежи равны, кроме последнего — он добирает остаток.
    assert rows[0]["amount"] == "4 166.66"
    assert rows[-1]["amount"] == "4 166.70"


def test_schedule_dates_use_month_arithmetic():
    """31 января + 1 месяц = 28 февраля, а не 3 марта: relativedelta, не timedelta."""
    rows = ld.build_schedule(300_000, 3, date(2026, 1, 31))
    assert [r["date"] for r in rows] == ["28.02.2026", "31.03.2026", "30.04.2026"]


def test_schedule_single_payment_is_whole_sum():
    rows = ld.build_schedule(2_500_000, 1, date(2026, 9, 12))
    assert len(rows) == 1
    assert rows[0]["amount"] == "25 000.00"
    assert rows[0]["balance"] == "0.00"


@pytest.mark.parametrize("count,total", [(0, 1000), (-1, 1000), (2, 0), (2, -5)])
def test_schedule_rejects_nonsense(count, total):
    with pytest.raises(ld.DocumentError):
        ld.build_schedule(total, count, date(2026, 9, 12))


# ─── Контекст ─────────────────────────────────────────────────────────────────


# Реквизиты, без которых расписка RU+UZ не собирается (см. _HANDWRITTEN_REQUIRED).
FULL_CREDITOR = {
    "name": "FARID IMPEKS LLC", "tin": "309876543", "address": "г. Ташкент, ул. Амира Темура, 107Б",
    "representative": "Масуджанов Фаридун", "position": "Директор", "position_uz": "Директор",
    "representative_gen": "директора Масуджанова Фаридуна",
}


def _ctx(**over):
    base = dict(
        doc_type="raspiska_ru", city="Ташкент",
        debtor={"full_name": "Иванов Иван Иванович", "passport": "AA1234567"},
        creditor=dict(FULL_CREDITOR),
        product_name="Экскаватор JCB 3CX", total_cents=2_500_000, currency="USD",
        start_date=date(2026, 9, 12), term_months=6,
        payment_type="installment", installments_count=6,
        penalty_rate="0,5", grace_days=5,
    )
    base.update(over)
    return ld.build_context(**base)


def test_context_covers_every_template_placeholder():
    """Ключи контекста обязаны покрывать плейсхолдеры шаблона.

    Недостающий ключ docxtpl отрисует ПУСТОТОЙ — документ уйдёт клиенту с
    дырой на месте паспорта или суммы, и ни одна проверка этого не заметит.
    """
    import re
    import zipfile

    for doc_type in ld.TEMPLATES:
        xml = zipfile.ZipFile(ld.template_path(doc_type)).read("word/document.xml").decode()
        text = re.sub(r"<[^>]+>", "", xml)
        placeholders = {
            m for m in re.findall(r"\{\{\s*([\w.]+)\s*\}\}", text)
            if not m.startswith("item.")
        }
        ctx = _ctx(doc_type=doc_type)
        missing = placeholders - set(ctx)
        assert not missing, f"{doc_type}: контекст не отдаёт {sorted(missing)}"


def test_payment_clause_matches_document_language():
    """Пункт о порядке оплаты — на языке документа.

    В русской расписке узбекская фраза выглядит как ошибка нотариуса;
    поймать её может только человек, читающий текст, поэтому тест.
    """
    ru = _ctx(doc_type="raspiska_ru")["payment_clause"]
    uz = _ctx(doc_type="tilxat_uz")["payment_clause"]
    assert "рассрочку" in ru
    assert "тўлаш" in uz
    assert ru != uz


def test_amount_words_match_document_language():
    assert _ctx(doc_type="raspiska_ru")["total_amount_words"] == "двадцать пять тысяч"
    assert _ctx(doc_type="tilxat_uz")["total_amount_words"] == "йигирма беш минг"


def test_single_payment_gives_one_row():
    ctx = _ctx(payment_type="single", installments_count=None)
    assert len(ctx["schedule"]) == 1
    assert "единовременным" in ctx["payment_clause"]


def test_installments_longer_than_term_rejected():
    """График, который кончается позже срока расписки, противоречит сам себе."""
    with pytest.raises(ld.DocumentError, match="позже срока"):
        _ctx(term_months=3, installments_count=6)


@pytest.mark.parametrize(
    "over,msg",
    [
        ({"doc_type": "нет-такого"}, "тип документа"),
        ({"payment_type": "кредит"}, "тип оплаты"),
        ({"payment_type": "installment", "installments_count": 1}, "двух платежей"),
        ({"term_months": 0}, "не меньше месяца"),
    ],
)
def test_context_validation(over, msg):
    with pytest.raises(ld.DocumentError, match=msg):
        _ctx(**over)


def test_end_date_is_start_plus_term():
    assert _ctx(term_months=6)["end_date"] == "12.03.2027"


# ─── Рендер ───────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not HAS_SOFFICE, reason="нет LibreOffice (в образе он есть)")
@pytest.mark.parametrize("doc_type", ["raspiska_ru", "tilxat_uz"])
def test_render_pdf(doc_type, tmp_path):
    import asyncio

    from pypdf import PdfReader

    ctx = _ctx(doc_type=doc_type, witness_name="Петров П.П.")
    pdf = asyncio.run(ld.render_pdf(doc_type, ctx, tmp_path))
    assert pdf.is_file()
    assert pdf.name.startswith(doc_type)

    raw = "\n".join(p.extract_text() for p in PdfReader(str(pdf)).pages)
    # Пробелы схлопываем: LibreOffice переносит строки по ширине страницы, и
    # «Экскаватор JCB 3CX» может приехать разорванным. Проверяем содержание,
    # а не вёрстку — иначе тест ломается от правки любого поля выше по тексту.
    text = " ".join(raw.split())
    assert "Иванов Иван Иванович" in text
    assert "Экскаватор JCB 3CX" in text
    # Ни один плейсхолдер не должен доехать до подписи.
    assert "{{" not in text and "{%" not in text
    # Условный абзац свидетеля отработал.
    assert "Петров" in text
    # График попал в таблицу целиком.
    assert text.count("4 166.66") == 5


@pytest.mark.skipif(not HAS_SOFFICE, reason="нет LibreOffice")
def test_render_reports_missing_template():
    import asyncio

    with pytest.raises(ld.DocumentError, match="Шаблон не найден"):
        asyncio.run(ld.render_pdf("raspiska_ru", _ctx(), "/tmp", template_override="/nope.docx"))


def _docx_paragraphs(path) -> list[str]:
    from docx import Document

    doc = Document(str(path))
    rows = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            rows.extend(cell.text for cell in row.cells)
    return rows


def test_committed_templates_match_the_generator(tmp_path):
    """Шаблон в репозитории — результат прогона `scripts/build_legal_templates`.

    .docx двоичный: правку в нём не видно в диффе и не отревьюить. Исходник —
    скрипт, а расхождение между ним и файлом означает, что кто-то поправил
    шаблон в Word и следующий прогон скрипта эту правку молча затрёт.
    """
    from scripts import build_legal_templates as gen

    for doc_type, builder in (("raspiska_ru", gen.build_ru), ("tilxat_uz", gen.build_uz)):
        fresh = tmp_path / f"{doc_type}.docx"
        builder().save(str(fresh))
        assert _docx_paragraphs(fresh) == _docx_paragraphs(ld.template_path(doc_type)), (
            f"{doc_type}: шаблон разошёлся со скриптом — "
            "пересоберите `python -m scripts.build_legal_templates`"
        )


def test_template_keeps_the_look_of_the_sample():
    """Вид расписки взят с образца руководства — проверяем его приметы.

    Заголовок обычным начертанием (не капсом), город и дата ОДНОЙ строкой,
    сумма — отдельным жирным абзацем, подпись — линейкой во всю ширину с
    мелкой подписью под ней. Всё это легко потерять при следующей правке
    текста, а заметно только на распечатанном документе.
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    doc = Document(str(ld.template_path("raspiska_ru")))
    pars = doc.paragraphs

    assert pars[0].text == "Расписка"
    assert pars[0].alignment == WD_ALIGN_PARAGRAPH.CENTER

    # Город и дата — один абзац с табуляцией, а не два выключенных по краям.
    assert pars[1].text == "г. {{ city }}\t{{ document_date }} г."

    amount = next(p for p in pars if p.text.startswith("{{ total_amount }}"))
    assert all(run.bold for run in amount.runs), "сумма должна быть жирной"

    consent = next(p for p in pars if p.text.startswith("Содержание настоящей расписки"))
    assert all(run.bold for run in consent.runs)

    ruled = [p for p in pars if p._element.find(qn("w:pPr")) is not None
             and p._element.find(qn("w:pPr")).find(qn("w:pBdr")) is not None]
    assert len(ruled) == 3, "линейка под подпись: должник, кредитор, свидетель"
    assert sum("(подпись, фамилия" in p.text for p in pars) == 3


# ─── Расписка RU+UZ (бланк юриста, данные должника — от руки) ─────────────────


def test_ru_uz_only_in_dollars():
    """Текст бланка: «стоимость определена в долларах США». Сумовая расписка
    с этим текстом противоречила бы сама себе."""
    with pytest.raises(ld.DocumentError, match="долларах США"):
        _ctx(doc_type="raspiska_ru_uz", currency="UZS")


def test_ru_uz_requires_signatory_requisites():
    with pytest.raises(ld.DocumentError, match="должность подписанта.*ИНН|ИНН.*должность"):
        _ctx(doc_type="raspiska_ru_uz", creditor={"name": "X", "representative": "Петров"})


def test_ru_uz_basis_is_charter_or_power_of_attorney():
    charter = _ctx(doc_type="raspiska_ru_uz")
    assert (charter["creditor_basis_ru"], charter["creditor_basis_uz"]) == ("Устава", "Устав")

    poa = _ctx(doc_type="raspiska_ru_uz",
               creditor={**FULL_CREDITOR, "poa_number": "12", "poa_date": "01.02.2026"})
    assert poa["creditor_basis_ru"] == "доверенности № 12 от 01.02.2026"
    assert poa["creditor_basis_uz"] == "01.02.2026 йилдаги № 12 ишончнома"


def test_ru_uz_genitive_signatory_falls_back_without_inventing_endings():
    """Родительный падеж — из реквизитов как есть; нет его — должность и ФИО
    без выдуманных окончаний."""
    assert _ctx(doc_type="raspiska_ru_uz")["creditor_representative_gen"] == "директора Масуджанова Фаридуна"
    plain = {k: v for k, v in FULL_CREDITOR.items() if k != "representative_gen"}
    assert _ctx(doc_type="raspiska_ru_uz", creditor=plain)["creditor_representative_gen"] == (
        "Директор Масуджанов Фаридун"
    )


def test_ru_uz_uzbek_city_falls_back_to_city():
    assert _ctx(doc_type="raspiska_ru_uz")["city_uz"] == "Ташкент"
    assert _ctx(doc_type="raspiska_ru_uz", creditor={**FULL_CREDITOR, "city_uz": "Тошкент"})["city_uz"] == "Тошкент"


def test_ru_uz_template_matches_the_lawyer_source(tmp_path):
    """Шаблон — результат `scripts/build_raspiska_ru_uz` над бланком юриста.

    Правка .docx руками разошлась бы со скриптом и молча пропала при
    следующей сборке. Заодно: ни одной [скобки] бланка не осталось.
    """
    from scripts import build_raspiska_ru_uz as gen

    fresh = tmp_path / "raspiska_ru_uz.docx"
    gen.build().save(str(fresh))
    committed = _docx_paragraphs(ld.template_path("raspiska_ru_uz"))
    assert _docx_paragraphs(fresh) == committed, (
        "шаблон разошёлся со скриптом — пересоберите `python -m scripts.build_raspiska_ru_uz`"
    )
    assert not [t for t in committed if "[" in t]


def test_ru_uz_keeps_handwritten_lines_and_layout():
    """Данные должника и сумма остаются чертой «от руки»; город и дата — одной
    строкой через табуляцию (в бланке пробелы, и дата уезжала на вторую строку);
    заголовок графика не отрывается от таблицы."""
    from docx import Document

    doc = Document(str(ld.template_path("raspiska_ru_uz")))
    texts = [p.text for p in doc.paragraphs]
    assert sum(t.startswith("Сумма цифрами: ____") for t in texts) == 1
    assert sum(t.startswith("Рақамда: ____") for t in texts) == 1
    assert "г. {{ city }}\t«___» _______________ 20__ г." in texts
    assert "{{ city_uz }} ш.\t«___» _______________ 20__ й." in texts
    headings = [p for p in doc.paragraphs if p.text in ("3. График платежей:", "3. Тўлов жадвали:")]
    assert len(headings) == 2 and all(p.paragraph_format.keep_with_next for p in headings)


@pytest.mark.skipif(not HAS_SOFFICE, reason="нет LibreOffice (в образе он есть)")
def test_render_ru_uz_pdf(tmp_path):
    import asyncio

    from pypdf import PdfReader

    ctx = _ctx(doc_type="raspiska_ru_uz", creditor={**FULL_CREDITOR, "city_uz": "Тошкент"})
    pdf = asyncio.run(ld.render_pdf("raspiska_ru_uz", ctx, tmp_path))
    text = " ".join(" ".join(p.extract_text() for p in PdfReader(str(pdf)).pages).split())
    assert "{{" not in text and "{%" not in text and "[" not in text
    assert text.count("Экскаватор JCB 3CX") == 2, "товар — в русской и узбекской части"
    assert "в лице директора Масуджанова Фаридуна, действующего на основании Устава" in text
    # График из шести платежей — в обеих таблицах.
    assert text.count("4 166.66 USD") == 10
    # ФИО должника в документ не печатается: его пишут от руки.
    assert "Иванов" not in text
