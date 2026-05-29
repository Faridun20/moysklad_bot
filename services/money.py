"""
Канонические деньги: целые копейки (минорные единицы) как единственная
форма хранения и арифметики денежных сумм.

Зачем: суммы исторически хранились как float (REAL) — на Postgres это
single-precision float4 (теряет точность выше ~16k), а float-сравнения
(`a == b`, `round(x, 2)`) дают тихие баги. Тут — единый слой: парсинг
ввода, арифметика и форматирование строго через int-копейки и Decimal.

Конвенция: 1 единица валюты = 100 копеек. Округление при конвертации
из мажорных единиц — ROUND_HALF_UP (бытовое «округление к большему на .5»).
Форматирование для дисплея использует обычное правило Python (half-even),
чтобы совпадать с историческим utils.helpers.format_price.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

# Зеркалит services.database._AMOUNT_MAX (10_000_000.0 мажорных единиц).
# Держим локально, чтобы money.py не импортировал database (там обратный
# импорт money → цикл). 1e7 единиц = 1e9 копеек — влезает в Postgres INT4,
# но _cents-колонки объявлены BIGINT с запасом.
MAX_CENTS = 1_000_000_000

_ONE = Decimal("1")


def to_cents(value: float | int | str | Decimal) -> int:
    """Мажорные единицы (1500.50) → копейки (150050).

    Через Decimal(str(value)), НИКОГДА не float*100 (бинарный дрейф).
    Округление ROUND_HALF_UP. Бросает на нечисловом входе — вызывающий
    обязан валидировать пользовательский ввод через parse_amount."""
    d = Decimal(str(value))
    return int((d * 100).quantize(_ONE, rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    """Копейки → точное мажорное Decimal (для вычислений/сериализации)."""
    return Decimal(int(cents)) / 100


def format_cents(
    cents: int,
    *,
    decimals: int = 0,
    sep: str = ",",
    grouping: bool = True,
    trim: bool = False,
) -> str:
    """Копейки → строка для дисплея.

    decimals — знаков после запятой; grouping — разделять тысячи;
    sep — символ разделителя тысяч (',' как в Python по умолчанию,
    ' ' для русского стиля); trim — срезать хвостовые нули дробной части.

    Округление — стандартное для Python-формата (half-even), чтобы
    совпадать с историческим format_price (f"{x/100:,.0f}")."""
    major = from_cents(cents)
    spec = ("," if grouping else "") + f".{decimals}f"
    s = format(major, spec)
    if grouping and sep != ",":
        s = s.replace(",", sep)
    if trim and decimals > 0:
        s = s.rstrip("0").rstrip(".")
    return s


def parse_amount(text: str | None) -> int | None:
    """Пользовательский ввод суммы → копейки, или None если не число/<=0.

    Принимает «1500», «1 500,50», «49.99». Граница системы: бот-парсеры
    и WebApp write-эндпоинты конвертируют тут, дальше код работает в копейках."""
    if text is None:
        return None
    raw = str(text).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not raw:
        return None
    try:
        d = Decimal(raw)
    except (ArithmeticError, ValueError):
        return None
    if d <= 0:
        return None
    return int((d * 100).quantize(_ONE, rounding=ROUND_HALF_UP))


def mul_qty(cents: int, qty: float | int | str | Decimal) -> int:
    """Тотал строки: цена_в_копейках × количество (количество дробное).

    Округление результата один раз, ROUND_HALF_UP."""
    return int((Decimal(int(cents)) * Decimal(str(qty))).quantize(_ONE, rounding=ROUND_HALF_UP))


def convert_cents(cents: int, rate: float | int | str | Decimal) -> int:
    """Конвертация суммы в копейках по курсу (rate — мажор/мажор)."""
    return int((Decimal(int(cents)) * Decimal(str(rate))).quantize(_ONE, rounding=ROUND_HALF_UP))


def add(*values: int) -> int:
    """Сумма копеек (явная функция — чтобы не было соблазна складывать float)."""
    return sum(int(v) for v in values)


def sub(a: int, b: int) -> int:
    return int(a) - int(b)


def validate_cents(cents: int) -> tuple[bool, str]:
    """Проверка диапазона суммы (зеркалит database.validate_amount, но в копейках)."""
    if cents < 0:
        return False, "Сумма не может быть отрицательной"
    if cents > MAX_CENTS:
        return False, f"Сумма превышает лимит ({MAX_CENTS // 100:.0f})"
    return True, ""
