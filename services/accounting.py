"""
Бухгалтерия, этап 1: счета, журнал денежных операций, «Деньги сейчас».

Зачем. Денежный учёт до этого знал только «платёж по заказу» и «сдачу
наличных»: ни куда пришли деньги (касса, банковский счёт, чья карта), ни
расходов, ни остатка кассы на вечер в нём не было. Схема — в
`services/accounting_schema.py`, модель в двух словах:

* документ (`acc_docs`) — одно действие человека; строки (`acc_entries`) —
  движения по счетам внутри него. «Получил 500 USD наличными и 6,35 млн сум на
  карту» — один документ из двух строк, и отменяется он целиком;
* сумма строки хранится в валюте СЧЁТА, рядом — курс к базовой валюте, его
  источник (`cbu` — курс ЦБ на дату, `manual` — курс, который поставил
  менеджер; `base` — строка в базовой валюте) и сумма в базовой. Курс ЦБ
  сохраняется и при ручном курсе — видно, насколько менеджер от него отошёл;
* остаток счёта = приходы − расходы по ДЕЙСТВУЮЩИМ документам. Действующий —
  проведённый и не потерявший основание: платёж по заказу, который босс
  отклонил старой кнопкой в «Долгах», или удалённое поступление по рассрочке
  выводят документ из остатков сами, без хуков в чужих модулях
  (`EFFECTIVE_SQL`). Иначе касса показывала бы деньги, которых не было.

Долг НЕ считается здесь заново. «Получил деньги» по заказу пишет обычный
платёж `payments` в валюте заказа (сумма пересчитана по выбранному курсу), и
дальше долг уменьшает существующая логика: `services.debts` →
подтверждение босса → `_maybe_close_order_after_payment`. По рассрочке —
обычное поступление `machine_payment_receipts` через функции `services.machines`.
Платёж и строки журнала пишутся ОДНОЙ транзакцией: деньги в журнале без
платежа (или наоборот) — это расхождение, которое потом ищут руками.

Всё за выключателем `accounting_enabled` (app_settings, по умолчанию выкл.):
владелец сначала переносит историю склада, потом решает, с какой даты вести.
Пока выключено, ручки отвечают отказом, а старые экраны и потоки не меняются.

Роли собраны константами ниже — одно место, если владелец решит иначе.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from services import adb_core, money
from utils.helpers import local_now

logger = logging.getLogger(__name__)

SETTING_ENABLED = "accounting_enabled"
SETTING_START_DATE = "accounting_start_date"

ACCOUNT_KINDS = {"cash": "Касса", "bank": "Банковский счёт", "card": "Карта"}
DOC_KINDS = ("opening", "receipt", "expense", "transfer", "exchange", "reconcile")

# Кто записывает деньги (получил, расход, перевод, закрыть день). Бухгалтера
# сейчас нет, его работу делает менеджер, но роль в системе есть — не
# закрываем ей дверь заранее.
ROLES_RECORD = ("admin", "boss", "manager", "bookkeeper")
# Справочник счетов, выключатель, отмена чужих документов, сторно
# подтверждённого платежа. Поступления по рассрочке записывает и менеджер
# (решение владельца: продажи и рассрочки техники — его работа).
ROLES_MANAGE = ("admin", "boss")
# Кто видит ВСЕ документы журнала. Менеджер видит свои: «менеджер вносит свои
# расходы, руководитель видит все» (требование владельца).
ROLES_SEE_ALL = ("admin", "boss", "bookkeeper")

# Сколько лишнего (в минорных единицах валюты долга) можно принять сверх
# остатка: пересчёт сумов в доллары округляет каждую строку до цента, а клиент
# отдаёт круглую сумму. Больше — это уже переплата, её надо решать отдельно.
OVERPAY_TOLERANCE_MINOR = 100

MAX_LINES = 10
NOTE_MAX = 500
_RATE_Q = Decimal("0.0001")


class AccountingError(Exception):
    """Ошибка формы/правил — текст показывается человеку как есть."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Actor:
    user_id: int
    name: str
    role: str
    username: str = ""


@dataclass(frozen=True)
class RateInfo:
    quote: Decimal          # единиц валюты за 1 единицу базовой
    source: str             # base | cbu | manual
    cbu: Decimal | None     # курс ЦБ на дату (для сравнения), None — не нашли


# ─── Общие мелочи ────────────────────────────────────────────────────────────


def _db():
    # Лениво: тесты перезагружают services.database (importlib.reload) под
    # свою базу, и ссылка, взятая на импорте, указывала бы на старые настройки.
    from services import database

    return database


def base_currency() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


def today_str() -> str:
    """Бизнес-дата: контейнер работает в зоне бизнеса (TZ=Asia/Tashkent),
    `created_at` пишется в ней же (CLAUDE.md, раздел Time)."""
    return local_now().date().isoformat()


def _now_str() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M:%S")


def _pg() -> bool:
    return adb_core._use_postgres()


async def _insert_id(conn: Any, sql: str, *args: Any) -> int:
    if _pg():
        return int(await conn.fetchval(sql + " RETURNING id", *args))
    await conn.execute(sql, *args)
    return int(await conn.fetchval("SELECT last_insert_rowid()"))


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _fmt(cents: int) -> str:
    """Сумма для текста: «1 000», «999.99» — копейки только когда они есть."""
    return money.format_cents(int(cents), decimals=2, sep=" ", trim=True)


def fmt_rate(q: Decimal | None) -> str | None:
    """Курс строкой без экспоненты и хвостовых нулей: «12700.5», «1»."""
    if q is None:
        return None
    s = format(q.normalize(), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def parse_rate(raw: Any) -> Decimal | None:
    """Курс из ввода: «12 700,50» → Decimal. None — не число / не положительный."""
    if raw is None:
        return None
    text = str(raw).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    if not text:
        return None
    try:
        q = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not q.is_finite() or q <= 0 or q > Decimal("1000000000"):
        return None
    return q.quantize(_RATE_Q, rounding=ROUND_HALF_UP)


# ─── Свой курс: насколько можно отойти от ЦБ ─────────────────────────────────
#
# Курс, введённый руками, раньше ничем не ограничивался: «12 130 сум по курсу 1»
# закрывали долларовый заказ на 12 130 USD карманной мелочью. Теперь свой курс
# не дальше `app_settings.manual_rate_max_deviation_pct` процентов от курса ЦБ
# (для всех ролей), а без курса ЦБ свой ставит только руководство — сверить
# его не с чем. Одна проверка на разбивку оплаты (`order_payments.compute_parts`)
# и документы бухгалтерии (`resolve_rates`).
MANUAL_RATE_MAX_DEVIATION_PCT_DEFAULT = Decimal(10)
MANUAL_RATE_WITHOUT_CBU_ROLES = ("admin", "boss")


def manual_rate_max_deviation_pct() -> Decimal:
    from services.database import get_setting

    raw = get_setting("manual_rate_max_deviation_pct", float(MANUAL_RATE_MAX_DEVIATION_PCT_DEFAULT))
    try:
        pct = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return MANUAL_RATE_MAX_DEVIATION_PCT_DEFAULT
    if not pct.is_finite() or pct < 0:
        return MANUAL_RATE_MAX_DEVIATION_PCT_DEFAULT
    return pct


def manual_rate_refusal(currency: str, manual: Decimal | None, cbu: Decimal | None,
                        role: str | None) -> str | None:
    """Текст отказа своему курсу или None, если курс допустим.

    `role` — НАСТОЯЩАЯ роль (совмещение ролей права руководителя не даёт);
    None — считаем не руководителем.
    """
    if manual is None:
        return None
    cur = (currency or "").upper()
    if cbu is None or cbu <= 0:
        if role in MANUAL_RATE_WITHOUT_CBU_ROLES:
            return None
        return (f"Курса ЦБ для {cur} на эту дату нет — свой курс может поставить только "
                "руководитель. Попросите руководителя внести оплату или повторите, когда появится курс ЦБ")
    if manual == cbu:
        return None
    limit = manual_rate_max_deviation_pct()
    deviation = abs(manual - cbu) * 100 / cbu
    if deviation <= limit:
        return None
    return (f"Курс {cur} {fmt_rate(manual)} отличается от курса ЦБ {fmt_rate(cbu)} на "
            f"{deviation.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}% — допустимо не больше "
            f"{fmt_rate(limit)}%. Проверьте курс: это {cur} за 1 {base_currency()}")


def parse_cents(raw: Any, *, allow_zero: bool = False) -> int | None:
    """Сумма из ввода в минорных единицах. `allow_zero` — для пересчёта кассы,
    где «0» — законный ответ (касса пустая)."""
    if raw is None:
        return None
    if allow_zero:
        text = str(raw).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
        if text in ("0", "0.0", "0.00"):
            return 0
    return money.parse_amount(str(raw))


def to_base_cents(cents: int, currency: str, quote: Decimal) -> int:
    """Сумма в валюте → базовая. `quote` — единиц валюты за 1 базовую."""
    if currency.upper() == base_currency():
        return int(cents)
    return int((Decimal(int(cents)) / quote).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def from_base_cents(base_cents: int, currency: str, quote: Decimal) -> int:
    if currency.upper() == base_currency():
        return int(base_cents)
    return int((Decimal(int(base_cents)) * quote).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def convert_cents(cents: int, from_cur: str, to_cur: str, rates: dict[str, RateInfo]) -> int:
    """Пересчёт строки в валюту долга через базовую. Одна валюта — без
    округлений вовсе: 500 USD в долларовый долг это ровно 500 USD."""
    f, t = from_cur.upper(), to_cur.upper()
    if f == t:
        return int(cents)
    base = to_base_cents(cents, f, rates[f].quote) if f != base_currency() else int(cents)
    return from_base_cents(base, t, rates[t].quote) if t != base_currency() else base


def _parse_date(raw: Any, *, default: str) -> str:
    text = str(raw or "").strip()[:10]
    if not text:
        return default
    try:
        d = date.fromisoformat(text)
    except ValueError as e:
        raise AccountingError("Дата должна быть в формате ГГГГ-ММ-ДД") from e
    if d.isoformat() > today_str():
        raise AccountingError("Дата не может быть в будущем")
    return d.isoformat()


async def _audit(actor: Actor, action: str, details: str) -> None:
    try:
        await asyncio.to_thread(
            _db().add_audit_log, actor.user_id, actor.name, actor.role, action, details
        )
    except Exception:
        # Аудит — след, а не условие: деньги уже записаны и закоммичены.
        logger.exception("accounting: аудит %s не записан", action)


# ─── Выключатель ─────────────────────────────────────────────────────────────


async def get_state() -> dict:
    db = _db()
    enabled = await asyncio.to_thread(db.get_setting, SETTING_ENABLED, False)
    start = await asyncio.to_thread(db.get_setting, SETTING_START_DATE, None)
    return {
        "enabled": bool(enabled),
        "start_date": start or None,
        "base_currency": base_currency(),
        "today": today_str(),
    }


async def is_enabled() -> bool:
    return (await get_state())["enabled"]


async def require_enabled() -> None:
    if not await is_enabled():
        raise AccountingError("Бухгалтерия выключена — включает руководитель", status=409)


async def set_enabled(actor: Actor, enabled: bool, start_date: str | None = None) -> dict:
    if actor.role not in ROLES_MANAGE:
        raise AccountingError("Включает и выключает бухгалтерию руководитель", status=403)
    db = _db()
    if start_date:
        start = _parse_date(start_date, default=today_str())
        await asyncio.to_thread(db.set_setting, SETTING_START_DATE, start, actor.user_id)
    elif enabled and not await asyncio.to_thread(db.get_setting, SETTING_START_DATE, None):
        # Дата старта нужна как дата начальных остатков по умолчанию.
        await asyncio.to_thread(db.set_setting, SETTING_START_DATE, today_str(), actor.user_id)
    await asyncio.to_thread(db.set_setting, SETTING_ENABLED, bool(enabled), actor.user_id)
    await _audit(actor, "accounting_toggled", "включена" if enabled else "выключена")
    return await get_state()


# ─── Курсы ───────────────────────────────────────────────────────────────────


def _cbu_quote_sync(currency: str, day: str) -> Decimal | None:
    """Курс ЦБ на дату как «единиц валюты за 1 базовую».

    Архив `currency_rate_daily` (его пишет tasks/run_fx_sync) — ближайший день
    не позже даты; нет архива — текущий `currency_rates`. Хранится там
    «1 UZS = X USD», человеку нужен обратный — «сум за доллар».
    """
    db = _db()
    cur = currency.upper()
    if cur == base_currency():
        return Decimal(1)
    rate = db.get_currency_rate_asof(cur, day) or db.current_rate_to_base(cur)
    if not rate or rate <= 0:
        return None
    quote = (Decimal(1) / Decimal(str(rate))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Курс, заведённый «наоборот» (12 500 вместо 1/12 500), даёт 0.00 — это не
    # курс, а опечатка в справочнике; делить на ноль из-за неё нельзя.
    return quote if quote > 0 else None


async def cbu_quotes(currencies: list[str], day: str | None = None) -> dict[str, Decimal | None]:
    day = day or today_str()
    out: dict[str, Decimal | None] = {}
    for cur in dict.fromkeys(c.upper() for c in currencies if c):
        out[cur] = await asyncio.to_thread(_cbu_quote_sync, cur, day)
    return out


async def known_currencies() -> list[str]:
    """Валюты счетов — те же, что разрешены платежам (`config.ALLOWED_CURRENCIES`),
    базовая первой."""
    from config import ALLOWED_CURRENCIES

    base = base_currency()
    return [base] + [c.upper() for c in ALLOWED_CURRENCIES if c.upper() != base]


async def rates_view(day: str | None = None) -> dict:
    day = day or today_str()
    currencies = [c for c in await known_currencies() if c != base_currency()]
    quotes = await cbu_quotes(currencies, day)
    return {
        "base_currency": base_currency(),
        "date": day,
        "rates": {c: {"cbu": fmt_rate(q)} for c, q in quotes.items()},
    }


async def resolve_rates(
    currencies: set[str], client_rates: dict | None, day: str, role: str | None = None
) -> dict[str, RateInfo]:
    """Курс по каждой не-базовой валюте документа.

    По умолчанию — ЦБ на дату документа. Менеджер может поставить свой: тогда
    источник `manual`, а курс ЦБ всё равно сохраняется рядом (свой курс — не
    дальше допуска от ЦБ, без ЦБ — только руководство: `manual_rate_refusal`,
    `role` — настоящая роль автора документа). Своего курса нет и
    ЦБ не знает валюту — отказ с просьбой указать курс: молча посчитать по
    единице значило бы записать сумы долларами.
    """
    client_rates = client_rates or {}
    base = base_currency()
    need = sorted(c.upper() for c in currencies if c and c.upper() != base)
    cbu = await cbu_quotes(need, day)
    out: dict[str, RateInfo] = {base: RateInfo(Decimal(1), "base", Decimal(1))}
    for cur in need:
        raw = client_rates.get(cur)
        if isinstance(raw, dict):
            raw = raw.get("rate")
        manual = parse_rate(raw) if raw not in (None, "") else None
        if raw not in (None, "") and manual is None:
            raise AccountingError(f"Курс {cur}: введите положительное число")
        cbu_q = cbu.get(cur)
        if manual is not None:
            refusal = manual_rate_refusal(cur, manual, cbu_q, role)
            if refusal:
                raise AccountingError(refusal)
            source = "cbu" if cbu_q is not None and manual == cbu_q else "manual"
            out[cur] = RateInfo(manual, source, cbu_q)
        elif cbu_q is not None:
            out[cur] = RateInfo(cbu_q, "cbu", cbu_q)
        else:
            raise AccountingError(f"Нет курса ЦБ для {cur} — укажите курс вручную")
    return out


def _check_ceiling(cents: int, currency: str, rates: dict[str, RateInfo]) -> None:
    info = rates.get(currency.upper())
    rate_to_base = (Decimal(1) / info.quote) if info else None
    ok, err = money.validate_cents(cents, rate_to_base)
    if not ok:
        raise AccountingError(err)


# ─── Остатки: какие документы действуют ──────────────────────────────────────

# Проведённый документ перестаёт действовать, когда у него пропало основание:
# платёж по заказу отклонён (кнопкой в «Долгах» или в боте) или поступление по
# рассрочке удалено из карточки сделки. Условие одно на остатки, сверку и
# журнал — второй вариант этой формулы разошёлся бы с первым.
EFFECTIVE_SQL = (
    "d.status = 'posted' "
    "AND (d.payment_id IS NULL OR NOT EXISTS ("
    "  SELECT 1 FROM payments p WHERE p.id = d.payment_id AND p.status = 'rejected')) "
    "AND (d.machine_receipt_id IS NULL OR EXISTS ("
    "  SELECT 1 FROM machine_payment_receipts r WHERE r.id = d.machine_receipt_id))"
)


async def account_balance(account_id: int, conn: Any = None) -> int:
    db = conn if conn is not None else adb_core
    row = await db.fetchrow(
        "SELECT "
        "COALESCE(SUM(CASE WHEN e.direction = 'in' THEN e.amount_cents ELSE 0 END), 0) AS i, "
        "COALESCE(SUM(CASE WHEN e.direction = 'out' THEN e.amount_cents ELSE 0 END), 0) AS o "
        "FROM acc_entries e JOIN acc_docs d ON d.id = e.doc_id "
        f"WHERE e.account_id = $1 AND {EFFECTIVE_SQL}",
        int(account_id),
    )
    return int(row["i"] or 0) - int(row["o"] or 0) if row else 0


def _account_view(a: dict) -> dict:
    return {
        "id": int(a["id"]),
        "name": a["name"],
        "kind": a["kind"],
        "kind_label": ACCOUNT_KINDS.get(a["kind"], a["kind"]),
        "currency": (a["currency"] or "").upper(),
        "bank": a.get("bank") or "",
        "card_last4": a.get("card_last4") or "",
        "holder": a.get("holder") or "",
        "note": a.get("note") or "",
        "archived": bool(a.get("archived_at")),
    }


async def list_accounts(include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM acc_accounts"
    if not include_archived:
        sql += " WHERE archived_at IS NULL"
    sql += " ORDER BY archived_at IS NOT NULL, kind, name, id"
    rows = await adb_core.fetch(sql)
    accounts = [_account_view(r) for r in rows]
    if not accounts:
        return []
    ids = [a["id"] for a in accounts]
    ph = ", ".join(f"${i + 1}" for i in range(len(ids)))
    openings = await adb_core.fetch(
        "SELECT e.account_id, e.amount_cents, d.doc_date FROM acc_entries e "
        "JOIN acc_docs d ON d.id = e.doc_id "
        f"WHERE d.kind = 'opening' AND d.status = 'posted' AND e.account_id IN ({ph})",
        *ids,
    )
    by_acc = {int(r["account_id"]): r for r in openings}
    for a in accounts:
        op = by_acc.get(a["id"])
        a["opening_cents"] = int(op["amount_cents"]) if op else 0
        a["opening_date"] = op["doc_date"] if op else None
    return accounts


async def balances(actor: Actor | None = None) -> dict:
    """«Деньги сейчас»: остаток каждого счёта в его валюте, движение за
    сегодня и итог ≈ в базовой по ТЕКУЩЕМУ курсу (вопрос «сколько у нас сейчас»,
    а не «сколько было»). Валюта без курса в итог не входит — итог помечается
    `partial`, как в дебиторке: молча сложенные сумы с долларами хуже пропуска.
    """
    today = today_str()
    accounts = await list_accounts(include_archived=False)
    rows = await adb_core.fetch(
        "SELECT e.account_id, e.direction, "
        "COALESCE(SUM(e.amount_cents), 0) AS s, "
        "COALESCE(SUM(CASE WHEN d.doc_date = $1 THEN e.amount_cents ELSE 0 END), 0) AS t "
        "FROM acc_entries e JOIN acc_docs d ON d.id = e.doc_id "
        f"WHERE {EFFECTIVE_SQL} GROUP BY e.account_id, e.direction",
        today,
    )
    agg: dict[int, dict[str, int]] = {}
    for r in rows:
        slot = agg.setdefault(int(r["account_id"]), {"in": 0, "out": 0, "t_in": 0, "t_out": 0})
        if r["direction"] == "in":
            slot["in"] += int(r["s"] or 0)
            slot["t_in"] += int(r["t"] or 0)
        else:
            slot["out"] += int(r["s"] or 0)
            slot["t_out"] += int(r["t"] or 0)
    closes = await adb_core.fetch(
        "SELECT account_id, MAX(close_date) AS last_close FROM acc_day_closes GROUP BY account_id"
    )
    last_close = {int(r["account_id"]): r["last_close"] for r in closes}

    db = _db()
    total_base = Decimal(0)
    partial = False
    by_currency: dict[str, int] = {}
    for a in accounts:
        s = agg.get(a["id"], {"in": 0, "out": 0, "t_in": 0, "t_out": 0})
        a["balance_cents"] = s["in"] - s["out"]
        a["today_in_cents"] = s["t_in"]
        a["today_out_cents"] = s["t_out"]
        a["last_close_date"] = last_close.get(a["id"])
        by_currency[a["currency"]] = by_currency.get(a["currency"], 0) + a["balance_cents"]
    for cur, cents in by_currency.items():
        rate = await asyncio.to_thread(db.current_rate_to_base, cur)
        if rate is None:
            partial = True if cents else partial
            continue
        total_base += Decimal(cents) * Decimal(str(rate))
    return {
        "today": today,
        "base_currency": base_currency(),
        "accounts": accounts,
        "by_currency": [{"currency": c, "cents": v} for c, v in sorted(by_currency.items())],
        "total_base_cents": int(total_base.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
        "partial": partial,
    }


# ─── Справочник счетов ───────────────────────────────────────────────────────


async def _load_accounts(ids: list[int], conn: Any = None) -> dict[int, dict]:
    db = conn if conn is not None else adb_core
    uniq = [int(i) for i in dict.fromkeys(ids)]
    if not uniq:
        return {}
    ph = ", ".join(f"${i + 1}" for i in range(len(uniq)))
    rows = await db.fetch(f"SELECT * FROM acc_accounts WHERE id IN ({ph})", *uniq)
    return {int(r["id"]): r for r in rows}


def _id_arg(raw: Any, not_found: str) -> int:
    """id из запроса; мусор — «не найден» (404), а не 500 на int()."""
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise AccountingError(not_found, status=404) from e
    if value <= 0:
        raise AccountingError(not_found, status=404)
    return value


def _account_arg(raw: Any, label: str = "Счёт") -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise AccountingError(f"{label}: выберите счёт") from e
    if value <= 0:
        raise AccountingError(f"{label}: выберите счёт")
    return value


async def _active_account(account_id: int, label: str = "Счёт") -> dict:
    acc = (await _load_accounts([account_id])).get(account_id)
    if not acc:
        raise AccountingError(f"{label}: счёт не найден", status=404)
    if acc.get("archived_at"):
        raise AccountingError(f"{label}: «{acc['name']}» в архиве")
    return acc


async def save_account(actor: Actor, data: dict) -> dict:
    """Завести или изменить счёт. Начальный остаток — документ `opening`
    в журнале, а не поле счёта: остаток тогда считается одной формулой, а
    правка начального остатка остаётся в истории (старый документ отменяется
    с причиной, новый проводится)."""
    if actor.role not in ROLES_MANAGE:
        raise AccountingError("Счета заводит руководитель", status=403)
    account_id = data.get("account_id") or data.get("id")
    name = _text(data.get("name"), 80)
    if not name:
        raise AccountingError("Название счёта обязательно")
    kind = str(data.get("kind") or "").strip()
    if kind not in ACCOUNT_KINDS:
        raise AccountingError("Тип счёта: касса, банковский счёт или карта")
    currency = str(data.get("currency") or "").strip().upper()
    if currency not in await known_currencies():
        raise AccountingError("Валюта счёта не поддерживается")
    last4 = "".join(ch for ch in str(data.get("card_last4") or "") if ch.isdigit())
    if last4 and len(last4) != 4:
        raise AccountingError("Последние цифры карты — ровно 4")
    bank = _text(data.get("bank"), 40)
    holder = _text(data.get("holder"), 80)
    note = _text(data.get("note"), 200)

    opening_given = "opening" in data and data.get("opening") not in (None,)
    opening_cents = 0
    if opening_given:
        raw = data.get("opening")
        if str(raw).strip() == "":
            opening_cents = 0
        else:
            parsed = parse_cents(raw, allow_zero=True)
            if parsed is None:
                raise AccountingError("Начальный остаток: введите сумму (0, если пусто)")
            opening_cents = parsed
    state = await get_state()
    opening_date = _parse_date(data.get("opening_date"), default=state["start_date"] or today_str())
    rates = await resolve_rates({currency}, None, opening_date) if opening_cents else {}
    if opening_cents:
        _check_ceiling(opening_cents, currency, rates)

    now = _now_str()
    async with adb_core.transaction() as txn:
        if account_id:
            account_id = _account_arg(account_id)
            cur_row = await txn.fetchrow("SELECT * FROM acc_accounts WHERE id = $1", account_id)
            if not cur_row:
                raise AccountingError("Счёт не найден", status=404)
            if (cur_row["currency"] or "").upper() != currency:
                used = await txn.fetchval(
                    "SELECT COUNT(*) FROM acc_entries WHERE account_id = $1", account_id
                )
                if int(used or 0):
                    raise AccountingError(
                        "Валюту счёта с операциями не меняют — заведите новый счёт"
                    )
            await txn.execute(
                "UPDATE acc_accounts SET name = $1, kind = $2, currency = $3, bank = $4, "
                "card_last4 = $5, holder = $6, note = $7, updated_at = $8 WHERE id = $9",
                name, kind, currency, bank or None, last4 or None, holder or None,
                note or None, now, account_id,
            )
            created = False
        else:
            account_id = await _insert_id(
                txn,
                "INSERT INTO acc_accounts (name, kind, currency, bank, card_last4, holder, note, "
                "created_by, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)",
                name, kind, currency, bank or None, last4 or None, holder or None, note or None,
                actor.user_id, now,
            )
            created = True

        if opening_given:
            prev = await txn.fetch(
                "SELECT d.id, e.amount_cents, d.doc_date FROM acc_docs d "
                "JOIN acc_entries e ON e.doc_id = d.id "
                "WHERE d.kind = 'opening' AND d.status = 'posted' AND e.account_id = $1",
                account_id,
            )
            same = (
                len(prev) == 1
                and int(prev[0]["amount_cents"]) == opening_cents
                and prev[0]["doc_date"] == opening_date
            ) or (not prev and opening_cents == 0)
            if not same:
                for p in prev:
                    await txn.execute(
                        "UPDATE acc_docs SET status = 'void', void_reason = $1, voided_by = $2, "
                        "voided_by_name = $3, voided_at = $4 WHERE id = $5 AND status = 'posted'",
                        "Начальный остаток изменён в справочнике", actor.user_id, actor.name,
                        now, int(p["id"]),
                    )
                if opening_cents:
                    doc_id = await _insert_doc(
                        txn, actor, kind="opening", doc_date=opening_date,
                        note="Начальный остаток", now=now,
                    )
                    await _insert_entry(
                        txn, doc_id, account_id, "in", opening_cents, currency,
                        rates[currency],
                    )
    await _audit(
        actor, "accounting_account_saved",
        f"{'новый' if created else 'изменён'} счёт #{account_id} «{name}» {currency}"
        + (f", начальный остаток {_fmt(opening_cents)}" if opening_given else ""),
    )
    accounts = await list_accounts(include_archived=True)
    return next(a for a in accounts if a["id"] == account_id)


async def set_archived(actor: Actor, account_id: Any, archived: bool) -> dict:
    if actor.role not in ROLES_MANAGE:
        raise AccountingError("Счета в архив убирает руководитель", status=403)
    account_id = _account_arg(account_id)
    rc = await adb_core.execute(
        "UPDATE acc_accounts SET archived_at = $1, updated_at = $2 WHERE id = $3",
        _now_str() if archived else None, _now_str(), account_id,
    )
    if rc <= 0:
        raise AccountingError("Счёт не найден", status=404)
    await _audit(
        actor, "accounting_account_archived",
        f"счёт #{account_id} {'в архив' if archived else 'из архива'}",
    )
    return {"ok": True, "account_id": account_id, "archived": bool(archived)}


# ─── Запись документа ────────────────────────────────────────────────────────


def _request_key(actor: Actor, kind: str, raw: Any) -> str | None:
    # Ключ в пространстве пользователя и вида операции: чужой ключ (или тот же
    # UUID, случайно отправленный в другую форму) не вернёт чужой документ.
    if not raw:
        return None
    return f"{kind}:{actor.user_id}:{str(raw)[:128]}"


async def _find_by_key(key: str | None) -> dict | None:
    if not key:
        return None
    return await adb_core.fetchrow("SELECT * FROM acc_docs WHERE request_key = $1", key)


async def _insert_doc(
    txn: Any, actor: Actor, *, kind: str, doc_date: str, now: str, request_key: str | None = None,
    order_id: int | None = None, deal_id: int | None = None, counterparty: str | None = None,
    target_currency: str | None = None, target_cents: int | None = None,
    category: str | None = None, note: str | None = None,
) -> int:
    return await _insert_id(
        txn,
        "INSERT INTO acc_docs (kind, doc_date, request_key, order_id, deal_id, counterparty, "
        "target_currency, target_cents, category, note, status, created_by, created_by_name, "
        "created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'posted', $11, $12, $13)",
        kind, doc_date, request_key, order_id, deal_id, counterparty, target_currency,
        target_cents, category, note, actor.user_id, actor.name, now,
    )


async def _insert_entry(
    txn: Any, doc_id: int, account_id: int, direction: str, cents: int, currency: str,
    rate: RateInfo, *, base_cents: int | None = None, target_cents: int | None = None,
) -> None:
    currency = currency.upper()
    if base_cents is None:
        base_cents = to_base_cents(cents, currency, rate.quote)
    await txn.execute(
        "INSERT INTO acc_entries (doc_id, account_id, direction, amount_cents, currency, rate, "
        "rate_source, cbu_rate, amount_base_cents, target_cents) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        doc_id, account_id, direction, int(cents), currency, fmt_rate(rate.quote),
        rate.source, fmt_rate(rate.cbu), int(base_cents), target_cents,
    )


async def _run_idempotent(key: str | None, work) -> tuple[dict, bool]:
    """Выполнить запись; повтор с тем же ключом отдаёт прежний документ.

    Ключ — UNIQUE в `acc_docs`, и документ вставляется ПЕРВЫМ в транзакции:
    параллельный двойной тап на Postgres ждёт первую транзакцию на индексе и
    падает на уникальности, после чего мы находим уже записанный документ.
    Отдельной таблицы ключей нет — ключ живёт вместе с тем, что он защищает.
    """
    prev = await _find_by_key(key)
    if prev is not None:
        return prev, True
    try:
        doc_id = await work()
    except Exception:
        prev = await _find_by_key(key)
        if prev is not None:
            return prev, True
        raise
    row = await adb_core.fetchrow("SELECT * FROM acc_docs WHERE id = $1", doc_id)
    return row or {"id": doc_id}, False


# ─── «Получил деньги» ────────────────────────────────────────────────────────

_ORDER_OPEN_STATUSES = ("approved", "shipped", "partially_returned")


async def _lock_account(txn: Any, account_id: int) -> None:
    """Строка счёта под `FOR UPDATE` до конца транзакции (Postgres). На SQLite
    пишущая транзакция и так одна (`BEGIN IMMEDIATE`)."""
    if _pg():
        await txn.fetchrow("SELECT id FROM acc_accounts WHERE id = $1 FOR UPDATE", account_id)


async def _lock_order(txn: Any, order_id: int) -> dict | None:
    sql = (
        "SELECT id, user_id, payment_type, currency, agent_name, paid_confirmed_at, status "
        "FROM orders WHERE id = $1"
    )
    if _pg():
        sql += " FOR UPDATE"
    return await txn.fetchrow(sql, order_id)


def _order_guard(order: dict | None) -> None:
    # Те же условия, что у `mark_order_paid`: оплату принимаем по активному
    # заказу в долг/«оплата сразу», ещё не закрытому.
    if not order:
        raise AccountingError("Заказ не найден", status=404)
    if order["payment_type"] not in ("credit", "paid"):
        raise AccountingError("По этому заказу оплату не принимают")
    if order["paid_confirmed_at"] is not None:
        raise AccountingError("Заказ уже полностью оплачен")
    if order["status"] not in _ORDER_OPEN_STATUSES:
        raise AccountingError("Заказ не одобрен — оплату по нему не принимают")


async def _deal_remaining_cents(txn: Any, deal_id: int) -> int:
    """Остаток по рассрочке — тем же способом, что строка «Долгов»
    (`receivables.machine_debt_rows`): неоплаченные платежи графика минус
    частичные поступления, разложенные `machines.allocate_receipts`."""
    from services.machines import allocate_receipts

    schedule = await txn.fetch(
        "SELECT id, seq, amount_cents, paid_at FROM machine_deal_payments "
        "WHERE deal_id = $1 ORDER BY seq",
        deal_id,
    )
    received = int(await txn.fetchval(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM machine_payment_receipts WHERE deal_id = $1",
        deal_id,
    ) or 0)
    covered = {int(r["id"]): int(r["covered_cents"]) for r in allocate_receipts(
        [dict(r) for r in schedule], received)}
    rest = 0
    for r in schedule:
        if int(r["seq"] or 0) == 0 or r["paid_at"]:
            continue
        rest += max(0, int(r["amount_cents"]) - covered.get(int(r["id"]), 0))
    return rest


def _parse_lines(raw_lines: Any) -> list[tuple[int, int]]:
    if not isinstance(raw_lines, list) or not raw_lines:
        raise AccountingError("Добавьте хотя бы одну строку: счёт и сумма")
    if len(raw_lines) > MAX_LINES:
        raise AccountingError(f"Не больше {MAX_LINES} строк в одном поступлении")
    out = []
    for n, line in enumerate(raw_lines, start=1):
        if not isinstance(line, dict):
            raise AccountingError(f"Строка {n}: неверный формат")
        account_id = _account_arg(line.get("account_id"), f"Строка {n}")
        cents = parse_cents(line.get("amount"))
        if cents is None:
            raise AccountingError(f"Строка {n}: введите сумму больше нуля")
        out.append((account_id, cents))
    return out


def _receipt_result(doc: dict, *, repeated: bool, extra: dict | None = None) -> dict:
    res = {
        "ok": True,
        "doc_id": int(doc["id"]),
        "repeated": repeated,
        "payment_id": doc.get("payment_id"),
        "machine_receipt_id": doc.get("machine_receipt_id"),
        "target_currency": doc.get("target_currency"),
        "credited_cents": doc.get("target_cents"),
    }
    if extra:
        res.update(extra)
    return res


async def record_receipt(actor: Actor, data: dict) -> dict:
    """Деньги от клиента по заказу или по рассрочке — несколько строк
    «счёт + сумма», валюта строки = валюта счёта.

    Каждая строка пересчитывается в валюту долга по курсу документа (ЦБ на
    дату или курс менеджера), сумма пересчётов — это то, что гасит долг. Её
    записываем существующим механизмом: платёж `payments` в валюте заказа
    (дальше — обычное подтверждение и закрытие заказа) или поступление по
    рассрочке. Оплата долларового заказа сумами тем самым гасит долг ровно по
    выбранному курсу, а в кассе остаются именно сумы.
    """
    await require_enabled()
    if actor.role not in ROLES_RECORD:
        raise AccountingError("Нет доступа", status=403)
    key = _request_key(actor, "receipt", data.get("idempotency_key"))
    prev = await _find_by_key(key)
    if prev is not None:
        return _receipt_result(prev, repeated=True)

    if bool(data.get("order_id")) == bool(data.get("deal_id")):
        raise AccountingError("Укажите основание: заказ или рассрочку")
    order_id = _id_arg(data.get("order_id"), "Заказ не найден") if data.get("order_id") else 0
    deal_id = _id_arg(data.get("deal_id"), "Сделка не найдена") if data.get("deal_id") else 0
    lines = _parse_lines(data.get("lines"))
    accounts = await _load_accounts([a for a, _ in lines])
    for n, (acc_id, _) in enumerate(lines, start=1):
        acc = accounts.get(acc_id)
        if not acc:
            raise AccountingError(f"Строка {n}: счёт не найден", status=404)
        if acc.get("archived_at"):
            raise AccountingError(f"Строка {n}: счёт «{acc['name']}» в архиве")
    note = _text(data.get("note"), NOTE_MAX)
    doc_date = today_str()

    if order_id:
        head = await adb_core.fetchrow(
            "SELECT id, user_id, currency, agent_name FROM orders WHERE id = $1", order_id
        )
        if not head:
            raise AccountingError("Заказ не найден", status=404)
        if int(head["user_id"]) != actor.user_id and actor.role not in ROLES_SEE_ALL:
            raise AccountingError("Оплату по чужому заказу записывает руководитель", status=403)
        target_cur = (head["currency"] or base_currency()).upper()
        counterparty = head["agent_name"] or None
    else:
        head = await adb_core.fetchrow(
            "SELECT id, currency, buyer_name, kind FROM machine_deals WHERE id = $1", deal_id
        )
        if not head:
            raise AccountingError("Сделка не найдена", status=404)
        if head["kind"] != "credit":
            raise AccountingError("Поступления записываются только по рассрочке")
        target_cur = (head["currency"] or base_currency()).upper()
        counterparty = head["buyer_name"] or None

    currencies = {str(accounts[a]["currency"]).upper() for a, _ in lines} | {target_cur}
    rates = await resolve_rates(currencies, data.get("rates"), doc_date, actor.role)
    computed = []
    for acc_id, cents in lines:
        cur = str(accounts[acc_id]["currency"]).upper()
        _check_ceiling(cents, cur, rates)
        computed.append((acc_id, cents, cur, convert_cents(cents, cur, target_cur, rates)))
    total_target = sum(c[3] for c in computed)
    if total_target <= 0:
        raise AccountingError("Сумма в валюте долга получилась нулевой — проверьте курс")
    _check_ceiling(total_target, target_cur, rates)
    now = _now_str()
    info: dict[str, Any] = {}

    async def work() -> int:
        async with adb_core.transaction() as txn:
            doc_id = await _insert_doc(
                txn, actor, kind="receipt", doc_date=doc_date, now=now, request_key=key,
                order_id=order_id or None, deal_id=deal_id or None, counterparty=counterparty,
                target_currency=target_cur, target_cents=total_target, note=note or None,
            )
            if order_id:
                from services.debts import calc_claimable_cents

                order = await _lock_order(txn, order_id)
                _order_guard(order)
                claimable = (await calc_claimable_cents([order_id], conn=txn)).get(order_id, 0)
                credited = _credit_or_raise(total_target, claimable, target_cur)
                comment = f"Оплата по заказу #{order_id}" + (
                    f" ({counterparty})" if counterparty else "") + f" · журнал денег №{doc_id}"
                payment_id = await _insert_id(
                    txn,
                    "INSERT INTO payments (user_id, username, full_name, amount_cents, currency, "
                    "comment, status, created_at, order_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)",
                    actor.user_id, actor.username, actor.name, credited, target_cur, comment,
                    now, order_id,
                )
                await txn.execute(
                    "UPDATE orders SET paid_at = COALESCE(paid_at, $1), updated_at = $1 WHERE id = $2",
                    now, order_id,
                )
                await txn.execute(
                    "UPDATE acc_docs SET payment_id = $1, target_cents = $2 WHERE id = $3",
                    payment_id, credited, doc_id,
                )
                info.update(payment_id=payment_id, credited=credited)
            else:
                from services import machines

                deal = await machines._lock_deal(txn, deal_id)
                if not deal:
                    raise AccountingError("Сделка не найдена", status=404)
                if deal["closed_at"]:
                    raise AccountingError("Рассрочка уже закрыта")
                rest = await _deal_remaining_cents(txn, deal_id)
                credited = _credit_or_raise(total_target, rest, target_cur)
                closed = await machines._insert_receipt_locked(
                    txn, deal, credited, user_id=actor.user_id,
                    note=(f"Журнал денег №{doc_id}" + (f" · {note}" if note else ""))[:200],
                    received_at=None,
                )
                # Сделка под блокировкой — последнее поступление по ней наше.
                receipt_id = int(await txn.fetchval(
                    "SELECT MAX(id) FROM machine_payment_receipts WHERE deal_id = $1", deal_id
                ))
                await txn.execute(
                    "UPDATE acc_docs SET machine_receipt_id = $1, target_cents = $2 WHERE id = $3",
                    receipt_id, credited, doc_id,
                )
                info.update(receipt_id=receipt_id, credited=credited, closed=closed)
            for acc_id, cents, cur, tgt in computed:
                await _insert_entry(txn, doc_id, acc_id, "in", cents, cur, rates[cur], target_cents=tgt)
            return doc_id

    doc, repeated = await _run_idempotent(key, work)
    if repeated:
        return _receipt_result(doc, repeated=True)

    lines_txt = ", ".join(
        f"{_fmt(c)} {cur} → {accounts[a]['name']}" for a, c, cur, _ in computed
    )
    rate_txt = ", ".join(
        f"{c} {fmt_rate(r.quote)} ({r.source})" for c, r in rates.items() if r.source != "base"
    )
    extra: dict[str, Any] = {}
    if order_id:
        await _audit(
            actor, "accounting_receipt",
            f"№{doc['id']} заказ #{order_id}: {lines_txt}; зачтено "
            f"{_fmt(info['credited'])} {target_cur}" + (f"; курс {rate_txt}" if rate_txt else ""),
        )
        payment_status = "pending"
        # Руководитель — сам тот, кто подтверждает: ждать от него второго
        # нажатия на собственную запись незачем. Подтверждение — обычный
        # `confirm_payment`, он же закрывает заказ.
        if actor.role in ROLES_MANAGE:
            if await _db().confirm_payment(info["payment_id"], actor.user_id, actor.name):
                payment_status = "confirmed"
        extra["payment_status"] = payment_status
        from services.debts import calc_claimable_cents, calc_order_balance

        bal = await calc_order_balance(order_id)
        extra["remaining_cents"] = bal.remaining_cents
        extra["claimable_cents"] = (await calc_claimable_cents([order_id])).get(order_id, 0)
    else:
        from services import machines

        await _audit(
            actor, "accounting_receipt",
            f"№{doc['id']} рассрочка #{deal_id}: {lines_txt}; зачтено "
            f"{_fmt(info['credited'])} {target_cur}" + (f"; курс {rate_txt}" if rate_txt else ""),
        )
        after = await machines._after_receipt_added(
            deal_id, info["credited"], target_cur, info["closed"],
            user_id=actor.user_id, full_name=actor.name,
        )
        extra["deal_closed"] = bool(after.get("deal_closed"))
    return _receipt_result(doc, repeated=False, extra=extra)


def _credit_or_raise(total: int, available: int, currency: str) -> int:
    if available <= 0:
        raise AccountingError("По этому долгу нечего принимать: всё оплачено или ждёт подтверждения")
    if total > available + OVERPAY_TOLERANCE_MINOR:
        raise AccountingError(
            f"Получено больше остатка: остаток {_fmt(available)} {currency}, "
            f"получено {_fmt(total)} {currency}. Переплату так не записать — "
            "уменьшите сумму или поправьте курс"
        )
    return min(total, available)


async def receipt_targets(actor: Actor) -> dict:
    """Открытые долги, по которым можно принять деньги: заказы (менеджер —
    свои) и рассрочки (руководству). «Можно принять» = `calc_claimable_cents`
    — ровно то число, которым запись и ограничена."""
    from services.database import get_open_debts
    from services.debts import calc_claimable_cents, calc_order_balances

    see_all = actor.role in ROLES_SEE_ALL
    orders = await get_open_debts(user_id=None if see_all else actor.user_id)
    ids = [int(o["id"]) for o in orders if o.get("status") in _ORDER_OPEN_STATUSES]
    claimable = await calc_claimable_cents(ids) if ids else {}
    bals = await calc_order_balances(ids) if ids else {}
    out_orders = []
    for o in orders:
        oid = int(o["id"])
        if oid not in claimable:
            continue
        bal = bals.get(oid)
        out_orders.append({
            "order_id": oid,
            "agent_name": o.get("agent_name") or "—",
            "manager": o.get("full_name") or "",
            "currency": (o.get("currency") or base_currency()).upper(),
            "total_cents": bal.total_cents if bal else 0,
            "remaining_cents": bal.remaining_cents if bal else 0,
            "claimable_cents": claimable[oid],
            "due_date": o.get("due_date"),
        })
    deals = []
    # Рассрочки — всем, кто записывает деньги: их ведёт менеджер.
    if actor.role in ROLES_RECORD:
        from services.receivables import machine_debt_rows

        for d in await machine_debt_rows(today_str()):
            deals.append({
                "deal_id": d["deal_id"],
                "machine_name": d["machine_name"],
                "buyer_name": d["buyer_name"],
                "currency": d["currency"],
                "remaining_cents": money.to_cents(d["remaining"]),
                "next_due": d.get("next_due"),
            })
    return {"orders": out_orders, "deals": deals}


# ─── Расход ──────────────────────────────────────────────────────────────────


async def record_expense(actor: Actor, data: dict) -> dict:
    """Расход: с какого счёта, сколько, на что. Примечание обязательно —
    категории у владельца нет, и без текста расход через месяц не объяснить."""
    await require_enabled()
    if actor.role not in ROLES_RECORD:
        raise AccountingError("Нет доступа", status=403)
    key = _request_key(actor, "expense", data.get("idempotency_key"))
    prev = await _find_by_key(key)
    if prev is not None:
        return {"ok": True, "doc_id": int(prev["id"]), "repeated": True}
    account_id = _account_arg(data.get("account_id"))
    acc = await _active_account(account_id)
    cents = parse_cents(data.get("amount"))
    if cents is None:
        raise AccountingError("Введите сумму больше нуля")
    note = _text(data.get("note"), NOTE_MAX)
    if not note:
        raise AccountingError("Напишите, на что потратили")
    category = _text(data.get("category"), 40) or None
    doc_date = _parse_date(data.get("doc_date"), default=today_str())
    cur = str(acc["currency"]).upper()
    rates = await resolve_rates({cur}, data.get("rates"), doc_date, actor.role)
    _check_ceiling(cents, cur, rates)
    now = _now_str()

    async def work() -> int:
        async with adb_core.transaction() as txn:
            doc_id = await _insert_doc(
                txn, actor, kind="expense", doc_date=doc_date, now=now, request_key=key,
                category=category, note=note,
            )
            await _insert_entry(txn, doc_id, account_id, "out", cents, cur, rates[cur])
            return doc_id

    doc, repeated = await _run_idempotent(key, work)
    if not repeated:
        await _audit(
            actor, "accounting_expense",
            f"№{doc['id']}: {_fmt(cents)} {cur} с «{acc['name']}» — {note}",
        )
    return {"ok": True, "doc_id": int(doc["id"]), "repeated": repeated}


# ─── Перевод и обмен ─────────────────────────────────────────────────────────


async def record_transfer(actor: Actor, data: dict) -> dict:
    """Перевод между своими счетами — в том числе сдача наличных «менеджер →
    касса». Разные валюты — это обмен: две стороны с ФАКТИЧЕСКИМИ суммами, курс
    выводится из них (сколько реально дали сумов за доллар), курс ЦБ хранится
    рядом для сравнения. В базовой валюте обе стороны равны: обмен не создаёт и
    не уничтожает деньги, разница с ЦБ — это курсовая разница, её считает
    отчёт о прибыли, а не остаток счёта."""
    await require_enabled()
    if actor.role not in ROLES_RECORD:
        raise AccountingError("Нет доступа", status=403)
    key = _request_key(actor, "transfer", data.get("idempotency_key"))
    prev = await _find_by_key(key)
    if prev is not None:
        return {"ok": True, "doc_id": int(prev["id"]), "kind": prev["kind"], "repeated": True}
    from_id = _account_arg(data.get("from_account_id"), "Откуда")
    to_id = _account_arg(data.get("to_account_id"), "Куда")
    if from_id == to_id:
        raise AccountingError("Счета «откуда» и «куда» совпадают")
    src = await _active_account(from_id, "Откуда")
    dst = await _active_account(to_id, "Куда")
    out_cents = parse_cents(data.get("amount"))
    if out_cents is None:
        raise AccountingError("Введите сумму больше нуля")
    src_cur, dst_cur = str(src["currency"]).upper(), str(dst["currency"]).upper()
    base = base_currency()
    doc_date = _parse_date(data.get("doc_date"), default=today_str())
    note = _text(data.get("note"), NOTE_MAX) or None

    if src_cur == dst_cur:
        kind = "transfer"
        in_cents = out_cents
        rates = await resolve_rates({src_cur}, None, doc_date)
        _check_ceiling(out_cents, src_cur, rates)
        src_rate = dst_rate = rates[src_cur]
        base_cents = to_base_cents(out_cents, src_cur, src_rate.quote)
    else:
        kind = "exchange"
        parsed_in = parse_cents(data.get("amount_in"))
        if parsed_in is None:
            raise AccountingError(f"Обмен: сколько получили в {dst_cur}")
        in_cents = parsed_in
        cbu = await resolve_rates({src_cur, dst_cur}, None, doc_date)
        _check_ceiling(out_cents, src_cur, cbu)
        _check_ceiling(in_cents, dst_cur, cbu)
        if src_cur == base:
            base_cents = out_cents
        elif dst_cur == base:
            base_cents = in_cents
        else:
            base_cents = to_base_cents(out_cents, src_cur, cbu[src_cur].quote)
        if base_cents <= 0:
            raise AccountingError("Сумма обмена слишком мала")

        def actual(cur: str, cents: int) -> RateInfo:
            if cur == base:
                return RateInfo(Decimal(1), "base", Decimal(1))
            quote = (Decimal(cents) / Decimal(base_cents)).quantize(_RATE_Q, rounding=ROUND_HALF_UP)
            return RateInfo(quote, "manual", cbu[cur].cbu)

        src_rate, dst_rate = actual(src_cur, out_cents), actual(dst_cur, in_cents)
    now = _now_str()

    async def work() -> int:
        async with adb_core.transaction() as txn:
            doc_id = await _insert_doc(
                txn, actor, kind=kind, doc_date=doc_date, now=now, request_key=key, note=note,
            )
            await _insert_entry(txn, doc_id, from_id, "out", out_cents, src_cur, src_rate,
                                base_cents=base_cents)
            await _insert_entry(txn, doc_id, to_id, "in", in_cents, dst_cur, dst_rate,
                                base_cents=base_cents)
            return doc_id

    doc, repeated = await _run_idempotent(key, work)
    if not repeated:
        await _audit(
            actor, f"accounting_{kind}",
            f"№{doc['id']}: {_fmt(out_cents)} {src_cur} «{src['name']}» → "
            f"{_fmt(in_cents)} {dst_cur} «{dst['name']}»",
        )
    return {"ok": True, "doc_id": int(doc["id"]), "kind": doc.get("kind", kind), "repeated": repeated}


# ─── Закрыть день (сверка) ───────────────────────────────────────────────────


async def close_day(actor: Actor, data: dict) -> dict:
    """Пересчитали кассу — записали факт. Остаток по журналу сравнивается с
    пересчётом под тем же подсчётом, расхождение проводится документом сверки
    (приход или расход на разницу), и остаток счёта становится равным
    пересчитанному. Закрытие пишется и при нуле: «сошлось» — тоже ответ на
    ежедневный вопрос владельца «что там, сколько»."""
    await require_enabled()
    if actor.role not in ROLES_RECORD:
        raise AccountingError("Нет доступа", status=403)
    key = _request_key(actor, "close_day", data.get("idempotency_key"))
    prev = await _find_by_key(key)
    if prev is not None:
        return await _close_result(prev, repeated=True)
    account_id = _account_arg(data.get("account_id"))
    acc = await _active_account(account_id)
    counted = parse_cents(data.get("counted"), allow_zero=True)
    if counted is None:
        raise AccountingError("Введите пересчитанную сумму (0, если пусто)")
    note = _text(data.get("note"), NOTE_MAX)
    cur = str(acc["currency"]).upper()
    day = today_str()
    rates = await resolve_rates({cur}, None, day)
    _check_ceiling(counted, cur, rates)
    now = _now_str()

    async def work() -> int:
        async with adb_core.transaction() as txn:
            # Сначала документ: он занимает ключ идемпотентности.
            doc_id = await _insert_doc(
                txn, actor, kind="reconcile", doc_date=day, now=now, request_key=key,
                note=note or None,
            )
            # Замок счёта ДО расчёта остатка. Без него два пересчёта одной кассы
            # (два человека, двойной тап с разными ключами) на Postgres
            # читали один и тот же остаток — каждый не видел незакоммиченную
            # сверку соседа — и оба проводили разницу: расхождение удваивалось,
            # и остаток уезжал от пересчитанного на ту же сумму в другую
            # сторону. Под замком второй ждёт первого и считает остаток уже
            # после его сверки.
            #
            # Повторный пересчёт за тот же день — законное действие («вечером
            # пересчитали ещё раз»), а не ошибка: он сверяется с остатком ПОСЛЕ
            # прошлой сверки, поэтому проводит только новое расхождение (при
            # том же счёте — ноль) и пишет ещё одну строку закрытия. Отказывать
            # в нём значило бы не дать поправить опечатку в пересчёте.
            await _lock_account(txn, account_id)
            expected = await account_balance(account_id, conn=txn)
            diff = counted - expected
            if diff and not note:
                raise AccountingError(
                    f"Расхождение {_fmt(abs(diff))} {cur} — "
                    "напишите причину в примечании"
                )
            if diff:
                await _insert_entry(
                    txn, doc_id, account_id, "in" if diff > 0 else "out", abs(diff), cur, rates[cur]
                )
            await txn.execute(
                "UPDATE acc_docs SET target_currency = $1, target_cents = $2 WHERE id = $3",
                cur, diff, doc_id,
            )
            await txn.execute(
                "INSERT INTO acc_day_closes (account_id, close_date, expected_cents, counted_cents, "
                "diff_cents, doc_id, note, created_by, created_by_name, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                account_id, day, expected, counted, diff, doc_id, note or None,
                actor.user_id, actor.name, now,
            )
            return doc_id

    doc, repeated = await _run_idempotent(key, work)
    res = await _close_result(doc, repeated=repeated)
    if not repeated:
        await _audit(
            actor, "accounting_close_day",
            f"№{doc['id']} «{acc['name']}»: по журналу {_fmt(res['expected_cents'])}, "
            f"пересчёт {_fmt(res['counted_cents'])}, разница "
            f"{_fmt(res['diff_cents'])} {cur}",
        )
    return res


async def _close_result(doc: dict, *, repeated: bool) -> dict:
    row = await adb_core.fetchrow("SELECT * FROM acc_day_closes WHERE doc_id = $1", int(doc["id"]))
    return {
        "ok": True,
        "doc_id": int(doc["id"]),
        "repeated": repeated,
        "account_id": int(row["account_id"]) if row else None,
        "expected_cents": int(row["expected_cents"]) if row else 0,
        "counted_cents": int(row["counted_cents"]) if row else 0,
        "diff_cents": int(row["diff_cents"]) if row else 0,
        "currency": doc.get("target_currency"),
    }


# ─── Журнал ──────────────────────────────────────────────────────────────────

KIND_FILTERS = {
    "receipt": ("receipt",),
    "expense": ("expense",),
    "transfer": ("transfer", "exchange"),
    "reconcile": ("reconcile", "opening"),
}


def _doc_state(d: dict) -> str:
    """Состояние документа для экрана: проведён, ждёт подтверждения платежа,
    потерял основание (платёж отклонён / поступление удалено), отменён."""
    if d["status"] == "void":
        return "void"
    if d.get("payment_id"):
        ps = d.get("payment_status")
        if ps == "rejected":
            return "payment_rejected"
        if ps == "pending":
            return "payment_pending"
    if d.get("machine_receipt_id") and not d.get("receipt_alive"):
        return "receipt_deleted"
    return "posted"


async def journal(actor: Actor, filters: dict) -> dict:
    await require_enabled()
    where = ["1 = 1"]
    params: list[Any] = []

    def p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if actor.role not in ROLES_SEE_ALL:
        where.append(f"d.created_by = {p(actor.user_id)}")
    if filters.get("account_id"):
        acc = _account_arg(filters.get("account_id"))
        where.append(f"d.id IN (SELECT doc_id FROM acc_entries WHERE account_id = {p(acc)})")
    kinds = KIND_FILTERS.get(str(filters.get("kind") or ""))
    if kinds:
        where.append("d.kind IN (" + ", ".join(p(k) for k in kinds) + ")")
    if filters.get("since"):
        where.append(f"d.doc_date >= {p(_parse_date(filters['since'], default=today_str()))}")
    if filters.get("until"):
        until = str(filters["until"])[:10]
        try:
            date.fromisoformat(until)
        except ValueError as e:
            raise AccountingError("Дата должна быть в формате ГГГГ-ММ-ДД") from e
        where.append(f"d.doc_date <= {p(until)}")
    if filters.get("order_id"):
        where.append(f"d.order_id = {p(_id_arg(filters['order_id'], 'Заказ не найден'))}")
    try:
        limit = max(1, min(int(filters.get("limit") or 100), 300))
    except (TypeError, ValueError):
        limit = 100
    rows = await adb_core.fetch(
        "SELECT d.*, p.status AS payment_status, r.id AS receipt_alive "
        "FROM acc_docs d "
        "LEFT JOIN payments p ON p.id = d.payment_id "
        "LEFT JOIN machine_payment_receipts r ON r.id = d.machine_receipt_id "
        f"WHERE {' AND '.join(where)} ORDER BY d.doc_date DESC, d.id DESC LIMIT {limit}",
        *params,
    )
    docs: list[dict] = []
    entries = await _entries_for([int(r["id"]) for r in rows])
    today = today_str()
    for r in rows:
        docs.append(_doc_view_sync(r, entries.get(int(r["id"]), []), actor, today))
    return {"docs": docs, "today": today}


async def _entries_for(doc_ids: list[int]) -> dict[int, list[dict]]:
    if not doc_ids:
        return {}
    ph = ", ".join(f"${i + 1}" for i in range(len(doc_ids)))
    rows = await adb_core.fetch(
        "SELECT e.*, a.name AS account_name, a.kind AS account_kind "
        "FROM acc_entries e LEFT JOIN acc_accounts a ON a.id = e.account_id "
        f"WHERE e.doc_id IN ({ph}) ORDER BY e.id",
        *doc_ids,
    )
    out: dict[int, list[dict]] = {}
    for e in rows:
        out.setdefault(int(e["doc_id"]), []).append({
            "account_id": int(e["account_id"]),
            "account_name": e.get("account_name") or "—",
            "direction": e["direction"],
            "amount_cents": int(e["amount_cents"]),
            "currency": e["currency"],
            "rate": e["rate"],
            "rate_source": e["rate_source"],
            "cbu_rate": e.get("cbu_rate"),
            "amount_base_cents": int(e["amount_base_cents"]),
            "target_cents": e.get("target_cents"),
        })
    return out


def _doc_view_sync(d: dict, entries: list[dict], actor: Actor, today: str) -> dict:
    state = _doc_state(d)
    return {
        "id": int(d["id"]),
        "kind": d["kind"],
        "doc_date": d["doc_date"],
        "state": state,
        "order_id": d.get("order_id"),
        "deal_id": d.get("deal_id"),
        "payment_id": d.get("payment_id"),
        "counterparty": d.get("counterparty") or "",
        "target_currency": d.get("target_currency"),
        "target_cents": d.get("target_cents"),
        "category": d.get("category") or "",
        "note": d.get("note") or "",
        "void_reason": d.get("void_reason") or "",
        "voided_by_name": d.get("voided_by_name") or "",
        "created_by": d.get("created_by"),
        "created_by_name": d.get("created_by_name") or "",
        "created_at": d.get("created_at"),
        "entries": entries,
        "can_void": state != "void" and _can_void(actor, d, today) is None,
    }


async def get_doc(actor: Actor, doc_id: Any) -> dict:
    await require_enabled()
    doc_id = _id_arg(doc_id, "Документ не найден")
    row = await adb_core.fetchrow(
        "SELECT d.*, p.status AS payment_status, r.id AS receipt_alive FROM acc_docs d "
        "LEFT JOIN payments p ON p.id = d.payment_id "
        "LEFT JOIN machine_payment_receipts r ON r.id = d.machine_receipt_id WHERE d.id = $1",
        doc_id,
    )
    if not row:
        raise AccountingError("Документ не найден", status=404)
    if actor.role not in ROLES_SEE_ALL and int(row["created_by"]) != actor.user_id:
        raise AccountingError("Документ не найден", status=404)
    entries = (await _entries_for([doc_id])).get(doc_id, [])
    view = _doc_view_sync(row, entries, actor, today_str())
    if row["kind"] == "reconcile":
        close = await adb_core.fetchrow("SELECT * FROM acc_day_closes WHERE doc_id = $1", doc_id)
        if close:
            view["close"] = {
                "expected_cents": int(close["expected_cents"]),
                "counted_cents": int(close["counted_cents"]),
                "diff_cents": int(close["diff_cents"]),
            }
    return view


# ─── Отмена (сторно) ─────────────────────────────────────────────────────────


def _can_void(actor: Actor, d: dict, today: str) -> str | None:
    """None — можно; иначе текст отказа. Руководитель отменяет любой документ.
    Менеджер — только свой и только в день записи: исправить опечатку, а не
    переписать вчерашнюю кассу, которую уже пересчитали."""
    if actor.role in ROLES_MANAGE:
        return None
    if int(d.get("created_by") or 0) != actor.user_id:
        return "Чужую запись отменяет руководитель"
    if str(d.get("created_at") or "")[:10] != today:
        return "Прошлые дни отменяет руководитель"
    if d["kind"] in ("reconcile", "opening"):
        return "Сверку и начальный остаток отменяет руководитель"
    if d.get("payment_id") and d.get("payment_status") == "confirmed":
        return "Платёж уже подтверждён — отменить может руководитель"
    if d.get("machine_receipt_id"):
        return "Поступление по рассрочке отменяет руководитель"
    return None


async def _reverse_confirmed_payment(actor: Actor, payment_id: int, order_id: int | None) -> None:
    """Сторно ПОДТВЕРЖДЁННОГО платежа: платёж → rejected, заказ снова открыт,
    если без этого платежа он не покрыт. Штатного пути «отменить подтверждение»
    в проекте нет, а руководитель должен иметь возможность исправить свою же
    запись (он подтверждает её сразу при записи). Под блокировкой заказа —
    как закрытие в `_maybe_close_order_after_payment`."""
    from services.debts import calc_order_balance

    async with adb_core.transaction() as txn:
        if order_id:
            await _lock_order(txn, int(order_id))
        rc = await txn.execute(
            "UPDATE payments SET status = 'rejected' WHERE id = $1 AND status = 'confirmed'",
            payment_id,
        )
        if rc <= 0 or not order_id:
            return
        bal = await calc_order_balance(int(order_id), conn=txn)
        if bal.remaining_cents > 0:
            await txn.execute(
                "UPDATE orders SET paid_confirmed_at = NULL, paid_confirmed_by = NULL, "
                "paid_confirmed_by_name = NULL, updated_at = $1 WHERE id = $2",
                _now_str(), int(order_id),
            )


async def void_doc(actor: Actor, data: dict) -> dict:
    await require_enabled()
    if actor.role not in ROLES_RECORD:
        raise AccountingError("Нет доступа", status=403)
    reason = _text(data.get("reason"), 300)
    if len(reason) < 3:
        raise AccountingError("Напишите причину отмены")
    doc_id = _id_arg(data.get("doc_id"), "Документ не найден")
    d = await adb_core.fetchrow(
        "SELECT d.*, p.status AS payment_status FROM acc_docs d "
        "LEFT JOIN payments p ON p.id = d.payment_id WHERE d.id = $1",
        doc_id,
    )
    if not d:
        raise AccountingError("Документ не найден", status=404)
    if d["status"] == "void":
        return {"ok": True, "doc_id": doc_id, "already": True}
    denied = _can_void(actor, d, today_str())
    if denied:
        raise AccountingError(denied, status=403)

    db = _db()
    # Сначала снимаем основание, потом гасим документ: наоборот при сбое
    # посередине остался бы висеть платёж, который долг уже уменьшает, а в
    # журнале его денег нет.
    if d.get("payment_id"):
        if d.get("payment_status") == "pending":
            await db.reject_payment(int(d["payment_id"]), actor.user_id, actor.name)
        elif d.get("payment_status") == "confirmed":
            await _reverse_confirmed_payment(actor, int(d["payment_id"]), d.get("order_id"))
    if d.get("machine_receipt_id"):
        from services import machines

        alive = await adb_core.fetchval(
            "SELECT id FROM machine_payment_receipts WHERE id = $1", int(d["machine_receipt_id"])
        )
        if alive:
            res = await machines.delete_receipt(
                int(d["machine_receipt_id"]), user_id=actor.user_id, full_name=actor.name
            )
            if not res.get("ok"):
                raise AccountingError(res.get("error") or "Поступление по рассрочке не удалено")
    rc = await adb_core.execute(
        "UPDATE acc_docs SET status = 'void', void_reason = $1, voided_by = $2, voided_by_name = $3, "
        "voided_at = $4 WHERE id = $5 AND status = 'posted'",
        reason, actor.user_id, actor.name, _now_str(), doc_id,
    )
    if rc > 0:
        await _audit(actor, "accounting_void", f"№{doc_id} ({d['kind']}) отменён: {reason}")
    return {"ok": True, "doc_id": doc_id, "already": rc <= 0}
