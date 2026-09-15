"""
Excel-экспорт (PR D + продуктовый аудит B6). Чистые функции → bytes, без
сети/БД — данные готовит вызывающая ручка (`webapp/server.py`), здесь только
раскладка по листам и защита от формул (`_text`).

build_analytics_xlsx(data) собирает .xlsx из dict'а аналитики (тот же
формат что отдаёт /api/analytics для boss). Листы:
  • Сводка   — общие цифры (выручка, заказы, средний чек, тренд)
  • Клиенты  — топ клиентов по выручке
  • Менеджеры — топ менеджеров
  • Товары   — топ товаров + прибыль (где известна себестоимость)

B6 добавляет четыре узкие выгрузки — по одной на экран, из которого их
вызвали (Склад → Каталог, Деньги → Долги, Склад → Накладные, Клиенты):
  • build_stock_xlsx           — остаток склада
  • build_debts_xlsx           — дебиторка со сроками (services.receivables)
  • build_invoices_xlsx        — накладные за период
  • build_counterparties_xlsx  — контрагенты: обороты и текущий долг

Доставка — файлом в Telegram (тот же приём, что у `/api/analytics/export`:
`bot.send_document`, в WebApp нет ни одного места, отдающего файл напрямую
браузеру). openpyxl backward-compatible с Excel/Google Sheets/LibreOffice.
"""

from __future__ import annotations

import io
from typing import Any


def _autosize(ws, max_width: int = 50) -> None:
    """Подогнать ширину колонок под содержимое (грубо, по max длине)."""
    for col in ws.columns:
        length = 0
        letter = col[0].column_letter
        for cell in col:
            v = cell.value
            if v is not None:
                length = max(length, len(str(v)))
        ws.column_dimensions[letter].width = min(max(length + 2, 10), max_width)


# Символы, с которых Excel начинает ФОРМУЛУ. openpyxl хранит строку с ведущим
# «=» как формулу (data_type 'f'), и контрагент, названный
# «=HYPERLINK("http://…","Клиент")», выполнится у босса при открытии файла.
# Имена приходят от менеджеров, поэтому текстовые ячейки экранируются
# апострофом — Excel показывает его как обычный текст. Числа не трогаем.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def _text(value) -> str:
    """Текстовая ячейка без риска стать формулой."""
    text = "—" if value is None else str(value)
    if text[:1] in _FORMULA_LEADERS:
        return "'" + text
    return text


def build_analytics_xlsx(data: dict[str, Any]) -> bytes:
    """Собрать .xlsx из dict аналитики. Возвращает bytes (для send_document).

    `data` — структура из /api/analytics (boss): label/total/count/clients/
    avg_check/trend/top_clients/top_managers/top_products.
    Толерантна к отсутствующим ключам (пустые секции).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    bold = Font(bold=True)

    # ─── Лист «Сводка» ───
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Показатель", "Значение"])
    for c in ws[1]:
        c.font = bold
    ws.append(["Период", _text(data.get("label", ""))])
    # Выручка — итог в базовой валюте по курсу; разные валюты в одно число не
    # складываются, поэтому ниже — строки по валютам и что осталось без курса.
    base_cur = _text(data.get("base_currency") or "USD")
    ws.append([f"Выручка, ≈ {base_cur}", round(float(data.get("total", 0) or 0), 2)])
    for row in data.get("total_by_currency", []) or []:
        ws.append([f"  в т.ч. {_text(row.get('currency'))}", round(float(row.get("total", 0) or 0), 2)])
    for row in data.get("missing_rates", []) or []:
        ws.append([
            f"  без курса не учтено, {_text(row.get('currency'))}",
            round(float(row.get("amount", 0) or 0), 2),
        ])
    ws.append(["Заказов", data.get("count", 0)])
    ws.append(["Клиентов", data.get("clients", 0)])
    ws.append(["Средний чек", round(float(data.get("avg_check", 0) or 0), 2)])
    ws.append(["Тренд, %", data.get("trend", 0)])
    _autosize(ws)

    # ─── Лист «Клиенты» ───
    ws_c = wb.create_sheet("Клиенты")
    # Валюта — отдельной колонкой: топ клиентов и товаров считается раздельно
    # по валютам, и «Выручка» без неё не читается.
    ws_c.append(["Клиент", "Выручка", "Заказов", "Валюта"])
    for c in ws_c[1]:
        c.font = bold
    for row in data.get("top_clients", []) or []:
        ws_c.append(
            [_text(row.get("name")), round(float(row.get("revenue", 0) or 0), 2), row.get("count", 0),
             _text(row.get("currency") or base_cur)]
        )
    _autosize(ws_c)

    # ─── Лист «Менеджеры» ───
    ws_m = wb.create_sheet("Менеджеры")
    ws_m.append(["Менеджер", "Выручка", "Заказов"])
    for c in ws_m[1]:
        c.font = bold
    for row in data.get("top_managers", []) or []:
        ws_m.append(
            [_text(row.get("name")), round(float(row.get("revenue", 0) or 0), 2), row.get("count", 0)]
        )
    _autosize(ws_m)

    # ─── Лист «Товары» (+ прибыль где известна) ───
    ws_p = wb.create_sheet("Товары")
    ws_p.append(["Товар", "Кол-во", "Выручка", "Прибыль", "Валюта"])
    for c in ws_p[1]:
        c.font = bold
    for row in data.get("top_products", []) or []:
        profit = row.get("profit")
        ws_p.append(
            [
                _text(row.get("name")),
                row.get("qty", 0),
                round(float(row.get("sum", 0) or 0), 2),
                round(float(profit), 2) if profit is not None else "н/д",
                _text(row.get("currency") or base_cur),
            ]
        )
    _autosize(ws_p)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _single_sheet(title: str, headers: list[str], rows: list[list[Any]]) -> bytes:
    """Один лист «заголовок + строки», как у всех B6-выгрузок ниже."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
    for row in rows:
        ws.append(row)
    _autosize(ws)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_stock_xlsx(rows: list[dict[str, Any]]) -> bytes:
    """Остаток склада: товар, единица, категория, остаток.

    `rows` — из `services.warehouse.get_stock()` (product/unit/category/
    quantity); строится под экран «Склад → Каталог» — теми же данными, что
    видны на экране, никакой себестоимости или резерва здесь нет.
    """
    data = [
        [
            _text(r.get("name")),
            _text(r.get("unit") or "шт"),
            _text(r.get("category")),
            round(float(r.get("quantity", 0) or 0), 3),
        ]
        for r in rows
    ]
    return _single_sheet("Каталог", ["Товар", "Единица", "Категория", "Остаток"], data)


def build_debts_xlsx(rows: list[dict[str, Any]]) -> bytes:
    """Дебиторка со сроками — заказы в долг и рассрочки техники одним списком.

    `rows` — уже приведены к плоскому виду вызывающей стороной (обычно из
    `services.receivables.collect()` + `bucket_of()` на каждую строку):
    source/title/counterparty/owner_name/due_date/amount/currency/bucket_label.
    """
    data = [
        [
            "Заказ" if r.get("source") == "order" else "Техника",
            _text(r.get("title")),
            _text(r.get("counterparty")),
            _text(r.get("owner_name")),
            _text(r.get("due_date")) if r.get("due_date") else "—",
            round(float(r.get("amount", 0) or 0), 2),
            _text(r.get("currency")),
            _text(r.get("bucket_label")),
        ]
        for r in rows
    ]
    return _single_sheet(
        "Долги",
        ["Источник", "№", "Контрагент", "Менеджер", "Срок оплаты", "Долг", "Валюта", "Просрочка"],
        data,
    )


def build_invoices_xlsx(rows: list[dict[str, Any]]) -> bytes:
    """Накладные за период — списком, без построчного состава.

    `rows` — из `services.warehouse.list_invoices`-подобной выборки за
    диапазон дат: number/date/type_label/counterparty/amount/currency/
    status_label.
    """
    data = [
        [
            _text(r.get("number")),
            _text(r.get("date")),
            _text(r.get("type_label")),
            _text(r.get("counterparty")),
            round(float(r.get("amount", 0) or 0), 2),
            _text(r.get("currency")),
            _text(r.get("status_label")),
        ]
        for r in rows
    ]
    return _single_sheet(
        "Накладные",
        ["Номер", "Дата", "Тип", "Контрагент", "Сумма", "Валюта", "Статус"],
        data,
    )


def build_counterparties_xlsx(rows: list[dict[str, Any]]) -> bytes:
    """Контрагенты: обороты и текущий долг.

    `rows` — по контрагенту: name/phone/orders_count/purchases (список
    {currency,total}) /debts (список {currency,total}). Суммы по нескольким
    валютам не складываются — каждая валюта своей строкой в колонках
    «Покупки»/«Долг», через «; ».
    """

    def _money_list(items) -> str:
        parts = [f"{float(x.get('total', 0) or 0):,.2f} {x.get('currency')}".replace(",", " ")
                 for x in (items or [])]
        return "; ".join(parts) if parts else "—"

    data = [
        [
            _text(r.get("name")),
            _text(r.get("phone")),
            r.get("orders_count", 0),
            _money_list(r.get("purchases")),
            _money_list(r.get("debts")),
        ]
        for r in rows
    ]
    return _single_sheet(
        "Клиенты",
        ["Клиент", "Телефон", "Заказов", "Сумма покупок", "Текущий долг"],
        data,
    )
