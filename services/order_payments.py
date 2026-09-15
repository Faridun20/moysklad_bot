"""
«Как получены деньги» по заказу: разбивка оплаты по способам и валютам.

Требование владельца: заказ «оплата сразу» после одобрения НЕ уезжает, пока
менеджер не ввёл, как клиент отдал деньги — наличными, на карту, перечислением
на счёт, частями и в разных валютах. Долговой заказ отгружается без денег, но
каждое поступление по нему тоже вводится разбивкой. «Чтобы потом не возникало
вопросов»: у каждого платежа известен способ, а у наличных — в какой сдаче они
дошли до кассы.

Модель (схема — `services/order_payments_schema.py`):

* Строка разбивки `payment_parts` = способ + валюта + сумма, как отдал клиент,
  курс и сумма в ВАЛЮТЕ ЗАКАЗА. Под каждую строку пишется ОДИН обычный платёж
  `payments` на эту сумму — долг, закрытие заказа, «Получено/Ждёт» и дебиторка
  дальше считаются прежним кодом (`services.debts`), второй формулы долга нет.
* Карта и перечисление ждут подтверждения руководителя/бухгалтера (они
  сверяют банк): обычный `confirm_payment`.
* Наличные — у менеджера на руках, пока он не сдал их в кассу. Подтверждаются
  они ТОЛЬКО сдачей (`confirm_payment` наличную строку отвергает): сдача
  привязывается к строкам (`cash_deposit_parts`), и её подтверждение
  подтверждает их платежи той же транзакцией.
* Статус строки не хранится, а выводится: платёж pending/confirmed/rejected +
  «в сдаче #N» для наличных (`part_state`).

Правила суммы (`settle_parts`):
* «оплата сразу» — сумма разбивки обязана совпасть с тем, что по заказу ещё
  причитается; меньше — отказ с текстом: условия «оплата сразу» одобрил
  руководитель, и молча превратить недостачу в долг значит переписать его
  решение (такой заказ возвращают на доработку и оформляют «в долг»);
* «в долг» — любая часть, но не больше остатка;
* пересчёт валют округляет каждую строку до цента, поэтому при строке в другой
  валюте допускается расхождение до 1 единицы базовой валюты, и оно
  поглощается самой крупной пересчитанной строкой — итог сходится до цента.

Замки — общий «заявлено по заказу» (`services.debts.lock_orders`) ДО любого
расчёта остатка, как у отметки оплаты и сдачи.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from services import adb_core, money

logger = logging.getLogger(__name__)

METHODS: dict[str, str] = {
    "cash": "наличные",
    "card": "на карту",
    "bank": "перечислением на счёт",
}
NONCASH_METHODS = ("card", "bank")
RATE_SOURCES = ("same", "cbu", "manual")
MAX_PARTS = 10

# Кто вносит разбивку по ЧУЖОМУ заказу. Свой заказ вносит автор-менеджер.
ROLES_RECORD_ANY = ("admin", "boss")
# Кто подтверждает карту/перечисление (сверяет банк) и сдачу наличных.
# Менеджер попадает сюда через совмещение ролей (services.roles.ROLE_ALSO_ACTS_AS:
# он временно бухгалтер) только пока активных admin/boss/bookkeeper нет — см.
# `confirm_rights` (сервисный рубеж для HTTP и кнопок бота).
ROLES_CONFIRM = ("admin", "boss", "bookkeeper")

# Статусы заказа, в которых по нему принимают деньги (как у mark_order_paid).
_OPEN_STATUSES = ("approved", "shipped", "partially_returned")


class PaymentError(Exception):
    """Ошибка правил/формы — текст показывается человеку как есть."""

    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(frozen=True)
class PartInput:
    method: str
    currency: str
    amount_cents: int
    rate: Decimal | None = None


@dataclass(frozen=True)
class PartCalc:
    method: str
    currency: str
    amount_cents: int
    rate: Decimal | None          # курс валюты строки (единиц за 1 базовую), None — базовая
    order_rate: Decimal | None    # курс валюты заказа, None — базовая
    rate_source: str              # same | cbu | manual
    cbu_rate: Decimal | None
    order_amount_cents: int


# ─── Чистые функции (тесты — tests/test_order_payments.py) ───────────────────


def _base() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


def _allowed_currencies() -> tuple[str, ...]:
    from config import ALLOWED_CURRENCIES

    return tuple(c.upper() for c in ALLOWED_CURRENCIES)


def fmt_cents(cents: int, currency: str = "") -> str:
    """«12 130», «5 000.5» — копейки только когда они есть; UZS — целым."""
    trim = money.format_cents(int(cents), decimals=2, sep=" ", trim=True)
    if (currency or "").upper() == "UZS":
        trim = money.format_cents(int(cents), decimals=0, sep=" ")
    return f"{trim} {currency}".strip()


def fmt_rate(q: Decimal | None) -> str | None:
    from services.accounting import fmt_rate as _f

    return _f(q)


def parse_parts(raw: Any, allowed: tuple[str, ...] | None = None) -> list[PartInput]:
    """Строки формы → проверенные `PartInput`. Бросает `PaymentError` с номером строки."""
    from services.accounting import parse_rate

    allowed = allowed or _allowed_currencies()
    if not isinstance(raw, list) or not raw:
        raise PaymentError("Добавьте хотя бы одну строку: как получены деньги и сколько")
    if len(raw) > MAX_PARTS:
        raise PaymentError(f"Не больше {MAX_PARTS} строк в одной оплате")
    out: list[PartInput] = []
    for n, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            raise PaymentError(f"Строка {n}: неверный формат")
        method = str(row.get("method") or "").strip().lower()
        if method not in METHODS:
            raise PaymentError(f"Строка {n}: выберите способ — наличные, карта или перечисление")
        currency = str(row.get("currency") or "").strip().upper()
        if currency not in allowed:
            raise PaymentError(f"Строка {n}: валюта {currency or '—'} не поддерживается")
        raw_amount = row.get("amount")
        cents = money.parse_amount(str(raw_amount)) if raw_amount not in (None, "") else None
        if cents is None or cents <= 0:
            raise PaymentError(f"Строка {n}: введите сумму больше нуля")
        raw_rate = row.get("rate")
        rate = None
        if raw_rate not in (None, ""):
            rate = parse_rate(raw_rate)
            if rate is None:
                raise PaymentError(f"Строка {n}: курс — положительное число")
        out.append(PartInput(method, currency, int(cents), rate))
    return out


def rate_currency(part_cur: str, order_cur: str, base: str) -> str | None:
    """Чей курс нужен строке: НЕбазовая валюта пары. None — пересчёта нет."""
    p, o = part_cur.upper(), order_cur.upper()
    if p == o:
        return None
    if p != base and o != base:
        # USD/UZS — пара всегда с базовой; третья валюта потребовала бы двух курсов.
        raise PaymentError(f"Пересчёт {p} → {o} без базовой валюты не поддерживается")
    return p if p != base else o


def _q(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def convert_to_order(amount_cents: int, part_cur: str, order_cur: str, base: str,
                     quote: Decimal | None) -> int:
    """Сумма строки → валюта заказа. `quote` — единиц небазовой валюты за 1 базовую."""
    p, o = part_cur.upper(), order_cur.upper()
    if p == o:
        return int(amount_cents)
    if quote is None or quote <= 0:
        raise PaymentError("Нет курса для пересчёта")
    if o == base:  # сумы → доллары
        return _q(Decimal(int(amount_cents)) / quote)
    return _q(Decimal(int(amount_cents)) * quote)  # доллары → сумы


def compute_parts(inputs: list[PartInput], order_currency: str, base: str,
                  cbu: dict[str, Decimal | None], role: str | None = None) -> list[PartCalc]:
    """Курс и сумма в валюте заказа по каждой строке.

    Курс по умолчанию — ЦБ (`cbu`, «сум за доллар»); введённый человеком
    выигрывает, и тогда источник `manual`, а ЦБ сохраняется рядом. Свой курс —
    не дальше допуска от ЦБ, без курса ЦБ — только руководству
    (`accounting.manual_rate_refusal`; `role` — настоящая роль вносящего,
    None — как не руководитель).
    """
    from services.accounting import manual_rate_refusal

    order_cur = order_currency.upper()
    out: list[PartCalc] = []
    for n, inp in enumerate(inputs, start=1):
        rc = rate_currency(inp.currency, order_cur, base)
        if rc is None:
            # Потолок суммы — в эквиваленте базовой (money.validate_cents): сумы
            # техники законно миллиардами, доллары такими быть не могут.
            own = cbu.get(inp.currency) if inp.currency != base else Decimal(1)
            ok, err = money.validate_cents(inp.amount_cents, (Decimal(1) / own) if own else None)
            if not ok:
                raise PaymentError(f"Строка {n}: {err}")
            out.append(PartCalc(inp.method, inp.currency, inp.amount_cents, None, None,
                                "same", None, inp.amount_cents))
            continue
        cbu_q = cbu.get(rc)
        refusal = manual_rate_refusal(rc, inp.rate, cbu_q, role)
        if refusal:
            raise PaymentError(f"Строка {n}: {refusal}", code="manual_rate")
        quote = inp.rate if inp.rate is not None else cbu_q
        if quote is None:
            raise PaymentError(f"Строка {n}: нет курса ЦБ для {rc} — укажите курс")
        source = "cbu" if inp.rate is None or (cbu_q is not None and inp.rate == cbu_q) else "manual"
        order_cents = convert_to_order(inp.amount_cents, inp.currency, order_cur, base, quote)
        if order_cents <= 0:
            raise PaymentError(f"Строка {n}: в валюте заказа выходит ноль — проверьте курс")
        ok, err = money.validate_cents(inp.amount_cents, (Decimal(1) / quote) if inp.currency != base else 1)
        if not ok:
            raise PaymentError(f"Строка {n}: {err}")
        out.append(PartCalc(
            inp.method, inp.currency, inp.amount_cents,
            quote if inp.currency.upper() == rc else None,
            quote if order_cur == rc else None,
            source, cbu_q, order_cents,
        ))
    return out


def tolerance_cents(calcs: list[PartCalc], order_currency: str, base: str) -> int:
    """Допуск на округление пересчёта: 0 без пересчёта, иначе 1 единица базовой
    валюты в валюте заказа (у сумового заказа — курс × 100 тийинов)."""
    converted = [c for c in calcs if c.rate_source != "same"]
    if not converted:
        return 0
    if order_currency.upper() == base:
        return 100
    quotes = [c.order_rate for c in converted if c.order_rate]
    return max(100, _q(Decimal(100) * max(quotes))) if quotes else 100


def settle_parts(calcs: list[PartCalc], due_cents: int, *, exact: bool, order_currency: str,
                 base: str) -> list[PartCalc]:
    """Проверить сумму разбивки против того, что причитается, и поглотить
    копейки пересчёта. `exact` — «оплата сразу»: сумма обязана совпасть."""
    if due_cents <= 0:
        raise PaymentError("По заказу нечего вносить: всё оплачено или уже ждёт подтверждения",
                           code="nothing_due")
    total = sum(c.order_amount_cents for c in calcs)
    tol = tolerance_cents(calcs, order_currency, base)
    cur = order_currency.upper()
    if total > due_cents + tol:
        raise PaymentError(
            f"Введено больше, чем нужно: к оплате {fmt_cents(due_cents, cur)}, "
            f"введено {fmt_cents(total, cur)}", code="over")
    if exact and total < due_cents - tol:
        raise PaymentError(
            f"Не хватает {fmt_cents(due_cents - total, cur)}: заказ «оплата сразу» — сумма "
            f"должна совпасть с суммой к оплате ({fmt_cents(due_cents, cur)}). Если клиент часть "
            "остался должен, это уже заказ «в долг» — его оформляют через доработку заявки",
            code="short")
    target = due_cents if (exact or total > due_cents) else total
    diff = target - total
    if diff == 0:
        return calcs
    # Сюда попадаем только при пересчёте (tol > 0): расхождение — копейки
    # округления, их берёт самая крупная пересчитанная строка.
    idx = max((i for i, c in enumerate(calcs) if c.rate_source != "same"),
              key=lambda i: calcs[i].order_amount_cents)
    fixed = calcs[idx].order_amount_cents + diff
    if fixed <= 0:
        raise PaymentError("Сумма в валюте заказа не сходится — проверьте курс")
    out = list(calcs)
    out[idx] = replace(calcs[idx], order_amount_cents=fixed)
    return out


def part_label(method: str, amount_cents: int, currency: str) -> str:
    return f"{METHODS.get(method, method)} {fmt_cents(amount_cents, currency)}"


# ─── Чтение ──────────────────────────────────────────────────────────────────


def _ph(n: int, start: int = 1) -> str:
    return ", ".join(f"${i}" for i in range(start, start + n))


_ACTIVE_DEPOSIT_SQL = (
    "SELECT cdp.part_id, cdp.deposit_id, d.status FROM cash_deposit_parts cdp "
    "JOIN cash_deposits d ON d.id = cdp.deposit_id "
    "WHERE d.status IN ('pending', 'confirmed') AND cdp.part_id IN ({ph})"
)


async def parts_for_orders(order_ids: list[int], conn: Any = None) -> dict[int, list[dict]]:
    """Строки разбивки по заказам с выведенным состоянием — батчем.

    state: `on_hand` (наличные у менеджера), `in_deposit` (в неподтверждённой
    сдаче), `awaiting_bank` (карта/счёт ждут проверки), `confirmed`, `rejected`.
    """
    db = conn if conn is not None else adb_core
    ids = sorted({int(o) for o in order_ids or []})
    if not ids:
        return {}
    rows: list[dict] = []
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        rows.extend(await db.fetch(
            "SELECT pp.*, p.status AS payment_status, p.confirmed_at AS payment_confirmed_at "
            "FROM payment_parts pp JOIN payments p ON p.id = pp.payment_id "
            f"WHERE pp.order_id IN ({_ph(len(chunk))}) ORDER BY pp.created_at, pp.id",
            *chunk,
        ))
    part_ids = [int(r["id"]) for r in rows if r["method"] == "cash"]
    deposit_of: dict[int, tuple[int, str]] = {}
    for start in range(0, len(part_ids), 5000):
        chunk = part_ids[start:start + 5000]
        for d in await db.fetch(_ACTIVE_DEPOSIT_SQL.format(ph=_ph(len(chunk))), *chunk):
            deposit_of[int(d["part_id"])] = (int(d["deposit_id"]), str(d["status"]))
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(int(r["order_id"]), []).append(part_view(dict(r), deposit_of.get(int(r["id"]))))
    return out


def part_state(method: str, payment_status: str, deposit: tuple[int, str] | None) -> str:
    if payment_status == "confirmed":
        return "confirmed"
    if payment_status == "rejected":
        return "rejected"
    if method == "cash":
        return "in_deposit" if deposit and deposit[1] == "pending" else "on_hand"
    return "awaiting_bank"


STATE_LABELS = {
    "on_hand": "у менеджера — ждут сдачи в кассу",
    "in_deposit": "сданы в кассу — ждут подтверждения",
    "awaiting_bank": "ждёт проверки банка",
    "confirmed": "подтверждено",
    "rejected": "отклонено",
}


def part_view(r: dict, deposit: tuple[int, str] | None = None) -> dict:
    state = part_state(r["method"], r.get("payment_status") or "pending", deposit)
    return {
        "id": int(r["id"]),
        "payment_id": int(r["payment_id"]),
        "order_id": int(r["order_id"]),
        "method": r["method"],
        "method_label": METHODS.get(r["method"], r["method"]),
        "currency": r["currency"],
        "amount_cents": int(r["amount_cents"]),
        "amount": float(money.from_cents(int(r["amount_cents"]))),
        "order_amount_cents": int(r["order_amount_cents"]),
        "rate": r.get("rate") or r.get("order_rate"),
        "rate_source": r.get("rate_source"),
        "state": state,
        "state_label": STATE_LABELS[state],
        "deposit_id": deposit[0] if deposit else None,
        "created_by": int(r["created_by"]),
        "created_by_name": r.get("created_by_name") or "",
        "created_at": (r.get("created_at") or "")[:16],
    }


async def parts_by_payment(payment_ids: list[int], conn: Any = None) -> dict[int, dict]:
    """Строка разбивки по id платежа (для лент и карточек подтверждения)."""
    db = conn if conn is not None else adb_core
    ids = sorted({int(p) for p in payment_ids or []})
    out: dict[int, dict] = {}
    for start in range(0, len(ids), 5000):
        chunk = ids[start:start + 5000]
        for r in await db.fetch(
            f"SELECT * FROM payment_parts WHERE payment_id IN ({_ph(len(chunk))})", *chunk
        ):
            out[int(r["payment_id"])] = dict(r)
    return out


async def payment_method(payment_id: int, conn: Any = None) -> str | None:
    db = conn if conn is not None else adb_core
    return await db.fetchval("SELECT method FROM payment_parts WHERE payment_id = $1", int(payment_id))


async def payment_in_active_deposit(payment_id: int, conn: Any = None) -> int | None:
    db = conn if conn is not None else adb_core
    v = await db.fetchval(
        "SELECT cdp.deposit_id FROM cash_deposit_parts cdp "
        "JOIN payment_parts pp ON pp.id = cdp.part_id "
        "JOIN cash_deposits d ON d.id = cdp.deposit_id "
        "WHERE pp.payment_id = $1 AND d.status IN ('pending', 'confirmed') LIMIT 1",
        int(payment_id),
    )
    return int(v) if v is not None else None


async def payment_gap_cents(order_ids: list[int], conn: Any = None) -> dict[int, int]:
    """Сколько по заказу ещё НЕ объяснено оплатой, в копейках валюты заказа.

    gap = сумма − возвраты − «объяснённые» платежи − сдачи (pending+confirmed),
    где объяснённый платёж — подтверждённый, либо ожидающий со строкой разбивки,
    либо записанный журналом денег (у счёта есть вид: касса/карта/банк). Старый
    автоплатёж одобрения «оплата сразу» способа не несёт — он НЕ объясняет
    деньги, и заказ с ним к отгрузке не допускается, пока менеджер не введёт
    разбивку (она этот автоплатёж заменяет).
    """
    from services.debts import calc_allocated_deposit_cents, calc_order_balances

    db = conn if conn is not None else adb_core
    ids = sorted({int(o) for o in order_ids or []})
    if not ids:
        return {}
    balances = await calc_order_balances(ids, conn=db)
    allocated = await calc_allocated_deposit_cents(ids, conn=db)
    rows = await db.fetch(
        "SELECT p.order_id, UPPER(COALESCE(p.currency, '')) AS cur, "
        "COALESCE(SUM(p.amount_cents), 0) AS c FROM payments p "
        f"WHERE p.order_id IN ({_ph(len(ids))}) AND (p.status = 'confirmed' OR "
        "(p.status = 'pending' AND (EXISTS (SELECT 1 FROM payment_parts pp WHERE pp.payment_id = p.id) "
        "OR EXISTS (SELECT 1 FROM acc_docs ad WHERE ad.payment_id = p.id AND ad.status = 'posted')))) "
        "GROUP BY p.order_id, UPPER(COALESCE(p.currency, ''))",
        *ids,
    )
    explained: dict[int, int] = {}
    for r in rows:
        oid = int(r["order_id"])
        bal = balances.get(oid)
        if bal is None:
            continue
        if (r["cur"] or bal.currency) != bal.currency:
            continue
        explained[oid] = explained.get(oid, 0) + int(r["c"] or 0)
    return {
        oid: max(0, bal.total_cents - bal.returns_cents - explained.get(oid, 0) - allocated.get(oid, 0))
        for oid, bal in balances.items()
    }


# ─── Запись разбивки ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Actor:
    user_id: int
    name: str
    role: str
    username: str = ""


async def _cbu_for(currencies: set[str]) -> dict[str, Decimal | None]:
    from services.accounting import cbu_quotes, today_str

    need = sorted(c for c in currencies if c and c != _base())
    return await cbu_quotes(need, today_str()) if need else {}


async def _insert_id(txn: Any, sql: str, *args: Any) -> int:
    if adb_core._use_postgres():
        return int(await txn.fetchval(sql + " RETURNING id", *args))
    await txn.execute(sql, *args)
    return int(await txn.fetchval("SELECT last_insert_rowid()"))


def _now() -> str:
    from services.database import now_str

    return now_str()


async def record_payment_parts(order_id: int, actor: Actor, raw_parts: Any, *,
                               idem_key: str | None = None,
                               supersede_payment_ids: list[int] | None = None) -> dict:
    """Записать, как получены деньги по заказу. Одна транзакция на всё.

    Возвращает {ok, order_id, payments: [...], parts: [...], total_cents,
    currency, superseded: [payment_id], gap_cents}. Ошибки — `PaymentError`.

    `supersede_payment_ids` — явная замена ожидающих платежей без способа
    (разовый `scripts/migrate_payment_breakdown`: старая отметка оплаты по
    заказу «в долг» раскладывается на строки). У «оплаты сразу» такие платежи
    заменяются всегда.
    """
    from services.database import idem_store_in
    from services.debts import calc_claimable_cents, lock_orders

    inputs = parse_parts(raw_parts)
    head = await adb_core.fetchrow(
        "SELECT id, user_id, currency, payment_type FROM orders WHERE id = $1", int(order_id)
    )
    if not head:
        raise PaymentError("Заказ не найден", status=404)
    from services.roles import role_allowed

    if int(head["user_id"]) != actor.user_id and not role_allowed(actor.role, ROLES_RECORD_ANY):
        raise PaymentError("Оплату по чужому заказу вносит руководитель", status=403)
    base = _base()
    order_cur = (head["currency"] or base).upper()
    cbu = await _cbu_for({i.currency for i in inputs} | {order_cur})
    calcs = compute_parts(inputs, order_cur, base, cbu, role=actor.role)
    now = _now()
    result: dict[str, Any] = {}

    async with adb_core.transaction() as txn:
        await lock_orders(txn, [int(order_id)])
        order = await txn.fetchrow(
            "SELECT id, user_id, currency, payment_type, status, paid_confirmed_at, agent_name "
            "FROM orders WHERE id = $1", int(order_id),
        )
        if order is None:
            raise PaymentError("Заказ не найден", status=404)
        ptype = (order["payment_type"] or "paid")
        if order["paid_confirmed_at"] is not None:
            raise PaymentError("Заказ уже полностью оплачен", status=409, code="closed")
        if order["status"] not in _OPEN_STATUSES:
            raise PaymentError("Оплату вносят по одобренному заказу", status=409, code="status")

        superseded: list[int] = []
        for pid in supersede_payment_ids or []:
            row = await txn.fetchrow(
                "SELECT p.status, p.order_id, (SELECT COUNT(*) FROM payment_parts pp "
                "WHERE pp.payment_id = p.id) AS parts FROM payments p WHERE p.id = $1", int(pid),
            )
            if row is None or int(row["order_id"] or 0) != int(order_id):
                raise PaymentError(f"Платёж #{pid} не относится к заказу #{order_id}", status=409)
            if row["status"] != "pending" or int(row["parts"] or 0):
                raise PaymentError(f"Платёж #{pid} уже не ожидающий или уже разложен", status=409)
            await txn.execute(
                "UPDATE payments SET status = 'rejected' WHERE id = $1 AND status = 'pending'", int(pid)
            )
            superseded.append(int(pid))
        if ptype == "paid":
            # Старый автоплатёж одобрения (и любая ожидающая отметка без
            # способа) — это «деньги неизвестно как». Разбивка их заменяет:
            # иначе они держали бы весь остаток «заявленным», и ни разбивку,
            # ни сдачу по заказу было бы не записать (так и было с заказом
            # #27 на проде: автоплатёж на всю сумму, сдачи «Заказы: —»).
            rows = await txn.fetch(
                "SELECT p.id FROM payments p WHERE p.order_id = $1 AND p.status = 'pending' "
                "AND NOT EXISTS (SELECT 1 FROM payment_parts pp WHERE pp.payment_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM acc_docs ad WHERE ad.payment_id = p.id "
                "AND ad.status = 'posted')",
                int(order_id),
            )
            auto = [int(r["id"]) for r in rows if int(r["id"]) not in superseded]
            superseded.extend(auto)
            for pid in auto:
                await txn.execute(
                    "UPDATE payments SET status = 'rejected' WHERE id = $1 AND status = 'pending'", pid
                )
        due = (await calc_claimable_cents([int(order_id)], conn=txn)).get(int(order_id), 0)
        calcs = settle_parts(calcs, due, exact=(ptype == "paid"), order_currency=order_cur, base=base)

        agent = order["agent_name"] or ""
        payments_out = []
        parts_out = []
        for c in calcs:
            comment = (
                f"Оплата по заказу #{order_id}" + (f" ({agent})" if agent else "")
                + f" · {part_label(c.method, c.amount_cents, c.currency)}"
            )
            pid = await _insert_id(
                txn,
                "INSERT INTO payments (user_id, username, full_name, amount_cents, currency, "
                "comment, status, created_at, order_id) VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)",
                actor.user_id, actor.username, actor.name, c.order_amount_cents, order_cur,
                comment[:500], now, int(order_id),
            )
            part_id = await _insert_id(
                txn,
                "INSERT INTO payment_parts (payment_id, order_id, method, currency, amount_cents, "
                "rate, order_rate, rate_source, cbu_rate, order_amount_cents, created_by, "
                "created_by_name, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
                pid, int(order_id), c.method, c.currency, c.amount_cents, fmt_rate(c.rate),
                fmt_rate(c.order_rate), c.rate_source, fmt_rate(c.cbu_rate), c.order_amount_cents,
                actor.user_id, actor.name, now,
            )
            payments_out.append(pid)
            parts_out.append({
                "id": part_id, "payment_id": pid, "method": c.method, "currency": c.currency,
                "amount_cents": c.amount_cents, "order_amount_cents": c.order_amount_cents,
                "rate": fmt_rate(c.rate or c.order_rate), "rate_source": c.rate_source,
            })
        await txn.execute(
            "UPDATE orders SET paid_at = COALESCE(paid_at, $1), updated_at = $2 WHERE id = $3",
            now, now, int(order_id),
        )
        result = {
            "ok": True, "order_id": int(order_id), "currency": order_cur, "payment_type": ptype,
            "payments": payments_out, "payment_id": payments_out[0] if payments_out else None,
            "parts": parts_out, "superseded": superseded,
            "total_cents": sum(c.order_amount_cents for c in calcs), "due_cents": due,
        }
        await idem_store_in(txn, idem_key, result)

    await _audit(actor, "order_payment_recorded", recorded_text(int(order_id), order_cur, calcs, superseded))
    return result


def recorded_text(order_id: int, order_cur: str, calcs: list[PartCalc], superseded: list[int]) -> str:
    bits = []
    for c in calcs:
        t = part_label(c.method, c.amount_cents, c.currency)
        if c.rate_source != "same":
            t += f" по курсу {fmt_rate(c.rate or c.order_rate)} ({c.rate_source}) = {fmt_cents(c.order_amount_cents, order_cur)}"
        bits.append(t)
    text = f"Заказ #{order_id}: как получены деньги — " + "; ".join(bits)
    if superseded:
        text += f"; заменён платёж без способа #{', #'.join(str(p) for p in superseded)}"
    return text


async def _audit(actor: Actor, action: str, details: str) -> None:
    from services import database

    try:
        await asyncio.to_thread(database.add_audit_log, actor.user_id, actor.name, actor.role,
                                action, details[:2000])
    except Exception:
        logger.exception("order_payments: аудит %s не записан", action)


# ─── Наличные на руках и сдача ───────────────────────────────────────────────


_ON_HAND_SQL = (
    "SELECT pp.*, o.agent_name, o.currency AS order_currency, o.created_at AS order_created_at "
    "FROM payment_parts pp "
    "JOIN payments p ON p.id = pp.payment_id "
    "JOIN orders o ON o.id = pp.order_id "
    "WHERE pp.method = 'cash' AND p.status = 'pending' AND pp.created_by = $1 "
    "AND NOT EXISTS (SELECT 1 FROM cash_deposit_parts cdp JOIN cash_deposits d ON d.id = cdp.deposit_id "
    "WHERE cdp.part_id = pp.id AND d.status IN ('pending', 'confirmed')) "
)


async def cash_on_hand(manager_id: int, currency: str | None = None, conn: Any = None) -> list[dict]:
    """Наличные строки разбивки, которые менеджер ещё не сдал, — старые первыми (FIFO)."""
    db = conn if conn is not None else adb_core
    sql = _ON_HAND_SQL
    args: list[Any] = [int(manager_id)]
    if currency:
        args.append(currency.upper())
        sql += f"AND UPPER(pp.currency) = ${len(args)} "
    sql += "ORDER BY pp.created_at ASC, pp.id ASC"
    return [dict(r) for r in await db.fetch(sql, *args)]


def cash_on_hand_summary(rows: list[dict]) -> dict:
    """{by_currency: [{currency, amount_cents}], orders: [{order_id, currency, amount_cents, agent_name}]}."""
    by_cur: dict[str, int] = {}
    by_order: dict[tuple[int, str], dict] = {}
    for r in rows:
        cur = str(r["currency"]).upper()
        by_cur[cur] = by_cur.get(cur, 0) + int(r["amount_cents"])
        key = (int(r["order_id"]), cur)
        entry = by_order.setdefault(key, {
            "order_id": key[0], "currency": cur, "amount_cents": 0,
            "agent_name": r.get("agent_name") or "", "since": (r.get("created_at") or "")[:10],
        })
        entry["amount_cents"] += int(r["amount_cents"])
    return {
        "by_currency": [{"currency": c, "amount_cents": v} for c, v in sorted(by_cur.items())],
        "orders": list(by_order.values()),
    }


async def split_part_locked(txn: Any, part: dict, take_cents: int) -> bool:
    """Разделить наличную строку: `take_cents` остаётся в ней, остаток — новой
    строкой с новым платежом. Под замком заказа. False — делить нечего
    (одна из частей в валюте заказа округлилась бы до нуля)."""
    total = int(part["amount_cents"])
    order_total = int(part["order_amount_cents"])
    take = int(take_cents)
    if take <= 0 or take >= total:
        return False
    take_order = _q(Decimal(order_total) * Decimal(take) / Decimal(total))
    rest, rest_order = total - take, order_total - take_order
    if take_order <= 0 or rest_order <= 0:
        return False
    pay = await txn.fetchrow(
        "SELECT user_id, username, full_name, currency, comment, created_at, order_id, status "
        "FROM payments WHERE id = $1", int(part["payment_id"]),
    )
    if pay is None or pay["status"] != "pending":
        return False
    await txn.execute("UPDATE payment_parts SET amount_cents = $1, order_amount_cents = $2 WHERE id = $3",
                      take, take_order, int(part["id"]))
    await txn.execute("UPDATE payments SET amount_cents = $1 WHERE id = $2", take_order, int(part["payment_id"]))
    new_pid = await _insert_id(
        txn,
        "INSERT INTO payments (user_id, username, full_name, amount_cents, currency, comment, status, "
        "created_at, order_id) VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)",
        pay["user_id"], pay["username"], pay["full_name"], rest_order, pay["currency"],
        pay["comment"], pay["created_at"], pay["order_id"],
    )
    await _insert_id(
        txn,
        "INSERT INTO payment_parts (payment_id, order_id, method, currency, amount_cents, rate, "
        "order_rate, rate_source, cbu_rate, order_amount_cents, split_from, created_by, "
        "created_by_name, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
        new_pid, int(part["order_id"]), part["method"], part["currency"], rest, part.get("rate"),
        part.get("order_rate"), part["rate_source"], part.get("cbu_rate"), rest_order, int(part["id"]),
        int(part["created_by"]), part.get("created_by_name"), part["created_at"],
    )
    return True


async def allocate_deposit_to_parts_locked(txn: Any, manager_id: int, currency: str, amount_cents: int,
                                           order_ids: list[int] | None) -> tuple[list[dict], int]:
    """FIFO сдачи по наличным на руках. Вызывающий держит замок заказов-кандидатов.

    → (строки [{part_id, order_id, amount_cents}], сколько осталось).
    Сдача берёт строку целиком; если денег меньше строки — строка делится.
    """
    rows = await cash_on_hand(manager_id, currency, conn=txn)
    if order_ids:
        wanted = {int(o) for o in order_ids}
        rows = [r for r in rows if int(r["order_id"]) in wanted]
    left = int(amount_cents)
    taken: list[dict] = []
    for r in rows:
        if left <= 0:
            break
        amt = int(r["amount_cents"])
        if amt > left:
            if not await split_part_locked(txn, r, left):
                break
            amt = left
        taken.append({"part_id": int(r["id"]), "order_id": int(r["order_id"]), "amount_cents": amt,
                      "payment_id": int(r["payment_id"])})
        left -= amt
    return taken, left


async def deposit_currency(deposit_ids: list[int], conn: Any = None) -> dict[int, str]:
    db = conn if conn is not None else adb_core
    ids = sorted({int(d) for d in deposit_ids or []})
    if not ids:
        return {}
    rows = await db.fetch(
        f"SELECT deposit_id, currency FROM cash_deposit_currency WHERE deposit_id IN ({_ph(len(ids))})", *ids
    )
    got = {int(r["deposit_id"]): str(r["currency"]).upper() for r in rows}
    base = _base()
    return {d: got.get(d, base) for d in ids}


async def deposit_orders_view(deposit_ids: list[int], conn: Any = None) -> dict[int, list[dict]]:
    """Что закрывает сдача: наличные строки разбивки + старое распределение
    (`cash_deposit_orders`). Элемент: {order_id, amount, amount_cents, currency, kind}."""
    db = conn if conn is not None else adb_core
    ids = sorted({int(d) for d in deposit_ids or []})
    if not ids:
        return {}
    curs = await deposit_currency(ids, conn=db)
    out: dict[int, list[dict]] = {}
    for r in await db.fetch(
        "SELECT cdp.deposit_id, cdp.order_id, SUM(cdp.amount_cents) AS c FROM cash_deposit_parts cdp "
        f"WHERE cdp.deposit_id IN ({_ph(len(ids))}) GROUP BY cdp.deposit_id, cdp.order_id "
        "ORDER BY cdp.order_id",
        *ids,
    ):
        did = int(r["deposit_id"])
        cents = int(r["c"] or 0)
        out.setdefault(did, []).append({
            "order_id": int(r["order_id"]), "amount_cents": cents, "amount": float(money.from_cents(cents)),
            "amount_allocated": float(money.from_cents(cents)), "amount_allocated_cents": cents,
            "currency": curs[did], "kind": "cash_part",
        })
    for r in await db.fetch(
        "SELECT cdo.deposit_id, cdo.order_id, cdo.amount_allocated_cents, o.currency FROM cash_deposit_orders cdo "
        f"LEFT JOIN orders o ON o.id = cdo.order_id WHERE cdo.deposit_id IN ({_ph(len(ids))}) ORDER BY cdo.order_id",
        *ids,
    ):
        did = int(r["deposit_id"])
        cents = int(r["amount_allocated_cents"] or 0)
        out.setdefault(did, []).append({
            "order_id": int(r["order_id"]), "amount_cents": cents, "amount": float(money.from_cents(cents)),
            "amount_allocated": float(money.from_cents(cents)), "amount_allocated_cents": cents,
            "currency": (r["currency"] or _base()).upper(), "kind": "debt",
        })
    return out


async def confirm_deposit_parts_locked(txn: Any, deposit_id: int, rates: dict[str, float | None]) -> list[int]:
    """Подтвердить платежи наличных строк сдачи. Под замком заказов → [order_id]."""
    rows = await txn.fetch(
        "SELECT pp.payment_id, pp.order_id, p.currency FROM cash_deposit_parts cdp "
        "JOIN payment_parts pp ON pp.id = cdp.part_id JOIN payments p ON p.id = pp.payment_id "
        "WHERE cdp.deposit_id = $1",
        int(deposit_id),
    )
    now = _now()
    orders: set[int] = set()
    for r in rows:
        rc = await txn.execute(
            "UPDATE payments SET status = 'confirmed', confirmed_at = $1 WHERE id = $2 AND status = 'pending'",
            now, int(r["payment_id"]),
        )
        rate = rates.get(str(r["currency"] or "").upper())
        if rc > 0 and rate is not None:
            await txn.execute(
                "UPDATE payments SET fx_rate_to_base = $1 WHERE id = $2 AND fx_rate_to_base IS NULL",
                float(rate), int(r["payment_id"]),
            )
        orders.add(int(r["order_id"]))
    return sorted(orders)


async def deposit_part_orders(deposit_id: int, conn: Any = None) -> tuple[list[int], set[str]]:
    db = conn if conn is not None else adb_core
    rows = await db.fetch(
        "SELECT DISTINCT pp.order_id, p.currency FROM cash_deposit_parts cdp "
        "JOIN payment_parts pp ON pp.id = cdp.part_id JOIN payments p ON p.id = pp.payment_id "
        "WHERE cdp.deposit_id = $1",
        int(deposit_id),
    )
    return sorted({int(r["order_id"]) for r in rows}), {str(r["currency"] or "").upper() for r in rows}


async def attach_deposit_to_parts(deposit_id: int, *, dry_run: bool = True) -> dict:
    """Разово: сдача без распределения (как прод-сдачи #1/#2, «Заказы: —»)
    ложится FIFO на наличные строки разбивки своего менеджера в своей валюте.
    Подтверждённая сдача сразу подтверждает платежи строк и закрывает покрытые
    заказы — как если бы её подтвердили после разбивки. Для
    `scripts/migrate_payment_breakdown`; в рабочем коде не зовётся.

    → {ok, deposit_id, status, currency, parts: [...], unallocated_cents, closed}.
    """
    from services import database as db
    from services.debts import lock_orders

    dep = await adb_core.fetchrow("SELECT * FROM cash_deposits WHERE id = $1", int(deposit_id))
    if dep is None:
        return {"ok": False, "error": f"Сдача #{deposit_id} не найдена"}
    if dep["status"] not in ("pending", "confirmed") or int(dep["amount_cents"]) <= 0:
        return {"ok": False, "error": f"Сдача #{deposit_id}: статус {dep['status']}, сумма {dep['amount_cents']} — не распределяется"}
    has_alloc = await adb_core.fetchval(
        "SELECT (SELECT COUNT(*) FROM cash_deposit_orders WHERE deposit_id = $1) + "
        "(SELECT COUNT(*) FROM cash_deposit_parts WHERE deposit_id = $1)", int(deposit_id),
    )
    if int(has_alloc or 0):
        return {"ok": False, "error": f"Сдача #{deposit_id} уже распределена — не трогаю"}
    currency = (await deposit_currency([int(deposit_id)]))[int(deposit_id)]
    rows = await cash_on_hand(int(dep["manager_id"]), currency)
    if dry_run:
        left = int(dep["amount_cents"])
        plan = []
        for r in rows:
            if left <= 0:
                break
            take = min(left, int(r["amount_cents"]))
            plan.append({"order_id": int(r["order_id"]), "part_id": int(r["id"]), "amount_cents": take})
            left -= take
        return {"ok": True, "dry_run": True, "deposit_id": int(deposit_id), "status": dep["status"],
                "currency": currency, "parts": plan, "unallocated_cents": left, "closed": []}

    rates = {}
    for r in rows:
        cur = str(r.get("order_currency") or "").upper()
        if cur and cur not in rates:
            rates[cur] = await asyncio.to_thread(db.get_currency_rate, cur)
    closed: list[int] = []
    async with adb_core.transaction() as txn:
        await lock_orders(txn, [int(r["order_id"]) for r in rows])
        taken, left = await allocate_deposit_to_parts_locked(
            txn, int(dep["manager_id"]), currency, int(dep["amount_cents"]),
            sorted({int(r["order_id"]) for r in rows}),
        )
        await txn.execute(
            "INSERT INTO cash_deposit_currency (deposit_id, currency) VALUES ($1, $2) "
            "ON CONFLICT (deposit_id) DO NOTHING", int(deposit_id), currency,
        )
        for p in taken:
            await txn.execute(
                "INSERT INTO cash_deposit_parts (deposit_id, part_id, order_id, amount_cents) "
                "VALUES ($1, $2, $3, $4)", int(deposit_id), p["part_id"], p["order_id"], p["amount_cents"],
            )
        if dep["status"] == "confirmed":
            for oid in await confirm_deposit_parts_locked(txn, int(deposit_id), rates):
                done, _c = await db._close_order_if_covered_locked(
                    txn, oid, dep["confirmed_by"], "перенос разбивки оплаты"
                )
                if done:
                    closed.append(oid)
    return {"ok": True, "dry_run": False, "deposit_id": int(deposit_id), "status": dep["status"],
            "currency": currency, "parts": taken, "unallocated_cents": left, "closed": closed}


# ─── Кто подтверждает ────────────────────────────────────────────────────────


CONFIRM_FORBIDDEN = "confirm_forbidden"
NO_CONFIRMERS_NOTE = "руководителя/бухгалтера в системе нет"


def _active_confirmers() -> list[dict]:
    from services.database import get_all_users

    return [
        u for u in get_all_users()
        if not u.get("deactivated_at") and u.get("role") in ROLES_CONFIRM
    ]


async def confirm_rights(actor_id: int, owner_ids: Any, actor_role: str | None = None) -> dict:
    """Может ли `actor_id` подтвердить деньги, внесённые `owner_ids`. СЕРВИСНЫЙ рубеж
    для сдачи и платежа (HTTP, кнопки бота, любые будущие пути).

    * носитель роли (admin/boss/bookkeeper) — да; но бухгалтер не подтверждает
      СВОИ деньги, пока в системе есть другой активный подтверждающий;
      руководитель, внёсший сам, — да (как «Получил деньги» в бухгалтерии);
    * менеджер (бухгалтер только через `ROLE_ALSO_ACTS_AS`) и любой другой —
      только пока активных admin/boss/bookkeeper нет вовсе (`mode='no_boss'`).
      Совмещение ролей не даёт права подтверждать свои или чужие деньги в
      обход живого руководителя.

    → {allowed, mode ('holder'|'no_boss'|None), own, confirmers_exist, names,
       error, note}. `note` — пометка для аудита/экрана («подтвердил сам…»),
    выводится из ФАКТИЧЕСКОГО наличия подтверждающих, а не из роли.
    """
    from services.database import get_role

    uid = int(actor_id)
    role = actor_role if actor_role is not None else await asyncio.to_thread(get_role, uid)
    holders = await asyncio.to_thread(_active_confirmers)
    others = [u for u in holders if int(u["user_id"]) != uid]
    names = [u.get("full_name") or str(u["user_id"]) for u in others][:3]
    owners = {int(o) for o in (owner_ids or []) if o is not None}
    own = uid in owners
    who = ", ".join(names) or "руководитель или бухгалтер"
    out = {"allowed": True, "mode": "holder", "own": own, "confirmers_exist": bool(holders),
           "names": names, "error": None, "note": None}
    if role in ROLES_CONFIRM:
        if own and role not in ("admin", "boss") and others:
            out.update(allowed=False, mode=None,
                       error=f"Свои деньги не подтверждают сами: подтвердит {who}")
        elif own:
            out["note"] = ("внёс и подтвердил руководитель" if role in ("admin", "boss")
                           else "внёс и подтвердил сам — другого подтверждающего в системе нет")
        return out
    if others:
        out.update(allowed=False, mode=None,
                   error=f"Подтверждает {who}: пока в системе есть руководитель или бухгалтер, "
                         "менеджер деньги не подтверждает")
        return out
    out["mode"] = "no_boss"
    out["note"] = (f"подтверждено самим сдающим — {NO_CONFIRMERS_NOTE}" if own
                   else f"подтвердил {role or 'сотрудник'} — {NO_CONFIRMERS_NOTE}")
    return out


async def require_confirm_rights(actor_id: int, owner_ids: Any, actor_role: str | None = None) -> dict:
    """`confirm_rights` или `PaymentError(403, code=confirm_forbidden)`."""
    rights = await confirm_rights(actor_id, owner_ids, actor_role)
    if not rights["allowed"]:
        raise PaymentError(rights["error"], status=403, code=CONFIRM_FORBIDDEN)
    return rights
