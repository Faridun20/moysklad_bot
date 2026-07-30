"""
Остаток к оплате по заказу — ЕДИНСТВЕННЫЙ источник истины (T2.1).

До этого модуля остаток считали в четырёх местах и тремя разными способами:

  • `/api/debts` (webapp)            — правильно: платежи + сдачи + возвраты
  • `get_order_payment_summary`      — забывал сдачи наличных → заказ, закрытый
                                       сдачей, показывал долг (§2.10)
  • `tasks/run_debts_notify`         — считал ПОЛНУЮ сумму заказа, игнорируя
                                       вообще все оплаты (§2.9): клиент внёс 80%,
                                       а в утреннем напоминании висело 100%
  • `_maybe_close_order_after_payment` — эталон, по которому заказ закрывается

Формула (всё в копейках, целочисленно):

    remaining = total − confirmed_payments − confirmed_deposits − confirmed_returns

где
  total              — Σ(quantity × price_cents) по позициям заказа;
  confirmed_payments — Σ подтверждённых платежей **в валюте заказа**. Фильтр по
                       валюте обязателен: без него платёж в «дешёвой» валюте
                       (UZS) в копейках ложно перекрывал USD-заказ (WP-04);
  confirmed_deposits — Σ подтверждённых сдач наличных, распределённых на заказ
                       (`cash_deposit_orders` × `cash_deposits.status='confirmed'`);
  confirmed_returns  — Σ подтверждённых возвратов, КРОМЕ cash: cash-возврат
                       физически отдаёт деньги (отрицательная сдача в кассе) и
                       не должен ещё и уменьшать долг, иначе клиента кредитуют
                       дважды.

Результат зажат снизу нулём: переплата не делает остаток отрицательным.

Работа внутри транзакции: все функции принимают `conn` — это либо модуль
`adb_core` (по умолчанию, своё соединение), либо txn-объект из
`adb_core.transaction()`. Интерфейс у них общий (`fetch/fetchrow/fetchval/
execute`), поэтому вызывающий может считать остаток под тем же `FOR UPDATE`,
под которым потом закрывает заказ.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from services import adb_core

# ─── SQL-фрагменты (канонические определения) ────────────────────────────────
#
# Без placeholder'ов, безопасно встраивать в f-string. Работают и в SQLite,
# и в Postgres. Суммы и сравнения — в целых копейках, без float и без epsilon.
# `services.database` импортирует их отсюда, чтобы определение было одно.

SUM_PAYMENTS_CENTS = "COALESCE(SUM(amount_cents), 0)"
SUM_ORDER_TOTAL_CENTS = "COALESCE(SUM(CAST(round(quantity * price_cents) AS INTEGER)), 0)"
SUM_RETURNS_CENTS = "COALESCE(SUM(total_amount_cents), 0)"
SUM_ALLOC_CENTS = "COALESCE(SUM(cdo.amount_allocated_cents), 0)"

# Возвраты, уменьшающие «к оплате»: ТОЛЬКО не-cash (debt_reduction / no_refund /
# без метода). Cash-возврат физически отдаёт деньги — он не должен ещё и
# уменьшать долг по заказу.
RETURN_OWED_FILTER = (
    "status = 'confirmed' AND (refund_method IS NULL OR refund_method != 'cash')"
)


class OrderBalance(NamedTuple):
    """Разложение остатка по заказу. Всё в копейках, всё в валюте заказа."""

    order_id: int
    currency: str
    total_cents: int
    confirmed_cents: int
    pending_cents: int
    deposits_cents: int
    returns_cents: int
    remaining_cents: int


def _empty(order_id: int, currency: str) -> OrderBalance:
    return OrderBalance(order_id, currency, 0, 0, 0, 0, 0, 0)


def _remaining(total: int, confirmed: int, deposits: int, returns: int) -> int:
    """Остаток не уходит в минус: переплата — это не отрицательный долг."""
    return max(0, total - confirmed - deposits - returns)


def _base_currency() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


async def calc_order_balances(
    order_ids: list[int], conn: Any = None
) -> dict[int, OrderBalance]:
    """Остатки по списку заказов — батчем, без N+1.

    Пять запросов на любое число заказов (валюты, позиции, платежи, сдачи,
    возвраты) вместо пяти на каждый. Заказы, которых нет в БД, в результат
    не попадают.
    """
    db = conn if conn is not None else adb_core
    ids = [int(o) for o in dict.fromkeys(order_ids or [])]
    if not ids:
        return {}

    ph = ", ".join(f"${i + 1}" for i in range(len(ids)))
    base = _base_currency()

    cur_rows = await db.fetch(f"SELECT id, currency FROM orders WHERE id IN ({ph})", *ids)
    currencies = {int(r["id"]): (r["currency"] or base).upper() for r in cur_rows}
    if not currencies:
        return {}

    totals = {
        int(r["order_id"]): int(r["c"] or 0)
        for r in await db.fetch(
            f"SELECT order_id, {SUM_ORDER_TOTAL_CENTS} AS c FROM order_items "
            f"WHERE order_id IN ({ph}) GROUP BY order_id",
            *ids,
        )
    }
    # Платежи: разбивка по статусу И валюте — валюту сверяем с валютой заказа
    # уже в Python, потому что она у каждого заказа своя.
    pay_rows = await db.fetch(
        f"SELECT order_id, status, UPPER(COALESCE(currency, '')) AS cur, "
        f"{SUM_PAYMENTS_CENTS} AS c FROM payments "
        f"WHERE order_id IN ({ph}) AND status IN ('confirmed', 'pending') "
        f"GROUP BY order_id, status, UPPER(COALESCE(currency, ''))",
        *ids,
    )
    deposits = {
        int(r["order_id"]): int(r["c"] or 0)
        for r in await db.fetch(
            f"SELECT cdo.order_id AS order_id, {SUM_ALLOC_CENTS} AS c "
            f"FROM cash_deposit_orders cdo "
            f"JOIN cash_deposits d ON d.id = cdo.deposit_id "
            f"WHERE cdo.order_id IN ({ph}) AND d.status = 'confirmed' "
            f"GROUP BY cdo.order_id",
            *ids,
        )
    }
    returns = {
        int(r["order_id"]): int(r["c"] or 0)
        for r in await db.fetch(
            f"SELECT order_id, {SUM_RETURNS_CENTS} AS c FROM returns "
            f"WHERE order_id IN ({ph}) AND {RETURN_OWED_FILTER} GROUP BY order_id",
            *ids,
        )
    }

    confirmed: dict[int, int] = {}
    pending: dict[int, int] = {}
    for r in pay_rows:
        oid = int(r["order_id"])
        order_cur = currencies.get(oid)
        if order_cur is None:
            continue
        # NULL-валюту платежа трактуем как валюту заказа (легаси-safe).
        pay_cur = (r["cur"] or order_cur).upper()
        if pay_cur != order_cur:
            continue
        bucket = confirmed if r["status"] == "confirmed" else pending
        bucket[oid] = bucket.get(oid, 0) + int(r["c"] or 0)

    out: dict[int, OrderBalance] = {}
    for oid, cur in currencies.items():
        total_c = totals.get(oid, 0)
        conf_c = confirmed.get(oid, 0)
        dep_c = deposits.get(oid, 0)
        ret_c = returns.get(oid, 0)
        out[oid] = OrderBalance(
            order_id=oid,
            currency=cur,
            total_cents=total_c,
            confirmed_cents=conf_c,
            pending_cents=pending.get(oid, 0),
            deposits_cents=dep_c,
            returns_cents=ret_c,
            remaining_cents=_remaining(total_c, conf_c, dep_c, ret_c),
        )
    return out


async def calc_order_balance(order_id: int, conn: Any = None) -> OrderBalance:
    """Остаток по одному заказу. Несуществующий заказ → нули."""
    balances = await calc_order_balances([order_id], conn=conn)
    return balances.get(int(order_id)) or _empty(int(order_id), _base_currency())


async def calc_remaining_cents(order_id: int, conn: Any = None) -> int:
    """Сколько ещё должен клиент по заказу, в копейках. 0 — заказ покрыт."""
    return (await calc_order_balance(order_id, conn=conn)).remaining_cents
