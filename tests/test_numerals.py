"""Сумма прописью в валюте документа: счёт на оплату и товарная накладная.

Числительные (род тысяч, 11–14, «минг» без «бир») проверяет
`tests/test_legal_docs.py`; здесь — согласование слова валюты с числом на
«трудных» числах, по-русски и по-узбекски, в долларах и сумах.
"""

from decimal import Decimal

import pytest

from services.numerals import currency_noun, money_in_words, money_phrase

CASES = [
    # число, ru USD, uz USD, ru UZS, uz UZS
    (1, "один доллар США", "бир АҚШ доллари", "один сум", "бир сўм"),
    (2, "два доллара США", "икки АҚШ доллари", "два сум", "икки сўм"),
    (5, "пять долларов США", "беш АҚШ доллари", "пять сум", "беш сўм"),
    (11, "одиннадцать долларов США", "ўн бир АҚШ доллари", "одиннадцать сум", "ўн бир сўм"),
    (21, "двадцать один доллар США", "йигирма бир АҚШ доллари", "двадцать один сум", "йигирма бир сўм"),
    (101, "сто один доллар США", "юз бир АҚШ доллари", "сто один сум", "юз бир сўм"),
    (1_000_000, "один миллион долларов США", "бир миллион АҚШ доллари",
     "один миллион сум", "бир миллион сўм"),
]


@pytest.mark.parametrize("n,ru_usd,uz_usd,ru_uzs,uz_uzs", CASES)
def test_money_phrase_agrees_currency_with_the_number(n, ru_usd, uz_usd, ru_uzs, uz_uzs):
    value = Decimal(n)
    assert money_phrase(value, "ru", "USD") == ru_usd
    assert money_phrase(value, "uz", "USD") == uz_usd
    assert money_phrase(value, "ru", "UZS") == ru_uzs
    assert money_phrase(value, "uz", "UZS") == uz_uzs


@pytest.mark.parametrize("n,form", [(12, "долларов США"), (22, "доллара США"), (111, "долларов США"),
                                    (1001, "доллар США"), (1_000, "долларов США")])
def test_russian_dollar_forms_follow_the_last_digits(n, form):
    assert currency_noun(n, "ru", "USD") == form


def test_cents_follow_the_currency_word():
    """«400,50 (четыреста) долларов США 50 центов.» — центы после валюты."""
    assert money_in_words(Decimal("400.50"), "ru", "USD") == ("четыреста", "долларов США 50 центов")
    assert money_in_words(Decimal("2.01"), "ru", "USD") == ("два", "доллара США 01 цент")
    assert money_in_words(Decimal("2.03"), "ru", "USD") == ("два", "доллара США 03 цента")
    assert money_in_words(Decimal("400.50"), "uz", "USD") == ("тўрт юз", "АҚШ доллари 50 цент")
    # Целая сумма — без хвоста центов; сумы — целые.
    assert money_in_words(Decimal("1092000"), "ru", "UZS") == ("один миллион девяносто две тысячи", "сум")


@pytest.mark.parametrize("bad", [("ru", "EUR"), ("en", "USD")])
def test_unknown_currency_or_language_is_refused(bad):
    lang, currency = bad
    with pytest.raises(ValueError):
        money_phrase(Decimal(1), lang, currency)


def test_negative_amount_is_refused():
    with pytest.raises(ValueError):
        money_in_words(Decimal("-1"), "ru", "USD")
