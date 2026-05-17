"""
FastAPI сервер для WebApp.
Запускается параллельно с ботом.
"""

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


# ─── Запуск ───────────────────────────────────────────────────────────────────


async def start_webapp():
    """Запустить FastAPI сервер в фоне."""
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("WebApp запускается на порту %d", port)
    await server.serve()
