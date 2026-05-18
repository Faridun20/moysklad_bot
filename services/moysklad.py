"""
Все запросы к API МойСклад
"""

import aiohttp
from datetime import datetime

from config import MS_TOKEN
from utils.helpers import extract_id_from_href

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"
MS_HEADERS = {
    "Authorization": f"Bearer {MS_TOKEN}",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
}


async def ms_get(session: aiohttp.ClientSession, path: str, params: dict = None):
    url = f"{MS_BASE}/{path}"
    async with session.get(url, headers=MS_HEADERS, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def get_all_stock() -> list[dict]:
    """Получить все остатки со всех страниц."""
    all_rows = []
    offset = 0
    limit = 100
    async with aiohttp.ClientSession() as session:
        while True:
            data = await ms_get(
                session, "report/stock/all", params={"limit": limit, "offset": offset}
            )
            rows = data if isinstance(data, list) else data.get("rows", [])
            all_rows.extend(rows)
            if len(rows) < limit:
                break
            offset += limit
    return [r for r in all_rows if r.get("stock", 0) != 0]


async def get_categories() -> list[dict]:
    """Получить список категорий товаров."""
    async with aiohttp.ClientSession() as session:
        data = await ms_get(session, "entity/productfolder", params={"limit": 100})
    return data if isinstance(data, list) else data.get("rows", [])


async def get_shipments(since: datetime, until: datetime = None) -> list[dict]:
    """Получить отгрузки за период."""
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.000")
    filter_str = f"moment>{since_str}"
    if until:
        until_str = until.strftime("%Y-%m-%d %H:%M:%S.000")
        filter_str += f";moment<{until_str}"
    async with aiohttp.ClientSession() as session:
        data = await ms_get(
            session,
            "entity/demand",
            params={
                "filter": filter_str,
                "expand": "agent,owner",
                "order": "moment,desc",
                "limit": 100,
            },
        )
    return data if isinstance(data, list) else data.get("rows", [])


async def get_shipment_positions(demand_id: str) -> list[dict]:
    """Получить позиции (товары) конкретной отгрузки."""
    async with aiohttp.ClientSession() as session:
        data = await ms_get(
            session,
            f"entity/demand/{demand_id}/positions",
            params={"limit": 100, "expand": "assortment,uom"},
        )
    return data if isinstance(data, list) else data.get("rows", [])


async def get_sales_stats(since: datetime, until: datetime = None) -> dict:
    """Статистика продаж за период: выручка, отгрузки, клиенты, топ товаров."""
    shipments = await get_shipments(since, until)
    if not shipments:
        return {"total": 0, "count": 0, "clients": 0, "top_products": []}

    total = sum(s.get("sum", 0) for s in shipments)
    clients = len(
        set(
            s.get("agent", {}).get("name", "")
            for s in shipments
            if s.get("agent", {}).get("name")
        )
    )

    product_sums: dict[str, dict] = {}
    for s in shipments[:15]:
        demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
        if not demand_id:
            continue
        try:
            positions = await get_shipment_positions(demand_id)
            for pos in positions:
                name = pos.get("assortment", {}).get("name", "—")
                qty = pos.get("quantity", 0)
                price = pos.get("price", 0)
                pos_sum = qty * price
                if name not in product_sums:
                    product_sums[name] = {"sum": 0, "qty": 0}
                product_sums[name]["sum"] += pos_sum
                product_sums[name]["qty"] += qty
        except Exception:
            pass

    top_products = sorted(
        product_sums.items(), key=lambda x: x[1]["sum"], reverse=True
    )[:5]

    return {
        "total": total,
        "count": len(shipments),
        "clients": clients,
        "top_products": top_products,
    }

async def get_employee_shipments(
    since: datetime,
    until: datetime = None,
    employee_href: str = None,
) -> list[dict]:
    """Получить отгрузки конкретного сотрудника по его href."""
    since_str = since.strftime("%Y-%m-%d %H:%M:%S.000")
    filter_str = f"moment>{since_str}"
    if until:
        filter_str += f";moment<{until.strftime('%Y-%m-%d %H:%M:%S.000')}"
    if employee_href:
        filter_str += f";owner={employee_href}"

    async with aiohttp.ClientSession() as session:
        data = await ms_get(
            session,
            "entity/demand",
            params={
                "filter": filter_str,
                "expand": "agent,owner",
                "order": "moment,desc",
                "limit": 100,
            },
        )
    return data if isinstance(data, list) else data.get("rows", [])


async def get_employee_stats(
    since: datetime,
    until: datetime = None,
    employee_href: str = None,
) -> dict:
    """Персональная статистика сотрудника."""
    shipments = await get_employee_shipments(since, until, employee_href)
    if not shipments:
        return {
            "total": 0, "count": 0, "clients": 0,
            "top_products": [], "by_day": {}, "product_sums": {}
        }

    total = sum(s.get("sum", 0) for s in shipments)
    clients = len(set(
        s.get("agent", {}).get("name", "")
        for s in shipments if s.get("agent", {}).get("name")
    ))

    # По дням недели
    days_ru = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
    by_day = {v: 0 for v in days_ru.values()}
    for s in shipments:
        try:
            day_num = datetime.strptime(s.get("moment", "")[:10], "%Y-%m-%d").weekday()
            by_day[days_ru[day_num]] += 1
        except Exception:
            pass

    # По товарам
    product_sums: dict[str, dict] = {}
    for s in shipments[:10]:  # Уменьшаем чтобы не превышать лимит
        demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
        if not demand_id:
            continue
        try:
            positions = await get_shipment_positions(demand_id)
            for pos in positions:
                name = pos.get("assortment", {}).get("name", "—")
                qty = pos.get("quantity", 0)
                price = pos.get("price", 0)
                if name not in product_sums:
                    product_sums[name] = {"sum": 0, "qty": 0}
                product_sums[name]["sum"] += qty * price
                product_sums[name]["qty"] += qty
        except Exception:
            pass

    top_products = sorted(
        product_sums.items(), key=lambda x: x[1]["sum"], reverse=True
    )[:5]

    return {
        "total": total,
        "count": len(shipments),
        "clients": clients,
        "top_products": top_products,
        "by_day": by_day,
        "product_sums": product_sums,
    }


async def get_employee_href(ms_employee_id: str) -> str:
    """Получить href сотрудника по его ID."""
    return f"{MS_BASE}/entity/employee/{ms_employee_id}"