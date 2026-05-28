"""
UX-парсеры однострочного ввода: платёж («1500 USD за аренду») и
количество+цена заказа («5 150» / «5x150»). Это границы пользовательского
ввода после редизайна потоков — раньше были многошаговыми FSM, теперь
парсятся одной строкой, поэтому покрываем их явно.
"""

import pytest

from handlers.payments import _parse_payment_input
from handlers.orders import _parse_qty_price


# ─── Платёж: сумма [валюта] [комментарий] ────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1500 USD за аренду", (1500.0, "USD", "за аренду")),
        ("1500 usd за аренду", (1500.0, "USD", "за аренду")),  # валюта case-insensitive
        ("1500 за аренду", (1500.0, None, "за аренду")),  # валюта опущена
        ("1500 USD", (1500.0, "USD", "")),  # без комментария
        ("1500", (1500.0, None, "")),  # только сумма
        ("2,5 eur кофе", (2.5, "EUR", "кофе")),  # запятая-десятичный
        ("  300   RUB  ", (300.0, "RUB", "")),  # лишние пробелы
        ("1000 UZS за май, аренда", (1000.0, "UZS", "за май, аренда")),
    ],
)
def test_parse_payment_valid(text, expected):
    assert _parse_payment_input(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "abc", "0 usd", "-5 usd", "usd 100"])
def test_parse_payment_invalid_amount(text):
    amount, _currency, _comment = _parse_payment_input(text)
    assert amount is None


def test_parse_payment_currency_only_right_after_amount():
    # «USD» в середине комментария не считается валютой — только сразу после суммы.
    amount, currency, comment = _parse_payment_input("1500 за USD аренду")
    assert (amount, currency) == (1500.0, None)
    assert comment == "за USD аренду"


# ─── Заказ: количество [цена] ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5 150", (5.0, 150.0)),
        ("5", (5.0, 0.0)),  # только количество → цена 0 (неизвестна)
        ("5x150", (5.0, 150.0)),  # латинская x
        ("5х150", (5.0, 150.0)),  # кириллическая х
        ("5*150", (5.0, 150.0)),  # звёздочка
        ("5×150", (5.0, 150.0)),  # знак умножения
        ("2,5 49.99", (2.5, 49.99)),  # запятая в количестве, точка в цене
        ("  3   100  ", (3.0, 100.0)),
    ],
)
def test_parse_qty_price_valid(text, expected):
    assert _parse_qty_price(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "abc", "0", "-5", "0 100"])
def test_parse_qty_price_invalid_qty(text):
    qty, _price = _parse_qty_price(text)
    assert qty is None


@pytest.mark.parametrize("text", ["5 abc", "5 -10", "5 1.2.3"])
def test_parse_qty_price_qty_ok_price_bad(text):
    # Количество валидно, но цена не распарсилась → (qty, None) — это сигнал
    # обработчику показать ошибку именно про цену, не теряя количество.
    qty, price = _parse_qty_price(text)
    assert qty == 5.0
    assert price is None
