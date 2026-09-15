"""
«Куда поступили деньги»: карты и расчётные счета, на которые клиент платит.

Требование владельца: выбрав в оплате «карта» или «на счёт», менеджер
указывает не только способ, но и ЧЬЯ это карта или ЧЕЙ счёт — последние цифры
карты и владельца, фирму и номер счёта. Руководитель сверяет банк именно по
этой строке («на карту •••• 1234 (Фаридун М.)»), и без неё «пришли ли деньги»
приходилось выяснять звонком.

Модель:

* справочник — та же таблица `acc_accounts`, что у бухгалтерии (вид card/bank,
  владелец, банк, последние 4 цифры карты). Отдельного «справочника получателей»
  нет: когда учёт включат, поступления уже указывают на свои счета. Справочник
  работает и при ВЫКЛЮЧЕННОЙ бухгалтерии — форма оплаты от выключателя не
  зависит;
* реквизиты, которых в `acc_accounts` нет (номер расчётного счёта, ИНН, МФО), —
  sidecar `acc_account_details` (схема — `services/order_payments_schema.py`);
* **номер карты целиком не хранится нигде** — только последние 4 цифры. Полный
  номер в форме отвергается текстом, а не обрезается молча: человек должен
  понимать, что мы его не сохранили. Расчётный счёт (20 цифр) хранится целиком:
  это реквизит для перевода, а не платёжное средство;
* ссылка «строка оплаты → счёт» — `payment_part_accounts` (заказы) и
  `machine_receipt_accounts` (рассрочка). Архивный счёт в выбор не попадает,
  но на старых платежах показывается как был.

Кто что может: завести карту/счёт — менеджер и руководство (новую карту
находят ровно в тот момент, когда клиент на неё заплатил); изменить и убрать в
архив — руководство, менеджер — только пока руководителя в системе нет
(`machine_deal_requests.decision_rights`, как удаление поступлений). Права
проверяет ручка, сервис получает готовый режим.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from services import adb_core

logger = logging.getLogger(__name__)

KINDS: dict[str, str] = {"card": "Карта", "bank": "Расчётный счёт"}
# Способ оплаты → вид записи справочника. Совпадают по имени намеренно.
METHOD_KIND: dict[str, str] = {"card": "card", "bank": "bank"}
ROLES_SEE = ("admin", "boss", "manager", "bookkeeper")
ROLES_ADD = ("admin", "boss", "manager")

ACCOUNT_NUMBER_LEN = 20
HOLDER_MAX = 80
BANK_MAX = 60
# Код валюты — 6–8-я цифры расчётного счёта в Узбекистане (ISO 4217 числом).
_ACCOUNT_CURRENCY_CODES = {"000": "UZS", "840": "USD", "978": "EUR", "643": "RUB"}

# Колонки справочника под префиксом `acc_` — для JOIN'ов в чужих выборках
# (строки разбивки, поступления по рассрочке): одна форма на все места показа.
ACCOUNT_COLUMNS_SQL = (
    "a.id AS acc_id, a.name AS acc_name, a.kind AS acc_kind, a.currency AS acc_currency, "
    "a.bank AS acc_bank, a.card_last4 AS acc_card_last4, a.holder AS acc_holder, "
    "a.archived_at AS acc_archived_at, ad.account_number AS acc_account_number"
)


def account_join_sql(link_alias: str) -> str:
    """LEFT JOIN справочника по колонке `account_id` ссылки `link_alias`."""
    return (
        f"LEFT JOIN acc_accounts a ON a.id = {link_alias}.account_id "
        "LEFT JOIN acc_account_details ad ON ad.account_id = a.id "
    )


class AccountError(Exception):
    """Ошибка формы/правил — текст показывается человеку как есть."""

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


# ─── Чистые функции (тесты — tests/test_pay_accounts.py) ─────────────────────


def _digits(raw: Any) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _clean(raw: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip()[:limit]


def norm_holder(raw: Any) -> str:
    """Для сравнения тёзок: регистр, пробелы, ё=е, без точек («Фаридун М.» =
    «фаридун м»)."""
    text = _clean(raw, HOLDER_MAX).lower().replace("ё", "е").replace(".", "")
    return re.sub(r"\s+", " ", text).strip()


def account_currency_from_number(number: str) -> str | None:
    """Валюта по коду в номере счёта (6–8-я цифры): 000 — сумы, 840 — доллары."""
    if len(number) != ACCOUNT_NUMBER_LEN:
        return None
    return _ACCOUNT_CURRENCY_CODES.get(number[5:8])


def validate(kind: str, data: dict, *, allowed_currencies: list[str], default_currency: str) -> dict:
    """Форма карты/счёта → нормализованные поля. Бросает `AccountError`."""
    if kind not in KINDS:
        raise AccountError("Выберите: карта или расчётный счёт")
    holder = _clean(data.get("holder"), HOLDER_MAX)
    bank = _clean(data.get("bank"), BANK_MAX)
    note = _clean(data.get("note"), 200)
    currency = str(data.get("currency") or "").strip().upper() or default_currency
    fields: dict[str, Any] = {
        "kind": kind, "holder": holder, "bank": bank, "note": note,
        "card_last4": None, "account_number": None, "company_tin": None, "mfo": None,
    }
    if kind == "card":
        if not holder:
            raise AccountError("Укажите владельца карты — например, «Фаридун М.»", code="holder")
        raw = str(data.get("card_last4") or "")
        last4 = _digits(raw)
        if len(last4) > 4:
            # Полный номер НЕ обрезаем молча: человек должен знать, что мы его
            # не храним и что ввести нужно только хвост.
            raise AccountError(
                "Нужны только последние 4 цифры карты — полный номер карты не храним", code="last4"
            )
        if len(last4) != 4 or (raw.strip() and not re.fullmatch(r"[\d\s•*.·-]*", raw.strip())):
            raise AccountError("Последние цифры карты — ровно 4 цифры", code="last4")
        fields["card_last4"] = last4
    else:
        if not holder:
            raise AccountError("Укажите фирму или владельца счёта — например, «ООО Farid Impeks»",
                               code="holder")
        raw = str(data.get("account_number") or "")
        number = _digits(raw)
        if len(number) != ACCOUNT_NUMBER_LEN or re.search(r"[^\d\s-]", raw.strip()):
            raise AccountError(f"Номер расчётного счёта — {ACCOUNT_NUMBER_LEN} цифр", code="account_number")
        fields["account_number"] = number
        tin = _digits(data.get("company_tin"))
        if data.get("company_tin") not in (None, "") and len(tin) not in (9, 14):
            raise AccountError("ИНН — 9 цифр (ПИНФЛ — 14)", code="company_tin")
        mfo = _digits(data.get("mfo"))
        if data.get("mfo") not in (None, "") and len(mfo) != 5:
            raise AccountError("МФО банка — 5 цифр", code="mfo")
        fields["company_tin"] = tin or None
        fields["mfo"] = mfo or None
        # Валюту счёта задаёт сам номер: сумовой счёт долларовым не станет.
        by_number = account_currency_from_number(number)
        if by_number and by_number in allowed_currencies:
            currency = by_number
    if currency not in allowed_currencies:
        raise AccountError(f"Валюта {currency} не поддерживается", code="currency")
    fields["currency"] = currency
    fields["name"] = display_name(fields)
    return fields


def _tail(number: str | None) -> str:
    return (number or "")[-4:]


def display_name(a: dict) -> str:
    """Название записи в `acc_accounts.name` (его видит и бухгалтерия)."""
    if a.get("kind") == "card":
        name = f"Карта •••• {a.get('card_last4') or '????'}"
        return f"{name} · {a['holder']}" if a.get("holder") else name
    head = a.get("holder") or a.get("bank") or "Счёт"
    tail = _tail(a.get("account_number"))
    return f"{head} · …{tail}" if tail else head


def _account_body(kind: str | None, a: dict) -> str:
    """Сама запись без предлога: «•••• 1234 (Фаридун М.)» / «ООО … (…6789)».

    Один текст на оба направления денег: «на карту …» у поступления и
    «с карты …» у выплаты поставщику обязаны называть карту одинаково —
    иначе один и тот же счёт в двух лентах читается как два разных.
    """
    holder = (a.get("holder") or "").strip()
    if kind == "card":
        if a.get("card_last4"):
            return f"•••• {a['card_last4']}" + (f" ({holder})" if holder else "")
        return f"«{a.get('name') or holder or '—'}»"
    tail = _tail(a.get("account_number"))
    who = holder or (a.get("bank") or "") or (a.get("name") or "—")
    return who + (f" (…{tail})" if tail else "")


def destination_label(kind: str | None, a: dict | None) -> str | None:
    """«на карту •••• 1234 (Фаридун М.)» / «на счёт ООО Farid Impeks (…6789)».

    `a` — поля записи без префикса (kind, name, holder, card_last4, bank,
    account_number). Записи бухгалтерии без реквизитов подписываются названием.
    """
    if not a:
        return None
    kind = a.get("kind") or kind
    return ("на карту " if kind == "card" else "на счёт ") + _account_body(kind, a)


def source_label(kind: str | None, a: dict | None) -> str | None:
    """«с карты •••• 1234 (Фаридун М.)» / «со счёта ООО Farid Impeks (…6789)».

    Деньги, ушедшие поставщику: та же запись справочника, другой предлог.
    Отдельная функция, а не флаг у `destination_label`, — чтобы вызывающий не
    мог случайно подписать выплату как поступление.
    """
    if not a:
        return None
    kind = a.get("kind") or kind
    return ("с карты " if kind == "card" else "со счёта ") + _account_body(kind, a)


def from_prefixed(row: dict) -> dict | None:
    """Строка выборки с `ACCOUNT_COLUMNS_SQL` → поля записи (None — ссылки нет)."""
    if not row or row.get("acc_id") is None:
        return None
    return {
        "id": int(row["acc_id"]),
        "kind": row.get("acc_kind"),
        "name": row.get("acc_name") or "",
        "currency": (row.get("acc_currency") or "").upper(),
        "bank": row.get("acc_bank") or "",
        "card_last4": row.get("acc_card_last4") or "",
        "holder": row.get("acc_holder") or "",
        "account_number": row.get("acc_account_number") or "",
        "archived": bool(row.get("acc_archived_at")),
    }


def view(a: dict) -> dict:
    """Запись для экрана: подписи считает сервер, фронт их только рисует."""
    kind = a.get("kind")
    number = a.get("account_number") or ""
    if kind == "card":
        title = f"•••• {a.get('card_last4')}" if a.get("card_last4") else (a.get("name") or "Карта")
        if a.get("holder"):
            title += f" · {a['holder']}"
        sub_bits = [a.get("bank") or "", a.get("currency") or ""]
    else:
        title = (a.get("holder") or a.get("name") or "Счёт") + (f" · …{_tail(number)}" if number else "")
        sub_bits = [a.get("bank") or "", f"МФО {a['mfo']}" if a.get("mfo") else "",
                    f"ИНН {a['company_tin']}" if a.get("company_tin") else "", a.get("currency") or ""]
    return {
        "id": int(a["id"]),
        "kind": kind,
        "kind_label": KINDS.get(kind or "", kind or ""),
        "name": a.get("name") or "",
        "holder": a.get("holder") or "",
        "bank": a.get("bank") or "",
        "card_last4": a.get("card_last4") or "",
        "account_number": number,
        "account_tail": _tail(number),
        "company_tin": a.get("company_tin") or "",
        "mfo": a.get("mfo") or "",
        "currency": (a.get("currency") or "").upper(),
        "note": a.get("note") or "",
        "archived": bool(a.get("archived") if "archived" in a else a.get("archived_at")),
        "title": title,
        "sub": " · ".join(b for b in sub_bits if b),
        "label": destination_label(kind, a),
    }


# ─── Чтение ──────────────────────────────────────────────────────────────────


_SELECT = (
    "SELECT a.*, ad.account_number, ad.company_tin, ad.mfo FROM acc_accounts a "
    "LEFT JOIN acc_account_details ad ON ad.account_id = a.id "
)


def _ph(n: int, start: int = 1) -> str:
    return ", ".join(f"${i}" for i in range(start, start + n))


async def list_accounts(*, include_archived: bool = False, kinds: tuple[str, ...] = tuple(KINDS),
                        conn: Any = None) -> list[dict]:
    db = conn if conn is not None else adb_core
    sql = _SELECT + f"WHERE a.kind IN ({_ph(len(kinds))})"
    if not include_archived:
        sql += " AND a.archived_at IS NULL"
    sql += f" ORDER BY a.archived_at IS NOT NULL, a.kind, {adb_core.order_by_name('a.name')}, a.id"
    return [view(dict(r)) for r in await db.fetch(sql, *kinds)]


async def get_account(account_id: int, conn: Any = None) -> dict | None:
    db = conn if conn is not None else adb_core
    row = await db.fetchrow(_SELECT + "WHERE a.id = $1", int(account_id))
    return view(dict(row)) if row else None


async def load_accounts(ids: list[int], conn: Any = None) -> dict[int, dict]:
    db = conn if conn is not None else adb_core
    uniq = sorted({int(i) for i in ids or [] if i})
    out: dict[int, dict] = {}
    for start in range(0, len(uniq), 5000):
        chunk = uniq[start:start + 5000]
        for r in await db.fetch(_SELECT + f"WHERE a.id IN ({_ph(len(chunk))})", *chunk):
            out[int(r["id"])] = view(dict(r))
    return out


async def last_used(user_id: int, conn: Any = None) -> dict[str, int | None]:
    """Последняя карта и последний счёт, которые выбирал человек, — выводятся
    из его же платежей и поступлений (отдельной «настройки» нет: она расходилась
    бы с тем, что он на самом деле выбирал). Архивные не предлагаются."""
    db = conn if conn is not None else adb_core
    rows = [dict(r) for r in await db.fetch(
        "SELECT a.kind, ppa.account_id, pp.created_at AS at FROM payment_part_accounts ppa "
        "JOIN payment_parts pp ON pp.id = ppa.part_id JOIN acc_accounts a ON a.id = ppa.account_id "
        "WHERE pp.created_by = $1 AND a.archived_at IS NULL ORDER BY pp.created_at DESC, pp.id DESC LIMIT 40",
        int(user_id),
    )]
    rows += [dict(r) for r in await db.fetch(
        "SELECT a.kind, mra.account_id, r.created_at AS at FROM machine_receipt_accounts mra "
        "JOIN machine_payment_receipts r ON r.id = mra.receipt_id JOIN acc_accounts a ON a.id = mra.account_id "
        "WHERE r.received_by = $1 AND a.archived_at IS NULL ORDER BY r.created_at DESC, r.id DESC LIMIT 40",
        int(user_id),
    )]
    rows += [dict(r) for r in await db.fetch(
        "SELECT a.kind, spp.account_id, spp.created_at AS at FROM supplier_payment_parts spp "
        "JOIN acc_accounts a ON a.id = spp.account_id "
        "WHERE spp.created_by = $1 AND a.archived_at IS NULL "
        "ORDER BY spp.created_at DESC, spp.payment_id DESC LIMIT 40",
        int(user_id),
    )]
    rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    out: dict[str, int | None] = {k: None for k in KINDS}
    for r in rows:
        if r["kind"] in out and out[r["kind"]] is None:
            out[r["kind"]] = int(r["account_id"])
    return out


async def usage_count(account_id: int, conn: Any = None) -> int:
    db = conn if conn is not None else adb_core
    n = await db.fetchval(
        "SELECT (SELECT COUNT(*) FROM payment_part_accounts WHERE account_id = $1) + "
        "(SELECT COUNT(*) FROM machine_receipt_accounts WHERE account_id = $1) + "
        # Выплаты поставщикам — те же деньги на том же счёте: без них хвост
        # карты, с которой платили полгода, «переехал» бы на другую карту.
        "(SELECT COUNT(*) FROM supplier_payment_parts WHERE account_id = $1) + "
        "(SELECT COUNT(*) FROM acc_entries WHERE account_id = $1)",
        int(account_id),
    )
    return int(n or 0)


async def check_for_method(account_id: Any, method: str, *, row: int | None = None,
                           conn: Any = None) -> dict:
    """Счёт, выбранный для строки со способом `method`: существует, того же вида,
    не в архиве. → запись; иначе `AccountError`."""
    prefix = f"Строка {row}: " if row else ""
    try:
        acc_id = int(account_id)
    except (TypeError, ValueError) as e:
        raise AccountError(f"{prefix}{required_text(method)}", code="account_required") from e
    acc = await get_account(acc_id, conn=conn)
    if acc is None:
        raise AccountError(f"{prefix}карта или счёт не найдены — выберите из списка",
                           code="account_invalid")
    want = METHOD_KIND.get(method)
    if acc["kind"] != want:
        raise AccountError(
            f"{prefix}выбран{'а карта' if acc['kind'] == 'card' else ' счёт'}, а способ — "
            f"{'карта' if method == 'card' else 'перечисление на счёт'}: выберите "
            f"{'карту' if method == 'card' else 'счёт'}",
            code="account_invalid",
        )
    if acc["archived"]:
        raise AccountError(f"{prefix}«{acc['title']}» убрана в архив — выберите другую",
                           code="account_archived")
    return acc


def required_text(method: str) -> str:
    if method == "card":
        return "укажите, на какую карту пришли деньги (последние 4 цифры и владелец)"
    return "укажите, на какой счёт пришли деньги (фирма и номер счёта)"


# ─── Запись ──────────────────────────────────────────────────────────────────


def _now() -> str:
    from services.database import now_str

    return now_str()


async def _insert_id(txn: Any, sql: str, *args: Any) -> int:
    if adb_core._use_postgres():
        return int(await txn.fetchval(sql + " RETURNING id", *args))
    await txn.execute(sql, *args)
    return int(await txn.fetchval("SELECT last_insert_rowid()"))


def _currencies() -> tuple[list[str], str]:
    from config import ALLOWED_CURRENCIES, BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    return [base] + [c.upper() for c in ALLOWED_CURRENCIES if c.upper() != base], base


async def _find_duplicate(txn: Any, fields: dict, exclude_id: int | None = None) -> dict | None:
    """Та же карта (хвост + владелец) или тот же расчётный счёт (номер)."""
    if fields["kind"] == "card":
        rows = await txn.fetch(
            _SELECT + "WHERE a.kind = 'card' AND a.card_last4 = $1", fields["card_last4"]
        )
        want = norm_holder(fields["holder"])
        for r in rows:
            if int(r["id"]) != (exclude_id or 0) and norm_holder(r["holder"]) == want:
                return view(dict(r))
        return None
    row = await txn.fetchrow(
        _SELECT + "WHERE ad.account_number = $1 AND a.id <> $2", fields["account_number"], int(exclude_id or 0)
    )
    return view(dict(row)) if row else None


async def _lock_key(txn: Any, fields: dict) -> None:
    # Два одновременных «Новая карта» с одними данными иначе оба не нашли бы
    # дубля. У счёта есть UNIQUE по номеру, у карты — только этот замок.
    if adb_core._use_postgres():
        key = fields["card_last4"] if fields["kind"] == "card" else fields["account_number"]
        await txn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"pay_account:{fields['kind']}:{key}")


async def create_account(actor: Actor, data: dict) -> dict:
    """Завести карту/счёт. Тот же (хвост+владелец / номер счёта) не заводится
    второй раз: ответ возвращает уже заведённый (`existed`). Архивный тёзка —
    отказ с текстом: вернуть его может руководитель."""
    from services.roles import role_allowed

    if not role_allowed(actor.role, ROLES_ADD):
        raise AccountError("Карты и счета заводят менеджер и руководитель", status=403)
    allowed, base = _currencies()
    fields = validate(str(data.get("kind") or ""), data, allowed_currencies=allowed,
                      default_currency=str(data.get("currency") or base).upper())
    now = _now()
    existed = None
    async with adb_core.transaction() as txn:
        await _lock_key(txn, fields)
        existed = await _find_duplicate(txn, fields)
        if existed is None:
            account_id = await _insert_id(
                txn,
                "INSERT INTO acc_accounts (name, kind, currency, bank, card_last4, holder, note, "
                "created_by, created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)",
                fields["name"], fields["kind"], fields["currency"], fields["bank"] or None,
                fields["card_last4"], fields["holder"] or None, fields["note"] or None,
                actor.user_id, now,
            )
            if fields["kind"] == "bank":
                await txn.execute(
                    "INSERT INTO acc_account_details (account_id, account_number, company_tin, mfo, "
                    "created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $5)",
                    account_id, fields["account_number"], fields["company_tin"], fields["mfo"], now,
                )
    if existed is not None:
        if existed["archived"]:
            raise AccountError(
                f"«{existed['title']}» уже есть, но убрана в архив — вернуть её может руководитель "
                "(Настройки → Карты и счета)", status=409, code="archived_duplicate",
            )
        return {"ok": True, "existed": True, "account": existed}
    account = await get_account(account_id)
    assert account is not None
    await _audit(actor, "pay_account_created", f"{account['kind_label']} #{account_id}: {account['label']}")
    return {"ok": True, "existed": False, "account": account}


async def update_account(actor: Actor, account_id: Any, data: dict, *, mode: str = "boss") -> dict:
    """Поправить запись. Вид не меняется; хвост карты и номер счёта — только
    пока по записи нет денег: иначе старые платежи молча «переехали» бы на
    другую карту, а руководитель сверял банк по прежней."""
    try:
        acc_id = int(account_id)
    except (TypeError, ValueError) as e:
        raise AccountError("Карта или счёт не найдены", status=404) from e
    current = await get_account(acc_id)
    if current is None or current["kind"] not in KINDS:
        raise AccountError("Карта или счёт не найдены", status=404)
    allowed, _base = _currencies()
    merged = {**{k: current.get(k) for k in ("holder", "bank", "note", "card_last4", "account_number",
                                             "company_tin", "mfo", "currency")}, **(data or {})}
    fields = validate(current["kind"], merged, allowed_currencies=allowed, default_currency=current["currency"])
    now = _now()
    async with adb_core.transaction() as txn:
        await _lock_key(txn, fields)
        used = await usage_count(acc_id, conn=txn)
        key_changed = (
            (fields["kind"] == "card" and fields["card_last4"] != current["card_last4"])
            or (fields["kind"] == "bank" and fields["account_number"] != current["account_number"])
        )
        if used and key_changed:
            raise AccountError(
                "По этой записи уже есть платежи — номер не меняют: заведите новую и уберите эту в архив",
                status=409, code="in_use",
            )
        if used and fields["currency"] != current["currency"]:
            raise AccountError("По этой записи уже есть платежи — валюту не меняют", status=409, code="in_use")
        dup = await _find_duplicate(txn, fields, exclude_id=acc_id)
        if dup is not None:
            raise AccountError(f"Такая запись уже есть: «{dup['title']}»", status=409, code="duplicate")
        await txn.execute(
            "UPDATE acc_accounts SET name = $1, currency = $2, bank = $3, card_last4 = $4, holder = $5, "
            "note = $6, updated_at = $7 WHERE id = $8",
            fields["name"], fields["currency"], fields["bank"] or None, fields["card_last4"],
            fields["holder"] or None, fields["note"] or None, now, acc_id,
        )
        if fields["kind"] == "bank":
            rc = await txn.execute(
                "UPDATE acc_account_details SET account_number = $1, company_tin = $2, mfo = $3, "
                "updated_at = $4 WHERE account_id = $5",
                fields["account_number"], fields["company_tin"], fields["mfo"], now, acc_id,
            )
            if not rc:
                await txn.execute(
                    "INSERT INTO acc_account_details (account_id, account_number, company_tin, mfo, "
                    "created_at, updated_at) VALUES ($1, $2, $3, $4, $5, $5)",
                    acc_id, fields["account_number"], fields["company_tin"], fields["mfo"], now,
                )
    account = await get_account(acc_id)
    assert account is not None
    await _audit(actor, "pay_account_updated",
                 f"#{acc_id}: было «{current['label']}», стало «{account['label']}»" + _mode_note(mode))
    return {"ok": True, "account": account}


async def set_archived(actor: Actor, account_id: Any, archived: bool, *, mode: str = "boss") -> dict:
    try:
        acc_id = int(account_id)
    except (TypeError, ValueError) as e:
        raise AccountError("Карта или счёт не найдены", status=404) from e
    current = await get_account(acc_id)
    if current is None or current["kind"] not in KINDS:
        raise AccountError("Карта или счёт не найдены", status=404)
    if current["archived"] == bool(archived):
        return {"ok": True, "account": current, "changed": False}
    now = _now()
    await adb_core.execute(
        "UPDATE acc_accounts SET archived_at = $1, updated_at = $2 WHERE id = $3",
        now if archived else None, now, acc_id,
    )
    account = await get_account(acc_id)
    await _audit(actor, "pay_account_archived",
                 f"«{current['label']}» {'в архив' if archived else 'из архива'}" + _mode_note(mode))
    return {"ok": True, "account": account, "changed": True}


def _mode_note(mode: str) -> str:
    return " · менеджером — руководителя в системе нет" if mode == "no_boss" else ""


async def _audit(actor: Actor, action: str, details: str) -> None:
    from services import database

    try:
        await asyncio.to_thread(database.add_audit_log, actor.user_id, actor.name, actor.role,
                                action, details[:2000])
    except Exception:
        logger.exception("pay_accounts: аудит %s не записан", action)
