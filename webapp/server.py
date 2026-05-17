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

# Раздаём статику (CSS, JS)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
    """История платежей текущего пользователя."""
    import sqlite3
    from services.database import DB_PATH

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    user_id = user["id"]

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, amount, currency, comment, status, created_at
            FROM payments
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return JSONResponse({"payments": rows})
    except Exception as e:
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

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def start_webapp():
    """Запустить FastAPI сервер в фоне."""
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("WebApp запускается на порту %d", port)
    await server.serve()
