"""
Числительные прописью: русский и узбекский (кириллица).

Зачем своя реализация, а не num2words:
  • узбекского в num2words нет вовсе (`NotImplementedError`), а в расписке
    сумма прописью — защита документа от подделки: откат на цифры её снимает;
  • num2words объявляет зависимостью `docopt`, который не собирается на
    современном setuptools («Failed building wheel for docopt») — установка
    падает, и сборка образа вместе с ней. При этом ни один модуль num2words
    docopt не импортирует, то есть зависимость ещё и лишняя.

Расписка печатает голое числительное в скобках («25 000 000 (двадцать пять
миллионов) сум» — слово валюты стоит в бланке). Счёт на оплату и товарная
накладная печатаются в валюте ЗАКАЗА, поэтому слово валюты согласуется с
числом здесь же (`currency_noun`, `money_in_words`): «1 доллар США», «2 доллара
США», «5 долларов США»; сум по-русски в документах не склоняется («1 сум»,
«5 сум»), по-узбекски существительное после числительного всегда в
единственном числе («беш АҚШ доллари»).
"""

from __future__ import annotations

from decimal import Decimal

# ─── Русский ──────────────────────────────────────────────────────────────────

_RU_UNITS_M = ("", "один", "два", "три", "четыре", "пять", "шесть", "семь",
               "восемь", "девять")
# Тысяча женского рода: «одна тысяча», «две тысячи». Остальные разряды мужского.
_RU_UNITS_F = ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь",
               "восемь", "девять")
_RU_TEENS = ("десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
             "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать")
_RU_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят",
            "семьдесят", "восемьдесят", "девяносто")
_RU_HUNDREDS = ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот",
                "семьсот", "восемьсот", "девятьсот")
# (единственное, 2-4, 5+) для каждого разряда
_RU_SCALES = (
    None,
    ("тысяча", "тысячи", "тысяч"),
    ("миллион", "миллиона", "миллионов"),
    ("миллиард", "миллиарда", "миллиардов"),
    ("триллион", "триллиона", "триллионов"),
)


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    """Форма слова для числа: 1 тысяча, 2 тысячи, 5 тысяч (с учётом 11-14)."""
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    last = n % 10
    if last == 1:
        return forms[0]
    if 2 <= last <= 4:
        return forms[1]
    return forms[2]


def _ru_group(n: int, feminine: bool) -> list[str]:
    """Трёхзначная группа прописью."""
    out = []
    if n >= 100:
        out.append(_RU_HUNDREDS[n // 100])
        n %= 100
    if 10 <= n <= 19:
        out.append(_RU_TEENS[n - 10])
        return out
    if n >= 20:
        out.append(_RU_TENS[n // 10])
        n %= 10
    if n:
        out.append((_RU_UNITS_F if feminine else _RU_UNITS_M)[n])
    return out


def ru_number(value: int) -> str:
    """Целое число прописью по-русски."""
    if value == 0:
        return "ноль"
    if value < 0:
        return "минус " + ru_number(-value)

    groups = []
    while value:
        groups.append(value % 1000)
        value //= 1000
    if len(groups) > len(_RU_SCALES):
        raise ValueError("число слишком велико для прописи")

    words: list[str] = []
    for idx in range(len(groups) - 1, -1, -1):
        g = groups[idx]
        if not g:
            continue
        # Женский род только у тысяч.
        words += _ru_group(g, feminine=(idx == 1))
        if idx:
            words.append(_ru_plural(g, _RU_SCALES[idx]))
    return " ".join(words)


# ─── Узбекский (кириллица) ────────────────────────────────────────────────────
#
# Узбекские числительные строго аддитивны: ни рода, ни согласования по числу —
# 1234 это «бир минг икки юз ўттиз тўрт». Поэтому таблица короче русской.

_UZ_UNITS = ("", "бир", "икки", "уч", "тўрт", "беш", "олти", "етти", "саккиз", "тўққиз")
_UZ_TENS = ("", "ўн", "йигирма", "ўттиз", "қирқ", "эллик", "олтмиш", "етмиш",
            "саксон", "тўқсон")
_UZ_SCALES = ("", "минг", "миллион", "миллиард", "триллион")


def _uz_group(n: int) -> list[str]:
    out = []
    if n >= 100:
        # «бир юз» не говорят — просто «юз».
        if n // 100 > 1:
            out.append(_UZ_UNITS[n // 100])
        out.append("юз")
        n %= 100
    if n >= 10:
        out.append(_UZ_TENS[n // 10])
        n %= 10
    if n:
        out.append(_UZ_UNITS[n])
    return out


def uz_number(value: int) -> str:
    """Целое число прописью по-узбекски (кириллица)."""
    if value == 0:
        return "нол"
    if value < 0:
        return "минус " + uz_number(-value)

    groups = []
    while value:
        groups.append(value % 1000)
        value //= 1000
    if len(groups) > len(_UZ_SCALES):
        raise ValueError("число слишком велико для прописи")

    words: list[str] = []
    for idx in range(len(groups) - 1, -1, -1):
        g = groups[idx]
        if not g:
            continue
        # «минг» без «бир»: 1000 — «минг», а не «бир минг».
        if not (idx == 1 and g == 1):
            words += _uz_group(g)
        if idx:
            words.append(_UZ_SCALES[idx])
    return " ".join(w for w in words if w)


# ─── Точка входа ──────────────────────────────────────────────────────────────

_LANGS = {"ru": ru_number, "uz": uz_number}


def amount_in_words(value: Decimal, lang: str) -> str:
    """Сумма прописью для скобок в документе.

    Копейки НЕ разворачиваем в слова, а печатаем дробью «50/100» — принятая
    в договорах форма, однозначная и не требующая согласования с валютой.
    Целая сумма остаётся без хвоста: «двадцать пять тысяч», а не
    «двадцать пять тысяч 00/100».
    """
    fn = _LANGS.get(lang)
    if fn is None:
        raise ValueError(f"нет прописи для языка {lang!r}")
    if value < 0:
        raise ValueError("сумма не может быть отрицательной")

    whole = int(value)
    cents = int((value - whole) * 100)
    words = fn(whole)
    return f"{words} {cents:02d}/100" if cents else words


# ─── Валюта прописью (счёт на оплату, товарная накладная) ────────────────────

# Код валюты → (формы существительного по-русски, по-узбекски,
# формы разменной единицы по-русски, по-узбекски). Сум — целыми: тийинов в
# расчётах нет (см. services/money), поэтому разменной единицы у него нет.
_CURRENCY_WORDS: dict[str, dict] = {
    "USD": {
        "ru": ("доллар США", "доллара США", "долларов США"),
        "uz": "АҚШ доллари",
        "minor_ru": ("цент", "цента", "центов"),
        "minor_uz": "цент",
    },
    "UZS": {
        "ru": ("сум", "сум", "сум"),
        "uz": "сўм",
        "minor_ru": None,
        "minor_uz": None,
    },
}


def currency_noun(value: int, lang: str, currency: str) -> str:
    """Слово валюты, согласованное с целым числом: 1 доллар США, 2 доллара США."""
    spec = _CURRENCY_WORDS.get((currency or "").upper())
    if spec is None:
        raise ValueError(f"нет слов для валюты {currency!r}")
    if lang == "ru":
        return _ru_plural(int(value), spec["ru"])
    if lang == "uz":
        return spec["uz"]
    raise ValueError(f"нет прописи для языка {lang!r}")


def money_in_words(value: Decimal, lang: str, currency: str) -> tuple[str, str]:
    """(числительное, хвост с валютой) для строки «Всего к оплате».

    Документ печатает «1 092 000 (один миллион девяносто две тысячи) сум.» —
    как в бланке владельца: числительное в скобках, валюта после. Хвост несёт
    согласованное слово валюты и центы, если они есть:
    «400,50 (четыреста) долларов США 50 центов.»
    """
    if value < 0:
        raise ValueError("сумма не может быть отрицательной")
    fn = _LANGS.get(lang)
    if fn is None:
        raise ValueError(f"нет прописи для языка {lang!r}")
    code = (currency or "").upper()
    spec = _CURRENCY_WORDS.get(code)
    if spec is None:
        raise ValueError(f"нет слов для валюты {currency!r}")
    whole = int(value)
    cents = int((value - whole) * 100)
    tail = currency_noun(whole, lang, code)
    minor = spec["minor_ru"] if lang == "ru" else spec["minor_uz"]
    if cents and minor:
        word = _ru_plural(cents, minor) if lang == "ru" else minor
        tail = f"{tail} {cents:02d} {word}"
    elif cents:
        # Валюта без разменной единицы, а дробь пришла — дробью, как в расписке.
        tail = f"{tail} {cents:02d}/100"
    return fn(whole), tail


def money_phrase(value: Decimal, lang: str, currency: str) -> str:
    """Сумма прописью одной фразой: «четыреста долларов США», «бир сўм»."""
    words, tail = money_in_words(value, lang, currency)
    return f"{words} {tail}"
