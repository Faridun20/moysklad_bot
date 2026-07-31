"""
FastAPI сервер для WebApp.
Запускается параллельно с ботом.
"""

import asyncio
import base64
import binascii
import logging
import math
import os
import re
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from utils.helpers import redact_token
from webapp.auth import verify_init_data

# Берём роль из in-memory кэша (TTL 60s) вместо SELECT'а на каждый API-запрос.
# `get_role` оставляем как имя для обратной совместимости с кодом ниже.
from services.roles import cached_role as get_role
from services.rate_limit import acquire as rate_limit_acquire
from services import money


# Хранилище фоновых задач — предотвращает преждевременный GC до завершения.
_background_tasks: set[asyncio.Task] = set()


def _spawn_bg(coro, name: str) -> asyncio.Task:
    """Запустить фоновую задачу: держим сильную ссылку (иначе GC может убить
    её до завершения) + логируем необработанное исключение (раньше fire-and-
    forget create_task падал молча)."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    def _log_exc(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            logger.error("Фоновая задача %s упала", name, exc_info=t.exception())

    task.add_done_callback(_log_exc)
    return task


# ─── Идемпотентность мутаций ─────────────────────────────────────────
# Ключ идемпотентности живёт в ОБЩЕЙ БД (таблица idempotency_keys), а не в
# памяти процесса.
#
# Был in-memory кэш с TTL 30 c. Он не переживал рестарт и не делился между
# воркерами uvicorn, поэтому на денежных ручках (mark_paid, approve,
# confirm_payment) защиты фактически не было: ретрай клиента после рестарта
# или запрос, попавший в другой воркер, проходил как новый — лишний платёж,
# лишний документ в МойСклад, двойное уведомление (T2.5).

_IDEM_KEY_MAX = 128  # WP-22: cap длины клиентского ключа (storage/memory DoS)


def _cap_idem_key(raw) -> str | None:
    """Ограничить длину клиентского idempotency_key (WP-22): ключ идёт в
    таблицу идемпотентности; неограниченный ключ от валидного юзера — вектор
    раздувания storage (cap по числу записей не ограничивает РАЗМЕР ключа).
    UUID укладывается в 128 с запасом."""
    if not raw:
        return None
    return str(raw)[:_IDEM_KEY_MAX]


class _Idem:
    """Claim → работа → store, с освобождением ключа при сбое.

    `claim()` возвращает сохранённый результат прошлого выполнения (тогда
    endpoint просто отдаёт его) либо None — значит ключ наш и надо работать.
    Если ключ занят, а результата ещё нет (операция в полёте или упала до
    store), поднимаем 409: безопаснее отказать, чем рискнуть дублем денег.

    Без ключа от клиента все методы — no-op, поведение как раньше.
    """

    __slots__ = ("_adb", "_key", "_op", "_uid")

    def __init__(self, adb, operation: str, user_id: int, raw_key):
        capped = _cap_idem_key(raw_key)
        self._adb = adb
        self._op = operation
        self._uid = user_id
        self._key = f"{operation}:{user_id}:{capped}" if capped else None

    @property
    def active(self) -> bool:
        return self._key is not None

    async def claim(self) -> dict | None:
        if not self._key:
            return None
        prev = await self._adb.idem_claim(self._key, self._op, self._uid)
        if prev is None:
            return None  # ключ наш
        if prev:
            return prev  # готовый результат прошлой попытки
        raise HTTPException(status_code=409, detail="Запрос уже обрабатывается")

    async def store(self, result: dict) -> None:
        if self._key:
            await self._adb.idem_store(self._key, result)

    async def release(self) -> None:
        """Освободить ключ — операция не состоялась, ретрай должен быть возможен."""
        if self._key:
            await self._adb.idem_release(self._key)


def _dev_bypass_user() -> dict | None:
    """ЛОКАЛЬНЫЙ обход Telegram-авторизации для визуальной отладки WebApp в
    обычном браузере (без подписанного initData). Возвращает синтетического
    юзера ИЛИ None (обход не активен / запрещён).

    Активируется ТОЛЬКО env-флагом DEV_AUTH_BYPASS ∈ {1,true,yes}.
    Жёсткий предохранитель: если задан DATABASE_URL (= прод/Postgres) — обход
    игнорируется с ERROR-логом, чтобы случайно выставленный на Railway флаг
    не отключил авторизацию денежного бэкенда. Роль юзера всё равно берётся из
    БД (get_role в _authorize) — сид-скрипт даёт DEV_USER_ID роль admin.

    Читаем os.environ напрямую (не через config): устойчиво к обоим путям
    конфига (config_local.py vs env-ветка config.py)."""
    if os.environ.get("DEV_AUTH_BYPASS", "").strip().lower() not in ("1", "true", "yes"):
        return None
    if os.environ.get("DATABASE_URL"):
        logger.error(
            "DEV_AUTH_BYPASS проигнорирован: задан DATABASE_URL (прод/Postgres) — "
            "обход авторизации запрещён вне локальной SQLite."
        )
        return None
    try:
        uid = int(os.environ.get("DEV_USER_ID") or "999000001")
    except ValueError:
        uid = 999000001
    return {"id": uid, "first_name": "Dev", "username": "dev"}


def _authorize(
    data: dict,
    allowed_roles: tuple[str, ...] | None = ("admin", "boss", "manager"),
    rate_limit_scope: str | None = None,
    rate_limit_max: int = 30,
    rate_limit_window: float = 60.0,
) -> dict:
    """
    Общая проверка для API endpoint'ов: валидируем initData и роль,
    опционально применяем per-user rate limit для дорогих эндпоинтов.

    allowed_roles=None — роль НЕ проверяется (любой валидный Telegram-юзер):
    для эндпоинтов, которые сами скоупят данные по user_id (история своих
    платежей, свои заказы). Rate-limit при этом всё равно применяется.

    Возвращает dict-юзера из Telegram. Бросает HTTPException на отказ.
    Используйте вместо того, чтобы дублировать verify_init_data +
    get_role + role-check + rate-limit в каждом endpoint'е (легко забыть).
    """
    user = _dev_bypass_user() or verify_init_data(data.get("initData", ""))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Telegram data")
    # R1: деактивацию проверяем отдельно от роли — кэш ролей per-process с TTL,
    # деактивация из бот-процесса иначе не видна webapp до истечения TTL. Касается
    # и allowed_roles=None (свои-данные эндпоинты): уволенный не должен дёргать
    # даже их. Через короткий деакт-кэш (TTL 30с, инвалидируется при
    # deactivate/reactivate) — иначе это был бы SELECT на КАЖДЫЙ /api/* запрос.
    from services.roles import cached_is_deactivated

    if cached_is_deactivated(user["id"]):
        raise HTTPException(status_code=403, detail="Доступ деактивирован")
    if allowed_roles is not None:
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

# Однократное предупреждение, если активен локальный обход авторизации.
if _dev_bypass_user() is not None:
    logger.warning(
        "DEV_AUTH_BYPASS активен — Telegram-авторизация ОТКЛЮЧЕНА (локальная "
        "отладка). НЕ для прода: при DATABASE_URL обход сам себя глушит."
    )

app = FastAPI(title="МойСклад WebApp")

# Gzip: статика (app.js ~141KB, style.css ~59KB) и крупные JSON-ответы
# (/api/orders, /api/stock, /api/analytics) отдавались несжатыми — заметно на
# мобильном. minimum_size — не жмём мелочь, где оверхед сжатия не окупается.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=500)


# ─── Metrics middleware: латентность + status-code counters для /api/* ─────
#
# Цель — за ровно один хук покрыть все 50+ /api/* endpoint'ов. Раньше
# любой долгий response (например /api/analytics на холодном кэше) был
# «чёрным ящиком» — могли заметить только из Telegram-жалоб «WebApp
# тупит». Теперь — /api/metrics показывает p50/p95 на каждый endpoint.
@app.middleware("http")
async def _metrics_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    # Нормализация: убираем ID из путей типа /api/orders/123/items.
    # Сейчас наши endpoint'ы POST-only с body-параметрами, динамических
    # path-сегментов нет, нормализация не нужна. Если в будущем появятся —
    # добавить regex-подмена тут (?P<id>\d+ → '{id}').
    metric_name = path
    import time as _time

    from services import metrics as _metrics

    start = _time.perf_counter()
    try:
        response = await call_next(request)
        status = response.status_code
        if status >= 500:
            _metrics.incr(f"{metric_name}.5xx")
        elif status >= 400:
            _metrics.incr(f"{metric_name}.4xx")
        else:
            _metrics.incr(f"{metric_name}.ok")
        return response
    except Exception:
        _metrics.incr(f"{metric_name}.error")
        raise
    finally:
        _metrics.record_timing(metric_name, (_time.perf_counter() - start) * 1000.0)


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

        _spawn_bg(handle_ms_events(events), "handle_ms_events")

        # Новые отгрузки → уведомляем boss/admin МГНОВЕННО (раньше это делал
        # поллер раз в N секунд, отсюда задержка до нескольких минут). Дедуп
        # внутри notify_new_shipment не даст задвоить с поллером-резервом.
        from services.notifier import notify_new_shipment

        for did in _new_demand_ids_from_events(events):
            _spawn_bg(notify_new_shipment(did), f"notify_new_shipment:{did}")

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


@app.post("/api/metrics")
async def api_metrics(request: Request):
    """Снимок in-process метрик: counts, p50/p95, MS API latency, pool stats.

    Только admin/boss — содержит technical-info (URL'ы endpoint'ов,
    error counts), это не для рядового менеджера. Используется для
    диагностики «WebApp тупит» / «новая отгрузка не пришла».

    Возвращает JSON со структурой:
        {
          "uptime_sec": ...,
          "version": "...",
          "counters": {"/api/home.ok": 1234, "ms.create_demand.error": 2, ...},
          "timings": {"/api/home": {"count": N, "p50_ms": ..., "p95_ms": ...}, ...},
          "pool": {"used": N, "free": N, "max": N, "util_pct": N} | {}
        }
    """
    from services import metrics as _metrics
    from services.database import get_pool_stats

    data = await request.json()
    _authorize(data, allowed_roles=("admin", "boss"), rate_limit_scope="api_metrics")
    snap = _metrics.snapshot()
    snap["version"] = APP_VERSION
    snap["pool"] = await asyncio.to_thread(get_pool_stats)
    return JSONResponse(snap)


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
    # Через _authorize (allowed_roles=None) — валидирует initData И проверяет
    # деактивацию (WP-21): раньше /api/me звал verify_init_data напрямую, минуя
    # гейт деактивации → уволенный получал 200 (роль guest) вместо 403. Это
    # единственный аутентифицированный эндпоинт, обходивший проверку.
    user = _authorize(data, allowed_roles=None, rate_limit_scope="api_me", rate_limit_max=60)

    user_id = user["id"]
    role = get_role(user_id)
    from config import BASE_CURRENCY

    return JSONResponse(
        {
            "user_id": user_id,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "role": role,
            # Касса/сдачи хранятся в базовой валюте (нет currency-колонки) —
            # фронт показывает её код, а не хардкод «USD».
            "base_currency": (BASE_CURRENCY or "USD").upper(),
        }
    )


@app.post("/api/search")
async def api_search(request: Request):
    """Глобальный поиск по заказам / платежам / контрагентам.

    Менеджер видит только свои заказы и платежи (user_id-скоуп);
    boss/admin — все. Контрагенты — общий справочник (видны всем,
    они и так нужны для создания заказов).
    """
    from services import async_db as adb
    from services import snapshot

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_search",
    )
    query = (data.get("query") or "").strip()[:100]
    if not query:
        return JSONResponse({"ok": True, "orders": [], "payments": [], "agents": []})

    role = get_role(user["id"])
    # Менеджер — только свои; начальство — всё.
    scope_uid = user["id"] if role == "manager" else None

    orders = await adb.search_orders(query, user_id=scope_uid, limit=20)
    payments = await adb.search_payments(query, user_id=scope_uid, limit=20)
    agents = await asyncio.to_thread(snapshot.get_counterparties, query, 20)

    # Урезаем заказы/платежи до полезного для UI набора полей.
    orders_out = [
        {
            "id": o["id"],
            "status": o.get("status"),
            "agent_name": o.get("agent_name") or "—",
            "full_name": o.get("full_name") or "—",
            "currency": o.get("currency") or "",
            "created_at": (o.get("created_at") or "")[:16],
        }
        for o in orders
    ]
    payments_out = [
        {
            "id": p["id"],
            "amount": p.get("amount"),
            "currency": p.get("currency") or "",
            "status": p.get("status"),
            "full_name": p.get("full_name") or "—",
            "comment": (p.get("comment") or "")[:80],
            "order_id": p.get("order_id"),
        }
        for p in payments
    ]
    return JSONResponse(
        {"ok": True, "orders": orders_out, "payments": payments_out, "agents": agents}
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
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_home",
        rate_limit_max=120,
    )
    user_id = user["id"]
    role = get_role(user_id)

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
    # Заполняется только в boss-ветке ниже, но читается в отдельном boss-блоке
    # (лидерборд) — объявляем заранее, чтобы тип был определён (mypy has-type).
    week_shipments: list[dict] | BaseException = []

    # ─── Сегодня ──────────────────────────────────────
    if is_boss:
        # Босс видит общую выручку за сегодня + лидерборд за неделю. Оба источника
        # — независимые запросы к МойСклад; тянем их параллельно (раньше шли
        # последовательно через всю функцию — ~1с на двух round-trip'ах подряд).
        ms_results = await asyncio.gather(
            get_sales_stats(start_of_day, now),
            get_shipments(week_ago, now),
            return_exceptions=True,
        )
        today_stats = ms_results[0]
        week_shipments = ms_results[1]
        if isinstance(today_stats, BaseException):
            logger.warning("home: failed to load today stats: %s", today_stats)
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

        # Дашборд «Требует внимания» (фронт уже рендерит data.attention): счётчики
        # всего, что ждёт действия босса, чтобы он шёл в нужный раздел WebApp.
        # Дешёвые локальные SELECT'ы — последовательно (без конкурентности на пуле,
        # чтобы не споткнуться на одиночном соединении aiosqlite в тестах).
        # T2.13 (§3.8): COUNT(*) вместо четырёх полных SELECT * ради len().
        counts = await adb.count_boss_attention()
        result["attention"] = {
            "requests": len(pending),
            "payments": counts["payments"],
            "deposits": counts["deposits"],
            "returns": counts["returns"],
            "debts": counts["debts"],
        }

        # Топ-сотрудники из УЖЕ полученных недельных отгрузок (см. gather выше).
        # Группируем по кастомному атрибуту telegram_full_name (его проставляет
        # ms_demand при создании отгрузки из бота). Нет атрибута → "Прочее
        # (вручную в МойСклад)". Раньше группировали по `owner` — техническая
        # учётка API-токена, все отгрузки липли к одному имени.
        if isinstance(week_shipments, BaseException):
            logger.warning("home: failed to load top employees: %s", week_shipments)
            result["top_employees"] = []
        else:
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


# ─── API: операционная сводка ────────────────────────────────────────────────


@app.post("/api/ops-summary")
async def api_ops_summary(request: Request):
    """Операционная сводка для босса/админа: зависшие заявки, несданные деньги,
    складские алерты, здоровье cron, рассинхрон с МойСклад.

    Раньше это уходило большим дайджестом в Telegram (`run_ops_monitor`) — теперь
    смотрим в WebApp, а бот шлёт лишь короткий дневной пинг со ссылкой сюда.
    Всё — локальные запросы (без МС API); тяжёлый dead-stock исключён.
    """
    from services.ops_summary import gather_ops_summary

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_ops_summary",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    summary = await gather_ops_summary()
    return JSONResponse(summary)


# ─── API: остатки склада ─────────────────────────────────────────────────────


@app.post("/api/stock")
async def api_stock(request: Request):
    """Список товаров со склада."""
    from services.moysklad import get_all_stock, get_categories
    from utils.helpers import extract_id_from_href

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_stock",
        rate_limit_max=120,
    )
    role = get_role(user["id"])

    # МойСклад может быть недоступен (сеть/токен/5xx). Раньше это всплывало как
    # HTTP 500 с сырым «401 Unauthorized …» на экране «Каталог». Деградируем
    # мягко: пустой каталог + флаг ms_unavailable, фронт покажет подсказку.
    try:
        rows, cats = await asyncio.gather(
            get_all_stock(),
            get_categories(),
        )
    except Exception as e:
        logger.warning("stock: каталог МойСклад недоступен: %s", e)
        return JSONResponse({"products": [], "categories": [], "ms_unavailable": True})

    # PR C: подмешиваем цены руководства. sale_price — всем (менеджер
    # видит минимум и дефолт), cost_price — ТОЛЬКО boss/admin (себестоимость
    # не раскрываем менеджерам).
    from services import async_db as adb

    is_boss = role in ("admin", "boss")
    ms_ids = [extract_id_from_href(r.get("meta", {}).get("href", "")) for r in rows]
    prices = await adb.get_product_prices_by_ids([i for i in ms_ids if i])

    products = []
    for r, ms_id in zip(rows, ms_ids, strict=True):
        pp = prices.get(ms_id) if ms_id else None
        item = {
            "name": r.get("name", "—"),
            "stock": r.get("stock", 0),
            "reserve": r.get("reserve", 0),
            "unit": r.get("uom", {}).get("name", "шт"),
            # href нужен чтобы при создании заявки через WebApp
            # позиция уехала в МойСклад demand с правильной ссылкой на товар
            "href": r.get("meta", {}).get("href", ""),
            "folder_id": extract_id_from_href(r.get("folder", {}).get("meta", {}).get("href", "")),
            "folder_name": r.get("folder", {}).get("name", ""),
            "sale_price": (pp.get("sale_price") if pp else None),
        }
        if is_boss and pp:
            item["cost_price"] = pp.get("cost_price")
        products.append(item)

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
    from datetime import datetime

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_analytics",
        rate_limit_max=120,
    )
    user_id = user["id"]
    role = get_role(user_id)

    now = datetime.now()
    # PR D: произвольный диапазон. Если заданы since/until (ISO) — используем их
    # вместо preset'а. Иначе — пресет week/month/3month/year.
    since, until, prev_since, label = _resolve_analytics_period(data, now)

    if role == "manager":
        # Личная аналитика — считаем из локальной БД по одобренным заявкам.
        return JSONResponse(await _personal_analytics(user_id, since, until, prev_since, label))

    # Босс/админ — компания, из МойСклад
    payload = await _company_analytics_payload(since, until, prev_since, label)
    return JSONResponse(payload)


async def _company_analytics_payload(since, until, prev_since, label: str) -> dict:
    """Расчёт компанейской аналитики (boss/admin) из МойСклад.

    Вынесено из api_analytics, чтобы /api/analytics/export переиспользовал
    тот же расчёт. Включает маржу по топ-товарам (cost из product_prices),
    топ клиентов и топ менеджеров.
    """
    from datetime import datetime

    from services import async_db as adb
    from services.moysklad import get_sales_stats, get_shipments

    # МойСклад может быть недоступен (сеть, истёкший/битый токен, 5xx). Раньше
    # любое исключение тут всплывало как HTTP 500, и WebApp показывал «Не удалось
    # загрузить: Unexpected token … is not valid JSON» (500-боди — не JSON).
    # Деградируем мягко: пустые продажи + локальный топ-менеджеров всё равно
    # отдаём, плюс флаг ms_unavailable для подсказки в UI.
    _empty_stats: dict = {"total": 0, "count": 0, "clients": 0, "top_products": []}
    _ms_state = {"ok": True}

    async def _safe_call(coro, default, label):
        """Значение MS-вызова или default при сбое (сеть/токен/5xx) + флаг."""
        try:
            return await coro
        except Exception as e:  # noqa: BLE001 — сюда же ClientResponseError 4xx/5xx
            logger.warning("analytics: %s недоступен: %s", label, e)
            _ms_state["ok"] = False
            return default

    current, prev, shipments = await asyncio.gather(
        _safe_call(get_sales_stats(since, until), _empty_stats, "get_sales_stats"),
        _safe_call(get_sales_stats(prev_since, since), _empty_stats, "get_sales_stats(prev)"),
        _safe_call(get_shipments(since, until), [], "get_shipments"),
    )
    ms_unavailable = not _ms_state["ok"]

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

    # Маржа по топ-товарам. МС price — минорные (÷100); cost — мажорные
    # (как ввело руководство). cost None → profit не считаем.
    top_products = current["top_products"][:5]
    prod_ms_ids = [d.get("ms_id") for _n, d in top_products if d.get("ms_id")]
    costs = await adb.get_product_prices_by_ids(prod_ms_ids) if prod_ms_ids else {}
    top = []
    for name, d in top_products:
        revenue = d["sum"] / 100
        item = {"name": name, "sum": revenue, "qty": d["qty"]}
        cost_row = costs.get(d.get("ms_id") or "")
        cost = cost_row.get("cost_price") if cost_row else None
        if cost is not None:
            item["profit"] = round(revenue - float(cost) * d["qty"], 2)
            item["margin_known"] = True
        else:
            item["margin_known"] = False
        top.append(item)

    top_clients = [
        {"name": name, "revenue": d["sum"] / 100, "count": d["count"]}
        for name, d in current.get("top_clients", [])[:10]
    ]
    # Топ менеджеров — из ЛОКАЛЬНЫХ orders (надёжно), а не из МС-атрибута
    # telegram_full_name (он ставится лишь когда demand создал бот → раньше
    # список был почти всегда «Прочее (вручную)»). Группировка по orders.user_id.
    perf = await adb.get_manager_performance(
        since.strftime("%Y-%m-%d %H:%M:%S"), until.strftime("%Y-%m-%d %H:%M:%S")
    )
    top_managers = [
        {
            "name": m["full_name"],
            "revenue": m["revenue"],
            "revenue_by_currency": m.get("revenue_by_currency", []),
            "count": m["shipped"],
            "orders": m["orders_count"],
            "debt": m["debt"],
            "debt_by_currency": m.get("debt_by_currency", []),
            "returns": m["returns_count"],
        }
        for m in perf[:10]
    ]

    return {
        "label": label,
        "scope": "company",
        "total": current["total"] / 100,
        "count": current["count"],
        "clients": current["clients"],
        "avg_check": (current["total"] / current["count"] / 100) if current["count"] else 0,
        "trend": trend,
        "by_day": [{"day": days_ru[i], "count": by_day[i]} for i in range(7)],
        "top_products": top,
        "top_clients": top_clients,
        "top_managers": top_managers,
        "ms_unavailable": ms_unavailable,
    }


@app.post("/api/analytics/export")
async def api_analytics_export(request: Request):
    """Выгрузить аналитику в Excel и прислать файлом в Telegram. boss/admin only."""
    from datetime import datetime

    from services.excel_export import build_analytics_xlsx

    data = await request.json()
    user = _authorize(
        data, allowed_roles=("admin", "boss"), rate_limit_scope="api_analytics_export"
    )
    now = datetime.now()
    since, until, prev_since, label = _resolve_analytics_period(data, now)
    try:
        payload = await _company_analytics_payload(since, until, prev_since, label)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    xlsx_bytes = await asyncio.to_thread(build_analytics_xlsx, payload)
    fname = f"analytics-{(label or 'report').replace(' ', '_').replace('—', '-')[:40]}.xlsx"

    from aiogram.types import BufferedInputFile

    bot = await get_notify_bot()
    try:
        await bot.send_document(
            chat_id=user["id"],
            document=BufferedInputFile(xlsx_bytes, filename=fname),
            caption=f"📊 Аналитика · {label}",
        )
    except Exception:
        logger.exception("analytics export send_document failed")
        raise HTTPException(status_code=502, detail="Не удалось отправить файл в Telegram")
    return JSONResponse({"ok": True, "sent": True})


def _resolve_analytics_period(data: dict, now):
    """Вернуть (since, until, prev_since, label) для аналитики.

    Если в payload заданы since/until (ISO YYYY-MM-DD) — кастомный диапазон
    (prev_since = since − длительность, для trend). Иначе preset (until=now).
    Кастомный диапазон clamp'ится ≤366 дней.
    """
    from datetime import datetime, timedelta

    since_raw = (data.get("since") or "").strip()
    until_raw = (data.get("until") or "").strip()
    if since_raw and until_raw:
        try:
            since = datetime.strptime(since_raw[:10], "%Y-%m-%d")
            until = datetime.strptime(until_raw[:10], "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Даты в формате YYYY-MM-DD")
        if until <= since:
            raise HTTPException(status_code=400, detail="until должен быть позже since")
        span = until - since
        if span > timedelta(days=366):
            raise HTTPException(status_code=400, detail="Диапазон не больше года")
        # until с фронта — ЭКСКЛЮЗИВНАЯ граница (next-day-полночь), поэтому в метке
        # показываем ВЫБРАННЫЙ конец = until − 1 день (WP-20), иначе пользователь
        # видел день, который не выбирал (и сверка с МС «по N-е» расходилась).
        label_until = (until - timedelta(days=1)).strftime("%Y-%m-%d")
        label = f"{since_raw[:10]} — {label_until}"
        return since, until, since - span, label

    period = data.get("period", "week")
    # Календарные границы (а не скользящее окно «now − N дней»): «Неделя» — с
    # понедельника текущей недели, «Месяц» — с 1-го числа, «Год» — с 1 января.
    # prev_since — начало ПРЕДЫДУЩЕГО такого же периода (для тренда). until=now
    # (период «по сейчас»), так что текущая неделя/месяц считаются нарастающим
    # итогом, а сравниваются с целым предыдущим — это ожидаемо для дашборда.
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        since = day0 - timedelta(days=day0.weekday())          # понедельник
        prev_since = since - timedelta(weeks=1)
        label = "Неделя"
    elif period == "3month":
        # Начало квартала: 1-е число первого месяца квартала.
        q_first_month = ((now.month - 1) // 3) * 3 + 1
        since = day0.replace(day=1, month=q_first_month)
        prev_month = q_first_month - 3
        prev_year = since.year
        if prev_month <= 0:
            prev_month += 12
            prev_year -= 1
        prev_since = since.replace(year=prev_year, month=prev_month)
        label = "Квартал"
    elif period == "year":
        since = day0.replace(month=1, day=1)
        prev_since = since.replace(year=since.year - 1)
        label = "Год"
    else:  # month (дефолт)
        since = day0.replace(day=1)
        prev_year, prev_month = (since.year - 1, 12) if since.month == 1 else (since.year, since.month - 1)
        prev_since = since.replace(year=prev_year, month=prev_month)
        label = "Месяц"
    return since, now, prev_since, label


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

    from config import BASE_CURRENCY

    base_cur = (BASE_CURRENCY or "USD").upper()

    def _agg(start_iso, end_iso):
        # Деньги НЕ суммируем между валютами (USD + UZS + EUR — бессмысленно):
        # выручка и топ-товары группируются по валюте заказа. Раньше total был
        # одним числом и складывал разные валюты в мусор.
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        count = 0
        clients: set[str] = set()
        product_sums: dict[tuple[str, str], dict] = {}
        by_day = [0] * 7
        for o in relevant:
            ts = _ts(o)
            if ts < start_iso or ts > end_iso:
                continue
            cur = (o.get("currency") or base_cur).upper()
            items = items_by_order.get(o["id"], [])
            sub = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
            totals[cur] = totals.get(cur, 0.0) + sub
            counts[cur] = counts.get(cur, 0) + 1
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
                agg = product_sums.setdefault((name, cur), {"sum": 0.0, "qty": 0.0})
                agg["sum"] += qty * price
                agg["qty"] += qty
        return totals, counts, count, len(clients), product_sums, by_day

    cur_totals, cur_counts, cur_count, cur_clients, cur_products, by_day = _agg(since_iso, until_iso)
    prev_totals, _pc, _, _, _, _ = _agg(prev_since_iso, since_iso)

    # Выручка по валютам (сорт. по убыванию), тренд считается ПО КАЖДОЙ валюте
    # отдельно — иначе процент сравнивал бы несравнимые суммы.
    revenue = []
    for cur in sorted(cur_totals, key=lambda c: cur_totals[c], reverse=True):
        tot = cur_totals[cur]
        prev = prev_totals.get(cur, 0.0)
        tr = round((tot - prev) / prev * 100) if prev > 0 else 0
        revenue.append({"currency": cur, "total": tot, "count": cur_counts.get(cur, 0), "trend": tr})

    top_sorted = sorted(cur_products.items(), key=lambda kv: kv[1]["sum"], reverse=True)[:5]
    top_products = [
        {"name": n, "currency": c, "sum": d["sum"], "qty": d["qty"]} for (n, c), d in top_sorted
    ]

    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    return {
        "label": label,
        "scope": "personal",
        "revenue": revenue,
        "count": cur_count,
        "clients": cur_clients,
        "by_day": [{"day": days_ru[i], "count": by_day[i]} for i in range(7)],
        "top_products": top_products,
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
    user = _authorize(
        data,
        allowed_roles=None,  # любой валидный юзер — отдаём только его платежи
        rate_limit_scope="api_payments_history",
        rate_limit_max=120,
    )
    user_id = user["id"]

    def _load():
        with get_conn() as conn:
            cur = get_cursor(conn)
            cur.execute(
                q(
                    "SELECT id, amount_cents, currency, comment, status, created_at "
                    "FROM payments WHERE user_id = ? "
                    "ORDER BY created_at DESC LIMIT 50"
                ),
                (user_id,),
            )
            # amount (мажорные) — для контракта JSON, считаем из копеек.
            return [
                dict(r, amount=float(money.from_cents(int(r["amount_cents"] or 0))))
                for r in cur.fetchall()
            ]

    try:
        # to_thread не блокирует event loop, пока psycopg2 ждёт ответа БД
        rows = await asyncio.to_thread(_load)
        return JSONResponse({"payments": rows})
    except Exception as e:
        logger.exception("payments/history failed for user_id=%s", user_id)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cash/history")
async def api_cash_history(request: Request):
    """Единая лента движения денег (платежи + сдачи + возвраты) — для босса.
    Менеджер видит свою историю через /api/payments/history; здесь — общая
    картина «кто/когда/сколько», которой раньше не было."""
    from datetime import datetime

    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_cash_history",
        rate_limit_max=120,
    )
    # Период — как в /api/money/summary, чтобы лента и итог «Деньги» были за один
    # период (WP-11). Раньше лента всегда отдавала последние 80 движений за всё
    # время → под январским заголовком висели июньские платежи.
    now = datetime.now()
    since, until, _prev, _label = _resolve_analytics_period(data, now)
    rows = await adb.get_cash_history(
        80,
        since=since.strftime("%Y-%m-%d %H:%M:%S"),
        until=until.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return JSONResponse({"history": rows})


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
                # Превью позиций — босс видит, ЧТО подтверждает, без открытия заказа.
                "items": [
                    {"name": it["product_name"], "quantity": it["quantity"], "unit": it["unit"]}
                    for it in items[:3]
                ],
                "created_at": (o.get("created_at") or "")[:16],
            }
        )

    return JSONResponse({"pending": result, "role": get_role(user["id"])})


@app.post("/api/payments/unlinked")
async def api_payments_unlinked(request: Request):
    """Confirmed-платежи без order_id — кандидаты для ретроспективного линка.

    PR #43 (tech debt #3b): бухгалтер/босс видит «бытовые» платежи в кассе
    и понимает, что некоторые из них на самом деле — частичные оплаты
    конкретного заказа. Этот endpoint показывает список таких платежей;
    `/api/payments/link` потом привязывает.
    """
    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "bookkeeper"),
        rate_limit_scope="api_payments_unlinked",
    )
    limit = int(data.get("limit", 100))
    payments = await adb.get_unlinked_payments(limit=limit)
    return JSONResponse({"ok": True, "payments": payments})


@app.post("/api/payments/link")
async def api_payments_link(request: Request):
    """Ретроспективно привязать стендалон-платёж к заказу.

    Только admin/boss — изменяет финансовые связи, нужен audit-grade
    контроль (audit-запись пишется внутри link_payment_to_order).

    Payload: {"initData": "...", "payment_id": N, "order_id": M}
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_payments_link",
    )
    try:
        payment_id = int(data.get("payment_id"))
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="payment_id и order_id обязательны (числа)")
    res = await adb.link_payment_to_order(
        payment_id,
        order_id,
        linked_by=user["id"],
        linked_name=(user.get("first_name") or "") + " " + (user.get("last_name") or ""),
    )
    if not res.get("ok"):
        # 409 Conflict для race-кейса (платёж уже привязан); 400 для
        # валидационных (платежа/заказа нет).
        err = res.get("error", "Не удалось привязать")
        status = 409 if "уже" in err or "Параллельная" in err else 400
        raise HTTPException(status_code=status, detail=err)
    return JSONResponse(res)


def _payment_identity(user: dict) -> tuple[int, str, str]:
    """(user_id, full_name, username) для платежа из Telegram-юзера."""
    user_id = user["id"]
    full_name = (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("username", "")
        or str(user_id)
    )
    username = f"@{user['username']}" if user.get("username") else "—"
    return user_id, full_name, username


def _validate_payment_amount(raw) -> float:
    """0 < amount < 10M, конечное. Иначе HTTP 400 (S3: nan/inf отравляют FIFO)."""
    import math

    try:
        amount = float(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверная сумма")
    if not (math.isfinite(amount) and 0 < amount < 10_000_000):
        raise HTTPException(status_code=400, detail="Неверная сумма")
    return amount


def _validate_quantity(raw) -> float:
    """0 < qty < 1M, конечное. Иначе 400 — negative/NaN/inf отравляют тоталы и
    расчёт долга (get_agent_current_debt суммирует live order_items)."""
    import math

    try:
        qty = float(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверное количество")
    if not (math.isfinite(qty) and 0 < qty < 1_000_000):
        raise HTTPException(status_code=400, detail="Неверное количество")
    return qty


def _require_draft_order(order) -> None:
    """Состав/агента/валюту заказа можно менять только в статусе draft. Иначе 409:
    иначе менеджер прямым API-вызовом меняет уже одобренный/отгруженный заказ —
    долг разъезжается с одобренным кредит-лимитом, без ре-проверки и аудита
    (бот-путь и /api/orders/delete уже гейтят по draft)."""
    if (order or {}).get("status") != "draft":
        raise HTTPException(
            status_code=409, detail="Заказ уже отправлен — редактирование недоступно"
        )


async def _notify_batch_payments(full_name, username, comment, created):
    """Одно уведомление боссу по созданным платежам (кнопка ✅/❌ на каждый).
    Best-effort: ошибка отправки не должна терять уже созданные платежи."""
    from services.notifier import aget_notify_recipients, tg_send_message
    from utils.helpers import esc

    if not created:
        return
    lines = "\n".join(f"• {a:,.0f} {c}" for _, a, c in created)
    notify_text = (
        f"💳 <b>Новые платежи</b> от {esc(full_name)} ({esc(username)})\n"
        f"{esc(comment)}\n\n{lines}"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"✅ {a:,.0f} {c}", "callback_data": f"pay_ok:{pid}"},
                {"text": "❌", "callback_data": f"pay_no:{pid}"},
            ]
            for pid, a, c in created
        ]
    }
    for uid in await aget_notify_recipients():
        await tg_send_message(uid, notify_text, reply_markup=keyboard)


async def _send_payments_batch(user: dict, items: list, comment_raw: str, idem_key=None):
    """Мульти-валютная отправка: несколько строк {amount, currency} → отдельные
    платежи (каждый — одна валюта), ОДНО уведомление боссу с кнопкой принять/
    отклонить на каждый. Экономит менеджеру N сабмитов.

    Идемпотентность: ретрай с тем же idempotency_key не создаёт дубль-набор
    (DB-level idem_claim, как в /api/deposits/create). Частичный сбой при создании
    не теряет уведомление — шлём по уже созданным через try/finally."""
    from config import ALLOWED_CURRENCIES
    from services import async_db as adb

    if len(items) > 20:
        raise HTTPException(status_code=400, detail="Слишком много строк (макс 20)")
    comment = (comment_raw or "").strip()[:1000]
    if not comment:
        raise HTTPException(status_code=400, detail="Укажите комментарий")

    parsed: list[tuple[float, str]] = []
    for it in items:
        amount = _validate_payment_amount((it or {}).get("amount", 0))
        currency = (it or {}).get("currency", "USD")
        if currency not in ALLOWED_CURRENCIES:
            raise HTTPException(status_code=400, detail="Неверная валюта")
        parsed.append((amount, currency))

    user_id, full_name, username = _payment_identity(user)

    # DB-уровневая идемпотентность: двойной POST (ретрай клиента/мультиворкер) не
    # создаёт второй набор платежей. Ключ занят без результата → 409 (как в deposits).
    full_idem = f"payments_send:{user_id}:{idem_key}" if idem_key else None
    if full_idem:
        prev = await adb.idem_claim(full_idem, "payments_send", user_id)
        if prev is not None:
            if prev.get("payment_ids"):
                return JSONResponse(prev)
            raise HTTPException(status_code=409, detail="Запрос уже обрабатывается")

    role = get_role(user_id)
    created: list[tuple[int, float, str]] = []
    try:
        for amount, currency in parsed:
            pid = await adb.add_payment(user_id, username, full_name, amount, currency, comment)
            await adb.add_audit_log(
                user_id,
                full_name,
                role,
                "payment_sent",
                f"Платёж #{pid}: {amount:,.0f} {currency} — {comment}",
            )
            created.append((pid, amount, currency))
    except Exception:
        # Часть платежей могла создаться до сбоя (add_payment автокоммитит
        # построчно) — уведомляем по ним (иначе они «осиротеют» без видимости
        # боссу).
        await _notify_batch_payments(full_name, username, comment, created)
        if full_idem:
            if created:
                # Уже закоммиченные платежи нельзя откатить → фиксируем частичный
                # результат, чтобы ретрай вернул их, а НЕ создал второй набор
                # (дубль денег). Недостающие позиции менеджер досоздаёт отдельно.
                await adb.idem_store(
                    full_idem,
                    {"payment_ids": [pid for pid, _, _ in created], "status": "partial"},
                )
            else:
                # Ничего не закоммичено → освобождаем ключ под полноценный ретрай.
                await adb.idem_release(full_idem)
        raise

    await _notify_batch_payments(full_name, username, comment, created)
    resp = {"payment_ids": [pid for pid, _, _ in created], "status": "pending"}
    if full_idem:
        await adb.idem_store(full_idem, resp)
    return JSONResponse(resp)


@app.post("/api/payments/send")
async def api_payments_send(request: Request):
    """Отправить новый платёж на подтверждение (одиночный или мульти-валютный)."""
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

    idem_key = _cap_idem_key(data.get("idempotency_key"))

    # Мульти-валютная отправка: items=[{amount, currency}, …] + общий comment.
    items = data.get("items")
    if isinstance(items, list) and items:
        return await _send_payments_batch(user, items, data.get("comment", ""), idem_key=idem_key)

    # Round 6 (S3): isnan/isinf + верхний лимит — float('1e308') проходит
    # `> 0`, отравляет FIFO-математику в БД, отдаёт `nan USD` боссу в UI.
    import math

    try:
        amount = float(data.get("amount", 0))
        if not (math.isfinite(amount) and 0 < amount < 10_000_000):
            raise ValueError
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверная сумма")

    from config import ALLOWED_CURRENCIES

    currency = data.get("currency", "USD")
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=400, detail="Неверная валюта")

    # Round 6 (S7): cap 1000 — DB-колонка TEXT (unbounded), идёт в Telegram-
    # уведомление и в audit_log. Без cap'а — DB-bloat + риск >4096 char для
    # шаблона уведомления.
    comment = (data.get("comment", "") or "").strip()[:1000]
    if not comment:
        raise HTTPException(status_code=400, detail="Укажите комментарий")

    user_id = user["id"]
    full_name = (
        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
        or user.get("username", "")
        or str(user_id)
    )
    username = f"@{user['username']}" if user.get("username") else "—"

    # Идемпотентность одиночного платежа (WP-22): раньше её имела ТОЛЬКО batch-
    # ветка → ретрай/таймаут одиночного POST создавал второй pending-платёж (босс
    # видел два и подтверждал оба → касса завышена). DB-level idem_claim, как в
    # batch/deposits.
    full_idem = f"payments_send:{user_id}:{idem_key}" if idem_key else None
    if full_idem:
        prev = await adb.idem_claim(full_idem, "payments_send", user_id)
        if prev is not None:
            if prev.get("payment_ids"):
                return JSONResponse(prev)
            raise HTTPException(status_code=409, detail="Запрос уже обрабатывается")

    # Сохраняем в БД (через async-обёртку — не блокируем event loop)
    try:
        payment_id = await adb.add_payment(user_id, username, full_name, amount, currency, comment)
    except Exception:
        if full_idem:
            await adb.idem_release(full_idem)  # ключ свободен под полноценный ретрай
        raise

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

    result = {"payment_id": payment_id, "payment_ids": [payment_id], "status": "pending"}
    if full_idem:
        await adb.idem_store(full_idem, result)  # ретрай тем же ключом вернёт это
    return JSONResponse(result)


# ─── API: деньги (итоги за период) ───────────────────────────────────────────


@app.post("/api/money/summary")
async def api_money_summary(request: Request):
    """Поступления компании за период (boss/admin): подтверждённые платежи по
    валютам + сдачи наличных. Период — как в аналитике (week/month/3month/year
    или произвольный since/until). Деньги на удалённых заказах исключены."""
    from datetime import datetime

    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_money_summary",
        rate_limit_max=60,
    )
    now = datetime.now()
    since, until, _prev, label = _resolve_analytics_period(data, now)
    since_s = since.strftime("%Y-%m-%d %H:%M:%S")
    until_s = until.strftime("%Y-%m-%d %H:%M:%S")
    totals = await adb.get_money_totals(since_s, until_s)
    totals["period"] = {"label": label, "since": since_s[:10], "until": until_s[:10]}

    # Единый итог в базовой валюте (через курсы, как в долгах): платежи по валютам
    # + сдачи (база). convert_to_base → None, если курс не задан. Раньше такие
    # суммы МОЛЧА выпадали из «≈ …» (был баг «не считает суммы >999»: крупные
    # UZS-суммы без курса исчезали из итога). Теперь возвращаем их явным списком
    # `missing_rates`, чтобы UI показал «без курса не учтено: …», а не терял молча.
    from config import BASE_CURRENCY
    from services.database import convert_to_base

    base_cur = (BASE_CURRENCY or "USD").upper()
    # (сумма в мажорных единицах, валюта): платежи по валютам + сдачи (в базовой).
    parts = [(p["total_cents"] / 100, p["currency"]) for p in totals["payments"]]
    parts.append((totals["deposits"]["total_cents"] / 100, base_cur))
    known_sum = 0.0
    known_any = False
    missing: dict[str, float] = {}  # валюта → сумма (мажор), не вошедшая в итог
    for amt, cur in parts:
        # amt != 0 (не > 0): нетто-сдачи бывают отрицательными (cash-возвраты).
        if not amt:
            continue
        conv = convert_to_base(amt, cur)
        if conv is None:
            missing[cur] = missing.get(cur, 0.0) + amt
        else:
            known_sum += conv
            known_any = True
    totals["base_currency"] = base_cur
    totals["base_total"] = round(known_sum, 2) if known_any else None
    totals["base_partial"] = known_any and bool(missing)
    totals["missing_rates"] = [
        {"currency": cur, "amount": round(amt, 2)} for cur, amt in missing.items()
    ]
    return JSONResponse(totals)


# ─── API: заказы ─────────────────────────────────────────────────────────────


@app.post("/api/orders")
async def api_orders(request: Request):
    """Список заказов текущего пользователя."""
    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=None,  # любой валидный юзер — scope по роли ниже
        rate_limit_scope="api_orders",
        rate_limit_max=120,
    )

    from services import async_db as adb

    role = get_role(user["id"])

    if role in ("admin", "boss"):
        orders = await adb.get_all_orders()
    else:
        orders = await adb.get_user_orders(user["id"])

    from config import BASE_CURRENCY
    from utils.helpers import extract_id_from_href

    is_boss = role in ("admin", "boss")

    # Батч-загрузка позиций: один SQL вместо N (N+1 был на больших списках)
    items_by_order = await adb.get_order_items_by_ids([o["id"] for o in orders]) if orders else {}

    # PR C: прибыль по заказу — ТОЛЬКО boss/admin. Себестоимость из
    # product_prices (батч по всем ms_id позиций). profit = Σ (price−cost)×qty.
    # Если у позиции cost неизвестна — заказ помечается profit_partial=True
    # (не врём нулём). Менеджеру profit/cost не отдаём вообще.
    cost_by_ms: dict = {}
    if is_boss:
        all_ms_ids = {
            extract_id_from_href(it.get("product_href", ""))
            for items in items_by_order.values()
            for it in items
            if it.get("product_href")
        }
        prices = await adb.get_product_prices_by_ids([i for i in all_ms_ids if i])
        cost_by_ms = {
            k: v.get("cost_price") for k, v in prices.items() if v.get("cost_price") is not None
        }

    result = []
    for o in orders:
        items = items_by_order.get(o["id"], [])
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        entry = {
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
            # Заморозка/возврат на доработку (reject→draft цикл).
            "frozen": bool(o.get("frozen")),
            "rejection_count": int(o.get("rejection_count") or 0),
            "rejection_comment": o.get("rejection_comment") or "",
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
        if is_boss:
            profit = 0.0
            partial = False
            for it in items:
                ms_id = extract_id_from_href(it.get("product_href", "")) if it.get(
                    "product_href"
                ) else ""
                cost = cost_by_ms.get(ms_id)
                qty = float(it.get("quantity", 0) or 0)
                price = float(it.get("price", 0) or 0)
                if cost is None:
                    partial = True  # себестоимость не задана — не учитываем
                else:
                    profit += (price - float(cost)) * qty
            entry["profit"] = round(profit, 2)
            entry["profit_partial"] = partial  # True = часть позиций без cost
        result.append(entry)

    return JSONResponse({"orders": result, "role": role, "default_currency": BASE_CURRENCY})


@app.post("/api/orders/requests")
async def api_pending_requests(request: Request):
    """Заявки на отгрузку — только для boss/admin."""
    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_orders_requests",
        rate_limit_max=120,
    )

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
        ptype = (order.get("payment_type") or "paid") if order else "paid"
        entry = {
            "id": r["id"],
            "order_id": r["order_id"],
            "full_name": r["full_name"],
            "status": r["status"],
            "created_at": r["created_at"][:16],
            "agent_name": order.get("agent_name", "") if order else "",
            "payment_type": ptype,
            "due_date": order.get("due_date") if order else None,
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
        # Кредит-контекст для credit-заявок — босс видит долг/лимит ПЕРЕД апрувом,
        # не уходя в «Лимиты». Общий helper (без double-count, как в боте).
        if order and order.get("agent_id") and ptype == "credit":
            from services.order_workflow import order_credit_context

            ctx = await order_credit_context(order, total)
            if ctx:
                entry["credit"] = ctx
        result.append(entry)

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
    override = bool(data.get("override"))
    # Idempotency: повторный тап «Одобрить» (или ретрай по таймауту) не должен
    # повторно дёргать approve — это двойное уведомление, второй PDF и, до T2.4,
    # второй комплект документов в МойСклад. Ключ в общей БД (T2.5).
    # Сохраняем ТОЛЬКО финальный успех: needs_override — это запрос
    # подтверждения, и фронт повторит вызов ТЕМ ЖЕ ключом с override=true,
    # поэтому ключ обязательно освобождаем, иначе повтор упрётся в 409.
    from services import async_db as adb

    idem = _Idem(adb, "approve_request", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)

    bot = await get_notify_bot()
    try:
        result = await approve_shipment_request(
            req_id, user["id"], boss_name, bot, override=override
        )
    except Exception:
        await idem.release()
        raise
    if not result["ok"]:
        await idem.release()
        # Превышение кредитного лимита — не ошибка, а запрос подтверждения:
        # фронт показывает цифры и повторяет вызов с override=true.
        if result.get("needs_override"):
            return JSONResponse(
                {"ok": False, "needs_override": True, "over": result.get("over"), "req_id": req_id}
            )
        raise HTTPException(status_code=409, detail=result["error"])
    resp = {"ok": True, "req_id": req_id}
    await idem.store(resp)
    return JSONResponse(resp)


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


@app.post("/api/requests/return_to_draft")
async def api_return_to_draft(request: Request):
    """Босс возвращает заявку на доработку (заказ → черновик, после серии → freeze)."""
    from services.order_workflow import return_order_to_draft

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_return_to_draft",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    try:
        req_id = int(data.get("req_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="req_id обязателен")
    comment = (data.get("comment") or "").strip()[:500]
    if len(comment) < 3:
        raise HTTPException(status_code=400, detail="Укажите причину (минимум 3 символа)")

    boss_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    bot = await get_notify_bot()
    result = await return_order_to_draft(req_id, user["id"], boss_name, comment, bot)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse(
        {
            "ok": True,
            "req_id": req_id,
            "frozen": result.get("frozen", False),
            "rejection_count": result.get("rejection_count", 0),
        }
    )


@app.post("/api/orders/unfreeze")
async def api_unfreeze_order(request: Request):
    """Админ размораживает заказ (frozen=0 + сброс счётчика отклонений)."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin",),
        rate_limit_scope="api_unfreeze_order",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    result = await adb.unfreeze_order(order_id, user["id"], name)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return JSONResponse({"ok": True, "order_id": order_id})


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
    # Round 6 (S4): жёсткие cap'ы — agent_id UUID-style ≤64, agent_name ≤200
    # (DB-колонки TEXT unbounded, без cap'а admin/boss могут раздуть строки).
    agent_id = (data.get("agent_id") or "").strip()[:64]
    agent_name = (data.get("agent_name") or "").strip()[:200]
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id обязателен")
    # Лимит — только контрагенту, на которого реально был заказ (а не любому из
    # справочника МС). UI и так показывает лишь overview-контрагентов, но
    # страхуем API: иначе можно создать лимит-сироту, которого нет в overview.
    if not await adb.agent_has_order(agent_id):
        raise HTTPException(
            status_code=400,
            detail="Лимит можно задать только контрагенту, на которого есть заказ",
        )
    # Round 6 (S3): isnan/isinf + верхний лимит. inf лимит делает любой долг
    # «свободным», ломает overview.
    import math

    try:
        limit_amount = float(data.get("limit_amount"))
        if not (math.isfinite(limit_amount) and 0 <= limit_amount < 10_000_000):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit_amount должен быть числом 0..10M")

    await adb.set_credit_limit(
        agent_id, agent_name, limit_amount, set_by=user["id"], notes="WebApp"
    )
    return JSONResponse({"ok": True, "agent_id": agent_id, "limit_amount": limit_amount})


# ─── API: контрагенты («Клиенты») ─────────────────────────────────────────────


@app.post("/api/clients/overview")
async def api_clients_overview(request: Request):
    """Список контрагентов: МС-баланс + локальный долг/лимит. Только начальство."""
    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_clients_overview",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    from config import BASE_CURRENCY

    clients = await adb.get_clients_overview()
    return JSONResponse(
        {"ok": True, "clients": clients, "base_currency": (BASE_CURRENCY or "USD").upper()}
    )


@app.post("/api/clients/detail")
async def api_clients_detail(request: Request):
    """Карточка контрагента: имя/телефон/МС-баланс (снапшот) + локальный долг/лимит
    + заказы в боте + покупки из МС (отгрузки). Только начальство."""
    from services import async_db as adb
    from services import moysklad, snapshot

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_clients_detail",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )
    agent_id = (data.get("agent_id") or "").strip()[:64]
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id обязателен")

    cp = await asyncio.to_thread(snapshot.get_counterparty, agent_id)
    debt = await adb.get_agent_current_debt(agent_id)
    limit = await adb.get_credit_limit(agent_id)
    orders = await adb.get_orders_by_agent(agent_id)
    # История денег по клиенту: платежи, сдачи (в части, распределённой на его
    # заказы) и возвраты. Формат строки — как в общей ленте «Деньги», поэтому
    # фронт рисует её тем же кодом.
    money_history = await adb.get_agent_money_history(agent_id)
    # Покупки из МС — best-effort: при сбое МС карточка всё равно открывается.
    try:
        purchases = await moysklad.get_counterparty_purchases(agent_id)
    except Exception:
        purchases = {"top_products": [], "recent": [], "total_cents": 0, "count": 0}
    from config import BASE_CURRENCY

    return JSONResponse(
        {
            "ok": True,
            "agent_id": agent_id,
            "name": (cp or {}).get("name") or "",
            "phone": (cp or {}).get("phone") or "",
            "balance_cents": (cp or {}).get("balance_cents"),
            "debt": debt,
            "limit": limit,
            "free": round(limit - debt, 2),
            "over_limit": debt > limit,
            "orders": orders,
            "money_history": money_history,
            "purchases": purchases,
            "base_currency": (BASE_CURRENCY or "USD").upper(),
        }
    )


# Идентификаторы МойСклад — UUID. Проверяем формат перед подстановкой в путь
# запроса: значение приходит от клиента, а `entity/demand/{id}/positions` —
# это путь, и «id» вида `../../entity/counterparty/xxx` увёл бы запрос в другую
# сущность. Роль здесь и так admin/boss, но подставлять сырую строку в URL —
# привычка, которая однажды выстрелит в менее защищённом месте.
_MS_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                         r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@app.post("/api/clients/shipment")
async def api_clients_shipment(request: Request):
    """Состав отгрузки клиента: позиции demand'а из МойСклад.

    В карточке клиента отгрузки показывались одной суммой и датой — увидеть,
    ЧТО именно уехало, было нельзя, хотя это первый вопрос при разборе долга.

    Позиции документа после создания неизменяемы, поэтому в сервисе уже стоит
    часовой кэш — повторное открытие той же отгрузки в МС не ходит.
    """
    from services import moysklad

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_clients_shipment",
        rate_limit_max=60,
    )
    demand_id = (data.get("demand_id") or "").strip()
    if not _MS_UUID_RE.match(demand_id):
        raise HTTPException(status_code=400, detail="demand_id: ожидается идентификатор МойСклад")

    try:
        rows = await moysklad.get_shipment_positions(demand_id)
    except Exception as e:
        # МС недоступен — это не поломка карточки: остальное в ней уже
        # отрисовано, разворачивается только одна строка.
        logger.warning("Не удалось получить позиции отгрузки %s: %s", demand_id, e)
        raise HTTPException(status_code=502, detail="МойСклад не ответил, попробуйте позже")

    positions = []
    for pos in rows:
        quantity = float(pos.get("quantity", 0) or 0)
        price_cents = int(pos.get("price", 0) or 0)
        positions.append(
            {
                "name": (pos.get("assortment") or {}).get("name") or "—",
                "quantity": quantity,
                "unit": (pos.get("uom") or {}).get("name") or "шт",
                "price_cents": price_cents,
                "sum_cents": int(round(quantity * price_cents)),
            }
        )
    from config import BASE_CURRENCY

    return JSONResponse(
        {
            "ok": True,
            "demand_id": demand_id,
            "positions": positions,
            "sum_cents": sum(p["sum_cents"] for p in positions),
            "currency": (BASE_CURRENCY or "USD").upper(),
        }
    )


# ─── API: курсы валют (PR #42 / tech debt #3a) ────────────────────────────────


@app.post("/api/currency/rates")
async def api_currency_rates(request: Request):
    """Прочитать все курсы валют. Любая авторизованная роль (для UI-сводок)."""
    from services import async_db as adb

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "manager", "bookkeeper", "warehouse_keeper"),
        rate_limit_scope="api_currency_rates_get",
    )
    rates = await adb.get_all_currency_rates()
    from config import BASE_CURRENCY

    return JSONResponse({"ok": True, "base": BASE_CURRENCY, "rates": rates})


@app.post("/api/currency/rates/set")
async def api_currency_rates_set(request: Request):
    """Установить курс валюты к BASE_CURRENCY. Только admin/boss.

    Payload: {"initData": "...", "currency_code": "UZS", "rate_to_base": 0.000079}
    Семантика: 1 unit currency_code = rate_to_base unit BASE_CURRENCY.
    Например для UZS→USD при курсе 1 USD ≈ 12 600 UZS:
        1 UZS = 1/12600 ≈ 0.0000794 USD → rate_to_base = 0.0000794
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_currency_rates_set",
    )
    code = (data.get("currency_code") or "").strip()
    rate = data.get("rate_to_base")
    ok, err = await adb.set_currency_rate(code, rate, user["id"])
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    return JSONResponse({"ok": True, "currency_code": code.upper(), "rate_to_base": float(rate)})


# ─── API: цены товаров (PR C — управление ценами руководством) ───────────────


@app.post("/api/products/prices")
async def api_products_prices(request: Request):
    """Список заданных цен товаров. Только admin/boss (содержит cost_price)."""
    from services import async_db as adb

    data = await request.json()
    _authorize(data, allowed_roles=("admin", "boss"), rate_limit_scope="api_products_prices")
    rows = await adb.get_all_product_prices()
    return JSONResponse({"ok": True, "prices": rows})


@app.post("/api/products/prices/set")
async def api_products_prices_set(request: Request):
    """Установить цену продажи (минимум) и/или себестоимость товара.

    Только admin/boss. Payload:
      {"initData": "...", "ms_id": "...", "product_name": "...",
       "sale_price": 150.0, "cost_price": 100.0, "currency": "USD"}
    sale_price/cost_price опциональны (null = не задавать/сбросить).
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data, allowed_roles=("admin", "boss"), rate_limit_scope="api_products_prices_set"
    )
    ms_id = (data.get("ms_id") or "").strip()[:64]
    if not ms_id:
        raise HTTPException(status_code=400, detail="ms_id обязателен")
    product_name = (data.get("product_name") or "").strip()[:300]

    def _opt_price(key):
        v = data.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key}: не число")

    sale_price = _opt_price("sale_price")
    cost_price = _opt_price("cost_price")
    currency = (data.get("currency") or "").strip()

    ok, err = await adb.set_product_price(
        ms_id, product_name, sale_price, cost_price, currency, user["id"]
    )
    if not ok:
        raise HTTPException(status_code=400, detail=err)

    await adb.add_audit_log(
        user["id"],
        ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip(),
        get_role(user["id"]),
        "product_price_set",
        f"{ms_id} ({product_name}): sale={sale_price} cost={cost_price}",
    )
    return JSONResponse({"ok": True, "ms_id": ms_id})


# ─── API: техника (экскаваторы) ──────────────────────────────────────────────
# Раздел переехал из бота: формы, списки и фотографии — работа для экрана, а не
# для командной строки в чате. В боте остаётся быстрый просмотр и ввод моточасов
# с площадки.
#
# Роли: смотреть и вводить моточасы может менеджер; заводить машину, править
# карточку, двигать статус и оформлять сделки — только admin/boss. Себестоимость
# и паспорт покупателя режет `services.machines` на чтении, здесь их просто не
# существует для менеджера.

_MACHINE_ROLES = ("admin", "boss", "manager")
_MACHINE_BOSS = ("admin", "boss")


def _machine_response(res: dict) -> JSONResponse:
    """Результат сервиса техники → HTTP-ответ.

    Тексты ошибок в сервисе писались для человека — отдаём их как есть, а не
    переписываем здесь во второй раз.

    Код важнее текста: **409** значит «состояние на сервере уже другое, обнови
    карточку» (машину продали, пока форма была открыта; показание моточасов
    требует подтверждения), **400** — «исправь поле». Различить их иначе фронт
    не может, а действия у него противоположные. Дополнительные поля ответа
    (`current`, `previous`, `needs_force`) уходят клиенту вместе с `detail`:
    без них форма не сможет предложить подтверждение.
    """
    if res.get("ok"):
        return JSONResponse(res)
    error = str(res.get("error") or "Не удалось выполнить операцию")
    if "не найден" in error.lower():
        code = 404
    elif res.get("needs_force") or "current" in res or "сделка невозможна" in error:
        code = 409
    else:
        code = 400
    return JSONResponse({**res, "detail": error}, status_code=code)


def _machine_photo_public(row: dict) -> dict:
    """Фото наружу: только id и подпись.

    `tg_file_id` клиенту не нужен и опасен — он открывает файл через Bot API
    любому, кто знает токен, и переживает удаление карточки. Собираем ответ
    явным списком полей, а не `dict(row)`: при следующей правке схемы неявный
    вариант молча вынесет наружу новую колонку.
    """
    return {
        "id": int(row["id"]),
        "caption": row.get("caption") or "",
        "sort_order": int(row.get("sort_order") or 0),
        "uploaded_at": row.get("uploaded_at") or "",
    }


@app.post("/api/machines/list")
async def api_machines_list(request: Request):
    """Список техники + счётчики по статусам. Payload: {"status": "in_stock"?}."""
    from services import machines

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=_MACHINE_ROLES,
        rate_limit_scope="api_machines_list",
    )
    role = get_role(user["id"])
    status = (data.get("status") or "").strip() or None
    if status and status not in machines.STATUSES:
        # Не пустой список: «машины пропали» выглядит как потеря данных, а это
        # опечатка в фильтре.
        raise HTTPException(status_code=400, detail=f"Неизвестный статус: {status}")

    rows = await machines.list_machines(role=role, status=status)
    counts = await machines.count_by_status()
    return JSONResponse(
        {
            "ok": True,
            "machines": rows,
            "counts": counts,
            "status": status or "all",
            "can_manage": role in _MACHINE_BOSS,
            "can_see_cost": machines.can_see_cost(role),
            "status_labels": machines.STATUS_LABELS,
        }
    )


@app.post("/api/machines/card")
async def api_machines_card(request: Request):
    """Карточка машины: данные, фото, история моточасов, сделки, переходы."""
    from services import machines

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=_MACHINE_ROLES,
        rate_limit_scope="api_machines_card",
    )
    role = get_role(user["id"])
    try:
        machine_id = int(data.get("machine_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="machine_id: не число")
    if machine_id <= 0:
        raise HTTPException(status_code=400, detail="machine_id обязателен")

    machine = await machines.get_machine(machine_id, role=role)
    if not machine:
        raise HTTPException(status_code=404, detail="Машина не найдена")

    photos = await machines.list_photos(machine_id)
    hours = await machines.get_hours_history(machine_id)
    deals = await machines.list_deals(machine_id, role=role)
    return JSONResponse(
        {
            "ok": True,
            "machine": machine,
            "photos": [_machine_photo_public(p) for p in photos],
            "hours": hours,
            "deals": deals,
            # Граф переходов приходит с сервера: рисовать его копию на фронте
            # значит завести второй источник правды о жизненном цикле машины.
            "next_statuses": machines.next_status_options(machine.get("status")),
            "can_manage": role in _MACHINE_BOSS,
            # Без канала-хранилища загрузка не работает — кнопку рисовать нельзя.
            "can_upload_photo": _machine_photos_chat_id() is not None,
            "status_labels": machines.STATUS_LABELS,
        }
    )


def _machine_id_arg(data: dict, key: str = "machine_id") -> int:
    try:
        value = int(data.get(key) or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key}: не число")
    if value <= 0:
        raise HTTPException(status_code=400, detail=f"{key} обязателен")
    return value


def _machine_money(raw, label: str) -> int | None:
    """Сумма из формы («25 000», «25000.50») → копейки.

    Граница системы: наружу и внутрь ходят копейки, парсинг человеческой записи
    живёт ровно здесь. Пустое поле — это «не задано», а не ноль.
    """
    if raw is None or str(raw).strip() == "":
        return None
    cents = money.parse_amount(raw)
    if cents is None:
        raise HTTPException(status_code=400, detail=f"{label}: не число или не больше нуля")
    return cents


def _machine_text(data: dict, key: str, limit: int = 200) -> str | None:
    value = (str(data.get(key) or "")).strip()[:limit]
    return value or None


def _actor_name(user: dict) -> str:
    return ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip()


@app.post("/api/machines/create")
async def api_machines_create(request: Request):
    """Завести машину. Менеджеру можно — себестоимость он всё равно не задаёт."""
    from services import async_db as adb
    from services import machines

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=_MACHINE_ROLES,
        rate_limit_scope="api_machines_create",
        rate_limit_max=20,
    )
    role = get_role(user["id"])
    status = (data.get("status") or "in_transit").strip()
    if status not in machines.STATUSES:
        raise HTTPException(status_code=400, detail=f"Неизвестный статус: {status}")

    year = data.get("year")
    hours = data.get("hours")
    try:
        year = int(year) if str(year or "").strip() else None
        hours = int(hours) if str(hours or "").strip() else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Год и моточасы — целые числа")

    payload = {
        "vin": (data.get("vin") or "").strip()[:64],
        "name": (data.get("name") or "").strip()[:200],
        "created_by": user["id"],
        "creator_name": _actor_name(user),
        "brand": _machine_text(data, "brand", 100),
        "model": _machine_text(data, "model", 100),
        "year": year,
        "hours": hours,
        "price_cents": _machine_money(data.get("price"), "Цена"),
        "currency": (data.get("currency") or "USD").strip().upper()[:8],
        "status": status,
        "eta_date": _machine_text(data, "eta_date", 20),
        "container_no": _machine_text(data, "container_no", 50),
        "location": _machine_text(data, "location", 200),
        "notes": _machine_text(data, "notes", 1000),
    }
    # Себестоимость менеджер не видит — значит и записать не может. Иначе роль
    # режется только на чтении, и поле утекает обратно через форму.
    if machines.can_see_cost(role):
        payload["cost_cents"] = _machine_money(data.get("cost"), "Себестоимость")

    idem = _Idem(adb, "machine_create", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await machines.create_machine(**payload)
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/machines/update")
async def api_machines_update(request: Request):
    """Правка описательных полей карточки. Только admin/boss.

    VIN здесь не меняется намеренно — сервис его в whitelist не пускает: смена
    серийника это не правка, а другая машина.
    """
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_machines_update"
    )
    machine_id = _machine_id_arg(data)
    raw = data.get("fields")
    if not isinstance(raw, dict) or not raw:
        raise HTTPException(status_code=400, detail="Нечего менять")

    fields: dict = {}
    for key, value in raw.items():
        if key in ("price", "price_cents"):
            fields["price_cents"] = _machine_money(value, "Цена")
        elif key in ("cost", "cost_cents"):
            fields["cost_cents"] = _machine_money(value, "Себестоимость")
        elif key == "year":
            try:
                fields["year"] = int(value) if str(value or "").strip() else None
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Год — целое число")
        else:
            fields[key] = (str(value).strip()[:1000] or None) if value is not None else None
    res = await machines.update_machine_fields(
        machine_id, user_id=user["id"], full_name=_actor_name(user), **fields
    )
    return _machine_response(res)


@app.post("/api/machines/hours")
async def api_machines_hours(request: Request):
    """Записать моточасы. Может менеджер — показания снимают с площадки.

    `force` (запись показания меньше предыдущего — законная замена счётчика)
    только для руководства: иначе подтверждение «да, я уверен» обесценивает
    саму проверку от опечатки.
    """
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_hours"
    )
    role = get_role(user["id"])
    machine_id = _machine_id_arg(data)
    try:
        hours = int(data.get("hours"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Моточасы — целое число")

    force = bool(data.get("force"))
    if force and role not in _MACHINE_BOSS:
        raise HTTPException(
            status_code=403, detail="Откат показания подтверждает руководитель"
        )
    res = await machines.add_hours(
        machine_id, hours, user_id=user["id"], full_name=_actor_name(user), force=force
    )
    return _machine_response(res)


@app.post("/api/machines/status")
async def api_machines_status(request: Request):
    """Сменить статус машины. Только admin/boss.

    `expected` присылает фронт — тот статус, который он нарисовал. В этом смысл
    CAS: пока карточка висела открытой, машину мог продать другой, и безусловный
    UPDATE затёр бы его решение.

    Граф переходов проверяем здесь, а не в `set_status`: внутренние вызовы
    (`create_deal`, `close_deal`) двигают статус в обход ручного графа законно —
    он описывает кнопки интерфейса, а не жизненный цикл целиком.
    """
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_machines_status"
    )
    machine_id = _machine_id_arg(data)
    target = (data.get("status") or "").strip()
    expected = (data.get("expected") or "").strip()
    if target not in machines.STATUSES:
        raise HTTPException(status_code=400, detail=f"Неизвестный статус: {target}")
    if not expected:
        raise HTTPException(status_code=400, detail="expected обязателен")
    if target not in machines.next_statuses(expected):
        raise HTTPException(
            status_code=400,
            detail=f"Переход «{machines.STATUS_LABELS.get(expected, expected)}» → "
            f"«{machines.STATUS_LABELS.get(target, target)}» не предусмотрен",
        )
    res = await machines.set_status(
        machine_id, target, user_id=user["id"], full_name=_actor_name(user), expected=expected
    )
    return _machine_response(res)


@app.post("/api/machines/deal")
async def api_machines_deal(request: Request):
    """Оформить продажу или рассрочку. Только admin/boss.

    Ключ идемпотентности обязателен: сделка — денежный факт, а двойной тап по
    «Оформить» на телефоне обычное дело. Повтор отдаёт тот же `deal_id`.
    """
    from services import async_db as adb
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_machines_deal"
    )
    machine_id = _machine_id_arg(data)
    kind = (data.get("kind") or "").strip()
    if kind not in machines.DEAL_KINDS:
        raise HTTPException(status_code=400, detail=f"Тип сделки: {' / '.join(machines.DEAL_KINDS)}")
    price_cents = _machine_money(data.get("price"), "Цена")
    if not price_cents:
        raise HTTPException(status_code=400, detail="Цена сделки обязательна")
    buyer_name = (data.get("buyer_name") or "").strip()[:200]
    if not buyer_name:
        raise HTTPException(status_code=400, detail="Покупатель обязателен")
    if not data.get("idempotency_key"):
        raise HTTPException(status_code=400, detail="idempotency_key обязателен")

    idem = _Idem(adb, "machine_deal", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await machines.create_deal(
            machine_id,
            kind=kind,
            price_cents=price_cents,
            buyer_name=buyer_name,
            created_by=user["id"],
            creator_name=_actor_name(user),
            currency=(data.get("currency") or "USD").strip().upper()[:8],
            buyer_phone=_machine_text(data, "buyer_phone", 40),
            buyer_passport=_machine_text(data, "buyer_passport", 100),
            buyer_note=_machine_text(data, "buyer_note", 1000),
            agent_ms_id=_machine_text(data, "agent_ms_id", 64),
            due_date=_machine_text(data, "due_date", 20),
        )
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/machines/deal_close")
async def api_machines_deal_close(request: Request):
    """Закрыть рассрочку: деньги получены полностью, машина → «Продана»."""
    from services import async_db as adb
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_machines_deal_close"
    )
    deal_id = _machine_id_arg(data, "deal_id")

    idem = _Idem(adb, "machine_deal_close", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await machines.close_deal(
            deal_id, user_id=user["id"], full_name=_actor_name(user)
        )
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        # «Сделка не найдена или уже закрыта» — состояние на сервере другое,
        # карточку надо перечитать, а не править поле.
        return JSONResponse({**res, "detail": res.get("error", "")}, status_code=409)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/machines/deals_open")
async def api_machines_deals_open(request: Request):
    """Незакрытые рассрочки по технике — кому напоминать о сроке."""
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_machines_deals_open"
    )
    deals = await machines.get_open_credit_deals(role=get_role(user["id"]))
    return JSONResponse({"ok": True, "deals": deals})


# ─── Фотографии техники ──────────────────────────────────────────────────────
# Единственный сторедж фотографий — Telegram: он хранит их бесплатно и вечно, а
# файловая система Railway эфемерна (после каждого деплоя пусто). Отсюда два
# следствия, которые и определяют весь код ниже.
#
# 1. Прямую ссылку Telegram клиенту отдать НЕЛЬЗЯ: она выглядит как
#    `https://api.telegram.org/file/bot<TOKEN>/...` и содержит токен бота. Файл
#    проксируем через себя.
# 2. Кэшируем в памяти процесса, а не на диске — по той же причине эфемерности.
#    Ключ — `file_unique_id`: он переживает смену сервера Bot API, в отличие от
#    `tg_file_id`. Кэшируем сразу байты, а не `file_path`: тот живёт около часа
#    и всё равно требует второго запроса.

_PHOTO_CACHE: "OrderedDict[str, tuple[float, bytes]]" = OrderedDict()
_PHOTO_CACHE_TTL = 600.0
_PHOTO_CACHE_MAX_BYTES = 32 * 1024 * 1024
_PHOTO_MAX_BYTES = 5 * 1024 * 1024
# Сигнатуры форматов, которые Telegram принимает как фото. Проверяем именно
# байты: заявленный в data-URL тип пишет клиент, и через поле «фотография»
# иначе пройдёт что угодно.
_PHOTO_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))


def _photo_cache_get(key: str) -> bytes | None:
    entry = _PHOTO_CACHE.get(key)
    if not entry:
        return None
    stamp, blob = entry
    if time.time() - stamp > _PHOTO_CACHE_TTL:
        _PHOTO_CACHE.pop(key, None)
        return None
    _PHOTO_CACHE.move_to_end(key)
    return blob


def _photo_cache_put(key: str, blob: bytes) -> None:
    _PHOTO_CACHE[key] = (time.time(), blob)
    _PHOTO_CACHE.move_to_end(key)
    total = sum(len(b) for _, b in _PHOTO_CACHE.values())
    while total > _PHOTO_CACHE_MAX_BYTES and len(_PHOTO_CACHE) > 1:
        _, (_, dropped) = _PHOTO_CACHE.popitem(last=False)
        total -= len(dropped)


def _photo_media_type(blob: bytes) -> str | None:
    for magic, media in _PHOTO_MAGIC:
        if blob.startswith(magic):
            return media
    return None


def _machine_photos_chat_id() -> int | None:
    """Приватный канал-хранилище для загруженных из WebApp фотографий.

    Прецедент — `BACKUP_TG_CHAT_ID`. Без переменной загрузка выключена: фото
    по-прежнему можно прислать боту, поэтому это деградация функции, а не
    поломка раздела.
    """
    raw = os.environ.get("MACHINE_PHOTOS_TG_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.error("MACHINE_PHOTOS_TG_CHAT_ID должен быть числом, получено: %r", raw)
        return None


@app.post("/api/machines/photo")
async def api_machines_photo(request: Request):
    """Отдать фотографию машины байтами.

    `photo_id` ищем СРЕДИ ФОТО ЗАЯВЛЕННОЙ МАШИНЫ — это и есть защита от
    подстановки чужого id: снимок обязан принадлежать той машине, к которой
    у пользователя есть доступ.
    """
    from services import machines

    data = await request.json()
    _authorize(
        data,
        allowed_roles=_MACHINE_ROLES,
        rate_limit_scope="api_machines_photo",
        rate_limit_max=120,  # лента карточки — это десяток запросов подряд
    )
    machine_id = _machine_id_arg(data)
    photo_id = _machine_id_arg(data, "photo_id")

    photos = await machines.list_photos(machine_id)
    photo = next((p for p in photos if int(p["id"]) == photo_id), None)
    if not photo:
        raise HTTPException(status_code=404, detail="Фото не найдено")

    headers = {
        "Cache-Control": "private, max-age=600",
        "X-Content-Type-Options": "nosniff",
    }
    cached = _photo_cache_get(str(photo["file_unique_id"]))
    if cached is not None:
        return Response(cached, media_type=_photo_media_type(cached) or "image/jpeg", headers=headers)

    try:
        bot = await get_notify_bot()
        meta = await bot.get_file(str(photo["tg_file_id"]))
        if (meta.file_size or 0) > _PHOTO_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Фото слишком большое")
        buf = await bot.download_file(meta.file_path)
        blob = buf.read() if hasattr(buf, "read") else bytes(buf)
    except HTTPException:
        raise
    except Exception as e:
        # Протухший file_id, удалённое сообщение, сбой сети — это «фото сейчас
        # недоступно», а не поломка сервера: 500 поднял бы тревогу на ровном
        # месте. Текст исключения aiogram может содержать токен (он входит в
        # URL файлового API), поэтому в лог он идёт только через redact_token.
        logger.warning(
            "Не удалось отдать фото #%s машины #%s: %s",
            photo_id, machine_id, redact_token(repr(e)),
        )
        raise HTTPException(status_code=404, detail="Фото недоступно")

    _photo_cache_put(str(photo["file_unique_id"]), blob)
    return Response(blob, media_type=_photo_media_type(blob) or "image/jpeg", headers=headers)


@app.post("/api/machines/photo_upload")
async def api_machines_photo_upload(request: Request):
    """Загрузить фотографию машины из WebApp.

    Приходит data-URL (base64), а не multipart: `python-multipart` в
    зависимостях нет, и `UploadFile`/`Form` без него роняют приложение на
    старте. JSON заодно сохраняет единый контракт `_authorize(data)`. Раздувание
    base64 на треть безболезненно — браузер ужимает снимок canvas'ом до
    отправки.

    Файл кладём в приватный канал и храним только идентификаторы: своего
    стореджа у нас нет и заводить его ради десятка снимков незачем.
    """
    from services import machines

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=_MACHINE_ROLES,
        rate_limit_scope="api_machines_photo_upload",
        rate_limit_max=20,
    )
    machine_id = _machine_id_arg(data)
    chat_id = _machine_photos_chat_id()
    if chat_id is None:
        raise HTTPException(
            status_code=503,
            detail="Загрузка фото не настроена: нет MACHINE_PHOTOS_TG_CHAT_ID. "
                   "Пришлите фото боту.",
        )

    raw = str(data.get("data_url") or "")
    if not raw.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="Ожидается изображение")
    if "," not in raw:
        raise HTTPException(status_code=400, detail="Повреждённое изображение")
    # Оценка размера ДО декодирования: base64 длиннее оригинала на треть, и
    # декодировать 40 МБ мусора, чтобы потом его отвергнуть, незачем.
    payload = raw.split(",", 1)[1]
    if len(payload) > _PHOTO_MAX_BYTES * 4 // 3 + 1024:
        raise HTTPException(status_code=413, detail="Фото больше 5 МБ")
    try:
        blob = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(status_code=400, detail="Повреждённое изображение")
    if len(blob) > _PHOTO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Фото больше 5 МБ")
    if _photo_media_type(blob) is None:
        raise HTTPException(status_code=400, detail="Поддерживаются JPEG и PNG")

    machine = await machines.get_machine(machine_id, role=get_role(user["id"]))
    if not machine:
        raise HTTPException(status_code=404, detail="Машина не найдена")

    caption = (str(data.get("caption") or "")).strip()[:200]
    try:
        from aiogram.types import BufferedInputFile

        bot = await get_notify_bot()
        sent = await bot.send_photo(
            chat_id,
            BufferedInputFile(blob, filename=f"machine-{machine_id}.jpg"),
            caption=f"#{machine_id} {machine.get('vin') or ''} {caption}".strip()[:1024],
        )
    except Exception as e:
        logger.warning("Не удалось загрузить фото машины #%s: %s", machine_id, redact_token(repr(e)))
        raise HTTPException(status_code=502, detail="Telegram не принял фото, попробуйте ещё раз")

    # Берём самый крупный размер: Telegram отдаёт лесенку превью, и первый
    # элемент — миниатюра ~90px, из которой карточку не рассмотреть.
    best = max(sent.photo or [], key=lambda p: (p.width or 0) * (p.height or 0), default=None)
    if best is None:
        raise HTTPException(status_code=502, detail="Telegram не вернул файл")
    res = await machines.add_photo(
        machine_id,
        tg_file_id=best.file_id,
        file_unique_id=best.file_unique_id,
        uploaded_by=user["id"],
        caption=caption or None,
    )
    return _machine_response(res)


@app.post("/api/machines/photo_delete")
async def api_machines_photo_delete(request: Request):
    """Открепить фотографию от машины. Только admin/boss.

    Из Telegram файл не удаляем — там он и не мешает, а вот восстановить
    случайно снятый снимок иначе было бы нечем.
    """
    from services import machines

    data = await request.json()
    _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_machines_photo_delete"
    )
    machine_id = _machine_id_arg(data)
    photo_id = _machine_id_arg(data, "photo_id")
    photos = await machines.list_photos(machine_id)
    if not any(int(p["id"]) == photo_id for p in photos):
        raise HTTPException(status_code=404, detail="Фото не найдено")
    res = await machines.delete_photo(photo_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail="Фото не найдено")
    return JSONResponse({"ok": True, "photo_id": photo_id})


@app.post("/api/users/deactivate")
async def api_users_deactivate(request: Request):
    """Деактивировать/реактивировать пользователя (#32). Admin only.
    Payload: {"initData": "...", "user_id": N, "action": "deactivate"|"reactivate"}."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(data, allowed_roles=("admin",), rate_limit_scope="api_users_deactivate")
    try:
        target_uid = int(data.get("user_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="user_id обязателен (число)")
    action = (data.get("action") or "deactivate").strip().lower()
    if action not in ("deactivate", "reactivate"):
        raise HTTPException(status_code=400, detail="action: deactivate|reactivate")
    if action == "deactivate" and target_uid == user["id"]:
        raise HTTPException(status_code=400, detail="Нельзя деактивировать самого себя")

    if action == "deactivate":
        ok = await adb.deactivate_user(target_uid, user["id"])
    else:
        ok = await adb.reactivate_user(target_uid, user["id"])
    from services.roles import invalidate_role

    invalidate_role(target_uid)
    if not ok:
        raise HTTPException(status_code=409, detail="Нечего менять (состояние уже такое)")

    actor_name = ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip()
    await adb.add_audit_log(
        user["id"], actor_name, "admin", f"user_{action}d", f"user #{target_uid}: {action}"
    )
    return JSONResponse({"ok": True, "user_id": target_uid, "action": action})


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
    # Батч вместо N+1: одним запросом тянем заказы всех сдач сразу.
    orders_by_deposit = await adb.get_cash_deposit_orders_batch([d["id"] for d in deposits])
    for d in deposits:
        d["orders"] = orders_by_deposit.get(d["id"], [])
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
    idem = _Idem(adb, "deposit_confirm", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    dep = await adb.get_cash_deposit(deposit_id)
    try:
        res = await adb.confirm_cash_deposit(deposit_id, user["id"], name)
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
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
    resp = {"ok": True, "deposit_id": deposit_id, "closed_orders": res.get("closed_orders", [])}
    await idem.store(resp)
    return JSONResponse(resp)


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
    # Round 6 (S2): cap 500 — DB column TEXT, шлётся в Telegram (4096 лимит).
    reason = (data.get("reason") or "").strip()[:500]
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


@app.post("/api/deposits/create")
async def api_deposits_create(request: Request):
    """Менеджер сдаёт наличные: создаём сдачу (авто-FIFO по своим открытым
    заказам) и шлём подтверждающим карточку с кнопками (как /deposit в боте)."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_deposits_create",
        rate_limit_max=10,
    )
    # Round 6 (S3): isnan/isinf + верхний лимит — `1e308` отравляет FIFO.
    import math

    try:
        amount = float(data.get("amount"))
        if not (math.isfinite(amount) and 0 < amount < 10_000_000):
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="Сумма должна быть положительным числом до 10М"
        )

    # R2: DB-уровневая идемпотентность. create_cash_deposit не защищён claim'ом —
    # двойной POST (ретрай клиента после рестарта webapp/мультиворкер) создаёт две
    # сдачи. idem_claim атомарно столбит ключ в общей БД (in-mem кэш не переживал
    # рестарт). Если ключ уже был — отдаём сохранённый результат, не повторяем.
    idem_key = _cap_idem_key(data.get("idempotency_key"))
    full_key = f"deposit_create:{user['id']}:{idem_key}" if idem_key else None
    if full_key:
        prev = await adb.idem_claim(full_key, "deposit_create", user["id"])
        if prev is not None:
            if prev.get("deposit_id"):
                return JSONResponse(prev)
            # Ключ занят, но результата нет (операция в полёте/упала до store) —
            # безопаснее отказать, чем рискнуть дублем.
            raise HTTPException(status_code=409, detail="Запрос уже обрабатывается")
    try:
        res = await adb.create_cash_deposit(user["id"], amount)
    except Exception:
        if full_key:
            await adb.idem_release(full_key)  # упало до store — освободить ретраю
        raise
    if not res.get("ok"):
        if full_key:
            await adb.idem_release(full_key)
        raise HTTPException(status_code=400, detail=res.get("error", "не удалось создать сдачу"))

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    # Переиспользуем то же уведомление с клавиатурой, что и бот-команда /deposit.
    from handlers.deposits import _notify_confirmers

    bot = await get_notify_bot()
    try:
        await _notify_confirmers(bot, res["deposit_id"], name, amount)
    except Exception:
        logger.warning("deposit create notify failed", exc_info=True)
    resp = {"ok": True, "deposit_id": res["deposit_id"]}
    if full_key:
        await adb.idem_store(full_key, resp)
    return JSONResponse(resp)


@app.post("/api/deposits/my")
async def api_deposits_my(request: Request):
    """Свои сдачи (для менеджера). admin/boss тоже видят свои."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_deposits_my",
    )
    deposits = await adb.get_manager_cash_deposits(user["id"])
    return JSONResponse({"ok": True, "deposits": deposits})


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
    idem = _Idem(adb, "return_confirm", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await adb.confirm_return(return_id, user["id"], name)
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        raise HTTPException(status_code=409, detail=res.get("error", "уже обработано"))

    # Best-effort: создать «Возврат покупателя» в МойСклад (no-op без MS-контекста).
    from services import ms_returns

    try:
        await ms_returns.create_salesreturn(return_id)
    except Exception:
        logger.warning("MS salesreturn create failed", exc_info=True)
    resp = {"ok": True, "return_id": return_id, "order_status": res.get("order_status")}
    await idem.store(resp)
    return JSONResponse(resp)


@app.post("/api/returns/goods_received")
async def api_returns_goods_received(request: Request):
    """Склад/boss отмечает «товар по возврату получен» (паритет бот-кнопки ret_got)."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "warehouse_keeper"),
        rate_limit_scope="api_returns_goods_received",
    )
    try:
        return_id = int(data.get("return_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="return_id обязателен")
    idem = _Idem(adb, "return_goods", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await adb.mark_return_goods_received(return_id, user["id"])
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        raise HTTPException(status_code=409, detail=res.get("error", "уже обработано"))
    resp = {"ok": True, "return_id": return_id}
    await idem.store(resp)
    return JSONResponse(resp)


@app.post("/api/returns/positions")
async def api_returns_positions(request: Request):
    """Позиции заказа, доступные к возврату (T3.1).

    Нужен фронту, чтобы собрать ЧАСТИЧНЫЙ возврат: в /api/orders позиции
    приходят без id и без returned_qty, поэтому выбрать «вернуть 2 из 5»
    было не из чего — частичный возврат существовал только в боте (§5.2.6).

    Доступное = quantity − returned_qty. Гейты (роль, владелец, статус
    заказа) — те же, что в /api/returns/create: экран не должен показывать
    то, что потом отвергнет создание.
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "warehouse_keeper", "manager"),
        rate_limit_scope="api_returns_positions",
    )
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")

    order = await adb.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.get("status") not in ("shipped", "paid", "partially_returned") and not order.get(
        "paid_confirmed_at"
    ):
        raise HTTPException(
            status_code=409, detail="Возврат доступен только для отгруженных/оплаченных"
        )
    privileged = get_role(user["id"]) in ("admin", "boss", "warehouse_keeper")
    if not privileged and order.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Возврат только по своим заказам")

    from config import BASE_CURRENCY

    positions = []
    for it in await adb.get_order_items(order_id):
        avail = float(it.get("quantity", 0) or 0) - float(it.get("returned_qty", 0) or 0)
        if avail <= 0:
            continue
        positions.append(
            {
                "item_id": it["id"],
                "name": it.get("product_name") or "—",
                "unit": it.get("unit") or "шт",
                "available": avail,
                "price": float(it.get("price", 0) or 0),
            }
        )
    return JSONResponse(
        {
            "ok": True,
            "order_id": order_id,
            "currency": order.get("currency") or BASE_CURRENCY,
            "positions": positions,
        }
    )


@app.post("/api/returns/create")
async def api_returns_create(request: Request):
    """Оформить полный возврат по заказу (быстрый флоу, как /return в боте).
    Частичный возврат позиций — отдельной фазой."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "warehouse_keeper", "manager"),
        rate_limit_scope="api_returns_create",
        rate_limit_max=10,
    )
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")
    # Round 6 (S2): cap 500 — DB-колонка TEXT, идёт в дальнейшие уведомления.
    reason = (data.get("reason") or "").strip()[:500]
    if len(reason) < 3:
        raise HTTPException(status_code=400, detail="Опишите причину возврата")
    refund = data.get("refund_method")
    if refund not in ("cash", "debt_reduction", "no_refund"):
        raise HTTPException(status_code=400, detail="Некорректный способ возврата денег")

    # R2: DB-уровневая идемпотентность (двойной POST создавал два возврата —
    # двойной refund/занижение долга). idem_claim столбит ключ в общей БД.
    idem_key = _cap_idem_key(data.get("idempotency_key"))
    full_key = f"return_create:{user['id']}:{idem_key}" if idem_key else None
    if full_key:
        prev = await adb.idem_claim(full_key, "return_create", user["id"])
        if prev is not None:
            if prev.get("return_id"):
                return JSONResponse(prev)
            raise HTTPException(status_code=409, detail="Запрос уже обрабатывается")

    order = await adb.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    # Отгружен/оплачен/частично-возвращён ИЛИ оплачен по легаси (paid_confirmed_at).
    if order.get("status") not in ("shipped", "paid", "partially_returned") and not order.get(
        "paid_confirmed_at"
    ):
        raise HTTPException(
            status_code=409, detail="Возврат доступен только для отгруженных/оплаченных"
        )
    # H2: менеджер вправе вернуть только свой заказ; начальство/склад — любой.
    privileged = get_role(user["id"]) in ("admin", "boss", "warehouse_keeper")
    if not privileged and order.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Возврат только по своим заказам")

    # T3.1: частичный возврат. Раньше эндпоинт жёстко слал "full" и возвращал
    # ВСЕ позиции целиком — частичный возврат существовал только в боте
    # (§5.2.6). Теперь фронт может прислать items: [{item_id, quantity}].
    #
    # Доступное к возврату = quantity − returned_qty (как в боте): позиция,
    # уже возвращённая прошлым возвратом, второй раз не отдаётся.
    items = await adb.get_order_items(order_id)
    avail_by_id = {
        it["id"]: float(it.get("quantity", 0) or 0) - float(it.get("returned_qty", 0) or 0)
        for it in items
    }
    price_by_id = {it["id"]: float(it.get("price", 0) or 0) for it in items}
    returnable = {iid: a for iid, a in avail_by_id.items() if a > 0}
    if not returnable:
        if full_key:
            await adb.idem_release(full_key)
        raise HTTPException(status_code=409, detail="Нет позиций, доступных к возврату")

    raw_items = data.get("items")
    if raw_items is None:
        # Полный возврат — всё доступное (поведение по умолчанию, как было).
        ret_items = [
            (iid, avail, round(avail * price_by_id[iid], 2)) for iid, avail in returnable.items()
        ]
        return_type = "full"
    else:
        if not isinstance(raw_items, list) or not raw_items:
            if full_key:
                await adb.idem_release(full_key)
            raise HTTPException(status_code=400, detail="Выберите хотя бы одну позицию")
        ret_items = []
        for row in raw_items:
            try:
                iid = int(str((row or {}).get("item_id")))
                qty = float(str((row or {}).get("quantity")))
            except (TypeError, ValueError, AttributeError):
                if full_key:
                    await adb.idem_release(full_key)
                raise HTTPException(status_code=400, detail="Позиция: нужны item_id и quantity")
            if iid not in returnable:
                if full_key:
                    await adb.idem_release(full_key)
                raise HTTPException(
                    status_code=400, detail=f"Позиция {iid} недоступна к возврату"
                )
            if not (math.isfinite(qty) and 0 < qty <= returnable[iid] + 1e-9):
                if full_key:
                    await adb.idem_release(full_key)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Позиция {iid}: количество должно быть от 0 до "
                        f"{returnable[iid]:g}"
                    ),
                )
            qty = min(qty, returnable[iid])
            ret_items.append((iid, qty, round(qty * price_by_id[iid], 2)))
        # Выбраны все позиции в полном объёме — это фактически полный возврат
        # (та же логика, что в боте: от типа зависит статус заказа).
        is_full = len(ret_items) == len(returnable) and all(
            abs(qty - returnable[iid]) < 1e-9 for iid, qty, _ in ret_items
        )
        return_type = "full" if is_full else "partial"

    try:
        res = await adb.create_return(
            order_id,
            return_type,
            reason,
            ret_items,
            refund_method=refund,
            created_by=user["id"],
            force=privileged,
        )
    except Exception:
        if full_key:
            await adb.idem_release(full_key)  # упало до store — освободить ретраю
        raise
    if not res.get("ok"):
        if full_key:
            await adb.idem_release(full_key)
        raise HTTPException(status_code=409, detail=res.get("error", "не удалось"))

    # То же уведомление с кнопками, что и бот-команда /return.
    from handlers.returns import _notify_confirmers

    bot = await get_notify_bot()
    try:
        await _notify_confirmers(bot, res["return_id"], order_id, res["total_amount"], refund)
    except Exception:
        logger.warning("return create notify failed", exc_info=True)
    resp = {"ok": True, "return_id": res["return_id"], "total_amount": res["total_amount"]}
    if full_key:
        await adb.idem_store(full_key, resp)
    return JSONResponse(resp)


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
    # T3.2: тот же дефект, что у бот-кнопки «Новый заказ» — openOrderEditor(null)
    # создаёт черновик на КАЖДОЕ открытие редактора, так что выход назад и
    # повторный вход плодят пустые заказы. Переиспользуем пустой черновик.
    order_id, _created = await adb.get_or_create_draft(
        user["id"], full_name, data.get("comment", "")
    )
    return JSONResponse({"order_id": order_id})


@app.post("/api/orders/ship")
async def api_orders_ship(request: Request):
    """Босс/админ/кладовщик отмечает заказ отгруженным (approved → shipped).
    Альтернатива МС-вебхуку. Уведомляем создателя заказа."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "warehouse_keeper"),
        rate_limit_scope="api_orders_ship",
    )
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")

    order = await adb.get_order(order_id)
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    idem = _Idem(adb, "order_ship", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await adb.mark_order_shipped(order_id, user["id"], name)
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        raise HTTPException(status_code=409, detail=res.get("error", "не удалось отгрузить"))

    creator = order.get("user_id") if order else None
    if creator and creator != user["id"]:
        bot = await get_notify_bot()
        try:
            await bot.send_message(creator, f"🚚 Ваш заказ #{order_id} отгружен.")
        except Exception:
            logger.warning("order ship notify failed", exc_info=True)
    resp = {"ok": True, "order_id": order_id}
    await idem.store(resp)
    return JSONResponse(resp)


@app.post("/api/orders/cancel")
async def api_orders_cancel(request: Request):
    """Босс/админ отменяет approved-заказ (симметрично /cancel в боте).
    Shipped → через возврат. Уведомляем создателя заказа."""
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_orders_cancel",
    )
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")
    # Round 6 (S2): cap 500 — DB-колонка TEXT, шлётся в Telegram.
    reason = (data.get("reason") or "").strip()[:500]
    if len(reason) < 3:
        raise HTTPException(status_code=400, detail="Укажите причину отмены")

    order = await adb.get_order(order_id)
    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    # T2.6: тот же код, что и в боте — с реверсом customerorder в МойСклад.
    # Раньше здесь реверса НЕ было: заказ, отменённый из WebApp, оставался
    # в МС живым документом с резервом товара навсегда, и реконсиляция его
    # уже не подбирала (§5.2.2).
    from services.order_workflow import cancel_order_full

    res = await cancel_order_full(order_id, user["id"], name, reason)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "не удалось отменить"))

    creator = order.get("user_id") if order else None
    if creator and creator != user["id"]:
        from utils.helpers import esc

        bot = await get_notify_bot()
        try:
            await bot.send_message(
                creator,
                f"🚫 Ваш заказ #{order_id} отменён боссом.\nПричина: {esc(reason)}",
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("order cancel notify failed", exc_info=True)
    return JSONResponse({"ok": True, "order_id": order_id})


@app.post("/api/orders/add_item")
async def api_add_item(request: Request):
    data = await request.json()
    # Round 6 (L_R5): _authorize вместо голого verify_init_data — иначе
    # юзер, понижённый до guest после создания draft'а, мог дописывать
    # позиции к своему старому ордеру (owner-check проходит, role не
    # проверялась).
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_orders_add_item",
    )

    from services import async_db as adb
    from utils.helpers import extract_id_from_href

    order = await adb.get_order(data["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")
    _require_draft_order(order)

    quantity = _validate_quantity(data.get("quantity"))

    try:
        price = float(data.get("price", 0) or 0)
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Неверная цена")

    # PR C: минимальная цена продажи, заданная руководством. По product_href
    # → ms_id → product_prices.sale_price. Если задана:
    #   • price не передан/0 → префилл sale_price (дефолт)
    #   • price < sale_price → 400 (нельзя продать ниже минимума)
    product_href = data.get("product_href", "")
    ms_id = extract_id_from_href(product_href) if product_href else ""
    if ms_id:
        pp = await adb.get_product_price(ms_id)
        sale_min = pp.get("sale_price") if pp else None
        if sale_min is not None:
            if price <= 0:
                price = float(sale_min)  # префилл дефолтом
            elif price < float(sale_min):
                raise HTTPException(
                    status_code=400,
                    detail=f"Цена ниже минимальной ({sale_min:g})",
                )

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
        product_href=product_href,
        quantity=quantity,
        unit=data.get("unit", "шт"),
        price=price,
        note=data.get("note", ""),
    )
    return JSONResponse({"item_id": item_id})


@app.post("/api/orders/remove_item")
async def api_remove_item(request: Request):
    data = await request.json()
    # Round 6 (L_R5): _authorize вместо verify_init_data — см. add_item.
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_orders_remove_item",
    )

    from services import async_db as adb

    item = await adb.get_order_item(data["item_id"])
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    order = await adb.get_order(item["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")
    _require_draft_order(order)
    await adb.remove_order_item(data["item_id"])
    return JSONResponse({"ok": True})


@app.post("/api/orders/set_agent")
async def api_set_agent(request: Request):
    data = await request.json()
    # Round 6 (L_R5): _authorize. Round 6 (S4): cap agent_id/agent_name.
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_orders_set_agent",
    )

    from services import async_db as adb

    order = await adb.get_order(data["order_id"])
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")
    _require_draft_order(order)

    agent_id = (data.get("agent_id") or "").strip()[:64]
    agent_name = (data.get("agent_name") or "").strip()[:200]
    await adb.update_order_agent(data["order_id"], agent_id, agent_name)
    return JSONResponse({"ok": True})


@app.post("/api/orders/submit")
async def api_submit_order(request: Request):
    data = await request.json()
    # Round 6 (L_R5): _authorize вместо verify_init_data — без неё guest мог
    # сабмитить свой старый draft и боссы получали заявку от понижённого юзера.
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_orders_submit",
        rate_limit_max=10,
    )

    from services import async_db as adb
    from services.notifier import aget_notify_recipients, tg_send_message
    from handlers.orders import format_request_notify

    order_id = data["order_id"]
    order = await adb.get_order(order_id)
    if not order or order["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Нет доступа")

    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )

    # T2.3: весь сабмит — в одной транзакции внутри order_workflow.submit_order
    # (CAS на draft, тип оплаты, submitted_at, вставка заявки). Здесь остаются
    # только HTTP-специфика: коды ответов и уведомления.
    from services.order_workflow import submit_order

    idem_key = data.get("idempotency_key")
    res = await submit_order(
        order_id,
        user["id"],
        full_name,
        payment_type=data.get("payment_type"),
        due_date=data.get("due_date"),
        idem_key=f"order_submit:{user['id']}:{idem_key}" if idem_key else None,
    )
    if not res.get("ok"):
        # 409 — состояние заказа (уже отправлен / заморожен), 400 — данные.
        detail = res.get("error") or "Не удалось отправить заявку"
        conflict = res.get("status") is not None or "уже отправлен" in detail or "заморожен" in detail
        raise HTTPException(status_code=409 if conflict else 400, detail=detail)

    req_id = res["req_id"]
    order = await adb.get_order(order_id)  # перечитываем: нужен для уведомления
    items = await adb.get_order_items(order_id)
    await adb.add_audit_log(
        user["id"],
        full_name,
        get_role(user["id"]),
        "shipment_request_sent",
        f"Заявка #{req_id} (заказ #{order_id}) через WebApp",
    )

    # Уведомляем руководителей
    from services.order_workflow import resubmit_diff_line
    from handlers.orders import build_credit_context

    notify_text = format_request_notify(order, items, req_id)
    notify_text += await build_credit_context(order, items)  # UX: долг/лимит клиента инлайн
    notify_text += await resubmit_diff_line(order_id, items)  # #30: diff после доработки
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
    _spawn_bg(snapshot.refresh_counterparties(), "refresh_counterparties")
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

    # T2.1: остаток считает services.debts — тот же код, что в карточке заказа
    # и в утреннем напоминании о долгах. Батчем (пять запросов на любое число
    # заказов), поэтому N+1 не появляется. items тянем отдельно только ради
    # items_count в ответе.
    from services.debts import calc_order_balances

    debt_ids = [d["id"] for d in debts]
    items_by_order = await adb.get_order_items_by_ids(debt_ids) if debt_ids else {}
    balances = await calc_order_balances(debt_ids) if debt_ids else {}

    result = []
    for o in debts:
        items = items_by_order.get(o["id"], [])
        bal = balances.get(o["id"])
        if bal is None:
            continue
        total = float(money.from_cents(bal.total_cents))
        confirmed = float(money.from_cents(bal.confirmed_cents))
        pending = float(money.from_cents(bal.pending_cents))
        remaining = float(money.from_cents(bal.remaining_cents))
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

    # Единый остаток к получению в базовой валюте (объединяет разные валюты) —
    # чтобы не складывать «5000 UZS + 200 USD» в уме. convert_to_base кэширован;
    # долги без курса валюты не учитываются (флаг partial). Как в боте (#27).
    from services.database import convert_to_base

    base_cur = (BASE_CURRENCY or "USD").upper()
    rem_bases = [convert_to_base(r["remaining"], r["currency"]) for r in result if r["remaining"] > 0]
    known = [b for b in rem_bases if b is not None]
    remaining_base_total = round(sum(known), 2) if known else None
    remaining_base_partial = bool(known) and len(known) < len(rem_bases)

    # Остаток к получению РАЗДЕЛЬНО по валютам (не складываем) — фронт покажет
    # построчно; конвертированный ≈ итог остаётся как вспомогательный.
    rem_by_cur: dict[str, float] = {}
    for r in result:
        if r["remaining"] > 0:
            rem_by_cur[r["currency"]] = rem_by_cur.get(r["currency"], 0.0) + r["remaining"]
    remaining_by_currency = [
        {"currency": k, "total": v}
        for k, v in sorted(rem_by_cur.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return JSONResponse(
        {
            "debts": result,
            "role": role,
            "scope": "company" if is_boss else "personal",
            "today": today,
            "money_received": [{"currency": k, "total": v} for k, v in summary["received"].items()],
            "money_pending": [{"currency": k, "total": v} for k, v in summary["pending"].items()],
            "remaining_by_currency": remaining_by_currency,
            "remaining_base_total": remaining_base_total,
            "remaining_base_partial": remaining_base_partial,
            "base_currency": base_cur,
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
        # LEFT JOIN orders: платежи по фантомным заказам (ms_deleted_at —
        # документ удалён в МойСклад) НЕ должны попадать в «получено»: заказа
        # нет, значит и денег по нему в сводке быть не должно. Standalone-
        # платежи без order_id (o.id IS NULL) считаем как раньше — это
        # реальные поступления.
        where = "WHERE (o.id IS NULL OR o.ms_deleted_at IS NULL)"
        params: list = []
        if user_id is not None:
            where += " AND p.user_id = ?"
            params.append(user_id)
        sql = (
            f"SELECT p.status, p.currency, COALESCE(SUM(p.amount_cents), 0) AS total_cents "
            f"FROM payments p LEFT JOIN orders o ON o.id = p.order_id "
            f"{where} "
            f"GROUP BY p.status, p.currency"
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
        amt = float(money.from_cents(int(r.get("total_cents") or 0)))
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

    # Idempotency: double-click по «Оплачено» частичной суммой мог создать две
    # строки платежа. Ключ — в общей БД (T2.5), поэтому защита переживает
    # рестарт и работает между воркерами.
    idem = _Idem(adb, "mark_paid", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)

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

    # amount: если передан и валиден — частичная оплата; иначе закроет остаток.
    # Через общий валидатор (isfinite + потолок) — inf/nan/огромное не пройдут.
    amount_raw = data.get("amount")
    amount = None
    if amount_raw is not None and amount_raw != "":
        amount = _validate_payment_amount(amount_raw)

    try:
        ok, payment_id = await adb.mark_order_paid(
            order_id,
            user_id,
            full_name,
            amount=amount,
            username=username,
        )
    except Exception:
        await idem.release()  # упало до store — ретрай должен быть возможен
        raise
    if not ok:
        await idem.release()
        raise HTTPException(
            status_code=400,
            detail="Не удалось создать платёж (возможно, заказ уже полностью оплачен)",
        )

    # Сразу шлём боссу push с inline-кнопками для approve.
    await _notify_bosses_payment_pending(order_id, full_name, payment_id)

    result = {"ok": True, "payment_id": payment_id}
    await idem.store(result)
    return JSONResponse(result)


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
        if summary.get("total_base") is not None and currency != summary.get("base_currency"):
            lines.append(
                f"   ≈ <b>{fmt(summary['total_base'])} {summary['base_currency']}</b>"
            )
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

    # Idempotency: ключ в общей БД (T2.5) — двойной клик «Подтвердить» не
    # подтвердит платежи дважды даже после рестарта или в другом воркере.
    idem = _Idem(adb, "confirm_payment", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
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

    try:
        n = await adb.confirm_all_pending_payments_for_order(order_id, user["id"], full_name)
    except Exception:
        await idem.release()
        raise

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
    await idem.store(result)
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

    # Idempotency: double-click reject не должен слать менеджеру два
    # уведомления об отклонении (сам UPDATE атомарен и второй раз даёт n=0).
    # Ключ в общей БД (T2.5).
    idem = _Idem(adb, "reject_payment", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)

    full_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )

    # Аналогично confirm: сохраняем pending до UPDATE, чтобы знать кого
    # уведомить персонально (не только владельцу заказа — у каждого
    # платежа может быть свой user_id).
    pending_before = [
        p for p in await adb.get_payments_for_order(order_id) if p["status"] == "pending"
    ]

    try:
        n = await adb.reject_all_pending_payments_for_order(order_id, user["id"], full_name)
    except Exception:
        await idem.release()
        raise

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

    result = {"ok": True, "rejected_count": n}
    # #37 (F5): фиксируем результат под ключом — когда-то здесь был только
    # claim без store, поэтому ретрай тем же ключом слал повторное уведомление.
    await idem.store(result)
    return JSONResponse(result)


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
