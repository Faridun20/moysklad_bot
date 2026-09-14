"""Сборка .docx-шаблонов расписки и тилхата (`templates/legal/`).

Шаблон — двоичный файл: в диффе его не прочесть, правку не отревьюить, а
поправить «отступ у подписи» можно только открыв Word, которого на сервере нет.
Поэтому исходник шаблона — этот скрипт, а .docx лежит в репозитории как
результат его прогона:

    python -m scripts.build_legal_templates

Вид взят с образца расписки, который присылает руководство: заголовок
«Расписка» по центру обычным начертанием, город слева и дата справа ОДНОЙ
строкой, тело — Times New Roman 14 пт, по ширине, с абзацным отступом, сумма
отдельной жирной строкой, согласие должника — жирным, а подпись — линейка во
всю ширину с мелкой подписью под ней. Юридическое содержание при этом то же,
что было: ни один пункт не выброшен, преамбула лишь собрала в себя прежние
пункты 1 и 2 (кто у кого что получил и за сколько) — ровно так, как в образце.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Cm, Pt

OUT_DIR = Path(__file__).resolve().parent.parent / "templates" / "legal"

FONT = "Times New Roman"
BODY_PT = 14
TABLE_PT = 11
CAPTION_PT = 10
INDENT = Cm(1.25)

# Поля по ГОСТ Р 7.0.97: слева 3 см под подшивку, справа 1.5, сверху/снизу 2.
MARGINS = dict(left=Cm(3), right=Cm(1.5), top=Cm(2), bottom=Cm(2))


def _run(par, text: str, *, bold: bool = False, size: int = BODY_PT):
    run = par.add_run(text)
    run.bold = bold
    run.font.name = FONT
    run.font.size = Pt(size)
    # Кириллица берёт шрифт из w:cs/w:eastAsia, а не из w:ascii: без этого
    # LibreOffice подставляет свой дефолт и документ едет другим начертанием.
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), FONT)
    return run


def _par(doc, text="", *, align=None, indent=None, bold=False,
         size=BODY_PT, space_after=6, space_before=0, line=1.15):
    par = doc.add_paragraph()
    par.alignment = align
    fmt = par.paragraph_format
    fmt.first_line_indent = indent
    fmt.space_after = Pt(space_after)
    fmt.space_before = Pt(space_before)
    fmt.line_spacing = line
    if text:
        _run(par, text, bold=bold, size=size)
    return par


def _rule(doc):
    """Линейка для подписи во всю ширину строки.

    Это НЕ строка подчёркиваний: в образце линия ровная и доходит до правого
    поля, а подчёркивания зависят от ширины символа и от того, куда их
    перенесёт по месту. Рисуем нижней границей пустого абзаца.
    """
    par = _par(doc, "", space_after=2, space_before=18)
    ppr = par._element.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "12")       # 1.5 пт
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "000000")
    borders.append(bottom)
    ppr.append(borders)
    return par


def _city_date(doc, city_text: str, date_text: str):
    """Город слева, дата справа — одной строкой (правая табуляция у поля)."""
    par = _par(doc, "", space_after=24)
    width = Cm(21) - MARGINS["left"] - MARGINS["right"]
    par.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
    _run(par, f"{city_text}\t{date_text}")
    return par


def _schedule_table(doc, head: tuple[str, str, str, str]):
    table = doc.add_table(rows=4, cols=4)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    widths = (Cm(1.4), Cm(4.2), Cm(5.4), Cm(5.5))

    def fill(row, cells, *, bold=False):
        for cell, text, width in zip(row.cells, cells, widths, strict=True):
            cell.width = width
            par = cell.paragraphs[0]
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
            par.paragraph_format.space_after = Pt(2)
            par.paragraph_format.line_spacing = 1
            if text:
                _run(par, text, bold=bold, size=TABLE_PT)

    fill(table.rows[0], head, bold=True)
    # Строки цикла docxtpl: `{%tr %}` съедает строку таблицы целиком.
    fill(table.rows[1], ("{%tr for item in schedule %}", "", "", ""))
    fill(table.rows[2], (
        "{{ item.number }}",
        "{{ item.date }}",
        "{{ item.amount }} {{ currency }}",
        "{{ item.balance }} {{ currency }}",
    ))
    fill(table.rows[3], ("{%tr endfor %}", "", "", ""))
    return table


def _base_document() -> Document:
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = MARGINS["left"]
    section.right_margin = MARGINS["right"]
    section.top_margin = MARGINS["top"]
    section.bottom_margin = MARGINS["bottom"]
    return doc


def _signature(doc, *, label: str, name: str, caption: str, tail: str = ""):
    # keep_with_next на всём блоке: подпись, оторванная от линейки переносом
    # страницы, — это документ, который подписывают не глядя на то, под чем.
    pars = [_par(doc, label, space_after=2, space_before=12)]
    if name:
        pars.append(_par(doc, name, space_after=0))
    pars.append(_rule(doc))
    _par(doc, caption + tail, size=CAPTION_PT, space_after=6, indent=INDENT)
    for par in pars:
        par.paragraph_format.keep_with_next = True


def build_ru() -> Document:
    doc = _base_document()
    _par(doc, "Расписка", align=WD_ALIGN_PARAGRAPH.CENTER, space_after=28)
    _city_date(doc, "г. {{ city }}", "{{ document_date }} г.")

    j = WD_ALIGN_PARAGRAPH.JUSTIFY
    _par(doc,
         "Я, {{ debtor_full_name }}, {{ debtor_birth_date }} года рождения "
         "(паспорт: {{ debtor_passport }}, ПИНФЛ: {{ debtor_pinfl }}), "
         "проживающий(ая) и зарегистрированный(ая) по адресу: "
         "{{ debtor_address }}, телефон: {{ debtor_phone }}, получил(а) от",
         align=j, indent=INDENT)
    _par(doc,
         "{{ creditor_name }} (ИНН: {{ creditor_tin }}, адрес: "
         "{{ creditor_address }}) следующий товар: {{ product_name }}, "
         "общей стоимостью",
         align=j, indent=INDENT)
    _par(doc,
         "{{ total_amount }} ({{ total_amount_words }}) {{ currency }},",
         align=j, indent=INDENT, bold=True)
    _par(doc, "и обязуюсь оплатить его на следующих условиях:",
         align=j, indent=INDENT)

    _par(doc,
         "1. Платежи определены в {{ currency }}; оплата производится в сумах "
         "Республики Узбекистан по официальному курсу Центрального банка "
         "Республики Узбекистан на день платежа.", align=j, indent=INDENT)
    _par(doc,
         "2. Я обязуюсь полностью погасить указанную задолженность в период "
         "с {{ start_date }} по {{ end_date }}.", align=j, indent=INDENT)
    _par(doc,
         "3. {{ payment_clause }} Платёж(и) производятся согласно следующему "
         "графику:", align=j, indent=INDENT)
    _schedule_table(doc, ("№", "Дата платежа", "Сумма платежа", "Остаток долга"))
    _par(doc, "Каждый платёж должен быть произведён не позднее даты, указанной "
              "в графике.", align=j, indent=INDENT, space_before=8)
    _par(doc,
         "4. В случае нарушения срока платежа я обязуюсь уплатить пеню в "
         "размере {{ penalty_rate }} % от неоплаченной суммы за каждый день "
         "просрочки.", align=j, indent=INDENT)
    _par(doc,
         "5. Право собственности на товар сохраняется за {{ creditor_name }} "
         "до полной оплаты задолженности (статья 424 Гражданского кодекса "
         "Республики Узбекистан).", align=j, indent=INDENT)
    _par(doc,
         "6. До момента полной оплаты указанный товар находится в залоге в "
         "пользу {{ creditor_name }} (статья 421 Гражданского кодекса "
         "Республики Узбекистан). Я не вправе продавать, дарить, закладывать "
         "или иным образом отчуждать товар третьим лицам.",
         align=j, indent=INDENT)
    _par(doc,
         "7. При нарушении срока платежа более чем на {{ grace_days }} дней "
         "{{ creditor_name }} вправе досрочно потребовать погашения "
         "оставшейся части задолженности либо изъять товар.",
         align=j, indent=INDENT)

    _par(doc,
         "Содержание настоящей расписки мне понятно, указанные в ней сведения "
         "соответствуют действительности, расписку подписал(а) добровольно, "
         "без какого-либо принуждения.",
         align=j, indent=INDENT, bold=True, space_before=12)

    caption = "(подпись, фамилия, имя отчество, полностью)"
    _signature(doc, label="Должник:", name="{{ debtor_full_name }}", caption=caption)
    _signature(doc, label="Представитель кредитора:",
               name="{{ creditor_representative }}", caption=caption, tail="   М.П.")
    _par(doc, "{%p if witness_name %}", space_after=0)
    _signature(doc, label="Свидетель:", name="{{ witness_name }}", caption=caption)
    _par(doc, "{%p endif %}", space_after=0)
    return doc


def build_uz() -> Document:
    doc = _base_document()
    _par(doc, "Тилхат", align=WD_ALIGN_PARAGRAPH.CENTER, space_after=28)
    _city_date(doc, "{{ city }} шаҳри", "{{ document_date }} й.")

    j = WD_ALIGN_PARAGRAPH.JUSTIFY
    _par(doc,
         "Мен, {{ debtor_full_name }}, {{ debtor_birth_date }} йилда туғилган "
         "(паспорт: {{ debtor_passport }}, ЖШШИР: {{ debtor_pinfl }}), "
         "яшаш манзили: {{ debtor_address }}, телефон рақами: "
         "{{ debtor_phone }}, қуйидаги шахсдан:", align=j, indent=INDENT)
    _par(doc,
         "{{ creditor_name }} (СТИР: {{ creditor_tin }}, манзил: "
         "{{ creditor_address }}) қуйидаги товарни қабул қилиб олдим: "
         "{{ product_name }}, умумий қиймати", align=j, indent=INDENT)
    _par(doc,
         "{{ total_amount }} ({{ total_amount_words }}) {{ currency }},",
         align=j, indent=INDENT, bold=True)
    _par(doc, "ва уни қуйидаги шартлар асосида тўлаш мажбуриятини оламан:",
         align=j, indent=INDENT)

    _par(doc,
         "1. Тўловлар {{ currency }} да белгиланган бўлиб, тўлов Ўзбекистон "
         "Республикаси сўмида, тўлов кунидаги Ўзбекистон Республикаси Марказий "
         "банкининг расмий курси бўйича амалга оширилади.", align=j, indent=INDENT)
    _par(doc,
         "2. Мен мазкур қарзни {{ start_date }} дан бошлаб {{ end_date }} гача "
         "тўлиқ тўлаш мажбуриятини оламан.", align=j, indent=INDENT)
    _par(doc,
         "3. {{ payment_clause }} Тўлов(лар) қуйидаги жадвалга мувофиқ амалга "
         "оширилади:", align=j, indent=INDENT)
    _schedule_table(doc, ("№", "Тўлов санаси", "Тўлов суммаси", "Қолдиқ қарз"))
    _par(doc, "Ҳар бир тўлов жадвалда кўрсатилган санадан кечиктирмасдан "
              "амалга оширилиши шарт.", align=j, indent=INDENT, space_before=8)
    _par(doc,
         "4. Тўлов муддати бузилган тақдирда, мен кечиктирилган ҳар бир кун "
         "учун тўланмаган сумманинг {{ penalty_rate }} % миқдорида пеня тўлаш "
         "мажбуриятини оламан.", align=j, indent=INDENT)
    _par(doc,
         "5. Товарга бўлган мулк ҳуқуқи қарз тўлиқ тўланганга қадар "
         "{{ creditor_name }} да сақланиб қолади (Ўзбекистон Республикаси "
         "Фуқаролик кодексининг 424-моддаси).", align=j, indent=INDENT)
    _par(doc,
         "6. Тўлиқ тўлов амалга оширилгунга қадар мазкур товар "
         "{{ creditor_name }} фойдасига гаровда ҳисобланади (Ўзбекистон "
         "Республикаси Фуқаролик кодексининг 421-моддаси). Мен товарни учинчи "
         "шахсларга сотиш, ҳадя қилиш, гаровга қўйиш ёки бошқа тарзда "
         "бегоналаштириш ҳуқуқига эга эмасман.", align=j, indent=INDENT)
    _par(doc,
         "7. Тўлов муддати {{ grace_days }} кундан ортиқ бузилган тақдирда, "
         "{{ creditor_name }} қарзнинг қолган қисмини муддатидан олдин тўлиқ "
         "талаб қилиш ёки товарни қайтариб олиш ҳуқуқига эга.",
         align=j, indent=INDENT)

    _par(doc,
         "Ушбу тилхат мазмуни менга тушунарли, унда кўрсатилган маълумотлар "
         "ҳақиқатга мос ва мен уни ўз ихтиёрим билан, ҳеч қандай мажбурлашсиз "
         "имзоладим.", align=j, indent=INDENT, bold=True, space_before=12)

    caption = "(имзо, фамилия, исм, отасининг исми тўлиқ)"
    _signature(doc, label="Қарздор:", name="{{ debtor_full_name }}", caption=caption)
    _signature(doc, label="Қарз берувчи вакили:",
               name="{{ creditor_representative }}", caption=caption, tail="   М.Ў.")
    _par(doc, "{%p if witness_name %}", space_after=0)
    _signature(doc, label="Гувоҳ:", name="{{ witness_name }}", caption=caption)
    _par(doc, "{%p endif %}", space_after=0)
    return doc


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, build in (("raspiska_ru.docx", build_ru), ("tilxat_uz.docx", build_uz)):
        path = OUT_DIR / name
        build().save(str(path))
        print(f"собран {path}")


if __name__ == "__main__":
    main()
