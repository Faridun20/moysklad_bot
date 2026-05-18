"""
FastAPI сервер для WebApp.
Запускается параллельно с ботом.
"""
import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from webapp.auth import verify_init_data
from services.database import get_role

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="МойСклад WebApp")


class CachedStaticFiles(StaticFiles):
    """StaticFiles + Cache-Control: пусть браузер хранит CSS/JS сутки."""

    def __init__(self, *args, max_age: int = 86400, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_header = f"public, max-age={max_age}"

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers.setdefault("Cache-Control", self._cache_header)
        return resp


# Раздаём статику (CSS, JS) с кэшированием на сутки
app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")


# ─── Главная страница ─────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    """Отдаём главную HTML страницу."""
    html_file = STATIC_DIR / "index.html"
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


# ─── API: проверка авторизации ────────────────────────────────────────────────


@app.post("/api/me")
async def get_me(request: Request):
    """
    Принимает initData от Telegram WebApp,
    проверяет подпись, возвращает информацию о пользователе и его роли.
    """
    data = await request.json()
    init_data = data.get("initData", "")

    user = verify_init_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    user_id = user["id"]
    role = get_role(user_id)

    return JSONResponse({
        "user_id": user_id,
        "first_name": user.get("first_name", ""),
        "username": user.get("username", ""),
        "role": role,
    })


# ─── API: главный экран (сводка дня + мои заказы + для босса аналитика) ────


@app.post("/api/home")
async def api_home(request: Request):
    """
    Данные для Главного экрана WebApp — компактная сводка, чтобы юзер
    видел что-то полезное сразу, не переключаясь по табам.

    Возвращает разное содержимое в зависимости от роли:
    - все: today (выручка/отгрузок/клиентов за сегодня) + my_orders сводка
    - boss/admin: дополнительно pending_requests и top_employees за неделю
    """
    from datetime import datetime, timedelta
    from services.moysklad import get_sales_stats, get_shipments
    from services.database import (
        get_user_orders, get_pending_requests,
        get_moysklad_employee_id,
    )

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    user_id = user["id"]
    role = get_role(user_id)
    if role not in ("admin", "boss", "manager"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    now = datetime.utcnow()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    # Сводка за сегодня — общая (по всему МойСклад)
    try:
        today_stats = await get_sales_stats(start_of_day, now)
    except Exception as e:
        logger.warning("home: failed to load today stats: %s", e)
        today_stats = {"total": 0, "count": 0, "clients": 0, "top_products": []}

    # Заказы текущего юзера
    my_orders = get_user_orders(user_id)
    orders_by_status = {"draft": 0, "pending": 0, "approved": 0, "rejected": 0, "shipped": 0}
    for o in my_orders:
        orders_by_status[o["status"]] = orders_by_status.get(o["status"], 0) + 1

    recent = [
        {
            "id": o["id"],
            "status": o["status"],
            "agent_name": o.get("agent_name", ""),
            "created_at": o["created_at"][:16],
        }
        for o in my_orders[:5]
    ]

    result = {
        "role": role,
        "today": {
            "revenue": today_stats["total"] / 100,  # копейки → ₽/₸ etc
            "shipments": today_stats["count"],
            "clients": today_stats["clients"],
        },
        "my_orders": {
            "draft": orders_by_status["draft"],
            "pending": orders_by_status["pending"],
            "approved": orders_by_status["approved"],
            "rejected": orders_by_status["rejected"],
            "total": len(my_orders),
            "recent": recent,
        },
        "ms_linked": bool(get_moysklad_employee_id(user_id)),
    }

    # Для босса/админа — дополнительно
    if role in ("admin", "boss"):
        pending = get_pending_requests()
        result["pending_requests"] = len(pending)

        # Топ-сотрудники за последнюю неделю по выручке.
        # Берём отгрузки за неделю и группируем по owner.name.
        # Это +1 запрос к МойСклад, но дешевле чем дёргать get_employee_stats
        # на каждого сотрудника отдельно.
        try:
            week_shipments = await get_shipments(week_ago, now)
            by_owner: dict[str, dict] = {}
            for s in week_shipments:
                owner_name = (s.get("owner") or {}).get("name") or "—"
                cur = by_owner.setdefault(owner_name, {"sum": 0, "count": 0})
                cur["sum"] += s.get("sum", 0)
                cur["count"] += 1
            top_emp = sorted(
                by_owner.items(), key=lambda kv: kv[1]["sum"], reverse=True
            )[:5]
            result["top_employees"] = [
                {"name": name, "revenue": d["sum"] / 100, "count": d["count"]}
                for name, d in top_emp
            ]
        except Exception as e:
            logger.warning("home: failed to load top employees: %s", e)
            result["top_employees"] = []

    return JSONResponse(result)


# ─── API: остатки склада ─────────────────────────────────────────────────────


@app.post("/api/stock")
async def api_stock(request: Request):
    """Список товаров со склада."""
    from services.moysklad import get_all_stock, get_categories
    from utils.helpers import extract_id_from_href

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    role = get_role(user["id"])
    if role not in ("admin", "boss", "manager"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    try:
        rows, cats = await asyncio.gather(
            get_all_stock(),
            get_categories(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Готовим компактный JSON
    products = [
        {
            "name": r.get("name", "—"),
            "stock": r.get("stock", 0),
            "reserve": r.get("reserve", 0),
            "unit": r.get("uom", {}).get("name", "шт"),
            "folder_id": extract_id_from_href(
                r.get("folder", {}).get("meta", {}).get("href", "")
            ),
            "folder_name": r.get("folder", {}).get("name", ""),
        }
        for r in rows
    ]

    categories = [
        {
            "id": extract_id_from_href(c.get("meta", {}).get("href", "")),
            "name": c.get("name", "—"),
        }
        for c in cats
    ]

    return JSONResponse({"products": products, "categories": categories})


# ─── API: аналитика продаж ───────────────────────────────────────────────────


@app.post("/api/analytics")
async def api_analytics(request: Request):
    """Аналитика продаж за период."""
    from datetime import datetime, timedelta
    from services.moysklad import get_sales_stats, get_shipments
    from utils.helpers import extract_id_from_href

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    role = get_role(user["id"])
    if role not in ("admin", "boss", "manager"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    period = data.get("period", "week")
    now = datetime.utcnow()

    periods = {
        "week": (now - timedelta(weeks=1), now - timedelta(weeks=2), "Неделя"),
        "month": (now - timedelta(days=30), now - timedelta(days=60), "Месяц"),
        "3month": (now - timedelta(days=90), now - timedelta(days=180), "3 месяца"),
        "year": (now - timedelta(days=365), now - timedelta(days=730), "Год"),
    }
    since, prev_since, label = periods.get(period, periods["month"])

    try:
        current, prev, shipments = await asyncio.gather(
            get_sales_stats(since, now),
            get_sales_stats(prev_since, since),
            get_shipments(since, now),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # По дням недели
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    by_day = [0] * 7
    for s in shipments:
        try:
            moment = s.get("moment", "")[:10]
            day_num = datetime.strptime(moment, "%Y-%m-%d").weekday()
            by_day[day_num] += 1
        except Exception:
            pass

    # Тренд
    trend = 0
    if prev["total"] > 0:
        trend = round((current["total"] - prev["total"]) / prev["total"] * 100)

    # Топ товаров — top_products это список tuples (name, data)
    top = [
        {"name": name, "sum": d["sum"] / 100, "qty": d["qty"]}
        for name, d in current["top_products"][:5]
    ]

    return JSONResponse({
        "label": label,
        "total": current["total"] / 100,
        "count": current["count"],
        "clients": current["clients"],
        "avg_check": (current["total"] / current["count"] / 100) if current["count"] else 0,
        "trend": trend,
        "by_day": [{"day": days_ru[i], "count": by_day[i]} for i in range(7)],
        "top_products": top,
    })

# ─── API: платежи ─────────────────────────────────────────────────────────────


@app.post("/api/payments/history")
async def api_payments_history(request: Request):
    """История платежей текущего пользователя.

    Работает поверх get_conn(), поэтому одинаково корректно для SQLite
    и PostgreSQL — раньше эндпоинт жёстко звал sqlite3.connect(DB_PATH),
    и на Railway (где БД — Postgres, а DB_PATH указывает на ephemeral
    /tmp/payments.db) валился с «unable to open database file».
    """
    from services.database import get_conn, get_cursor, q

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    user_id = user["id"]

    try:
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q(
                    "SELECT id, amount, currency, comment, status, created_at "
                    "FROM payments WHERE user_id = ? "
                    "ORDER BY created_at DESC LIMIT 50"
                ),
                (user_id,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        return JSONResponse({"payments": rows})
    except Exception as e:
        logger.exception("payments/history failed for user_id=%s", user_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/payments/send")
async def api_payments_send(request: Request):
    """Отправить новый платёж на подтверждение."""
    from config import ADMIN_IDS, TELEGRAM_TOKEN
    from services.database import add_payment, add_audit_log, get_role
    from utils.formatters import format_payment_notify
    import aiohttp

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    # Валидация
    try:
        amount = float(data.get("amount", 0))
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверная сумма")

    currency = data.get("currency", "USD")
    if currency not in ("USD", "UZS", "RUB", "EUR"):
        raise HTTPException(status_code=400, detail="Неверная валюта")

    comment = (data.get("comment", "") or "").strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Укажите комментарий")

    user_id = user["id"]
    full_name = (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("username", "")
        or str(user_id)
    )
    username = f"@{user['username']}" if user.get("username") else "—"

    # Сохраняем в БД
    payment_id = add_payment(user_id, username, full_name, amount, currency, comment)

    # Аудит
    add_audit_log(
        user_id, full_name, get_role(user_id),
        "payment_sent",
        f"Платёж #{payment_id}: {amount:,.0f} {currency} — {comment}",
    )

    # Уведомляем админов через Telegram API напрямую
    notify_text = format_payment_notify(
        payment_id, full_name, username, amount, currency, comment
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Принять",   "callback_data": f"pay_ok:{payment_id}"},
            {"text": "❌ Отклонить", "callback_data": f"pay_no:{payment_id}"},
        ]]
    }

    from services.notifier import get_notify_recipients
    recipients = get_notify_recipients()

    async with aiohttp.ClientSession() as session:
        for uid in recipients:
            try:
                await session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={
                        "chat_id": uid,
                        "text": notify_text,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard,
                    },
                )
            except Exception as e:
                logger.warning("Не удалось уведомить %d: %s", uid, e)

    return JSONResponse({"payment_id": payment_id, "status": "pending"})

# ─── API: заказы ─────────────────────────────────────────────────────────────


@app.post("/api/orders")
async def api_orders(request: Request):
    """Список заказов текущего пользователя."""
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services.database import get_user_orders, get_order_items, get_role
    role = get_role(user["id"])

    if role in ("admin", "boss"):
        from services.database import get_all_orders
        orders = get_all_orders()
    else:
        orders = get_user_orders(user["id"])

    result = []
    for o in orders:
        items = get_order_items(o["id"])
        result.append({
            "id": o["id"],
            "status": o["status"],
            "full_name": o["full_name"],
            "agent_name": o.get("agent_name", ""),
            "comment": o.get("comment", ""),
            "created_at": o["created_at"][:16],
            "items_count": len(items),
            "items": [
                {
                    "name": it["product_name"],
                    "quantity": it["quantity"],
                    "unit": it["unit"],
                }
                for it in items
            ],
        })

    return JSONResponse({"orders": result, "role": role})


@app.post("/api/orders/requests")
async def api_pending_requests(request: Request):
    """Заявки на отгрузку — только для boss/admin."""
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services.database import get_role
    role = get_role(user["id"])
    if role not in ("admin", "boss"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    from services.database import get_pending_requests, get_order, get_order_items
    requests = get_pending_requests()
    result = []
    for r in requests:
        order = get_order(r["order_id"])
        items = get_order_items(r["order_id"]) if order else []
        result.append({
            "id": r["id"],
            "order_id": r["order_id"],
            "full_name": r["full_name"],
            "status": r["status"],
            "created_at": r["created_at"][:16],
            "agent_name": order.get("agent_name", "") if order else "",
            "items": [
                {"name": it["product_name"], "quantity": it["quantity"], "unit": it["unit"]}
                for it in items
            ],
        })

    return JSONResponse({"requests": result})

# ─── API: создание заказа ────────────────────────────────────────────────────


@app.post("/api/orders/create")
async def api_create_order(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services.database import create_order, get_role
    role = get_role(user["id"])
    if role not in ("admin", "boss", "manager"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    full_name = (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("username", str(user["id"]))
    )
    order_id = create_order(user["id"], full_name, data.get("comment", ""))
    return JSONResponse({"order_id": order_id})


@app.post("/api/orders/add_item")
async def api_add_item(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services.database import add_order_item, get_order
    order = get_order(data["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")

    item_id = add_order_item(
        order_id=data["order_id"],
        product_name=data["product_name"],
        product_href=data.get("product_href", ""),
        quantity=float(data["quantity"]),
        unit=data.get("unit", "шт"),
        note=data.get("note", ""),
    )
    return JSONResponse({"item_id": item_id})


@app.post("/api/orders/remove_item")
async def api_remove_item(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services.database import remove_order_item
    remove_order_item(data["item_id"])
    return JSONResponse({"ok": True})


@app.post("/api/orders/set_agent")
async def api_set_agent(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services.database import update_order_agent, get_order
    order = get_order(data["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")

    update_order_agent(data["order_id"], data["agent_id"], data["agent_name"])
    return JSONResponse({"ok": True})


@app.post("/api/orders/submit")
async def api_submit_order(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services.database import (
        get_order, get_order_items, create_shipment_request,
        update_order_status, add_audit_log, get_role,
    )
    from services.notifier import get_notify_recipients
    from utils.formatters import DIV
    from handlers.orders import format_request_notify
    import aiohttp as _aiohttp
    from config import TELEGRAM_TOKEN

    order_id = data["order_id"]
    order = get_order(order_id)
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")
    if order["status"] != "draft":
        raise HTTPException(status_code=400, detail="Заказ уже отправлен")

    items = get_order_items(order_id)
    if not items:
        raise HTTPException(status_code=400, detail="Добавьте товары")
    if not order.get("agent_name"):
        raise HTTPException(status_code=400, detail="Выберите клиента")

    full_name = (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("username", str(user["id"]))
    )
    req_id = create_shipment_request(order_id, user["id"], full_name)
    update_order_status(order_id, "pending")
    add_audit_log(
        user["id"], full_name, get_role(user["id"]),
        "shipment_request_sent",
        f"Заявка #{req_id} (заказ #{order_id}) через WebApp",
    )

    # Уведомляем руководителей
    notify_text = format_request_notify(order, items, req_id)
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Одобрить",  "callback_data": f"req_ok:{req_id}"},
            {"text": "❌ Отклонить", "callback_data": f"req_no:{req_id}"},
        ]]
    }
    async with _aiohttp.ClientSession() as session:
        for uid in get_notify_recipients():
            try:
                await session.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={
                        "chat_id": uid,
                        "text": notify_text,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard,
                    },
                )
            except Exception:
                pass

    return JSONResponse({"req_id": req_id})


@app.post("/api/agents")
async def api_agents(request: Request):
    """Список клиентов (контрагентов) из МойСклад."""
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    import aiohttp as _aiohttp
    from config import MS_TOKEN
    MS_BASE_URL = "https://api.moysklad.ru/api/remap/1.2"
    headers = {"Authorization": f"Bearer {MS_TOKEN}", "Accept-Encoding": "gzip"}

    search = data.get("search", "")
    params = {"limit": 50, "order": "name"}
    if search:
        params["search"] = search

    async with _aiohttp.ClientSession() as session:
        async with session.get(
            f"{MS_BASE_URL}/entity/counterparty",
            headers=headers, params=params,
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()

    agents = [
        {
            "id": a.get("id", ""),
            "name": a.get("name", "—"),
            "phone": a.get("phone", ""),
        }
        for a in result.get("rows", [])
    ]
    return JSONResponse({"agents": agents})
# ─── Запуск ───────────────────────────────────────────────────────────────────

async def start_webapp():
    """Запустить FastAPI сервер в фоне."""
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("WebApp запускается на порту %d", port)
    await server.serve()
