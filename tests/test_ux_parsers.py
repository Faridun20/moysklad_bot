"""
UX-парсер однострочного ввода платежа («1500 USD за аренду») — граница
пользовательского ввода: сумма, необязательная валюта, комментарий.

T3.3: парсер «количество+цена» ушёл вместе с созданием заказа в боте (в WebApp
это отдельные поля формы, парсить нечего).
"""

import pytest

from handlers.payments import _parse_payment_input


# ─── Платёж: сумма [валюта] [комментарий] ────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1500 USD за аренду", (1500.0, "USD", "за аренду")),
        ("1500 usd за аренду", (1500.0, "USD", "за аренду")),  # валюта case-insensitive
        ("1500 за аренду", (1500.0, None, "за аренду")),  # валюта опущена
        ("1500 USD", (1500.0, "USD", "")),  # без комментария
        ("1500", (1500.0, None, "")),  # только сумма
        ("2,5 uzs кофе", (2.5, "UZS", "кофе")),  # запятая-десятичный
        ("  300   UZS  ", (300.0, "UZS", "")),  # лишние пробелы
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
