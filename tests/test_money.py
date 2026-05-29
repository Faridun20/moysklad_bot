"""
Канонический денежный модуль services/money.py — целые копейки.

Покрываем: round-trip копеек, ROUND_HALF_UP на полукопейке, парсинг
пользовательского ввода, тотал строки с дробным количеством, конвертацию
по курсу, форматирование (совпадение с legacy format_price), валидацию,
и сам факт устранения float-дрейфа (0.1+0.2).
"""

from decimal import Decimal

import pytest

from services import money
from utils.helpers import format_price


# ─── to_cents / from_cents ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,cents",
    [
        (1500.0, 150000),
        (49.99, 4999),
        ("1500", 150000),
        ("2.5", 250),
        (0, 0),
        (1, 100),
        (Decimal("99.95"), 9995),
    ],
)
def test_to_cents(value, cents):
    assert money.to_cents(value) == cents


def test_to_cents_half_up():
    # Полукопейка округляется вверх (бытовое правило), не к чётному.
    assert money.to_cents("0.005") == 1
    assert money.to_cents("0.004") == 0
    assert money.to_cents("2.345") == 235


def test_from_cents_exact():
    assert money.from_cents(150000) == Decimal("1500.00")
    assert money.from_cents(4999) == Decimal("49.99")


@pytest.mark.parametrize("cents", [0, 1, 99, 100, 4999, 150000, 999_999_999])
def test_round_trip(cents):
    assert money.to_cents(money.from_cents(cents)) == cents


def test_no_float_drift():
    # Главная причина миграции: 0.1 + 0.2 != 0.3 во float, но в копейках точно.
    assert money.to_cents("0.1") + money.to_cents("0.2") == money.to_cents("0.3")


# ─── parse_amount (граница ввода) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,cents",
    [
        ("1500", 150000),
        ("1 500,50", 150050),
        ("49.99", 4999),
        ("  300  ", 30000),
        ("0.01", 1),
    ],
)
def test_parse_amount_valid(text, cents):
    assert money.parse_amount(text) == cents


@pytest.mark.parametrize("text", ["", "   ", "abc", "0", "-5", "-0.5", None])
def test_parse_amount_invalid(text):
    assert money.parse_amount(text) is None


# ─── mul_qty / convert_cents ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cents,qty,total",
    [
        (15050, 2, 30100),
        (10000, 2.5, 25000),
        (4999, 3, 14997),
        (333, 3, 999),
        (10000, "1.5", 15000),
    ],
)
def test_mul_qty(cents, qty, total):
    assert money.mul_qty(cents, qty) == total


@pytest.mark.parametrize(
    "cents,rate,out",
    [
        (100000, 1, 100000),
        (100000, 0.5, 50000),
        (10000, "2", 20000),
    ],
)
def test_convert_cents(cents, rate, out):
    assert money.convert_cents(cents, rate) == out


# ─── format_cents ────────────────────────────────────────────────────────────


def test_format_cents_default():
    assert money.format_cents(150000) == "1,500"
    assert money.format_cents(0) == "0"


def test_format_cents_decimals_and_sep():
    assert money.format_cents(150050, decimals=2, sep=" ") == "1 500.50"
    assert money.format_cents(150000, decimals=2, sep=" ", trim=True) == "1 500"
    assert money.format_cents(150050, decimals=2, trim=True) == "1,500.5"


def test_format_cents_no_grouping():
    assert money.format_cents(150000, grouping=False) == "1500"


@pytest.mark.parametrize("cents", [0, 100, 4999, 150000, 1_234_567_89])
def test_format_price_is_format_cents_alias(cents):
    # legacy format_price теперь делегирует money.format_cents — проверяем,
    # что вывод совпадает с прямым вызовом канонического форматтера.
    assert format_price(cents) == money.format_cents(cents, decimals=0, sep=",")


# ─── validate_cents ──────────────────────────────────────────────────────────


def test_validate_cents():
    assert money.validate_cents(150000)[0] is True
    assert money.validate_cents(0)[0] is True
    assert money.validate_cents(-1)[0] is False
    assert money.validate_cents(money.MAX_CENTS + 1)[0] is False
