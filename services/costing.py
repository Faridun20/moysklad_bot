"""
Себестоимость товара: партии прихода, фиксация при отгрузке, курсовая разница.

Всё новое поведение — за выключателем `app_settings.accounting_enabled` (тот же
ключ, что у учёта денег; по умолчанию выключен). Учёт начинают ПОСЛЕ переноса
истории склада: пока выключатель выключен, ни одна партия и ни одна фиксация не
пишется, накладные и приёмка работают как раньше.

Решения, определяющие модуль:

* **Метод — FIFO по партиям, а не средняя.** Владелец спрашивает прибыль ПО
  КОНТЕЙНЕРУ («закуплено → продано → осталось») и курсовую разницу «для
  партии». Средняя себестоимость смешивает контейнеры в одно число, и ответить
  «сколько продано из контейнера X» после неё нечем. FIFO отвечает прямо: из
  какой партии ушла каждая единица. Для телефона разницы нет — экран показывает
  «себестоимость продажи» и среднюю по остатку (средневзвешенную по
  непроданным партиям), то есть привычное одно число.
* **Партия = строка приходной накладной** (`cost_batches`), единица учёта —
  накладная, а не контейнер: так ручной приход и контейнер считаются одним
  кодом. Контейнер лишь дописывает к своим партиям цену, валюту и курс
  прибытия. Партия без цены законна — место в очереди FIFO она занимает сразу,
  цену впишут позже, и продажи из неё тогда же получат себестоимость.
* **Остаток склада — это ПОСЛЕДНИЕ партии.** Товар, лежавший до включения
  учёта, партии не имеет. Поэтому при отгрузке сначала уходит «непокрытая»
  часть остатка (старый товар, себестоимость ручная из `product_prices` или
  неизвестна), и только потом партии от старых к новым. То же правило читает
  оценку остатка. Оно же лечит расхождения в обе стороны: партии, которых
  физически уже нет (продали при выключенном учёте), просто не покрывают
  остаток и в оценку не входят.
* **Себестоимость отгрузки ЗАМОРАЖИВАЕТСЯ** (`sale_costs`): новая закупка по
  другой цене прибыль прошлых продаж не меняет. Исключение одно и намеренное —
  исправили цену ТОЙ ЖЕ партии (опечатка, цену вписали позже): это исправление
  данных, и продажи из неё пересчитываются.
* **Отмена не требует обратного хода.** Отменённая накладная (и расход, и
  приход) просто выпадает из всех выборок по `invoices.status`: отменили
  отгрузку — её фиксации перестали расходовать партию.
* **Курсы — Decimal строкой.** REAL на Postgres — float4, а курс сума к доллару
  (0.000079…) из него потерял бы знаки.

Курсовая разница (условная) — только по продажам в НЕбазовой валюте из партий с
известным курсом прибытия: «эта выручка в сумах по курсу дня прибытия товара
стоила бы X долларов, по курсу дня продажи — Y». Y − X < 0 — потеря на курсе,
а не на цене. По остатку, закупленному не в базовой валюте, — то же сравнение
«по курсу прибытия» и «по курсу сегодня».
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from typing import Any

from services import adb_core
from services import database as _db

logger = logging.getLogger(__name__)

SWITCH_KEY = "accounting_enabled"

# Себестоимость и прибыль видит только руководство. Бухгалтерию добавить сюда,
# когда она появится как роль с этим правом: сейчас `bookkeeper` сливается с
# менеджером, а менеджеру закупочные цены не показываем.
COST_ROLES = ("admin", "boss")

# Валюты закупки, которые предлагает форма. Расширяется строкой: курс любой
# валюты выражается «сум за единицу», и ЦБ отдаёт его тем же запросом.
PURCHASE_CURRENCIES = ("USD", "UZS", "CNY")
LOCAL_CURRENCY = "UZS"

_ONE = Decimal(1)
_RATE_CTX = Context(prec=20)


def can_see_cost(role: str | None) -> bool:
    return role in COST_ROLES


def base_currency() -> str:
    from config import BASE_CURRENCY

    return (BASE_CURRENCY or "USD").upper()


# ─── Выключатель ──────────────────────────────────────────────────────────────


def _truthy(raw: Any) -> bool:
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


async def is_enabled(conn: Any = None) -> bool:
    """Включён ли учёт себестоимости.

    Читаем `app_settings` напрямую, а не через `get_setting`: хук зовётся внутри
    транзакции накладной, и синхронный слой из неё — это второе соединение и
    поток ради одного значения. Внутри транзакции читаем ТЕМ ЖЕ соединением.
    """
    sql = "SELECT value FROM app_settings WHERE key = $1"
    if conn is not None:
        raw = await conn.fetchval(sql, SWITCH_KEY)
    else:
        raw = await adb_core.fetchval(sql, SWITCH_KEY)
    return raw is not None and _truthy(raw)


async def set_enabled(value: bool, user_id: int | None) -> None:
    await asyncio.to_thread(_db.set_setting, SWITCH_KEY, bool(value), user_id)


# ─── Числа ────────────────────────────────────────────────────────────────────


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        d = Decimal(text)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _cents(value: Decimal) -> int:
    return int(value.quantize(_ONE, rounding=ROUND_HALF_UP))


def _rstr(value: Decimal) -> str:
    """Курс строкой: 20 значащих цифр — с запасом для сума к доллару."""
    return format(_RATE_CTX.create_decimal(value).normalize(), "f")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _day(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


# ─── Курсы ────────────────────────────────────────────────────────────────────


async def _rate_asof(conn: Any, code: str, day: str) -> Decimal | None:
    """Курс к базовой из дневного архива на дату (ближайший ранний день)."""
    code = (code or "").upper()
    if not code:
        return None
    if code == base_currency():
        return _ONE
    raw = await conn.fetchval(
        "SELECT rate_to_base FROM currency_rate_daily "
        "WHERE currency_code = $1 AND rate_date <= $2 ORDER BY rate_date DESC LIMIT 1",
        code,
        day[:10],
    )
    rate = _dec(raw)
    return rate if rate is not None and rate > 0 else None


async def _rate_current(conn: Any, code: str) -> Decimal | None:
    code = (code or "").upper()
    if not code:
        return None
    if code == base_currency():
        return _ONE
    raw = await conn.fetchval(
        "SELECT rate_to_base FROM currency_rates WHERE currency_code = $1", code
    )
    rate = _dec(raw)
    return rate if rate is not None and rate > 0 else None


async def _rate_for_day(conn: Any, code: str, day: str) -> Decimal | None:
    """Курс операции: за прошлую дату — архив, за сегодня — текущий.

    Текущий курс первым для «сегодня», потому что его же берёт снимок курса
    заказа (`orders.fx_rate_to_base`) и его руководство правит руками: прибыль
    по накладной не должна считаться по другому курсу, чем сам заказ.
    """
    if day and day[:10] < _today():
        return await _rate_asof(conn, code, day) or await _rate_current(conn, code)
    return await _rate_current(conn, code) or await _rate_asof(conn, code, day or _today())


def rates_from_uzs(
    currency: str, uzs_per_usd: Decimal, uzs_per_unit: Decimal
) -> dict[str, Decimal]:
    """Курсы к базовой из курсов «сум за единицу».

    На площадке курс знают как «1 USD = 12 650 сум», поэтому форма спрашивает
    именно так, а курс к базовой выводится: rate(X) = сум_за_X / сум_за_базовую.
    Работает при базовой USD и при базовой UZS.
    """
    per: dict[str, Decimal] = {"USD": uzs_per_usd, LOCAL_CURRENCY: _ONE}
    # USD и сум уже заданы сами собой; «сум за единицу» нужен прочим валютам.
    per.setdefault(currency.upper(), uzs_per_unit)
    base = base_currency()
    if base not in per:
        raise ValueError(f"Базовая валюта {base} не выражается через сум")
    return {code: _RATE_CTX.divide(value, per[base]) for code, value in per.items()}


async def _fetch_cbu_uzs_per(code: str, day: str) -> Decimal | None:
    """Сум за 1 единицу валюты по ЦБ РУз на дату. Граница с сетью.

    Короткий таймаут: подсказка курса не должна держать карточку контейнера,
    если ЦБ не отвечает, — тогда предложим текущий курс или спросим руками.
    """
    from services import fx_rates

    url = f"{fx_rates.CBU_BASE}/{code.upper()}/{day[:10]}/"
    try:
        data = await asyncio.wait_for(fx_rates._cbu_get(url), timeout=4)
    except Exception as e:  # noqa: BLE001 — сеть ЦБ не повод ронять карточку
        logger.info("ЦБ: курс %s на %s не получен: %s", code, day, e)
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    rate = _dec(data[0].get("Rate"))
    nominal = _dec(data[0].get("Nominal")) or _ONE
    if rate is None or rate <= 0 or nominal <= 0:
        return None
    return rate / nominal


async def _uzs_per(conn: Any, code: str, day: str, *, current: bool) -> Decimal | None:
    """«Сум за 1 единицу» из наших курсов: rate(X) / rate(UZS), обе к базовой."""
    if current:
        x, uzs = await _rate_current(conn, code), await _rate_current(conn, LOCAL_CURRENCY)
    else:
        x, uzs = await _rate_asof(conn, code, day), await _rate_asof(conn, LOCAL_CURRENCY, day)
    if x is None or uzs is None or uzs <= 0:
        return None
    return _RATE_CTX.divide(x, uzs)


async def suggest_rates(day: str) -> dict:
    """Подсказка курса на дату прибытия: «сум за 1 USD» и за прочие валюты.

    Порядок источников: дневной архив ЦБ (его пишет `run_fx_sync`) → запрос в
    ЦБ на дату (ответ запоминаем в архив — следующая карточка в сеть не
    пойдёт) → текущий курс. Источник отдаём вместе с числом: «курс ЦБ на 14.09»
    и «текущий курс, на дату прибытия архива нет» — разные утверждения.
    """
    day = (day or _today())[:10]
    codes = [c for c in PURCHASE_CURRENCIES if c != LOCAL_CURRENCY]
    found: dict[str, Decimal] = {}
    sources: dict[str, str] = {}
    for code in codes:
        value = await _uzs_per(adb_core, code, day, current=False)
        if value is not None:
            found[code], sources[code] = value, "cbu"
    missing = [c for c in codes if c not in found]
    if missing:
        fetched = await asyncio.gather(*(_fetch_cbu_uzs_per(c, day) for c in missing))
        for code, value in zip(missing, fetched, strict=True):
            if value is not None:
                found[code], sources[code] = value, "cbu"
        await _remember_cbu(day, {c: v for c, v in zip(missing, fetched, strict=True) if v})
    for code in codes:
        if code not in found:
            value = await _uzs_per(adb_core, code, day, current=True)
            if value is not None:
                found[code], sources[code] = value, "current"
    per = {
        code: format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        for code, value in found.items()
    }
    return {"date": day, "source": sources.get("USD"), "sources": sources, "uzs_per": per}


async def _remember_cbu(day: str, uzs_per: dict[str, Decimal]) -> None:
    """Записать полученное от ЦБ в дневной архив (тот же, что пишет run_fx_sync)."""
    per_base: Decimal | None = _ONE if base_currency() == LOCAL_CURRENCY else uzs_per.get(base_currency())
    if per_base is None:
        per_base = await _uzs_per(adb_core, base_currency(), day, current=False)
    if per_base is None or per_base <= 0:
        return
    rows: dict[str, Decimal] = {}
    if base_currency() in uzs_per:
        # Курс сума к базовой пишем, только если его и спрашивали у ЦБ: из
        # архива он и так есть, а перезапись пересчётом дала бы дрейф знаков.
        rows[LOCAL_CURRENCY] = _RATE_CTX.divide(_ONE, per_base)
    for code, value in uzs_per.items():
        if code != base_currency():
            rows[code] = _RATE_CTX.divide(value, per_base)
    for code, rate in rows.items():
        await asyncio.to_thread(_db.set_currency_rate_daily, code, day, float(rate), "cbu")


async def _ref_rates(conn: Any, currencies: set[str], day: str) -> dict[str, str]:
    """Курсы на дату партии для всех валют, в которых её могут продать."""
    out: dict[str, str] = {}
    for code in sorted({c.upper() for c in currencies} | {base_currency(), LOCAL_CURRENCY, "USD"}):
        rate = await _rate_for_day(conn, code, day)
        if rate is not None:
            out[code] = _rstr(rate)
    return out


# ─── Хук склада: партии и фиксации ────────────────────────────────────────────


async def record_invoice_in(
    txn: Any,
    *,
    invoice_id: int,
    invoice_type: str,
    invoice_date: str,
    currency: str,
    positions: list[dict],
) -> None:
    """Точка входа из `warehouse.create_invoice_in` — той же транзакцией.

    Приход заводит партии, расход фиксирует себестоимость. Выключен учёт — не
    делает ничего. Ошибка здесь откатывает накладную вместе с ней: полу-учёт
    (накладная есть, фиксации нет) хуже отказа, потому что прибыль по такой
    продаже молча выпала бы из отчёта.
    """
    if not await is_enabled(txn):
        return
    cur = (currency or base_currency()).upper()
    if invoice_type == "incoming":
        await _record_batches(txn, invoice_id, invoice_date, cur, positions)
    elif invoice_type == "outgoing":
        await _record_sales(txn, invoice_id, invoice_date, cur, positions)


async def _record_batches(
    txn: Any, invoice_id: int, invoice_date: str, currency: str, positions: list[dict]
) -> None:
    rate = await _rate_for_day(txn, currency, invoice_date)
    refs = json.dumps(await _ref_rates(txn, {currency}, invoice_date))
    stamp = _db.now_str()
    for p in positions:
        qty = _dec(p["quantity"]) or Decimal(0)
        price = p.get("price_cents")
        total = (
            _cents(Decimal(int(price)) * qty * rate)
            if price is not None and rate is not None
            else None
        )
        await txn.execute(
            "INSERT INTO cost_batches (invoice_id, product_id, container_id, batch_date, "
            "quantity, unit_price_cents, currency, rate_to_base, total_cost_base_cents, "
            "ref_rates, created_at) VALUES ($1, $2, NULL, $3, $4, $5, $6, $7, $8, $9, $10)",
            int(invoice_id),
            int(p["product_id"]),
            invoice_date[:10],
            float(qty),
            None if price is None else int(price),
            currency,
            None if rate is None else _rstr(rate),
            total,
            refs,
            stamp,
        )


async def _batches_state(conn: Any, product_ids: list[int] | None = None) -> dict[int, dict]:
    """Партии по товарам с остатком и покрытием остатка склада.

    {product_id: {"batches": [...от старых к новым], "stock", "uncovered"}}.
    `remaining` — сколько в партии не продано (по действующим отгрузкам),
    `covered` — сколько из этого реально лежит на складе: остаток склада
    покрывается партиями от НОВЫХ к старым, всё, что не покрыто, — товар старше
    учёта (`uncovered`).

    `product_ids=None` — все товары, без `IN (...)`: каталог на тысячи позиций
    упёрся бы в лимит параметров SQLite.
    """
    where_b = where_s = where_st = ""
    args: list[Any] = []
    if product_ids is not None:
        if not product_ids:
            return {}
        args = [int(x) for x in sorted(set(product_ids))]
        ph = ", ".join(f"${i + 1}" for i in range(len(args)))
        where_b = f" AND b.product_id IN ({ph})"
        where_s = f" AND s.product_id IN ({ph})"
        where_st = f" WHERE product_id IN ({ph})"
    batches = await conn.fetch(
        "SELECT b.id, b.invoice_id, b.product_id, b.container_id, b.batch_date, b.quantity, "
        "       b.unit_price_cents, b.currency, b.rate_to_base, b.total_cost_base_cents, "
        "       b.ref_rates "
        "FROM cost_batches b JOIN invoices i ON i.id = b.invoice_id "
        f"WHERE i.status = 'confirmed'{where_b} "
        "ORDER BY b.product_id, b.batch_date, b.id",
        *args,
    )
    consumed_rows = await conn.fetch(
        "SELECT s.batch_id, SUM(s.quantity) AS qty "
        "FROM sale_costs s JOIN invoices i ON i.id = s.invoice_id "
        f"WHERE i.status = 'confirmed' AND s.batch_id IS NOT NULL{where_s} "
        "GROUP BY s.batch_id",
        *args,
    )
    stock_rows = await conn.fetch(
        f"SELECT product_id, SUM(quantity) AS qty FROM stock{where_st} GROUP BY product_id",
        *args,
    )
    consumed = {int(r["batch_id"]): _dec(r["qty"]) or Decimal(0) for r in consumed_rows}
    stock = {int(r["product_id"]): _dec(r["qty"]) or Decimal(0) for r in stock_rows}

    out: dict[int, dict] = {}
    for b in batches:
        pid = int(b["product_id"])
        entry = out.setdefault(pid, {"batches": [], "stock": stock.get(pid, Decimal(0))})
        qty = _dec(b["quantity"]) or Decimal(0)
        remaining = max(Decimal(0), qty - consumed.get(int(b["id"]), Decimal(0)))
        entry["batches"].append({**dict(b), "qty": qty, "remaining": remaining})
    for pid in (product_ids if product_ids is not None else stock.keys()):
        out.setdefault(int(pid), {"batches": [], "stock": stock.get(int(pid), Decimal(0))})
    for entry in out.values():
        _cover(entry, entry["stock"])
    return out


def _cover(entry: dict, stock: Decimal) -> None:
    left = max(Decimal(0), stock)
    for b in reversed(entry["batches"]):
        take = min(b["remaining"], left)
        b["covered"] = take
        left -= take
    entry["uncovered"] = left


def _part_cost(batch: dict, qty: Decimal) -> int | None:
    total = batch.get("total_cost_base_cents")
    if total is None or not batch["qty"]:
        return None
    return _cents(Decimal(int(total)) * qty / batch["qty"])


async def _manual_cost(conn: Any, product_id: int, qty: Decimal, day: str) -> int | None:
    """Ручная себестоимость (`product_prices`) в базовой валюте — для товара
    старше учёта. Карточку цены не ломаем: она остаётся источником, пока у товара
    нет партий."""
    row = await conn.fetchrow(
        "SELECT cost_price_cents, currency FROM product_prices WHERE ms_id = $1",
        str(product_id),
    )
    if not row or row.get("cost_price_cents") is None:
        return None
    rate = await _rate_for_day(conn, (row.get("currency") or base_currency()), day)
    if rate is None:
        return None
    return _cents(Decimal(int(row["cost_price_cents"])) * qty * rate)


async def _record_sales(
    txn: Any, invoice_id: int, invoice_date: str, currency: str, positions: list[dict]
) -> None:
    sale_rate = await _rate_for_day(txn, currency, invoice_date)
    sale_rate_s = None if sale_rate is None else _rstr(sale_rate)
    stamp = _db.now_str()
    product_ids = [int(p["product_id"]) for p in positions]
    # Состояние читаем ПОСЛЕ движения остатка (хук стоит за ним), поэтому
    # «остаток до отгрузки» = нынешний + отгружаемое.
    state = await _batches_state(txn, product_ids)

    for p in positions:
        pid = int(p["product_id"])
        need = _dec(p["quantity"]) or Decimal(0)
        price = int(p.get("price_cents") or 0)
        entry = state.get(pid) or {"batches": [], "stock": Decimal(0)}
        _cover(entry, entry["stock"] + need)
        parts: list[tuple[dict | None, Decimal]] = []
        # 1) Сначала товар старше учёта: он физически лежит на складе дольше.
        old = min(need, entry["uncovered"])
        if old > 0:
            parts.append((None, old))
            need -= old
        # 2) Потом партии от старых к новым.
        for b in entry["batches"]:
            if need <= 0:
                break
            take = min(need, b["covered"])
            if take > 0:
                parts.append((b, take))
                need -= take
        if need > 0:
            # Партий меньше, чем уходит товара (остаток и партии разошлись).
            # Не угадываем — фиксируем без себестоимости, отчёт это покажет.
            parts.append((None, need))

        for batch, qty in parts:
            if batch is None:
                cost = await _manual_cost(txn, pid, qty, invoice_date)
                source = "manual" if cost is not None else "unknown"
                ref = None
            else:
                cost = _part_cost(batch, qty)
                source = "batch"
                ref = _ref_for(batch, currency)
            await txn.execute(
                "INSERT INTO sale_costs (invoice_id, product_id, batch_id, quantity, "
                "cost_base_cents, cost_source, sale_price_cents, currency, sale_rate_to_base, "
                "ref_rate_to_base, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                int(invoice_id),
                pid,
                None if batch is None else int(batch["id"]),
                float(qty),
                cost,
                source,
                price,
                currency,
                sale_rate_s,
                ref,
                stamp,
            )


def _ref_for(batch: dict, currency: str) -> str | None:
    """Курс валюты продажи на дату прибытия партии (для курсовой разницы)."""
    try:
        refs = json.loads(batch.get("ref_rates") or "{}")
    except (TypeError, ValueError):
        return None
    value = refs.get((currency or "").upper())
    return str(value) if value is not None else None


# ─── Контейнер: цены закупки и курс прибытия ─────────────────────────────────


async def _lock_products(txn: Any, product_ids: list[int]) -> None:
    """Сериализовать пересчёт цен партии с отгрузками тех же товаров.

    Отгрузка держит `FOR UPDATE` на строках `stock`; берём те же строки в том
    же порядке — иначе продажа, прочитавшая партию «без цены», закоммитила бы
    фиксацию уже ПОСЛЕ того, как мы пересчитали продажи этой партии.
    """
    if not _db.USE_POSTGRES or not product_ids:
        return
    ids = sorted(set(int(x) for x in product_ids))
    ph = ", ".join(f"${i + 1}" for i in range(len(ids)))
    await txn.fetch(
        f"SELECT product_id FROM stock WHERE product_id IN ({ph}) "
        "ORDER BY product_id, warehouse_id FOR UPDATE",
        *ids,
    )


async def _header(conn: Any, container_id: int) -> dict | None:
    row = await conn.fetchrow(
        "SELECT * FROM container_costing WHERE container_id = $1", int(container_id)
    )
    return dict(row) if row else None


async def _apply_container_in(
    txn: Any, container_id: int, invoice_id: int, matched: list[dict]
) -> None:
    """Дописать к партиям накладной контейнера цену, валюту и курс прибытия.

    `matched` — позиции приёмки, сопоставленные с товаром (`container_receipt.
    match_items`): несколько позиций одного товара складываются в одну партию
    со средней ценой, итог в базовой валюте считается точно, по позициям.
    Заодно в приходную накладную уходят цены — документ показывает, во что
    обошлась партия.
    """
    container = await txn.fetchrow(
        "SELECT arrived_at, created_at FROM containers WHERE id = $1", int(container_id)
    )
    arrived_day = _day((container or {}).get("arrived_at") or (container or {}).get("created_at")) or _today()
    header = await _header(txn, container_id)
    prices_rows = await txn.fetch(
        "SELECT item_id, unit_price_cents FROM container_item_costs WHERE container_id = $1",
        int(container_id),
    )
    prices = {int(r["item_id"]): int(r["unit_price_cents"]) for r in prices_rows}

    rates: dict[str, Decimal] | None = None
    currency = base_currency()
    if header:
        currency = str(header["currency"]).upper()
        per_usd = _dec(header["uzs_per_usd"])
        per_unit = _dec(header["uzs_per_unit"])
        if per_usd and per_unit and per_usd > 0 and per_unit > 0:
            rates = rates_from_uzs(currency, per_usd, per_unit)

    by_product: dict[int, dict] = {}
    for m in matched:
        pid = int(m["product_id"])
        d = by_product.setdefault(pid, {"qty": Decimal(0), "sum": Decimal(0), "known": True})
        qty = _dec(m.get("quantity")) or Decimal(0)
        d["qty"] += qty
        price = prices.get(int(m.get("id") or 0))
        if price is None:
            d["known"] = False
        else:
            d["sum"] += Decimal(price) * qty

    await _lock_products(txn, list(by_product))
    lines = await txn.fetch(
        "SELECT product_id, quantity FROM invoice_items WHERE invoice_id = $1", int(invoice_id)
    )
    refs = json.dumps({k: _rstr(v) for k, v in rates.items()}) if rates else None
    stamp = _db.now_str()
    invoice_total = 0
    for line in lines:
        pid = int(line["product_id"])
        info = by_product.get(pid)
        known = bool(info and info["known"] and info["qty"] > 0 and rates is not None)
        unit_price = _cents(info["sum"] / info["qty"]) if known and info else None
        total_base = (
            _cents(info["sum"] * rates[currency]) if known and info and rates else None
        )
        if unit_price is not None:
            invoice_total += int(
                (Decimal(unit_price) * (_dec(line["quantity"]) or Decimal(0))).quantize(
                    _ONE, rounding=ROUND_HALF_UP
                )
            )
        existing = await txn.fetchval(
            "SELECT id FROM cost_batches WHERE invoice_id = $1 AND product_id = $2",
            int(invoice_id),
            pid,
        )
        if existing is None:
            # Контейнер оприходовали при выключенном учёте — партии нет.
            await txn.execute(
                "INSERT INTO cost_batches (invoice_id, product_id, container_id, batch_date, "
                "quantity, unit_price_cents, currency, rate_to_base, total_cost_base_cents, "
                "ref_rates, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                int(invoice_id),
                pid,
                int(container_id),
                arrived_day,
                float(_dec(line["quantity"]) or 0),
                unit_price,
                currency,
                _rstr(rates[currency]) if rates else None,
                total_base,
                refs,
                stamp,
            )
        else:
            await txn.execute(
                "UPDATE cost_batches SET container_id = $1, batch_date = $2, "
                "unit_price_cents = $3, currency = $4, rate_to_base = $5, "
                "total_cost_base_cents = $6, ref_rates = COALESCE($7, ref_rates) "
                "WHERE id = $8",
                int(container_id),
                arrived_day,
                unit_price,
                currency,
                _rstr(rates[currency]) if rates else None,
                total_base,
                refs,
                int(existing),
            )
        # Цена в приходной накладной — не движение склада: остаток не меняется,
        # поэтому мимо warehouse, но той же транзакцией, что и партия.
        await txn.execute(
            "UPDATE invoice_items SET price_cents = $1 WHERE invoice_id = $2 AND product_id = $3",
            unit_price,
            int(invoice_id),
            pid,
        )
    await txn.execute(
        "UPDATE invoices SET currency = $1, total_amount_cents = $2 WHERE id = $3",
        currency,
        invoice_total,
        int(invoice_id),
    )
    batch_ids = [
        int(r["id"])
        for r in await txn.fetch(
            "SELECT id FROM cost_batches WHERE invoice_id = $1", int(invoice_id)
        )
    ]
    await _recost_sales_in(txn, batch_ids)


async def _recost_sales_in(txn: Any, batch_ids: list[int]) -> None:
    """Пересчитать фиксации продаж из партий, у которых сменилась цена."""
    for bid in batch_ids:
        batch = await txn.fetchrow("SELECT * FROM cost_batches WHERE id = $1", bid)
        if not batch:
            continue
        b = {**dict(batch), "qty": _dec(batch["quantity"]) or Decimal(0)}
        for s in await txn.fetch(
            "SELECT id, quantity, currency FROM sale_costs WHERE batch_id = $1", bid
        ):
            await txn.execute(
                "UPDATE sale_costs SET cost_base_cents = $1, ref_rate_to_base = $2 "
                "WHERE id = $3",
                _part_cost(b, _dec(s["quantity"]) or Decimal(0)),
                _ref_for(b, str(s["currency"])),
                int(s["id"]),
            )


async def after_container_receipt_in(
    txn: Any,
    *,
    container_id: int,
    invoice_id: int,
    previous_invoice_id: int | None,
    matched: list[dict],
) -> None:
    """Зовёт `container_receipt.receive` той же транзакцией, что и приход.

    Переоприходование отменило прежнюю накладную и завело новую — продажи,
    уже списанные из прежних партий, переезжают на новые партии того же
    товара. Иначе они продолжили бы расходовать отменённую партию, а новая
    выглядела бы нетронутой, и остаток контейнера задвоился бы.
    """
    if not await is_enabled(txn):
        return
    if previous_invoice_id:
        for old in await txn.fetch(
            "SELECT id, product_id FROM cost_batches WHERE invoice_id = $1",
            int(previous_invoice_id),
        ):
            new_id = await txn.fetchval(
                "SELECT id FROM cost_batches WHERE invoice_id = $1 AND product_id = $2",
                int(invoice_id),
                int(old["product_id"]),
            )
            if new_id is not None:
                await txn.execute(
                    "UPDATE sale_costs SET batch_id = $1 WHERE batch_id = $2",
                    int(new_id),
                    int(old["id"]),
                )
    await _apply_container_in(txn, container_id, invoice_id, matched)


def _parse_price_cents(raw: Any) -> int | None:
    d = _dec(raw)
    if d is None or d < 0:
        raise ValueError("bad")
    return _cents(d * 100)


async def save_container_costing(
    container_id: int,
    *,
    currency: str,
    uzs_per_usd: Any,
    uzs_per_unit: Any = None,
    rate_source: str | None = None,
    prices: dict[int, Any],
    user_id: int,
    full_name: str = "",
) -> dict:
    """Сохранить цены закупки и курс прибытия; разнести их по партиям.

    Цены правятся и после суток окна приёмки: окно стережёт КОЛИЧЕСТВА, по
    которым двигался остаток, а закупочную цену честно вписывают позже, когда
    приходят бумаги поставщика. Пустое поле цены — «ещё не знаем», не ноль.
    """
    from services import container_receipt, containers

    if not await is_enabled():
        return {"ok": False, "error": "Учёт себестоимости выключен"}
    container = await containers.get_container(container_id)
    if not container:
        return {"ok": False, "error": "Контейнер не найден"}
    cur = (currency or "").strip().upper()
    if cur not in PURCHASE_CURRENCIES:
        return {"ok": False, "error": f"Валюта закупки: {', '.join(PURCHASE_CURRENCIES)}"}
    per_usd = _dec(uzs_per_usd)
    if per_usd is None or per_usd <= 0:
        return {"ok": False, "error": "Укажите курс: сколько сум за 1 USD на дату прибытия"}
    if cur == "USD":
        per_unit = per_usd
    elif cur == LOCAL_CURRENCY:
        per_unit = _ONE
    else:
        per_unit_dec = _dec(uzs_per_unit)
        if per_unit_dec is None or per_unit_dec <= 0:
            return {"ok": False, "error": f"Укажите курс: сколько сум за 1 {cur}"}
        per_unit = per_unit_dec

    items = await containers.list_items(container_id)
    known_ids = {int(i["id"]) for i in items}
    parsed: dict[int, int | None] = {}
    for raw_id, raw_price in (prices or {}).items():
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "Позиция не из этого контейнера"}
        if item_id not in known_ids:
            return {"ok": False, "error": "Позиция не из этого контейнера"}
        if raw_price is None or str(raw_price).strip() == "":
            parsed[item_id] = None
            continue
        try:
            parsed[item_id] = _parse_price_cents(raw_price)
        except ValueError:
            return {"ok": False, "error": "Цена должна быть числом не меньше нуля"}

    matched, _unmatched = await container_receipt.match_items(items)
    stamp = _db.now_str()
    source = (rate_source or "manual")[:16]
    rate_date = _day(container.get("arrived_at")) or _today()
    async with adb_core.transaction() as txn:
        if _db.USE_POSTGRES:
            # Тот же замок, что у приёмки: цены не должны разминуться с
            # переоприходованием, которое как раз меняет накладную контейнера.
            await txn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"container:receive:{container_id}"
            )
        await txn.execute("DELETE FROM container_costing WHERE container_id = $1", container_id)
        await txn.execute(
            "INSERT INTO container_costing (container_id, currency, uzs_per_usd, uzs_per_unit, "
            "rate_source, rate_date, updated_by, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            container_id,
            cur,
            _rstr(per_usd),
            _rstr(per_unit),
            source,
            rate_date,
            user_id,
            stamp,
        )
        for item_id, cents in parsed.items():
            await txn.execute(
                "DELETE FROM container_item_costs WHERE item_id = $1 AND container_id = $2",
                item_id,
                container_id,
            )
            if cents is not None:
                await txn.execute(
                    "INSERT INTO container_item_costs (item_id, container_id, unit_price_cents, "
                    "updated_by, updated_at) VALUES ($1, $2, $3, $4, $5)",
                    item_id,
                    container_id,
                    cents,
                    user_id,
                    stamp,
                )
        invoice_id = await txn.fetchval(
            "SELECT r.invoice_id FROM container_receipt r JOIN invoices i ON i.id = r.invoice_id "
            "WHERE r.container_id = $1 AND i.status = 'confirmed'",
            container_id,
        )
        if invoice_id is not None:
            await _apply_container_in(txn, container_id, int(invoice_id), matched)

    await asyncio.to_thread(
        _db.add_audit_log,
        user_id,
        full_name,
        await asyncio.to_thread(_db.get_role, user_id),
        "container_costing",
        f"#{container_id}: {cur}, 1 USD = {per_usd} сум, цен {sum(1 for v in parsed.values() if v is not None)}",
    )
    return {"ok": True, "applied_to_invoice": invoice_id is not None}


# ─── Оценка: строки продаж, контейнеры, товары ───────────────────────────────


def _eval_sale(row: dict) -> dict:
    """Выручка, себестоимость и курсовая разница одной фиксации — в базовой валюте."""
    qty = _dec(row["quantity"]) or Decimal(0)
    amount = Decimal(int(row["sale_price_cents"] or 0)) * qty  # копейки валюты продажи
    sale_rate = _dec(row.get("sale_rate_to_base"))
    ref_rate = _dec(row.get("ref_rate_to_base"))
    currency = str(row.get("currency") or base_currency()).upper()
    revenue = _cents(amount * sale_rate) if sale_rate is not None else None
    cost = row.get("cost_base_cents")
    out: dict[str, Any] = {
        "qty": qty,
        "amount_cents": _cents(amount),
        "currency": currency,
        "revenue": revenue,
        "cost": None if cost is None else int(cost),
        "fx": None,
    }
    if currency != base_currency() and sale_rate is not None and ref_rate is not None:
        at_arrival = _cents(amount * ref_rate)
        out["fx"] = {
            "at_arrival": at_arrival,
            "at_sale": int(revenue or 0),
            "diff": int(revenue or 0) - at_arrival,
        }
    return out


def _add_fx(acc: dict, fx: dict | None, currency: str, amount: int) -> None:
    if not fx:
        return
    acc["at_arrival_cents"] += fx["at_arrival"]
    acc["at_sale_cents"] += fx["at_sale"]
    acc["diff_cents"] += fx["diff"]
    by = acc["by_currency"].setdefault(currency, {"currency": currency, "amount_cents": 0, "diff_cents": 0})
    by["amount_cents"] += amount
    by["diff_cents"] += fx["diff"]


def _empty_fx() -> dict:
    return {"at_arrival_cents": 0, "at_sale_cents": 0, "diff_cents": 0, "by_currency": {}}


def _finish_fx(fx: dict) -> dict:
    return {**fx, "by_currency": sorted(fx["by_currency"].values(), key=lambda d: d["currency"])}


async def container_summaries(container_ids: list[int] | None = None) -> dict[int, dict]:
    """«Закуплено → продано → осталось → маржа» по контейнерам. Без N+1.

    Считается за всё время, а не за период: контейнер — это история одной
    закупки, и «продано за неделю» на его карточке ответило бы не на тот вопрос.
    """
    args: list[Any] = []
    where = "b.container_id IS NOT NULL"
    if container_ids is not None:
        if not container_ids:
            return {}
        args = [int(x) for x in sorted(set(container_ids))]
        where += f" AND b.container_id IN ({', '.join(f'${i + 1}' for i in range(len(args)))})"
    batch_rows = await adb_core.fetch(
        "SELECT b.id, b.product_id, b.container_id, b.currency, b.unit_price_cents, "
        "       b.total_cost_base_cents "
        "FROM cost_batches b JOIN invoices i ON i.id = b.invoice_id "
        f"WHERE i.status = 'confirmed' AND {where}",
        *args,
    )
    if not batch_rows:
        return {}
    state = await _batches_state(adb_core, sorted({int(b["product_id"]) for b in batch_rows}))
    by_id: dict[int, dict] = {}
    for entry in state.values():
        for b in entry["batches"]:
            by_id[int(b["id"])] = b

    sales = await adb_core.fetch(
        "SELECT s.quantity, s.cost_base_cents, s.sale_price_cents, s.currency, "
        "       s.sale_rate_to_base, s.ref_rate_to_base, b.container_id "
        "FROM sale_costs s JOIN invoices i ON i.id = s.invoice_id "
        "JOIN cost_batches b ON b.id = s.batch_id "
        f"WHERE i.status = 'confirmed' AND {where}",
        *args,
    )
    today_rates: dict[str, Decimal | None] = {}
    out: dict[int, dict] = {}
    for row in batch_rows:
        cid = int(row["container_id"])
        s = out.setdefault(
            cid,
            {
                "purchased_qty": Decimal(0), "purchased_cost_cents": 0, "cost_unknown_qty": Decimal(0),
                "sold_qty": Decimal(0), "revenue_cents": 0, "cogs_cents": 0,
                "revenue_unknown": False, "remaining_qty": Decimal(0), "remaining_cost_cents": 0,
                "stock_fx_diff_cents": 0, "fx": _empty_fx(),
            },
        )
        b = by_id.get(int(row["id"]))
        if not b:
            continue
        s["purchased_qty"] += b["qty"]
        if b["total_cost_base_cents"] is None:
            s["cost_unknown_qty"] += b["qty"]
        else:
            s["purchased_cost_cents"] += int(b["total_cost_base_cents"])
        covered = b.get("covered", Decimal(0))
        s["remaining_qty"] += covered
        part = _part_cost(b, covered)
        if part is not None:
            s["remaining_cost_cents"] += part
        # Остаток, купленный не в базовой валюте: во что он обошёлся бы по
        # сегодняшнему курсу — «переоценка» к курсу прибытия.
        cur = str(b["currency"]).upper()
        if cur != base_currency() and b.get("unit_price_cents") is not None and part is not None:
            if cur not in today_rates:
                today_rates[cur] = await _rate_current(adb_core, cur)
            rate_now = today_rates[cur]
            if rate_now is not None:
                s["stock_fx_diff_cents"] += (
                    _cents(Decimal(int(b["unit_price_cents"])) * covered * rate_now) - part
                )
    for row in sales:
        acc = out.get(int(row["container_id"]))
        if acc is None:
            continue
        ev = _eval_sale(row)
        acc["sold_qty"] += ev["qty"]
        if ev["revenue"] is None:
            acc["revenue_unknown"] = True
        else:
            acc["revenue_cents"] += ev["revenue"]
        if ev["cost"] is not None:
            acc["cogs_cents"] += ev["cost"]
        _add_fx(acc["fx"], ev["fx"], ev["currency"], ev["amount_cents"])
    for s in out.values():
        s["margin_cents"] = s["revenue_cents"] - s["cogs_cents"]
        s["margin_pct"] = (
            round(s["margin_cents"] / s["revenue_cents"] * 100, 1) if s["revenue_cents"] else None
        )
        s["fx"] = _finish_fx(s["fx"])
        for key in ("purchased_qty", "cost_unknown_qty", "sold_qty", "remaining_qty"):
            s[key] = float(s[key])
    return out


async def container_card(container_id: int) -> dict:
    """Всё для блока «Закупка и себестоимость» на карточке контейнера."""
    from services import containers

    container = await containers.get_container(container_id)
    if not container:
        return {"ok": False, "error": "Контейнер не найден"}
    enabled = await is_enabled()
    if not enabled:
        return {"ok": True, "enabled": False}
    header = await _header(adb_core, container_id)
    items = await containers.list_items(container_id)
    costs = {
        int(r["item_id"]): int(r["unit_price_cents"])
        for r in await adb_core.fetch(
            "SELECT item_id, unit_price_cents FROM container_item_costs WHERE container_id = $1",
            container_id,
        )
    }
    day = _day(container.get("arrived_at")) or _today()
    suggestion = None if header else await suggest_rates(day)
    summary = (await container_summaries([container_id])).get(container_id)
    return {
        "ok": True,
        "enabled": True,
        "base_currency": base_currency(),
        "currencies": list(PURCHASE_CURRENCIES),
        "arrived": container.get("status") == "arrived",
        "rate_day": day,
        "header": header,
        "suggestion": suggestion,
        "items": [
            {
                "id": int(i["id"]),
                "name": i["name"],
                "unit": i.get("unit") or "шт",
                "qty": float(i["arrived_qty"] if i.get("arrived_qty") is not None else i.get("expected_qty") or 0),
                "product_id": i.get("product_id"),
                "unit_price_cents": costs.get(int(i["id"])),
            }
            for i in items
        ],
        "summary": summary,
    }


async def current_costs(product_ids: list[int] | None = None) -> dict[int, dict]:
    """Средняя себестоимость ОСТАТКА по партиям (средневзвешенная по тому, что
    лежит на складе). Товар без партий с ценой в ответ не попадает — у него
    остаётся ручная себестоимость из карточки цены."""
    state = await _batches_state(adb_core, product_ids)
    out: dict[int, dict] = {}
    for pid, entry in state.items():
        qty = Decimal(0)
        cost = 0
        for b in entry["batches"]:
            covered = b.get("covered", Decimal(0))
            part = _part_cost(b, covered)
            if covered > 0 and part is not None:
                qty += covered
                cost += part
        if qty > 0:
            out[pid] = {
                "unit_cost_cents": _cents(Decimal(cost) / qty),
                "qty": float(qty),
                "uncovered_qty": float(entry["uncovered"]),
            }
    return out


async def product_cost(product_id: int) -> dict:
    """Себестоимость товара и история партий — для карточки цены у руководства."""
    state = (await _batches_state(adb_core, [int(product_id)])).get(int(product_id)) or {
        "batches": [], "stock": Decimal(0), "uncovered": Decimal(0),
    }
    containers_rows = await adb_core.fetch(
        "SELECT DISTINCT c.id, c.number FROM cost_batches b JOIN containers c ON c.id = b.container_id "
        "WHERE b.product_id = $1",
        int(product_id),
    )
    numbers = {int(r["id"]): r["number"] for r in containers_rows}
    manual = await adb_core.fetchrow(
        "SELECT cost_price_cents, currency FROM product_prices WHERE ms_id = $1", str(product_id)
    )
    avg = (await current_costs([int(product_id)])).get(int(product_id))
    history = []
    for b in reversed(state["batches"]):
        unit_cost = (
            _cents(Decimal(int(b["total_cost_base_cents"])) / b["qty"])
            if b["total_cost_base_cents"] is not None and b["qty"]
            else None
        )
        history.append(
            {
                "batch_id": int(b["id"]),
                "date": b["batch_date"],
                "container_id": b["container_id"],
                "container_number": numbers.get(int(b["container_id"])) if b["container_id"] else None,
                "qty": float(b["qty"]),
                "remaining": float(b.get("covered", Decimal(0))),
                "unit_price_cents": b["unit_price_cents"],
                "currency": b["currency"],
                "unit_cost_cents": unit_cost,
            }
        )
    return {
        "ok": True,
        "enabled": await is_enabled(),
        "base_currency": base_currency(),
        "avg_cost_cents": avg["unit_cost_cents"] if avg else None,
        "source": "batches" if avg else ("manual" if manual and manual.get("cost_price_cents") is not None else None),
        "manual": (
            {"cost_cents": int(manual["cost_price_cents"]), "currency": manual.get("currency") or base_currency()}
            if manual and manual.get("cost_price_cents") is not None
            else None
        ),
        "stock": float(state["stock"]),
        "uncovered_qty": float(state.get("uncovered", Decimal(0))),
        "batches": history[:30],
    }


async def order_profits() -> dict[int, dict]:
    """Прибыль отгруженных заказов по зафиксированной себестоимости.

    {order_id: {"profit": мажорные единицы В ВАЛЮТЕ ЗАКАЗА, "partial": bool}}.
    Валюта заказа, а не базовая: список заказов показывает суммы в валюте
    заказа, и прибыль рядом в долларах при сделке в сумах читалась бы как
    опечатка. Себестоимость переводится обратно по тому же курсу продажи, по
    которому зафиксирована, — курс сегодняшнего дня прибыль не двигает.
    Без `IN (...)`: фиксаций столько, сколько отгрузок после включения учёта.
    """
    rows = await adb_core.fetch(
        "SELECT os.order_id, s.quantity, s.cost_base_cents, s.sale_price_cents, "
        "       s.sale_rate_to_base "
        "FROM order_shipment os "
        "JOIN invoices i ON i.id = os.invoice_id AND i.status = 'confirmed' "
        "JOIN sale_costs s ON s.invoice_id = os.invoice_id"
    )
    out: dict[int, dict] = {}
    for r in rows:
        oid = int(r["order_id"])
        d = out.setdefault(oid, {"cents": Decimal(0), "partial": False})
        qty = _dec(r["quantity"]) or Decimal(0)
        rate = _dec(r["sale_rate_to_base"])
        if r["cost_base_cents"] is None or rate is None or rate <= 0:
            d["partial"] = True
            continue
        d["cents"] += Decimal(int(r["sale_price_cents"] or 0)) * qty - Decimal(
            int(r["cost_base_cents"])
        ) / rate
    return {
        oid: {"profit": float((d["cents"] / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
              "partial": d["partial"]}
        for oid, d in out.items()
    }


# ─── Отчёт руководства за период ─────────────────────────────────────────────


async def period_report(since: Any, until: Any = None) -> dict:
    """Прибыль по месяцам, товарам и контейнерам, курсовая разница, минусовые сделки.

    Считается ТОЛЬКО по отгрузкам с фиксацией себестоимости — то есть проведённым
    после включения учёта. Прежние продажи себестоимости не имеют, и
    приписывать им сегодняшнюю значило бы выдумать прибыль задним числом.
    Позиции без известной себестоимости в прибыль не входят, но их выручка
    показывается отдельно: прибыль «без половины товара» не должна выглядеть
    полной.
    """
    from services.warehouse import _upper_bound

    enabled = await is_enabled()
    args: list[Any] = [_day(since)]
    sql = (
        "SELECT s.invoice_id, s.product_id, s.batch_id, s.quantity, s.cost_base_cents, "
        "       s.cost_source, s.sale_price_cents, s.currency, s.sale_rate_to_base, "
        "       s.ref_rate_to_base, i.invoice_number, i.invoice_date, i.counterparty_id, "
        "       c.name AS counterparty_name, p.name AS product_name, b.container_id, "
        "       os.order_id "
        "FROM sale_costs s "
        "JOIN invoices i ON i.id = s.invoice_id "
        "LEFT JOIN products p ON p.id = s.product_id "
        "LEFT JOIN counterparties c ON c.id = i.counterparty_id "
        "LEFT JOIN cost_batches b ON b.id = s.batch_id "
        "LEFT JOIN order_shipment os ON os.invoice_id = s.invoice_id "
        "WHERE i.type = 'outgoing' AND i.status = 'confirmed' AND i.invoice_date >= $1"
    )
    if until is not None:
        day, exclusive = _upper_bound(until)
        args.append(day)
        sql += f" AND i.invoice_date {'<' if exclusive else '<='} ${len(args)}"
    rows = await adb_core.fetch(sql + " ORDER BY i.invoice_date, s.id", *args)
    started = await adb_core.fetchval("SELECT MIN(created_at) FROM sale_costs")

    totals: dict[str, Any] = {"revenue_cents": 0, "cogs_cents": 0, "profit_cents": 0,
              "unknown_cost_revenue_cents": 0, "unknown_cost_lines": 0, "no_rate_lines": 0}
    fx = _empty_fx()
    months: dict[str, dict] = {}
    products: dict[int, dict] = {}
    deals: dict[int, dict] = {}
    container_ids: set[int] = set()
    for r in rows:
        ev = _eval_sale(r)
        if ev["revenue"] is None:
            totals["no_rate_lines"] += 1
            continue
        month = str(r["invoice_date"])[:7]
        m = months.setdefault(month, {"month": month, "revenue_cents": 0, "cogs_cents": 0,
                                       "profit_cents": 0, "fx_diff_cents": 0})
        pr = products.setdefault(int(r["product_id"]), {
            "product_id": int(r["product_id"]), "name": r.get("product_name") or "—",
            "qty": 0.0, "revenue_cents": 0, "cogs_cents": 0, "profit_cents": 0, "partial": False,
        })
        deal = deals.setdefault(int(r["invoice_id"]), {
            "invoice_id": int(r["invoice_id"]), "invoice_number": r["invoice_number"],
            "date": r["invoice_date"], "counterparty": r.get("counterparty_name") or "—",
            "order_id": r.get("order_id"), "currency": ev["currency"],
            "revenue_cents": 0, "cogs_cents": 0, "partial": False,
        })
        pr["qty"] += float(ev["qty"])
        if ev["cost"] is None:
            totals["unknown_cost_revenue_cents"] += ev["revenue"]
            totals["unknown_cost_lines"] += 1
            pr["partial"] = True
            deal["partial"] = True
        else:
            for acc in (totals, m, pr):
                acc["revenue_cents"] += ev["revenue"]
                acc["cogs_cents"] += ev["cost"]
                acc["profit_cents"] += ev["revenue"] - ev["cost"]
            deal["revenue_cents"] += ev["revenue"]
            deal["cogs_cents"] += ev["cost"]
        if ev["fx"]:
            m["fx_diff_cents"] += ev["fx"]["diff"]
            _add_fx(fx, ev["fx"], ev["currency"], ev["amount_cents"])
        if r.get("container_id"):
            container_ids.add(int(r["container_id"]))

    def _pct(profit: int, revenue: int) -> float | None:
        return round(profit / revenue * 100, 1) if revenue else None

    totals["margin_pct"] = _pct(totals["profit_cents"], totals["revenue_cents"])
    product_list = sorted(products.values(), key=lambda d: d["profit_cents"], reverse=True)
    for d in product_list:
        d["margin_pct"] = _pct(d["profit_cents"], d["revenue_cents"])
    negative = []
    for d in deals.values():
        d["profit_cents"] = d["revenue_cents"] - d["cogs_cents"]
        # Минусовой считаем сделку, где себестоимость известна: убыток по
        # позиции без цены закупки — это незаполненные данные, а не цена продажи.
        if d["cogs_cents"] and d["profit_cents"] < 0:
            negative.append(d)
    negative.sort(key=lambda d: d["profit_cents"])

    summaries = await container_summaries(sorted(container_ids)) if container_ids else {}
    numbers = {}
    if summaries:
        ids = sorted(summaries)
        numbers = {
            int(r["id"]): r["number"]
            for r in await adb_core.fetch(
                f"SELECT id, number FROM containers WHERE id IN ({', '.join(f'${i + 1}' for i in range(len(ids)))})",
                *ids,
            )
        }
    return {
        "ok": True,
        "enabled": enabled,
        "base_currency": base_currency(),
        "started_at": (str(started)[:10] if started else None),
        "totals": totals,
        "by_month": [months[k] for k in sorted(months)],
        "top_products": product_list[:10],
        "worst_products": [d for d in reversed(product_list) if d["profit_cents"] < 0][:5],
        "negative_deals": negative[:20],
        "fx": _finish_fx(fx),
        "containers": [
            {"container_id": cid, "number": numbers.get(cid), **s}
            for cid, s in sorted(summaries.items(), key=lambda kv: -kv[1]["revenue_cents"])
        ][:10],
    }


# ─── Права: что уходит наружу ─────────────────────────────────────────────────


def redact_invoice(invoice: dict, role: str | None) -> dict:
    """Приходная накладная без цен для тех, кому себестоимость не положена.

    Цена в ПРИХОДЕ — это закупочная цена, то есть себестоимость. Расход
    оставляем как есть: его цена — продажная, её менеджер и так назначает.
    Режем в ответе ручки, а не во фронте: иначе любой новый экран вернёт её
    обратно (тот же приём, что `machines.visible_machine`).
    """
    if can_see_cost(role) or invoice.get("type") != "incoming":
        return invoice
    out = dict(invoice)
    out["total_amount_cents"] = None
    out["prices_hidden"] = True
    if isinstance(out.get("items"), list):
        out["items"] = [{**dict(it), "price_cents": None} for it in out["items"]]
    return out

