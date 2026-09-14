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

# Потолок одной суммы (платёж, сдача, цена техники) — в ЭКВИВАЛЕНТЕ базовой
# валюты, а не «10 000 000 в любой валюте». Прежний потолок в единицах
# валюты для сумов означал ≈ $800: заказ техники в UZS нельзя было оплатить
# одним платежом. Сторож от опечаток и `1e308`, а не бизнес-лимит, поэтому
# в базовой валюте он прежний — 10 000 000 (MAX_CENTS), старые USD-суммы
# ведут себя как раньше.
MAX_BASE_CENTS = 1_000_000_000
MAX_CENTS = MAX_BASE_CENTS  # имя из прежнего API: потолок в базовой валюте

# Технический потолок в ЛЮБОЙ валюте — для валюты без курса (пересчитать
# потолок не во что) и как верхняя граница пересчёта. 1e15 копеек < 2**53:
# сумма точно переживает float в JSON и Number во фронте, и далеко до BIGINT.
HARD_MAX_CENTS = 10**15

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


def max_cents_for_rate(rate_to_base: float | int | str | Decimal | None) -> int:
    """Потолок суммы в копейках валюты с курсом `rate_to_base` (мажор/мажор).

    MAX_BASE_CENTS в пересчёте: при курсе 0.00008 (12 500 сум за доллар)
    это 12 500 × 10 000 000 сум. Курса нет или он негодный — HARD_MAX_CENTS:
    отказывать в платеже из-за незаданного курса нельзя, а технический
    потолок всё равно режет `1e308`.
    """
    if rate_to_base is None:
        return HARD_MAX_CENTS
    try:
        rate = Decimal(str(rate_to_base))
    except (ArithmeticError, ValueError):
        return HARD_MAX_CENTS
    if not rate.is_finite() or rate <= 0:
        return HARD_MAX_CENTS
    limit = (Decimal(MAX_BASE_CENTS) / rate).to_integral_value(rounding=ROUND_HALF_UP)
    return int(min(Decimal(HARD_MAX_CENTS), limit))


def validate_cents(
    cents: int, rate_to_base: float | int | str | Decimal | None = 1
) -> tuple[bool, str]:
    """Проверка диапазона суммы в копейках.

    `rate_to_base` — курс валюты суммы к базовой; по умолчанию 1 (сумма в
    базовой валюте). Текст отказа называет потолок в базовой валюте — именно
    так он и задан.
    """
    if cents < 0:
        return False, "Сумма не может быть отрицательной"
    if cents > max_cents_for_rate(rate_to_base):
        return False, (
            f"Сумма превышает лимит (эквивалент {MAX_BASE_CENTS // 100:,} в базовой валюте)"
            .replace(",", " ")
        )
    return True, ""
