"""
Ежедневная сверка кассы: пересчитали наличные руками — записали, что вышло.

Требование владельца: «ежедневной сверки кассы хватит… пишется и при нуле».
Дыра, которую это закрывает: `order_payments.cash_on_hand` показывает деньги,
которые КТО-ТО ВНЁС в систему как оплату. Если менеджер оплату просто не
завёл, расхождение не ловит ничто — заказ выглядит неоплаченным, а наличные
лежат у человека. Физический пересчёт — единственный способ это увидеть.

Что здесь есть и чего нет:

* **Ожидаемое берётся из живого источника** — `order_payments.cash_on_hand`
  (наличные строки разбивки этого человека: платёж ещё `pending`, живой сдачи
  нет). То есть ровно то, что ДОЛЖНО сейчас физически лежать у него в кармане.
  Второй формулы «сколько у менеджера налички» в проекте не появляется.
* **Валюты считаются РАЗДЕЛЬНО.** Доллары и сумы не складываются: сумма в
  одной цифре по курсу — это переоценка, а не сверка, и расхождение в 50 USD
  утонуло бы в округлении миллионов сумов. Одна строка на валюту, строки
  одного пересчёта склеивает `request_key`.
* **Пишется и при нуле** — «записан пересчёт, расхождений нет» тоже факт: без
  него нельзя отличить «сверили, всё сошлось» от «сверку не делали».
* **Денег модуль не двигает.** Ни платежа, ни сдачи, ни долга он не создаёт и
  не правит — только вставляет свою строку и пишет аудит. Примечание к
  расхождению ИНФОРМАЦИОННОЕ: если бы «забыл занести оплату» само заводило
  платёж, недостачу можно было бы объяснить текстом и закрыть самому себе.
  Расхождение остаётся расхождением, и его видит руководитель.
* **Подтверждения нет.** Это запись пересчёта, а не движение денег: ждать
  одобрения значит не записать сверку вовсе.

Права: записывает тот, у кого касса физически на руках, — менеджер, а также
admin/boss (свою). Чужую историю видит только руководство (`ROLES_SEE_ALL`):
менеджеру отдаётся его собственная.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from services import adb_core

logger = logging.getLogger(__name__)

# Кто может записать пересчёт: у кого касса бывает на руках.
ROLES_RECORD: tuple[str, ...] = ("admin", "boss", "manager")
# Кто видит чужие пересчёты и сводку расхождений.
ROLES_SEE_ALL: tuple[str, ...] = ("admin", "boss")

NOTE_MAX = 500
HISTORY_LIMIT = 50
# Экран очереди дел («Сегодня») ведёт сюда же, что и вкладка «Касса → Сверка».
SCREEN = "money:reconcile"


class CashCountError(Exception):
    """Ошибка формы — текст показывается человеку как есть."""

    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Actor:
    user_id: int
    name: str
    role: str


# ─── Чистые функции (тесты — tests/test_cash_reconciliation.py) ──────────────


def parse_counts(raw: Any, allowed_currencies: list[str] | tuple[str, ...]) -> dict[str, int]:
    """Форма пересчёта → {валюта: копейки}. Бросает `CashCountError`.

    Валюта без введённой суммы в пересчёт НЕ попадает: «не считал сумы» и
    «сумов ноль» — разные утверждения, и второе нужно сказать явно (ввести 0).
    """
    from services import money

    allowed = {str(c).upper() for c in allowed_currencies}
    if isinstance(raw, dict):
        items = [{"currency": k, "amount": v} for k, v in raw.items()]
    elif isinstance(raw, list):
        items = [r for r in raw if isinstance(r, dict)]
    else:
        raise CashCountError("Введите пересчитанные суммы")
    out: dict[str, int] = {}
    for row in items:
        cur = str(row.get("currency") or "").upper()
        if cur not in allowed:
            raise CashCountError(f"Валюта {cur or '—'} не в ходу")
        amount = row.get("amount")
        if amount is None or str(amount).strip() == "":
            continue
        cents = money.parse_amount(str(amount))
        if cents is None:
            # parse_amount отдаёт None и на «0», и на мусоре. Ноль законен
            # («в кассе пусто» — это тоже результат пересчёта), поэтому
            # отличаем его сами, а не через исключение.
            if _is_zero(amount):
                cents = 0
            else:
                raise CashCountError(f"{cur}: сумма должна быть числом не меньше нуля")
        if cur in out:
            raise CashCountError(f"Валюта {cur} указана дважды")
        out[cur] = int(cents)
    if not out:
        raise CashCountError("Введите пересчитанную сумму хотя бы по одной валюте")
    return out


def _is_zero(raw: Any) -> bool:
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(raw).replace(",", ".").replace(" ", "")) == 0
    except (InvalidOperation, ValueError, ArithmeticError):
        return False


def build_lines(counted: dict[str, int], system: dict[str, int]) -> list[dict]:
    """{валюта: пересчитано} + {валюта: по системе} → строки сверки.

    Валюта считается САМА ПО СЕБЕ: `diff = пересчитано − по системе` в её
    собственных копейках. Валюта, которой в системе нет вовсе, законна — это
    ровно случай «наличные есть, а оплата не занесена», ради которого всё и
    затевалось; ожидаемое по ней ноль, и вся сумма идёт в расхождение.
    """
    lines = []
    for cur in sorted(counted):
        sys_cents = int(system.get(cur, 0))
        got = int(counted[cur])
        lines.append({
            "currency": cur,
            "counted_cents": got,
            "system_cents": sys_cents,
            "diff_cents": got - sys_cents,
        })
    return lines


def summarize(lines: list[dict]) -> dict:
    """Итог пересчёта: сошлось ли и по каким валютам разошлось."""
    mismatched = [ln for ln in lines if int(ln["diff_cents"]) != 0]
    return {
        "matched": not mismatched,
        "mismatched_currencies": [ln["currency"] for ln in mismatched],
        "max_abs_diff_cents": max((abs(int(ln["diff_cents"])) for ln in lines), default=0),
    }


def result_message(lines: list[dict]) -> str:
    """Текст итога для человека. Совпало — так и говорим (это нормальный исход)."""
    from services import money

    mismatched = [ln for ln in lines if int(ln["diff_cents"]) != 0]
    if not mismatched:
        return "Записан пересчёт, расхождений нет"
    parts = []
    for ln in mismatched:
        diff = int(ln["diff_cents"])
        sign = "излишек" if diff > 0 else "недостача"
        parts.append(f"{sign} {money.format_cents(abs(diff), sep=' ')} {ln['currency']}")
    return "Записано с расхождением: " + " · ".join(parts)


# ─── Ожидаемое: что должно физически лежать у человека ───────────────────────


async def system_on_hand(user_id: int) -> dict[str, int]:
    """{валюта: копейки} наличных, которые по системе на руках у человека.

    Один источник с формой «Сдать наличные» — `order_payments.cash_on_hand`.
    """
    from services import order_payments

    rows = await order_payments.cash_on_hand(int(user_id))
    summary = order_payments.cash_on_hand_summary(rows)
    return {r["currency"]: int(r["amount_cents"]) for r in summary["by_currency"]}


def currencies() -> list[str]:
    from config import ALLOWED_CURRENCIES

    return [str(c).upper() for c in ALLOWED_CURRENCIES]


async def context(actor: Actor) -> dict:
    """Что показать в форме сверки: ожидаемое по валютам и сегодняшний статус."""
    from services.roles import role_allowed

    system = await system_on_hand(actor.user_id)
    today = _today()
    return {
        "ok": True,
        "date": today,
        "currencies": currencies(),
        "system": [
            {"currency": c, "amount_cents": int(system.get(c, 0))} for c in currencies()
        ],
        "done_today": await done_today(actor.user_id),
        "can_see_all": role_allowed(actor.role, ROLES_SEE_ALL),
    }


# ─── Запись ──────────────────────────────────────────────────────────────────


def _today() -> str:
    from utils.helpers import local_now

    return local_now().strftime("%Y-%m-%d")


def _now() -> str:
    from services.database import now_str

    return now_str()


async def record(actor: Actor, raw_counts: Any, note: Any = None,
                 request_key: str | None = None) -> dict:
    """Записать пересчёт кассы. Ничего, кроме своей таблицы, не трогает.

    Возвращает {ok, date, lines, matched, message, repeated}.
    """
    from services.roles import role_allowed

    if not role_allowed(actor.role, ROLES_RECORD):
        raise CashCountError("Сверку кассы записывает тот, у кого касса на руках",
                             status=403, code="forbidden")
    counted = parse_counts(raw_counts, currencies())
    system = await system_on_hand(actor.user_id)
    lines = build_lines(counted, system)
    text = str(note or "").strip()[:NOTE_MAX] or None
    day, now = _today(), _now()
    key = str(request_key).strip()[:80] if request_key else None

    # Повтор по ключу (двойной тап, ретрай сети) — не второй пересчёт: отдаём
    # то, что уже записано. Проверка ДО транзакции + UNIQUE-индекс как рубеж.
    if key:
        existing = await _rows_by_key(key)
        if existing:
            return _result(day, existing, repeated=True)

    try:
        async with adb_core.transaction() as txn:
            for ln in lines:
                await txn.execute(
                    "INSERT INTO daily_cash_counts (count_date, counted_by, counted_by_name, "
                    "currency, counted_cents, system_cents, diff_cents, note, request_key, "
                    "created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    day, int(actor.user_id), actor.name, ln["currency"],
                    int(ln["counted_cents"]), int(ln["system_cents"]), int(ln["diff_cents"]),
                    text, key, now,
                )
    except Exception:
        # Гонку двух одинаковых ключей ловит UNIQUE-индекс: победитель записал,
        # проигравший читает его строки, а не падает пятисоткой на человека.
        if key:
            existing = await _rows_by_key(key)
            if existing:
                return _result(day, existing, repeated=True)
        raise

    await _audit(actor, lines, text)
    return _result(day, lines, repeated=False, note=text)


async def _rows_by_key(key: str) -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT currency, counted_cents, system_cents, diff_cents, note, count_date "
        "FROM daily_cash_counts WHERE request_key = $1 ORDER BY currency", key,
    )
    return [dict(r) for r in rows]


def _result(day: str, lines: list[dict], *, repeated: bool, note: str | None = None) -> dict:
    clean = [
        {
            "currency": str(ln["currency"]),
            "counted_cents": int(ln["counted_cents"]),
            "system_cents": int(ln["system_cents"]),
            "diff_cents": int(ln["diff_cents"]),
        }
        for ln in lines
    ]
    day = str(lines[0].get("count_date") or day) if lines else day
    return {
        "ok": True,
        "date": day,
        "lines": clean,
        "note": note if note is not None else (lines[0].get("note") if lines else None),
        "repeated": repeated,
        "message": result_message(clean),
        **summarize(clean),
    }


async def _audit(actor: Actor, lines: list[dict], note: str | None) -> None:
    from services import money

    parts = [
        f"{ln['currency']}: пересчёт {money.format_cents(ln['counted_cents'], sep=' ')}, "
        f"по системе {money.format_cents(ln['system_cents'], sep=' ')}, "
        f"разница {money.format_cents(ln['diff_cents'], sep=' ')}"
        for ln in lines
    ]
    details = "; ".join(parts) + (f" · {note}" if note else "")
    try:
        from services import database

        await asyncio.to_thread(database.add_audit_log, actor.user_id, actor.name, actor.role,
                                "cash_reconciliation", details[:2000])
    except Exception:
        logger.exception("cash_reconciliation: аудит не записан")


# ─── Чтение ──────────────────────────────────────────────────────────────────


async def done_today(user_id: int) -> bool:
    """Сверял ли этот человек кассу сегодня."""
    value = await adb_core.fetchval(
        "SELECT COUNT(*) FROM daily_cash_counts WHERE counted_by = $1 AND count_date = $2",
        int(user_id), _today(),
    )
    return bool(value)


async def history(*, user_id: int | None = None, only_diff: bool = False,
                  limit: int = HISTORY_LIMIT) -> list[dict]:
    """История пересчётов, новые сверху. `user_id=None` — все (руководству).

    `only_diff` — только строки с расхождением: это и есть то, что руководитель
    обязан посмотреть, остальное — подтверждение, что сверку делают.
    """
    sql = ("SELECT id, count_date, counted_by, counted_by_name, currency, counted_cents, "
           "system_cents, diff_cents, note, request_key, created_at FROM daily_cash_counts")
    args: list[Any] = []
    where: list[str] = []
    if user_id is not None:
        args.append(int(user_id))
        where.append(f"counted_by = ${len(args)}")
    if only_diff:
        where.append("diff_cents <> 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    args.append(max(1, min(int(limit), 200)))
    sql += f" ORDER BY count_date DESC, id DESC LIMIT ${len(args)}"
    return [dict(r) for r in await adb_core.fetch(sql, *args)]


async def mismatches_since(since_date: str, limit: int = 20) -> list[dict]:
    """Расхождения с указанной даты — для вечернего дайджеста руководителю."""
    rows = await adb_core.fetch(
        "SELECT count_date, counted_by_name, currency, counted_cents, system_cents, diff_cents, "
        "note FROM daily_cash_counts WHERE diff_cents <> 0 AND count_date >= $1 "
        "ORDER BY count_date DESC, id DESC LIMIT $2",
        str(since_date)[:10], max(1, min(int(limit), 100)),
    )
    return [dict(r) for r in rows]


# ─── Напоминание («Сегодня») ─────────────────────────────────────────────────


def reminder_time_hhmm() -> tuple[int, int] | None:
    """Час напоминания из настройки, либо None — «не напоминать».

    ПУСТАЯ настройка это выключенное напоминание, и она же по умолчанию (см.
    `database._DEFAULT_SETTINGS`). Мусор в настройке тоже гасит напоминание, а
    не подставляет свой час: пункт в очереди дел, взявшийся из опечатки, —
    худший вид напоминания.
    """
    from services.database import get_setting

    raw = str(get_setting("cash_reconciliation_reminder_time", "") or "").strip()
    if not raw:
        return None
    try:
        h, m = raw.split(":")
        h, m = int(h), int(m)
    except (TypeError, ValueError):
        return None
    return (h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def reminder_hour_reached(now=None) -> bool:
    """Наступил ли час напоминания. Отдельно от БД — чтобы тестировать временем."""
    from utils.helpers import local_now

    at = reminder_time_hhmm()
    if at is None:
        return False
    now = now or local_now()
    return (now.hour, now.minute) >= at


async def reminder_due(user_id: int, role: str, now=None) -> bool:
    """Пора ли напомнить этому человеку о сверке кассы.

    Условия: напоминание вообще включено (час задан), роль может записать
    сверку, час наступил, и сегодня человек ещё не сверялся.

    Наличие налички на руках В УСЛОВИЯ НЕ ВХОДИТ намеренно: «по системе ноль, а
    в кармане деньги» — ровно тот случай, ради которого сверка и нужна, и
    спрятать напоминание по пустому остатку значило бы не показать его как раз
    тогда, когда оно важнее всего.
    """
    from services.roles import role_allowed

    if not reminder_hour_reached(now):
        return False
    if not role_allowed(role, ROLES_RECORD):
        return False
    return not await done_today(user_id)
