"""
Дебиторка одним списком: «где наши деньги».

Деньги компании лежат в двух несвязанных учётах. Заказы в кредит живут в
`orders` и считаются через `services.debts`; рассрочки по технике — в
`machine_deal_payments`, отдельным графиком. Пока рассрочка была одна, это никому
не мешало; на десятке ответить «сколько нам должны и когда это придёт» стало
нельзя, не открывая карточки машин по очереди.

Этот модуль приводит оба потока к одному виду (`Receivable`) и считает по нему
агрегаты: разбивку по срокам, прогноз поступлений, платёжную дисциплину.

Три правила, вынесенные сюда намеренно:

* **Остаток по заказу НЕ пересчитываем.** Зовём `services.debts.calc_order_balances`
  — тот же код, что в `/api/debts`, карточке заказа и утренней напоминалке. Пятый
  способ посчитать долг ровно так и появляется.
* **Валюты не складываем молча.** `convert_to_base` возвращает None при
  незаданном курсе; такие суммы попадают в разбивку по валютам, а
  конвертированный итог помечается `partial`. Иначе «5 000 UZS + 200 USD»
  превращается в бессмысленное число, по которому принимают решения.
* **Границы просрочки считаем в Python.** `due_date` — локальная строка
  YYYY-MM-DD, а `NOW()`/`datetime('now')` в SQL отдают UTC: лексикографическое
  сравнение в разных кадрах молча всегда ложно (CLAUDE.md).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

from services import adb_core, money
from utils.helpers import local_now

# Корзины просрочки. Порядок важен: от самой болезненной к спокойной — так же
# читается и на экране, и в отчёте.
AGING_BUCKETS = ("overdue_90", "overdue_60", "overdue_30", "overdue_1", "not_due")

AGING_LABELS = {
    "overdue_90": "Просрочено >90 дней",
    "overdue_60": "Просрочено 60—90",
    "overdue_30": "Просрочено 30—60",
    "overdue_1": "Просрочено до 30",
    "not_due": "Срок не наступил",
}


@dataclass(frozen=True)
class Receivable:
    """Одна строка дебиторки — заказ или платёж графика рассрочки."""

    source: str            # "order" | "machine"
    ref_id: int            # orders.id | machine_deal_payments.id
    title: str             # "#142" | "JCB 3CX"
    counterparty: str      # контрагент заказа | покупатель техники
    owner_id: int | None   # менеджер-владелец заказа; у техники его нет
    due_date: str | None   # YYYY-MM-DD
    amount_cents: int      # остаток к получению
    currency: str


def _base_currency() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


def _money_block(items: list[Receivable]) -> dict:
    """Сумма списка: разбивка по валютам + конвертированный итог с флагом.

    `partial=True` значит «часть сумм в итог не вошла, потому что курс не
    задан». Показать такой итог без флага — соврать в цифре, по которой
    принимают решение.
    """
    from services.database import convert_to_base

    by_cur: dict[str, int] = {}
    for r in items:
        by_cur[r.currency] = by_cur.get(r.currency, 0) + r.amount_cents

    known: list[float] = []
    unknown = 0
    for cur, cents in by_cur.items():
        converted = convert_to_base(float(money.from_cents(cents)), cur)
        if converted is None:
            unknown += 1
        else:
            known.append(converted)
    return {
        "by_currency": [
            {"currency": c, "total": float(money.from_cents(v))}
            for c, v in sorted(by_cur.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "base_total": round(sum(known), 2) if known else None,
        "base_currency": _base_currency(),
        "partial": bool(known) and unknown > 0,
        "count": len(items),
    }


# ─── Сбор ────────────────────────────────────────────────────────────────────


async def _order_receivables(user_id: int | None) -> list[Receivable]:
    from services.database import get_open_debts
    from services.debts import calc_order_balances

    orders = await get_open_debts(user_id=user_id)
    if not orders:
        return []
    balances = await calc_order_balances([o["id"] for o in orders])
    out: list[Receivable] = []
    for o in orders:
        bal = balances.get(o["id"])
        if bal is None or bal.remaining_cents <= 0:
            continue
        out.append(
            Receivable(
                source="order",
                ref_id=int(o["id"]),
                title=f"#{o['id']}",
                counterparty=o.get("agent_name") or "—",
                owner_id=o.get("user_id"),
                due_date=(o.get("due_date") or None),
                amount_cents=bal.remaining_cents,
                currency=bal.currency,
            )
        )
    return out


async def machine_receivables() -> list[Receivable]:
    """Неоплаченные платежи графиков по незакрытым рассрочкам.

    Взнос (`seq = 0`) отмечен оплаченным в момент сделки, поэтому сюда не
    попадает сам собой — отдельного условия не нужно.
    """
    rows = await adb_core.fetch(
        "SELECT p.id, p.due_date, p.amount_cents, d.currency, d.buyer_name, "
        "       m.name AS machine_name, m.vin "
        "FROM machine_deal_payments p "
        "JOIN machine_deals d ON d.id = p.deal_id "
        "JOIN machines m ON m.id = d.machine_id "
        "WHERE p.paid_at IS NULL AND d.closed_at IS NULL "
        "ORDER BY p.due_date"
    )
    return [
        Receivable(
            source="machine",
            ref_id=int(r["id"]),
            title=r["machine_name"] or r["vin"] or "—",
            counterparty=r["buyer_name"] or "—",
            owner_id=None,
            due_date=(r["due_date"] or None),
            amount_cents=int(r["amount_cents"] or 0),
            currency=(r["currency"] or _base_currency()).upper(),
        )
        for r in rows
    ]


async def collect(user_id: int | None = None, *, include_machines: bool = True) -> list[Receivable]:
    """Вся дебиторка одним списком.

    `user_id` задан — только заказы этого менеджера. Технику при этом не
    отдаём вовсе (`include_machines=False` у вызывающего): рассрочки оформляет
    руководство, и менеджеру это не «пустой блок», а чужой участок.
    """
    orders = await _order_receivables(user_id)
    machines = await machine_receivables() if include_machines else []
    return orders + machines


# ─── Агрегаты ────────────────────────────────────────────────────────────────


def bucket_of(due_date: str | None, today: date) -> str:
    """Корзина просрочки для одной строки."""
    if not due_date:
        # Заказ без срока просроченным считать нельзя — срок ему просто не
        # ставили.
        return "not_due"
    try:
        due = date.fromisoformat(str(due_date)[:10])
    except ValueError:
        return "not_due"
    days = (today - due).days
    if days <= 0:
        return "not_due"
    if days > 90:
        return "overdue_90"
    if days > 60:
        return "overdue_60"
    if days > 30:
        return "overdue_30"
    return "overdue_1"


def aging(items: list[Receivable], today: date | None = None) -> dict:
    """Разбивка по срокам. Долг на 5 дней и долг на 5 месяцев — разные вещи, а
    в плоском списке они выглядят одинаково."""
    today = today or local_now().date()
    grouped: dict[str, list[Receivable]] = {b: [] for b in AGING_BUCKETS}
    for r in items:
        grouped[bucket_of(r.due_date, today)].append(r)
    return {
        "buckets": [
            {"key": b, "label": AGING_LABELS[b], **_money_block(grouped[b])}
            for b in AGING_BUCKETS
        ],
        "overdue": _money_block([r for b in AGING_BUCKETS[:-1] for r in grouped[b]]),
        "total": _money_block(items),
    }


def totals_by_source(items: list[Receivable]) -> dict:
    """«Нам должны: заказы / техника / всего» — вопрос, с которого начинается
    разбор."""
    return {
        "orders": _money_block([r for r in items if r.source == "order"]),
        "machines": _money_block([r for r in items if r.source == "machine"]),
        "all": _money_block(items),
    }


def _add_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def forecast(items: list[Receivable], months: int = 6, today: date | None = None) -> list[dict]:
    """Ожидаемые поступления по месяцам вперёд.

    Просроченное и бессрочное в прогноз не кладём: «должны были заплатить в
    марте» — это не поступление августа, и подмешивать одно к другому значит
    нарисовать деньги, которых не ждут.
    """
    today = today or local_now().date()
    start = date(today.year, today.month, 1)
    edges = [start]
    for _ in range(months):
        edges.append(_add_month(edges[-1]))

    out = []
    for i in range(months):
        lo, hi = edges[i], edges[i + 1]
        chunk = [
            r for r in items
            if r.due_date and lo <= date.fromisoformat(str(r.due_date)[:10]) < hi
            and date.fromisoformat(str(r.due_date)[:10]) >= today
        ]
        out.append({
            "month": lo.strftime("%Y-%m"),
            **_money_block(chunk),
            "machines": _money_block([r for r in chunk if r.source == "machine"]),
        })
    return out


def by_counterparty(items: list[Receivable], limit: int = 10) -> list[dict]:
    """Топ должников. Группируем по имени: у покупателя техники другого
    идентификатора пока нет."""
    grouped: dict[str, list[Receivable]] = {}
    for r in items:
        grouped.setdefault(r.counterparty, []).append(r)
    rows = [
        {"name": name, "sources": sorted({r.source for r in rs}), **_money_block(rs)}
        for name, rs in grouped.items()
    ]
    # Сортируем по конвертированному итогу; строки без курса — в конец, но не
    # выбрасываем: долг существует и без курса.
    rows.sort(key=lambda x: (x["base_total"] is None, -(x["base_total"] or 0)))
    return rows[:limit]


def by_owner(items: list[Receivable], limit: int = 10) -> list[dict]:
    """Дебиторка по менеджерам — только заказы: у рассрочки владельца нет."""
    grouped: dict[int, list[Receivable]] = {}
    for r in items:
        if r.source == "order" and r.owner_id is not None:
            grouped.setdefault(int(r.owner_id), []).append(r)
    rows = [{"user_id": uid, **_money_block(rs)} for uid, rs in grouped.items()]
    rows.sort(key=lambda x: (x["base_total"] is None, -(x["base_total"] or 0)))
    return rows[:limit]


# ─── Платёжная дисциплина ────────────────────────────────────────────────────


async def collection_stats(since: str, until: str) -> dict:
    """Поступают ли платежи: собрано против ожидалось и доля платежей в срок.

    Считается ТОЛЬКО по рассрочкам техники, и это осознанно. У заказа один
    `due_date` на весь долг и нет отметки «этот платёж ожидали к этому дню» —
    приравнять частичную оплату заказа к плановому платежу значит выдумать
    метрику, которая выглядит точной и не значит ничего.

    * ожидалось — платежи с `due_date` в периоде;
    * собрано — из них те, что оплачены;
    * в срок — `paid_at` не позже `due_date` (дата, не время: платёж «в день
      срока» опозданием не считается).
    """
    rows = await adb_core.fetch(
        "SELECT p.due_date, p.paid_at, p.amount_cents, d.currency, d.buyer_name "
        "FROM machine_deal_payments p "
        "JOIN machine_deals d ON d.id = p.deal_id "
        "WHERE p.seq > 0 AND p.due_date >= $1 AND p.due_date <= $2",
        since, until,
    )
    expected = [dict(r) for r in rows]
    paid = [r for r in expected if r["paid_at"]]
    on_time = [r for r in paid if str(r["paid_at"])[:10] <= str(r["due_date"])[:10]]

    def _as_items(rs):
        return [
            Receivable("machine", 0, "", r["buyer_name"] or "—", None,
                       str(r["due_date"]), int(r["amount_cents"] or 0),
                       (r["currency"] or _base_currency()).upper())
            for r in rs
        ]

    # Кто тянет: считаем по числу опозданий, а не по сумме — систематичность
    # важнее размера конкретного платежа.
    late_by_buyer: dict[str, dict] = {}
    for r in expected:
        name = r["buyer_name"] or "—"
        entry = late_by_buyer.setdefault(name, {"name": name, "late": 0, "total": 0})
        entry["total"] += 1
        overdue_now = not r["paid_at"] and str(r["due_date"])[:10] < local_now().date().isoformat()
        late_paid = r["paid_at"] and str(r["paid_at"])[:10] > str(r["due_date"])[:10]
        if overdue_now or late_paid:
            entry["late"] += 1
    laggards = sorted(
        (e for e in late_by_buyer.values() if e["late"]),
        key=lambda e: (-e["late"], e["name"]),
    )[:10]

    return {
        "expected": _money_block(_as_items(expected)),
        "collected": _money_block(_as_items(paid)),
        "on_time_count": len(on_time),
        "paid_count": len(paid),
        "expected_count": len(expected),
        "on_time_share": round(len(on_time) / len(paid), 3) if paid else None,
        "laggards": laggards,
    }


# ─── Карточка покупателя техники ─────────────────────────────────────────────


def buyer_key(name: str | None) -> str:
    """Ключ покупателя: пока это имя, поэтому нормализуем регистр и пробелы.

    Настоящего идентификатора у покупателя техники нет (`machine_deals` хранит
    имя и паспорт), поэтому «Иванов  П.» и «иванов п.» обязаны схлопнуться —
    иначе один человек выглядит как двое должников.
    """
    return " ".join(str(name or "").split()).casefold()


async def buyer_card(name: str) -> dict:
    """Всё по одному покупателю: сделки, графики, остаток."""
    key = buyer_key(name)
    deals = await adb_core.fetch(
        "SELECT d.*, m.name AS machine_name, m.vin, m.id AS machine_id "
        "FROM machine_deals d JOIN machines m ON m.id = d.machine_id "
        "ORDER BY d.sold_at DESC, d.id DESC"
    )
    mine = [dict(d) for d in deals if buyer_key(d["buyer_name"]) == key]
    if not mine:
        return {}

    schedules = await asyncio.gather(
        *(_schedule_for(int(d["id"])) for d in mine)
    )
    outstanding: list[Receivable] = []
    for deal, rows in zip(mine, schedules, strict=True):
        deal["payments"] = rows
        if deal.get("closed_at"):
            continue
        outstanding.extend(
            Receivable(
                "machine", int(p["id"]), deal["machine_name"] or "—",
                deal["buyer_name"] or "—", None, p["due_date"],
                int(p["amount_cents"] or 0),
                (deal["currency"] or _base_currency()).upper(),
            )
            for p in rows if not p["paid_at"] and int(p["seq"]) > 0
        )
    return {
        "buyer": mine[0]["buyer_name"],
        "deals": mine,
        "outstanding": _money_block(outstanding),
        "aging": aging(outstanding),
    }


async def _schedule_for(deal_id: int) -> list[dict]:
    from services import machines

    return await machines.get_schedule(deal_id)


async def machine_debt_rows(today: str) -> list[dict]:
    """Открытые рассрочки строками для экрана «Долги».

    Одна строка на сделку (а не на платёж): в списке долгов нужен ответ «кто и
    сколько должен», а помесячная разбивка живёт в карточке. Показываем остаток
    целиком и ближайший неоплаченный платёж — по нему и красится строка.
    """
    deals = await adb_core.fetch(
        "SELECT d.id, d.buyer_name, d.currency, m.name AS machine_name, m.vin "
        "FROM machine_deals d JOIN machines m ON m.id = d.machine_id "
        "WHERE d.kind = 'credit' AND d.closed_at IS NULL"
    )
    if not deals:
        return []
    rest_rows = await adb_core.fetch(
        "SELECT deal_id, COALESCE(SUM(amount_cents), 0) AS rest "
        "FROM machine_deal_payments WHERE paid_at IS NULL AND seq > 0 GROUP BY deal_id"
    )
    rest_by_deal = {int(r["deal_id"]): int(r["rest"] or 0) for r in rest_rows}
    next_due = await next_due_by_deal([int(d["id"]) for d in deals])

    out = []
    for d in deals:
        deal_id = int(d["id"])
        rest = rest_by_deal.get(deal_id, 0)
        if rest <= 0:
            continue
        nxt = next_due.get(deal_id)
        due = str(nxt["due_date"])[:10] if nxt else None
        out.append({
            "deal_id": deal_id,
            "machine_name": d["machine_name"] or d["vin"] or "—",
            "buyer_name": d["buyer_name"] or "—",
            "currency": (d["currency"] or _base_currency()).upper(),
            "remaining": float(money.from_cents(rest)),
            "next_due": due,
            "next_amount": float(money.from_cents(int(nxt["amount_cents"]))) if nxt else None,
            "state": (
                "overdue" if due and due < today
                else ("due_today" if due == today else "upcoming")
            ),
        })
    # Ближайший срок сверху: список нужен, чтобы решить, кому звонить сегодня.
    out.sort(key=lambda x: (x["next_due"] is None, x["next_due"] or ""))
    return out


async def next_due_by_deal(deal_ids: list[int]) -> dict[int, dict]:
    """Ближайший неоплаченный платёж каждой сделки — для строки в списке долгов.

    Батчем: список рассрочек иначе превращается в N+1 при первом же десятке.
    """
    if not deal_ids:
        return {}
    placeholders = ", ".join(f"${i + 1}" for i in range(len(deal_ids)))
    rows = await adb_core.fetch(
        f"SELECT deal_id, id, seq, due_date, amount_cents "
        f"FROM machine_deal_payments "
        f"WHERE paid_at IS NULL AND seq > 0 AND deal_id IN ({placeholders}) "
        f"ORDER BY due_date",
        *deal_ids,
    )
    out: dict[int, dict] = {}
    for r in rows:
        out.setdefault(int(r["deal_id"]), dict(r))
    return out
