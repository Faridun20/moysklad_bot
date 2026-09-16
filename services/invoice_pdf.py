"""
Печатные формы склада и продаж: HTML → weasyprint. ОДИН движок на все бумаги.

* **Счёт на оплату** (`build_sales_invoice_html`) — клиенту ДО отгрузки
  (`services/sales_invoice.py`);
* **Товарная накладная** (`build_invoice_html`, расходная накладная склада) —
  сопровождает отгрузку;
* **Приходная накладная** (тот же `build_invoice_html`, `type="incoming"`) —
  внутренний документ приёмки.

Вид — по бланкам владельца («Счёт на оплату», «Накладная», 2026-09): название
компании тёмно-синим жирным слева, заголовок документа золотыми капителями
справа, золотые подписи блоков, две колонки сторон, тёмно-синяя шапка таблицы
с «зеброй», тёмная полоса итога, сумма прописью, подписи и серая оговорка про
ЭСФ/ЭТТН. Цвета и кегли сняты с .docx бланков (`_CSS`). Счёт и накладная
собираются из ОБЩИХ кусков (`_head_html`, `_parties_html`, `_items_table_html`,
`_sign_html`): бумаги одной компании обязаны выглядеть одной семьёй, а второй
набор стилей разошёлся бы с первым на первой же правке.

**Язык — выбор при печати** (как у расписки): `ru_uz` (обе страницы в одном
PDF), `ru`, `uz`. Узбекский текст взят ДОСЛОВНО со второй страницы бланков,
машинного перевода здесь нет. Приходная накладная — внутренняя, только рус.

**Валюта — валюта заказа/накладной.** Долларовый заказ печатается в USD
(«Цена за ед. (USD)», «(четыреста) долларов США»), сумовый — в сумах, целыми.
Числа — по-русски («1 092,00»). НДС: компания не плательщик — строка «Без НДС».

Разделение намеренное: `build_*_html` — чистые функции без БД (их и
тестируем), `render_*_pdf` — тонкие обёртки, импортирующие weasyprint ЛЕНИВО:
он тянет системные pango/cairo, и без них должен падать только рендер, а не
импорт модуля (его тянет webapp целиком). Данные из БД (реквизиты, покупатель,
основание) собирает вызывающий: `sales_invoice.build_sales_invoice` и
`waybill.prepare_invoice`.

Логотип необязателен: путь из env `INVOICE_LOGO_PATH`, без файла документ
печатается без картинки.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path

from services import money
from services.numerals import amount_in_words, money_in_words
from utils.helpers import esc

logger = logging.getLogger(__name__)

COMPANY_NAME = os.environ.get("COMPANY_NAME", "FARID IMPEKS LLC")

# Язык печати — выбор человека при печати/отправке (запоминается в user_prefs).
DOC_LANGS: tuple[str, ...] = ("ru_uz", "ru", "uz")
DEFAULT_DOC_LANG = "ru_uz"
DOC_LANG_LABELS = {"ru_uz": "Рус + Узб", "ru": "Рус", "uz": "Узб"}


def normalize_lang(raw: object) -> str | None:
    """`ru_uz`/`ru`/`uz` или None — прислали что-то другое."""
    value = str(raw or "").strip().lower()
    return value if value in DOC_LANGS else None


def lang_parts(lang: object) -> tuple[str, ...]:
    value = normalize_lang(lang) or DEFAULT_DOC_LANG
    return ("ru", "uz") if value == "ru_uz" else (value,)


# Кэш data-URI логотипа: файл на диске не меняется в течение жизни процесса.
_logo_cache: tuple[str, str | None] | None = None


def _logo_data_uri() -> str | None:
    """Логотип как data-URI, либо None. Ошибки чтения не фатальны."""
    global _logo_cache
    path = (os.environ.get("INVOICE_LOGO_PATH") or "").strip()
    if not path:
        return None
    if _logo_cache is not None and _logo_cache[0] == path:
        return _logo_cache[1]

    uri: str | None = None
    try:
        raw = Path(path).read_bytes()
        mime = mimetypes.guess_type(path)[0] or "image/png"
        uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    except OSError as e:
        # Именно предупреждение: документ выписывается и без лого.
        logger.warning("Логотип накладной не прочитан (%s): %s", path, e)
    _logo_cache = (path, uri)
    return uri


# ─── Форматы ─────────────────────────────────────────────────────────────────


def _fmt_qty(value) -> str:
    """Количество по-русски и без хвостовых нулей: 3, а не 3.0; 2,5 — с запятой.

    Через `:g` было два изъяна: дробная часть печаталась по-английски («2.5»),
    а количество от миллиона уходило в экспоненциальный вид («1e+06»).
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{f:.3f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


def _fmt_money(cents: int | None, currency: str) -> str:
    """«1 092,00» для USD; сумы — целыми («1 092 000»), тийины — только если есть."""
    if cents is None:
        return "—"
    c = int(cents)
    whole_only = (currency or "").upper() == "UZS" and c % 100 == 0
    return money.format_cents(c, decimals=0 if whole_only else 2)


_MONTHS = {
    "ru": ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"),
    "uz": ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
           "сентябрь", "октябрь", "ноябрь", "декабрь"),
}


def _date_parts(raw: object) -> tuple[str, str, str] | None:
    """`2026-09-16 14:05` или `16.09.2026` → ('16', '09', '2026'); иначе None."""
    text = str(raw or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        y, m, d = text[:4], text[5:7], text[8:10]
    elif len(text) >= 10 and text[2] == "." and text[5] == ".":
        d, m, y = text[:2], text[3:5], text[6:10]
    else:
        return None
    if not (y.isdigit() and m.isdigit() and d.isdigit()) or not 1 <= int(m) <= 12:
        return None
    return d, m, y


def date_long(raw: object, lang: str, *, suffix: bool = True) -> str:
    """«16» сентября 2026 г. / «16» сентябрь 2026 й. — как в строке номера бланка."""
    parts = _date_parts(raw)
    if parts is None:
        return esc(str(raw or ""))
    d, m, y = parts
    month = _MONTHS[lang][int(m) - 1]
    tail = (" г." if lang == "ru" else " й.") if suffix else ""
    return f"«{d}» {month} {y}{tail}"


# Единица измерения на узбекской странице: «шт» по-узбекски — «дона».
# Остальные сокращения (кг, м, л) в обоих языках пишутся одинаково.
_UNIT_UZ = {"шт": "дона", "шт.": "дона", "штук": "дона", "комплект": "тўплам", "компл": "тўплам"}


def _unit(raw: object, lang: str) -> str:
    unit = str(raw or "").strip()
    if lang == "uz":
        return _UNIT_UZ.get(unit.lower(), unit)
    return unit


def _blank(value: object, width: str = "wide") -> str:
    """Значение или черта для записи от руки: пустой реквизит не выкидываем
    строкой — бумагу дописывают ручкой."""
    text = str(value or "").strip()
    return esc(text) if text else f'<span class="blank blank--{width}"></span>'


# ─── Тексты ──────────────────────────────────────────────────────────────────
#
# Русские и узбекские формулировки — ДОСЛОВНО с бланков владельца (стр. 1 и 2).
# Отступления по решению владельца: «в т.ч. НДС (12%)» → «Без НДС» / «ҚҚСсиз»
# (компания не плательщик НДС); «Основание: Счёт № …» называет документ так,
# как он называется в системе и в заголовке, — «Счёт на оплату» / «Тўлов учун ҳисоб».

_T: dict[str, dict[str, str]] = {
    "ru": {
        "invoice_title": "Счёт на оплату",
        "waybill_title": "Товарная накладная",
        "number_from": "№ {n} от {date}",
        "supplier": "Поставщик",
        "buyer": "Покупатель",
        "sender": "Грузоотправитель",
        "receiver": "Грузополучатель",
        "tin_oked": "ИНН: {tin}&nbsp;&nbsp;&nbsp;ОКЭД: {oked}",
        "account": "Р/с: {v}",
        "bank_mfo": "Банк: {bank}&nbsp;&nbsp;&nbsp;МФО: {mfo}",
        "address": "Адрес: {v}",
        "phone": "Тел.: {v}",
        "buyer_tin": "ИНН / ПИНФЛ: {v}",
        "col_no": "№",
        "col_name": "Наименование товара",
        "col_unit": "Ед. изм.",
        "col_qty": "Кол-во",
        "col_price": "Цена за ед. ({cur})",
        "col_sum": "Сумма ({cur})",
        "cur_UZS": "сум",
        "subtotal": "Итого:",
        "no_vat": "Без НДС",
        "to_pay": "Всего к оплате:",
        "released": "Всего отпущено на сумму:",
        "valid": "Счёт действителен для оплаты в течение {days} с даты выставления.",
        "director": "Руководитель:",
        "director_caption": "подпись, Ф.И.О. — {name}",
        "stamp": "М.П.",
        "accountant": "Гл. бухгалтер:",
        "accountant_caption": "подпись, Ф.И.О. — {name}",
        "invoice_note": "Настоящий счёт — коммерческий документ на оплату; он не заменяет "
                        "электронный счёт-фактуру (ЭСФ), формируемую отдельно в установленном порядке.",
        "basis": "Основание: Счёт на оплату № {n} от {date}",
        "release_by": "Отпуск разрешил:",
        "release_caption": "должность, Ф.И.О.",
        "released_by": "Отпустил:",
        "released_by_caption": "Ф.И.О.",
        "received_by": "Груз получил:",
        "received_caption": "Ф.И.О. получателя&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
                            "Дата получения: «___» _________ 20__ г.",
        "waybill_note": "Настоящая накладная — товаросопроводительный документ; она не заменяет "
                        "электронную товарно-транспортную накладную (ЭТТН), оформляемую в установленном порядке.",
        "cancelled": "НАКЛАДНАЯ ОТМЕНЕНА",
    },
    "uz": {
        "invoice_title": "Тўлов учун ҳисоб",
        "waybill_title": "Товар накладноси",
        "number_from": "№ {n} {date}",
        "supplier": "Етказиб берувчи",
        "buyer": "Харидор",
        "sender": "Юк жўнатувчи",
        "receiver": "Юк олувчи",
        "tin_oked": "СТИР: {tin}&nbsp;&nbsp;&nbsp;ИФУТ: {oked}",
        "account": "Ҳисобварақ: {v}",
        "bank_mfo": "Банк: {bank}&nbsp;&nbsp;&nbsp;МФО: {mfo}",
        "address": "Манзил: {v}",
        "phone": "Тел.: {v}",
        "buyer_tin": "СТИР / ПИНФЛ: {v}",
        "col_no": "№",
        "col_name": "Товар номи",
        "col_unit": "Ўлчов бирлиги",
        "col_qty": "Миқдори",
        "col_price": "Нархи ({cur})",
        "col_sum": "Суммаси ({cur})",
        "cur_UZS": "сўм",
        "subtotal": "Жами:",
        "no_vat": "ҚҚСсиз",
        "to_pay": "Тўлашга жами:",
        "released": "Жами топширилди:",
        "valid": "Ушбу ҳисоб тақдим этилган кундан бошлаб {days} давомида тўлов учун амал қилади.",
        "director": "Раҳбар:",
        "director_caption": "имзо, Ф.И.Ш. — {name}",
        "stamp": "М.Ў.",
        "accountant": "Бош бухгалтер:",
        "accountant_caption": "имзо, Ф.И.Ш. — {name}",
        "invoice_note": "Ушбу ҳисоб — тўлов учун тижорат ҳужжати; у белгиланган тартибда алоҳида "
                        "расмийлаштириладиган электрон ҳисобварақ-фактура (ЭҲФ)ни алмаштирмайди.",
        "basis": "Асос: Тўлов учун ҳисоб № {n} {date} йилги",
        "release_by": "Юк беришга рухсат берди:",
        "release_caption": "лавозими, Ф.И.Ш.",
        "released_by": "Топширди:",
        "released_by_caption": "Ф.И.Ш.",
        "received_by": "Юкни қабул қилди:",
        "received_caption": "қабул қилувчининг Ф.И.Ш.&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
                            "Қабул қилинган сана: «___» _________ 20__ й.",
        "waybill_note": "Ушбу ҳужжат товар ҳамроҳлик ҳужжати; у белгиланган тартибда "
                        "расмийлаштириладиган электрон товар-транспорт накладнойси (ЭТТН)ни алмаштирмайди.",
        "cancelled": "НАКЛАДНОЙ БЕКОР ҚИЛИНГАН",
    },
}


def _cur_label(currency: str, lang: str) -> str:
    code = (currency or "").upper()
    return _T[lang]["cur_UZS"] if code == "UZS" else esc(code or "USD")


def bank_days(days: int, lang: str) -> str:
    """«3 банковских дней» / «1 банковского дня»; по-узбекски не склоняется."""
    if lang == "uz":
        return f"{days} банк куни"
    n = days % 100
    if n % 10 == 1 and n != 11:
        return f"{days} банковского дня"
    return f"{days} банковских дней"


def _words_line(label: str, total_cents: int, currency: str, lang: str) -> str:
    """«Всего к оплате: 1 092 000 (один миллион девяносто две тысячи) сум.»"""
    digits = _fmt_money(total_cents, currency)
    value = money.from_cents(int(total_cents))
    try:
        words, tail = money_in_words(value, lang, currency)
    except ValueError:
        # Валюты нет в словаре прописи — число прописью и код валюты.
        words, tail = amount_in_words(value, lang), str(currency or "")
    return (
        f'<p class="words"><b>{label}</b> '
        f'<span class="words-val">{digits} ({esc(words)}) {esc(tail)}.</span></p>'
    )


# ─── CSS: одна таблица стилей на все документы ───────────────────────────────
#
# Значения сняты с бланков владельца (.docx): поля 17,6 × 22,9 мм, тёмно-синий
# #1F3864, золото #B8860B, текст #404040, подписи #8C8C8C, «зебра» #F2F2F2,
# линии таблицы #D9D9D9; кегли 17 / 12 / 11 / 10,5 / 10 / 9 / 8,5 / 8 pt,
# отступы ячеек 1,76 × 2,47 мм, межстрочный 1,1. Шрифт — DejaVu Sans (стоит в
# образе; бланк владельца отрисован им же).

_CSS = """
@page { size: A4; margin: 17.6mm 22.9mm; }
* { box-sizing: border-box; }
body { font-family: "DejaVu Sans", "Liberation Sans", sans-serif; font-size: 10.5pt;
       color: #404040; line-height: 1.1; margin: 0; }
.doc + .doc { page-break-before: always; }
p { margin: 0; }
table { border-collapse: collapse; width: 100%; }

.head td { padding: 1.76mm 2.47mm; vertical-align: middle; }
.head .left { width: 57%; }
.head .right { white-space: nowrap; }
.logo { max-height: 18mm; max-width: 50mm; display: block; margin-bottom: 2mm; }
.company { font-size: 17pt; font-weight: bold; color: #1F3864; line-height: 1.1; }
.doc-title { text-align: right; font-size: 10pt; font-weight: bold; color: #B8860B;
             text-transform: uppercase; margin-bottom: 2pt; }
.doc-number { text-align: right; font-size: 12pt; font-weight: bold; color: #1F3864; }
.rule { border-bottom: 1pt solid #1F3864; height: 14pt; margin: 6pt 0 19pt; }

.basis { font-style: italic; font-size: 10pt; margin: -6pt 0 15pt; }

.parties td { width: 50%; padding: 1.76mm 2.47mm; vertical-align: top; }
.caption { font-size: 9pt; font-weight: bold; color: #B8860B; text-transform: uppercase;
           margin-bottom: 4pt; }
.line { margin-bottom: 2pt; }
.blank { display: inline-block; border-bottom: 0.6pt solid #404040; height: 9pt;
         vertical-align: baseline; }
.blank--wide { width: 40mm; }
.blank--short { width: 22mm; }
.gap { height: 15pt; }

.items { border: 0.5pt solid #D9D9D9; }
.items th, .items td { border: 0.5pt solid #D9D9D9; padding: 1.76mm 2.47mm; vertical-align: middle; }
.items th { background: #1F3864; color: #fff; font-weight: bold; font-size: 10pt; text-align: center; }
.items td { font-style: italic; font-size: 10.5pt; }
.items tbody tr:nth-child(even) td { background: #F2F2F2; }
.items .c { text-align: center; }
.items .r { text-align: right; }
.items .nowrap { white-space: nowrap; }
.items tfoot td { font-style: normal; background: #F2F2F2; text-align: right; }
.items tfoot tr.grand td { background: #1F3864; color: #fff; font-weight: bold; font-size: 12pt; }
.items thead { display: table-header-group; }
.items tr { page-break-inside: avoid; }

.words { margin: 13pt 0 5pt; font-size: 11pt; }
.words .words-val { font-weight: bold; color: #1F3864; }
.valid { font-size: 10pt; margin-bottom: 20pt; }
.after-words { height: 15pt; }

.sign { page-break-inside: avoid; }
.sign-line { font-size: 10.5pt; margin-bottom: 2pt; }
.sign-line b { color: #1F3864; }
.sign-caption { font-size: 8.5pt; font-style: italic; color: #8C8C8C; margin-bottom: 13pt; }
.stamp { display: inline-block; margin-left: 22mm; }
.note { font-size: 8pt; font-style: italic; color: #8C8C8C; line-height: 1.5;
        margin-top: 13pt; padding-bottom: 6pt; border-bottom: 1pt solid #D9D9D9; }
.cancelled { color: #b00; font-weight: bold; text-align: right; margin-top: 4pt; }
.comment { margin-top: 8pt; font-size: 9pt; }
"""

_UNDERSCORE = "______________________________"


def _page(body: str) -> str:
    return (
        f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><style>{_CSS}</style>'
        f"</head><body>{body}</body></html>"
    )


# ─── Общие куски ─────────────────────────────────────────────────────────────


def _head_html(company_name: str, title: str, number_line: str, logo_data_uri: str | None) -> str:
    logo = f'<img class="logo" src="{logo_data_uri}" alt="">' if logo_data_uri else ""
    return (
        '<table class="head"><tr>'
        f'<td class="left">{logo}<div class="company">{esc(company_name)}</div></td>'
        f'<td class="right"><div class="doc-title">{title}</div><div class="doc-number">{number_line}</div></td>'
        '</tr></table><div class="rule"></div>'
    )


def _parties_html(left_caption: str, left_lines: list[str], right_caption: str, right_lines: list[str]) -> str:
    def cell(caption: str, lines: list[str]) -> str:
        rows = "".join(f'<div class="line">{ln}</div>' for ln in lines)
        return f'<td><div class="caption">{caption}</div>{rows}</td>'

    return (
        f'<table class="parties"><tr>{cell(left_caption, left_lines)}{cell(right_caption, right_lines)}'
        '</tr></table><div class="gap"></div>'
    )


def _items_table_html(lines: list[dict], currency: str, lang: str, foot_rows: list[tuple[str, str, str]]) -> str:
    """Таблица позиций. `lines` — {product_name, unit, quantity, price_cents,
    amount_cents}; `foot_rows` — (класс строки, подпись, значение)."""
    t = _T[lang]
    cur = _cur_label(currency, lang)
    head = (
        "<thead><tr>"
        f'<th style="width:5.4%">{t["col_no"]}</th>'
        f'<th style="width:26.9%">{t["col_name"]}</th>'
        f'<th style="width:9.7%">{t["col_unit"]}</th>'
        f'<th style="width:9.7%">{t["col_qty"]}</th>'
        f'<th style="width:22.6%">{t["col_price"].format(cur=cur)}</th>'
        f'<th style="width:25.7%">{t["col_sum"].format(cur=cur)}</th>'
        "</tr></thead>"
    )
    body = []
    for i, ln in enumerate(lines, 1):
        body.append(
            "<tr>"
            f'<td class="c">{i}</td>'
            f"<td>{esc(ln.get('product_name'))}</td>"
            f'<td class="c">{esc(_unit(ln.get("unit"), lang))}</td>'
            f'<td class="c">{_fmt_qty(ln.get("quantity"))}</td>'
            f'<td class="r nowrap">{_fmt_money(ln.get("price_cents"), currency)}</td>'
            f'<td class="r nowrap">{_fmt_money(ln.get("amount_cents"), currency)}</td>'
            "</tr>"
        )
    foot = "".join(
        f'<tr class="{cls}"><td colspan="5">{label}</td><td class="nowrap">{value}</td></tr>'
        for cls, label, value in foot_rows
    )
    return f'<table class="items">{head}<tbody>{"".join(body)}</tbody><tfoot>{foot}</tfoot></table>'


def _sign_html(label: str, caption: str, stamp: str = "") -> str:
    stamp_html = f'<span class="stamp">{stamp}</span>' if stamp else ""
    return (
        '<div class="sign">'
        f'<p class="sign-line"><b>{label}</b> {_UNDERSCORE}</p>'
        f'<p class="sign-caption">{caption}{stamp_html}</p>'
        "</div>"
    )


def _name_or_blank(value: object) -> str:
    text = str(value or "").strip()
    return esc(text) if text else "__________________"


def _line_items(items: list[dict]) -> list[dict]:
    """Позиции накладной → строки таблицы (сумма строки — `money.mul_qty`,
    как в счёте: документы по одному заказу сходятся до цента)."""
    out = []
    for it in items:
        price = it.get("price_cents")
        price_cents = None if price is None else int(price)
        qty = it.get("quantity") or 0
        out.append({
            "product_name": it.get("product_name"),
            "unit": it.get("unit") or "",
            "quantity": qty,
            "price_cents": price_cents,
            "amount_cents": None if price_cents is None else money.mul_qty(price_cents, qty),
        })
    return out


# ─── Счёт на оплату ──────────────────────────────────────────────────────────


def _sales_invoice_part(doc: dict, lang: str, logo_data_uri: str | None) -> str:
    t = _T[lang]
    company = doc.get("company") or {}
    currency = str(doc.get("currency") or "USD").upper()
    total = int(doc.get("total_cents") or 0)
    date_src = doc.get("date_iso") or doc.get("date")
    head = _head_html(
        str(company.get("company_name") or COMPANY_NAME),
        t["invoice_title"],
        t["number_from"].format(n=esc(doc.get("number")), date=date_long(date_src, lang)),
        logo_data_uri,
    )
    supplier = [
        _blank(company.get("company_name") or COMPANY_NAME),
        t["tin_oked"].format(tin=_blank(company.get("company_tin"), "short"),
                             oked=_blank(company.get("company_oked"), "short")),
        t["account"].format(v=_blank(company.get("company_bank_account"))),
        t["bank_mfo"].format(bank=_blank(company.get("company_bank_name")),
                             mfo=_blank(company.get("company_bank_mfo"), "short")),
        t["address"].format(v=_blank(company.get("company_address"))),
        t["phone"].format(v=_blank(company.get("company_phone"))),
    ]
    buyer = [
        _blank(doc.get("client_name")),
        t["buyer_tin"].format(v=_blank(doc.get("client_tin"))),
        t["address"].format(v=_blank(doc.get("client_address"))),
        t["phone"].format(v=_blank(doc.get("client_phone"))),
    ]
    amount = _fmt_money(total, currency)
    table = _items_table_html(
        doc.get("lines") or [], currency, lang,
        [("", t["subtotal"], amount), ("", t["no_vat"], "—"), ("grand", t["to_pay"], amount)],
    )
    try:
        days = int(doc.get("valid_days") or 3)
    except (TypeError, ValueError):
        days = 3
    director = t["director_caption"].format(name=_name_or_blank(company.get("company_director")))
    accountant = t["accountant_caption"].format(name=_name_or_blank(company.get("company_chief_accountant")))
    return (
        '<section class="doc">'
        + head
        + _parties_html(t["supplier"], supplier, t["buyer"], buyer)
        + table
        + _words_line(t["to_pay"], total, currency, lang)
        + f'<p class="valid">{t["valid"].format(days=bank_days(days, lang))}</p>'
        + _sign_html(t["director"], director, t["stamp"])
        + _sign_html(t["accountant"], accountant)
        + f'<p class="note">{t["invoice_note"]}</p>'
        + "</section>"
    )


def build_sales_invoice_html(doc: dict, logo_data_uri: str | None = None, lang: str | None = None) -> str:
    """HTML «Счёта на оплату» (`services/sales_invoice.py`) — по бланку владельца.

    Счёт НИЧЕГО не двигает: ни остатка, ни долга. Это печатная форма. Любая
    строка из БД — через esc(): товар «Уголок 50<60» иначе ломает разметку.
    `lang` — `ru_uz`/`ru`/`uz`; не передан — берётся из `doc["lang"]`.
    """
    parts = lang_parts(lang or doc.get("lang"))
    return _page("".join(_sales_invoice_part(doc, p, logo_data_uri) for p in parts))


def render_sales_invoice_pdf(doc: dict) -> bytes:
    """HTML счёта → PDF. Язык — `doc["lang"]` (по умолчанию рус + узб)."""
    from weasyprint import HTML

    html = build_sales_invoice_html(doc, _logo_data_uri())
    return HTML(string=html).write_pdf()


def sales_invoice_filename(doc: dict) -> str:
    """Имя файла счёта: `schet-31.pdf` — номер счёта = номер заказа."""
    number = str(doc.get("number") or doc.get("order_id") or "")
    safe = "".join(ch for ch in number if ch.isalnum() or ch in "-_")
    return f"schet-{safe or 'order'}.pdf"


# ─── Товарная накладная (расход) и приходная накладная ───────────────────────


def _waybill_part(invoice: dict, lang: str, logo_data_uri: str | None) -> str:
    t = _T[lang]
    company = invoice.get("company") or {}
    buyer = invoice.get("buyer") or {}
    currency = str(invoice.get("currency") or "USD").upper()
    total = int(invoice.get("total_amount_cents") or 0)
    head = _head_html(
        str(company.get("company_name") or COMPANY_NAME),
        t["waybill_title"],
        t["number_from"].format(n=esc(invoice.get("invoice_number")),
                                date=date_long(invoice.get("invoice_date"), lang)),
        logo_data_uri,
    )
    basis = invoice.get("basis") or None
    basis_html = ""
    if basis and basis.get("order_id"):
        # «от «16» сентября 2026 г.» / ««16» сентябрь 2026 йилги» — у узбекской
        # строки «йилги» заменяет «й.».
        basis_date = date_long(basis.get("date"), lang, suffix=(lang == "ru"))
        basis_html = '<p class="basis">' + t["basis"].format(
            n=esc(basis.get("order_id")), date=basis_date
        ) + "</p>"
    sender = [
        _blank(company.get("company_name") or COMPANY_NAME),
        t["address"].format(v=_blank(company.get("company_address"))),
        t["phone"].format(v=_blank(company.get("company_phone"))),
    ]
    receiver = [
        _blank(invoice.get("counterparty_name")),
        t["address"].format(v=_blank(buyer.get("address"))),
        t["phone"].format(v=_blank(buyer.get("phone"))),
    ]
    amount = _fmt_money(total, currency)
    table = _items_table_html(
        _line_items(invoice.get("items") or []), currency, lang,
        [("", t["subtotal"], amount), ("grand", t["released"], amount)],
    )
    cancelled = (
        f'<div class="cancelled">{t["cancelled"]}</div>' if invoice.get("status") == "cancelled" else ""
    )
    release_by = str(company.get("company_release_by") or company.get("company_director") or "").strip()
    return (
        '<section class="doc">'
        + head
        + basis_html
        + _parties_html(t["sender"], sender, t["receiver"], receiver)
        + table
        + cancelled
        + _words_line(t["released"], total, currency, lang)
        + '<div class="after-words"></div>'
        + _sign_html(t["release_by"], esc(release_by) if release_by else t["release_caption"], t["stamp"])
        + _sign_html(t["released_by"], t["released_by_caption"])
        + _sign_html(t["received_by"], t["received_caption"])
        + f'<p class="note">{t["waybill_note"]}</p>'
        + "</section>"
    )


def _incoming_part(invoice: dict, logo_data_uri: str | None) -> str:
    """Приходная накладная — внутренний документ приёмки, только по-русски.

    Вид тот же (шапка, таблица), а стороны — по существу прихода: товар
    принимаем ОТ поставщика НА свой склад. Слов «грузоотправитель /
    грузополучатель» здесь нет: это термины отгрузки клиенту. Цены прихода —
    себестоимость: не руководству они приходят пустыми
    (`costing.redact_invoice`) и печатаются прочерком.
    """
    company = invoice.get("company") or {}
    currency = str(invoice.get("currency") or "USD").upper()
    hidden = invoice.get("total_amount_cents") is None
    head = _head_html(
        str(company.get("company_name") or COMPANY_NAME),
        "Приходная накладная",
        f"№ {esc(invoice.get('invoice_number'))} от {date_long(invoice.get('invoice_date'), 'ru')}",
        logo_data_uri,
    )
    parties = _parties_html(
        "Поставщик", [_blank(invoice.get("counterparty_name"))],
        "Склад", [_blank(invoice.get("warehouse_name"))],
    )
    amount = "—" if hidden else _fmt_money(int(invoice.get("total_amount_cents") or 0), currency)
    table = _items_table_html(
        _line_items(invoice.get("items") or []), currency, "ru",
        [("grand", "Всего принято на сумму:", amount)],
    )
    cancelled = '<div class="cancelled">НАКЛАДНАЯ ОТМЕНЕНА</div>' if invoice.get("status") == "cancelled" else ""
    comment = str(invoice.get("comment") or "").strip()
    comment_html = f'<p class="comment">Примечание: {esc(comment)}</p>' if comment else ""
    return (
        '<section class="doc">'
        + head + parties + table + cancelled + comment_html
        + '<div class="after-words"></div>'
        + _sign_html("Передал:", "Ф.И.О.")
        + _sign_html("Принял:", "Ф.И.О.")
        + "</section>"
    )


def build_invoice_html(invoice: dict, logo_data_uri: str | None = None) -> str:
    """HTML складской накладной: расход — «Товарная накладная» по бланку
    владельца (язык — `invoice["doc_lang"]`), приход — внутренняя приходная.

    invoice — то, что отдаёт `warehouse.get_invoice`, дополненное
    `waybill.prepare_invoice` (реквизиты компании, адрес и телефон клиента,
    основание — счёт по заказу). Без дополнения документ всё равно собирается:
    пустые реквизиты печатаются чертой. Любая строка из БД — через esc().
    """
    if invoice.get("type") == "incoming":
        return _page(_incoming_part(invoice, logo_data_uri))
    parts = lang_parts(invoice.get("doc_lang"))
    return _page("".join(_waybill_part(invoice, p, logo_data_uri) for p in parts))


def render_invoice_pdf(invoice: dict) -> bytes:
    """HTML накладной → PDF. Требует установленного weasyprint (импорт ленивый)."""
    from weasyprint import HTML

    html = build_invoice_html(invoice, _logo_data_uri())
    return HTML(string=html).write_pdf()


def invoice_filename(invoice: dict) -> str:
    """Имя файла: номер накладной без символов, ломающих файловые системы."""
    number = str(invoice.get("invoice_number") or invoice.get("id") or "invoice")
    safe = "".join(ch for ch in number if ch.isalnum() or ch in "-_")
    return f"{safe or 'invoice'}.pdf"
