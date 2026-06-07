"""
Локальный snapshot справочников и остатков МойСклад.

Зачем: при большом числе пользователей МойСклад API быстро становится
узким горлом. Все «холодные» данные (товары, категории, контрагенты,
сотрудники) меняются редко — нет смысла дёргать API на каждый запрос.
Остатки меняются часто, но webhooks от МойСклад дают возможность
обновлять их точечно вместо периодического полного pull-а.

Архитектура:
  - Раз в день рефрешим справочники (refresh_reference).
  - Каждые 2 часа — полный rescan остатков (safety net на случай
    пропущенных webhooks).
  - При получении webhook от МойСклад (см. webapp/api_ms_webhook)
    помечаем stock как dirty и просим refresh_stock в фоне.

Источник истины:
  - МойСклад — для справочников и остатков.
  - Локальная БД — для заказов/заявок/платежей бота.
"""

import asyncio
import logging

from services import adb_core
from services.database import get_conn, get_cursor, q, now_str
from services.moysklad import ms_get
from utils.helpers import extract_id_from_href, safe_get, extract_href

logger = logging.getLogger(__name__)


# ─── meta ────────────────────────────────────────────────────────────────────


async def meta_set(dataset: str, **fields) -> None:
    """Обновить метаданные снапшота для датасета (native async через adb_core).

    Имена столбцов в `fields` — код-контролируемые (last_full_refresh/rows_count/
    status/last_webhook_at), не пользовательский ввод → f-string безопасен."""
    cols = ["last_refresh"]
    vals: list = [now_str()]
    for k, v in fields.items():
        cols.append(k)
        vals.append(v)
    exists = await adb_core.fetchval("SELECT 1 FROM ms_snapshot_meta WHERE dataset = $1", dataset)
    if exists:
        set_clause = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(cols))
        await adb_core.execute(
            f"UPDATE ms_snapshot_meta SET {set_clause} WHERE dataset = ${len(cols) + 1}",
            *vals,
            dataset,
        )
    else:
        col_list = ", ".join(["dataset"] + cols)
        ph = ", ".join(f"${i + 1}" for i in range(1 + len(cols)))
        await adb_core.execute(
            f"INSERT INTO ms_snapshot_meta ({col_list}) VALUES ({ph})",
            dataset,
            *vals,
        )


async def meta_get(dataset: str) -> dict | None:
    """Метаданные снапшота датасета (native async)."""
    return await adb_core.fetchrow("SELECT * FROM ms_snapshot_meta WHERE dataset = $1", dataset)


# ─── Рефреш справочников ──────────────────────────────────────────────────────


async def _fetch_all(path: str, params: dict | None = None) -> list[dict]:
    """Постранично выкачать всё из endpoint'а МойСклад."""
    base_params = {"limit": 1000}
    if params:
        base_params.update(params)
    rows: list[dict] = []
    offset = 0
    while True:
        p = dict(base_params)
        p["offset"] = offset
        data = await ms_get(path, params=p)
        chunk = data if isinstance(data, list) else data.get("rows", [])
        rows.extend(chunk)
        if len(chunk) < base_params["limit"]:
            break
        offset += base_params["limit"]
    return rows


async def refresh_products() -> int:
    rows = await _fetch_all("entity/product")
    ts = now_str()
    values = [
        (
            ms_id,
            r.get("name", ""),
            extract_id_from_href(extract_href(r, "productFolder")),
            r.get("code", "") or r.get("article", ""),
            safe_get(r, "uom", "name", default="шт"),
            extract_href(r),
            ts,
        )
        for r in rows
        if (ms_id := (r.get("id") or extract_id_from_href(extract_href(r))))
    ]
    async with adb_core.transaction() as tx:
        await tx.execute("DELETE FROM ms_products")
        await tx.executemany(
            "INSERT INTO ms_products (ms_id, name, folder_id, code, unit, href, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            values,
        )
    await meta_set("products", last_full_refresh=ts, rows_count=len(values), status="ok")
    logger.info("snapshot.refresh_products: %d rows", len(values))
    return len(values)


async def refresh_categories() -> int:
    rows = await _fetch_all("entity/productfolder")
    ts = now_str()
    values = [
        (
            ms_id,
            r.get("name", ""),
            extract_id_from_href(extract_href(r, "productFolder")),
            extract_href(r),
            ts,
        )
        for r in rows
        if (ms_id := (r.get("id") or extract_id_from_href(extract_href(r))))
    ]
    async with adb_core.transaction() as tx:
        await tx.execute("DELETE FROM ms_categories")
        await tx.executemany(
            "INSERT INTO ms_categories (ms_id, name, parent_id, href, updated_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            values,
        )
    await meta_set("categories", last_full_refresh=ts, rows_count=len(values), status="ok")
    logger.info("snapshot.refresh_categories: %d rows", len(values))
    return len(values)


async def refresh_counterparties() -> int:
    rows = await _fetch_all("entity/counterparty", params={"order": "name"})
    # Баланс (взаиморасчёты) — отдельный дешёвый bulk-отчёт report/counterparty.
    # Строка: counterparty.meta.href → id, balance — в копейках (>0 клиент должен
    # нам, <0 аванс). Best-effort: если отчёт недоступен — пишем балансы как NULL.
    balances: dict[str, int] = {}
    try:
        for r in await _fetch_all("report/counterparty"):
            bid = extract_id_from_href(extract_href(r, "counterparty"))
            if bid:
                balances[bid] = int(r.get("balance") or 0)
    except Exception as e:  # noqa: BLE001 — отчёт опционален, имя/телефон важнее
        logger.warning("snapshot.refresh_counterparties: report/counterparty failed: %s", e)
    ts = now_str()
    values = [
        (
            ms_id,
            r.get("name", ""),
            r.get("phone", "") or "",
            extract_href(r),
            balances.get(ms_id),
            ts,
        )
        for r in rows
        if (ms_id := (r.get("id") or extract_id_from_href(extract_href(r))))
    ]
    async with adb_core.transaction() as tx:
        await tx.execute("DELETE FROM ms_counterparties")
        await tx.executemany(
            "INSERT INTO ms_counterparties (ms_id, name, phone, href, balance_cents, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            values,
        )
    await meta_set("counterparties", last_full_refresh=ts, rows_count=len(values), status="ok")
    logger.info(
        "snapshot.refresh_counterparties: %d rows (%d с балансом)", len(values), len(balances)
    )
    return len(values)


async def refresh_employees() -> int:
    rows = await _fetch_all("entity/employee")
    ts = now_str()
    values = [
        (ms_id, r.get("name", ""), extract_href(r), ts)
        for r in rows
        if (ms_id := (r.get("id") or extract_id_from_href(extract_href(r))))
    ]
    async with adb_core.transaction() as tx:
        await tx.execute("DELETE FROM ms_employees")
        await tx.executemany(
            "INSERT INTO ms_employees (ms_id, name, href, updated_at) VALUES ($1, $2, $3, $4)",
            values,
        )
    await meta_set("employees", last_full_refresh=ts, rows_count=len(values), status="ok")
    logger.info("snapshot.refresh_employees: %d rows", len(values))
    return len(values)


# ─── Рефреш остатков ──────────────────────────────────────────────────────────


async def refresh_stock() -> int:
    """Полный pull остатков. Вызывается как safety-net каждые 2 часа
    и из webhook-обработчика (после debounce)."""
    rows: list[dict] = []
    offset = 0
    limit = 1000
    while True:
        data = await ms_get("report/stock/all", params={"limit": limit, "offset": offset})
        chunk = data if isinstance(data, list) else data.get("rows", [])
        rows.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit

    ts = now_str()
    values = [
        (
            ms_id,
            r.get("name", ""),
            extract_id_from_href(extract_href(r, "folder")),
            safe_get(r, "folder", "name", default=""),
            safe_get(r, "uom", "name", default="шт"),
            r.get("stock", 0) or 0,
            r.get("reserve", 0) or 0,
            ts,
        )
        for r in rows
        if (ms_id := extract_id_from_href(extract_href(r)))
    ]
    async with adb_core.transaction() as tx:
        await tx.execute("DELETE FROM ms_stock")
        await tx.executemany(
            "INSERT INTO ms_stock (ms_id, name, folder_id, folder_name, unit, "
            "stock, reserve, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            values,
        )
    await meta_set("stock", last_full_refresh=ts, rows_count=len(values), status="ok")
    logger.info("snapshot.refresh_stock: %d rows", len(values))
    return len(values)


# ─── Дебаунс рефреша при webhook'ах ──────────────────────────────────────────

_stock_dirty = False
_stock_lock = asyncio.Lock()
# Уменьшили debounce с 5 до 2 секунд — менеджер на проде заметил,
# что после апрува остаток в WebApp обновляется заметно. 2с — баланс
# между «батчинг webhook'ов одной отгрузки» и «мгновенная UI-реакция».
_DEBOUNCE_SEC = 2


def mark_stock_dirty() -> None:
    """Вызывается из webhook-обработчика. Сам рефреш делает
    _stock_debounce_loop в фоне (запускается из bot.py)."""
    global _stock_dirty
    _stock_dirty = True


async def _stock_debounce_loop() -> None:
    """Бесконечная корутина: проверяет флаг и, если dirty, ждёт
    _DEBOUNCE_SEC и делает refresh_stock. Если за время ожидания
    прилетело ещё больше webhook'ов — один рефреш покрывает их все."""
    global _stock_dirty
    while True:
        try:
            if _stock_dirty:
                # Подождать на случай если пачка webhook'ов идёт подряд
                await asyncio.sleep(_DEBOUNCE_SEC)
                async with _stock_lock:
                    if _stock_dirty:
                        _stock_dirty = False
                        try:
                            await refresh_stock()
                            await meta_set("stock", last_webhook_at=now_str())
                        except Exception as e:
                            logger.exception("debounced stock refresh failed: %s", e)
                            _stock_dirty = True  # повторим на след. итерации
                            await asyncio.sleep(30)
            else:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stock debounce loop error")
            await asyncio.sleep(5)


# ─── Чтение ──────────────────────────────────────────────────────────────────


def get_stock(folder_id: str | None = None, only_positive: bool = False) -> list[dict]:
    """Список остатков. Если folder_id задан — только товары категории.

    По умолчанию `only_positive=False` — возвращаем и нулевые остатки,
    чтобы товар не пропадал из каталога после полной отгрузки. Раньше
    стояло True и менеджеры теряли позиции из списка как только
    останавливалась продажа.
    """
    where = []
    args: list = []
    if folder_id and folder_id != "all":
        where.append("folder_id = ?")
        args.append(folder_id)
    if only_positive:
        where.append("stock != 0")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(f"""SELECT ms_id, name, folder_id, folder_name, unit, stock, reserve
                  FROM ms_stock {where_sql} ORDER BY name"""),
            args,
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows


def get_low_stock(threshold: float = 5.0) -> list[dict]:
    """Товары с низким ДОСТУПНЫМ остатком: (stock − reserve) ≤ threshold,
    но в наличии (stock > 0). Сортировка по доступному (худшие сверху).

    Доступный = stock − reserve: зарезервированное под заказы уже «занято»,
    реально продать можно только свободное. Порог настраивается через
    app_settings `low_stock_threshold`.
    """
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT ms_id, name, folder_name, unit, stock, reserve "
                "FROM ms_stock "
                "WHERE (stock - reserve) <= ? AND stock > 0 "
                "ORDER BY (stock - reserve) ASC, name"
            ),
            (threshold,),
        )
        return [dict(r) for r in cur.fetchall()]


def search_products(query: str, limit: int = 10) -> list[dict]:
    """Поиск товаров в снапшоте по имени (LIKE, регистронезависимо). Для бот-UI
    выбора товара (напр. /prices ИМЯ). Возвращает [{ms_id, name, unit}]."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q(
                "SELECT ms_id, name, unit FROM ms_stock "
                "WHERE LOWER(name) LIKE ? ORDER BY name LIMIT ?"
            ),
            (f"%{(query or '').lower()}%", limit),
        )
        return [dict(r) for r in cur.fetchall()]


def get_product(ms_id: str) -> dict | None:
    """Товар снапшота по ms_id → {ms_id, name, unit} или None."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT ms_id, name, unit FROM ms_stock WHERE ms_id = ?"), (ms_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_categories() -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT ms_id, name, parent_id, href FROM ms_categories ORDER BY name"))
        return [dict(r) for r in cur.fetchall()]


def get_counterparties(search: str | None = None, limit: int = 50) -> list[dict]:
    if search:
        sql = (
            "SELECT ms_id, name, phone, balance_cents FROM ms_counterparties "
            "WHERE LOWER(name) LIKE ? ORDER BY name LIMIT ?"
        )
        args = (f"%{search.lower()}%", limit)
    else:
        sql = "SELECT ms_id, name, phone, balance_cents FROM ms_counterparties ORDER BY name LIMIT ?"
        args = (limit,)
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(sql), args)
        return [dict(r) for r in cur.fetchall()]


def get_counterparty(ms_id: str) -> dict | None:
    """Один контрагент из снапшота (для карточки): ms_id, name, phone, balance_cents."""
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT ms_id, name, phone, balance_cents, href FROM ms_counterparties WHERE ms_id = ?"),
            (ms_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_employees() -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT ms_id, name, href FROM ms_employees ORDER BY name"))
        return [dict(r) for r in cur.fetchall()]


def stats() -> dict:
    """Быстрая статистика по снапшоту (для /refresh-команды и диагностики)."""
    out = {}
    with get_conn() as conn:
        cur = get_cursor(conn)
        for tbl in (
            "ms_products",
            "ms_categories",
            "ms_counterparties",
            "ms_employees",
            "ms_stock",
        ):
            cur.execute(f"SELECT COUNT(*) AS c FROM {tbl}")
            row = cur.fetchone()
            out[tbl] = (dict(row) if not isinstance(row, dict) else row).get("c", 0) if row else 0
        cur.execute("SELECT * FROM ms_snapshot_meta")
        out["meta"] = [dict(r) for r in cur.fetchall()]
    return out


# ─── Высокоуровневые сценарии ────────────────────────────────────────────────


async def refresh_reference() -> dict:
    """Все справочники подряд. Вызывается утром и при /refresh."""
    counts = {}
    # Параллельно — независимые вызовы МойСклад
    products, categories, counterparties, employees = await asyncio.gather(
        refresh_products(),
        refresh_categories(),
        refresh_counterparties(),
        refresh_employees(),
        return_exceptions=True,
    )
    for name, val in [
        ("products", products),
        ("categories", categories),
        ("counterparties", counterparties),
        ("employees", employees),
    ]:
        if isinstance(val, Exception):
            logger.error("snapshot.refresh_reference: %s failed: %s", name, val)
            counts[name] = f"error: {val}"
        else:
            counts[name] = val
    return counts


async def refresh_all() -> dict:
    """Справочники + остатки. Полный init/manual /refresh."""
    ref = await refresh_reference()
    try:
        ref["stock"] = await refresh_stock()
    except Exception as e:
        logger.exception("snapshot.refresh_all: stock failed")
        ref["stock"] = f"error: {e}"
    return ref
