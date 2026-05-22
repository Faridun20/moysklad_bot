"""
FastAPI сервер для WebApp.
Запускается параллельно с ботом.
"""

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from webapp.auth import verify_init_data

# Берём роль из in-memory кэша (TTL 60s) вместо SELECT'а на каждый API-запрос.
# `get_role` оставляем как имя для обратной совместимости с кодом ниже.
from services.roles import cached_role as get_role
from services.rate_limit import acquire as rate_limit_acquire


# Хранилище фоновых задач — предотвращает преждевременный GC до завершения.
_background_tasks: set[asyncio.Task] = set()


# ─── Idempotency cache ──────────────────────────────────────────────
# Защита от double-click на confirm/reject платежей. Клиент посылает
# `idempotency_key` (random UUID per действие); если запрос с тем же
# ключом приходит повторно в течение TTL — возвращаем кэшированный
# результат, не дёргая БД повторно.
# SECURITY.md (Medium): сейчас фронт может два раза кликнуть approve и
# во время загрузки получить рассинхронизированное состояние.
_IDEM_CACHE: dict[str, tuple[float, dict]] = {}
_IDEM_TTL = 30.0


def _idem_get(key: str | None) -> dict | None:
    if not key:
        return None
    entry = _IDEM_CACHE.get(key)
    if entry and time.monotonic() - entry[0] < _IDEM_TTL:
        return entry[1]
    return None


def _idem_set(key: str | None, value: dict) -> None:
    if not key:
        return
    # Простой GC при разрастании кэша — выкидываем протухшие записи.
    if len(_IDEM_CACHE) > 200:
        cutoff = time.monotonic() - _IDEM_TTL
        for k in list(_IDEM_CACHE.keys()):
            if _IDEM_CACHE[k][0] < cutoff:
                _IDEM_CACHE.pop(k, None)
    _IDEM_CACHE[key] = (time.monotonic(), value)


def _authorize(
    data: dict,
    allowed_roles: tuple[str, ...] = ("admin", "boss", "manager"),
    rate_limit_scope: str | None = None,
    rate_limit_max: int = 30,
    rate_limit_window: float = 60.0,
) -> dict:
    """
    Общая проверка для API endpoint'ов: валидируем initData и роль,
    опционально применяем per-user rate limit для дорогих эндпоинтов.

    Возвращает dict-юзера из Telegram. Бросает HTTPException на отказ.
    Используйте вместо того, чтобы дублировать verify_init_data +
    get_role + role-check + rate-limit в каждом endpoint'е (легко забыть).
    """
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")
    role = get_role(user["id"])
    if role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Нет доступа")
    if rate_limit_scope:
        if not rate_limit_acquire(rate_limit_scope, user["id"], rate_limit_max, rate_limit_window):
            raise HTTPException(
                status_code=429,
                detail="Слишком много запросов, подождите минуту",
            )
    return user


logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _compute_app_version() -> str:
    """
    Версия для cache-busting статики WebApp.
    Берём в порядке надёжности:
      1) Railway-переменная с SHA коммита (RAILWAY_GIT_COMMIT_SHA)
      2) короткий git SHA, если доступен .git
      3) unix-таймстамп старта процесса — гарантирует уникальность
         для каждого нового запуска даже без git.
    """
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT_SHA")
    if sha:
        return sha[:8]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=Path(__file__).parent.parent,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip() or str(int(time.time()))
    except Exception:
        return str(int(time.time()))


APP_VERSION = _compute_app_version()
logger.info("WebApp version: %s", APP_VERSION)

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


# ─── Telegram webhook (опционально) ──────────────────────────────────────────
#
# В webhook-режиме bot.py регистрирует dp+bot через set_telegram_dispatcher(),
# а Telegram POST'ит апдейты сюда. Без вызова этой функции endpoint вернёт
# 503 — это безопасно, потому что webhook у Telegram при этом не зарегистрирован.

_tg_bot = None
_tg_dispatcher = None


def set_telegram_dispatcher(bot, dispatcher) -> None:
    """Регистрирует aiogram Bot+Dispatcher для приёма webhook-апдейтов.
    Вызывается из bot.py при TG_USE_WEBHOOK=1."""
    global _tg_bot, _tg_dispatcher
    _tg_bot = bot
    _tg_dispatcher = dispatcher


# Отдельный Bot-инстанс для исходящих уведомлений из API-эндпоинтов
# (approve/reject заявок). Нужен, потому что order_workflow вызывает
# bot.send_message / bot.send_document / _push_payment_confirmation,
# которые требуют aiogram.Bot. В webhook-режиме переиспользуем уже
# созданный _tg_bot; иначе (BOT_MODE=webapp без webhook или BOT_MODE=all)
# создаём свой ленивый синглтон. Это просто API-клиент к Telegram —
# polling/dispatcher ему не нужны.
_notify_bot = None
_notify_bot_lock = asyncio.Lock()


async def get_notify_bot():
    """Вернуть aiogram.Bot для отправки уведомлений из API-эндпоинтов."""
    global _notify_bot
    if _tg_bot is not None:
        return _tg_bot
    if _notify_bot is None:
        async with _notify_bot_lock:
            if _notify_bot is None:
                from aiogram import Bot
                from config import TELEGRAM_TOKEN

                _notify_bot = Bot(token=TELEGRAM_TOKEN)
    return _notify_bot


async def close_notify_bot() -> None:
    """Закрыть собственный notify-bot (если создавали). _tg_bot не трогаем —
    его жизненным циклом управляет bot.py."""
    global _notify_bot
    if _notify_bot is not None:
        try:
            await _notify_bot.session.close()
        except Exception:
            pass
        _notify_bot = None


@app.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Принимает Update-объекты от Telegram.

    Защита: один и тот же TG_WEBHOOK_SECRET проверяется и в URL,
    и в заголовке `X-Telegram-Bot-Api-Secret-Token`. Это не «двойная
    защита» по энтропии — утечка секрета компрометирует обе точки.
    Заголовок отдаёт Telegram строго если secret_token был указан
    при `set_webhook`; URL же позволяет Railway маршрутизировать
    запрос. Проверки оба — это валидация что запрос не подменён
    каким-то прокси по пути (header может потеряться) и что путь
    не угадан случайно (без header'а можно слать что угодно по URL).
    """
    import hmac as _hmac
    from config import TG_WEBHOOK_SECRET

    if not TG_WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="not found")
    # constant-time сравнение от теоретического timing-attack
    if not _hmac.compare_digest(secret, TG_WEBHOOK_SECRET):
        raise HTTPException(status_code=404, detail="not found")
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not _hmac.compare_digest(header_secret, TG_WEBHOOK_SECRET):
        raise HTTPException(status_code=404, detail="not found")
    if _tg_dispatcher is None or _tg_bot is None:
        # Бот ещё не подключил себя сюда — режим webhook отключён.
        # 503 говорит Telegram «попробуй позже», без потери апдейта.
        raise HTTPException(status_code=503, detail="bot not ready")

    from aiogram.types import Update

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad payload")

    try:
        update = Update.model_validate(payload, context={"bot": _tg_bot})
        await _tg_dispatcher.feed_webhook_update(_tg_bot, update)
    except Exception:
        logger.exception("Ошибка обработки Telegram update")
        # 200 всё равно — иначе Telegram заретраит и засрёт лог
    return JSONResponse({"ok": True})


# ─── Webhook от МойСклад ─────────────────────────────────────────────────────


# Лимит payload MS-webhook'а — реалистично от МойСклад приходит ≤ 50KB
# (события батчатся). 1MB достаточно с большим запасом, при этом
# защищает от DoS: кто-то с известным секретом мог бы слать тяжёлые
# body, забивая нашу память.
_MS_WEBHOOK_MAX_BYTES = 1 * 1024 * 1024


def _new_demand_ids_from_events(events: list[dict]) -> list[str]:
    """demand_id новых отгрузок (action=CREATE, type demand/retaildemand) из
    payload MS-вебхука. Вынесено отдельно — чтобы тестировать дискриминацию
    событий без асинхронной fire-and-forget обвязки хендлера."""
    from utils.helpers import extract_id_from_href

    ids: list[str] = []
    for e in events:
        if e.get("action") != "CREATE":
            continue
        if (e.get("meta") or {}).get("type", "").lower() not in ("demand", "retaildemand"):
            continue
        did = extract_id_from_href((e.get("meta") or {}).get("href", ""))
        if did:
            ids.append(did)
    return ids


@app.post("/api/ms-webhook/{secret}")
async def ms_webhook(secret: str, request: Request):
    """
    МойСклад дёргает этот endpoint при изменениях документов, влияющих
    на остатки (demand/supply/loss/move/inventory). Секрет в URL —
    единственная защита от чужих POST-ов.

    Получив событие, помечаем stock как dirty. Фоновая корутина
    _stock_debounce_loop через несколько секунд сделает refresh_stock,
    батча все полученные события в один pull.
    """
    import hmac as _hmac
    from services.ms_webhooks import get_webhook_secret
    from services.snapshot import mark_stock_dirty

    # constant-time сравнение секрета (timing-attack hardening)
    if not _hmac.compare_digest(secret, get_webhook_secret()):
        # Не отдаём 401 — не подсказываем атакующему, что секрет нужен.
        raise HTTPException(status_code=404, detail="not found")

    # Лимит на размер тела (defence in depth — реальный МС шлёт ≤50KB)
    cl = request.headers.get("Content-Length")
    try:
        if cl and int(cl) > _MS_WEBHOOK_MAX_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
    except ValueError:
        pass

    try:
        body_bytes = await request.body()
        if len(body_bytes) > _MS_WEBHOOK_MAX_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        import json as _json

        payload = _json.loads(body_bytes) if body_bytes else {}
    except HTTPException:
        raise
    except Exception:
        payload = {}

    events = payload.get("events", []) if isinstance(payload, dict) else []
    if events:
        logger.info(
            "ms-webhook: %d event(s): %s",
            len(events),
            ", ".join(f"{e.get('action')}.{(e.get('meta') or {}).get('type')}" for e in events[:5]),
        )

        from services.ms_webhooks import STOCK_SUBSCRIPTIONS

        _stock_types = {s[0] for s in STOCK_SUBSCRIPTIONS}

        # Остатки: только события от складских документов
        stock_events = [
            e for e in events if (e.get("meta") or {}).get("type", "").lower() in _stock_types
        ]
        if stock_events:
            mark_stock_dirty()
            # Документ изменился → читалки (get_sales_stats, get_shipments,
            # позиции) могут отдавать устаревшие данные. Сбрасываем все
            # TTL-кэши МС, чтобы следующее открытие «Аналитики» увидело
            # свежие цифры.
            from services.moysklad import invalidate_ms_cache

            invalidate_ms_cache()

        # Платежи / заказы покупателя: синхронизируем локальные данные
        from services.ms_sync_handler import handle_ms_events

        _task = asyncio.create_task(handle_ms_events(events))
        _background_tasks.add(_task)
        _task.add_done_callback(_background_tasks.discard)

        # Новые отгрузки → уведомляем boss/admin МГНОВЕННО (раньше это делал
        # поллер раз в N секунд, отсюда задержка до нескольких минут). Дедуп
        # внутри notify_new_shipment не даст задвоить с поллером-резервом.
        from services.notifier import notify_new_shipment

        for did in _new_demand_ids_from_events(events):
            _nt = asyncio.create_task(notify_new_shipment(did))
            _background_tasks.add(_nt)
            _nt.add_done_callback(_background_tasks.discard)

    # МойСклад ждёт 200 быстро, иначе ретраит. Сам рефреш делаем в фоне.
    return JSONResponse({"ok": True, "received": len(events)})


# ─── Health-check ────────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz():
    """Лёгкий ping-endpoint для Railway-мониторинга и uptime-чекеров.
    Не задевает БД и МойСклад — отвечает быстро даже если они лежат,
    чтобы внешний мониторинг видел: HTTP-слой жив, паника не общая."""
    import time as _t

    return JSONResponse({"ok": True, "version": APP_VERSION, "ts": int(_t.time())})


# ─── Главная страница ─────────────────────────────────────────────────────────


_INDEX_HTML_CACHE: tuple[float, str] | None = None  # (mtime, html)


def _read_index_html() -> str:
    """Читаем index.html и подставляем версию для cache-busting.

    Кэшируем по mtime файла: при `hot-reload` локально (uvicorn --reload
    редактирует index.html) — мы это сразу подхватим. На проде файл
    не меняется в рантайме, mtime стабилен — никакой overhead'а.
    """
    global _INDEX_HTML_CACHE
    path = STATIC_DIR / "index.html"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # Файла нет — отдаём кэшированный (если есть) или пустую заглушку
        return _INDEX_HTML_CACHE[1] if _INDEX_HTML_CACHE else ""
    if _INDEX_HTML_CACHE is None or _INDEX_HTML_CACHE[0] != mtime:
        raw = path.read_text(encoding="utf-8")
        html = raw.replace("{{VERSION}}", APP_VERSION)
        _INDEX_HTML_CACHE = (mtime, html)
    return _INDEX_HTML_CACHE[1]


@app.get("/", response_class=HTMLResponse)
async def index():
    """Отдаём главную HTML страницу.
    Cache-Control: no-cache гарантирует, что браузер всегда проверит свежесть
    HTML — но статика (CSS/JS) по-прежнему кэшируется надолго через
    версионированные URL."""
    return HTMLResponse(
        _read_index_html(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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

    return JSONResponse(
        {
            "user_id": user_id,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "role": role,
        }
    )


# ─── API: главный экран (сводка дня + мои заказы + для босса аналитика) ────


@app.post("/api/home")
async def api_home(request: Request):
    """
    Главный экран WebApp — разный для ролей.

    Менеджер видит ТОЛЬКО свои данные:
      - today: его выручка/отгрузки/клиенты за сегодня (из локальной БД)
      - my_orders: его заказы (по статусам + последние 5)

    Босс/админ видит общую картину:
      - today: общая выручка/отгрузки/клиенты по всему МойСклад
      - my_orders: его собственные заказы
      - pending_requests: количество заявок ожидающих апрува
      - top_employees: лидерборд за неделю
    """
    from datetime import datetime, timedelta
    from services.moysklad import get_sales_stats, get_shipments
    from services import async_db as adb

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    user_id = user["id"]
    role = get_role(user_id)
    if role not in ("admin", "boss", "manager"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    # ВАЖНО про TZ: now_str() в services/database пишет datetime.now() —
    # это LOCAL-время сервера (на Railway обычно UTC, но если в env
    # стоит TZ=Asia/Tashkent — будет +5). Чтобы сравнения с DB timestamp'ами
    # совпадали, читаем «now» тем же способом, что и пишем. Раньше тут был
    # datetime.utcnow() — в результате сегодняшние заказы выпадали из окна
    # на пару часов.
    now = datetime.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    # Заказы текущего юзера — нужны и менеджеру (сводка), и боссу (его лично).
    # await-вызов через async_db не блокирует event loop на время SQL.
    my_orders = await adb.get_user_orders(user_id)
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

    is_boss = role in ("admin", "boss")

    # ─── Сегодня ──────────────────────────────────────
    if is_boss:
        # Босс видит общую выручку компании
        try:
            today_stats = await get_sales_stats(start_of_day, now)
        except Exception as e:
            logger.warning("home: failed to load today stats: %s", e)
            today_stats = {"total": 0, "count": 0, "clients": 0, "top_products": []}
        today = {
            "revenue": today_stats["total"] / 100,
            "shipments": today_stats["count"],
            "clients": today_stats["clients"],
            "scope": "company",
        }
    else:
        # Менеджер: считаем личные показатели из локальных одобренных заявок.
        # Источник — наша БД, без обращения к МойСклад. Батч-запросом
        # подтягиваем сразу все позиции (раньше был N+1 по заказам).
        today_iso = start_of_day.strftime("%Y-%m-%d")
        relevant_today = [
            o
            for o in my_orders
            if o["status"] in ("approved", "shipped")
            and (o.get("updated_at") or o.get("created_at") or "")[:10] == today_iso
        ]
        items_by_order = (
            await adb.get_order_items_by_ids([o["id"] for o in relevant_today])
            if relevant_today
            else {}
        )
        my_today_revenue = 0.0
        my_today_clients: set[str] = set()
        for o in relevant_today:
            items = items_by_order.get(o["id"], [])
            my_today_revenue += sum(
                float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items
            )
            if o.get("agent_name"):
                my_today_clients.add(o["agent_name"])
        today = {
            "revenue": my_today_revenue,
            "shipments": len(relevant_today),
            "clients": len(my_today_clients),
            "scope": "personal",
        }

    from config import BASE_CURRENCY

    result = {
        "role": role,
        "today": today,
        "my_orders": {
            "draft": orders_by_status["draft"],
            "pending": orders_by_status["pending"],
            "approved": orders_by_status["approved"],
            "rejected": orders_by_status["rejected"],
            "total": len(my_orders),
            "recent": recent,
        },
        "ms_linked": bool(await adb.get_moysklad_employee_id(user_id)),
        "currency": BASE_CURRENCY,
    }

    if is_boss:
        pending = await adb.get_pending_requests()
        result["pending_requests"] = len(pending)

        # Топ-сотрудники: группируем по нашему кастомному атрибуту
        # telegram_full_name (его проставляет ms_demand при создании
        # отгрузки из бота). Если атрибута нет — попадаем в "Прочее /
        # МойСклад" (отгрузки, заведённые вручную через веб МойСклад).
        # Раньше группировали по `owner` — это техническая учётная запись
        # МойСклад API-токена → все отгрузки прилипали к одному имени.
        try:
            week_shipments = await get_shipments(week_ago, now)
            by_manager: dict[str, dict] = {}
            for s in week_shipments:
                tg_name = _extract_tg_attribute(s, "telegram_full_name")
                if not tg_name:
                    tg_name = "Прочее (вручную в МойСклад)"
                cur = by_manager.setdefault(tg_name, {"sum": 0, "count": 0})
                cur["sum"] += s.get("sum", 0) or 0
                cur["count"] += 1
            top_emp = sorted(by_manager.items(), key=lambda kv: kv[1]["sum"], reverse=True)[:5]
            result["top_employees"] = [
                {"name": name, "revenue": d["sum"] / 100, "count": d["count"]}
                for name, d in top_emp
            ]
        except Exception as e:
            logger.warning("home: failed to load top employees: %s", e)
            result["top_employees"] = []

    return JSONResponse(result)


def _extract_tg_attribute(demand: dict, attr_name: str) -> str | None:
    """Найти значение нашего кастомного атрибута в demand-документе.
    МойСклад возвращает attributes inline в виде
    [{"name": "...", "value": ...}, ...]."""
    attrs = demand.get("attributes") or []
    for a in attrs:
        if a.get("name") == attr_name:
            v = a.get("value")
            return str(v) if v not in (None, "") else None
    return None


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
            # href нужен чтобы при создании заявки через WebApp
            # позиция уехала в МойСклад demand с правильной ссылкой на товар
            "href": r.get("meta", {}).get("href", ""),
            "folder_id": extract_id_from_href(r.get("folder", {}).get("meta", {}).get("href", "")),
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
    """
    Аналитика продаж за период.

    Менеджер видит ТОЛЬКО свои показатели (из локальной БД).
    Босс/админ — общую по компании (из МойСклад API).
    """
    from datetime import datetime, timedelta
    from services.moysklad import get_sales_stats, get_shipments

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    user_id = user["id"]
    role = get_role(user_id)
    if role not in ("admin", "boss", "manager"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    period = data.get("period", "week")
    # Local-time чтобы совпадало с now_str() (см. /api/home комментарий).
    now = datetime.now()

    periods = {
        "week": (now - timedelta(weeks=1), now - timedelta(weeks=2), "Неделя"),
        "month": (now - timedelta(days=30), now - timedelta(days=60), "Месяц"),
        "3month": (now - timedelta(days=90), now - timedelta(days=180), "3 месяца"),
        "year": (now - timedelta(days=365), now - timedelta(days=730), "Год"),
    }
    since, prev_since, label = periods.get(period, periods["month"])

    if role == "manager":
        # Личная аналитика — считаем из локальной БД по одобренным заявкам.
        return JSONResponse(await _personal_analytics(user_id, since, now, prev_since, label))

    # Босс/админ — компания, из МойСклад
    try:
        current, prev, shipments = await asyncio.gather(
            get_sales_stats(since, now),
            get_sales_stats(prev_since, since),
            get_shipments(since, now),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    by_day = [0] * 7
    for s in shipments:
        try:
            moment = s.get("moment", "")[:10]
            day_num = datetime.strptime(moment, "%Y-%m-%d").weekday()
            by_day[day_num] += 1
        except Exception:
            pass

    trend = 0
    if prev["total"] > 0:
        trend = round((current["total"] - prev["total"]) / prev["total"] * 100)

    top = [
        {"name": name, "sum": d["sum"] / 100, "qty": d["qty"]}
        for name, d in current["top_products"][:5]
    ]

    return JSONResponse(
        {
            "label": label,
            "scope": "company",
            "total": current["total"] / 100,
            "count": current["count"],
            "clients": current["clients"],
            "avg_check": (current["total"] / current["count"] / 100) if current["count"] else 0,
            "trend": trend,
            "by_day": [{"day": days_ru[i], "count": by_day[i]} for i in range(7)],
            "top_products": top,
        }
    )


def _ts(o: dict) -> str:
    """Достать timestamp заказа как строку YYYY-MM-DD HH:MM:SS.
    Защищаемся от случаев когда updated_at — datetime-объект (Postgres),
    None, или строка с T-разделителем — возвращаем единый формат."""
    raw = o.get("updated_at") or o.get("created_at") or ""
    if raw is None:
        return ""
    s = str(raw)
    # ISO с 'T' → пробел, чтобы сравнения работали единообразно
    if len(s) >= 11 and s[10] == "T":
        s = s[:10] + " " + s[11:]
    return s[:19]


async def _personal_analytics(
    user_id: int,
    since,
    until,
    prev_since,
    label: str,
) -> dict:
    """Личная аналитика менеджера из локальной БД (no МойСклад API).

    Все позиции грузятся одним батч-запросом — раньше был N+1 по
    заказам, что давало многосекундные задержки на Postgres.
    """
    from datetime import datetime
    from services import async_db as adb

    orders = await adb.get_user_orders(user_id)
    since_iso = since.strftime("%Y-%m-%d %H:%M:%S")
    until_iso = until.strftime("%Y-%m-%d %H:%M:%S")
    prev_since_iso = prev_since.strftime("%Y-%m-%d %H:%M:%S")

    # Берём все одобренные заказы, попавшие хоть в один из двух окон —
    # текущее [since, until] или предыдущее [prev_since, since].
    relevant = [
        o
        for o in orders
        if o["status"] in ("approved", "shipped") and prev_since_iso <= _ts(o) <= until_iso
    ]

    # Диагностический лог — увидим в Railway почему аналитика пуста,
    # если такое снова случится. Логируем только агрегаты, не PII.
    logger.info(
        "analytics user=%s role=manager orders=%d approved=%d relevant=%d "
        "period=[%s..%s] (prev_since=%s)",
        user_id,
        len(orders),
        sum(1 for o in orders if o["status"] in ("approved", "shipped")),
        len(relevant),
        since_iso,
        until_iso,
        prev_since_iso,
    )

    items_by_order = (
        await adb.get_order_items_by_ids([o["id"] for o in relevant]) if relevant else {}
    )

    def _agg(start_iso, end_iso):
        total = 0.0
        count = 0
        clients: set[str] = set()
        product_sums: dict[str, dict] = {}
        by_day = [0] * 7
        for o in relevant:
            ts = _ts(o)
            if ts < start_iso or ts > end_iso:
                continue
            items = items_by_order.get(o["id"], [])
            sub = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
            total += sub
            count += 1
            if o.get("agent_name"):
                clients.add(o["agent_name"])
            try:
                d = datetime.strptime(ts[:10], "%Y-%m-%d").weekday()
                by_day[d] += 1
            except Exception:
                pass
            for it in items:
                name = it.get("product_name", "—")
                qty = float(it.get("quantity", 0))
                price = float(it.get("price", 0) or 0)
                agg = product_sums.setdefault(name, {"sum": 0.0, "qty": 0.0})
                agg["sum"] += qty * price
                agg["qty"] += qty
        top = sorted(product_sums.items(), key=lambda kv: kv[1]["sum"], reverse=True)[:5]
        return total, count, len(clients), top, by_day

    cur_total, cur_count, cur_clients, cur_top, by_day = _agg(since_iso, until_iso)
    prev_total, prev_count, _, _, _ = _agg(prev_since_iso, since_iso)

    trend = round((cur_total - prev_total) / prev_total * 100) if prev_total > 0 else 0
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    return {
        "label": label,
        "scope": "personal",
        "total": cur_total,
        "count": cur_count,
        "clients": cur_clients,
        "avg_check": (cur_total / cur_count) if cur_count else 0,
        "trend": trend,
        "by_day": [{"day": days_ru[i], "count": by_day[i]} for i in range(7)],
        "top_products": [{"name": n, "sum": d["sum"], "qty": d["qty"]} for n, d in cur_top],
    }


# ─── API: платежи ─────────────────────────────────────────────────────────────


@app.post("/api/payments/history")
async def api_payments_history(request: Request):
    """История платежей текущего пользователя.

    Работает поверх get_conn(), поэтому одинаково корректно для SQLite
    и PostgreSQL — раньше эндпоинт жёстко звал sqlite3.connect(DB_PATH),
    и на Railway (где БД — Postgres, а DB_PATH указывает на ephemeral
    /tmp/payments.db) валился с «unable to open database file».
    """
    import asyncio
    from services.database import get_conn, get_cursor, q

    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    user_id = user["id"]

    def _load():
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
            return [dict(r) for r in cur.fetchall()]

    try:
        # to_thread не блокирует event loop, пока psycopg2 ждёт ответа БД
        rows = await asyncio.to_thread(_load)
        return JSONResponse({"payments": rows})
    except Exception as e:
        logger.exception("payments/history failed for user_id=%s", user_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/payments/pending")
async def api_payments_pending(request: Request):
    """Paid-заказы с pending-оплатой, ожидающие подтверждения боссом.

    Surface для бага «нет возможности подтвердить оплату в WebApp»:
    credit-долги видны в /api/debts, а paid-заказы — нет. Здесь отдаём
    именно paid, чтобы таб «Платежи» показал блок «На подтверждение».
    Confirm/reject — через существующие /api/orders/confirm_payment и
    /api/orders/reject_payment (принимают order_id).
    """
    from services import async_db as adb
    from config import BASE_CURRENCY

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),  # подтверждает только начальство
        rate_limit_scope="api_payments_pending",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )

    orders = await adb.get_paid_orders_awaiting_confirmation()
    order_ids = [o["id"] for o in orders]
    items_by_order = await adb.get_order_items_by_ids(order_ids) if order_ids else {}
    payments_by_order = await adb.get_payments_for_orders(order_ids) if order_ids else {}

    result = []
    for o in orders:
        items = items_by_order.get(o["id"], [])
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        payments = payments_by_order.get(o["id"], [])
        pending = sum(float(p["amount"]) for p in payments if p["status"] == "pending")
        result.append(
            {
                "order_id": o["id"],
                "agent_name": o.get("agent_name") or "—",
                "full_name": o.get("full_name") or "—",
                "currency": o.get("currency") or BASE_CURRENCY,
                "total": total,
                "pending": pending,
                "items_count": len(items),
                "created_at": (o.get("created_at") or "")[:16],
            }
        )

    return JSONResponse({"pending": result, "role": get_role(user["id"])})


@app.post("/api/payments/send")
async def api_payments_send(request: Request):
    """Отправить новый платёж на подтверждение."""
    from services import async_db as adb
    from services.notifier import tg_send_message
    from utils.formatters import format_payment_notify

    data = await request.json()
    # Платежи отправляют только менеджеры (и админ для тестов). Босс
    # эти платежи апрувит — отправлять ему нечего. Раньше эндпоинт
    # принимал boss и спамил его же бесполезными уведомлениями.
    # Rate-limit жёсткий: 5 платежей в минуту на пользователя.
    user = _authorize(
        data,
        allowed_roles=("admin", "manager"),
        rate_limit_scope="api_payments_send",
        rate_limit_max=5,
        rate_limit_window=60.0,
    )

    try:
        amount = float(data.get("amount", 0))
        if amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверная сумма")

    from config import ALLOWED_CURRENCIES

    currency = data.get("currency", "USD")
    if currency not in ALLOWED_CURRENCIES:
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

    # Сохраняем в БД (через async-обёртку — не блокируем event loop)
    payment_id = await adb.add_payment(user_id, username, full_name, amount, currency, comment)

    # Аудит
    await adb.add_audit_log(
        user_id,
        full_name,
        get_role(user_id),
        "payment_sent",
        f"Платёж #{payment_id}: {amount:,.0f} {currency} — {comment}",
    )

    # Уведомляем админов через Telegram API напрямую
    notify_text = format_payment_notify(payment_id, full_name, username, amount, currency, comment)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Принять", "callback_data": f"pay_ok:{payment_id}"},
                {"text": "❌ Отклонить", "callback_data": f"pay_no:{payment_id}"},
            ]
        ]
    }

    from services.notifier import aget_notify_recipients

    recipients = await aget_notify_recipients()

    # tg_send_message переиспользует общую ClientSession — никакого
    # TCP+TLS-рукопожатия на каждое уведомление.
    for uid in recipients:
        await tg_send_message(uid, notify_text, reply_markup=keyboard)

    return JSONResponse({"payment_id": payment_id, "status": "pending"})


# ─── API: заказы ─────────────────────────────────────────────────────────────


@app.post("/api/orders")
async def api_orders(request: Request):
    """Список заказов текущего пользователя."""
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services import async_db as adb

    role = get_role(user["id"])

    if role in ("admin", "boss"):
        orders = await adb.get_all_orders()
    else:
        orders = await adb.get_user_orders(user["id"])

    from config import BASE_CURRENCY

    # Батч-загрузка позиций: один SQL вместо N (N+1 был на больших списках)
    items_by_order = await adb.get_order_items_by_ids([o["id"] for o in orders]) if orders else {}
    result = []
    for o in orders:
        items = items_by_order.get(o["id"], [])
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        result.append(
            {
                "id": o["id"],
                "status": o["status"],
                "full_name": o["full_name"],
                "agent_name": o.get("agent_name", ""),
                "comment": o.get("comment", ""),
                "currency": o.get("currency") or BASE_CURRENCY,
                # Поля долга — фронт показывает «В долг до X» или «Оплачено»
                # на карточке заказа. paid_at=null + payment_type=credit
                # значит ещё не закрыт.
                "payment_type": o.get("payment_type") or "paid",
                "due_date": o.get("due_date"),
                "paid_at": (o.get("paid_at") or "")[:16] if o.get("paid_at") else None,
                "paid_confirmed_at": (o.get("paid_confirmed_at") or "")[:16]
                if o.get("paid_confirmed_at")
                else None,
                "created_at": o["created_at"][:16],
                "items_count": len(items),
                "total": total,
                "items": [
                    {
                        "name": it["product_name"],
                        "quantity": it["quantity"],
                        "unit": it["unit"],
                        "price": float(it.get("price", 0) or 0),
                    }
                    for it in items
                ],
            }
        )

    return JSONResponse({"orders": result, "role": role, "default_currency": BASE_CURRENCY})


@app.post("/api/orders/requests")
async def api_pending_requests(request: Request):
    """Заявки на отгрузку — только для boss/admin."""
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    role = get_role(user["id"])
    if role not in ("admin", "boss"):
        raise HTTPException(status_code=403, detail="Нет доступа")

    from services import async_db as adb

    requests = await adb.get_pending_requests()
    # Батч-загрузка заказов и позиций — один SQL на каждое вместо 2N.
    order_ids = [r["order_id"] for r in requests]
    orders_by_id = await adb.get_orders_by_ids(order_ids) if order_ids else {}
    items_by_order = await adb.get_order_items_by_ids(order_ids) if order_ids else {}
    result = []
    for r in requests:
        order = orders_by_id.get(r["order_id"])
        items = items_by_order.get(r["order_id"], []) if order else []
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        result.append(
            {
                "id": r["id"],
                "order_id": r["order_id"],
                "full_name": r["full_name"],
                "status": r["status"],
                "created_at": r["created_at"][:16],
                "agent_name": order.get("agent_name", "") if order else "",
                "total": total,
                "items": [
                    {
                        "name": it["product_name"],
                        "quantity": it["quantity"],
                        "unit": it["unit"],
                        "price": float(it.get("price", 0) or 0),
                    }
                    for it in items
                ],
            }
        )

    return JSONResponse({"requests": result})


@app.post("/api/requests/approve")
async def api_approve_request(request: Request):
    """Босс одобряет заявку на отгрузку из WebApp.

    Вся логика (атомарный UPDATE, создание customerorder+demand в
    МойСклад, уведомление менеджера, PDF, авто-payment для paid-заказов)
    инкапсулирована в services.order_workflow.approve_shipment_request —
    тот же код, что вызывает Telegram-callback `req_ok:`.
    """
    from services.order_workflow import approve_shipment_request

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_approve_request",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    try:
        req_id = int(data.get("req_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="req_id обязателен")

    boss_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    bot = await get_notify_bot()
    result = await approve_shipment_request(req_id, user["id"], boss_name, bot)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse({"ok": True, "req_id": req_id})


@app.post("/api/requests/reject")
async def api_reject_request(request: Request):
    """Босс отклоняет заявку на отгрузку из WebApp."""
    from services.order_workflow import reject_shipment_request

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_reject_request",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    try:
        req_id = int(data.get("req_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="req_id обязателен")

    boss_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    bot = await get_notify_bot()
    result = await reject_shipment_request(req_id, user["id"], boss_name, bot)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse({"ok": True, "req_id": req_id})


# ─── API: кредитные лимиты (IMPLEMENTATION.md §3) ─────────────────────────────


@app.post("/api/credit/overview")
async def api_credit_overview(request: Request):
    """Сводка по контрагентам: лимит + текущий долг + свободный остаток.
    Только начальство. Логика — services.database.get_credit_overview."""
    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_credit_overview",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    agents = await adb.get_credit_overview()
    return JSONResponse({"ok": True, "agents": agents})


@app.post("/api/credit/set")
async def api_credit_set(request: Request):
    """Установить кредитный лимит контрагента. Только начальство."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_credit_set",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    agent_id = (data.get("agent_id") or "").strip()
    agent_name = (data.get("agent_name") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id обязателен")
    try:
        limit_amount = float(data.get("limit_amount"))
        if limit_amount < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit_amount должен быть числом >= 0")

    await adb.set_credit_limit(
        agent_id, agent_name, limit_amount, set_by=user["id"], notes="WebApp"
    )
    return JSONResponse({"ok": True, "agent_id": agent_id, "limit_amount": limit_amount})


# ─── API: сдачи наличных (IMPLEMENTATION.md §7) ───────────────────────────────


@app.post("/api/deposits/pending")
async def api_deposits_pending(request: Request):
    """Сдачи, ждущие подтверждения, с привязанными заказами. admin/boss/bookkeeper."""
    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "bookkeeper"),
        rate_limit_scope="api_deposits_pending",
    )
    deposits = await adb.get_pending_cash_deposits()
    for d in deposits:
        d["orders"] = await adb.get_cash_deposit_orders(d["id"])
    return JSONResponse({"ok": True, "deposits": deposits})


@app.post("/api/deposits/confirm")
async def api_deposits_confirm(request: Request):
    """Подтвердить сдачу. Покрытые заказы → paid; уведомляем менеджера."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "bookkeeper"),
        rate_limit_scope="api_deposits_confirm",
    )
    try:
        deposit_id = int(data.get("deposit_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="deposit_id обязателен")

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    dep = await adb.get_cash_deposit(deposit_id)
    res = await adb.confirm_cash_deposit(deposit_id, user["id"], name)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "уже обработано"))

    if dep and dep.get("manager_id"):
        closed = res.get("closed_orders") or []
        extra = f" Закрыты заказы: {', '.join('#' + str(o) for o in closed)}." if closed else ""
        bot = await get_notify_bot()
        try:
            await bot.send_message(
                dep["manager_id"], f"✅ Ваша сдача #{deposit_id} подтверждена.{extra}"
            )
        except Exception:
            logger.warning("deposit confirm notify failed", exc_info=True)
    return JSONResponse(
        {"ok": True, "deposit_id": deposit_id, "closed_orders": res.get("closed_orders", [])}
    )


@app.post("/api/deposits/reject")
async def api_deposits_reject(request: Request):
    """Отклонить сдачу с причиной; уведомляем менеджера."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "bookkeeper"),
        rate_limit_scope="api_deposits_reject",
    )
    try:
        deposit_id = int(data.get("deposit_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="deposit_id обязателен")
    reason = (data.get("reason") or "").strip()
    if len(reason) < 3:
        raise HTTPException(status_code=400, detail="Причина обязательна")

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    dep = await adb.get_cash_deposit(deposit_id)
    res = await adb.reject_cash_deposit(deposit_id, user["id"], name, reason)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "уже обработано"))

    if dep and dep.get("manager_id"):
        from utils.helpers import esc

        bot = await get_notify_bot()
        try:
            await bot.send_message(
                dep["manager_id"],
                f"❌ Ваша сдача #{deposit_id} отклонена.\nПричина: {esc(reason)}",
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("deposit reject notify failed", exc_info=True)
    return JSONResponse({"ok": True, "deposit_id": deposit_id})


# ─── API: возвраты (IMPLEMENTATION.md §8) ─────────────────────────────────────


@app.post("/api/returns/pending")
async def api_returns_pending(request: Request):
    """Возвраты на подтверждении. admin/boss/warehouse_keeper."""
    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "warehouse_keeper"),
        rate_limit_scope="api_returns_pending",
    )
    returns = await adb.get_pending_returns()
    return JSONResponse({"ok": True, "returns": returns})


@app.post("/api/returns/confirm")
async def api_returns_confirm(request: Request):
    """Подтвердить возврат (статус заказа → returned/partially_returned)."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_returns_confirm",
    )
    try:
        return_id = int(data.get("return_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="return_id обязателен")

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    res = await adb.confirm_return(return_id, user["id"], name)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "уже обработано"))
    return JSONResponse(
        {"ok": True, "return_id": return_id, "order_status": res.get("order_status")}
    )


# ─── API: создание заказа ────────────────────────────────────────────────────


@app.post("/api/orders/create")
async def api_create_order(request: Request):
    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_orders_create",
        rate_limit_max=10,
        rate_limit_window=60.0,
    )

    from services import async_db as adb

    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    order_id = await adb.create_order(user["id"], full_name, data.get("comment", ""))
    return JSONResponse({"order_id": order_id})


@app.post("/api/orders/add_item")
async def api_add_item(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services import async_db as adb

    order = await adb.get_order(data["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")

    try:
        price = float(data.get("price", 0) or 0)
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Неверная цена")

    # Если в payload пришла валюта и она ещё не зафиксирована на ордере —
    # сохраняем. Все позиции одного ордера должны быть в одной валюте.
    from config import ALLOWED_CURRENCIES

    requested_currency = (data.get("currency") or "").upper()
    if requested_currency and requested_currency in ALLOWED_CURRENCIES:
        if not order.get("currency"):
            await adb.update_order_currency(data["order_id"], requested_currency)

    item_id = await adb.add_order_item(
        order_id=data["order_id"],
        product_name=data["product_name"],
        product_href=data.get("product_href", ""),
        quantity=float(data["quantity"]),
        unit=data.get("unit", "шт"),
        price=price,
        note=data.get("note", ""),
    )
    return JSONResponse({"item_id": item_id})


@app.post("/api/orders/remove_item")
async def api_remove_item(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services import async_db as adb

    item = await adb.get_order_item(data["item_id"])
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    order = await adb.get_order(item["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")
    await adb.remove_order_item(data["item_id"])
    return JSONResponse({"ok": True})


@app.post("/api/orders/set_agent")
async def api_set_agent(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services import async_db as adb

    order = await adb.get_order(data["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")

    await adb.update_order_agent(data["order_id"], data["agent_id"], data["agent_name"])
    return JSONResponse({"ok": True})


@app.post("/api/orders/submit")
async def api_submit_order(request: Request):
    data = await request.json()
    user = verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    from services import async_db as adb
    from services.notifier import aget_notify_recipients, tg_send_message
    from handlers.orders import format_request_notify

    order_id = data["order_id"]
    order = await adb.get_order(order_id)
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")
    from services.order_workflow import validate_transition

    err = validate_transition(order, "pending")
    if err:
        raise HTTPException(status_code=400, detail=err)

    items = await adb.get_order_items(order_id)
    if not items:
        raise HTTPException(status_code=400, detail="Добавьте товары")
    if not order.get("agent_name"):
        raise HTTPException(status_code=400, detail="Выберите клиента")

    # ─── Тип оплаты ─────────────────────────────────────────────
    # payment_type: 'paid' (по умолчанию, оплачено сразу) или 'credit'.
    # Для credit обязателен due_date в формате YYYY-MM-DD и не раньше
    # сегодняшнего дня (нельзя задать долг с прошедшей датой).
    payment_type = (data.get("payment_type") or "paid").lower()
    due_date = (data.get("due_date") or "").strip() or None
    if payment_type not in ("paid", "credit"):
        raise HTTPException(status_code=400, detail="Неверный тип оплаты")
    if payment_type == "credit":
        if not due_date:
            raise HTTPException(status_code=400, detail="Укажите дату возврата долга")
        try:
            from datetime import date

            parsed = date.fromisoformat(due_date)
            if parsed < date.today():
                raise HTTPException(
                    status_code=400,
                    detail="Дата возврата не может быть в прошлом",
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="Неверный формат даты (нужно YYYY-MM-DD)")
    # Фиксируем на заказе ДО создания shipment_request, чтобы апрув
    # босса видел уже актуальный тип оплаты.
    await adb.set_order_payment(order_id, payment_type, due_date)
    # Перечитаем — нужно для уведомления
    order = await adb.get_order(order_id)

    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    req_id = await adb.create_shipment_request(order_id, user["id"], full_name)
    await adb.update_order_status(order_id, "pending")
    await adb.add_audit_log(
        user["id"],
        full_name,
        get_role(user["id"]),
        "shipment_request_sent",
        f"Заявка #{req_id} (заказ #{order_id}) через WebApp",
    )

    # Уведомляем руководителей
    notify_text = format_request_notify(order, items, req_id)
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"req_ok:{req_id}"},
                {"text": "❌ Отклонить", "callback_data": f"req_no:{req_id}"},
            ]
        ]
    }
    for uid in await aget_notify_recipients():
        await tg_send_message(uid, notify_text, reply_markup=keyboard)

    return JSONResponse({"req_id": req_id})


@app.post("/api/agents")
async def api_agents(request: Request):
    """Список клиентов (контрагентов). Сначала из локального snapshot,
    если он пуст — fallback на live API."""
    from services import snapshot
    from services.moysklad import ms_get

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_agents",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )

    # Санитизация search (SECURITY.md H11): без неё manager мог через
    # подобранные search-запросы устроить пачку дорогих запросов в
    # МойСклад. Длина ≤ 50, только буквы/цифры/обычные знаки —
    # без специальных символов, которые МойСклад может интерпретировать.
    raw_search = (data.get("search", "") or "").strip()
    if len(raw_search) > 50:
        raw_search = raw_search[:50]
    # Whitelist: буквы (любые юникодные), цифры, пробелы, основные знаки
    import re as _re

    search = _re.sub(r"[^\w\s\-\.,'@+()/]", "", raw_search, flags=_re.UNICODE)
    rows = snapshot.get_counterparties(search=search if search else None, limit=50)
    if rows:
        return JSONResponse(
            {
                "agents": [
                    {"id": r["ms_id"], "name": r.get("name", "—"), "phone": r.get("phone", "")}
                    for r in rows
                ]
            }
        )

    # Snapshot пуст — live fallback + триггер первичного рефреша
    params = {"limit": 50, "order": "name"}
    if search:
        params["search"] = search
    result = await ms_get("entity/counterparty", params=params)
    asyncio.create_task(snapshot.refresh_counterparties())
    return JSONResponse(
        {
            "agents": [
                {"id": a.get("id", ""), "name": a.get("name", "—"), "phone": a.get("phone", "")}
                for a in result.get("rows", [])
            ]
        }
    )


# ─── API: долги (credit-заказы без paid_at) ─────────────────────────────────


@app.post("/api/debts")
async def api_debts(request: Request):
    """Список открытых долгов.

    Менеджер видит только свои долги, boss/admin — все.
    Фильтр `mode`:
      - all (default) — все открытые
      - today        — к оплате сегодня и просроченные (для главного экрана)

    Каждый долг возвращается с уже посчитанной суммой и пометкой
    overdue/due_today/upcoming, чтобы фронт не пересчитывал даты.
    """
    from datetime import date
    from services import async_db as adb
    from config import BASE_CURRENCY

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_debts",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    user_id = user["id"]
    role = get_role(user_id)
    is_boss = role in ("admin", "boss")

    mode = (data.get("mode") or "all").lower()
    today = date.today().isoformat()
    due_through = today if mode == "today" else None

    # Менеджер видит только свои; босс — все
    debts = await adb.get_open_debts(
        user_id=None if is_boss else user_id,
        due_through=due_through,
    )

    # Батчем: позиции для total + платежи для подсчёта confirmed/pending
    debt_ids = [d["id"] for d in debts]
    items_by_order = await adb.get_order_items_by_ids(debt_ids) if debt_ids else {}
    payments_by_order = await adb.get_payments_for_orders(debt_ids) if debt_ids else {}

    result = []
    for o in debts:
        items = items_by_order.get(o["id"], [])
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        payments = payments_by_order.get(o["id"], [])
        confirmed = sum(p["amount"] for p in payments if p["status"] == "confirmed")
        pending = sum(p["amount"] for p in payments if p["status"] == "pending")
        remaining = max(0.0, total - confirmed)
        due = o.get("due_date")
        # State:
        #  - awaiting_confirmation — есть pending payments (boss решает)
        #  - partial — есть confirmed, но ещё не всё (pending=0)
        #  - иначе по due_date: overdue/due_today/upcoming
        if pending > 0:
            state = "awaiting_confirmation"
        elif confirmed > 0:
            state = "partial"
        elif due:
            state = "overdue" if due < today else ("due_today" if due == today else "upcoming")
        else:
            state = "upcoming"
        result.append(
            {
                "id": o["id"],
                "user_id": o["user_id"],
                "agent_name": o.get("agent_name") or "—",
                "full_name": o.get("full_name") or "—",
                "due_date": due,
                "currency": o.get("currency") or BASE_CURRENCY,
                "total": total,
                "confirmed": confirmed,
                "pending": pending,
                "remaining": remaining,
                "items_count": len(items),
                "created_at": (o.get("created_at") or "")[:10],
                "paid_at": (o.get("paid_at") or "")[:16] if o.get("paid_at") else None,
                "state": state,
                "is_mine": o["user_id"] == user_id,
            }
        )

    # Сводка «получено / ожидает» — по сумме payments, а не по orders.
    # Берём ВСЕ payments (включая привязанные к закрытым заказам), потому
    # что money_received = «реально пришло за всё время», а не только по
    # открытым долгам. Менеджер — свои, босс — все.
    summary = await _money_summary(
        adb,
        user_id=None if is_boss else user_id,
    )

    return JSONResponse(
        {
            "debts": result,
            "role": role,
            "scope": "company" if is_boss else "personal",
            "today": today,
            "money_received": [{"currency": k, "total": v} for k, v in summary["received"].items()],
            "money_pending": [{"currency": k, "total": v} for k, v in summary["pending"].items()],
        }
    )


async def _money_summary(adb, user_id: int | None) -> dict:
    """Сводка денежных потоков: отдельные суммы по валютам.

    Считается по payments (а не по orders) — это даёт точные цифры
    при частичных оплатах. Менеджер видит свои payments, boss — все.
    """
    import asyncio
    from services.database import get_conn, get_cursor, q

    def _load():
        where = "WHERE 1=1"
        params: list = []
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)
        sql = (
            f"SELECT status, currency, SUM(amount) AS total "
            f"FROM payments {where} "
            f"GROUP BY status, currency"
        )
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(q(sql), params)
            return [dict(r) for r in cur.fetchall()]

    rows = await asyncio.to_thread(_load)
    received: dict[str, float] = {}
    pending: dict[str, float] = {}
    for r in rows:
        cur_ = r.get("currency") or "USD"
        amt = float(r.get("total") or 0)
        if r.get("status") == "confirmed":
            received[cur_] = received.get(cur_, 0.0) + amt
        elif r.get("status") == "pending":
            pending[cur_] = pending.get(cur_, 0.0) + amt
    return {"received": received, "pending": pending}


@app.post("/api/orders/mark_paid")
async def api_mark_paid(request: Request):
    """Закрыть долг по конкретному заказу.

    Право: автор заказа (тот менеджер, который его создал) ИЛИ
    boss/admin (override на случай если менеджер недоступен).
    Идемпотентно — повторный клик ничего не ломает.
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_mark_paid",
        rate_limit_max=20,
        rate_limit_window=60.0,
    )

    order_id = data.get("order_id")
    if not order_id:
        raise HTTPException(status_code=400, detail="order_id обязателен")
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id должен быть числом")

    order = await adb.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")

    user_id = user["id"]
    role = get_role(user_id)
    is_owner = order["user_id"] == user_id
    is_boss = role in ("admin", "boss")
    if not (is_owner or is_boss):
        raise HTTPException(status_code=403, detail="Нет доступа")

    if order.get("payment_type") != "credit":
        raise HTTPException(status_code=400, detail="Это не кредитный заказ")

    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user_id)
    )
    username = f"@{user['username']}" if user.get("username") else ""

    # amount: если передан и валиден — частичная оплата; иначе закроет остаток
    amount_raw = data.get("amount")
    amount = None
    if amount_raw is not None and amount_raw != "":
        try:
            amount = float(amount_raw)
            if amount <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Неверная сумма")

    ok, payment_id = await adb.mark_order_paid(
        order_id,
        user_id,
        full_name,
        amount=amount,
        username=username,
    )
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Не удалось создать платёж (возможно, заказ уже полностью оплачен)",
        )

    # Сразу шлём боссу push с inline-кнопками для approve.
    await _notify_bosses_payment_pending(order_id, full_name, payment_id)

    return JSONResponse({"ok": True, "payment_id": payment_id})


async def _notify_bosses_payment_pending(
    order_id: int,
    manager_name: str,
    payment_id: int | None,
) -> None:
    """Когда менеджер отметил частичную/полную оплату по credit-заказу —
    шлём push'ы всем boss/admin с кнопками confirm/reject через
    стандартный payment-approval flow (pay_ok/pay_no callbacks).
    Best-effort, тихо ловим ошибки."""
    from services import async_db as adb
    from services.notifier import aget_notify_recipients, tg_send_message

    try:
        order = await adb.get_order(order_id)
        if not order or not payment_id:
            return
        payment = await adb.get_payment(payment_id)
        if not payment:
            return
        summary = await adb.get_order_payment_summary(order_id)
        from config import BASE_CURRENCY

        currency = order.get("currency") or BASE_CURRENCY
        agent = order.get("agent_name") or "—"
        due = order.get("due_date") or "—"
        amount = float(payment.get("amount") or 0)
        fmt = lambda n: f"{int(round(n)):,}".replace(",", " ")
        # summary["remaining"] = total - confirmed (без учёта pending).
        # «Останется после подтверждения ЭТОГО платежа» =
        #   remaining - amount_of_this_payment.
        remaining_after = max(0.0, summary["remaining"] - amount)
        confirmed_before = max(0.0, summary["confirmed"])
        total = summary["total"]

        lines = [
            "💳 <b>Требуется подтверждение оплаты</b>",
            "",
            f"Заказ #{order_id}",
            f"👨‍💼 Менеджер: <b>{manager_name}</b>",
            f"🏢 Клиент: <b>{agent}</b>",
            f"💵 Сумма платежа: <b>{fmt(amount)} {currency}</b>",
            f"📦 По заказу всего: <b>{fmt(total)} {currency}</b>",
        ]
        if confirmed_before > 0:
            lines.append(f"✅ Уже оплачено ранее: <b>{fmt(confirmed_before)} {currency}</b>")
        if remaining_after <= 0:
            lines.append("🎉 Этот платёж <b>закрывает долг полностью</b>")
        else:
            lines.append(f"📎 Останется к получению: <b>{fmt(remaining_after)} {currency}</b>")
        lines.append(f"📅 Срок: {due}")
        lines.append("")
        lines.append("Подтвердите, что эта сумма реально пришла в кассу.")
        text = "\n".join(lines)
        # Используем СУЩЕСТВУЮЩИЕ pay_ok/pay_no callbacks — это
        # стандартный payment-approval flow в handlers/payments.py.
        # После approve платежа _maybe_close_order_after_payment
        # автоматически проверит, закрылся ли заказ.
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Принять", "callback_data": f"pay_ok:{payment_id}"},
                    {"text": "❌ Отклонить", "callback_data": f"pay_no:{payment_id}"},
                ]
            ]
        }
        for uid in await aget_notify_recipients():
            await tg_send_message(uid, text, reply_markup=keyboard)
    except Exception:
        logger.exception("Не удалось отправить уведомление о подтверждении оплаты #%s", order_id)


@app.post("/api/orders/confirm_payment")
async def api_confirm_payment(request: Request):
    """Босс подтверждает все pending платежи по заказу одной кнопкой.

    Подтверждает каждый payment через стандартный confirm_payment(),
    после каждого — _maybe_close_order_after_payment проверяет, не
    закрылся ли заказ полностью.
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),  # только начальство
        rate_limit_scope="api_confirm_payment",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )

    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")

    # Idempotency: если фронт прислал ключ (uuid от клиента) — пробуем
    # вернуть кэшированный результат на случай double-click.
    idem_key = data.get("idempotency_key")
    if idem_key:
        cached = _idem_get(f"confirm:{user['id']}:{idem_key}")
        if cached is not None:
            return JSONResponse(cached)

    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )

    # Берём список pending до confirm — после атомарного UPDATE мы не
    # знаем, КОГО именно нужно уведомить (только количество). Сохраняем
    # копии payment-dict'ов и шлём уведомления каждому владельцу.
    pending_before = [
        p for p in await adb.get_payments_for_order(order_id) if p["status"] == "pending"
    ]

    n = await adb.confirm_all_pending_payments_for_order(order_id, user["id"], full_name)

    # Уведомляем менеджеров о подтверждённых платежах. Если race с другим
    # боссом — count меньше длины pending_before, берём первые n.
    if n > 0:
        from services.notifier import tg_send_message
        from utils.formatters import format_payment_confirmed

        for p in pending_before[:n]:
            try:
                text = format_payment_confirmed(
                    float(p.get("amount") or 0),
                    p.get("currency") or "—",
                    p.get("comment") or "",
                )
                await tg_send_message(p["user_id"], text)
            except Exception:
                logger.exception(
                    "Не удалось уведомить менеджера %s о подтверждении платежа #%s",
                    p.get("user_id"),
                    p.get("id"),
                )

    result = {"ok": True, "confirmed_count": n}
    if idem_key:
        _idem_set(f"confirm:{user['id']}:{idem_key}", result)
    return JSONResponse(result)


@app.post("/api/orders/reject_payment")
async def api_reject_payment(request: Request):
    """Босс отклоняет все pending платежи по заказу. Заказ остаётся
    в долгах с тем что было до отклонения."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_reject_payment",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )

    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")

    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )

    # Аналогично confirm: сохраняем pending до UPDATE, чтобы знать кого
    # уведомить персонально (не только владельцу заказа — у каждого
    # платежа может быть свой user_id).
    pending_before = [
        p for p in await adb.get_payments_for_order(order_id) if p["status"] == "pending"
    ]

    n = await adb.reject_all_pending_payments_for_order(order_id, user["id"], full_name)

    if n > 0:
        from services.notifier import tg_send_message
        from utils.formatters import format_payment_rejected

        for p in pending_before[:n]:
            try:
                text = format_payment_rejected(
                    float(p.get("amount") or 0),
                    p.get("currency") or "—",
                    p.get("comment") or "",
                )
                await tg_send_message(p["user_id"], text)
            except Exception:
                logger.exception(
                    "Не удалось уведомить менеджера %s об отклонении платежа #%s",
                    p.get("user_id"),
                    p.get("id"),
                )

    return JSONResponse({"ok": True, "rejected_count": n})


@app.post("/api/orders/delete_draft")
async def api_delete_draft(request: Request):
    """Удалить черновик заказа (только владелец, только status='draft').

    Каскадно удаляет позиции. Возвращает 404 если заказа нет, 403 если
    не свой или уже не draft."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_delete_draft",
        rate_limit_max=20,
        rate_limit_window=60.0,
    )
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")

    ok = await adb.delete_order(order_id, user["id"])
    if not ok:
        raise HTTPException(
            status_code=403,
            detail="Нельзя удалить (не свой / уже не черновик / не существует)",
        )
    return JSONResponse({"ok": True})


# ─── Запуск ───────────────────────────────────────────────────────────────────


async def start_webapp():
    """Запустить FastAPI сервер в фоне."""
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("WebApp запускается на порту %d", port)
    await server.serve()
