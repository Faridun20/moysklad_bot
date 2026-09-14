"""Сборка шаблона двуязычной расписки RU+UZ (`templates/legal/raspiska_ru_uz.docx`).

В отличие от `build_legal_templates.py`, текст здесь не пишется кодом: это
бланк юриста (`templates/legal/src/raspiska_ru_uz_source.docx`), и
формулировки в нём трогать нельзя. Скрипт только превращает бланк в шаблон
docxtpl и чинит вёрстку под LibreOffice, которым бот делает PDF:

* `[скобки]` — место для данных компании, товара и сроков — становятся
  переменными контекста (`services/legal_docs.build_context`). Строки с чертой
  `____` остаются: их Должник заполняет от руки, так задумано бланком.
* Таблица графика — строка-шаблон `{%tr for item in schedule %}` вместо трёх
  пустых строк: платежей бывает и два, и двенадцать.
* Город и дата в бланке разнесены пробелами — в LibreOffice дата переносится
  на вторую строку. Ставим табуляцию к правому полю.
* Заголовок графика не отрывается от таблицы, блоки подписей не рвутся
  между страницами.

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
OUT = ROOT / "templates" / "legal" / "raspiska_ru_uz.docx"

# Фраза бланка → шаблон. Замена внутри одного run: в бланке каждая [скобка]
# лежит целиком в своём run, форматирование (курсив названия) сохраняется.
# Счётчик — сколько раз фраза обязана встретиться: молча не заменённая скобка
# ушла бы клиенту в документе как «[ИНН]».
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
    ("[сана]дан [сана]гача", "{{ start_date }} йилдан {{ end_date }} йилгача", 1),
    ("[вакилнинг Ф.И.Ш.]", "{{ creditor_representative }}", 1),
    ("Лавозими: [лавозими]", "Лавозими: {{ creditor_position_uz }}", 1),
]

# Строка «город … дата»: пробелы-распорка между ними.
DATE_LINES = ("г. {{ city }}", "{{ city_uz }} ш.")

# Абзац перед таблицей графика — не отрывать от неё.
SCHEDULE_HEADINGS = ("3. График платежей:", "3. Тўлов жадвали:")

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
    for p in doc.paragraphs:
        if not p.runs or not p.runs[0].text.startswith(DATE_LINES):
            continue
        spacer = p.runs[1]
        if spacer.text.strip():
            raise SystemExit(f"строка даты изменилась: {p.text!r}")
        spacer.text = ""
        spacer._element.append(OxmlElement("w:tab"))
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)


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
        # Порядок детей w:trPr задан схемой (cantSplit раньше trHeight и
        # tblHeader): LibreOffice простит любой, Word откроет файл с «ошибкой».
        for row in table.rows:
            row._tr.get_or_add_trPr().insert(0, OxmlElement("w:cantSplit"))
        head_pr = table.rows[0]._tr.get_or_add_trPr()
        if head_pr.find(qn("w:tblHeader")) is None:
            head_pr.append(OxmlElement("w:tblHeader"))
        sample = table.rows[1].cells[0].paragraphs[0].runs
        rpr = sample[0]._element.find(qn("w:rPr")) if sample else None
        loop_open, data, loop_close = table.rows[1], table.rows[2], table.rows[3]
        _set_cell_text(loop_open.cells[0], "{%tr for item in schedule %}", rpr)
        for i, key in enumerate(("item.number", "item.date", "item.amount", "item.balance")):
            _set_cell_text(data.cells[i], "{{ " + key + " }}" + (" USD" if i >= 2 else ""), rpr)
        _set_cell_text(loop_close.cells[0], "{%tr endfor %}", rpr)
        for i in (1, 3):
            for cell in table.rows[i].cells[1:]:
                _set_cell_text(cell, "", rpr)


def _keep_together(doc) -> None:
    pars = doc.paragraphs
    for p in pars:
        if p.text.strip().startswith(SCHEDULE_HEADINGS):
            p.paragraph_format.keep_with_next = True
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


def build() -> Document:
    doc = Document(str(SOURCE))
    _replace_placeholders(doc)
    _fix_date_lines(doc)
    _schedule_rows(doc)
    _keep_together(doc)
    return doc


def main() -> None:
    build().save(str(OUT))
    print(f"собран {OUT}")


if __name__ == "__main__":
    main()
