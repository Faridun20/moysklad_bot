"""Сборка шаблонов расписки из бланка юриста — три вида из одного бланка.

    templates/legal/src/raspiska_ru_uz_source.docx  (бланк: РАСПИСКА + ТИЛХАТ)
      → templates/legal/raspiska_ru_uz.docx  — обе части, как в бланке;
      → templates/legal/raspiska_ru.docx     — только русская часть;
      → templates/legal/tilxat_uz.docx       — только узбекская часть.

Текст здесь не пишется кодом: формулировки юриста трогать нельзя, и один
бланк на три вида — чтобы русская расписка и узбекский тилхат не разошлись
с двуязычной по смыслу после следующей правки юриста. Скрипт только
превращает бланк в шаблон docxtpl и чинит вёрстку под LibreOffice, которым
бот делает PDF:

* `[скобки]` — место для реквизитов кредитора, товара, суммы и сроков —
  становятся переменными контекста (`services/legal_docs.build_context`).
  Строки с чертой `____` остаются: личные данные Должник пишет от руки, так
  задумано бланком.
* Таблица графика — строка-цикл `{%tr for item in schedule %}` вместо трёх
  строк-образцов: платежей бывает и один, и двенадцать.
* Город и дата в бланке разнесены пробелами — в LibreOffice дата переносится
  на вторую строку. Ставим табуляцию к правому полю.
* Заголовок графика не отрывается от таблицы, строки таблицы и блоки
  подписей не рвутся между страницами.
* Одноязычный вид — часть бланка до (или после) разрыва страницы перед
  «ТИЛХАТ»; сам разрыв уходит, иначе в конце/начале PDF пустая страница.

ЕДИНСТВЕННАЯ правка текста юриста — пункт о языках в одноязычных видах
(см. LANGUAGE_CLAUSE): «составлена на русском и узбекском языках… в случае
разночтений руководствуются текстом на узбекском языке» в документе на
одном языке ссылается на текст, которого в нём нет.

    python -m scripts.build_raspiska_ru_uz

Правка текста — в исходном .docx (Word), потом прогон скрипта. Если скрипт
упал на «не найдено: …», значит в бланке изменилась фраза-якорь — поправьте
REPLACEMENTS.
"""

from __future__ import annotations

import copy
from pathlib import Path

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "templates" / "legal" / "src" / "raspiska_ru_uz_source.docx"
OUT_DIR = ROOT / "templates" / "legal"

# Вид документа (ключ legal_docs.TEMPLATES) → какие части бланка берём.
VARIANTS: dict[str, tuple[str, ...]] = {
    "raspiska_ru_uz": ("ru", "uz"),
    "raspiska_ru": ("ru",),
    "tilxat_uz": ("uz",),
}

# Фраза бланка → шаблон. Замена внутри одного run: в бланке каждая [скобка]
# лежит целиком в своём run, форматирование (курсив названия) сохраняется.
# Счётчик — сколько раз фраза обязана встретиться в ПОЛНОМ бланке: молча не
# заменённая скобка ушла бы клиенту в документе как «[ИНН]».
REPLACEMENTS: list[tuple[str, str, int]] = [
    # ── RU ──
    ("г. [населённый пункт]", "г. {{ city }}", 1),
    ("[полное наименование ООО]", "{{ creditor_name }}", 2),
    ("[ИНН]", "{{ creditor_tin }}", 1),
    ("[юридический адрес]", "{{ creditor_address }}", 1),
    ("[должность, Ф.И.О. представителя]", "{{ creditor_representative_gen }}", 1),
    ("[Устава / доверенности № ___ от «___» _________ 20__ г.]", "{{ creditor_basis_ru }}", 1),
    ("[Устава / доверенности № ___ от «___»_________20__ г.]", "{{ creditor_basis_ru }}", 1),
    ("[наименование и характеристики товара]", "{{ product_name }}", 1),
    # Сумма в финальном бланке ПЕЧАТАЕТСЯ («сумма — печатается»), валюта —
    # «сум» текстом бланка; пропись — на языке своей части.
    ("[сумма цифрами]", "{{ total_amount }}", 1),
    ("[сумма прописью]", "{{ total_amount_words_ru }}", 1),
    ("в срок с [дата] по [дата]", "в срок с {{ start_date }} по {{ end_date }}", 1),
    ("[Ф.И.О. представителя]", "{{ creditor_representative }}", 1),
    ("Должность: [должность]", "Должность: {{ creditor_position }}", 1),
    # ── UZ ──
    ("[аҳоли пункти] ш.", "{{ city_uz }} ш.", 1),
    ("[ООО тўлиқ номи]", "{{ creditor_name }}", 2),
    ("[рақам]", "{{ creditor_tin }}", 1),
    ("[манзил]", "{{ creditor_address }}", 1),
    ("[лавозими, вакилнинг Ф.И.Ш.]", "{{ creditor_position_uz }} {{ creditor_representative }}", 1),
    ("[Устав / «___»_________20__ йилдаги № ___ ишончнома]", "{{ creditor_basis_uz }}", 2),
    ("[товарнинг номи ва тавсифи]", "{{ product_name }}", 1),
    ("[рақамда]", "{{ total_amount }}", 1),
    ("[ёзувда]", "{{ total_amount_words_uz }}", 1),
    ("[сана]дан [сана]гача", "{{ start_date }} йилдан {{ end_date }} йилгача", 1),
    ("[вакилнинг Ф.И.Ш.]", "{{ creditor_representative }}", 1),
    ("Лавозими: [лавозими]", "Лавозими: {{ creditor_position_uz }}", 1),
]

# Пункт о языках — ТОЛЬКО в одноязычных видах. Это единственное отступление
# от текста юриста: в русской расписке без узбекской части фраза «в случае
# разночтений руководствуются текстом на узбекском языке» отсылает к тексту,
# которого в документе нет, и сама создаёт спор о толковании. Убираем
# упоминание второго языка, оставляя «в двух экземплярах, имеющих одинаковую
# юридическую силу»; узбекский вариант собран из слов той же фразы бланка.
# В двуязычном виде пункт остаётся дословно.
LANGUAGE_CLAUSE: dict[str, tuple[str, str]] = {
    "ru": (
        "Настоящая расписка составлена на русском и узбекском языках в двух экземплярах, "
        "имеющих одинаковую юридическую силу. В случае разночтений между текстами Стороны "
        "руководствуются текстом на узбекском языке.",
        "Настоящая расписка составлена в двух экземплярах, имеющих одинаковую юридическую силу.",
    ),
    "uz": (
        "Ушбу тилхат рус ва ўзбек тилларида, бир хил юридик кучга эга бўлган икки нусхада "
        "тузилди. Матнлар ўртасида тафовут юзага келган тақдирда, Томонлар ўзбек тилидаги "
        "матнга асосланадилар.",
        "Ушбу тилхат бир хил юридик кучга эга бўлган икки нусхада тузилди.",
    ),
}

# Строка «город … дата»: пробелы-распорка между ними.
DATE_LINES = ("г. {{ city }}", "{{ city_uz }} ш.")

# Абзац перед таблицей графика — не отрывать от неё.
SCHEDULE_HEADINGS = ("2. График платежей:", "2. Тўлов жадвали:")

# Блоки подписей: (первая фраза, последняя фраза) — всё между ними держится
# вместе на одной странице.
SIGNATURE_BLOCKS = (
    ("Содержание настоящей расписки мне понятно", "(от руки, печатными буквами)"),
    ("Кредитор: {{ creditor_name }}", "М.П."),
    ("Свидетель (по желанию Сторон):", "(от руки, печатными буквами)"),
    ("Ушбу тилхатнинг мазмуни менга тушунарли", "(ўз қўли билан, катта ҳарфлар билан)"),
    ("Кредитор: {{ creditor_name }}", "М.Ў. (муҳр ўрни)"),
    ("Гувоҳ (Томонларнинг хоҳишига кўра):", "(ўз қўли билан, катта ҳарфлар билан)"),
)


def _body_text(doc) -> str:
    return "".join(t.text or "" for t in doc.element.body.iter(qn("w:t")))


def _replace_placeholders(doc) -> None:
    runs = [r for p in doc.paragraphs for r in p.runs]
    for old, new, expected in REPLACEMENTS:
        found = 0
        for run in runs:
            if old in run.text:
                found += run.text.count(old)
                run.text = run.text.replace(old, new)
        if found != expected:
            raise SystemExit(f"не найдено: {old!r} — ожидалось {expected}, найдено {found}")
    left = [p.text for p in doc.paragraphs if "[" in p.text]
    if left:
        raise SystemExit(f"в бланке остались незаменённые [скобки]: {left}")


def _text_width(doc) -> int:
    s = doc.sections[0]
    return s.page_width - s.left_margin - s.right_margin


def _fix_date_lines(doc) -> None:
    width = _text_width(doc)
    fixed = 0
    for p in doc.paragraphs:
        if not p.runs or not p.runs[0].text.startswith(DATE_LINES):
            continue
        spacer = p.runs[1]
        if spacer.text.strip():
            raise SystemExit(f"строка даты изменилась: {p.text!r}")
        spacer.text = ""
        spacer._element.append(OxmlElement("w:tab"))
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        fixed += 1
    if fixed != len(DATE_LINES):
        raise SystemExit(f"строк «город … дата» {fixed}, ожидалось {len(DATE_LINES)}")


def _set_cell_text(cell, text: str, rpr) -> None:
    par = cell.paragraphs[0]
    for r in list(par.runs):
        r._element.getparent().remove(r._element)
    run = par.add_run(text)
    if rpr is not None:
        run._element.insert(0, copy.deepcopy(rpr))


def _schedule_rows(doc) -> None:
    if len(doc.tables) != 2:
        raise SystemExit(f"в бланке {len(doc.tables)} таблиц, ожидались 2 (RU и UZ)")
    for table in doc.tables:
        if len(table.rows) != 4:
            raise SystemExit("таблица графика изменилась: ожидались шапка + 3 строки")
        # Строки не рвутся пополам, шапка повторяется на следующей странице.
        # Порядок детей w:trPr: cantSplit — первым, раньше trHeight и
        # tblHeader. LibreOffice простит любой, Word строже к порядку.
        for row in table.rows:
            row._tr.get_or_add_trPr().insert(0, OxmlElement("w:cantSplit"))
        head_pr = table.rows[0]._tr.get_or_add_trPr()
        if head_pr.find(qn("w:tblHeader")) is None:
            head_pr.append(OxmlElement("w:tblHeader"))
        sample = table.rows[1].cells[0].paragraphs[0].runs
        rpr = sample[0]._element.find(qn("w:rPr")) if sample else None
        # Три строки-образца («1 | [дата 1] | [сумма 1] | [остаток 1]») →
        # открытие цикла, строка данных, закрытие. docxtpl удаляет строки
        # с {%tr %} целиком, остаётся по строке на платёж. Валюты в ячейках
        # нет — как в бланке: «сум» стоит у общей стоимости.
        loop_open, data, loop_close = table.rows[1], table.rows[2], table.rows[3]
        _set_cell_text(loop_open.cells[0], "{%tr for item in schedule %}", rpr)
        for i, key in enumerate(("item.number", "item.date", "item.amount", "item.balance")):
            _set_cell_text(data.cells[i], "{{ " + key + " }}", rpr)
        _set_cell_text(loop_close.cells[0], "{%tr endfor %}", rpr)
        for i in (1, 3):
            for cell in table.rows[i].cells[1:]:
                _set_cell_text(cell, "", rpr)


def _keep_together(doc) -> None:
    pars = doc.paragraphs
    headings = 0
    for p in pars:
        if p.text.strip().startswith(SCHEDULE_HEADINGS):
            p.paragraph_format.keep_with_next = True
            headings += 1
    if headings != len(SCHEDULE_HEADINGS):
        raise SystemExit(f"заголовков графика {headings}, ожидалось {len(SCHEDULE_HEADINGS)}")
    start = 0
    for first, last in SIGNATURE_BLOCKS:
        i = next((k for k in range(start, len(pars)) if pars[k].text.strip().startswith(first)), None)
        if i is None:
            raise SystemExit(f"блок подписи не найден: {first!r}")
        j = next((k for k in range(i, len(pars)) if pars[k].text.strip() == last), None)
        if j is None:
            raise SystemExit(f"конец блока подписи не найден: {last!r}")
        for k in range(i, j):
            pars[k].paragraph_format.keep_with_next = True
        start = j + 1


def _split(doc, parts: tuple[str, ...]) -> None:
    """Оставить в документе только нужную часть бланка.

    Граница — абзац с разрывом страницы перед «ТИЛХАТ». Он удаляется вместе
    с отрезанной частью: оставленный разрыв дал бы пустую последнюю (в
    русской) или первую (в узбекской) страницу. sectPr — последний ребёнок
    body, несёт размер листа и поля — остаётся всегда.
    """
    if parts == ("ru", "uz"):
        return
    body = doc.element.body
    children = [el for el in body.iterchildren() if el.tag != qn("w:sectPr")]
    breaks = [
        i for i, el in enumerate(children)
        if el.tag == qn("w:p")
        and any(br.get(qn("w:type")) == "page" for br in el.iter(qn("w:br")))
    ]
    if len(breaks) != 1:
        raise SystemExit(f"разрывов страницы в бланке {len(breaks)}, ожидался 1 (перед «ТИЛХАТ»)")
    cut = breaks[0]
    if "".join(t.text or "" for t in children[cut].iter(qn("w:t"))).strip():
        raise SystemExit("в абзаце с разрывом страницы есть текст — граница частей изменилась")
    drop = children[cut:] if parts == ("ru",) else children[: cut + 1]
    for el in drop:
        body.remove(el)
    head = _body_text(doc).lstrip()
    expected = "РАСПИСКА" if parts == ("ru",) else "ТИЛХАТ"
    if not head.startswith(expected):
        raise SystemExit(f"часть {parts[0]} начинается не с «{expected}»: {head[:40]!r}")


def _single_language_clause(doc, lang: str) -> None:
    old, new = LANGUAGE_CLAUSE[lang]
    runs = [r for p in doc.paragraphs for r in p.runs if r.text == old]
    if len(runs) != 1:
        raise SystemExit(f"пункт о языках ({lang}) не найден одним run: {len(runs)}")
    runs[0].text = new


def build(variant: str = "raspiska_ru_uz") -> Document:
    if variant not in VARIANTS:
        raise SystemExit(f"неизвестный вид: {variant}")
    parts = VARIANTS[variant]
    doc = Document(str(SOURCE))
    # Сначала — всё по полному бланку: счётчики REPLACEMENTS и блоки подписей
    # описаны для него, и проверка «ничего не потеряли» одна на все виды.
    _replace_placeholders(doc)
    _fix_date_lines(doc)
    _schedule_rows(doc)
    _keep_together(doc)
    _split(doc, parts)
    if len(parts) == 1:
        _single_language_clause(doc, parts[0])
    if "[" in _body_text(doc):
        raise SystemExit(f"{variant}: в шаблоне остались [скобки]")
    return doc


def main() -> None:
    for variant in VARIANTS:
        out = OUT_DIR / f"{variant}.docx"
        build(variant).save(str(out))
        print(f"собран {out}")


if __name__ == "__main__":
    main()
