"""
Посты в канал: витрина, поступления, залежавшееся.

Канал — лицо компании и вход в воронку: под каждым постом кнопка «Спросить»,
которая ведёт в личку менеджера, а её переписку уже считает
`services/leads.py`. Поэтому пост — не рассылка, а начало разговора.

Правила, вынесенные в этот слой намеренно:

* **Наружу не уходит ни одна цифра количества.** Ни остаток, ни сколько
  приехало, ни сколько лежит. Клиенту это ничего не даёт, а конкуренту
  рассказывает о ваших объёмах и о том, сколько вы способны отгрузить. Внутри
  системы все числа остаются на месте. За этим следит `contains_quantities()`
  и тест на нём.
* **Публикует человек.** Сборщик готовит черновик, но `publish` вызывается
  только из ручки по нажатию — автопостинг в канал компании заводить нельзя.
* **Цена — не количество.** Её печатаем, если она задана в `product_prices`:
  это предложение, а не сведения о складе.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from services import adb_core
from services.database import get_setting, now_str
from utils.helpers import esc

logger = logging.getLogger(__name__)

POST_KINDS = ("showcase", "arrival", "stale")

KIND_LABELS = {
    "showcase": "🛒 Товар",
    "arrival": "📦 Поступление",
    "stale": "🏷 Есть в наличии",
}

# Сколько дней без продаж считаем «залежалось». Настройка — в app_settings,
# рядом с `low_stock_threshold`.
_STALE_DAYS_KEY = "stale_stock_days"
_STALE_DAYS_DEFAULT = 60

# Строка вида «12 шт», «500 м», «3 штуки» — то, чего в посте быть не должно.
# Ищем число рядом с единицей измерения, а не любое число: год выпуска и
# «PV 0.6» в названии товара — это не количество.
_QTY_RE = re.compile(
    r"\d[\d\s.,]*\s*(?:шт|штук\w*|компл\w*|уп\.?|упаков\w*|м\b|метр\w*|кг\b|т\b|л\b|пар\w*)",
    re.IGNORECASE,
)


def stale_days() -> int:
    try:
        return int(get_setting(_STALE_DAYS_KEY, _STALE_DAYS_DEFAULT))
    except (TypeError, ValueError):
        return _STALE_DAYS_DEFAULT


def contains_quantities(text: str) -> bool:
    """Есть ли в тексте количество с единицей измерения.

    Сторож для сборщиков: остатки и объёмы поставок — внутренняя информация, и
    попасть в канал они не должны даже случайно, через шаблон.
    """
    return bool(_QTY_RE.search(text or ""))


def _contact_line(manager_username: str | None) -> str:
    if manager_username:
        handle = manager_username.lstrip("@")
        return f'\n\n<a href="https://t.me/{esc(handle)}">Написать менеджеру</a>'
    return "\n\nНапишите менеджеру — ответим по наличию и цене"


def build_showcase(product: dict, *, price: str | None = None,
                   note: str | None = None, manager_username: str | None = None) -> str:
    """Карточка товара.

    Остаток не печатаем даже числом «в наличии: 12» — только сам факт наличия.
    """
    lines = [f"<b>{esc(product.get('name') or '—')}</b>"]
    if price:
        lines.append(f"\nЦена: <b>{esc(price)}</b>")
    lines.append("\n✅ Есть в наличии")
    if note:
        lines.append(f"\n{esc(note)}")
    return "".join(lines) + _contact_line(manager_username)


def build_arrival(names: list[str], *, note: str | None = None,
                  manager_username: str | None = None) -> str:
    """Анонс поступления: ТОЛЬКО названия.

    Ни номера контейнера, ни поставщика, ни количеств — всё это внутреннее.
    Клиенту важно, что товар появился; остальное рассказывает о вас лишнее.
    """
    if not names:
        return ""
    body = "\n".join(f"• {esc(n)}" for n in names)
    head = "📦 <b>Новое поступление</b>\n\nУже в продаже:\n"
    tail = f"\n\n{esc(note)}" if note else ""
    return head + body + tail + _contact_line(manager_username)


def build_stale(names: list[str], *, note: str | None = None,
                manager_username: str | None = None) -> str:
    """Пост по залежавшемуся. Что лежит и сколько — наружу не идёт: это
    предложение купить, а не отчёт о складе."""
    if not names:
        return ""
    body = "\n".join(f"• {esc(n)}" for n in names)
    head = "🏷 <b>Есть в наличии</b>\n\n"
    tail = f"\n\n{esc(note)}" if note else "\n\nСпрашивайте — подберём и посчитаем"
    return head + body + tail + _contact_line(manager_username)


# ─── Источники данных для черновиков ─────────────────────────────────────────


async def arrival_names(container_id: int) -> list[str]:
    """Названия фактически прибывших позиций контейнера.

    `arrived_qty > 0` — обещать то, что не доехало, нельзя. Само число решает
    только, попадёт ли строка в пост, и наружу не выходит.
    """
    rows = await adb_core.fetch(
        "SELECT name FROM container_items WHERE container_id = $1 "
        "AND arrived_qty IS NOT NULL AND arrived_qty > 0 ORDER BY id",
        container_id,
    )
    return [str(r["name"]) for r in rows]


def stale_candidates(dead_rows: list[dict], limit: int = 30) -> list[dict]:
    """Кандидаты в пост «залежавшееся» — для ВНУТРЕННЕГО экрана.

    Здесь остаток нужен: по нему решают, что выносить. В пост из этого списка
    уходят только названия.
    """
    rows = [
        {
            "name": r.get("name") or "—",
            "stock": float(r.get("stock", 0) or 0),
            "unit": r.get("unit") or "шт",
        }
        for r in dead_rows
        if float(r.get("stock", 0) or 0) > 0
    ]
    rows.sort(key=lambda r: -r["stock"])
    return rows[:limit]


# ─── История публикаций ──────────────────────────────────────────────────────


async def already_posted(kind: str, ref: str) -> dict | None:
    """Публиковали ли уже этот контейнер/товар.

    Без этой проверки один и тот же контейнер уходит в канал дважды — второй
    раз обычно потому, что первый забыли.
    """
    row = await adb_core.fetchrow(
        "SELECT * FROM channel_posts WHERE kind = $1 AND ref = $2 "
        "ORDER BY id DESC LIMIT 1",
        kind, ref,
    )
    return dict(row) if row else None


async def save_post(
    *, kind: str, ref: str | None, message_id: int | None, posted_by: int
) -> int:
    stamp = now_str()
    from services.database import USE_POSTGRES

    sql = (
        "INSERT INTO channel_posts (kind, ref, message_id, posted_by, posted_at, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $5)"
    )
    values = (kind, ref, message_id, posted_by, stamp)
    async with adb_core.transaction() as txn:
        if USE_POSTGRES:
            post_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            post_id = await txn.fetchval("SELECT last_insert_rowid()")
    return int(post_id)


async def history(limit: int = 30) -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT * FROM channel_posts ORDER BY id DESC LIMIT $1", limit
    )
    return [dict(r) for r in rows]


# Окно, в котором считаем отклик на пост, и глубина «обычного дня» для сравнения.
# Сутки — потому что канал читают в течение дня; 30 дней фона хватает, чтобы
# один удачный день не выглядел нормой.
EFFECT_WINDOW_HOURS = 24
EFFECT_BASELINE_DAYS = 30


async def post_effect(posted_at: str | None) -> dict | None:
    """Сколько новых обращений пришло в сутки после поста и сколько бывает обычно.

    Это КОРРЕЛЯЦИЯ, а не атрибуция: ссылка под постом ведёт прямо в личку
    менеджера и не несёт метки, поэтому кто именно пришёл с поста — неизвестно.
    Отвечает на «есть ли от канала отдача вообще», и подписывать это надо
    буквально «после поста», а не «из поста».

    Сравнимо напрямую: `channel_posts.posted_at` и `leads.first_seen_at` оба
    пишутся `now_str()` в локальном кадре.
    """
    stamp = _parse_stamp(posted_at)
    if stamp is None:
        return None
    window_end = stamp + timedelta(hours=EFFECT_WINDOW_HOURS)
    base_start = stamp - timedelta(days=EFFECT_BASELINE_DAYS)
    fmt = "%Y-%m-%d %H:%M:%S"

    after = await adb_core.fetchval(
        "SELECT COUNT(*) FROM leads WHERE first_seen_at >= $1 AND first_seen_at < $2",
        stamp.strftime(fmt), window_end.strftime(fmt),
    )
    before = await adb_core.fetchval(
        "SELECT COUNT(*) FROM leads WHERE first_seen_at >= $1 AND first_seen_at < $2",
        base_start.strftime(fmt), stamp.strftime(fmt),
    )
    return {
        "after": int(after or 0),
        # Обычный день — среднее за месяц до поста. Округляем до десятых:
        # «обычно 1,7 в день» честнее, чем «2».
        "baseline": round(int(before or 0) / EFFECT_BASELINE_DAYS, 1),
        "window_hours": EFFECT_WINDOW_HOURS,
    }


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
