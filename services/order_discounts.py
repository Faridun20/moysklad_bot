"""
Скидка к прайсу по обычному товару: видимость в карточке одобрения (C2) и
порог, за которым скидку одобряют ЯВНО (C5).

Зачем. У сделки по технике руководитель видит «24 000 USD · прайс 25 000 USD ·
скидка 4%» (`services.machine_deal_requests.terms`) и решает, зная, сколько
уступили. У обычного заказа цена позиции вводится свободно, и в заявке на
отгрузку видна только она: с чем её сравнивать — руководитель не знает. Этот
модуль считает то же самое для строк заказа и для заказа целиком.

Что помнить:

* **Эталон — `product_prices.sale_price_cents`** (прайс, задаёт руководство
  через «Цены»). Своего справочника скидок нет и не нужно: появится более
  умный прайс — он ляжет в то же поле, и потребители не поменяются.
* **Скидка НИГДЕ не хранится.** Она выводится из цены строки и прайса в момент
  показа: сохранённый процент устаревал бы при первой же правке прайса или
  цены позиции, а «история» решения и так пишется в `audit_log`
  (`discount_approved` — сколько уступили в момент одобрения).
* **Нет прайса — нет скидки, а не ноль.** Новый товар, цену которому ещё не
  задавали, показывается прочерком: «скидка 0%» на нём означала бы, что
  продали ровно по прайсу, которого нет.
* **Валюта обязана совпасть.** Прайс хранит свою валюту, заказ — свою; курс
  на день заказа к прайсу отношения не имеет (прайс не переоценивают каждый
  день). Валюта прайса ≠ валюте заказа → эталона нет, строка с прочерком.
* **Средняя по заказу — ВЗВЕШЕННАЯ** (по суммам прайса, а не среднее
  арифметическое процентов): позиция на 10 000 со скидкой 30% и позиция на
  100 со скидкой 0% — это скидка 29,7%, а не 15%.
* **Порог** — `app_settings.order_discount_requires_approval_pct` (по
  умолчанию 15). Скидка ≥ порога хотя бы по ОДНОЙ строке или в среднем по
  заказу помечает заявку. `0` (или отрицательное) — пометка выключена.

Что порог МЕНЯЕТ в потоке. Одобрение отгрузки больше не обязательно (решение
владельца, сентябрь 2026): менеджер отгружает заказ сам
(`order_workflow.ship_order_now`). Скидка ≥ порога — вместе с долгом сверх
кредитного лимита — единственное, что отправляет заказ руководителю:
1. «Отгрузить» отказывает `decision_required`, менеджер отправляет заявку;
   заявка помечается в очереди решений (бот-карточка и «Решения» в WebApp) —
   видно, ГДЕ уступили и сколько;
2. одобряющий подтверждает скидку ЯВНО — как превышение кредитного лимита:
   `order_workflow.approve_shipment_request` отвечает `needs_discount_ack`, и
   одобрение проходит только вторым нажатием (`discount_ack=True`), а факт
   уходит в аудит. После одобрения менеджер отгружает сам.
Менеджер при этом видит обычную строку состояния — `pending_note`, в стиле
отказов `payment_required`/`stock_not_written_off`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from services import money

logger = logging.getLogger(__name__)

SETTING_KEY = "order_discount_requires_approval_pct"
DEFAULT_THRESHOLD_PCT = 15.0

# Код состояния для менеджера — в одном ряду с `payment_required` и
# `stock_not_written_off` (services/database.mark_order_shipped).
PENDING_CODE = "discount_approval_required"


# ─── Порог ──────────────────────────────────────────────────────────────────


def _norm_threshold(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_PCT
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
        return DEFAULT_THRESHOLD_PCT
    return value


def threshold_pct() -> float:
    """Порог из app_settings (sync-путь: бот, форматтеры карточек)."""
    from services.database import get_setting

    return _norm_threshold(get_setting(SETTING_KEY, DEFAULT_THRESHOLD_PCT))


async def current_threshold_pct() -> float:
    """Порог из app_settings для async-путей (ручки WebApp, order_workflow)."""
    from services import async_db as adb

    return _norm_threshold(await adb.get_setting(SETTING_KEY, DEFAULT_THRESHOLD_PCT))


# ─── Расчёт ─────────────────────────────────────────────────────────────────


def line_pct(price_cents: int | None, ref_cents: int | None) -> float | None:
    """Скидка строки в процентах, или None — сравнивать не с чем.

    Отрицательное значение законно: продали ДОРОЖЕ прайса (карточка техники
    так и пишет — «выше прайса на X%»).
    """
    if price_cents is None or ref_cents is None:
        return None
    try:
        ref = int(ref_cents)
        price = int(price_cents)
    except (TypeError, ValueError):
        return None
    if ref <= 0:
        return None
    return float(round(Decimal(ref - price) * 100 / Decimal(ref), 1))


def item_price_cents(item: dict) -> int | None:
    """Цена позиции в копейках. `order_items` отдаёт и `price_cents`, и
    мажорную `price` (`database._item_row`) — берём копейки, мажорную только
    как запасной путь (ручки, где позиция собрана из JSON)."""
    cents = item.get("price_cents")
    if cents is not None:
        try:
            return int(cents)
        except (TypeError, ValueError):
            return None
    price = item.get("price")
    if price in (None, ""):
        return None
    try:
        return money.to_cents(price)
    except (ArithmeticError, ValueError):
        return None


def _qty(item: dict) -> Decimal:
    try:
        q = Decimal(str(item.get("quantity") or 0))
    except (ArithmeticError, ValueError):
        return Decimal(0)
    return q if q > 0 else Decimal(0)


def reference_cents(price_row: dict | None, order_currency: str | None) -> int | None:
    """Прайс товара в копейках — только если он задан И в валюте заказа.

    Валюта прайса ≠ валюте заказа: курс тут не помощник (прайс не
    переоценивают ежедневно), и «пересчитанная» скидка была бы выдумкой.
    """
    if not price_row:
        return None
    cents = price_row.get("sale_price_cents")
    if cents is None:
        sale = price_row.get("sale_price")
        if sale in (None, ""):
            return None
        try:
            cents = money.to_cents(sale)
        except (ArithmeticError, ValueError):
            return None
    try:
        cents = int(cents)
    except (TypeError, ValueError):
        return None
    if cents <= 0:
        return None

    from config import BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    row_cur = (price_row.get("currency") or base).upper()
    order_cur = (order_currency or base).upper()
    if row_cur != order_cur:
        return None
    return cents


def product_ids(items: Iterable[dict]) -> list[str]:
    """Карточки номенклатуры позиций — для батч-выборки прайсов."""
    out = {str(it.get("product_id")) for it in items if it.get("product_id")}
    return sorted(out)


async def load_reference_prices(*item_lists: Iterable[dict]) -> dict[str, dict]:
    """Прайсы по всем позициям одним запросом (батч, без N+1).

    Принимает сколько угодно списков позиций — ручка «Заявки» считает скидку
    сразу по всем заявкам страницы, и по запросу на заявку это был бы тот же
    N+1, который уже чинили в кредит-контексте.
    """
    from services import async_db as adb

    ids: set[str] = set()
    for items in item_lists:
        ids.update(product_ids(items))
    if not ids:
        return {}
    return await adb.get_product_prices_by_ids(sorted(ids))


def summarize(
    items: list[dict],
    prices: dict[str, dict],
    currency: str | None,
    *,
    threshold: float,
) -> dict:
    """Скидка по строкам и по заказу целиком. Чистая функция — БД не трогает.

    Возвращает:
        lines        — по строке на позицию: `discount_pct` (None — прайса
                       нет), `ref_price_cents`, `flagged`;
        avg_pct      — взвешенная средняя по строкам С прайсом (None — таких
                       строк нет);
        max_pct      — самая большая скидка среди строк;
        flagged      — порог задан и пробит строкой или средней;
        covered/total — сколько позиций удалось сравнить (для «—» на экране).
    """
    from config import BASE_CURRENCY

    cur = (currency or BASE_CURRENCY or "USD").upper()
    lines: list[dict] = []
    ref_total = Decimal(0)
    act_total = Decimal(0)
    max_pct: float | None = None
    covered = 0
    for it in items:
        price_cents = item_price_cents(it)
        ref = reference_cents(prices.get(str(it.get("product_id") or "")), cur)
        pct = line_pct(price_cents, ref)
        flagged = pct is not None and threshold > 0 and pct >= threshold
        if pct is not None and ref is not None and price_cents is not None:
            covered += 1
            qty = _qty(it)
            ref_total += Decimal(ref) * qty
            act_total += Decimal(price_cents) * qty
            if max_pct is None or pct > max_pct:
                max_pct = pct
        lines.append(
            {
                "item_id": it.get("id"),
                "name": it.get("product_name") or it.get("name") or "",
                "price_cents": price_cents,
                "ref_price_cents": ref,
                "discount_pct": pct,
                "flagged": flagged,
            }
        )
    avg_pct: float | None = None
    if ref_total > 0:
        avg_pct = float(round((ref_total - act_total) * 100 / ref_total, 1))
    flagged = threshold > 0 and (
        (max_pct is not None and max_pct >= threshold)
        or (avg_pct is not None and avg_pct >= threshold)
    )
    return {
        "currency": cur,
        "threshold_pct": threshold,
        "lines": lines,
        "avg_pct": avg_pct,
        "max_pct": max_pct,
        "flagged": flagged,
        "covered_lines": covered,
        "total_lines": len(lines),
    }


async def order_discount(items: list[dict], currency: str | None) -> dict:
    """Скидка по одному заказу: прайсы + порог из БД, дальше `summarize`."""
    prices = await load_reference_prices(items)
    return summarize(items, prices, currency, threshold=await current_threshold_pct())


# ─── Подписи ────────────────────────────────────────────────────────────────


def _fmt(cents: int | None, currency: str) -> str:
    if cents is None:
        return "—"
    return f"{money.format_cents(int(cents), decimals=0, sep=' ')} {currency}"


def pct_label(pct: float | None) -> str:
    """«скидка 4%» / «выше прайса на 4%» / «—». Одна подпись на бот и WebApp."""
    if pct is None:
        return "—"
    if pct > 0:
        return f"скидка {pct:g}%"
    if pct < 0:
        return f"выше прайса на {abs(pct):g}%"
    return "по прайсу"


def line_label(line: dict, currency: str) -> str:
    """Хвост строки позиции: «прайс 25 000 USD · скидка 4%». '' — прайса нет."""
    if line.get("ref_price_cents") is None or line.get("discount_pct") is None:
        return ""
    return f"прайс {_fmt(line['ref_price_cents'], currency)} · {pct_label(line['discount_pct'])}"


def summary_label(summary: dict) -> str:
    """Строка сводки по заказу для карточки решения. '' — сравнивать не с чем."""
    if not summary or not summary.get("covered_lines"):
        return ""
    avg = summary.get("avg_pct")
    mx = summary.get("max_pct")
    text = f"Скидка по заказу: {pct_label(avg)}"
    if mx is not None and avg is not None and abs(mx - avg) >= 0.1:
        text += f" · максимум по позиции {mx:g}%"
    missing = int(summary.get("total_lines") or 0) - int(summary.get("covered_lines") or 0)
    if missing > 0:
        text += f" · без прайса: {missing}"
    return text


def flag_label(summary: dict) -> str:
    """Пометка «скидка выше порога» — для карточки решения. '' — не помечено."""
    if not summary or not summary.get("flagged"):
        return ""
    thr = summary.get("threshold_pct")
    worst = summary.get("max_pct")
    thr_txt = f"{float(thr):g}" if thr is not None else "—"
    worst_txt = f"{float(worst):g}" if worst is not None else "—"
    return f"Скидка {worst_txt}% — порог {thr_txt}%, нужно явное решение"


def pending_note(summary: dict) -> str:
    """Что видит МЕНЕДЖЕР по своей заявке — в стиле блокирующих состояний
    (`payment_required`, `stock_not_written_off`): что происходит и почему."""
    if not summary or not summary.get("flagged"):
        return ""
    worst = summary.get("max_pct")
    thr = summary.get("threshold_pct")
    worst_txt = f"{float(worst):g}" if worst is not None else "—"
    thr_txt = f"{float(thr):g}" if thr is not None else "—"
    return (
        f"Ждёт одобрения из-за скидки {worst_txt}% (порог {thr_txt}%) — "
        "решение принимает руководитель."
    )


def refusal(summary: dict, order_id: int | None = None) -> dict:
    """Отказ одобрения «скидка не подтверждена» — тем же словарём, что и
    остальные отказы потока заказа: `code` для фронта, `error` человеку."""
    worst = summary.get("max_pct")
    thr = summary.get("threshold_pct")
    worst_txt = f"{float(worst):g}" if worst is not None else "—"
    thr_txt = f"{float(thr):g}" if thr is not None else "—"
    head = f"Заказ #{order_id}: " if order_id else ""
    return {
        "ok": False,
        "code": PENDING_CODE,
        "needs_discount_ack": True,
        "discount": summary,
        "error": (
            f"{head}скидка {worst_txt}% при пороге {thr_txt}% — "
            "подтвердите решение явно."
        ),
    }
