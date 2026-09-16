"""
PDF расходной накладной: HTML-шаблон → weasyprint.

Разделение намеренное: `build_invoice_html` — чистая функция без внешних
зависимостей (её и тестируем), `render_invoice_pdf` — тонкая обёртка,
импортирующая weasyprint ЛЕНИВО. Импорт weasyprint тянет системные
библиотеки (pango/cairo); если их нет на машине, модуль всё равно
импортируется и всё, кроме самого рендера, работает и тестируется.

Логотип необязателен: путь берётся из env `INVOICE_LOGO_PATH`, и без файла
накладная просто печатается без картинки. Блокировать выписку документа
из-за отсутствующей картинки нельзя.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path

from services import money
from utils.helpers import esc

logger = logging.getLogger(__name__)

COMPANY_NAME = os.environ.get("COMPANY_NAME", "FARID IMPEKS LLC")

_TYPE_TITLE = {
    "incoming": "ПРИХОДНАЯ НАКЛАДНАЯ",
    "outgoing": "РАСХОДНАЯ НАКЛАДНАЯ",
}

# Жалоба владельца: «многие пункты написаны одинаково». Одно и то же слово
# «Контрагент» стояло и над тем, кто у нас покупает, и над тем, у кого покупаем
# мы. В накладной сторона называется по существу: отгружаем — клиенту,
# приходуем — от поставщика (словарь интерфейса в CLAUDE.md).
_PARTY_LABEL = {
    "incoming": "Поставщик",
    "outgoing": "Клиент",
}

# Кто ставит подпись — тоже зависит от направления: по расходной товар
# отгружают и получают, по приходной передают и принимают.
_SIGN_LABELS = {
    "incoming": ("Передал — подпись", "Принял — подпись"),
    "outgoing": ("Отгрузил — подпись", "Получил — подпись"),
}

# Кэш data-URI логотипа: файл на диске не меняется в течение жизни процесса,
# а перечитывать и перекодировать его на каждую накладную незачем.
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
        # Именно предупреждение, а не ошибка: накладная выписывается и без лого.
        logger.warning("Логотип накладной не прочитан (%s): %s", path, e)
    _logo_cache = (path, uri)
    return uri


def _fmt_qty(value) -> str:
    """Количество по-русски и без хвостовых нулей: 3, а не 3.0; 2,5 — с запятой.

    Через `:g` было два изъяна: дробная часть печаталась по-английски («2.5»)
    рядом с русскими суммами, а количество от миллиона уходило в
    экспоненциальный вид («1e+06») — в накладной это нечитаемо.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{f:.3f}".rstrip("0").rstrip(".")
    return (text or "0").replace(".", ",")


_CSS = """
@page { size: A4; margin: 18mm 15mm; }
body { font-family: "DejaVu Sans", "Liberation Sans", sans-serif;
       font-size: 10pt; color: #111; }
.head { display: flex; justify-content: space-between; align-items: flex-start;
        border-bottom: 2px solid #111; padding-bottom: 8px; margin-bottom: 14px; }
.logo { max-height: 22mm; max-width: 55mm; }
.company { font-size: 13pt; font-weight: bold; }
h1 { font-size: 13pt; margin: 0 0 2px; text-align: right; }
.meta { text-align: right; font-size: 9pt; color: #444; }
.parties { margin-bottom: 12px; font-size: 10pt; }
.parties div { margin-bottom: 2px; }
.label { color: #666; display: inline-block; min-width: 28mm; }
table { width: 100%; border-collapse: collapse; margin-top: 6px; }
th, td { border: 1px solid #999; padding: 4px 6px; }
th { background: #f0f0f0; font-size: 9pt; text-align: left; }
td.num, th.num { text-align: right; white-space: nowrap; }
td.idx { text-align: right; color: #666; width: 8mm; }
tfoot td { font-weight: bold; border-top: 2px solid #111; }
.cancelled { color: #b00; font-weight: bold; text-align: right; margin-top: 4px; }
.sign { margin-top: 16mm; display: flex; justify-content: space-between;
        font-size: 9pt; color: #333; }
.sign div { width: 45%; border-top: 1px solid #777; padding-top: 3px; }
.comment { margin-top: 8px; font-size: 9pt; color: #444; }
/* Счёт: реквизиты компании мелкой строкой под названием, итог прописью —
   отдельным абзацем под таблицей (в накладной их нет). */
.req { font-size: 8.5pt; color: #444; margin-top: 2px; }
.words { margin-top: 8px; font-size: 10pt; }
.note { margin-top: 10px; font-size: 8.5pt; color: #666; }
"""


def build_invoice_html(invoice: dict, logo_data_uri: str | None = None) -> str:
    """Собрать HTML печатной формы накладной.

    invoice — то, что отдаёт `warehouse.get_invoice`: шапка + `items`.
    Любая строка, пришедшая из БД (имя контрагента, название товара,
    комментарий), проходит через esc(): без этого товар с именем
    «Уголок 50<60» ломает разметку, а комментарий с тегом — подменяет её.
    """
    items = invoice.get("items") or []
    currency = esc(invoice.get("currency") or "USD")
    inv_type = invoice.get("type")
    title = _TYPE_TITLE.get(inv_type, "НАКЛАДНАЯ")
    party_label = _PARTY_LABEL.get(inv_type, "Клиент или поставщик")
    sign_from, sign_to = _SIGN_LABELS.get(inv_type, ("Отпустил — подпись", "Получил — подпись"))

    rows = []
    for i, it in enumerate(items, 1):
        price_cents = int(it.get("price_cents") or 0)
        line_cents = money.mul_qty(price_cents, it.get("quantity") or 0)
        rows.append(
            "<tr>"
            f'<td class="idx">{i}</td>'
            f"<td>{esc(it.get('product_name'))}</td>"
            f"<td>{esc(it.get('sku') or '')}</td>"
            f"<td>{esc(it.get('unit') or '')}</td>"
            f'<td class="num">{_fmt_qty(it.get("quantity"))}</td>'
            f'<td class="num">{money.format_cents(price_cents, decimals=2)}</td>'
            f'<td class="num">{money.format_cents(line_cents, decimals=2)}</td>'
            "</tr>"
        )

    total = money.format_cents(int(invoice.get("total_amount_cents") or 0), decimals=2)
    logo_html = (
        f'<img class="logo" src="{logo_data_uri}" alt="">' if logo_data_uri else ""
    )
    counterparty = esc(invoice.get("counterparty_name") or "—")
    warehouse = esc(invoice.get("warehouse_name") or "—")
    comment = invoice.get("comment")
    comment_html = f'<div class="comment">Примечание: {esc(comment)}</div>' if comment else ""
    cancelled_html = (
        '<div class="cancelled">НАКЛАДНАЯ ОТМЕНЕНА</div>'
        if invoice.get("status") == "cancelled"
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
<div class="head">
  <div>{logo_html}<div class="company">{esc(COMPANY_NAME)}</div></div>
  <div>
    <h1>{title}</h1>
    <div class="meta">№ {esc(invoice.get('invoice_number'))}<br>
    от {esc(invoice.get('invoice_date'))}</div>
  </div>
</div>
<div class="parties">
  <div><span class="label">{party_label}:</span> {counterparty}</div>
  <div><span class="label">Склад:</span> {warehouse}</div>
  <div><span class="label">Валюта:</span> {currency}</div>
</div>
<table>
  <thead><tr>
    <th class="idx">№</th><th>Наименование</th><th>Артикул</th><th>Ед.</th>
    <th class="num">Кол-во</th><th class="num">Цена</th><th class="num">Сумма</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
  <tfoot><tr>
    <td colspan="6" class="num">Итого, {currency}</td>
    <td class="num">{total}</td>
  </tr></tfoot>
</table>
{cancelled_html}
{comment_html}
<div class="sign">
  <div>{sign_from}</div>
  <div>{sign_to}</div>
</div>
</body></html>"""


def build_sales_invoice_html(doc: dict, logo_data_uri: str | None = None) -> str:
    """HTML «Счёта» — документа клиенту ДО отгрузки (`services/sales_invoice.py`).

    Вёрстка, шрифты и стили — те же, что у накладной (`_CSS` выше): второй
    печатный движок и второй набор стилей разошлись бы на первой же правке, а
    клиент получал бы от одной компании две разные по виду бумаги. Отличия
    ровно три и они по существу: реквизиты компании в шапке, телефон клиента и
    итог прописью — счёт человек несёт в банк и в бухгалтерию, накладную нет.

    Счёт НИЧЕГО не двигает: ни остатка, ни долга. Это печатная форма.
    Любая строка из БД — через esc(): товар «Уголок 50<60» иначе ломает
    разметку документа.
    """
    lines = doc.get("lines") or []
    currency = esc(doc.get("currency") or "USD")
    company = doc.get("company") or {}

    rows = []
    for i, ln in enumerate(lines, 1):
        rows.append(
            "<tr>"
            f'<td class="idx">{i}</td>'
            f"<td>{esc(ln.get('product_name'))}</td>"
            f"<td>{esc(ln.get('unit') or '')}</td>"
            f'<td class="num">{_fmt_qty(ln.get("quantity"))}</td>'
            f'<td class="num">{money.format_cents(int(ln.get("price_cents") or 0), decimals=2)}</td>'
            f'<td class="num">{money.format_cents(int(ln.get("amount_cents") or 0), decimals=2)}</td>'
            "</tr>"
        )

    total = money.format_cents(int(doc.get("total_cents") or 0), decimals=2)
    logo_html = (
        f'<img class="logo" src="{logo_data_uri}" alt="">' if logo_data_uri else ""
    )
    # Реквизиты необязательны: незаполненное поле просто не печатается — иначе
    # счёт нельзя было бы выписать, пока руководство не дозаполнит «Настройки».
    req = " · ".join(
        esc(str(company.get(key) or "").strip())
        for key in ("company_tin", "company_address")
        if str(company.get(key) or "").strip()
    )
    req_html = f'<div class="req">{req}</div>' if req else ""
    phone = str(doc.get("client_phone") or "").strip()
    phone_html = (
        f'<div><span class="label">Телефон:</span> {esc(phone)}</div>' if phone else ""
    )
    comment = str(doc.get("comment") or "").strip()
    comment_html = (
        f'<div class="comment">Примечание: {esc(comment)}</div>' if comment else ""
    )
    company_name = esc(str(company.get("company_name") or COMPANY_NAME))

    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
<div class="head">
  <div>{logo_html}<div class="company">{company_name}</div>{req_html}</div>
  <div>
    <h1>СЧЁТ НА ОПЛАТУ</h1>
    <div class="meta">№ {esc(doc.get('number'))}<br>
    от {esc(doc.get('date'))}</div>
  </div>
</div>
<div class="parties">
  <div><span class="label">Клиент:</span> {esc(doc.get('client_name') or '—')}</div>
  {phone_html}
  <div><span class="label">Валюта:</span> {currency}</div>
</div>
<table>
  <thead><tr>
    <th class="idx">№</th><th>Наименование</th><th>Ед.</th>
    <th class="num">Кол-во</th><th class="num">Цена</th><th class="num">Сумма</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
  <tfoot><tr>
    <td colspan="5" class="num">Итого, {currency}</td>
    <td class="num">{total}</td>
  </tr></tfoot>
</table>
<div class="words">Всего к оплате: {total} {currency}
  ({esc(doc.get('total_words') or '')})</div>
{comment_html}
<div class="note">Счёт не заменяет документ отгрузки: товар передаётся
по расходной накладной.</div>
<div class="sign">
  <div>Счёт выписал — подпись</div>
  <div>Клиент — подпись</div>
</div>
</body></html>"""


def render_sales_invoice_pdf(doc: dict) -> bytes:
    """HTML счёта → PDF. Тот же weasyprint, что у накладной (импорт ленивый)."""
    from weasyprint import HTML

    html = build_sales_invoice_html(doc, _logo_data_uri())
    return HTML(string=html).write_pdf()


def sales_invoice_filename(doc: dict) -> str:
    """Имя файла счёта: `schet-31.pdf` — номер счёта = номер заказа."""
    number = str(doc.get("number") or doc.get("order_id") or "")
    safe = "".join(ch for ch in number if ch.isalnum() or ch in "-_")
    return f"schet-{safe or 'order'}.pdf"


def render_invoice_pdf(invoice: dict) -> bytes:
    """HTML накладной → PDF. Требует установленного weasyprint.

    Импорт ленивый: weasyprint тянет системные pango/cairo, и на машине без
    них должен падать только сам рендер, а не импорт services.invoice_pdf
    (его тянет webapp целиком).
    """
    from weasyprint import HTML

    html = build_invoice_html(invoice, _logo_data_uri())
    return HTML(string=html).write_pdf()


def invoice_filename(invoice: dict) -> str:
    """Имя файла: номер накладной без символов, ломающих файловые системы."""
    number = str(invoice.get("invoice_number") or invoice.get("id") or "invoice")
    safe = "".join(ch for ch in number if ch.isalnum() or ch in "-_")
    return f"{safe or 'invoice'}.pdf"
