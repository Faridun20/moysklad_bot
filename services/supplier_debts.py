"""
Кредиторка: «сколько мы должны поставщикам» — зеркало `services/receivables.py`.

Долг перед поставщиком — НЕ отдельная сущность, ровно как долг клиента: там
это заказ `payment_type='credit'`, здесь — ПРИХОДНАЯ накладная с контрагентом.
Второй таблицы «долг» нет намеренно: сумма долга обязана быть той же суммой, по
которой товар пришёл на склад, а две записи одного числа расходятся на первой
же правке цены (её вписывают позже — «Закупка и себестоимость»).

Три решения, определяющие модуль:

* **Долг = приходная накладная, платёж = `supplier_payments`.** Ровно та же
  формула, по которой перенос истории из МойСклад показывает расчёты с
  поставщиками (`scripts/migrate_history_from_moysklad.show_supplier_balance`):
  приход минус выплаты. Строка `supplier_payments` заводится ТОЛЬКО здесь и
  переносом — в `payments` исходящие деньги не попадают никогда, там деньги ОТ
  клиентов и на них считается вся дебиторка.

* **Условий по умолчанию нет.** Накладная без строки `supplier_invoice_terms` —
  это долг: четыреста исторических накладных из МойСклад размечать задним
  числом некому, а долг по ним настоящий. «Уже оплачено» — явная отметка, она и
  убирает накладную из долгов.

* **Валюты не складываем молча.** Блок сумм — общий с дебиторкой
  (`receivables.money_block`): разбивка по валютам + конвертированный итог с
  флагом `partial`. Курс к валюте долга при выплате считает тот же код, что и
  разбивка оплаты заказа (`order_payments.compute_parts`), поэтому свой курс
  здесь так же не уходит дальше `manual_rate_max_deviation_pct` от ЦБ.

**Аванс поставщику — не ошибка.** Выплатили больше, чем поставлено, — обычная
практика (о ней прямо сказано в переносе истории), поэтому общий платёж сверх
долга принимается и показывается отдельной строкой «аванс», а не отвергается.
Платёж, привязанный к КОНКРЕТНОЙ накладной, больше её остатка — другое дело:
это опечатка в сумме, и она отвергается текстом.

Права — admin/boss (кортежи `_SUPPLIER_SEE_ROLES`/`_SUPPLIER_RECORD_ROLES` в
`webapp/server.py`), то есть те же, что у себестоимости (`costing.COST_ROLES`),
а НЕ «бухгалтерские»: сумма прихода это закупочная цена, то есть
себестоимость, и экран «Поставщикам» показывает её прямым текстом. Открыть его
бухгалтеру сегодня значит открыть его и менеджеру — бухгалтера в штате нет, и
`roles.ROLE_ALSO_ACTS_AS` отдаёт его права менеджеру, а наценка в обход
`costing.redact_invoice` — ровно то, что `costing` и закрывает. Появится
настоящий бухгалтер (снимется `roles.PAUSED_ROLES`) — добавить его сюда и в
`costing.COST_ROLES` одной правкой; решение владельца ждём (см. CLAUDE.md,
«Долги поставщикам»).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from services import adb_core, money, order_payments, pay_accounts, receivables
from services.order_payments import Actor, PaymentError

logger = logging.getLogger(__name__)

# Права на экран и выплату — литеральными кортежами в `webapp/server.py`
# (`_SUPPLIER_SEE_ROLES`/`_SUPPLIER_RECORD_ROLES`): их разбирает
# `scripts/gen_role_matrix.py`, а он читает ТОЛЬКО server.py. Второй список
# здесь разошёлся бы с ним на первой же правке, поэтому его нет.
PAYMENT_TYPES = ("credit", "paid")
NOTE_MAX = 500
# Сколько строк «как заплатили» принимает одна форма. Каждая строка — отдельная
# выплата в истории: у платежа поставщику один способ, «наличные + карта» это
# две выплаты, и в ленте они обязаны читаться по отдельности.
MAX_PARTS = order_payments.MAX_PARTS


def _base() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


def _allowed_currencies() -> tuple[str, ...]:
    from config import ALLOWED_CURRENCIES

    return tuple(c.upper() for c in ALLOWED_CURRENCIES)


def _now() -> str:
    from services.database import now_str

    return now_str()


def _fx_to_base(quote: Any) -> float | None:
    """«Сум за доллар» → «долларов за сум»: семантика `fx_rate_to_base`.

    В `currency_rates` и в снимках `orders/payments.fx_rate_to_base` курс лежит
    множителем к базовой валюте (`convert_to_base_at` его умножает), а расчёт
    разбивки работает с обратной величиной («единиц валюты за 1 базовую», как
    её называет человек). Перепутать их — завысить итог в десятки тысяч раз.
    """
    from decimal import Decimal

    if quote in (None, 0):
        return None
    try:
        value = Decimal(str(quote))
    except (ArithmeticError, ValueError):
        return None
    if value <= 0:
        return None
    return float(Decimal(1) / value)


# ─── Модель строки долга ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SupplierDebt:
    """Одна приходная накладная как долг перед поставщиком."""

    invoice_id: int
    invoice_number: str
    invoice_date: str
    supplier_id: int
    supplier_name: str
    container_id: int | None
    currency: str
    total_cents: int
    direct_cents: int       # выплаты, привязанные к этой накладной
    general_cents: int      # доля общих выплат поставщику (FIFO, соглашение)
    remaining_cents: int
    due_date: str | None    # из условий; нет — стареет со дня накладной
    payment_type: str       # credit | paid


def debt_due_date(row: dict) -> str | None:
    """Срок, по которому стареет долг: заданный в условиях или дата прихода.

    У накладной своего срока обычно нет — его знает только тот, кто
    договаривался. Пока он не задан, считаем от даты прихода: товар на складе,
    значит деньги причитаются, и «не задан срок» не должно означать «не
    просрочено никогда» (то же правило, что у `database.debt_due_date` для
    заказа «оплата сразу»).
    """
    due = (row.get("due_date") or "").strip() if row.get("due_date") else ""
    return due[:10] or (str(row.get("invoice_date") or "")[:10] or None)


def as_receivable(d: SupplierDebt) -> receivables.Receivable:
    """Строка долга в общем виде — ради общих сумматоров (`money_block`,
    `aging`, `by_counterparty`). Второго набора корзин просрочки быть не
    должно: «просрочено 60—90» у нас и у клиента — одно и то же окно."""
    return receivables.Receivable(
        source="supplier",
        ref_id=d.invoice_id,
        title=d.invoice_number,
        counterparty=d.supplier_name,
        owner_id=None,
        due_date=d.due_date,
        amount_cents=d.remaining_cents,
        currency=d.currency,
    )


# ─── Чтение ──────────────────────────────────────────────────────────────────


_INVOICES_SQL = (
    "SELECT i.id, i.invoice_number, i.invoice_date, i.currency, i.total_amount_cents, "
    "       i.counterparty_id, i.comment, c.name AS supplier_name, "
    "       t.payment_type, t.due_date, cr.container_id "
    "FROM invoices i "
    "JOIN counterparties c ON c.id = i.counterparty_id "
    "LEFT JOIN supplier_invoice_terms t ON t.invoice_id = i.id "
    "LEFT JOIN container_receipt cr ON cr.invoice_id = i.id "
    "WHERE i.type = 'incoming' AND i.status = 'confirmed' "
    "      AND i.counterparty_id IS NOT NULL "
)

_PAYMENTS_SQL = (
    "SELECT sp.id, sp.counterparty_id, sp.invoice_id, sp.currency, sp.amount_cents, "
    "       sp.comment, sp.paid_at, sp.created_at, sp.ms_paymentout_id, sp.supplier_name, "
    "       spp.method, spp.account_id, spp.debt_currency, spp.debt_amount_cents, "
    "       spp.rate, spp.rate_source, spp.created_by, spp.created_by_name, "
    f"      {pay_accounts.ACCOUNT_COLUMNS_SQL} "
    "FROM supplier_payments sp "
    "LEFT JOIN supplier_payment_parts spp ON spp.payment_id = sp.id "
    f"{pay_accounts.account_join_sql('spp')}"
    "WHERE sp.counterparty_id IS NOT NULL "
)


def effective_payment(row: dict) -> tuple[str, int]:
    """Чем и сколько платёж гасит долг: (валюта долга, копейки).

    У выплат, записанных формой, это `debt_currency`/`debt_amount_cents` —
    сумы, которыми закрыли долларовый приход, пересчитаны на момент выплаты.
    У перенесённых из МойСклад сайдкара нет: там валюта платежа и есть валюта
    погашения.
    """
    cents = row.get("debt_amount_cents")
    cur = row.get("debt_currency")
    if cents is None or not cur:
        return (str(row.get("currency") or _base()).upper(), int(row.get("amount_cents") or 0))
    return (str(cur).upper(), int(cents))


async def _fetch_invoices(supplier_id: int | None, conn: Any = None) -> list[dict]:
    db = conn if conn is not None else adb_core
    sql = _INVOICES_SQL
    args: list[Any] = []
    if supplier_id:
        args.append(int(supplier_id))
        sql += f"AND i.counterparty_id = ${len(args)} "
    sql += "ORDER BY i.invoice_date, i.id"
    return [dict(r) for r in await db.fetch(sql, *args)]


async def _fetch_payments(supplier_id: int | None, conn: Any = None) -> list[dict]:
    db = conn if conn is not None else adb_core
    sql = _PAYMENTS_SQL
    args: list[Any] = []
    if supplier_id:
        args.append(int(supplier_id))
        sql += f"AND sp.counterparty_id = ${len(args)} "
    sql += "ORDER BY COALESCE(sp.paid_at, sp.created_at), sp.id"
    return [dict(r) for r in await db.fetch(sql, *args)]


@dataclass(frozen=True)
class Ledger:
    """Расчёты с поставщиками целиком: долги, авансы и приход без цены."""

    debts: list[SupplierDebt]
    advances: dict[tuple[int, str], int]      # (supplier_id, валюта) → копейки аванса
    payments: list[dict]                       # лента выплат (сырые строки + подписи)
    unpriced: list[dict]                       # приход с контрагентом, но без суммы
    suppliers: dict[int, str]                  # id → имя


def _allocate(invoices: list[dict], payments: list[dict]) -> Ledger:
    """Чистая раскладка выплат по накладным. Тестируется без БД.

    Привязанная выплата гасит СВОЮ накладную. Выплата без накладной (и та, чья
    валюта с накладной не сошлась — пересчитать её нечем) гасит открытые
    накладные того же поставщика в той же валюте ПО ПОРЯДКУ, от старых к новым.
    Это СОГЛАШЕНИЕ, а не факт из документа, — то же, что делает перенос истории
    с платежами без основания и `create_cash_deposit` со сдачами.
    """
    direct: dict[int, int] = {}
    pool: dict[tuple[int, str], int] = {}
    inv_by_id = {int(i["id"]): i for i in invoices}
    suppliers: dict[int, str] = {}

    for p in payments:
        # Имя поставщика знают и выплаты: у того, кому только переплатили,
        # накладных нет вовсе, а строка «аванс» без имени бесполезна.
        if p.get("counterparty_id") is not None and p.get("supplier_name"):
            suppliers.setdefault(int(p["counterparty_id"]), str(p["supplier_name"]))
        cur, cents = effective_payment(p)
        if cents <= 0:
            continue
        inv_id = p.get("invoice_id")
        inv = inv_by_id.get(int(inv_id)) if inv_id else None
        if inv is not None and str(inv["currency"] or "").upper() == cur:
            direct[int(inv_id)] = direct.get(int(inv_id), 0) + cents
            continue
        # Накладная чужая/не наша выборка/валюта не та — деньги не теряем, они
        # уходят в общий счёт поставщика.
        cp = p.get("counterparty_id")
        if cp is None:
            continue
        pool[(int(cp), cur)] = pool.get((int(cp), cur), 0) + cents

    debts: list[SupplierDebt] = []
    unpriced: list[dict] = []
    open_rows: dict[tuple[int, str], list[tuple[dict, int]]] = {}

    for inv in invoices:
        inv_id = int(inv["id"])
        sup_id = int(inv["counterparty_id"])
        suppliers[sup_id] = inv.get("supplier_name") or suppliers.get(sup_id) or "—"
        ptype = (inv.get("payment_type") or "credit")
        total = int(inv.get("total_amount_cents") or 0)
        cur = str(inv["currency"] or "").upper()
        if ptype == "paid":
            continue
        if total <= 0:
            # Приход без цены: контейнер посчитали, закупочную ещё не вписали.
            # Молча пропустить его — значит ответить «поставщику ничего не
            # должны» про товар, который стоит на складе.
            unpriced.append({
                "invoice_id": inv_id,
                "invoice_number": inv.get("invoice_number") or "",
                "invoice_date": str(inv.get("invoice_date") or "")[:10],
                "supplier_id": sup_id,
                "supplier_name": suppliers[sup_id],
                "container_id": int(inv["container_id"]) if inv.get("container_id") else None,
            })
            continue
        rest = max(0, total - direct.get(inv_id, 0))
        open_rows.setdefault((sup_id, cur), []).append((inv, rest))

    for (sup_id, cur), rows in open_rows.items():
        left = pool.pop((sup_id, cur), 0)
        for inv, rest in rows:
            inv_id = int(inv["id"])
            take = min(left, rest)
            left -= take
            debts.append(SupplierDebt(
                invoice_id=inv_id,
                invoice_number=inv.get("invoice_number") or "",
                invoice_date=str(inv.get("invoice_date") or "")[:10],
                supplier_id=sup_id,
                supplier_name=suppliers[sup_id],
                container_id=int(inv["container_id"]) if inv.get("container_id") else None,
                currency=cur,
                total_cents=int(inv.get("total_amount_cents") or 0),
                direct_cents=direct.get(inv_id, 0),
                general_cents=take,
                remaining_cents=max(0, rest - take),
                due_date=debt_due_date(inv),
                payment_type=inv.get("payment_type") or "credit",
            ))
        if left > 0:
            pool[(sup_id, cur)] = left

    debts.sort(key=lambda d: (d.due_date or "", d.invoice_id))
    return Ledger(
        debts=debts,
        advances={k: v for k, v in pool.items() if v > 0},
        payments=payments,
        unpriced=unpriced,
        suppliers=suppliers,
    )


async def ledger(supplier_id: int | None = None, conn: Any = None) -> Ledger:
    """Расчёты с поставщиками: два запроса на любое число накладных и выплат."""
    invoices = await _fetch_invoices(supplier_id, conn=conn)
    payments = await _fetch_payments(supplier_id, conn=conn)
    return _allocate(invoices, payments)


# ─── Виды для экрана ─────────────────────────────────────────────────────────


def debt_view(d: SupplierDebt, today: str) -> dict:
    return {
        "invoice_id": d.invoice_id,
        "invoice_number": d.invoice_number,
        "invoice_date": d.invoice_date,
        "supplier_id": d.supplier_id,
        "supplier_name": d.supplier_name,
        "container_id": d.container_id,
        "currency": d.currency,
        "total": float(money.from_cents(d.total_cents)),
        "paid": float(money.from_cents(d.direct_cents + d.general_cents)),
        "remaining": float(money.from_cents(d.remaining_cents)),
        "remaining_cents": d.remaining_cents,
        "due_date": d.due_date,
        "payment_type": d.payment_type,
        "days": _days_since(d.due_date, today),
        "state": (
            "overdue" if d.due_date and d.due_date < today
            else ("due_today" if d.due_date == today else "upcoming")
        ),
    }


def _days_since(due: str | None, today: str) -> int | None:
    """Сколько дней долг «висит»: срок минус сегодня. Отрицательное — срок ещё
    не наступил. Считаем в Python от локальной даты — сравнивать строку даты с
    `NOW()` в SQL нельзя (разные кадры времени, CLAUDE.md)."""
    if not due:
        return None
    try:
        return (date.fromisoformat(today) - date.fromisoformat(due[:10])).days
    except ValueError:
        return None


def payment_view(row: dict) -> dict:
    """Строка ленты выплат. Подпись счёта считает сервер — как у поступлений."""
    method = row.get("method")
    account = pay_accounts.from_prefixed(row) if method in order_payments.NONCASH_METHODS else None
    cur = str(row.get("currency") or _base()).upper()
    debt_cur, debt_cents = effective_payment(row)
    return {
        "id": int(row["id"]),
        "supplier_id": int(row["counterparty_id"]) if row.get("counterparty_id") else None,
        "invoice_id": int(row["invoice_id"]) if row.get("invoice_id") else None,
        "amount": float(money.from_cents(int(row.get("amount_cents") or 0))),
        "currency": cur,
        "debt_amount": float(money.from_cents(debt_cents)),
        "debt_currency": debt_cur,
        "method": method,
        "method_label": order_payments.METHODS.get(method or "", ""),
        "account_id": account["id"] if account else None,
        "account_label": pay_accounts.source_label(method, account),
        "rate": row.get("rate"),
        "rate_source": row.get("rate_source"),
        "comment": row.get("comment") or "",
        "paid_at": str(row.get("paid_at") or row.get("created_at") or "")[:16],
        "created_by": int(row["created_by"]) if row.get("created_by") else None,
        "created_by_name": row.get("created_by_name") or "",
        # Перенесённые из МойСклад выплат не редактировались человеком и способа
        # не несут — экран подписывает их «перенос», а не пустым способом.
        "migrated": bool(row.get("ms_paymentout_id")),
    }


def payment_label(method: str | None, amount_cents: int, currency: str,
                  account: dict | None = None) -> str:
    """«с карты •••• 1234 (Фаридун М.) · 7 130 USD» — подпись выплаты."""
    src = pay_accounts.source_label(method, account) if method in order_payments.NONCASH_METHODS else None
    amount = order_payments.fmt_cents(amount_cents, currency)
    if src:
        return f"{src} · {amount}"
    return f"{order_payments.METHODS.get(method or '', 'выплата')} {amount}"


def by_supplier(led: Ledger, today: str) -> list[dict]:
    """«Кому и сколько должны» — строка на поставщика.

    Сортировка по конвертированному итогу; поставщики без курса — в конец, но
    не выброшены: долг существует и без курса.
    """
    grouped: dict[int, list[SupplierDebt]] = {}
    for d in led.debts:
        if d.remaining_cents > 0:
            grouped.setdefault(d.supplier_id, []).append(d)
    rows: list[dict] = []
    seen = set(grouped)
    for sup_id, items in grouped.items():
        block = receivables.money_block([as_receivable(d) for d in items])
        oldest = min((d.due_date or "9999-12-31") for d in items)
        rows.append({
            "supplier_id": sup_id,
            "supplier_name": led.suppliers.get(sup_id, "—"),
            "invoices": len(items),
            "oldest_due": None if oldest == "9999-12-31" else oldest,
            "days": _days_since(None if oldest == "9999-12-31" else oldest, today),
            "advance": _advance_block(led, sup_id),
            **block,
        })
    # Поставщик, которому только переплатили, в долгах не появится — а аванс у
    # него лежит, и разговор с ним начинается именно с этого.
    for (sup_id, _cur), cents in led.advances.items():
        if sup_id in seen or cents <= 0:
            continue
        seen.add(sup_id)
        rows.append({
            "supplier_id": sup_id,
            "supplier_name": led.suppliers.get(sup_id, "—"),
            "invoices": 0,
            "oldest_due": None,
            "days": None,
            "advance": _advance_block(led, sup_id),
            **receivables.money_block([]),
        })
    rows.sort(key=lambda x: (x["base_total"] is None, -(x["base_total"] or 0)))
    return rows


def _advance_block(led: Ledger, supplier_id: int) -> list[dict]:
    return [
        {"currency": cur, "total": float(money.from_cents(cents))}
        for (sup, cur), cents in sorted(led.advances.items())
        if sup == supplier_id and cents > 0
    ]


# ─── Условия оплаты прихода ──────────────────────────────────────────────────


def parse_due_date(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as e:
        raise PaymentError("Срок оплаты — дата в виде ГГГГ-ММ-ДД") from e
    return text


async def _load_incoming_invoice(invoice_id: int, conn: Any = None) -> dict:
    db = conn if conn is not None else adb_core
    row = await db.fetchrow(
        "SELECT id, type, status, currency, counterparty_id, total_amount_cents, invoice_number "
        "FROM invoices WHERE id = $1",
        int(invoice_id),
    )
    if row is None:
        raise PaymentError("Приход не найден — обновите список", status=404)
    inv = dict(row)
    if inv["type"] != "incoming":
        raise PaymentError("Долг поставщику считается только по приходу", status=409)
    if inv["status"] != "confirmed":
        raise PaymentError("Приход отменён — долга по нему нет", status=409, code="cancelled")
    if inv.get("counterparty_id") is None:
        raise PaymentError(
            "У прихода не указан поставщик — укажите его в приходе, иначе долг не к кому "
            "отнести", status=409, code="no_supplier",
        )
    return inv


async def set_terms(actor: Actor, invoice_id: Any, payment_type: str,
                    due_date: Any = None) -> dict:
    """Условия оплаты приходной накладной: «в долг» + срок или «уже оплачено».

    «Уже оплачено» убирает накладную из долгов и НЕ пишет выплату: чем и с
    какого счёта заплатили в тот раз, форма не знает, а выдумать способ значит
    соврать в ленте денег. Нужна выплата — её вносят обычной формой.
    """
    if payment_type not in PAYMENT_TYPES:
        raise PaymentError("Условия оплаты: «в долг» или «уже оплачено»")
    try:
        inv_id = int(invoice_id)
    except (TypeError, ValueError) as e:
        raise PaymentError("Не выбран приход — обновите список") from e
    due = parse_due_date(due_date) if payment_type == "credit" else None
    inv = await _load_incoming_invoice(inv_id)
    now = _now()
    # UPDATE-или-INSERT — под замком накладной и одной транзакцией: без него два
    # нажатия подряд оба видят «строки нет» и второй INSERT падает на PK (500
    # вместо повторного сохранения тех же условий).
    async with adb_core.transaction() as txn:
        await lock_invoices(txn, [inv_id])
        updated = await txn.execute(
            "UPDATE supplier_invoice_terms SET payment_type = $1, due_date = $2, updated_at = $3 "
            "WHERE invoice_id = $4",
            payment_type, due, now, inv_id,
        )
        if not updated:
            await txn.execute(
                "INSERT INTO supplier_invoice_terms (invoice_id, payment_type, due_date, "
                "created_by, created_by_name, created_at) VALUES ($1, $2, $3, $4, $5, $6)",
                inv_id, payment_type, due, actor.user_id, actor.name, now,
            )
    label = "оплачено сразу" if payment_type == "paid" else f"в долг до {due or '—'}"
    await _audit(actor, "supplier_terms_set",
                 f"Приход {inv.get('invoice_number') or inv_id}: {label}")
    return {"ok": True, "invoice_id": inv_id, "payment_type": payment_type, "due_date": due}


# ─── Выплата поставщику ──────────────────────────────────────────────────────


async def lock_invoices(txn: Any, invoice_ids: list[int]) -> None:
    """`FOR UPDATE` строк приходных накладных — замок «сколько по приходу уже
    выплачено», по возрастанию id.

    Тот же приём, что `debts.lock_orders`: две выплаты по одной накладной без
    него обе видят прежний остаток и обе проходят проверку «не больше
    остатка». На SQLite пишущая транзакция одна — no-op.
    """
    ids = sorted({int(i) for i in invoice_ids or []})
    if not ids or not adb_core._use_postgres():
        return
    await txn.fetch(
        "SELECT id FROM invoices WHERE id = ANY($1::bigint[]) ORDER BY id FOR UPDATE", ids
    )


async def _invoice_paid_cents(txn: Any, invoice_id: int, currency: str) -> int:
    """Сколько по накладной уже выплачено, в её валюте. Под замком накладной."""
    rows = await txn.fetch(
        "SELECT sp.currency, sp.amount_cents, spp.debt_currency, spp.debt_amount_cents "
        "FROM supplier_payments sp LEFT JOIN supplier_payment_parts spp ON spp.payment_id = sp.id "
        "WHERE sp.invoice_id = $1",
        int(invoice_id),
    )
    total = 0
    for r in rows:
        cur, cents = effective_payment(dict(r))
        if cur == currency.upper():
            total += cents
    return total


async def _supplier_name(supplier_id: int, conn: Any = None) -> str:
    db = conn if conn is not None else adb_core
    name = await db.fetchval("SELECT name FROM counterparties WHERE id = $1", int(supplier_id))
    if name is None:
        raise PaymentError(f"Поставщик #{supplier_id} не найден — выберите его в справочнике", status=404)
    return str(name)


def parse_paid_at(raw: Any) -> str:
    """Дата выплаты → момент для `supplier_payments.paid_at`.

    Сегодняшняя дата получает текущее время (лента сортируется по моменту),
    прошедшая — полночь: выдумывать час задним числом незачем. Будущая дата
    отвергается — деньги, которых ещё не отдали, долг не гасят.
    """
    now = _now()
    if raw in (None, ""):
        return now
    day = str(raw).strip()[:10]
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as e:
        raise PaymentError("Дата выплаты — в виде ГГГГ-ММ-ДД") from e
    from utils.helpers import local_now

    today = local_now().date()
    if parsed > today:
        raise PaymentError("Дата выплаты — не в будущем")
    return now if parsed == today else f"{day} 00:00:00"


async def record_payment(actor: Actor, data: dict, *, idem_key: str | None = None) -> dict:
    """Записать выплату поставщику. Одна транзакция на все строки формы.

    `data`: supplier_id, parts[{method, currency, amount, rate?, account_id?}],
    invoice_id? (привязать к конкретному приходу), currency? (валюта долга у
    общей выплаты), note?, paid_at?.

    Каждая строка формы — отдельная строка `supplier_payments` в валюте, в
    которой деньги реально ушли, плюс сайдкар с курсом и суммой в валюте долга.
    """
    from services.database import idem_store_in

    try:
        supplier_id = int(data.get("supplier_id") or 0)
    except (TypeError, ValueError) as e:
        raise PaymentError("Выберите поставщика") from e
    if supplier_id <= 0:
        raise PaymentError("Выберите поставщика")
    supplier_name = await _supplier_name(supplier_id)

    raw_invoice = data.get("invoice_id")
    invoice: dict | None = None
    if raw_invoice not in (None, "", 0, "0"):
        try:
            invoice_id = int(raw_invoice)
        except (TypeError, ValueError) as e:
            raise PaymentError("Выберите приход из списка") from e
        invoice = await _load_incoming_invoice(invoice_id)
        if int(invoice["counterparty_id"]) != supplier_id:
            raise PaymentError("Этот приход оформлен на другого поставщика", status=409)
        terms = await adb_core.fetchrow(
            "SELECT payment_type FROM supplier_invoice_terms WHERE invoice_id = $1", invoice_id
        )
        if terms is not None and terms["payment_type"] == "paid":
            raise PaymentError(
                "Приход отмечен как уже оплаченный — снимите отметку, если по нему всё же "
                "платят", status=409, code="already_paid",
            )
        debt_currency = str(invoice["currency"] or _base()).upper()
    else:
        debt_currency = str(data.get("currency") or _base()).upper()
        if debt_currency not in _allowed_currencies():
            raise PaymentError(f"Валюта {debt_currency or '—'} не поддерживается")

    inputs = order_payments.parse_parts(data.get("parts"))
    base = _base()
    cbu = await order_payments._cbu_for({i.currency for i in inputs} | {debt_currency})
    # Свой курс не дальше `manual_rate_max_deviation_pct` от ЦБ — тот же
    # рубеж, что у оплаты заказа: формула одна, и послаблений «для выплат» нет.
    calcs = order_payments.compute_parts(inputs, debt_currency, base, cbu, role=actor.role)

    accounts: dict[int, dict] = {}
    for n, inp in enumerate(inputs, start=1):
        if inp.account_id is None:
            continue
        try:
            accounts[inp.account_id] = await pay_accounts.check_for_method(
                inp.account_id, inp.method, row=n
            )
        except pay_accounts.AccountError as e:
            raise PaymentError(e.message, status=e.status, code=e.code) from e

    note = str(data.get("note") or "").strip()[:NOTE_MAX]
    paid_at = parse_paid_at(data.get("paid_at"))
    total_debt_cents = sum(c.order_amount_cents for c in calcs)
    result: dict[str, Any] = {}

    async with adb_core.transaction() as txn:
        if invoice is not None:
            await lock_invoices(txn, [int(invoice["id"])])
            fresh = await txn.fetchrow(
                "SELECT status, total_amount_cents, currency FROM invoices WHERE id = $1",
                int(invoice["id"]),
            )
            if fresh is None or fresh["status"] != "confirmed":
                raise PaymentError("Приход отменён — долга по нему нет", status=409,
                                   code="cancelled")
            total = int(fresh["total_amount_cents"] or 0)
            if total <= 0:
                raise PaymentError(
                    "У прихода не заполнена сумма: впишите закупочные цены — без них долг "
                    "считать не из чего", status=409, code="no_amount",
                )
            already = await _invoice_paid_cents(txn, int(invoice["id"]), debt_currency)
            rest = max(0, total - already)
            tol = order_payments.tolerance_cents(calcs, debt_currency, base)
            if total_debt_cents > rest + tol:
                raise PaymentError(
                    f"Больше, чем осталось по приходу: к оплате "
                    f"{order_payments.fmt_cents(rest, debt_currency)}, введено "
                    f"{order_payments.fmt_cents(total_debt_cents, debt_currency)}. Аванс "
                    "поставщику вносят выплатой без привязки к приходу",
                    status=409, code="over",
                )

        now = _now()
        payments_out: list[dict] = []
        for c in calcs:
            account = accounts.get(c.account_id) if c.account_id is not None else None
            head = f"Выплата поставщику {supplier_name}"
            if invoice is not None:
                head += f" · приход {invoice.get('invoice_number') or invoice['id']}"
            comment = f"{head} · {payment_label(c.method, c.amount_cents, c.currency, account)}"
            if note:
                comment += f" · {note}"
            # Снимок курса к базовой валюте — как у orders/payments: общий итог
            # по прошлым выплатам не должен «плыть» за сегодняшним курсом.
            # ВНИМАНИЕ на семантику: `c.rate` — «единиц валюты за 1 базовую»
            # (сум за доллар), а `fx_rate_to_base` умножают на сумму
            # (`convert_to_base_at`), то есть это «базовой за 1 единицу».
            # Записать сюда курс как есть значит завысить итог в 10^8 раз.
            # Курс строки есть только при пересчёте; выплата сумами по сумовому
            # долгу его не несёт, а снимок ей всё равно нужен — берём курс ЦБ.
            fx = (
                _fx_to_base(c.rate if c.rate is not None else cbu.get(c.currency))
                if c.currency != base else None
            )
            pid = await order_payments._insert_id(
                txn,
                "INSERT INTO supplier_payments (counterparty_id, supplier_name, amount_cents, "
                "currency, comment, invoice_id, fx_rate_to_base, paid_at, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                supplier_id, supplier_name, c.amount_cents, c.currency, comment[:500],
                int(invoice["id"]) if invoice is not None else None, fx, paid_at, now,
            )
            await txn.execute(
                "INSERT INTO supplier_payment_parts (payment_id, method, account_id, "
                "debt_currency, debt_amount_cents, rate, rate_source, cbu_rate, created_by, "
                "created_by_name, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                pid, c.method, c.account_id, debt_currency, c.order_amount_cents,
                order_payments.fmt_rate(c.rate), c.rate_source,
                order_payments.fmt_rate(c.cbu_rate), actor.user_id, actor.name, now,
            )
            payments_out.append({
                "id": pid, "method": c.method, "currency": c.currency,
                "amount": float(money.from_cents(c.amount_cents)),
                "amount_cents": c.amount_cents,
                "debt_amount_cents": c.order_amount_cents,
                "account_id": c.account_id,
                "account_label": pay_accounts.source_label(c.method, account),
                "rate": order_payments.fmt_rate(c.rate), "rate_source": c.rate_source,
            })
        result = {
            "ok": True,
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "invoice_id": int(invoice["id"]) if invoice is not None else None,
            "debt_currency": debt_currency,
            "total_cents": total_debt_cents,
            "total": float(money.from_cents(total_debt_cents)),
            "payments": payments_out,
            "paid_at": paid_at[:16],
        }
        await idem_store_in(txn, idem_key, result)

    await _audit(actor, "supplier_payment_recorded",
                 recorded_text(supplier_name, invoice, debt_currency, calcs, accounts, note))
    return result


def recorded_text(supplier_name: str, invoice: dict | None, debt_currency: str,
                  calcs: list[order_payments.PartCalc], accounts: dict[int, dict] | None,
                  note: str = "") -> str:
    bits = []
    for c in calcs:
        account = (accounts or {}).get(c.account_id) if c.account_id is not None else None
        t = payment_label(c.method, c.amount_cents, c.currency, account)
        if c.rate_source != "same":
            t += (
                f" по курсу {order_payments.fmt_rate(c.rate or c.order_rate)} ({c.rate_source})"
                f" = {order_payments.fmt_cents(c.order_amount_cents, debt_currency)}"
            )
        bits.append(t)
    head = f"Выплата поставщику {supplier_name}"
    if invoice is not None:
        head += f" по приходу {invoice.get('invoice_number') or invoice['id']}"
    text = f"{head}: " + "; ".join(bits)
    if note:
        text += f"; примечание: {note}"
    return text


async def _audit(actor: Actor, action: str, details: str) -> None:
    from services import database

    try:
        await asyncio.to_thread(database.add_audit_log, actor.user_id, actor.name, actor.role,
                                action, details[:2000])
    except Exception:
        logger.exception("supplier_debts: аудит %s не записан", action)


# ─── Сводка экрана ───────────────────────────────────────────────────────────


async def overview(supplier_id: int | None = None, *, payments_limit: int = 50) -> dict:
    """Всё, что рисует экран «Деньги → Поставщикам», одним ответом."""
    from utils.helpers import local_now

    today = local_now().date().isoformat()
    led = await ledger(supplier_id)
    items = [as_receivable(d) for d in led.debts if d.remaining_cents > 0]
    payments = [payment_view(p) for p in led.payments]
    payments.sort(key=lambda p: (p["paid_at"], p["id"]), reverse=True)
    advances: dict[str, int] = {}
    for (_sup, cur), cents in led.advances.items():
        advances[cur] = advances.get(cur, 0) + cents
    return {
        "ok": True,
        "today": today,
        "base_currency": _base(),
        # Валюты формы — те же, что разрешены платежам: выплата поставщику
        # ходит теми же деньгами, что и поступление от клиента.
        "currencies": [_base()] + [c for c in _allowed_currencies() if c != _base()],
        "total": receivables.money_block(items),
        "aging": receivables.aging(items, local_now().date()),
        "suppliers": by_supplier(led, today),
        "debts": [debt_view(d, today) for d in led.debts if d.remaining_cents > 0],
        "unpriced": led.unpriced,
        "advances": [
            {"currency": cur, "total": float(money.from_cents(cents))}
            for cur, cents in sorted(advances.items()) if cents > 0
        ],
        "payments": payments[:payments_limit],
        "payments_total": len(payments),
    }


def overdue_count(led: Ledger, today: str) -> int:
    """Сколько приходов просрочено. Чистая функция — считает очередь «Сегодня».

    Именно счётчик, а не сумма: в очереди стоят числа дел, а суммы в разных
    валютах одним числом не показать (`money_block` и существует ради этого).
    """
    return sum(1 for d in led.debts
               if d.remaining_cents > 0 and d.due_date and d.due_date < today)


async def overdue_now() -> int:
    """Просроченные выплаты поставщикам на сегодня — для `work_queue`."""
    from utils.helpers import local_now

    return overdue_count(await ledger(), local_now().date().isoformat())
