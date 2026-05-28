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

from services.database import get_conn, get_cursor, q, now_str
from services.moysklad import ms_get
from utils.helpers import extract_id_from_href, safe_get, extract_href

logger = logging.getLogger(__name__)


# ─── meta ────────────────────────────────────────────────────────────────────


def meta_set(dataset: str, **fields) -> None:
    """Обновить метаданные снапшота для конкретного датасета."""
    cols = ["last_refresh"]
    vals = [now_str()]
    for k, v in fields.items():
        cols.append(k)
        vals.append(v)
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT 1 FROM ms_snapshot_meta WHERE dataset = ?"),
            (dataset,),
        )
        exists = cur.fetchone()
        if exists:
            set_clause = ", ".join(f"{c} = ?" for c in cols)
            cur.execute(
                q(f"UPDATE ms_snapshot_meta SET {set_clause} WHERE dataset = ?"),
                (*vals, dataset),
            )
        else:
            col_list = ", ".join(["dataset"] + cols)
            ph = ", ".join(["?"] * (1 + len(cols)))
            cur.execute(
                q(f"INSERT INTO ms_snapshot_meta ({col_list}) VALUES ({ph})"),
                (dataset, *vals),
            )
        conn.commit()


def meta_get(dataset: str) -> dict | None:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(
            q("SELECT * FROM ms_snapshot_meta WHERE dataset = ?"),
            (dataset,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


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
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("DELETE FROM ms_products")
        for r in rows:
            ms_id = r.get("id") or extract_id_from_href(extract_href(r))
            if not ms_id:
                continue
            cur.execute(
                q("""INSERT INTO ms_products
                     (ms_id, name, folder_id, code, unit, href, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)"""),
                (
                    ms_id,
                    r.get("name", ""),
                    extract_id_from_href(extract_href(r, "productFolder")),
                    r.get("code", "") or r.get("article", ""),
                    safe_get(r, "uom", "name", default="шт"),
                    extract_href(r),
                    now_str(),
                ),
            )
        conn.commit()
    meta_set("products", last_full_refresh=now_str(), rows_count=len(rows), status="ok")
    logger.info("snapshot.refresh_products: %d rows", len(rows))
    return len(rows)


async def refresh_categories() -> int:
    rows = await _fetch_all("entity/productfolder")
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("DELETE FROM ms_categories")
        for r in rows:
            ms_id = r.get("id") or extract_id_from_href(extract_href(r))
            if not ms_id:
                continue
            cur.execute(
                q("""INSERT INTO ms_categories
                     (ms_id, name, parent_id, href, updated_at)
                     VALUES (?, ?, ?, ?, ?)"""),
                (
                    ms_id,
                    r.get("name", ""),
                    extract_id_from_href(extract_href(r, "productFolder")),
                    extract_href(r),
                    now_str(),
                ),
            )
        conn.commit()
    meta_set("categories", last_full_refresh=now_str(), rows_count=len(rows), status="ok")
    logger.info("snapshot.refresh_categories: %d rows", len(rows))
    return len(rows)


async def refresh_counterparties() -> int:
    rows = await _fetch_all("entity/counterparty", params={"order": "name"})
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("DELETE FROM ms_counterparties")
        for r in rows:
            ms_id = r.get("id") or extract_id_from_href(extract_href(r))
            if not ms_id:
                continue
            cur.execute(
                q("""INSERT INTO ms_counterparties
                     (ms_id, name, phone, href, updated_at)
                     VALUES (?, ?, ?, ?, ?)"""),
                (
                    ms_id,
                    r.get("name", ""),
                    r.get("phone", "") or "",
                    extract_href(r),
                    now_str(),
                ),
            )
        conn.commit()
    meta_set("counterparties", last_full_refresh=now_str(), rows_count=len(rows), status="ok")
    logger.info("snapshot.refresh_counterparties: %d rows", len(rows))
    return len(rows)


async def refresh_employees() -> int:
    rows = await _fetch_all("entity/employee")
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("DELETE FROM ms_employees")
        for r in rows:
            ms_id = r.get("id") or extract_id_from_href(extract_href(r))
            if not ms_id:
                continue
            cur.execute(
                q("""INSERT INTO ms_employees (ms_id, name, href, updated_at)
                     VALUES (?, ?, ?, ?)"""),
                (ms_id, r.get("name", ""), extract_href(r), now_str()),
            )
        conn.commit()
    meta_set("employees", last_full_refresh=now_str(), rows_count=len(rows), status="ok")
    logger.info("snapshot.refresh_employees: %d rows", len(rows))
    return len(rows)


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

    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute("DELETE FROM ms_stock")
        for r in rows:
            href = extract_href(r)
            ms_id = extract_id_from_href(href)
            if not ms_id:
                continue
            cur.execute(
                q("""INSERT INTO ms_stock
                     (ms_id, name, folder_id, folder_name, unit,
                      stock, reserve, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""),
                (
                    ms_id,
                    r.get("name", ""),
                    extract_id_from_href(extract_href(r, "folder")),
                    safe_get(r, "folder", "name", default=""),
                    safe_get(r, "uom", "name", default="шт"),
                    r.get("stock", 0) or 0,
                    r.get("reserve", 0) or 0,
                    now_str(),
                ),
            )
        conn.commit()
    meta_set("stock", last_full_refresh=now_str(), rows_count=len(rows), status="ok")
    logger.info("snapshot.refresh_stock: %d rows", len(rows))
    return len(rows)


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
                            meta_set("stock", last_webhook_at=now_str())
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


def get_categories() -> list[dict]:
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q("SELECT ms_id, name, parent_id, href FROM ms_categories ORDER BY name"))
        return [dict(r) for r in cur.fetchall()]


def get_counterparties(search: str | None = None, limit: int = 50) -> list[dict]:
    if search:
        sql = "SELECT ms_id, name, phone FROM ms_counterparties WHERE LOWER(name) LIKE ? ORDER BY name LIMIT ?"
        args = (f"%{search.lower()}%", limit)
    else:
        sql = "SELECT ms_id, name, phone FROM ms_counterparties ORDER BY name LIMIT ?"
        args = (limit,)
    with get_conn() as conn:
        cur = get_cursor(conn)
        cur.execute(q(sql), args)
        return [dict(r) for r in cur.fetchall()]


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
