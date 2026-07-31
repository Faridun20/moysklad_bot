"""
Учёт экскаваторов: карточка машины, моточасы, фото, сделки (волна 4, T4.2).

Почему отдельный сервис, а не таблица товаров МойСклад: машина — не позиция
номенклатуры, у неё свой жизненный цикл (в пути → на складе → бронь → продана
или в рассрочку → архив), моточасы, фотографии и покупатель с паспортом.
`ms_product_id` оставлен как необязательная связь, если машину всё же завели
в МС.

Правила, вынесенные в этот слой намеренно:

* **`cost_cents` режется здесь, а не на фронте.** Себестоимость видят только
  boss/admin. Если резать в шаблоне, любой новый вызов (WebApp, экспорт, лог)
  вернёт её обратно — разделение ролей должно жить там, где читаются данные.
* **Смена статуса — только CAS** (`WHERE status = ...` + rowcount): два
  одновременных «продал» не должны создать две сделки на одну машину.
* **Моточасы пишутся историей**, в `machines.hours` дублируется последнее
  значение. Показание меньше предыдущего отвергается: счётчик назад не идёт,
  это опечатка. Осознанную замену счётчика проводит босс через `force=True`.
* **Деньги — целые копейки** (`services.money`), как во всём проекте.

Время пишем `now_str()` — в том же локальном кадре, что и остальные таблицы.
План волны 4 предписывал `TIMESTAMPTZ + utc_now()`, но смешение кадров даёт
ровно тот молчаливый баг, о котором предупреждает CLAUDE.md: сравнение
локальной строки с UTC-порогом всегда ложно. Пороги считаем в Python и
передаём параметром (см. `archive_sold_machines`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

from services import adb_core, money
from services.database import USE_POSTGRES, add_audit_log, get_role, now_str
from utils.helpers import local_now

logger = logging.getLogger(__name__)

# Жизненный цикл машины. Тот же список стоит CHECK-ограничением в схеме (T4.1) —
# здесь он нужен, чтобы отдать понятную ошибку до похода в БД.
STATUSES = ("in_transit", "in_stock", "reserved", "sold", "on_credit", "archived")

STATUS_LABELS = {
    "in_transit": "🚢 В пути",
    "in_stock": "🏗 На складе",
    "reserved": "🔒 Забронирована",
    "sold": "✅ Продана",
    "on_credit": "💳 В рассрочку",
    "archived": "📦 Архив",
}

# Из каких статусов машину можно продать. Проданную/архивную — нельзя, и это
# проверяется тем же CAS-UPDATE, а не отдельным SELECT'ом.
_SELLABLE = ("in_transit", "in_stock", "reserved")

# Разрешённые переходы статуса «руками». Живут здесь, а не в клавиатуре бота и
# не в шаблоне WebApp: интерфейсов у машины теперь два, а граф должен быть один
# — иначе первое же изменение разъедет их между собой.
#
# Продажи и рассрочки в графе нет намеренно: они требуют цены и покупателя,
# поэтому идут через `create_deal`, который двигает статус сам.
NEXT_STATUSES: dict[str, tuple[str, ...]] = {
    "in_transit": ("in_stock",),
    "in_stock": ("reserved",),
    "reserved": ("in_stock",),
    "sold": ("archived",),
    "on_credit": (),
    "archived": (),
}


# Подпись действия зависит от ПАРЫ статусов, а не от целевого: «на склад» из
# пути и «снять бронь» из брони ведут в один и тот же `in_stock`, но говорят о
# разном. Держим здесь, чтобы бот и WebApp не расходились в формулировках.
TRANSITION_LABELS: dict[tuple[str, str], str] = {
    ("in_transit", "in_stock"): "🏗 На склад",
    ("in_stock", "reserved"): "🔒 Забронировать",
    ("reserved", "in_stock"): "🏗 Снять бронь",
    ("sold", "archived"): "📦 В архив",
}


def next_statuses(status: str | None) -> tuple[str, ...]:
    """Куда можно перевести машину из текущего статуса."""
    return NEXT_STATUSES.get(status or "", ())


def next_status_options(status: str | None) -> list[dict]:
    """То же, но с подписями кнопок — интерфейсу нужен готовый список."""
    current = status or ""
    return [
        {
            "status": target,
            "label": TRANSITION_LABELS.get(
                (current, target), STATUS_LABELS.get(target, target)
            ),
        }
        for target in next_statuses(current)
    ]

DEAL_KINDS = ("sale", "credit")

# Себестоимость — только для руководства.
_BOSS_ONLY_FIELDS = ("cost_cents",)

# Паспорт покупателя — персональные данные, нужные для договора рассрочки.
# Режем по той же причине и в том же месте, что и себестоимость: покажет их
# ровно тот слой, который читает данные, а не каждый шаблон по отдельности.
_BOSS_ONLY_DEAL_FIELDS = ("buyer_passport",)

_VIN_SEPARATORS = re.compile(r"[\s\-—–_]+")
_VIN_MAX = 64


def normalize_vin(raw: str | None) -> str:
    """VIN к каноничному виду: upper, без пробелов и дефисов.

    Один и тот же серийник приезжает то с дефисами, то с пробелами (из
    накладной, из мессенджера, с фотографии таблички). Без нормализации UNIQUE
    не спасает: «JCB-123 456» и «jcb123456» — разные строки и две карточки на
    один экскаватор.

    Длину НЕ ограничиваем 17 символами: у корейской и китайской техники
    серийники короче и длиннее автомобильного VIN — жёсткая проверка отсечёт
    половину парка. Ограничиваем только сверху, чтобы не принять в поле
    пересланный текст целиком.
    """
    vin = _VIN_SEPARATORS.sub("", (raw or "")).upper()
    return vin[:_VIN_MAX]


def visible_machine(row: dict | None, role: str | None) -> dict | None:
    """Карточка без полей, которых роли не положено видеть."""
    if row is None:
        return None
    data = dict(row)
    if role not in ("admin", "boss"):
        for field in _BOSS_ONLY_FIELDS:
            data.pop(field, None)
    return data


def visible_deal(row: dict | None, role: str | None) -> dict | None:
    """Сделка без паспорта покупателя для всех, кроме руководства."""
    if row is None:
        return None
    data = dict(row)
    if role not in ("admin", "boss"):
        for field in _BOSS_ONLY_DEAL_FIELDS:
            data.pop(field, None)
    return data


def can_see_cost(role: str | None) -> bool:
    return role in ("admin", "boss")


async def _audit(user_id: int, full_name: str, action: str, details: str) -> None:
    """Каждое действие с машиной — в общий аудит-лог: учёт техники нужен ради
    истории, а не текущего состояния."""
    role = await asyncio.to_thread(get_role, user_id)
    await asyncio.to_thread(add_audit_log, user_id, full_name, role, action, details)


def _validate_cents(value: int | None, label: str) -> tuple[bool, str]:
    if value is None:
        return True, ""
    if not isinstance(value, int):
        return False, f"{label}: сумма должна быть в копейках (целое число)"
    ok, err = money.validate_cents(value)
    return ok, (f"{label}: {err}" if not ok else "")


# ─── Карточка машины ─────────────────────────────────────────────────────────


async def create_machine(
    *,
    vin: str,
    name: str,
    created_by: int,
    creator_name: str = "",
    brand: str | None = None,
    model: str | None = None,
    year: int | None = None,
    hours: int | None = None,
    price_cents: int | None = None,
    cost_cents: int | None = None,
    currency: str = "USD",
    status: str = "in_transit",
    eta_date: str | None = None,
    container_no: str | None = None,
    location: str | None = None,
    notes: str | None = None,
) -> dict:
    """Завести машину. Возвращает {ok, machine_id} либо {ok: False, error}."""
    vin_norm = normalize_vin(vin)
    if not vin_norm:
        return {"ok": False, "error": "VIN обязателен"}
    if not (name or "").strip():
        return {"ok": False, "error": "Название обязательно"}
    if status not in STATUSES:
        return {"ok": False, "error": f"Неизвестный статус: {status}"}
    for value, label in ((price_cents, "Цена"), (cost_cents, "Себестоимость")):
        ok, err = _validate_cents(value, label)
        if not ok:
            return {"ok": False, "error": err}
    if hours is not None and hours < 0:
        return {"ok": False, "error": "Моточасы не могут быть отрицательными"}

    if await adb_core.fetchrow("SELECT id FROM machines WHERE vin = $1", vin_norm):
        # Явная проверка ДО вставки — иначе пользователь получит текст
        # UNIQUE-нарушения драйвера вместо человеческой подсказки. Гонку всё
        # равно ловит UNIQUE в схеме, поэтому исключение ниже не глушим.
        return {"ok": False, "error": f"Машина с VIN {vin_norm} уже заведена"}

    stamp = now_str()
    columns = (
        "vin, name, brand, model, year, hours, hours_updated_at, price_cents, cost_cents, "
        "currency, status, eta_date, container_no, location, notes, created_by, "
        "created_at, updated_at"
    )
    values = (
        vin_norm,
        name.strip(),
        brand,
        model,
        year,
        hours,
        stamp if hours is not None else None,
        price_cents,
        cost_cents,
        (currency or "USD").upper(),
        status,
        eta_date,
        container_no,
        location,
        notes,
        created_by,
        stamp,
        stamp,
    )
    placeholders = ", ".join(f"${i + 1}" for i in range(len(values)))
    async with adb_core.transaction() as txn:
        sql = f"INSERT INTO machines ({columns}) VALUES ({placeholders})"
        if USE_POSTGRES:
            machine_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            machine_id = await txn.fetchval("SELECT last_insert_rowid()")
        if hours is not None:
            # Стартовое показание — тоже история: иначе первая же запись
            # моточасов выглядит как «взялось из ниоткуда».
            await txn.execute(
                "INSERT INTO machine_hours (machine_id, hours, recorded_by, recorded_at) "
                "VALUES ($1, $2, $3, $4)",
                machine_id, hours, created_by, stamp,
            )

    await _audit(created_by, creator_name, "machine_created", f"{vin_norm} · {name}")
    return {"ok": True, "machine_id": int(machine_id), "vin": vin_norm}


async def get_machine(machine_id: int, *, role: str | None = None) -> dict | None:
    row = await adb_core.fetchrow("SELECT * FROM machines WHERE id = $1", machine_id)
    return visible_machine(row, role)


async def find_machine_by_vin(vin: str, *, role: str | None = None) -> dict | None:
    row = await adb_core.fetchrow("SELECT * FROM machines WHERE vin = $1", normalize_vin(vin))
    return visible_machine(row, role)


async def list_machines(*, role: str | None = None, status: str | None = None) -> list[dict]:
    """Список машин. Парк — 10–25 единиц, поэтому ни пагинации, ни поиска,
    ни кэша здесь нет и не нужно (T4.2)."""
    query = "SELECT * FROM machines"
    params: list[Any] = []
    if status:
        params.append(status)
        query += " WHERE status = $1"
    else:
        # Архив в общем списке не мешается — его смотрят отдельным фильтром.
        query += " WHERE status <> 'archived'"
    query += " ORDER BY id DESC"
    rows = await adb_core.fetch(query, *params)
    return [visible_machine(r, role) or {} for r in rows]


async def count_by_status() -> dict[str, int]:
    """Сколько машин в каждом статусе — для счётчиков в фильтре.

    Одним GROUP BY, а не шестью запросами из ручки. Ключ `all` — размер списка
    БЕЗ фильтра, то есть без архива: считать его на фронте значит продублировать
    там правило «архив в общий список не входит» из `list_machines`.
    """
    rows = await adb_core.fetch("SELECT status, COUNT(*) AS n FROM machines GROUP BY status")
    counts = dict.fromkeys(STATUSES, 0)
    for row in rows:
        status = str(row["status"])
        if status in counts:
            counts[status] = int(row["n"])
    counts["all"] = sum(n for s, n in counts.items() if s != "archived")
    return counts


async def update_machine_fields(
    machine_id: int,
    *,
    user_id: int,
    full_name: str = "",
    **fields: Any,
) -> dict:
    """Правка описательных полей (не статуса — для него `set_status`)."""
    allowed = {
        "name", "brand", "model", "year", "price_cents", "cost_cents", "currency",
        "eta_date", "container_no", "location", "notes", "ms_product_id",
    }
    unknown = set(fields) - allowed
    if unknown:
        return {"ok": False, "error": f"Нельзя менять поля: {', '.join(sorted(unknown))}"}
    if not fields:
        return {"ok": False, "error": "Нечего менять"}
    for key in ("price_cents", "cost_cents"):
        if key in fields:
            ok, err = _validate_cents(fields[key], "Цена" if key == "price_cents" else "Себестоимость")
            if not ok:
                return {"ok": False, "error": err}

    keys = sorted(fields)
    assignments = ", ".join(f"{k} = ${i + 1}" for i, k in enumerate(keys))
    params = [fields[k] for k in keys] + [now_str(), machine_id]
    rows = await adb_core.execute(
        f"UPDATE machines SET {assignments}, updated_at = ${len(keys) + 1} "
        f"WHERE id = ${len(keys) + 2}",
        *params,
    )
    if not rows:
        return {"ok": False, "error": "Машина не найдена"}
    await _audit(
        user_id, full_name, "machine_updated", f"#{machine_id}: {', '.join(keys)}"
    )
    return {"ok": True}


async def set_status(
    machine_id: int,
    new_status: str,
    *,
    user_id: int,
    full_name: str = "",
    expected: str | tuple[str, ...] | None = None,
) -> dict:
    """Сменить статус машины (CAS).

    `expected` — статус (или набор), из которого переход допустим. Проверка
    идёт тем же UPDATE'ом: между SELECT и UPDATE машину мог продать другой
    пользователь, и безусловный UPDATE затёр бы его решение.
    """
    if new_status not in STATUSES:
        return {"ok": False, "error": f"Неизвестный статус: {new_status}"}
    current = await adb_core.fetchrow("SELECT status FROM machines WHERE id = $1", machine_id)
    if not current:
        return {"ok": False, "error": "Машина не найдена"}
    expected_tuple = (
        (expected,) if isinstance(expected, str) else tuple(expected or (current["status"],))
    )
    placeholders = ", ".join(f"${i + 3}" for i in range(len(expected_tuple)))
    rows = await adb_core.execute(
        f"UPDATE machines SET status = $1, updated_at = $2 "
        f"WHERE id = ${len(expected_tuple) + 3} AND status IN ({placeholders})",
        new_status, now_str(), *expected_tuple, machine_id,
    )
    if not rows:
        return {
            "ok": False,
            "error": f"Статус уже «{STATUS_LABELS.get(current['status'], current['status'])}»",
            "current": current["status"],
        }
    await _audit(
        user_id, full_name, "machine_status_changed",
        f"#{machine_id}: {current['status']} → {new_status}",
    )
    return {"ok": True, "from": current["status"], "to": new_status}


async def delete_machine(machine_id: int, *, user_id: int, full_name: str = "") -> dict:
    """Удалить машину вместе с историей.

    Детей чистим явно, хотя в схеме стоит ON DELETE CASCADE: на SQLite внешние
    ключи по умолчанию выключены, и без этого удаление вело бы себя по-разному
    в тестах и на проде (осиротевшие фото и моточасы).
    """
    async with adb_core.transaction() as txn:
        machine = await txn.fetchrow("SELECT vin FROM machines WHERE id = $1", machine_id)
        if not machine:
            return {"ok": False, "error": "Машина не найдена"}
        deals = await txn.fetchval(
            "SELECT COUNT(*) FROM machine_deals WHERE machine_id = $1", machine_id
        )
        if int(deals or 0):
            # Сделка — денежный факт; удаление машины стёрло бы историю продажи.
            return {"ok": False, "error": "По машине есть сделки — используйте архив"}
        await txn.execute("DELETE FROM machine_photos WHERE machine_id = $1", machine_id)
        await txn.execute("DELETE FROM machine_hours WHERE machine_id = $1", machine_id)
        await txn.execute("DELETE FROM machines WHERE id = $1", machine_id)
    await _audit(user_id, full_name, "machine_deleted", f"#{machine_id} · {machine['vin']}")
    return {"ok": True}


# ─── Моточасы ────────────────────────────────────────────────────────────────


async def add_hours(
    machine_id: int,
    hours: int,
    *,
    user_id: int,
    full_name: str = "",
    force: bool = False,
) -> dict:
    """Записать показание моточасов.

    Пишем в историю и дублируем последнее значение в `machines.hours`: карточке
    нужно текущее число, а динамика и опечатки видны только по истории.

    Показание меньше предыдущего по умолчанию отвергаем — счётчик назад не
    идёт, это опечатка (1500 вместо 15000). `force=True` для законного случая
    замены счётчика; такой факт уходит в аудит отдельной пометкой.
    """
    if not isinstance(hours, int) or hours < 0:
        return {"ok": False, "error": "Моточасы — целое неотрицательное число"}
    machine = await adb_core.fetchrow("SELECT hours FROM machines WHERE id = $1", machine_id)
    if not machine:
        return {"ok": False, "error": "Машина не найдена"}
    previous = machine["hours"]
    if previous is not None and hours < int(previous) and not force:
        return {
            "ok": False,
            "error": f"Показание меньше предыдущего ({previous}). Опечатка?",
            "previous": int(previous),
            "needs_force": True,
        }

    stamp = now_str()
    async with adb_core.transaction() as txn:
        await txn.execute(
            "INSERT INTO machine_hours (machine_id, hours, recorded_by, recorded_at) "
            "VALUES ($1, $2, $3, $4)",
            machine_id, hours, user_id, stamp,
        )
        await txn.execute(
            "UPDATE machines SET hours = $1, hours_updated_at = $2, updated_at = $2 WHERE id = $3",
            hours, stamp, machine_id,
        )
    note = f"#{machine_id}: {previous or 0} → {hours} м/ч" + (" (замена счётчика)" if force else "")
    await _audit(user_id, full_name, "machine_hours_added", note)
    return {"ok": True, "hours": hours, "previous": previous}


async def get_hours_history(machine_id: int, limit: int = 20) -> list[dict]:
    return await adb_core.fetch(
        "SELECT * FROM machine_hours WHERE machine_id = $1 "
        "ORDER BY recorded_at DESC, id DESC LIMIT $2",
        machine_id, limit,
    )


# ─── Фотографии ──────────────────────────────────────────────────────────────


async def add_photo(
    machine_id: int,
    *,
    tg_file_id: str,
    file_unique_id: str,
    uploaded_by: int,
    caption: str | None = None,
    sort_order: int = 0,
) -> dict:
    """Привязать фотографию к машине.

    Файл не скачиваем: на Railway эфемерная ФС, а Telegram и так хранит его
    вечно. `file_unique_id` обязателен — он переживает смену сервера Bot API,
    в отличие от `tg_file_id`, и по нему же ловится повторная отправка того же
    снимка (UNIQUE в схеме).
    """
    if not tg_file_id or not file_unique_id:
        return {"ok": False, "error": "Нужны tg_file_id и file_unique_id"}
    if not await adb_core.fetchrow("SELECT id FROM machines WHERE id = $1", machine_id):
        return {"ok": False, "error": "Машина не найдена"}
    existing = await adb_core.fetchrow(
        "SELECT id FROM machine_photos WHERE machine_id = $1 AND file_unique_id = $2",
        machine_id, file_unique_id,
    )
    if existing:
        # Идемпотентность: переслал ту же фотографию второй раз — не ошибка и
        # не дубль в карточке.
        return {"ok": True, "photo_id": int(existing["id"]), "duplicate": True}
    await adb_core.execute(
        "INSERT INTO machine_photos (machine_id, tg_file_id, file_unique_id, caption, "
        "sort_order, uploaded_by, uploaded_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
        machine_id, tg_file_id, file_unique_id, caption, sort_order, uploaded_by, now_str(),
    )
    return {"ok": True, "duplicate": False}


async def list_photos(machine_id: int) -> list[dict]:
    return await adb_core.fetch(
        "SELECT * FROM machine_photos WHERE machine_id = $1 ORDER BY sort_order, id",
        machine_id,
    )


async def delete_photo(photo_id: int) -> dict:
    rows = await adb_core.execute("DELETE FROM machine_photos WHERE id = $1", photo_id)
    return {"ok": bool(rows)}


# ─── Сделки ──────────────────────────────────────────────────────────────────


async def create_deal(
    machine_id: int,
    *,
    kind: str,
    price_cents: int,
    buyer_name: str,
    created_by: int,
    creator_name: str = "",
    currency: str = "USD",
    buyer_phone: str | None = None,
    buyer_passport: str | None = None,
    buyer_note: str | None = None,
    order_id: int | None = None,
    agent_ms_id: str | None = None,
    due_date: str | None = None,
) -> dict:
    """Оформить продажу или рассрочку.

    Статус машины меняем тем же CAS'ом в одной транзакции со вставкой сделки:
    иначе два одновременных «продал» создали бы две сделки на одну машину, и
    обе выглядели бы успешными.
    """
    if kind not in DEAL_KINDS:
        return {"ok": False, "error": f"Тип сделки: {' / '.join(DEAL_KINDS)}"}
    ok, err = _validate_cents(price_cents, "Цена")
    if not ok:
        return {"ok": False, "error": err}
    if not price_cents:
        return {"ok": False, "error": "Цена сделки обязательна"}
    if not (buyer_name or "").strip():
        return {"ok": False, "error": "Покупатель обязателен"}
    if kind == "credit" and not due_date:
        return {"ok": False, "error": "Для рассрочки нужен срок оплаты"}

    new_status = "sold" if kind == "sale" else "on_credit"
    stamp = now_str()
    placeholders = ", ".join(f"${i + 1}" for i in range(13))
    async with adb_core.transaction() as txn:
        machine = await txn.fetchrow("SELECT status FROM machines WHERE id = $1", machine_id)
        if not machine:
            return {"ok": False, "error": "Машина не найдена"}
        moved = await txn.execute(
            "UPDATE machines SET status = $1, updated_at = $2 "
            "WHERE id = $3 AND status IN ($4, $5, $6)",
            new_status, stamp, machine_id, *_SELLABLE,
        )
        if not moved:
            label = STATUS_LABELS.get(machine["status"], machine["status"])
            return {"ok": False, "error": f"Машина в статусе «{label}» — сделка невозможна"}
        sql = (
            "INSERT INTO machine_deals (machine_id, kind, price_cents, currency, buyer_name, "
            "buyer_phone, buyer_passport, buyer_note, order_id, agent_ms_id, sold_at, "
            f"due_date, created_by) VALUES ({placeholders})"
        )
        values = (
            machine_id, kind, price_cents, (currency or "USD").upper(), buyer_name.strip(),
            buyer_phone, buyer_passport, buyer_note, order_id, agent_ms_id, stamp,
            due_date, created_by,
        )
        if USE_POSTGRES:
            deal_id = await txn.fetchval(sql + " RETURNING id", *values)
        else:
            await txn.execute(sql, *values)
            deal_id = await txn.fetchval("SELECT last_insert_rowid()")

    await _audit(
        created_by, creator_name, "machine_deal_created",
        f"#{machine_id} · {kind} · {money.format_cents(price_cents)} {currency} · {buyer_name}",
    )
    return {"ok": True, "deal_id": int(deal_id), "status": new_status}


async def close_deal(deal_id: int, *, user_id: int, full_name: str = "") -> dict:
    """Закрыть рассрочку (деньги получены полностью)."""
    rows = await adb_core.execute(
        "UPDATE machine_deals SET closed_at = $1 WHERE id = $2 AND closed_at IS NULL",
        now_str(), deal_id,
    )
    if not rows:
        return {"ok": False, "error": "Сделка не найдена или уже закрыта"}
    deal = await adb_core.fetchrow("SELECT machine_id FROM machine_deals WHERE id = $1", deal_id)
    if deal:
        await set_status(
            int(deal["machine_id"]), "sold",
            user_id=user_id, full_name=full_name, expected="on_credit",
        )
    await _audit(user_id, full_name, "machine_deal_closed", f"сделка #{deal_id}")
    return {"ok": True}


async def list_deals(machine_id: int, *, role: str | None = None) -> list[dict]:
    rows = await adb_core.fetch(
        "SELECT * FROM machine_deals WHERE machine_id = $1 ORDER BY sold_at DESC, id DESC",
        machine_id,
    )
    return [visible_deal(r, role) or {} for r in rows]


async def get_open_credit_deals(*, role: str | None = None) -> list[dict]:
    """Незакрытые рассрочки — для напоминаний о сроке."""
    rows = await adb_core.fetch(
        "SELECT d.*, m.vin, m.name FROM machine_deals d "
        "JOIN machines m ON m.id = d.machine_id "
        "WHERE d.kind = 'credit' AND d.closed_at IS NULL ORDER BY d.due_date"
    )
    return [visible_deal(r, role) or {} for r in rows]


# ─── Архивация (T4.3) ────────────────────────────────────────────────────────


async def archive_sold_machines(days: int = 90) -> int:
    """Проданные машины старше `days` уходят в архив. Возвращает число машин.

    Физически не удаляем — история цен нужна для будущих сделок.

    Порог считаем в Python и передаём параметром: `sold_at` пишется локальным
    `now_str()`, а `NOW()`/`datetime('now')` в SQL дают UTC — лексикографическое
    сравнение строк в разных кадрах молча всегда ложно (CLAUDE.md).
    """
    threshold = (local_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return await adb_core.execute(
        "UPDATE machines SET status = 'archived', updated_at = $1 "
        "WHERE status = 'sold' AND id IN ("
        "    SELECT machine_id FROM machine_deals "
        "    WHERE kind = 'sale' AND sold_at < $2"
        ")",
        now_str(), threshold,
    )
