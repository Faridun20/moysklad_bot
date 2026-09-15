"""
FastAPI сервер для WebApp.
Запускается параллельно с ботом.
"""

import asyncio
import base64
import binascii
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from utils.helpers import esc, local_now, redact_token
from webapp.auth import verify_init_data

# Берём роль из in-memory кэша (TTL 60s) вместо SELECT'а на каждый API-запрос.
# `get_role` оставляем как имя для обратной совместимости с кодом ниже.
from services.roles import cached_role as get_role
from services.rate_limit import acquire as rate_limit_acquire
from services import money
from services import version as app_version
from services import user_prefs


# Фоновые задачи — общий хелпер бота и WebApp: utils/background.py
# (сильная ссылка до завершения + лог необработанного исключения).


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

    `atomic=True` — операция сама пишет результат в ключ своей транзакцией
    (`database.idem_store_in`, ей передаётся `idem.key`). Тогда ключ без
    результата старше `IDEM_RECLAIM_AFTER_S` значит «не закоммитилось», и ретрай
    его переиспользует, а не получает 409 сутки.
    """

    __slots__ = ("_adb", "_atomic", "_key", "_op", "_uid")

    def __init__(self, adb, operation: str, user_id: int, raw_key, *, atomic: bool = False):
        capped = _cap_idem_key(raw_key)
        self._adb = adb
        self._op = operation
        self._uid = user_id
        self._atomic = atomic
        self._key = f"{operation}:{user_id}:{capped}" if capped else None

    @property
    def active(self) -> bool:
        return self._key is not None

    @property
    def key(self) -> str | None:
        return self._key

    @asynccontextmanager
    async def released_on_reject(self):
        """Отказ ручки (HTTPException) до операции освобождает ключ.

        Проверки между claim и операцией (заказ не найден, чужой заказ, неверная
        сумма) бросали 4xx, не освободив ключ: ретрай с тем же ключом после
        исправления сутки получал «Запрос уже обрабатывается», хотя не было
        сделано ничего."""
        try:
            yield
        except HTTPException:
            await self.release()
            raise

    async def claim(self) -> dict | None:
        if not self._key:
            return None
        from services.database import IDEM_RECLAIM_AFTER_S

        prev = await self._adb.idem_claim(
            self._key, self._op, self._uid,
            reclaim_after_s=IDEM_RECLAIM_AFTER_S if self._atomic else None,
        )
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


SESSION_EXPIRED_DETAIL = "Сессия истекла — закройте и откройте приложение заново"


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
        # Подпись initData живёт час (webapp/auth.py MAX_INIT_DATA_AGE): чаще
        # всего 401 — это не подделка, а приложение, открытое с утра. Текст
        # видит человек, поэтому по-русски и с действием: подпись обновляет
        # только переоткрытие, «Повторить» не поможет. Фронт по коду 401
        # показывает свой экран «Сессия истекла».
        raise HTTPException(status_code=401, detail=SESSION_EXPIRED_DETAIL)
    # R1: деактивацию проверяем отдельно от роли — кэш ролей per-process с TTL,
    # деактивация из бот-процесса иначе не видна webapp до истечения TTL. Касается
    # и allowed_roles=None (свои-данные эндпоинты): уволенный не должен дёргать
    # даже их. Через короткий деакт-кэш (TTL 30с, инвалидируется при
    # deactivate/reactivate) — иначе это был бы SELECT на КАЖДЫЙ /api/* запрос.
    from services.roles import cached_is_deactivated, role_allowed

    if cached_is_deactivated(user["id"]):
        raise HTTPException(status_code=403, detail="Доступ деактивирован")
    if allowed_roles is not None:
        role = get_role(user["id"])
        # role_allowed, а не `in`: менеджер временно замещает кладовщика и
        # бухгалтера (services.roles.ROLE_ALSO_ACTS_AS) — одна точка на все ручки.
        if not role_allowed(role, allowed_roles):
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


# Версия считается в `services/version.py` — одним и тем же кодом для бота и
# WebApp. Два сервиса Railway деплоятся отдельно и разъезжаются штатно, а
# сравнить их SHA можно только если оба считают его одинаково.
APP_VERSION = app_version.APP_VERSION
logger.info("WebApp %s", app_version.startup_line())

# Однократное предупреждение, если активен локальный обход авторизации.
if _dev_bypass_user() is not None:
    logger.warning(
        "DEV_AUTH_BYPASS активен — Telegram-авторизация ОТКЛЮЧЕНА (локальная "
        "отладка). НЕ для прода: при DATABASE_URL обход сам себя глушит."
    )


async def _drain_background_tasks() -> None:
    """Дождаться фоновых задач (печатная форма после одобрения) перед остановкой.

    Рестарт при деплое не должен терять PDF, который уже обещан менеджеру;
    ждём ограниченно — зависшая задача не имеет права держать процесс.
    """
    from utils.background import pending

    left = pending()
    if not left:
        return
    logger.info("Останавливаемся: ждём %d фоновых задач", len(left))
    try:
        await asyncio.wait_for(asyncio.gather(*left, return_exceptions=True), timeout=20)
    except TimeoutError:
        logger.warning("Фоновые задачи не завершились за 20 с — выходим без них")


@asynccontextmanager
async def _lifespan(_app):
    """Жизненный цикл приложения: на остановке — дождаться фоновых задач.

    Раньше это был `@app.on_event("shutdown")`; Starlette 1.0 убрал события,
    а FastAPI держит их только как deprecated-обёртку — lifespan штатный путь.
    """
    yield
    await _drain_background_tasks()


app = FastAPI(title="Склад WebApp", lifespan=_lifespan)


@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    """Необработанная ошибка ручки: клиенту — короткий текст без внутренностей
    (раньше в detail уезжал `str(e)` вплоть до текста SQL), в лог — трасса,
    админам — алерт в Telegram с дросселем (`services.error_alerts`). Алерт
    уходит фоном: ответ не ждёт сети до Telegram."""
    from services import error_alerts
    from utils.background import spawn

    spawn(
        error_alerts.report_exception(exc, where=f"webapp {request.method} {request.url.path}"),
        name="error-alert",
    )
    return JSONResponse({"detail": error_alerts.USER_MESSAGE}, status_code=500)


# Gzip: статика (app.js ~141KB, style.css ~59KB) и крупные JSON-ответы
# (/api/orders, /api/stock, /api/analytics) отдавались несжатыми — заметно на
# мобильном. minimum_size — не жмём мелочь, где оверхед сжатия не окупается.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=500)


# ─── Потолок размера тела запроса ─────────────────────────────────────────────
#
# Все ручки читают `await request.json()` ДО `_authorize`: тело любого размера
# от кого угодно (даже без initData) целиком ложилось в память и парсилось.
# Потолок режет это на входе. Самый крупный законный запрос — фото base64 в
# JSON: до 5 МБ байтов (`_PHOTO_MAX_BYTES`) → ~6,7 МБ base64 + поля; фронт при
# этом ещё и ужимает снимок (`shrinkImage`). 8 МБ оставляют запас.
MAX_BODY_BYTES = 8 * 1024 * 1024
_BODY_TOO_LARGE = "Слишком большой запрос"


class _BodyTooLarge(Exception):
    """Тело перевалило потолок посреди чтения (chunked без Content-Length)."""


def _is_body_too_large(exc: BaseException) -> bool:
    if isinstance(exc, _BodyTooLarge):
        return True
    # BaseHTTPMiddleware (метрики) гоняет приложение в task group — исключение
    # может приехать завёрнутым в ExceptionGroup.
    return isinstance(exc, BaseExceptionGroup) and any(
        _is_body_too_large(e) for e in exc.exceptions
    )


class _BodySizeLimitMiddleware:
    """Чистый ASGI: считает байты ПО МЕРЕ чтения, а не только Content-Length —
    chunked-запрос без заголовка иначе прошёл бы мимо."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, scope, receive, send) -> None:
        response = JSONResponse({"detail": _BODY_TOO_LARGE}, status_code=413)
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    return await self._reject(scope, receive, send)
                break

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BaseException as exc:
            if not _is_body_too_large(exc) or started:
                raise
            await self._reject(scope, receive, send)


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


class _AuthCacheWarmMiddleware:
    """Прогреть кэш роли В ПОТОКЕ до того, как ручка вызовет `_authorize`.

    `_authorize` и `get_role` синхронные и зовутся из async-ручек (их 120+,
    переписывать каждую на await — огромный дифф поперёк всех `allowed_roles`).
    При промахе кэша (раз в 30 с на пользователя) SELECT шёл прямо в потоке
    event loop'а: все остальные запросы стояли, пока он идёт, а при
    исчерпанном пуле Postgres — ещё и с ожиданием коннекта. Здесь тело
    читается один раз (и отдаётся ручке как есть), `initData` проверяется той
    же `verify_init_data`, и роль дочитывается через `asyncio.to_thread`.
    Невалидная подпись кэш не трогает — иначе чужие id засоряли бы его.
    """

    # Крупнее — только загрузка фото (base64): второй разбор JSON там дороже
    # сэкономленного SELECT'а, такой запрос идёт старым путём.
    MAX_PARSE_BYTES = 256 * 1024

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not str(scope.get("path") or "").startswith("/api/")
        ):
            return await self.app(scope, receive, send)

        messages: list = []
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            body = message.get("body") or b""
            size += len(body)
            chunks.append(body)
            if not message.get("more_body"):
                break
        if size <= self.MAX_PARSE_BYTES:
            await _warm_auth_from_body(b"".join(chunks))

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)


async def _warm_auth_from_body(body: bytes) -> None:
    try:
        data = json.loads(body) if body else None
        if not isinstance(data, dict):
            return
        init_data = data.get("initData")
        user = _dev_bypass_user() or (
            verify_init_data(init_data) if isinstance(init_data, str) and init_data else None
        )
        if not user or "id" not in user:
            return
        from services.roles import warm_auth_cache

        await warm_auth_cache(user["id"])
    except Exception:  # noqa: BLE001 — прогрев best-effort: ручка всё проверит сама
        logger.debug("Прогрев кэша роли пропущен", exc_info=True)


app.add_middleware(_AuthCacheWarmMiddleware)

# Последним — значит самым внешним из пользовательских: лишнее тело режется
# раньше метрик и gzip.
app.add_middleware(_BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)


# Версионированный ассет (`?v=<SHA>` из index.html) неизменен по построению:
# новая сборка — новый URL. Его можно хранить год и не перепроверять
# (`immutable` снимает даже revalidate при pull-to-refresh). Прежний
# `max-age=86400` без immutable заставлял WebView раз в сутки тянуть app.js
# (~140 КБ) заново при том же коммите.
STATIC_IMMUTABLE = "public, max-age=31536000, immutable"
# Всё, что без версии или с ЧУЖОЙ версией, — только с проверкой свежести.
# Чужая версия — это старая вкладка после деплоя: ей отдаётся уже НОВЫЙ файл,
# и закрепить его на год под старым URL значит отравить кэш на случай отката
# на тот коммит. Сам index.html (и по «/», и по /static/index.html) — тоже
# no-cache: он и есть носитель версии.
STATIC_REVALIDATE = "no-cache"


class CachedStaticFiles(StaticFiles):
    """StaticFiles + Cache-Control по версии в query (`?v=`)."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        from urllib.parse import parse_qs

        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        versioned = bool(APP_VERSION) and APP_VERSION in query.get("v", [])
        is_html = str(full_path).endswith(".html")
        resp.headers["Cache-Control"] = (
            STATIC_IMMUTABLE if versioned and not is_html else STATIC_REVALIDATE
        )
        return resp


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


# ─── Health-check ────────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz():
    """Лёгкий ping-endpoint для Railway-мониторинга и uptime-чекеров.
    Не задевает БД и МойСклад — отвечает быстро даже если они лежат,
    чтобы внешний мониторинг видел: HTTP-слой жив, паника не общая."""
    import time as _t

    # Версия и uptime — чтобы «выкатилось ли» и «перезапускался ли» проверялись
    # одним curl'ом, без входа в панель Railway. Ручка открытая: SHA коммита и
    # заголовок сами по себе ничего не открывают, а закрытый healthcheck не
    # годится внешнему мониторингу.
    return JSONResponse({
        "ok": True,
        "version": APP_VERSION,
        "ts": int(_t.time()),
        **app_version.info().as_dict(),
    })


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
    from services.database import get_setting

    return JSONResponse(
        {
            "user_id": user_id,
            "first_name": user.get("first_name", ""),
            "username": user.get("username", ""),
            "role": role,
            # Касса/сдачи хранятся в базовой валюте (нет currency-колонки) —
            # фронт показывает её код, а не хардкод «USD».
            "base_currency": (BASE_CURRENCY or "USD").upper(),
            # Версия едет вместе с ролью: «выкатилось ли» проверяют с телефона,
            # а не из терминала, и отдельный запрос ради восьми знаков — это
            # запрос, который забудут сделать. Показывает её только руководство
            # (см. фронт): кладовщику номер сборки не нужен.
            "version": APP_VERSION,
            # Бухгалтерия (счета, журнал денег) включена руководителем — по
            # флагу фронт меняет вкладку «Касса» на «Счета» и кнопку оплаты.
            "accounting_enabled": bool(
                await asyncio.to_thread(get_setting, "accounting_enabled", False)
            ),
            # Личные настройки вида (services/user_prefs): «Рабочие действия»
            # руководителя. НЕ права — только что рисовать.
            "prefs": await asyncio.to_thread(user_prefs.get_prefs, user_id),
            # Удаление техники/товаров/накладных — только руководству?
            # (app_settings, по умолчанию выкл.; переключает руководство в
            # «Настройках»). Фронт прячет по нему кнопки менеджера
            # (`deleteActionsVisible`); сервер решает сам (`_require_delete_right`).
            "delete_requires_boss": await _delete_requires_boss(),
        }
    )


@app.post("/api/prefs/set")
async def api_prefs_set(request: Request):
    """Личная настройка интерфейса (services/user_prefs): {key, value}.

    Сейчас одна — `work_actions`, выключатель «Рабочие действия» в «Меню»
    руководителя. Меняет только ВИД (какие кнопки рисовать), права ручек не
    трогает. Каждое переключение — в аудит: «кто и когда включил себе работу
    менеджера» — вопрос, который задают после разбора.
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data, allowed_roles=("admin", "boss"), rate_limit_scope="api_prefs_set"
    )
    key = str(data.get("key") or "").strip()
    role = get_role(user["id"])
    if key not in user_prefs.PREFS or not user_prefs.applies_to(key, role):
        raise HTTPException(status_code=400, detail="Неизвестная настройка")
    value = data.get("value")
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail="value: true или false")
    prefs = await asyncio.to_thread(user_prefs.set_pref, user["id"], key, value)
    await adb.add_audit_log(
        user["id"], _actor_name(user), role, "pref_set", f"{key}={'on' if value else 'off'}"
    )
    return JSONResponse({"ok": True, "prefs": prefs})


# ─── Удаление: менеджеру или только руководству ──────────────────────────────
# Решение владельца: «Удаление техники, товаров и накладных — пока может и
# менеджер (я один), но в будущем только руководитель». Один выключатель
# `app_settings.delete_requires_boss` (по умолчанию выкл.) и одна проверка на
# все ручки удаления — иначе включённый флаг закрыл бы одну из них, а соседняя
# продолжала бы пускать менеджера.

_DELETE_ROLES = ("admin", "boss", "manager")


async def _delete_requires_boss() -> bool:
    from services.database import get_setting

    return bool(await asyncio.to_thread(get_setting, "delete_requires_boss", False))


async def _can_delete(role: str) -> bool:
    if role in ("admin", "boss"):
        return True
    return role == "manager" and not await _delete_requires_boss()


async def _require_delete_right(role: str) -> None:
    """403 менеджеру, если удаление оставлено руководству. Роль уже прошла
    `_authorize` ручки; кладовщик и бухгалтер сюда не доходят."""
    if role in ("admin", "boss"):
        return
    if role != "manager" or await _delete_requires_boss():
        raise HTTPException(
            status_code=403,
            detail="Удалять может только руководитель — так настроено в WebApp",
        )


@app.post("/api/settings/delete_requires_boss")
async def api_settings_delete_requires_boss(request: Request):
    """Руководство включает/выключает «удаление — только руководитель». Аудит."""
    from services import async_db as adb
    from services.database import set_setting

    data = await request.json()
    user = _authorize(
        data, allowed_roles=("admin", "boss"),
        rate_limit_scope="api_settings_delete_requires_boss", rate_limit_max=10,
    )
    if "enabled" not in data or not isinstance(data.get("enabled"), bool):
        raise HTTPException(status_code=400, detail="enabled: true или false")
    enabled = bool(data["enabled"])
    before = await _delete_requires_boss()
    await asyncio.to_thread(set_setting, "delete_requires_boss", enabled, user["id"])
    if before != enabled:
        await adb.add_audit_log(
            user["id"], _actor_name(user), get_role(user["id"]), "setting_changed",
            f"delete_requires_boss: {before} → {enabled} "
            + ("(удаление — только руководитель)" if enabled else "(удалять может и менеджер)"),
        )
    return JSONResponse({"ok": True, "delete_requires_boss": enabled})


@app.post("/api/search")
async def api_search(request: Request):
    """Глобальный поиск по заказам / платежам / контрагентам.

    Менеджер видит только свои заказы и платежи (user_id-скоуп);
    boss/admin — все. Контрагенты — общий справочник (видны всем,
    они и так нужны для создания заказов).
    """
    from services import async_db as adb
    from services import counterparties as cp_service

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
    agents = await cp_service.search(query, 20)

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
      - today: общая выручка/отгрузки/клиенты по складу компании
      - my_orders: его собственные заказы
      - pending_requests: количество заявок ожидающих апрува
      - top_employees: лидерборд за неделю
    """
    from datetime import datetime, timedelta
    from services import async_db as adb
    from services.warehouse import sales_stats

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

    # ─── Сегодня ──────────────────────────────────────
    if is_boss:
        # Босс видит общую выручку за сегодня — по расходным накладным склада.
        try:
            today_stats = await sales_stats(start_of_day, now)
        except Exception as e:  # noqa: BLE001 — экран важнее одной цифры
            logger.warning("home: failed to load today stats: %s", e)
            today_stats = {"total": 0, "count": 0, "clients": 0, "top_products": []}
        today = {
            # В базовой валюте по курсу: сумма копеек USD и UZS — не выручка.
            "revenue": today_stats.get("base_total", today_stats["total"]) / 100,
            "shipments": today_stats["count"],
            "clients": today_stats["clients"],
            "scope": "company",
        }
    else:
        # Менеджер: считаем личные показатели из его же одобренных заявок.
        # Батч-запросом
        # подтягиваем сразу все позиции (раньше был N+1 по заказам).
        today_iso = start_of_day.strftime("%Y-%m-%d")
        relevant_today = [
            o
            for o in my_orders
            if o["status"] in _PERSONAL_SALE_STATUSES
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

    # Явная аннотация: дальше в словарь кладут и числа, и списки словарей,
    # а выведенный тип зафиксировался бы по первым ключам.
    result: dict[str, Any] = {
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

        # Топ-сотрудники — из ЛОКАЛЬНЫХ заказов (`get_manager_performance`,
        # GROUP BY user_id). Раньше группировали отгрузки МойСклад по
        # кастомному атрибуту `telegram_full_name`, который проставлялся только
        # когда документ создал бот, — всё остальное липло к строке «Прочее
        # (вручную в МойСклад)». Своя таблица знает менеджера у каждого заказа.
        try:
            perf = await adb.get_manager_performance(
                week_ago.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
            )
            result["top_employees"] = [
                {"name": m["full_name"], "revenue": m["revenue"], "count": m["shipped"]}
                for m in perf[:5]
            ]
        except Exception as e:  # noqa: BLE001 — лидерборд не важнее экрана
            logger.warning("home: failed to load top employees: %s", e)
            result["top_employees"] = []

    return JSONResponse(result)


# ─── API: операционная сводка ────────────────────────────────────────────────


@app.post("/api/today")
async def api_today(request: Request):
    """Очередь дел: что ждёт этого человека и в каком порядке.

    Отдаётся ВСЕМ рабочим ролям, включая кладовщика и бухгалтера: до этого
    «Главная» держалась на `/api/home`, который им не отвечает, и раздел
    открывался экраном с ошибкой. Порядок и состав очереди считает
    `services.work_queue` — в шаблоне он разъехался бы с ролями.
    """
    from services import work_queue

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager", "warehouse_keeper", "bookkeeper"),
        rate_limit_scope="api_today",
        rate_limit_max=120,
    )
    role = get_role(user["id"])
    queue = await work_queue.gather(user["id"], role)
    return JSONResponse({
        "ok": True,
        "queue": queue,
        "total": sum(int(i["count"]) for i in queue),
    })


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
    """Список товаров со склада: остаток, резерв, доступное, цены.

    Источник — наши `products`/`stock`, а не каталог МойСклад. Из-за этого
    исчезла и ветка «МойСклад недоступен»: локальная БД либо есть, либо не
    отвечает ничего, и мягко деградировать тут не во что.
    """
    from services import async_db as adb
    from services.warehouse import get_catalog, get_categories

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_stock",
        rate_limit_max=120,
    )
    role = get_role(user["id"])

    rows, cats = await asyncio.gather(get_catalog(), get_categories())

    # PR C: подмешиваем цены руководства. sale_price — всем (менеджер видит
    # минимум и дефолт), cost_price — ТОЛЬКО boss/admin (себестоимость не
    # раскрываем менеджерам).
    is_boss = role in ("admin", "boss")
    prices = await adb.get_product_prices_by_ids([str(r["product_id"]) for r in rows])
    from services import costing

    fifo: dict = {}
    cost_cur = None
    if costing.can_see_cost(role) and await costing.is_enabled():
        fifo = await costing.current_costs()
        cost_cur = costing.base_currency()

    products = []
    for r in rows:
        pp = prices.get(str(r["product_id"]))
        item = {
            "product_id": r["product_id"],
            "name": r["name"],
            "stock": r["quantity"],
            "reserve": r["reserved"],
            "available": r["available"],
            "unit": r["unit"],
            "folder_id": r["category"],
            "folder_name": r["category"],
            "sale_price": (pp.get("sale_price") if pp else None),
        }
        if is_boss and pp:
            item["cost_price"] = pp.get("cost_price")
        if is_boss and fifo and r["product_id"] in fifo:
            # Учёт включён и у товара есть партии с ценой: средняя по остатку.
            # Ручная `cost_price` остаётся рядом — у товара без партий она
            # по-прежнему единственный ответ.
            item["cost_batches"] = fifo[r["product_id"]]["unit_cost_cents"] / 100
        products.append(item)

    return JSONResponse({"products": products, "categories": cats, "cost_currency": cost_cur})


# ─── API: аналитика продаж ───────────────────────────────────────────────────


@app.post("/api/analytics")
async def api_analytics(request: Request):
    """
    Аналитика продаж за период.

    Менеджер видит ТОЛЬКО свои показатели (по его заказам).
    Босс/админ — общую по компании (по расходным накладным склада).
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

    # Босс/админ — компания целиком
    payload = await _company_analytics_payload(since, until, prev_since, label)
    return JSONResponse(payload)


async def _company_analytics_payload(since, until, prev_since, label: str) -> dict:
    """Расчёт компанейской аналитики (boss/admin) по расходным накладным.

    Вынесено из api_analytics, чтобы /api/analytics/export переиспользовал тот
    же расчёт. Включает маржу по топ-товарам (cost из product_prices), топ
    клиентов и топ менеджеров.

    Источник сменился с МойСклад на свой склад, и вместе с ним ушла ветка
    «МС недоступен»: раньше любое исключение всплывало как HTTP 500 и WebApp
    показывал «Unexpected token … is not valid JSON». Локальные запросы так не
    падают, но обёртку `_safe_call` оставляем — пустая аналитика полезнее
    ошибки вместо экрана.
    """
    from datetime import datetime

    from services import async_db as adb
    from services.warehouse import sales_stats, shipment_counts_by_day

    _empty_stats: dict = {
        "total": 0, "count": 0, "clients": 0, "top_products": [],
        "base_total": 0, "base_count": 0, "base_partial": False, "missing": {},
    }
    _state = {"ok": True}

    async def _safe_call(coro, default, label):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001 — экран важнее одной цифры
            logger.warning("analytics: %s не посчитан: %s", label, e)
            _state["ok"] = False
            return default

    # По дням — агрегатом (shipment_counts_by_day), а не по списку отгрузок:
    # список обрезан тысячей строк, и в длинном периоде дни молча пустели.
    current, prev, day_counts = await asyncio.gather(
        _safe_call(sales_stats(since, until), _empty_stats, "sales_stats"),
        _safe_call(sales_stats(prev_since, since), _empty_stats, "sales_stats(prev)"),
        _safe_call(shipment_counts_by_day(since, until), {}, "shipment_counts_by_day"),
    )
    stats_incomplete = not _state["ok"]

    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    by_day = [0] * 7
    for day, n in day_counts.items():
        try:
            by_day[datetime.strptime(day, "%Y-%m-%d").weekday()] += n
        except ValueError:
            pass

    from config import BASE_CURRENCY

    base_cur = (BASE_CURRENCY or "USD").upper()
    # Выручка — в БАЗОВОЙ валюте по курсу (sales_stats.base_total), а не сумма
    # копеек всех валют: USD и UZS складывались в одно число, и от него же
    # считались тренд и средний чек. Что без курса — отдаётся отдельно
    # (`missing_rates`, `base_partial`), как в «Деньги → Отчёт».
    cur_base = int(current.get("base_total", current["total"]) or 0)
    prev_base = int(prev.get("base_total", prev["total"]) or 0)
    trend = 0
    if prev_base > 0:
        trend = round((cur_base - prev_base) / prev_base * 100)
    base_count = int(current.get("base_count", current["count"]) or 0)

    # Маржа по топ-товарам. Выручка — в копейках (÷100) в валюте накладной;
    # cost — мажорные (как ввело руководство) в валюте цены. cost None или
    # валюты разные → profit не считаем: сумы минус доллары — не прибыль.
    top_products = current["top_products"][:5]
    prod_ids = [str(d["product_id"]) for _n, d in top_products if d.get("product_id")]
    costs = await adb.get_product_prices_by_ids(prod_ids) if prod_ids else {}
    from services import costing

    if await costing.is_enabled():
        # Учёт себестоимости включён: прибыль считает блок «Прибыль» по
        # партиям. Прикидка по ручной себестоимости здесь дала бы второй,
        # расходящийся ответ на тот же вопрос — не показываем её вовсе.
        costs = {}
    top = []
    for name, d in top_products:
        revenue = d["sum"] / 100
        item_cur = (d.get("currency") or base_cur).upper()
        item = {"name": name, "sum": revenue, "qty": d["qty"], "currency": item_cur}
        cost_row = costs.get(str(d.get("product_id") or ""))
        cost = cost_row.get("cost_price") if cost_row else None
        cost_cur = ((cost_row or {}).get("currency") or base_cur).upper()
        if cost is not None and cost_cur == item_cur:
            item["profit"] = round(revenue - float(cost) * d["qty"], 2)
            item["margin_known"] = True
        else:
            item["margin_known"] = False
        top.append(item)

    top_clients = [
        {
            "name": name,
            "revenue": d["sum"] / 100,
            "count": d["count"],
            "currency": (d.get("currency") or base_cur).upper(),
        }
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

    by_cur = current.get("by_currency") or {}
    return {
        "label": label,
        "scope": "company",
        # Итог в базовой валюте — только то, что пересчитано по курсу.
        "total": cur_base / 100,
        "base_currency": base_cur,
        "base_partial": bool(current.get("base_partial")),
        "total_by_currency": [
            {"currency": c, "total": v / 100}
            for c, v in sorted(by_cur.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "missing_rates": [
            {"currency": c, "amount": v / 100}
            for c, v in sorted((current.get("missing") or {}).items())
        ],
        "count": current["count"],
        "clients": current["clients"],
        # Средний чек — по отгрузкам, вошедшим в итог: делить пересчитанную
        # часть на все отгрузки значит занизить его на долю «без курса».
        "avg_check": (cur_base / base_count / 100) if base_count else 0,
        "trend": trend,
        "by_day": [{"day": days_ru[i], "count": by_day[i]} for i in range(7)],
        "top_products": top,
        "top_clients": top_clients,
        "top_managers": top_managers,
        "stats_incomplete": stats_incomplete,
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
    # Сбой сборки — в общий обработчик: трасса в лог, клиенту без внутренностей.
    payload = await _company_analytics_payload(since, until, prev_since, label)

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


# Статусы, в которых заказ — состоявшаяся продажа менеджера (личный отчёт и
# «Сегодня»). Оплата и частичный возврат продажу не отменяют: после
# подтверждённой сдачи заказ становится `paid`, и фильтр по approved/shipped
# молча выкидывал его из выручки. Полный возврат (`returned`) и отмена — не
# продажа.
_PERSONAL_SALE_STATUSES = ("approved", "shipped", "paid", "partially_returned")


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
        if o["status"] in _PERSONAL_SALE_STATUSES and prev_since_iso <= _ts(o) <= until_iso
    ]

    # Диагностический лог — увидим в Railway почему аналитика пуста,
    # если такое снова случится. Логируем только агрегаты, не PII.
    logger.info(
        "analytics user=%s role=manager orders=%d approved=%d relevant=%d "
        "period=[%s..%s] (prev_since=%s)",
        user_id,
        len(orders),
        sum(1 for o in orders if o["status"] in _PERSONAL_SALE_STATUSES),
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
    except Exception:
        logger.error("payments/history failed for user_id=%s", user_id)
        raise  # общий обработчик: трасса в лог, клиенту без str(e)


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
        allowed_roles=("admin", "boss", "bookkeeper"),  # как confirm_payment
        rate_limit_scope="api_payments_pending",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )

    orders = await adb.get_paid_orders_awaiting_confirmation()
    order_ids = [o["id"] for o in orders]
    items_by_order = await adb.get_order_items_by_ids(order_ids) if order_ids else {}
    payments_by_order = await adb.get_payments_for_orders(order_ids) if order_ids else {}
    from services import order_payments

    parts_by_order = await order_payments.parts_for_orders(order_ids) if order_ids else {}

    result = []
    for o in orders:
        items = items_by_order.get(o["id"], [])
        total = sum(float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items)
        payments = payments_by_order.get(o["id"], [])
        pending = sum(float(p["amount"]) for p in payments if p["status"] == "pending")
        parts = parts_by_order.get(o["id"], [])
        cash_pending_ids = {
            pt["payment_id"] for pt in parts if pt["method"] == "cash" and pt["state"] in ("on_hand", "in_deposit")
        }
        confirmable = sum(
            float(p["amount"]) for p in payments
            if p["status"] == "pending" and int(p["id"]) not in cash_pending_ids
        )
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
                # Как получены деньги: карта/счёт — подтверждаются этой
                # карточкой, наличные — сдачей в кассу (не этой кнопкой).
                "parts": parts,
                "confirmable": confirmable,
                "cash_pending": pending - confirmable,
                "recorded_by_me": any(int(p["user_id"]) == int(user["id"]) for p in payments
                                      if p["status"] == "pending"),
            }
        )

    confirmers = await _money_confirmers(user["id"])
    return JSONResponse({
        "pending": result, "role": get_role(user["id"]), "confirmers_exist": confirmers["exist"],
    })


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


def _validate_payment_amount(raw, currency: str | None = None) -> float:
    """Сумма платежа в валюте `currency` (None — базовая): конечная, > 0 и не
    выше потолка в ЭКВИВАЛЕНТЕ базовой валюты. Иначе HTTP 400 (S3: nan/inf
    отравляют FIFO).

    Потолок «< 10 000 000 в любой валюте» для сумов означал ≈ $800 — оплату
    техники в UZS нельзя было провести вовсе. Правило одно на все ручки —
    `database.validate_amount_in_currency`.
    """
    from services.database import validate_amount_in_currency

    try:
        amount = float(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Неверная сумма")
    ok, err = validate_amount_in_currency(amount, currency)
    if not ok:
        detail = err if err and "лимит" in err else "Неверная сумма"
        raise HTTPException(status_code=400, detail=detail)
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


_UNIT_MAX = 16
# Единица измерения — короткое слово («шт», «кг», «м²», «уп.»): буквы, цифры,
# пробел и немного пунктуации. Остальное (в т.ч. `<`, `>`, кавычки) выкидываем.
_UNIT_JUNK = re.compile(r"[^\w .,/%²³-]", re.UNICODE)


def _clean_unit(raw) -> str:
    """Единица позиции заказа: белый список символов и потолок длины.

    Поле уходит в интерфейс руководства и в печатную форму; без проверки в
    нём приезжала разметка любой длины (stored-XSS, если где-то забыли
    экранирование). Фронт экранирует и сам — это второй рубеж, а не первый.
    """
    unit = _UNIT_JUNK.sub("", str(raw or "")).strip()[:_UNIT_MAX].strip()
    return unit or "шт"


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
    Best-effort: ошибка отправки не должна терять уже созданные платежи.

    Денежное событие — по КАЖДОЙ строке отдельно: платежи ниже
    `boss_instant_threshold_usd` не пушим, они остаются pending и уходят в
    вечерний дайджест (`services.boss_digest`); карточкой сразу идут только
    строки ≥ порога. Пустой отфильтрованный список — сообщение не шлём вовсе.
    """
    from services.notifier import aget_notify_recipients, tg_send_message
    from services.notify_policy import PAYMENT, should_notify_now
    from utils.helpers import esc

    created = [c for c in created if should_notify_now(PAYMENT, c[1], c[2])]
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
        currency = (it or {}).get("currency", "USD")
        if currency not in ALLOWED_CURRENCIES:
            raise HTTPException(status_code=400, detail="Неверная валюта")
        # Валюта — ДО суммы: потолок суммы задан в эквиваленте базовой валюты.
        amount = _validate_payment_amount((it or {}).get("amount", 0), currency)
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

    from config import ALLOWED_CURRENCIES

    currency = data.get("currency", "USD")
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=400, detail="Неверная валюта")

    # Round 6 (S3): isnan/isinf + верхний лимит — float('1e308') проходит
    # `> 0`, отравляет FIFO-математику в БД, отдаёт `nan USD` боссу в UI.
    # Потолок — в эквиваленте базовой валюты, поэтому валюта проверяется первой.
    amount = _validate_payment_amount(data.get("amount", 0), currency)

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

    # Уведомляем админов через Telegram API напрямую — только если сумма не
    # ниже boss_instant_threshold_usd (services.notify_policy): меньшие суммы
    # остаются pending и попадают в вечерний дайджест, а не в отдельный пуш.
    from services.notify_policy import PAYMENT, should_notify_now

    if should_notify_now(PAYMENT, amount, currency):
        notify_text = format_payment_notify(
            payment_id, full_name, username, amount, currency, comment
        )
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

    # Единый итог в базовой валюте: платежи + сдачи (база). Валюты без курса НЕ
    # выпадают молча (был баг «не считает суммы >999»: крупные UZS-суммы без
    # курса исчезали из итога) — они уходят явным списком `missing_rates`, и UI
    # пишет «без курса не учтено: …».
    #
    # Курс — СНИМОК на момент подтверждения (payments.fx_rate_to_base), как в
    # отчёте продаж (get_manager_performance): деньги, пришедшие, когда
    # 1 250 000 сум стоили 100 USD, не становятся 125 USD оттого, что сум
    # укрепился. Снимка нет (легаси-строка) — текущий курс.
    from config import BASE_CURRENCY
    from services.database import convert_to_base, convert_to_base_at

    base_cur = (BASE_CURRENCY or "USD").upper()
    # (сумма в мажорных единицах, валюта, снимок курса): платежи + сдачи.
    parts = [
        (p["total_cents"] / 100, p["currency"], p["fx_rate_to_base"])
        for p in totals.pop("payments_by_rate", [])
    ]
    # Сдачи — в своей валюте (наличные сумы в кассе — сумы, cash_deposit_currency).
    for dep in totals["deposits"].get("by_currency") or [
        {"currency": base_cur, "total_cents": totals["deposits"]["total_cents"]}
    ]:
        parts.append((dep["total_cents"] / 100, dep["currency"], None))
    known_sum = 0.0
    known_any = False
    missing: dict[str, float] = {}  # валюта → сумма (мажор), не вошедшая в итог
    for amt, cur, snap in parts:
        # amt != 0 (не > 0): нетто-сдачи бывают отрицательными (cash-возвраты).
        if not amt:
            continue
        conv = convert_to_base_at(amt, cur, snap)
        if conv is None:
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


# ─── API: дебиторка («где деньги») ───────────────────────────────────────────
# Заказы в кредит и рассрочки по технике — два учёта, но один вопрос: сколько
# нам должны, когда это придёт и кто тянет. Считает `services.receivables`,
# ручки только режут по роли и отдают.


async def _receivables_for(user_id: int) -> tuple[list, bool]:
    """Дебиторка в объёме роли. Второй элемент — видна ли техника."""
    from services import receivables

    is_boss = get_role(user_id) in ("admin", "boss")
    items = await receivables.collect(
        user_id=None if is_boss else user_id,
        # Рассрочки оформляет руководство: менеджеру это не пустой блок, а
        # чужой участок — поэтому не отдаём вовсе, а не отдаём нулём.
        include_machines=is_boss,
    )
    return items, is_boss


@app.post("/api/money/receivables")
async def api_money_receivables(request: Request):
    """«Где деньги»: разбивка по срокам, итоги по источникам, топ должников."""
    from services import receivables

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_money_receivables",
        rate_limit_max=30,
    )
    items, is_boss = await _receivables_for(user["id"])
    payload = {
        "ok": True,
        "scope": "company" if is_boss else "personal",
        "aging": receivables.aging(items),
        "totals": receivables.totals_by_source(items),
        "by_counterparty": receivables.by_counterparty(items),
    }
    if is_boss:
        # Разрез по менеджерам — управленческий: менеджеру он показал бы чужие
        # цифры, а себя он и так видит целиком.
        owners = receivables.by_owner(items)
        names = await _owner_names([o["user_id"] for o in owners])
        for row in owners:
            row["name"] = names.get(row["user_id"], f"#{row['user_id']}")
        payload["by_owner"] = owners
    return JSONResponse(payload)


async def _owner_names(user_ids: list[int]) -> dict[int, str]:
    """id менеджера → имя. Батчем: список владельцев иначе даёт N+1."""
    if not user_ids:
        return {}
    from services import async_db as adb

    users = await adb.get_all_users()
    return {
        int(u["user_id"]): (u.get("full_name") or u.get("username") or f"#{u['user_id']}")
        for u in users
        if int(u["user_id"]) in set(user_ids)
    }


@app.post("/api/money/forecast")
async def api_money_forecast(request: Request):
    """Ожидаемые поступления по месяцам вперёд. Только руководство."""
    from services import receivables

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_money_forecast",
        rate_limit_max=30,
    )
    raw_months = data.get("months")
    try:
        # Явная проверка на None, а не `or 6`: ноль — это запрос «ноль месяцев»,
        # и подменять его дефолтом значит молча ответить не на тот вопрос.
        months = 6 if raw_months is None or raw_months == "" else int(raw_months)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="months: не число")
    months = max(1, min(months, 12))
    items = await receivables.collect()
    return JSONResponse({"ok": True, "months": receivables.forecast(items, months=months)})


@app.post("/api/money/discipline")
async def api_money_discipline(request: Request):
    """Поступают ли платежи: собрано против ожидалось и доля платежей в срок."""
    from datetime import datetime

    from services import receivables

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_money_discipline",
        rate_limit_max=30,
    )
    now = datetime.now()
    since, until, _prev, label = _resolve_analytics_period(data, now)
    stats = await receivables.collection_stats(
        since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d")
    )
    stats["period"] = {"label": label, "since": since.strftime("%Y-%m-%d"),
                       "until": until.strftime("%Y-%m-%d")}
    stats["ok"] = True
    return JSONResponse(stats)


@app.post("/api/machines/buyer")
async def api_machines_buyer(request: Request):
    """Карточка покупателя техники: все его сделки, графики и остаток.

    Ключ — имя: настоящего идентификатора у покупателя пока нет
    (`machine_deals` хранит имя и паспорт), поэтому сервис схлопывает регистр
    и пробелы.
    """
    from services import receivables

    data = await request.json()
    _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_machines_buyer"
    )
    buyer = (data.get("buyer") or "").strip()[:200]
    if not buyer:
        raise HTTPException(status_code=400, detail="buyer обязателен")
    card = await receivables.buyer_card(buyer)
    if not card:
        raise HTTPException(status_code=404, detail="Покупатель не найден")
    return JSONResponse({"ok": True, **card})


# ─── API: канал ──────────────────────────────────────────────────────────────
# Канал — лицо компании: черновик собирает сервер, публикует человек кнопкой.
# Ни один сборщик не выпускает наружу количества (см. `services/channel.py`).

_CHANNEL_ROLES = ("admin", "boss")


async def _photo_bytes(tg_file_id: str, cache_key: str) -> bytes | None:
    """Байты фото из Telegram с тем же кэшем, что у фото техники."""
    cached = _photo_cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        bot = await get_notify_bot()
        meta = await bot.get_file(tg_file_id)
        if (meta.file_size or 0) > _PHOTO_MAX_BYTES:
            return None
        buf = await bot.download_file(meta.file_path)
        blob = buf.read() if hasattr(buf, "read") else bytes(buf)
    except Exception as e:
        logger.warning("Фото недоступно: %s", redact_token(repr(e)))
        return None
    _photo_cache_put(cache_key, blob)
    return blob


@app.post("/api/products/search")
async def api_products_search(request: Request):
    """Поиск товара в номенклатуре — для подсказок при вводе позиции.

    Читаем свою таблицу `products`: каталог теперь наш, и подсказка стоит один
    локальный запрос (`warehouse.search_products`: название или артикул, ё = е,
    с остатком).
    """
    data = await request.json()
    _authorize(
        data, allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_products_search", rate_limit_max=240,
    )
    query = (data.get("query") or "").strip()[:100]
    # `browse` — выбор из списка (шторка товара): список виден сразу, до первой
    # буквы, и сужается по мере ввода. Без него — подсказка под полем ввода,
    # где одна буква ещё не запрос и первые товары каталога наугад не нужны.
    browse = bool(data.get("browse"))
    if len(query) < 2 and not browse:
        return JSONResponse({"ok": True, "products": [], "query": query})
    try:
        limit = int(data.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    from services import warehouse

    rows = await warehouse.search_products(query, limit, browse=browse)
    return JSONResponse({"ok": True, "products": rows, "query": query})


@app.post("/api/products/photo")
async def api_products_photo(request: Request):
    """Отдать фото товара байтами. Прямую ссылку Telegram отдавать нельзя —
    в ней токен бота."""
    from services import product_photos

    data = await request.json()
    _authorize(
        data, allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_products_photo", rate_limit_max=120,
    )
    product_ref = _product_ref(data)
    photo_id = _machine_id_arg(data, "photo_id")
    photos = await product_photos.list_photos(product_ref)
    photo = next((p for p in photos if int(p["id"]) == photo_id), None)
    if not photo:
        raise HTTPException(status_code=404, detail="Фото не найдено")

    blob = await _photo_bytes(str(photo["tg_file_id"]), str(photo["file_unique_id"]))
    if blob is None:
        raise HTTPException(status_code=404, detail="Фото недоступно")
    return Response(
        blob, media_type=_photo_media_type(blob) or "image/jpeg",
        headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/products/photos")
async def api_products_photos(request: Request):
    """Список фото товара. `tg_file_id` наружу не отдаём — клиенту нужен только
    `photo_id`, а файловый URL Telegram содержит токен бота."""
    from services import product_photos

    data = await request.json()
    _authorize(
        data, allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_products_photos",
    )
    product_ref = _product_ref(data, required=True)
    photos = await product_photos.list_photos(product_ref)
    return JSONResponse({
        "ok": True,
        "photos": [
            {
                "id": int(p["id"]),
                "caption": p.get("caption") or "",
                "uploaded_at": p.get("uploaded_at") or "",
            }
            for p in photos
        ],
        "can_upload": _machine_photos_chat_id() is not None,
    })


@app.post("/api/products/photo_delete")
async def api_products_photo_delete(request: Request):
    """Открепить фото товара. Скоупится товаром — иначе `photo_id` из формы
    стирает чужой снимок."""
    from services import product_photos

    data = await request.json()
    _authorize(
        data, allowed_roles=_CHANNEL_ROLES, rate_limit_scope="api_products_photo_delete"
    )
    product_ref = _product_ref(data, required=True)
    photo_id = _machine_id_arg(data, "photo_id")
    if not product_ref:
        raise HTTPException(status_code=400, detail="Не указан товар")
    return _machine_response(await product_photos.delete_photo(product_ref, photo_id))


@app.post("/api/products/photo_upload")
async def api_products_photo_upload(request: Request):
    """Загрузить фото товара. base64 в JSON — как у техники: `python-multipart`
    в зависимостях нет."""
    from services import product_photos

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CHANNEL_ROLES, rate_limit_scope="api_products_photo_upload",
        # Пачкой грузят по одному запросу на снимок: карточка товара с десятком
        # ракурсов — это одно действие человека, а не подозрительная активность.
        rate_limit_max=60,
    )
    product_ref = _product_ref(data, required=True)
    chat_id = _machine_photos_chat_id()
    if chat_id is None:
        raise HTTPException(
            status_code=503,
            detail="Загрузка фото не настроена: нет PHOTOS_TG_CHAT_ID",
        )
    blob = _decode_photo(data.get("data_url"))

    try:
        from aiogram.types import BufferedInputFile

        bot = await get_notify_bot()
        sent = await bot.send_photo(
            chat_id, BufferedInputFile(blob, filename=f"product-{product_ref}.jpg"),
            caption=(data.get("caption") or "")[:200] or None,
        )
    except Exception as e:
        logger.warning("Фото товара не загружено: %s", redact_token(repr(e)))
        raise HTTPException(status_code=502, detail="Telegram не принял фото")

    best = max(sent.photo or [], key=lambda p: (p.width or 0) * (p.height or 0), default=None)
    if best is None:
        raise HTTPException(status_code=502, detail="Telegram не вернул файл")
    res = await product_photos.add_photo(
        product_ref, tg_file_id=best.file_id, file_unique_id=best.file_unique_id,
        uploaded_by=user["id"], caption=(data.get("caption") or "")[:200] or None,
    )
    return _machine_response(res)


def _decode_photo(raw_url) -> bytes:
    """data-URL → байты, с теми же проверками, что у фото техники."""
    raw = str(raw_url or "")
    if not raw.startswith("data:image/") or "," not in raw:
        raise HTTPException(status_code=400, detail="Ожидается изображение")
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
    return blob


@app.post("/api/channel/draft")
async def api_channel_draft(request: Request):
    """Черновик поста: текст собирает СЕРВЕР, а не фронт.

    Так правило «наружу не уходят количества» держится в одном месте и
    проверяется тестом, а не повторяется в шаблоне.
    """
    from services import channel

    data = await request.json()
    _authorize(data, allowed_roles=_CHANNEL_ROLES, rate_limit_scope="api_channel_draft")
    kind = (data.get("kind") or "").strip()
    if kind not in channel.POST_KINDS:
        raise HTTPException(status_code=400, detail=f"Тип поста: {', '.join(channel.POST_KINDS)}")

    username = (data.get("manager_username") or "").strip()[:64] or None
    note = (data.get("note") or "").strip()[:500] or None
    ref = None
    photo_id = None

    if kind == "arrival":
        container_id = _machine_id_arg(data, "container_id")
        ref = str(container_id)
        names = await channel.arrival_names(container_id)
        if not names:
            raise HTTPException(status_code=409, detail="В контейнере нет прибывших позиций")
        text = channel.build_arrival(names, note=note, manager_username=username)
    elif kind == "showcase":
        from services import product_photos, warehouse

        product_ref = _product_ref(data, required=True)
        product = await warehouse.get_product(product_ref)
        if not product:
            raise HTTPException(status_code=404, detail="Товар не найден в каталоге")
        ref = product_ref
        prices = await _price_label(product_ref)
        text = channel.build_showcase(
            product, price=prices, note=note, manager_username=username
        )
        first = await product_photos.first_photo(product_ref)
        photo_id = int(first["id"]) if first else None
    else:
        names = [str(n)[:200] for n in (data.get("names") or []) if str(n).strip()][:30]
        if not names:
            raise HTTPException(status_code=400, detail="Выберите хотя бы один товар")
        text = channel.build_stale(names, note=note, manager_username=username)

    return JSONResponse({
        "ok": True, "kind": kind, "ref": ref, "text": text, "photo_id": photo_id,
        "already_posted": await channel.already_posted(kind, ref) if ref else None,
        "can_publish": _channel_id() is not None,
    })


async def _price_label(product_ref: str) -> str | None:
    """Цена товара для витрины — только если её задавали руками."""
    from services import async_db as adb
    from config import BASE_CURRENCY

    prices = await adb.get_product_prices_by_ids([product_ref])
    row = prices.get(product_ref) or {}
    price = row.get("sale_price")
    if price is None or price == "":
        return None
    cents = money.parse_amount(price)
    if cents is None:
        return None
    return f"{money.format_cents(cents, decimals=0, sep=' ')} " \
           f"{(row.get('currency') or BASE_CURRENCY or 'USD').upper()}"


@app.post("/api/channel/publish")
async def api_channel_publish(request: Request):
    """Опубликовать пост. Только по нажатию человеком — автопостинга нет.

    Текст принимаем от клиента: черновик правят руками, и запрещать это значит
    заставлять публиковать не то, что хотели. Сборщик при этом количеств не
    выпускает — если человек допишет их сам, это его решение.
    """
    from services import channel

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CHANNEL_ROLES, rate_limit_scope="api_channel_publish",
        rate_limit_max=20,
    )
    chat_id = _channel_id()
    if chat_id is None:
        raise HTTPException(status_code=503, detail="Канал не настроен: нет CHANNEL_ID")
    kind = (data.get("kind") or "").strip()
    if kind not in channel.POST_KINDS:
        raise HTTPException(status_code=400, detail="Неизвестный тип поста")
    text = (data.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустой пост публиковать нечего")
    ref = (data.get("ref") or "").strip()[:64] or None

    photo_blob = None
    photo_id = data.get("photo_id")
    if photo_id and _product_ref(data):
        from services import product_photos

        photos = await product_photos.list_photos(_product_ref(data))
        photo = next((p for p in photos if int(p["id"]) == int(photo_id)), None)
        if photo:
            photo_blob = await _photo_bytes(
                str(photo["tg_file_id"]), str(photo["file_unique_id"])
            )

    try:
        bot = await get_notify_bot()
        if photo_blob:
            from aiogram.types import BufferedInputFile

            sent = await bot.send_photo(
                chat_id, BufferedInputFile(photo_blob, filename="post.jpg"),
                caption=text[:1024], parse_mode="HTML",
            )
        else:
            sent = await bot.send_message(chat_id, text[:4096], parse_mode="HTML")
    except Exception as e:
        logger.warning("Пост в канал не ушёл: %s", redact_token(repr(e)))
        raise HTTPException(status_code=502, detail="Telegram не принял пост")

    post_id = await channel.save_post(
        kind=kind, ref=ref, message_id=getattr(sent, "message_id", None),
        posted_by=user["id"],
    )
    return JSONResponse({"ok": True, "post_id": post_id,
                         "message_id": getattr(sent, "message_id", None)})


@app.post("/api/channel/stale")
async def api_channel_stale(request: Request):
    """Кандидаты в пост «залежавшееся». Внутренний экран — остаток здесь виден.

    Считается тем же кодом, что дневная ops-сводка: остатки минус всё, что
    отгружалось за период.
    """
    from services import channel

    data = await request.json()
    _authorize(data, allowed_roles=_CHANNEL_ROLES, rate_limit_scope="api_channel_stale",
               rate_limit_max=10)
    days = channel.stale_days()
    from tasks.run_ops_monitor import collect_dead_stock

    try:
        dead = await collect_dead_stock(days)
    except Exception as e:
        logger.warning("Не удалось собрать залежавшееся: %s", e)
        raise HTTPException(status_code=502, detail="МойСклад не ответил, попробуйте позже")
    return JSONResponse({
        "ok": True, "days": days, "items": channel.stale_candidates(dead),
    })


@app.post("/api/channel/history")
async def api_channel_history(request: Request):
    from services import channel

    data = await request.json()
    _authorize(data, allowed_roles=_CHANNEL_ROLES, rate_limit_scope="api_channel_history")
    posts = await channel.history()
    # Отклик считаем на каждый пост: это два локальных COUNT'а на строку, без
    # запросов в МойСклад. История ограничена 30 постами, N+1 здесь не страшен.
    for post in posts:
        post["effect"] = await channel.post_effect(post.get("posted_at"))
    return JSONResponse({
        "ok": True, "posts": posts, "kind_labels": channel.KIND_LABELS,
        "can_publish": _channel_id() is not None,
    })


# ─── API: воронка клиентов ───────────────────────────────────────────────────
# Данные наполняет наблюдатель переписок (`handlers/business.py`). Здесь только
# чтение и два ручных действия: исход сделки и привязка к контрагенту — их из
# переписки не вывести.

_LEAD_ROLES = ("admin", "boss", "manager")


@app.post("/api/leads/list")
async def api_leads_list(request: Request):
    """Лиды. Менеджер видит только свои — чужие переписки не его дело."""
    from services import leads

    data = await request.json()
    user = _authorize(data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_list")
    role = get_role(user["id"])
    is_boss = role in ("admin", "boss")
    status = (data.get("status") or "").strip() or None
    if status and status not in leads.STATUSES:
        raise HTTPException(status_code=400, detail=f"Неизвестный статус: {status}")
    # Исход и состояние разговора — разные вопросы («купил ли» и «на ком ход»),
    # поэтому это два независимых отбора, а не один общий список значений.
    state = (data.get("state") or "").strip() or None
    if state and state not in leads.STATE_FILTERS:
        raise HTTPException(status_code=400, detail=f"Неизвестное состояние: {state}")

    rows = await leads.list_leads(
        manager_id=None if is_boss else user["id"], status=status, state=state,
        search=(data.get("search") or "").strip()[:100] or None,
    )
    from services import lead_calls

    return JSONResponse({
        "ok": True,
        "leads": rows,
        "scope": "company" if is_boss else "personal",
        "status_labels": leads.STATUS_LABELS,
        "connections": await leads.list_connections() if is_boss else [],
        # Звонки без переписки — люди, которых в Telegram ещё нет. Отдаём вместе
        # со списком: это один экран «с кем сегодня работать», и второй запрос
        # ради него был бы лишним.
        # Менеджеру — только свои: в звонке телефон и заметка чужого разговора.
        "unlinked_calls": await lead_calls.list_calls(
            unlinked=True, limit=50, manager_id=None if is_boss else user["id"],
        ),
    })


@app.post("/api/leads/card")
async def api_leads_card(request: Request):
    """Карточка лида: отметки времени и события. Текстов переписки здесь нет —
    мы их не храним."""
    from services import leads

    data = await request.json()
    user = _authorize(data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_card")
    lead_id = _machine_id_arg(data, "lead_id")
    lead = await leads.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if get_role(user["id"]) not in ("admin", "boss") and lead.get("manager_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Это не ваш клиент")
    from services import lead_calls

    return JSONResponse({
        "ok": True, "lead": lead, "status_labels": leads.STATUS_LABELS,
        "lost_reasons": [
            {"key": k, "label": leads.LOST_REASON_LABELS[k]} for k in leads.LOST_REASONS
        ],
        "direction_labels": lead_calls.DIRECTION_LABELS,
        "source_labels": lead_calls.SOURCE_LABELS,
    })


@app.post("/api/leads/status")
async def api_leads_status(request: Request):
    """Отметить исход. Руками — в переписке его не видно: клиент может
    согласиться голосом, а может пропасть без слова."""
    from services import leads

    data = await request.json()
    user = _authorize(data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_status")
    lead_id = _machine_id_arg(data, "lead_id")
    lead = await leads.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if get_role(user["id"]) not in ("admin", "boss") and lead.get("manager_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Это не ваш клиент")
    res = await leads.set_status(
        lead_id, (data.get("status") or "").strip(),
        user_id=user["id"], full_name=_actor_name(user),
        # Причина отказа необязательна: обязательное поле на редко нажимаемой
        # кнопке приводит к тому, что её перестают нажимать вовсе.
        reason=(data.get("reason") or "").strip() or None,
        note=_machine_text(data, "note", 500),
    )
    return _machine_response(res)


@app.post("/api/leads/agents")
async def api_leads_agents(request: Request):
    """Поиск контрагента для привязки — по названию ИЛИ телефону.

    Телефон важнее названия: клиента помнят по номеру, а в справочнике он
    записан как «ООО Бахор Савдо».
    """
    from services import counterparties as cp_service

    data = await request.json()
    _authorize(
        data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_agents",
        rate_limit_max=240,
    )
    search = (data.get("search") or "").strip()[:100]
    rows = await cp_service.search(search or None, 20)
    return JSONResponse({"ok": True, "agents": rows})


@app.post("/api/leads/create_agent")
async def api_leads_create_agent(request: Request):
    """Завести контрагента в справочнике по клиенту и сразу привязать.

    Заводит ЧЕЛОВЕК кнопкой: каждый написавший — ещё не клиент, автосоздание
    превратило бы справочник в свалку из случайных собеседников.
    """
    from services import counterparties as cp_service
    from services import leads

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_create_agent",
        rate_limit_max=30,
    )
    lead_id = _machine_id_arg(data, "lead_id")
    lead = await leads.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if get_role(user["id"]) not in ("admin", "boss") and lead.get("manager_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Это не ваш клиент")

    name = _machine_text(data, "name", 255) or lead.get("display_name") or lead.get("username")
    created = await cp_service.create(
        name or "",
        phone=_machine_text(data, "phone", 64),
        telegram_id=lead.get("tg_user_id"),
    )
    if not created.get("ok"):
        return _machine_response(created)
    res = await leads.link_agent(
        lead_id, str(created["counterparty_id"]), user_id=user["id"],
        full_name=_actor_name(user),
    )
    if not res.get("ok"):
        return _machine_response(res)
    return JSONResponse({**res, "name": created["name"], "existed": created["existed"]})


def _is_lead_boss(user: dict) -> bool:
    return get_role(user["id"]) in ("admin", "boss")


async def _require_own_lead(lead_id: int, user: dict) -> dict:
    """Лид, к которому у пользователя есть доступ: руководству любой,
    менеджеру — только свой (тот же гейт, что у карточки и статуса)."""
    from services import leads

    lead = await leads.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if not _is_lead_boss(user) and lead.get("manager_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Это не ваш клиент")
    return lead


async def _require_own_call(call_id: int, user: dict) -> dict:
    """Звонок, который пользователь вправе трогать: руководству любой,
    менеджеру — только записанный им самим."""
    from services import lead_calls

    call = await lead_calls.get_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Звонок не найден")
    if not _is_lead_boss(user) and call.get("manager_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Это не ваш звонок")
    return call


@app.post("/api/leads/calls")
async def api_leads_calls(request: Request):
    """Журнал звонков. Без `lead_id` отдаёт непривязанные — тех, кого ещё не
    нашли в Telegram; это и есть список «кому перезвонить».

    Менеджер: по лиду — только по своему лиду; непривязанные — только свои."""
    from services import lead_calls

    data = await request.json()
    user = _authorize(data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_calls")
    lead_id = _machine_id_arg(data, "lead_id") if data.get("lead_id") else None
    if lead_id is not None:
        await _require_own_lead(lead_id, user)
    rows = await lead_calls.list_calls(
        lead_id=lead_id,
        unlinked=lead_id is None,
        manager_id=None if (lead_id is not None or _is_lead_boss(user)) else user["id"],
    )
    return JSONResponse({
        "ok": True,
        "calls": rows,
        "direction_labels": lead_calls.DIRECTION_LABELS,
        "source_labels": lead_calls.SOURCE_LABELS,
    })


@app.post("/api/leads/call_add")
async def api_leads_call_add(request: Request):
    """Записать звонок. Обязателен только менеджер: половину звонков заносят
    постфактум, когда номера уже нет под рукой, а «звонок без номера» — всё
    ещё обращение. Обязательное поле здесь означало бы, что звонки перестанут
    записывать вовсе."""
    from services import lead_calls

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_call_add",
        rate_limit_max=120,
    )
    lead_id = data.get("lead_id")
    if lead_id:
        # Свой звонок к чужому клиенту не подшивается — как и в call_link.
        await _require_own_lead(_machine_id_arg(data, "lead_id"), user)
    res = await lead_calls.add_call(
        manager_id=user["id"],
        phone=_machine_text(data, "phone", 64),
        display_name=_machine_text(data, "display_name", 200),
        direction=(data.get("direction") or "in").strip(),
        source=(data.get("source") or "").strip() or None,
        interest=_machine_text(data, "interest", 200),
        lead_id=_machine_id_arg(data, "lead_id") if lead_id else None,
        note=_machine_text(data, "note", 500),
    )
    return _machine_response(res)


@app.post("/api/leads/call_link")
async def api_leads_call_link(request: Request):
    """Связать записанный звонок с телеграм-лидом. Руками: Telegram номер
    собеседника не отдаёт, общего поля у звонка с перепиской нет, и угадывание
    означало бы чужой звонок в чужой карточке."""
    from services import lead_calls

    data = await request.json()
    data_user = _authorize(
        data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_call_link"
    )
    lead_id = _machine_id_arg(data, "lead_id")
    call_id = _machine_id_arg(data, "call_id")
    # Тот же гейт, что у карточки и статуса: менеджер, который не может даже
    # открыть чужого клиента, не должен подшивать к нему свой звонок. И звонок
    # тоже должен быть его: иначе чужой звонок уезжает в его карточку.
    await _require_own_lead(lead_id, data_user)
    await _require_own_call(call_id, data_user)

    res = await lead_calls.link_call(call_id, lead_id, user_id=data_user["id"])
    return _machine_response(res)


@app.post("/api/leads/call_delete")
async def api_leads_call_delete(request: Request):
    """Удалить ошибочную запись. Звонок — заметка менеджера, а не денежный
    факт: запрещать правку значит копить мусор в списке «перезвонить»."""
    from services import lead_calls

    data = await request.json()
    user = _authorize(data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_call_delete")
    call_id = _machine_id_arg(data, "call_id")
    await _require_own_call(call_id, user)
    res = await lead_calls.delete_call(call_id)
    return _machine_response(res)


@app.post("/api/leads/link")
async def api_leads_link(request: Request):
    """Связать лид с контрагентом МойСклад — чтобы «написал» и «купил»
    встретились."""
    from services import leads

    data = await request.json()
    user = _authorize(data, allowed_roles=_LEAD_ROLES, rate_limit_scope="api_leads_link")
    lead_id = _machine_id_arg(data, "lead_id")
    # Проверка владения была только у карточки и статуса — менеджер мог привязать
    # контрагента к чужому лиду. Ручку до сих пор не звал фронт, поэтому дыра и
    # не всплыла; закрываем прежде, чем кнопка появится.
    lead = await leads.get_lead(lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Лид не найден")
    if get_role(user["id"]) not in ("admin", "boss") and lead.get("manager_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Это не ваш клиент")
    counterparty_id = (data.get("counterparty_id") or "").strip()[:64] or None
    res = await leads.link_agent(
        lead_id, counterparty_id, user_id=user["id"], full_name=_actor_name(user)
    )
    return _machine_response(res)


@app.post("/api/leads/funnel")
async def api_leads_funnel(request: Request):
    """Воронка за период + разрез по менеджерам. Только руководство."""
    from datetime import datetime

    from services import leads

    data = await request.json()
    _authorize(
        data, allowed_roles=("admin", "boss"), rate_limit_scope="api_leads_funnel"
    )
    since, until, _prev, label = _resolve_analytics_period(data, datetime.now())
    since_s, until_s = since.strftime("%Y-%m-%d"), until.strftime("%Y-%m-%d")

    managers = await leads.by_manager(since_s, until_s)
    names = await _owner_names([m["manager_id"] for m in managers])
    for row in managers:
        row["name"] = names.get(row["manager_id"], f"#{row['manager_id']}")

    return JSONResponse({
        "ok": True,
        "funnel": await leads.funnel(since_s, until_s),
        "by_manager": managers,
        "awaiting": await leads.awaiting_reply(),
        "period": {"label": label, "since": since_s, "until": until_s},
    })


# ─── API: заказы ─────────────────────────────────────────────────────────────

# Страница списка заказов. Без страниц /api/orders отдавал ВСЕ заказы с
# позициями: у руководства за год это мегабайты JSON на каждый вход во вкладку
# по мобильной сети. Фильтры статуса и периода применяются ДО нарезки, иначе
# «Показать ещё» листал бы нефильтрованный список и на странице с фильтром
# оказывалось бы два заказа из двадцати.
_ORDERS_PAGE_MAX = 200
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _orders_page_params(data: dict) -> dict | None:
    """Параметры страницы из тела запроса или None — старый режим «всё сразу».

    Старый режим оставлен для вызовов без `limit` (бот, скрипты, тесты
    нагрузки): их ответ не меняется. Даты — строки YYYY-MM-DD в поясе
    клиента: «сегодня» считает телефон, у сервера пояс может быть другим.
    """
    raw_limit = data.get("limit")
    if raw_limit is None:
        return None
    try:
        limit = int(raw_limit)
        offset = int(data.get("offset") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректные limit/offset") from None
    limit = max(1, min(limit, _ORDERS_PAGE_MAX))
    offset = max(0, offset)
    raw_statuses = data.get("statuses") or []
    if not isinstance(raw_statuses, list):
        raise HTTPException(status_code=400, detail="statuses — список статусов")
    statuses = [str(x) for x in raw_statuses if x]
    date_from = str(data.get("date_from") or "")
    date_to = str(data.get("date_to") or "")
    for d in (date_from, date_to):
        if d and not _DATE_RE.match(d):
            raise HTTPException(status_code=400, detail="Дата периода — в формате ГГГГ-ММ-ДД")
    return {
        "limit": limit,
        "offset": offset,
        "statuses": statuses,
        "date_from": date_from,
        "date_to": date_to,
    }


def _paginate_orders(
    orders: list[dict],
    *,
    limit: int,
    offset: int,
    statuses: list[str],
    date_from: str,
    date_to: str,
) -> tuple[list[dict], dict]:
    """Отфильтровать (статус, период) и нарезать страницу — в Python.

    Ручка режет страницу в SQL (`database.get_orders_page`); эта функция —
    эталон семантики фильтров и полей страницы, с которым SQL-вариант сверяет
    `tests/test_orders_pagination.py`.

    `orders` уже отсортированы свежими вперёд (ORDER BY created_at DESC) —
    порядок страниц держится на нём. `pending_count` считается по ВСЕМ
    заказам роли, без фильтров: строка «Заявки на рассмотрении» у
    руководства не должна пропадать, когда выбран фильтр «Отгружены» или
    нужная заявка лежит на второй странице.
    """
    pending_count = sum(1 for o in orders if o.get("status") == "pending")
    wanted = set(statuses)

    def keep(o: dict) -> bool:
        if wanted and o.get("status") not in wanted:
            return False
        day = str(o.get("created_at") or "")[:10]
        if date_from and (not day or day < date_from):
            return False
        return not (date_to and (not day or day > date_to))

    filtered = [o for o in orders if keep(o)]
    chunk = filtered[offset : offset + limit]
    return chunk, _page_meta(
        total=len(filtered), offset=offset, limit=limit, returned=len(chunk),
        pending_count=pending_count,
    )


def _page_meta(*, total: int, offset: int, limit: int, returned: int, pending_count: int) -> dict:
    """Поля страницы в ответе `/api/orders` — одна форма для SQL-страницы
    (`database.get_orders_page`) и для эталонной нарезки `_paginate_orders`."""
    next_offset = offset + returned
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": next_offset < total,
        "next_offset": next_offset,
        "pending_count": pending_count,
    }


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
    page = _orders_page_params(data)

    page_meta: dict = {}
    if page is not None:
        # Страница — целиком в SQL (фильтры, LIMIT/OFFSET, total, pending_count):
        # раньше читались ВСЕ заказы роли и резались в Python, а позиции шли
        # одним IN по всем id — на истории это предел asyncpg в 32 767
        # параметров и мегабайты в памяти на каждый вход во вкладку.
        if role in ("admin", "boss"):
            scope = "all"
        elif role == "warehouse_keeper":
            scope = "to_ship"
        else:
            # Со страницами — только свои заказы, как и было (совмещение ролей
            # добавляет чужие «к отгрузке» лишь в ответе без limit).
            scope = "user"
        orders, total, pending_count = await adb.get_orders_page(
            scope=scope,
            user_id=user["id"],
            statuses=page["statuses"],
            date_from=page["date_from"],
            date_to=page["date_to"],
            limit=page["limit"],
            offset=page["offset"],
        )
        page_meta = _page_meta(
            total=total, offset=page["offset"], limit=page["limit"],
            returned=len(orders), pending_count=pending_count,
        )
    elif role in ("admin", "boss"):
        orders = await adb.get_all_orders()
    elif role == "warehouse_keeper":
        # Кладовщик заказов не создаёт — его список это то, что он отгружает:
        # одобренные (ждут его) и уже отгруженные. «Свои заказы» у него пусты,
        # и кнопка «Отгрузить» в WebApp никогда не появлялась (нашёл E2E
        # test_keeper_marks_approved_order_shipped); отгружать он мог только
        # командой /ship в боте.
        orders = [
            o for o in await adb.get_all_orders() if o.get("status") in ("approved", "shipped")
        ]
    else:
        orders = await adb.get_user_orders(user["id"])
        from services.roles import role_allowed

        if role_allowed(role, ("warehouse_keeper",)):
            # Совмещение ролей: менеджер отгружает за кладовщика, поэтому к своим
            # заказам добавляем чужие одобренные/отгруженные — иначе кнопке
            # «Отгрузить» не на чем появиться.
            own_ids = {o["id"] for o in orders}
            to_ship = [
                o for o in await adb.get_all_orders()
                if o.get("status") in ("approved", "shipped") and o["id"] not in own_ids
            ]
            orders = sorted(
                [*orders, *to_ship], key=lambda o: o.get("created_at") or "", reverse=True
            )

    from config import BASE_CURRENCY

    is_boss = role in ("admin", "boss")

    # Батч-загрузка позиций: один SQL вместо N (N+1 был на больших списках)
    items_by_order = await adb.get_order_items_by_ids([o["id"] for o in orders]) if orders else {}

    # PR C: прибыль по заказу — ТОЛЬКО boss/admin. Себестоимость из
    # product_prices (батч по всем товарам позиций). profit = Σ (price−cost)×qty.
    # Если у позиции cost неизвестна — заказ помечается profit_partial=True
    # (не врём нулём). Менеджеру profit/cost не отдаём вообще.
    cost_by_product: dict = {}
    if is_boss:
        all_product_ids = {
            str(it["product_id"])
            for items in items_by_order.values()
            for it in items
            if it.get("product_id")
        }
        prices = await adb.get_product_prices_by_ids(sorted(all_product_ids))
        cost_by_product = {
            k: v.get("cost_price") for k, v in prices.items() if v.get("cost_price") is not None
        }
    # Учёт себестоимости включён: у ОТГРУЖЕННОГО заказа прибыль берётся из
    # себестоимости, зафиксированной при отгрузке (партии), а не из ручной.
    shipped_profit: dict = {}
    if is_boss:
        from services import costing

        if await costing.is_enabled():
            shipped_profit = await costing.order_profits([o["id"] for o in orders])

    # Как получены деньги по заказу (разбивка) и сколько по «оплате сразу» ещё
    # не внесено — батчем: без разбивки такой заказ не отгрузить, и карточка
    # показывает «Внести оплату» вместо голой ошибки на «Отгрузить».
    from services import order_payments

    money_ids = [
        o["id"] for o in orders
        if o.get("status") in ("approved", "shipped", "paid", "partially_returned", "returned")
    ]
    parts_by_order = await order_payments.parts_for_orders(money_ids) if money_ids else {}
    gap_ids = [
        o["id"] for o in orders
        if o.get("status") == "approved" and (o.get("payment_type") or "paid") == "paid"
    ]
    gaps = await order_payments.payment_gap_cents(gap_ids) if gap_ids else {}

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
            "payment_parts": parts_by_order.get(o["id"], []),
            # «Оплата сразу», одобрен, а оплата внесена не вся — отгрузка
            # откажет (mark_order_shipped), пока менеджер не внесёт разбивку.
            "payment_gap": float(money.from_cents(gaps.get(o["id"], 0))),
            "needs_payment": gaps.get(o["id"], 0) > 0,
            "is_mine": o["user_id"] == user["id"],
            "items": [
                {
                    # id позиции — им редактор удаляет строку (`/api/orders/remove_item`).
                    # Без него фронт подставлял порядковый номер и бил в чужую позицию.
                    "id": it["id"],
                    "name": it["product_name"],
                    "quantity": it["quantity"],
                    "unit": it["unit"],
                    "price": float(it.get("price", 0) or 0),
                }
                for it in items
            ],
        }
        if is_boss and o["id"] in shipped_profit:
            entry["profit"] = shipped_profit[o["id"]]["profit"]
            entry["profit_partial"] = shipped_profit[o["id"]]["partial"]
            entry["profit_source"] = "batches"
        elif is_boss:
            profit = 0.0
            partial = False
            for it in items:
                cost = cost_by_product.get(str(it.get("product_id") or ""))
                qty = float(it.get("quantity", 0) or 0)
                price = float(it.get("price", 0) or 0)
                if cost is None:
                    partial = True  # себестоимость не задана — не учитываем
                else:
                    profit += (price - float(cost)) * qty
            entry["profit"] = round(profit, 2)
            entry["profit_partial"] = partial  # True = часть позиций без cost
        result.append(entry)

    return JSONResponse(
        {"orders": result, "role": role, "default_currency": BASE_CURRENCY, **page_meta}
    )


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

    from config import BASE_CURRENCY
    from services import async_db as adb

    requests = await adb.get_pending_requests()
    # Батч-загрузка заказов и позиций — один SQL на каждое вместо 2N.
    order_ids = [r["order_id"] for r in requests]
    orders_by_id = await adb.get_orders_by_ids(order_ids) if order_ids else {}
    items_by_order = await adb.get_order_items_by_ids(order_ids) if order_ids else {}
    # Кредит-контекст (долг + лимит контрагента) — тоже батчем, а не по одной
    # заявке: по 6 запросов на строку, 60 заявок = 360 запросов. Сторож —
    # tests/perf/test_query_counts.py::test_pending_requests_are_batched.
    from services.order_workflow import orders_credit_context

    totals: dict[int, float] = {}
    for r in requests:
        items = items_by_order.get(r["order_id"], [])
        totals[r["order_id"]] = sum(
            float(it.get("quantity", 0)) * float(it.get("price", 0) or 0) for it in items
        )
    credit_ctx = await orders_credit_context(
        [(orders_by_id[oid], totals[oid]) for oid in order_ids if oid in orders_by_id]
    )
    result = []
    for r in requests:
        order = orders_by_id.get(r["order_id"])
        items = items_by_order.get(r["order_id"], []) if order else []
        total = totals[r["order_id"]] if order else 0.0
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
            "currency": (order.get("currency") if order else None) or BASE_CURRENCY,
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
        ctx = credit_ctx.get(r["order_id"])
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
    """Карточка контрагента: имя/телефон + долг/лимит + заказы в боте +
    покупки (расходные накладные склада). Только начальство.

    Баланса взаиморасчётов МойСклад здесь больше нет: «сколько должен»
    считает `get_agent_current_debt` по нашим же заказам, и второй ответ на
    тот же вопрос с ним бы расходился.
    """
    from services import async_db as adb
    from services import counterparties as cp_service
    from services import warehouse

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

    cp = await cp_service.get(agent_id)
    debt = await adb.get_agent_current_debt(agent_id)
    limit = await adb.get_credit_limit(agent_id)
    orders = await adb.get_orders_by_agent(agent_id)
    # История денег по клиенту: платежи, сдачи (в части, распределённой на его
    # заказы) и возвраты. Формат строки — как в общей ленте «Деньги», поэтому
    # фронт рисует её тем же кодом.
    money_history = await adb.get_agent_money_history(agent_id)
    purchases = await warehouse.counterparty_purchases(agent_id)
    from config import BASE_CURRENCY

    return JSONResponse(
        {
            "ok": True,
            "agent_id": agent_id,
            "name": (cp or {}).get("name") or "",
            "phone": (cp or {}).get("phone") or "",
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


@app.post("/api/clients/shipment")
async def api_clients_shipment(request: Request):
    """Состав отгрузки клиента: позиции расходной накладной.

    В карточке клиента отгрузки показывались одной суммой и датой — увидеть,
    ЧТО именно уехало, было нельзя, хотя это первый вопрос при разборе долга.
    """
    from services import warehouse

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss"),
        rate_limit_scope="api_clients_shipment",
        rate_limit_max=60,
    )
    invoice_id = _machine_id_arg(data, "invoice_id")
    invoice = await warehouse.get_invoice(invoice_id)
    if not invoice or invoice.get("type") != "outgoing":
        raise HTTPException(status_code=404, detail="Накладная не найдена")

    positions = []
    # Итог копим в цикле, а не пересобираем генератором из уже готовых строк:
    # значения словаря позиции — объединение типов (строки, float, int), и
    # sum() по ним не проходит проверку типов (mypy — блокирующий гейт).
    total_cents = 0
    for pos in invoice.get("items") or []:
        quantity = float(pos.get("quantity", 0) or 0)
        price_cents = int(pos.get("price_cents", 0) or 0)
        line_cents = money.mul_qty(price_cents, quantity)
        total_cents += line_cents
        positions.append(
            {
                "name": pos.get("product_name") or "—",
                "quantity": quantity,
                "unit": pos.get("unit") or "шт",
                "price_cents": price_cents,
                "sum_cents": line_cents,
            }
        )

    return JSONResponse(
        {
            "ok": True,
            "invoice_id": invoice_id,
            "number": invoice.get("invoice_number"),
            "positions": positions,
            "sum_cents": total_cents,
            "currency": (invoice.get("currency") or "USD").upper(),
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
    if isinstance(rate, bool):
        # JSON true/false — float(True) == 1.0 прошёл бы как «курс 1».
        raise HTTPException(status_code=400, detail="Курс должен быть числом")
    # Ручная правка: границы пары + метка 'manual' в дневном архиве, чтобы
    # ночной синк с ЦБ не перезаписал её в тот же день.
    ok, err = await adb.set_currency_rate_manual(code, rate, user["id"])
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
      {"initData": "...", "product_id": N, "product_name": "...",
       "sale_price": 150.0, "cost_price": 100.0, "currency": "USD"}
    sale_price/cost_price опциональны (null = не задавать/сбросить).
    """
    from services import async_db as adb

    data = await request.json()
    user = _authorize(
        data, allowed_roles=("admin", "boss"), rate_limit_scope="api_products_prices_set"
    )
    ms_id = _product_ref(data, required=True)
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
    return JSONResponse({"ok": True, "product_id": ms_id})


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
    from services import machine_deal_requests as mdr
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
    # «Ждёт одобрения» — живая заявка на сделку, а не статус машины (статус
    # меняет только одобрение). Одним запросом на весь список.
    active = await mdr.active_by_machine([int(m["id"]) for m in rows if m.get("id")])
    for m in rows:
        m["pending_request"] = active.get(int(m["id"])) if m.get("id") else None
    pending_total = len(await mdr.list_requests(statuses=("pending",)))
    return JSONResponse(
        {
            "ok": True,
            "machines": rows,
            "counts": counts,
            "status": status or "all",
            "pending_requests": pending_total,
            # Флаг `delete_requires_boss` фронт берёт из /api/me (один источник);
            # здесь — уже посчитанное право роли.
            "can_delete": await _can_delete(role),
            "can_manage": role in _MACHINE_BOSS,
            "can_see_cost": machines.can_see_cost(role),
            "status_labels": machines.STATUS_LABELS,
        }
    )


@app.post("/api/machines/card")
async def api_machines_card(request: Request):
    """Карточка машины: данные, фото, история моточасов, сделки, переходы."""
    from services import machine_deal_requests as mdr
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
    # График рассрочки кладём внутрь сделки: отдельная ручка означала бы второй
    # запрос ровно за тем, что и так открыто на экране.
    for deal in deals:
        if deal.get("kind") == "credit":
            progress = await machines.deal_progress(int(deal["id"]))
            # Платежи отдаём уже с покрытием: клиент вносит частями, и «оплачен
            # или нет» на экране мало — видно должно быть, сколько внесено.
            deal["payments"] = progress["payments"]
            deal["progress"] = {k: v for k, v in progress.items() if k != "payments"}
            deal["receipts"] = await machines.list_receipts(int(deal["id"]))
    # Живая заявка на сделку (ждёт решения или на доработке): пока она есть,
    # новых заявок и ручных переходов у машины нет — решение должно состояться.
    active = (await mdr.active_by_machine([machine_id])).get(machine_id)
    request_view = None
    if active:
        request_view = mdr.visible_request(await mdr.get_request(int(active["id"])), role)
    rights = await mdr.decision_rights(user["id"], role)
    can_request = [] if active else mdr.allowed_kinds(machine.get("status"))
    # Деньги по рассрочке вносит менеджер (решение владельца); стирает —
    # руководство, менеджер только без руководителя (`_machine_money_undo_mode`).
    for deal in deals:
        deal["can_record"] = True
        deal["can_undo"] = bool(rights["can_decide"])
    return JSONResponse(
        {
            "ok": True,
            "machine": machine,
            "photos": [_machine_photo_public(p) for p in photos],
            "hours": hours,
            "deals": deals,
            # Граф переходов приходит с сервера: рисовать его копию на фронте
            # значит завести второй источник правды о жизненном цикле машины.
            "next_statuses": [] if active else machines.next_status_options(machine.get("status")),
            "request": request_view,
            # Какие заявки можно оформить: «Бронь» — со склада, продажа и
            # рассрочка — пока машина не продана. Менеджеру и руководству.
            "can_request": can_request,
            "can_decide": rights["can_decide"],
            "decide_hint": rights["hint"],
            "approvers_exist": rights["exist"],
            "viewer_id": user["id"],
            "can_delete": await _can_delete(role),
            "can_unreserve": (not active) and await mdr.can_unreserve(
                machine, viewer_id=user["id"], role=role, rights=rights),
            # «Прибыла» — работа приёмки, её делает и менеджер (ручка
            # /api/machines/arrive), в отличие от остальных переходов графа.
            "can_arrive": machine.get("status") == "in_transit",
            "can_manage": role in _MACHINE_BOSS,
            # Без канала-хранилища загрузка не работает — кнопку рисовать нельзя.
            "can_upload_photo": _machine_photos_chat_id() is not None,
            "status_labels": machines.STATUS_LABELS,
            # «Сегодня» считает сервер: просрочку платежа нельзя определять по
            # часам телефона — они и в другом поясе, и просто сбиты.
            "today": local_now().date().isoformat(),
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


def _product_ref(data: dict, required: bool = False) -> str:
    """Идентификатор товара из тела запроса — СТРОКОЙ.

    Фото и цены лежат в таблицах, чей ключ (`product_photos.ms_id`,
    `product_prices.ms_id`) остался текстовым с эпохи МойСклад: переименовать
    колонку нечем — инкрементальных миграций в проекте нет. После
    `backfill_local_identifiers` там лежит id НАШЕЙ карточки, поэтому наружу
    поле называется `product_id`, а внутрь уезжает его строковое представление.
    """
    raw = data.get("product_id")
    value = "" if raw is None else str(raw).strip()[:64]
    if required and not value:
        raise HTTPException(status_code=400, detail="Не указан товар")
    return value


def _optional_id(data: dict, key: str) -> int | None:
    """Необязательный положительный id из тела запроса. `None` — не прислали.

    Отличается от `_machine_id_arg` тем, что пустое значение — законный ответ
    «не выбрано», а не 400: поставщик контейнера и карточка товара у позиции
    задаются не всегда.
    """
    raw = data.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key}: не число")
    if value <= 0:
        raise HTTPException(status_code=400, detail=f"{key}: должен быть положительным")
    return value


def _machine_money(raw, label: str, *, allow_zero: bool = False) -> int | None:
    """Сумма из формы («25 000», «25000.50») → копейки.

    Граница системы: наружу и внутрь ходят копейки, парсинг человеческой записи
    живёт ровно здесь. Пустое поле — это «не задано», а не ноль.

    `allow_zero` — поле, где ноль законен: рассрочка без первоначального взноса.
    `parse_amount` ноль отвергает (цена или платёж в ноль — опечатка), и взнос
    «0» получал отказ «не число», хотя клиент просто ничего не внёс.
    """
    if raw is None or str(raw).strip() == "":
        return None
    cents = money.parse_amount(raw)
    if cents is None and allow_zero:
        from decimal import Decimal

        try:
            if Decimal(_normalized_amount(raw)) == 0:
                return 0
        except (ArithmeticError, ValueError):
            pass
    if cents is None:
        raise HTTPException(status_code=400, detail=f"{label}: не число или не больше нуля")
    return cents


def _normalized_amount(raw) -> str:
    """Запись суммы без пробелов-разделителей и с точкой — как её читает `parse_amount`."""
    return str(raw).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")


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

    # VIN правится отдельной функцией сервиса: у него нормализация и проверка
    # уникальности, которых нет у остальных полей. Делаем это ДО прочих правок —
    # если серийник занят, карточка не должна остаться частично изменённой.
    if "vin" in raw:
        vin_res = await machines.change_vin(
            machine_id, str(raw.pop("vin") or ""),
            user_id=user["id"], full_name=_actor_name(user),
        )
        if not vin_res.get("ok"):
            return _machine_response(vin_res)
        if not raw:
            return JSONResponse(vin_res)

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


@app.post("/api/machines/delete")
async def api_machines_delete(request: Request):
    """Удалить карточку машины. Менеджеру — пока руководитель не включил
    `delete_requires_boss` (`_require_delete_right`).

    Для машины со сделкой или живой заявкой сервис откажет: продажа — денежный
    факт, и стирать его вместе с карточкой нельзя. Такие уводят в архив.
    """
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_delete"
    )
    await _require_delete_right(get_role(user["id"]))
    machine_id = _machine_id_arg(data)
    res = await machines.delete_machine(
        machine_id, user_id=user["id"], full_name=_actor_name(user)
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
    from services import adb_core

    active = await adb_core.fetchrow(
        "SELECT id, kind, status, created_by FROM machine_deal_requests "
        "WHERE machine_id = $1 AND status IN ('pending', 'rework') LIMIT 1",
        machine_id,
    )
    if active:
        # Ручной переход из-под заявки менеджера перебил бы решение по ней.
        return _machine_response(machines.pending_refusal(active))
    res = await machines.set_status(
        machine_id, target, user_id=user["id"], full_name=_actor_name(user), expected=expected
    )
    if res.get("ok") and expected == "reserved":
        # Бронь снята руководителем — одобренная бронь менеджера больше не его.
        from services import machine_deal_requests as mdr

        async with adb_core.transaction() as txn:
            await mdr.release_bookings_locked(txn, machine_id)
    return _machine_response(res)


@app.post("/api/machines/unreserve")
async def api_machines_unreserve(request: Request):
    """Снять бронь. Менеджеру — свою бронь (или любую, пока руководителя в
    системе нет); остальные ручные переходы — `/api/machines/status`, руководству."""
    from services import async_db as adb
    from services import machine_deal_requests as mdr

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_unreserve"
    )
    machine_id = _machine_id_arg(data)
    idem = _Idem(adb, "machine_unreserve", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await mdr.unreserve(machine_id, actor_id=user["id"], actor_name=_actor_name(user),
                                  actor_role=get_role(user["id"]))
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_request_response(res)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/machines/arrive")
async def api_machines_arrive(request: Request):
    """Машина прибыла: «В пути» → «На складе» (+ где стоит).

    Открыта менеджеру, в отличие от `/api/machines/status`: встречает технику
    он, и без этой ручки у менеджера на карточке машины «в пути» не было
    ни одной кнопки, кроме моточасов. CAS по статусу — в сервисе; ключ
    идемпотентности отдаёт двойному тапу итог первого нажатия, а не 409.
    """
    from services import async_db as adb
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_arrive"
    )
    machine_id = _machine_id_arg(data)
    idem = _Idem(adb, "machine_arrive", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await machines.mark_arrived(
            machine_id,
            user_id=user["id"],
            full_name=_actor_name(user),
            location=_machine_text(data, "location", 200),
        )
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/machines/deal")
async def api_machines_deal(request: Request):
    """Бронь, продажа или рассрочка (`kind`: reserve | sale | credit).

    Менеджер → заявка на одобрение руководителю (`pending: true`, машина пока
    в прежнем статусе, но вторую заявку на неё не принять). Руководство →
    одобрено сразу, ответ как раньше (`deal_id`, `status`, `payments`). Правила
    одни для бота и WebApp — в `services.machine_deal_requests`.

    Ключ идемпотентности обязателен: сделка — денежный факт, а двойной тап по
    «Оформить» на телефоне обычное дело. Повтор отдаёт тот же ответ.
    """
    from services import async_db as adb
    from services import machine_deal_requests as mdr

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_deal"
    )
    role = get_role(user["id"])
    machine_id = _machine_id_arg(data, "machine_id")
    kind = (data.get("kind") or "").strip()
    if kind not in mdr.KINDS:
        raise HTTPException(status_code=400, detail=f"Тип сделки: {' / '.join(mdr.KINDS)}")
    price_cents = _machine_money(data.get("price"), "Цена")
    if kind != "reserve" and not price_cents:
        raise HTTPException(status_code=400, detail="Цена сделки обязательна")
    buyer_name = (data.get("buyer_name") or "").strip()[:200]
    if not buyer_name:
        raise HTTPException(status_code=400, detail="Покупатель обязателен")
    if not data.get("idempotency_key"):
        raise HTTPException(status_code=400, detail="idempotency_key обязателен")
    down_payment_cents, months = _machine_installment_args(data, kind)

    idem = _Idem(adb, "machine_deal", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await mdr.submit(
            machine_id,
            kind=kind,
            actor_id=user["id"],
            actor_name=_actor_name(user),
            actor_role=role,
            price_cents=price_cents,
            currency=(data.get("currency") or "USD").strip().upper()[:8],
            buyer_name=buyer_name,
            buyer_phone=_machine_text(data, "buyer_phone", 40),
            buyer_passport=_machine_text(data, "buyer_passport", 100),
            buyer_note=_machine_text(data, "buyer_note", 1000),
            agent_ms_id=_machine_text(data, "agent_ms_id", 64),
            down_payment_cents=down_payment_cents,
            months=months,
        )
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    await idem.store(res)
    return JSONResponse(res)


def _machine_installment_args(data: dict, kind: str) -> tuple[int, int]:
    """Взнос (ноль законен) и срок рассрочки из формы. Дату последнего платежа
    считает сервис по графику — введённая руками, она разошлась бы с ним."""
    down_payment_cents = _machine_money(
        data.get("down_payment"), "Первоначальный взнос", allow_zero=True
    ) or 0
    months = 0
    if kind == "credit":
        try:
            months = int(data.get("months") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Срок рассрочки — целое число месяцев")
    return down_payment_cents, months


# ─── Заявки на сделки по технике: решения руководителя ───────────────────────
# Поток и правила — `services/machine_deal_requests.py`. Ручки открыты
# менеджеру тоже: решать он может, только пока руководителя в системе нет
# (как с подтверждением денег, `_money_confirmers`), — это решает сервис.


def _machine_request_response(res: dict) -> JSONResponse:
    if not res.get("ok") and res.get("forbidden"):
        return JSONResponse({**res, "detail": res.get("error", "")}, status_code=403)
    return _machine_response(res)


@app.post("/api/machines/deals/pending")
async def api_machines_deals_pending(request: Request):
    """Заявки на одобрении (+ свои на доработке у автора). Паспорт — только
    руководству. `can_decide`/`decide_hint` — рисовать ли кнопки решения."""
    from services import machine_deal_requests as mdr

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_deals_pending",
        rate_limit_max=60,
    )
    role = get_role(user["id"])
    rights = await mdr.decision_rights(user["id"], role)
    pending = await mdr.list_requests(statuses=("pending",))
    rework = await mdr.list_requests(statuses=("rework",), created_by=user["id"])
    return JSONResponse({
        "ok": True,
        "requests": [mdr.visible_request(r, role) for r in pending],
        "my_rework": [mdr.visible_request(r, role) for r in rework],
        "can_decide": rights["can_decide"],
        "decide_hint": rights["hint"],
        "approvers_exist": rights["exist"],
        "viewer_id": user["id"],
    })


async def _machine_request_action(data: dict, user: dict, scope: str, op) -> JSONResponse:
    """Общий каркас решений: id заявки, ключ идемпотентности, коды ответа.
    Авторизация — в самой ручке (её `allowed_roles` читают net.test.js и
    gen_role_matrix)."""
    from services import async_db as adb

    request_id = _machine_id_arg(data, "request_id")
    idem = _Idem(adb, scope, user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        async with idem.released_on_reject():
            res = await op(data, user, get_role(user["id"]), request_id)
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_request_response(res)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/machines/deals/approve")
async def api_machines_deals_approve(request: Request):
    """Одобрить: машина → бронь/продана/в рассрочку, сделка и график — сразу."""
    from services import machine_deal_requests as mdr

    async def op(data, user, role, request_id):
        return await mdr.approve(
            request_id, actor_id=user["id"], actor_name=_actor_name(user), actor_role=role
        )

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_deals_approve"
    )
    return await _machine_request_action(data, user, "api_machines_deals_approve", op)


@app.post("/api/machines/deals/rework")
async def api_machines_deals_rework(request: Request):
    """На доработку с причиной: менеджер правит условия и отправляет снова."""
    from services import machine_deal_requests as mdr

    async def op(data, user, role, request_id):
        return await mdr.return_for_rework(
            request_id, actor_id=user["id"], actor_name=_actor_name(user), actor_role=role,
            reason=str(data.get("reason") or ""),
        )

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_deals_rework"
    )
    return await _machine_request_action(data, user, "api_machines_deals_rework", op)


@app.post("/api/machines/deals/reject")
async def api_machines_deals_reject(request: Request):
    """Отклонить (причина необязательна). Машина остаётся в прежнем статусе."""
    from services import machine_deal_requests as mdr

    async def op(data, user, role, request_id):
        return await mdr.reject(
            request_id, actor_id=user["id"], actor_name=_actor_name(user), actor_role=role,
            reason=_machine_text(data, "reason", 500),
        )

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_deals_reject"
    )
    return await _machine_request_action(data, user, "api_machines_deals_reject", op)


@app.post("/api/machines/deals/cancel")
async def api_machines_deals_cancel(request: Request):
    """Отозвать свою заявку (клиент передумал)."""
    from services import machine_deal_requests as mdr

    async def op(data, user, role, request_id):
        return await mdr.cancel(
            request_id, actor_id=user["id"], actor_name=_actor_name(user), actor_role=role
        )

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_deals_cancel"
    )
    return await _machine_request_action(data, user, "api_machines_deals_cancel", op)


@app.post("/api/machines/deals/resubmit")
async def api_machines_deals_resubmit(request: Request):
    """Отправить доработанную заявку снова. Поля — как у `/api/machines/deal`;
    пустое поле оставляет прежнее значение (паспорт менеджер не видит)."""
    from services import machine_deal_requests as mdr

    async def op(data, user, role, request_id):
        req = await mdr.get_request(request_id)
        kind = (req or {}).get("kind") or ""
        down, months = _machine_installment_args(data, kind)
        return await mdr.resubmit(
            request_id, actor_id=user["id"], actor_name=_actor_name(user), actor_role=role,
            price_cents=_machine_money(data.get("price"), "Цена"),
            currency=_machine_text(data, "currency", 8),
            buyer_name=_machine_text(data, "buyer_name", 200),
            buyer_phone=_machine_text(data, "buyer_phone", 40),
            buyer_passport=_machine_text(data, "buyer_passport", 100),
            buyer_note=_machine_text(data, "buyer_note", 1000),
            down_payment_cents=down if data.get("down_payment") not in (None, "") else None,
            months=months if kind == "credit" and data.get("months") not in (None, "") else None,
        )

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_deals_resubmit"
    )
    return await _machine_request_action(data, user, "api_machines_deals_resubmit", op)


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


@app.post("/api/machines/payment")
async def api_machines_payment(request: Request):
    """Отметить платёж графика рассрочки полученным (или снять отметку).

    Когда получен последний платёж, сервис закрывает сделку и переводит машину
    в «Продана» сам: закрывать руками после последнего платежа значит однажды
    забыть это сделать.
    """
    from services import async_db as adb
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_payment"
    )
    payment_id = _machine_id_arg(data, "payment_id")
    paid = data.get("paid", True)
    method = _machine_receipt_method(data, required=False)
    # Снять отметку = удалить поступление: как удаление денег — руководству,
    # менеджеру только пока руководителя в системе нет.
    undo_mode = None if paid else await _machine_money_undo_mode(user["id"])
    # Двойной тап «оплачен» с тем же ключом отдаёт результат первого, а не
    # второе поступление. Сервис дополнительно сериализует записи по сделке.
    idem = _Idem(adb, "machine_payment", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await machines.pay_installment(
            payment_id, user_id=user["id"], full_name=_actor_name(user), paid=bool(paid),
            method=method,
        )
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    if undo_mode == "no_boss":
        await _audit_no_boss_undo(user, f"снята отметка платежа графика #{payment_id}")
    await idem.store(res)
    return _machine_response(res)


def _machine_receipt_method(data: dict, *, required: bool) -> str | None:
    """Способ получения денег по рассрочке — как у разбивки оплаты заказа
    (наличные / карта / перечисление). Форма «Внести оплату» его требует."""
    from services import machines

    raw = (data.get("method") or "").strip()
    if not raw:
        if required:
            raise HTTPException(status_code=400, detail="Укажите способ: наличные, карта или перечисление")
        return None
    if raw not in machines.RECEIPT_METHODS:
        raise HTTPException(status_code=400, detail="Способ оплаты: наличные, карта или перечисление")
    return raw


async def _machine_money_undo_mode(user_id: int) -> str:
    """Удалить поступление по рассрочке (или снять отметку «оплачен»).

    Вносит деньги менеджер — его работа (решение владельца), а стирает их
    руководство; менеджер — только пока руководителя в системе нет, с пометкой
    в аудите (как подтверждение денег, `_money_confirmers`). → 'boss' | 'no_boss'.
    """
    from services import machine_deal_requests as mdr

    rights = await mdr.decision_rights(user_id, get_role(user_id))
    if not rights["can_decide"]:
        raise HTTPException(status_code=403, detail="Удалить поступление может руководитель")
    return "boss" if rights["viewer_is_holder"] else "no_boss"


async def _audit_no_boss_undo(user: dict, what: str) -> None:
    from services import async_db as adb

    await adb.add_audit_log(
        user["id"], _actor_name(user), get_role(user["id"]), "machine_receipt_deleted",
        f"{what} · менеджером — руководителя в системе нет",
    )


@app.post("/api/machines/receipt")
async def api_machines_receipt(request: Request):
    """Записать полученные по рассрочке деньги — сумма любая.

    Клиент платит не «платёж №3», а деньги: в один месяц больше, в другой
    меньше. Поступления гасят график по порядку, переплата уходит в следующие
    месяцы, а последний закрытый платёж закрывает сделку.
    """
    from services import async_db as adb
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_receipt"
    )
    deal_id = _machine_id_arg(data, "deal_id")
    amount_cents = _machine_money(data.get("amount"), "Сумма")
    if not amount_cents:
        raise HTTPException(status_code=400, detail="Сумма обязательна")
    method = _machine_receipt_method(data, required=True)
    if not data.get("idempotency_key"):
        raise HTTPException(status_code=400, detail="idempotency_key обязателен")

    idem = _Idem(adb, "machine_receipt", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await machines.add_receipt(
            deal_id, amount_cents, user_id=user["id"], full_name=_actor_name(user),
            note=_machine_text(data, "note", 200), method=method,
        )
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/machines/receipt_delete")
async def api_machines_receipt_delete(request: Request):
    """Удалить ошибочно внесённое поступление.

    Если им была закрыта рассрочка — она открывается обратно: иначе долг
    исчезает из напоминаний и дебиторки, хотя платёж не получен.
    """
    from services import machines

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_ROLES, rate_limit_scope="api_machines_receipt_delete"
    )
    mode = await _machine_money_undo_mode(user["id"])
    receipt_id = _machine_id_arg(data, "receipt_id")
    res = await machines.delete_receipt(
        receipt_id, user_id=user["id"], full_name=_actor_name(user)
    )
    if res.get("ok") and mode == "no_boss":
        await _audit_no_boss_undo(user, f"поступление #{receipt_id}")
    return _machine_response(res)


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


def _chat_id_env(*names: str) -> int | None:
    """Первый заданный id канала из перечисленных переменных."""
    for name in names:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            logger.error("%s должен быть числом, получено: %r", name, raw)
    return None


def _machine_photos_chat_id() -> int | None:
    """Приватный канал-хранилище для загруженных из WebApp фотографий.

    Прецедент — `BACKUP_TG_CHAT_ID`. Без переменной загрузка выключена: фото
    по-прежнему можно прислать боту, поэтому это деградация функции, а не
    поломка раздела.

    Имя обобщено до `PHOTOS_TG_CHAT_ID`: хранилище одно на технику и на товары,
    а заводить под каждый раздел свой канал незачем. Старое имя продолжает
    работать — переименовывать переменную на проде ради красоты не нужно.
    """
    return _chat_id_env("PHOTOS_TG_CHAT_ID", "MACHINE_PHOTOS_TG_CHAT_ID")


def _channel_id() -> int | None:
    """Публичный канал компании. Без него публикация выключена."""
    return _chat_id_env("CHANNEL_ID")


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
        # Пачкой грузят по одному запросу на снимок: экскаватор снимают с
        # десятка ракурсов, и это одно действие, а не подозрительная активность.
        rate_limit_max=60,
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


# ─── API: контейнеры ─────────────────────────────────────────────────────────
# Что едет, что уже здесь и сошёлся ли состав. Роли те же, что у техники:
# заводит и принимает менеджер, удаляет руководство.

_CONTAINER_ROLES = ("admin", "boss", "manager")


@app.post("/api/containers/list")
async def api_containers_list(request: Request):
    """Список контейнеров + счётчики. Payload: {"status": "in_transit"?}."""
    from services import containers

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_list"
    )
    status = (data.get("status") or "").strip() or None
    if status and status not in containers.STATUSES:
        raise HTTPException(status_code=400, detail=f"Неизвестный статус: {status}")
    search = (data.get("search") or "").strip()[:64] or None

    # Сводку и окно правки считает сервис одним проходом — раньше здесь был
    # запрос состава на КАЖДЫЙ контейнер, то есть N+1 на список.
    rows = await containers.list_containers(status, search=search)
    for row in rows:
        row["diff"] = row.pop("summary", None)
    return JSONResponse(
        {
            "ok": True,
            "containers": rows,
            "counts": await containers.count_by_status(),
            "status": status or "all",
            "can_manage": get_role(user["id"]) in _MACHINE_BOSS,
            "status_labels": containers.STATUS_LABELS,
        }
    )


@app.post("/api/containers/card")
async def api_containers_card(request: Request):
    """Карточка контейнера: состав с расхождениями «заявлено → прибыло»."""
    from services import containers

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_card"
    )
    container_id = _machine_id_arg(data, "container_id")
    container = await containers.get_container(container_id)
    if not container:
        raise HTTPException(status_code=404, detail="Контейнер не найден")

    from services import container_receipt

    items = containers.diff(await containers.list_items(container_id))
    # Позиции, заведённые свободным текстом: если в каталоге есть карточка с тем
    # же названием, показываем её сразу — приход по имени уйдёт именно туда, и
    # человек должен видеть это до оприходования, а не после.
    matches = await container_receipt.catalog_matches(items)
    for item in items:
        item["catalog_matches"] = matches.get(int(item["id"]), [])
    return JSONResponse(
        {
            "ok": True,
            "container": container,
            "items": items,
            "diff": containers.diff_summary(items),
            # Окно правки: фронт по нему решает, показывать ли кнопки, а не
            # выясняет это отказом ручки после нажатия.
            "edit_window": containers.edit_window(container),
            "receipt": await container_receipt.get_link(container_id),
            "can_manage": get_role(user["id"]) in _MACHINE_BOSS,
            "status_labels": containers.STATUS_LABELS,
        }
    )


@app.post("/api/containers/create")
async def api_containers_create(request: Request):
    from services import async_db as adb
    from services import containers

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_create",
        rate_limit_max=20,
    )
    idem = _Idem(adb, "container_create", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        res = await containers.create_container(
            number=(data.get("number") or "").strip()[:32],
            created_by=user["id"],
            creator_name=_actor_name(user),
            eta_date=_machine_text(data, "eta_date", 20),
            notes=_machine_text(data, "notes", 1000),
        )
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/containers/update")
async def api_containers_update(request: Request):
    from services import containers

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_update"
    )
    container_id = _machine_id_arg(data, "container_id")
    raw = data.get("fields")
    if not isinstance(raw, dict) or not raw:
        raise HTTPException(status_code=400, detail="Нечего менять")
    fields = {
        k: (str(v).strip()[:1000] or None) if v is not None else None for k, v in raw.items()
    }
    res = await containers.update_container(
        container_id, user_id=user["id"], full_name=_actor_name(user), **fields
    )
    return _machine_response(res)


@app.post("/api/containers/item_add")
async def api_containers_item_add(request: Request):
    """Добавить позицию в состав.

    `arrived_qty` задают, когда позицию нашли в прибывшем контейнере, а в
    заявленном составе её не было — то есть для излишка.
    """
    from services import containers

    data = await request.json()
    _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_item_add",
        rate_limit_max=120,  # состав заполняют подряд, позиция за позицией
    )
    container_id = _machine_id_arg(data, "container_id")
    name = (data.get("name") or "").strip()[:200]
    product_id = _optional_id(data, "product_id")

    def _num(key):
        value = data.get(key)
        if value is None or str(value).strip() == "":
            return None
        try:
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key}: не число")

    if product_id is None and name:
        # «Новый товар» с именем, которое в каталоге уже есть (регистр, пробелы
        # и ё не в счёт), — это не новый товар, а мимо нажатый поиск. Молча
        # принять значит завести при приёмке вторую карточку и развести остаток
        # по двум. Отказ несёт найденные карточки: форма предлагает выбрать.
        from services import container_receipt

        same = await container_receipt.same_name_products(name)
        if same:
            return JSONResponse(
                {
                    "ok": False,
                    "needs_choice": True,
                    "existing": [
                        {"product_id": int(p["id"]), "name": p["name"], "unit": p.get("unit")}
                        for p in same[:5]
                    ],
                    "detail": f"В каталоге уже есть «{same[0]['name']}» — выберите его",
                },
                status_code=409,
            )

    res = await containers.add_item(
        container_id,
        name=name,
        expected_qty=_num("expected_qty") or 0,
        arrived_qty=_num("arrived_qty"),
        unit=(data.get("unit") or "шт").strip()[:16],
        note=_machine_text(data, "note", 500),
        # Товар выбран из каталога — приёмка попадёт ровно на эту карточку,
        # без угадывания по названию.
        product_id=product_id,
    )
    return _machine_response(res)


@app.post("/api/containers/item_link")
async def api_containers_item_link(request: Request):
    """Привязать позицию состава к карточке номенклатуры.

    Нужна и постфактум: состав часто заводят до того, как товар появился в
    каталоге, а несопоставленная позиция — это остаток, которого нет.
    """
    from services import containers

    data = await request.json()
    _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_item_link",
        rate_limit_max=120,
    )
    container_id = _machine_id_arg(data, "container_id")
    item_id = _machine_id_arg(data, "item_id")
    res = await containers.link_item(
        container_id, item_id, product_id=_optional_id(data, "product_id") or 0
    )
    return _machine_response(res)


@app.post("/api/containers/item_create_product")
async def api_containers_item_create_product(request: Request):
    """Завести карточку товара в номенклатуре по названию позиции и привязать её.

    Заводит ЧЕЛОВЕК кнопкой: автосоздание из приёмки превратило бы каждую
    опечатку в новую позицию справочника.
    """
    from services import container_receipt, containers

    data = await request.json()
    _authorize(
        data, allowed_roles=_CONTAINER_ROLES,
        rate_limit_scope="api_containers_item_create_product", rate_limit_max=30,
    )
    container_id = _machine_id_arg(data, "container_id")
    item_id = _machine_id_arg(data, "item_id")
    # Позицию ищем ВНУТРИ заявленного контейнера — гейт от подстановки чужого id,
    # и заодно название берём наше, а не присланное клиентом.
    item = next(
        (i for i in await containers.list_items(container_id) if int(i["id"]) == item_id), None
    )
    if not item:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    created = await container_receipt.create_product(
        str(item["name"]), unit=str(item.get("unit") or "шт")
    )
    if not created.get("ok"):
        return _machine_response(created)
    res = await containers.link_item(
        container_id, item_id, product_id=int(created["product_id"])
    )
    if not res.get("ok"):
        return _machine_response(res)
    return JSONResponse({**res, "name": created.get("name"), "existed": created.get("existed")})


@app.post("/api/containers/item_delete")
async def api_containers_item_delete(request: Request):
    from services import containers

    data = await request.json()
    _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_item_delete"
    )
    container_id = _machine_id_arg(data, "container_id")
    item_id = _machine_id_arg(data, "item_id")
    # Позицию ищем ВНУТРИ заявленного контейнера — гейт от подстановки чужого id.
    res = await containers.delete_item(container_id, item_id)
    return _machine_response(res)


@app.post("/api/containers/check")
async def api_containers_check(request: Request):
    """Проставить фактические количества по позициям приёмки.

    Payload: {"container_id": N, "quantities": {"<item_id>": 18, ...}}.
    Пустое значение сбрасывает факт в «ещё не считали»: приёмщик должен иметь
    возможность отменить свою же опечатку, а не только записать ноль.
    """
    from services import containers

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_check"
    )
    container_id = _machine_id_arg(data, "container_id")
    raw = data.get("quantities")
    if not isinstance(raw, dict) or not raw:
        raise HTTPException(status_code=400, detail="Нечего сохранять")
    quantities: dict[int, object] = {}
    for key, value in raw.items():
        try:
            quantities[int(key)] = value
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="item_id: не число")
    from services import container_receipt

    resolutions = _container_resolutions(data)
    # Выбор товара по непривязанным позициям — ДО сохранения количеств и прихода:
    # отказ выбора (чужая позиция, нет товара) не должен оставлять сохранённый
    # факт без прихода, а без привязки накладная ушла бы без этих позиций.
    resolved = await container_receipt.resolve_items(container_id, resolutions)
    if not resolved.get("ok"):
        return _machine_response(resolved)

    # Что было до сохранения: откатить факт, если приход за ним не поехал.
    before = {int(i["id"]): i.get("arrived_qty") for i in await containers.list_items(container_id)}
    had_invoice = bool((await container_receipt.get_link(container_id)).get("invoice_id"))

    res = await containers.set_arrived_quantities(
        container_id, quantities, user_id=user["id"], full_name=_actor_name(user), audit=False
    )
    if not res.get("ok"):
        return _machine_response(res)
    res["resolved"] = resolved

    # Остаток пополняем сразу после сохранения — ради этого приёмку и считают.
    receipt = await container_receipt.receive(container_id, user_id=user["id"])
    res["receipt"] = receipt
    if not receipt.get("ok") and (had_invoice or receipt.get("code")):
        # Переоприходование не прошло (чаще всего товар прежнего прихода уже
        # отгружен, и отмена накладной увела бы остаток в минус). Раньше ручка
        # отвечала «ок»: карточка показывала новый факт, остаток стоял по
        # старому, а причина лежала в поле `receipt`, которое экран не читал.
        # Факт и остаток обязаны сходиться — возвращаем прежние количества и
        # отказываем целиком.
        await containers.set_arrived_quantities(
            container_id,
            {item_id: before.get(item_id) for item_id in quantities},
            user_id=user["id"], full_name=_actor_name(user), audit=False,
        )
        reason = str(receipt.get("error") or "приход не проведён")
        if receipt.get("code") == "insufficient_stock":
            reason = ("товар из прежнего прихода уже отгружен, и отмена прихода увела бы "
                      f"остаток в минус ({reason})")
        detail = f"Сверка не сохранена: {reason}. Количества оставлены прежними."
        return JSONResponse(
            {"ok": False, "reverted": True, "receipt": receipt, "detail": detail},
            status_code=409,
        )
    # Первый приход, которому нечего проводить (всё пусто, позиции без карточки
    # по старому API), — не откат: остаток и не должен был двигаться. Причина —
    # в `receipt`, экран показывает её отдельным сообщением.
    await containers.audit_checked(
        container_id, len(quantities), user_id=user["id"], full_name=_actor_name(user)
    )
    return JSONResponse(res)


def _container_resolutions(data: dict) -> dict[int, dict]:
    """`resolve` из тела запроса приёмки: выбор товара по непривязанным позициям."""
    from services import container_receipt

    parsed = container_receipt.parse_resolutions(data.get("resolve"))
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail="resolve: {item_id: {product_id: N} | {new: true}}",
        )
    return parsed


@app.post("/api/containers/supplier")
async def api_containers_supplier(request: Request):
    """Задать поставщика контейнера.

    Приёмку он не держит — локальной приходной накладной контрагент не
    обязателен. Но «от кого пришло» потом некому восстановить, поэтому поле
    спрашиваем заранее, а не в момент приёмки: когда считают коробки, о
    поставщике не думают.
    """
    from services import container_receipt

    data = await request.json()
    _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_supplier"
    )
    container_id = _machine_id_arg(data, "container_id")
    supplier_id = _optional_id(data, "supplier_id")
    name = (data.get("supplier_name") or "").strip()[:200] or None
    if supplier_id is None:
        raise HTTPException(status_code=400, detail="Выберите поставщика из справочника")
    res = await container_receipt.set_supplier(container_id, supplier_id=supplier_id, name=name)
    return _machine_response(res)


@app.post("/api/containers/supply")
async def api_containers_supply(request: Request):
    """Оприходовать контейнер вручную — повтор после сбоя или после того, как
    недостающий товар завели в номенклатуре.

    Повторный вызов ПЕРЕОПРИХОДУЕТ: прежний приход отменяется, новый создаётся
    с актуальными количествами.
    """
    from services import async_db as adb
    from services import container_receipt

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_supply",
        rate_limit_max=20,
    )
    container_id = _machine_id_arg(data, "container_id")
    resolutions = _container_resolutions(data)
    # Двойной тап с одним ключом отдаёт итог первой приёмки, а не переоприходует
    # второй раз (лишняя отменённая накладная в истории). Параллельные приёмки
    # без ключа сериализует сам сервис.
    idem = _Idem(adb, "container_supply", user["id"], data.get("idempotency_key"))
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    try:
        # Сначала выбор товара по непривязанным позициям (форма оприходования
        # показала их человеку), потом приход — иначе они в накладную не попадут.
        resolved = await container_receipt.resolve_items(container_id, resolutions)
        if not resolved.get("ok"):
            await idem.release()
            return _machine_response(resolved)
        res = await container_receipt.receive(container_id, user_id=user["id"])
        if resolutions:
            res["resolved"] = resolved
    except Exception:
        await idem.release()
        raise
    if not res.get("ok"):
        await idem.release()
        return _machine_response(res)
    await idem.store(res)
    return _machine_response(res)


@app.post("/api/containers/arrive")
async def api_containers_arrive(request: Request):
    """Отметить контейнер прибывшим."""
    from services import containers

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_CONTAINER_ROLES, rate_limit_scope="api_containers_arrive"
    )
    container_id = _machine_id_arg(data, "container_id")
    res = await containers.mark_arrived(
        container_id, user_id=user["id"], full_name=_actor_name(user)
    )
    return _machine_response(res)


@app.post("/api/containers/delete")
async def api_containers_delete(request: Request):
    """Удалить контейнер. Только admin/boss и пока открыто окно правки."""
    from services import containers

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_MACHINE_BOSS, rate_limit_scope="api_containers_delete"
    )
    container_id = _machine_id_arg(data, "container_id")
    res = await containers.delete_container(
        container_id, user_id=user["id"], full_name=_actor_name(user)
    )
    return _machine_response(res)


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
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "bookkeeper"),
        rate_limit_scope="api_deposits_pending",
    )
    deposits = await adb.get_pending_cash_deposits()
    await _decorate_deposits(deposits, user["id"])
    confirmers = await _money_confirmers(user["id"])
    return JSONResponse({"ok": True, "deposits": deposits, "confirmers_exist": confirmers["exist"]})


async def _money_confirmers(viewer_id: int) -> dict:
    """Кто в системе подтверждает деньги (руководитель/бухгалтер/админ).

    Менеджер проходит ручки подтверждения совмещением ролей (он временно
    бухгалтер), но экран обязан сказать, КОГДА это законно: пока ни одного
    активного руководителя/бухгалтера нет. Иначе он молча подтверждал бы
    собственные деньги в обход живого руководителя.
    """
    from services import async_db as adb
    from services import order_payments

    users = await adb.get_all_users()
    holders = [
        u for u in users
        if not u.get("deactivated_at") and u.get("role") in order_payments.ROLES_CONFIRM
    ]
    role = get_role(viewer_id)
    is_holder = role in order_payments.ROLES_CONFIRM
    exist = bool(holders)
    return {
        "exist": exist,
        "names": [u.get("full_name") or str(u["user_id"]) for u in holders][:3],
        # Кнопку подтверждения рисуем тому, кто её законно жмёт: носителю роли
        # или менеджеру, когда носителей нет вовсе.
        "can_confirm": is_holder or (role == "manager" and not exist),
        "viewer_is_holder": is_holder,
    }


async def _decorate_deposits(deposits: list[dict], viewer_id: int) -> None:
    """Валюта сдачи, что она закрывает («Заказы: #27 …») и чья она — батчем."""
    from services import async_db as adb
    from services import order_payments

    ids = [int(d["id"]) for d in deposits]
    if not ids:
        return
    orders_by_deposit = await order_payments.deposit_orders_view(ids)
    currencies = await order_payments.deposit_currency(ids)
    users = await adb.get_all_users()
    names = {int(u["user_id"]): u.get("full_name") or str(u["user_id"]) for u in users}
    for d in deposits:
        did = int(d["id"])
        d["orders"] = orders_by_deposit.get(did, [])
        d["currency"] = currencies.get(did)
        allocated = sum(o["amount_cents"] for o in d["orders"] if o["currency"] == d["currency"])
        d["unallocated"] = float(money.from_cents(max(0, int(d.get("amount_cents") or 0) - allocated)))
        d["manager_name"] = names.get(int(d["manager_id"]), str(d["manager_id"]))
        d["is_own"] = int(d["manager_id"]) == int(viewer_id)
        d["confirmed_by_name"] = names.get(int(d["confirmed_by"])) if d.get("confirmed_by") else None
        d["self_confirmed"] = bool(d.get("confirmed_by")) and int(d["confirmed_by"]) == int(d["manager_id"])


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
    resp = {
        "ok": True, "deposit_id": deposit_id, "closed_orders": res.get("closed_orders", []),
        "self_confirmed": bool(res.get("self_confirmed")),
    }
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
    # Сдача — в базовой валюте; потолок тот же, что у платежей
    # (database.validate_amount_in_currency), а не своя константа.
    from services.database import validate_amount_in_currency

    from config import ALLOWED_CURRENCIES, BASE_CURRENCY

    currency = str(data.get("currency") or BASE_CURRENCY or "USD").upper()
    if currency not in {c.upper() for c in ALLOWED_CURRENCIES}:
        raise HTTPException(status_code=400, detail=f"Валюта {currency} не поддерживается")
    raw_ids = data.get("order_ids")
    order_ids: list[int] | None = None
    if raw_ids:
        try:
            order_ids = [int(x) for x in raw_ids][:200]
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="order_ids — список номеров заказов")
    raw_amount = data.get("amount")
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        amount = float("nan")
    ok, err = validate_amount_in_currency(amount, currency)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=err if err and "лимит" in err else "Сумма должна быть положительным числом",
        )

    # R2: DB-уровневая идемпотентность. create_cash_deposit не защищён claim'ом —
    # двойной POST (ретрай клиента после рестарта webapp/мультиворкер) создаёт две
    # сдачи. idem_claim атомарно столбит ключ в общей БД (in-mem кэш не переживал
    # рестарт). Если ключ уже был — отдаём сохранённый результат, не повторяем.
    # Ключ занят, но результата нет (операция в полёте) — 409: безопаснее
    # отказать, чем рискнуть дублем. Результат пишет сама сдача в своей
    # транзакции (atomic), поэтому брошенный ключ переиспользуется.
    idem = _Idem(adb, "deposit_create", user["id"], data.get("idempotency_key"), atomic=True)
    prev = await idem.claim()
    if prev is not None:
        return JSONResponse(prev)
    try:
        res = await adb.create_cash_deposit(
            user["id"], amount, idem_key=idem.key, currency=currency, order_ids=order_ids
        )
    except Exception:
        await idem.release()  # упало до коммита — освободить ретраю
        raise
    if not res.get("ok"):
        await idem.release()
        raise HTTPException(status_code=400, detail=res.get("error", "не удалось создать сдачу"))

    name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get(
        "username", str(user["id"])
    )
    # Переиспользуем то же уведомление с клавиатурой, что и бот-команда /deposit.
    from handlers.deposits import _notify_confirmers

    bot = await get_notify_bot()
    try:
        await _notify_confirmers(bot, res["deposit_id"], name, amount, currency=currency)
    except Exception:
        logger.warning("deposit create notify failed", exc_info=True)
    from services import order_payments

    view = (await order_payments.deposit_orders_view([res["deposit_id"]])).get(res["deposit_id"], [])
    resp = {
        "ok": True, "deposit_id": res["deposit_id"], "currency": currency, "orders": view,
        "unallocated": float(money.from_cents(int(res.get("unallocated_cents") or 0))),
    }
    await idem.store(resp)
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
    await _decorate_deposits(deposits, user["id"])
    return JSONResponse({"ok": True, "deposits": deposits})


@app.post("/api/deposits/on_hand")
async def api_deposits_on_hand(request: Request):
    """Наличные, которые менеджер получил по заказам и ещё не сдал в кассу, —
    для формы «Сдать наличные»: сколько и по каким заказам, по валютам."""
    from services import order_payments

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_deposits_on_hand",
        rate_limit_max=60,
    )
    rows = await order_payments.cash_on_hand(user["id"])
    summary = order_payments.cash_on_hand_summary(rows)
    for item in summary["by_currency"] + summary["orders"]:
        item["amount"] = float(money.from_cents(item["amount_cents"]))
    from config import ALLOWED_CURRENCIES, BASE_CURRENCY

    return JSONResponse({
        "ok": True, **summary,
        "currencies": [c.upper() for c in ALLOWED_CURRENCIES],
        "base_currency": (BASE_CURRENCY or "USD").upper(),
    })


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

    resp = {
        "ok": True,
        "return_id": return_id,
        "order_status": res.get("order_status"),
        # Приход товара по возврату: номер накладной или причина, почему склад
        # не двигали, — чтобы расхождение было видно в ответе, а не в логах.
        "invoice_number": res.get("invoice_number"),
        "stock_skipped": res.get("stock_skipped"),
    }
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
    # двойной refund/занижение долга). Ключ столбится в общей БД, результат
    # пишет сам возврат в своей транзакции (atomic). Любой отказ проверок ниже
    # освобождает ключ (released_on_reject) — раньше 404/403/409 оставляли его
    # занятым на сутки.
    idem = _Idem(adb, "return_create", user["id"], data.get("idempotency_key"), atomic=True)
    prev = await idem.claim()
    if prev is not None:
        return JSONResponse(prev)

    async with idem.released_on_reject():
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
        # Сознательно `in`, а не role_allowed: совмещение ролей (менеджер замещает
        # кладовщика) НЕ снимает H2 — возврат чужого заказа двигает чужой долг, и
        # принимать товар за кладовщика для этого не нужно.
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
                raise HTTPException(status_code=400, detail="Выберите хотя бы одну позицию")
            ret_items = []
            for row in raw_items:
                try:
                    iid = int(str((row or {}).get("item_id")))
                    qty = float(str((row or {}).get("quantity")))
                except (TypeError, ValueError, AttributeError):
                    raise HTTPException(status_code=400, detail="Позиция: нужны item_id и quantity")
                if iid not in returnable:
                    raise HTTPException(
                        status_code=400, detail=f"Позиция {iid} недоступна к возврату"
                    )
                if any(seen == iid for seen, _, _ in ret_items):
                    # Две строки на одну позицию проходят «не больше доступного»
                    # каждая по отдельности; в базе это ещё и нарушение UNIQUE
                    # (return_id, order_item_id) — отвечаем текстом, а не 500-й.
                    raise HTTPException(status_code=400, detail=f"Позиция {iid} указана дважды")
                if not (math.isfinite(qty) and 0 < qty <= returnable[iid] + 1e-9):
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
            idem_key=idem.key,
        )
    except Exception:
        await idem.release()  # упало до коммита — освободить ретраю
        raise
    if not res.get("ok"):
        await idem.release()
        raise HTTPException(status_code=409, detail=res.get("error", "не удалось"))

    # То же уведомление с кнопками, что и бот-команда /return.
    from handlers.returns import _notify_confirmers

    bot = await get_notify_bot()
    try:
        await _notify_confirmers(bot, res["return_id"], order_id, res["total_amount"], refund)
    except Exception:
        logger.warning("return create notify failed", exc_info=True)
    resp = {"ok": True, "return_id": res["return_id"], "total_amount": res["total_amount"]}
    await idem.store(resp)
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
        if res.get("code") == "payment_required":
            # Отдельный код: фронт по нему открывает форму «как получены деньги»,
            # а не показывает голую ошибку.
            return JSONResponse(
                {"detail": res["error"], "code": res["code"], "gap_cents": res.get("gap_cents")},
                status_code=409,
            )
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

    # PR C: минимальная цена продажи, заданная руководством. По карточке
    # товара → product_prices.sale_price. Если задана:
    #   • price не передан/0 → префилл sale_price (дефолт)
    #   • price < sale_price → 400 (нельзя продать ниже минимума)
    product_ref = _product_ref(data)
    if product_ref:
        pp = await adb.get_product_price(product_ref)
        sale_min = pp.get("sale_price") if pp else None
        if sale_min is not None:
            if price <= 0:
                price = float(sale_min)  # префилл дефолтом
            elif price < float(sale_min):
                raise HTTPException(
                    status_code=400,
                    detail=f"Цена ниже минимальной ({sale_min:g})",
                )

    # Все позиции одного ордера — в одной валюте. Пустой заказ валюту берёт
    # из позиции (и может сменить, если позиции удалили). Заказ с позициями
    # другую валюту ОТВЕРГАЕТ: раньше сервер молча оставлял прежнюю, и цена,
    # введённая в сумах, ложилась в долларовый заказ как доллары.
    from config import ALLOWED_CURRENCIES

    requested_currency = (data.get("currency") or "").upper()
    if requested_currency and requested_currency in ALLOWED_CURRENCIES:
        current_currency = (order.get("currency") or "").upper()
        if current_currency != requested_currency:
            if current_currency and await adb.get_order_items(data["order_id"]):
                raise HTTPException(
                    status_code=409,
                    detail=f"Валюта заказа — {current_currency}: все позиции в одной валюте",
                )
            if not await adb.update_order_currency(
                data["order_id"], requested_currency, require_draft=True
            ):
                _require_draft_order(None)

    # Статус перепроверяется в транзакции записи (require_draft): проверка
    # выше — ранний понятный ответ, решает эта. Иначе сабмит между ними
    # получал позицию в уже отправленный заказ.
    item_id = await adb.add_order_item(
        order_id=data["order_id"],
        product_name=data["product_name"],
        product_href="",
        quantity=quantity,
        unit=_clean_unit(data.get("unit")),
        price=price,
        note=data.get("note", ""),
        product_id=int(product_ref) if product_ref.isdigit() else None,
        require_draft=True,
    )
    if item_id is None:
        _require_draft_order(None)
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
    # Статус — ещё раз в транзакции удаления (см. add_item).
    if not await adb.remove_order_item(data["item_id"], require_draft=True):
        # Позиция на месте — значит, заказ успели отправить; иначе её удалили.
        if await adb.get_order_item(data["item_id"]):
            _require_draft_order(None)
        raise HTTPException(status_code=404, detail="Позиция не найдена")
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
    if not await adb.update_order_agent(
        data["order_id"], agent_id, agent_name, require_draft=True
    ):
        _require_draft_order(None)
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

    # Уведомляем руководителей — заявка блокирует работу менеджера, поэтому
    # notify_policy.ORDER_REQUEST всегда «сразу» (см. services/notify_policy.py).
    from services.notify_policy import ORDER_REQUEST, should_notify_now
    from services.order_workflow import resubmit_diff_line
    from handlers.orders import build_credit_context

    if should_notify_now(ORDER_REQUEST):
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
    """Список клиентов (контрагентов) для подстановки в заказ.

    Справочник наш, поэтому ни снапшота, ни live-fallback'а больше нет.
    Санитизация строки поиска (SECURITY.md H11) осталась: она ограничивает
    длину и набор символов, то есть стоимость LIKE-запроса.
    """
    from services import counterparties as cp_service

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_agents",
        rate_limit_max=30,
        rate_limit_window=60.0,
    )

    raw_search = (data.get("search", "") or "").strip()[:50]
    # Whitelist: буквы (любые юникодные), цифры, пробелы, основные знаки
    import re as _re

    search = _re.sub(r"[^\w\s\-\.,'@+()/]", "", raw_search, flags=_re.UNICODE)
    rows = await cp_service.search(search or None, 50)
    return JSONResponse(
        {
            "agents": [
                {
                    "id": str(r["id"]),
                    "name": r.get("name", "—"),
                    "phone": r.get("phone") or "",
                    "type": r.get("type") or "customer",
                }
                for r in rows
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

    from services.database import debt_due_date

    # T2.1: остаток считает services.debts — тот же код, что в карточке заказа
    # и в утреннем напоминании о долгах. Батчем (пять запросов на любое число
    # заказов), поэтому N+1 не появляется. items тянем отдельно только ради
    # items_count в ответе.
    from services.debts import calc_order_balances

    debt_ids = [d["id"] for d in debts]
    items_by_order = await adb.get_order_items_by_ids(debt_ids) if debt_ids else {}
    balances = await calc_order_balances(debt_ids) if debt_ids else {}
    from services import order_payments
    from services.debts import calc_claimable_cents
    from services.roles import role_allowed

    parts_by_order = await order_payments.parts_for_orders(debt_ids) if debt_ids else {}
    claimable = await calc_claimable_cents(debt_ids) if debt_ids else {}
    # Кто подтверждает карту/перечисление: руководитель или бухгалтер. Менеджер
    # попадает сюда совмещением ролей (бухгалтера нет) — экран говорит об этом
    # прямо, а не молча даёт ему подтвердить собственные деньги.
    confirmers = await _money_confirmers(user_id)
    can_confirm = role_allowed(role, order_payments.ROLES_CONFIRM) and confirmers["can_confirm"]
    confirm_hint = (
        None if confirmers["viewer_is_holder"]
        else "подтверждаете вы — руководителя и бухгалтера в системе нет" if can_confirm
        else "подтвердит " + (", ".join(confirmers["names"]) or "руководитель или бухгалтер")
    )

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
        # У «оплаты сразу» своего срока нет — деньги причитались в день заказа
        # (database.debt_due_date, то же правило, что в фильтре «сейчас»).
        due = debt_due_date(o)
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
        parts = parts_by_order.get(o["id"], [])
        cash_pending_c = sum(
            p["order_amount_cents"] for p in parts if p["state"] in ("on_hand", "in_deposit")
        )
        # Ждёт = всё неподтверждённое; «после подтверждения» = остаток минус
        # ждущее. Наличные на руках подтверждаются сдачей, остальное — кнопкой.
        pending_c = bal.pending_cents
        remaining_after_c = max(0, bal.remaining_cents - pending_c)
        result.append(
            {
                "id": o["id"],
                "user_id": o["user_id"],
                "payment_type": o.get("payment_type") or "paid",
                "status": o.get("status"),
                "parts": parts,
                "pending_cash": float(money.from_cents(cash_pending_c)),
                "pending_confirmable": float(money.from_cents(max(0, pending_c - cash_pending_c))),
                "remaining_after_pending": float(money.from_cents(remaining_after_c)),
                "overpending": float(money.from_cents(max(0, pending_c - bal.remaining_cents))),
                "claimable": float(money.from_cents(claimable.get(o["id"], 0))),
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

    # Рассрочки по технике — второй поток тех же денег. Отдаём той же ручкой:
    # экран один, и второй запрос за тем же экраном не нужен. В кредитный лимит
    # контрагента они НЕ входят — покупатель техники это имя и паспорт, а не
    # контрагент МойСклад.
    machine_debts: list[dict] = []
    totals = None
    if is_boss:
        from services import receivables

        machine_debts = await receivables.machine_debt_rows(today)
        # Итог «нам должны: заказы / техника / всего» — по тем же строкам, что
        # уже посчитаны выше, без второго прохода по БД.
        order_items = [
            receivables.Receivable(
                "order", int(r["id"]), f"#{r['id']}", r["agent_name"], r["user_id"],
                r["due_date"], money.to_cents(r["remaining"]), r["currency"],
            )
            for r in result if r["remaining"] > 0
        ]
        totals = receivables.totals_by_source(
            order_items + await receivables.machine_receivables()
        )

    return JSONResponse(
        {
            "debts": result,
            "machine_debts": machine_debts,
            "totals": totals,
            "role": role,
            "can_confirm": can_confirm,
            "confirm_hint": confirm_hint,
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


async def _record_order_payment(data: dict, user: dict, op: str) -> JSONResponse:
    """Общее тело `/api/orders/payment` и `/api/orders/mark_paid`: разбивка
    «как получены деньги» (services.order_payments). Авторизация — в ручках."""
    from services import async_db as adb
    from services import order_payments

    try:
        order_id = int(data.get("order_id") or "")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")
    # Чужой заказ — 403 раньше разбора формы: посторонний не должен узнавать,
    # чего не хватает в запросе к заказу, который ему не принадлежит.
    head = await adb.get_order(order_id)
    if not head:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    from services.roles import role_allowed

    if head["user_id"] != user["id"] and not role_allowed(get_role(user["id"]), order_payments.ROLES_RECORD_ANY):
        raise HTTPException(status_code=403, detail="Нет доступа")
    if not data.get("parts"):
        # Сумма без способа больше не принимается: ради этого разбивка и
        # заведена («чтобы потом не возникало вопросов»).
        raise HTTPException(
            status_code=400,
            detail="Укажите, как получены деньги: наличные, карта или перечисление на счёт",
        )

    idem = _Idem(adb, op, user["id"], data.get("idempotency_key"), atomic=True)
    cached = await idem.claim()
    if cached is not None:
        return JSONResponse(cached)
    full_name = _actor_name(user) or user.get("username") or str(user["id"])
    actor = order_payments.Actor(
        user_id=int(user["id"]), name=full_name, role=get_role(user["id"]),
        username=f"@{user['username']}" if user.get("username") else "",
    )
    try:
        res = await order_payments.record_payment_parts(
            order_id, actor, data.get("parts"), idem_key=idem.key
        )
    except order_payments.PaymentError as e:
        await idem.release()
        return JSONResponse({"detail": e.message, "code": e.code}, status_code=e.status)
    except Exception:
        await idem.release()  # упало до коммита — ретрай должен быть возможен
        raise

    # Карта и перечисление — карточка подтверждающим с кнопками pay_ok/pay_no:
    # их сверяют с банком. Наличные подтверждаются сдачей — кнопок под ними нет.
    for part in res["parts"]:
        if part["method"] in order_payments.NONCASH_METHODS:
            await _notify_bosses_payment_pending(order_id, full_name, part["payment_id"])
    await idem.store(res)
    return JSONResponse(res)


@app.post("/api/orders/payment_context")
async def api_order_payment_context(request: Request):
    """Данные формы «Как получены деньги»: сколько внести, валюта заказа,
    тип оплаты, курсы ЦБ на сегодня и уже внесённые строки."""
    from services import async_db as adb
    from services import order_payments
    from services.accounting import cbu_quotes, fmt_rate, today_str
    from services.debts import calc_claimable_cents
    from services.roles import role_allowed

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_order_payment_context",
        rate_limit_max=60,
    )
    try:
        order_id = int(data.get("order_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="order_id обязателен")
    order = await adb.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    role = get_role(user["id"])
    if order["user_id"] != user["id"] and not role_allowed(role, order_payments.ROLES_RECORD_ANY):
        raise HTTPException(status_code=403, detail="Оплату по чужому заказу вносит руководитель")
    from config import ALLOWED_CURRENCIES, BASE_CURRENCY

    base = (BASE_CURRENCY or "USD").upper()
    ptype = order.get("payment_type") or "paid"
    currency = (order.get("currency") or base).upper()
    if ptype == "paid":
        due = (await order_payments.payment_gap_cents([order_id])).get(order_id, 0)
    else:
        due = (await calc_claimable_cents([order_id])).get(order_id, 0)
    from services.debts import calc_order_balance

    bal = await calc_order_balance(order_id)
    currencies = [c.upper() for c in ALLOWED_CURRENCIES]
    quotes = await cbu_quotes([c for c in currencies if c != base], today_str())
    parts = (await order_payments.parts_for_orders([order_id])).get(order_id, [])
    return JSONResponse({
        "order_id": order_id,
        "agent_name": order.get("agent_name") or "",
        "status": order.get("status"),
        "payment_type": ptype,
        "currency": currency,
        "total_cents": bal.total_cents,
        "due_cents": due,
        "exact": ptype == "paid",
        "base_currency": base,
        "currencies": currencies,
        "cbu": {c: fmt_rate(q) for c, q in quotes.items()},
        "methods": [[k, v] for k, v in order_payments.METHODS.items()],
        "parts": parts,
        "open": order.get("status") in ("approved", "shipped", "partially_returned")
        and not order.get("paid_confirmed_at"),
    })


@app.post("/api/orders/payment")
async def api_order_payment(request: Request):
    """Как клиент заплатил по заказу: строки {method, currency, amount, rate?}.

    «Оплата сразу» — сумма строк равна тому, что причитается (без неё заказ не
    отгрузить); «в долг» — любая часть остатка. Право: автор заказа или
    руководство. Идемпотентно по `idempotency_key` (атомарно с записью).
    """
    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_order_payment",
        rate_limit_max=20,
        rate_limit_window=60.0,
    )
    return await _record_order_payment(data, user, "order_payment")


@app.post("/api/orders/mark_paid")
async def api_mark_paid(request: Request):
    """Прежнее имя ручки «отметить оплату» — теперь та же разбивка, что
    `/api/orders/payment`. Сумма без способа отвергается (400)."""
    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_mark_paid",
        rate_limit_max=20,
        rate_limit_window=60.0,
    )
    return await _record_order_payment(data, user, "mark_paid")


async def _notify_bosses_payment_pending(
    order_id: int,
    manager_name: str,
    payment_id: int | None,
) -> None:
    """Когда менеджер отметил частичную/полную оплату по credit-заказу —
    шлём push'ы всем boss/admin с кнопками confirm/reject через
    стандартный payment-approval flow (pay_ok/pay_no callbacks).
    Best-effort, тихо ловим ошибки.

    Денежное событие — ниже `boss_instant_threshold_usd` пуш не шлём, платёж
    остаётся pending и попадает в вечерний дайджест (services.boss_digest)."""
    from services import async_db as adb
    from services.notifier import aget_notify_recipients, tg_send_message
    from services.notify_policy import PAYMENT, should_notify_now

    try:
        order = await adb.get_order(order_id)
        if not order or not payment_id:
            return
        payment = await adb.get_payment(payment_id)
        if not payment:
            return
        summary = await adb.get_order_payment_summary(order_id)
        from config import BASE_CURRENCY

        # Имя клиента и менеджера — пользовательский ввод, а сообщение идёт с
        # parse_mode=HTML: «ООО <Строй>» иначе ломает разметку, и босс НЕ
        # получает пуш о платеже, который ждёт его подтверждения.
        currency = esc(order.get("currency") or BASE_CURRENCY)
        agent = esc(order.get("agent_name") or "—")
        due = esc(order.get("due_date") or "—")
        manager_name = esc(manager_name)
        amount = float(payment.get("amount") or 0)
        if not should_notify_now(PAYMENT, amount, currency):
            return
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
        from services import order_payments

        part = (await order_payments.parts_by_payment([int(payment_id)])).get(int(payment_id))
        if part:
            lines.insert(
                5,
                "💳 Как получено: <b>"
                + esc(order_payments.part_label(part["method"], int(part["amount_cents"]), part["currency"]))
                + "</b>" + (f" (курс {esc(part.get('rate') or part.get('order_rate') or '')})"
                            if part.get("rate_source") != "same" else ""),
            )
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
        lines.append(
            "Проверьте банк и подтвердите, что деньги пришли."
            if part and part["method"] in order_payments.NONCASH_METHODS
            else "Подтвердите, что эта сумма реально пришла в кассу."
        )
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
    # Карту и перечисление сверяет с банком руководитель или бухгалтер
    # (менеджер — через совмещение ролей, пока бухгалтера нет; экран и аудит
    # помечают, когда человек подтверждает свои же деньги).
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "bookkeeper"),
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
    from services import order_payments

    all_pending = [
        p for p in await adb.get_payments_for_order(order_id) if p["status"] == "pending"
    ]
    methods = await order_payments.parts_by_payment([int(p["id"]) for p in all_pending])
    # Наличные на руках подтверждаются сдачей — эта кнопка их не трогает.
    pending_before = [
        p for p in all_pending if (methods.get(int(p["id"])) or {}).get("method") != "cash"
    ]
    skipped_cash = len(all_pending) - len(pending_before)

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

    self_note = None
    if n > 0 and any(int(p["user_id"]) == int(user["id"]) for p in pending_before[:n]):
        self_note = order_payments.self_confirm_note(user["id"], user["id"], get_role(user["id"]))
    result = {"ok": True, "confirmed_count": n, "skipped_cash": skipped_cash, "self_note": self_note}
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
        allowed_roles=("admin", "boss", "bookkeeper"),
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
    from services import order_payments

    pending_before = [
        p for p in await adb.get_payments_for_order(order_id)
        if p["status"] == "pending" and not await order_payments.payment_in_active_deposit(p["id"])
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


# ─── API: локальный складской учёт ───────────────────────────────────────────
#
# Остатки и накладные ведутся в собственных таблицах (services/warehouse.py),
# МойСклад здесь не участвует вообще. Права: приход — менеджер и выше, расход
# и отмена — только босс/админ. Расход клиенту идёт через заявку и одобрение;
# прямая расходная накладная обходила бы кредит-лимиты и решение босса.


@app.post("/api/wh/stock")
async def api_wh_stock(request: Request):
    """Текущие остатки локального склада."""
    from services import warehouse

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_stock",
        rate_limit_max=120,
    )
    warehouse_id = data.get("warehouse_id")
    rows = await warehouse.get_stock(
        warehouse_id=int(warehouse_id) if warehouse_id else None,
        only_positive=bool(data.get("only_positive")),
    )
    return JSONResponse(
        {
            "products": [
                {
                    "product_id": r["product_id"],
                    "name": r["name"],
                    "category": r["category"],
                    "sku": r["sku"],
                    "unit": r["unit"],
                    "quantity": float(r["quantity"] or 0),
                }
                for r in rows
            ]
        }
    )


@app.post("/api/wh/counterparties")
async def api_wh_counterparties(request: Request):
    """Справочник контрагентов для подстановки в накладную."""
    from services import adb_core

    data = await request.json()
    _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_counterparties",
        rate_limit_max=120,
    )
    search = (data.get("search") or "").strip()
    if search:
        # Поиск по кириллице — через lower() с обеих сторон. Встроенный SQLite
        # LOWER() ASCII-only, но adb_core._register_sqlite_functions
        # переопределяет его Unicode-aware, как и синхронный слой, — поэтому
        # запрос ведёт себя одинаково на проде и локально (CLAUDE.md).
        rows = await adb_core.fetch(
            "SELECT id, name, type, phone, telegram_id FROM counterparties "
            f"WHERE {adb_core.name_search_sql('name')} LIKE $1 "
            f"ORDER BY {adb_core.order_by_name('name')}, id LIMIT 100",
            adb_core.name_search_param(search),
        )
    else:
        rows = await adb_core.fetch(
            "SELECT id, name, type, phone, telegram_id FROM counterparties "
            f"ORDER BY {adb_core.order_by_name('name')}, id LIMIT 100"
        )
    return JSONResponse({"counterparties": rows})


@app.post("/api/wh/counterparties/create")
async def api_wh_counterparties_create(request: Request):
    """Завести контрагента прямо из формы накладной.

    До этого справочник пополнялся только из карточки клиента в «Воронке», а
    накладную выписывают на склад'е: новый покупатель приезжал, а выбрать его
    в форме было не из чего — работа вставала на ровном месте. Ручка та же
    `counterparties.create`: тёзка не заводится, и повторное нажатие кнопки
    вернёт уже заведённого (`existed`), а не второго такого же.

    Роли — как у самой формы накладной: кто выписывает документ, тот и заводит
    в нём контрагента.
    """
    from services import async_db as adb
    from services import counterparties as cp_service

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_counterparties_create",
        rate_limit_max=30,
    )
    cp_type = (data.get("type") or "customer").strip()
    created = await cp_service.create(
        (data.get("name") or "").strip()[:255],
        phone=(data.get("phone") or "").strip()[:64] or None,
        cp_type=cp_type,
    )
    if not created.get("ok"):
        raise HTTPException(status_code=400, detail=created.get("error", "Не удалось завести"))
    if not created["existed"]:
        await adb.add_audit_log(
            user["id"], _actor_name(user), get_role(user["id"]),
            "counterparty_create",
            f"#{created['counterparty_id']} {created['name']}",
        )
    return JSONResponse(created)


@app.post("/api/wh/invoices")
async def api_wh_invoices(request: Request):
    """Список накладных, новые сверху."""
    from services import warehouse

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_invoices",
        rate_limit_max=120,
    )
    inv_type = data.get("type")
    if inv_type not in (None, "", "incoming", "outgoing"):
        raise HTTPException(status_code=400, detail="Неизвестный тип накладной")
    try:
        limit = min(int(data.get("limit") or 50), 200)
        offset = max(int(data.get("offset") or 0), 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit/offset должны быть числами")

    rows = await warehouse.list_invoices(
        invoice_type=inv_type or None, limit=limit, offset=offset
    )
    # Сумма ПРИХОДА — закупочная цена, то есть себестоимость: не руководству
    # её не отдаём (services.costing.redact_invoice).
    from services.costing import redact_invoice

    role = get_role(user["id"])
    rows = [redact_invoice(r, role) for r in rows]
    from services import printing

    # Кнопка «Распечатать» рисуется, только если в контейнере есть клиент CUPS:
    # кнопка, которая гарантированно ответит отказом, хуже отсутствующей.
    return JSONResponse({"invoices": rows, "can_print": printing.is_available()})


@app.post("/api/wh/invoices/print")
async def api_wh_invoice_print(request: Request):
    """Напечатать накладную на офисный принтер (CUPS). ok = принято очередью."""
    import asyncio

    from services import printing, warehouse
    from services.invoice_pdf import invoice_filename, render_invoice_pdf

    data = await request.json()
    user = _authorize(
        data, allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_invoice_print", rate_limit_max=30,
    )
    try:
        invoice_id = int(data.get("invoice_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invoice_id обязателен")
    inv = await warehouse.get_invoice(invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Накладная не найдена")
    from services.costing import redact_invoice

    # Печать — тот же вывод наружу: закупочные цены прихода не руководству
    # не печатаем.
    inv = redact_invoice(inv, get_role(user["id"]))
    if not printing.is_available():
        return JSONResponse({"ok": False, "error": "Печать не настроена на этом сервере"})
    try:
        pdf = await asyncio.to_thread(render_invoice_pdf, inv)
    except Exception:
        logger.exception("Печать: не собран PDF накладной #%s", invoice_id)
        return JSONResponse({"ok": False, "error": "Не удалось собрать PDF накладной"})
    result = await printing.print_pdf_bytes(
        pdf, filename=invoice_filename(inv),
        label=f"Накладная {inv.get('invoice_number') or invoice_id} · {_actor_name(user)}",
    )
    return JSONResponse({"ok": result.ok, "message": result.message, "error": result.error})


# ─── Юридические документы (расписка, тилхат) ────────────────────────────────
#
# Форма в WebApp → services.documents → legal_docs (docxtpl → LibreOffice) →
# PDF в чат составителя с кнопкой «Распечатать» → печать из WebApp или бота.
_DOC_ROLES = ("admin", "boss", "manager")


@app.post("/api/docs/types")
async def api_docs_types(request: Request):
    """Справочник для формы: типы документов, реквизиты компании, что доступно."""
    from services import documents, printing

    data = await request.json()
    user = _authorize(data, allowed_roles=_DOC_ROLES, rate_limit_scope="api_docs_types")
    role = get_role(user["id"])
    company = await asyncio.to_thread(documents.company_requisites)
    return JSONResponse({
        # Все типы — один бланк юриста (обе части, рус., ўзб.), поля формы у
        # них одинаковые, поэтому различий между типами форма не получает.
        "types": [{"key": k, "label": v} for k, v in documents.DOC_TYPES.items()],
        "company": company,
        "company_fields": [{"key": k, "label": v} for k, v in documents.COMPANY_FIELDS],
        "can_edit_company": role in ("admin", "boss"),
        "can_print": printing.is_available(),
    })


@app.post("/api/docs/company/set")
async def api_docs_company_set(request: Request):
    """Реквизиты компании (кредитор в расписке) — задаёт руководство один раз."""
    from services import documents

    data = await request.json()
    user = _authorize(
        data, allowed_roles=("admin", "boss"), rate_limit_scope="api_docs_company_set"
    )
    values = data.get("company")
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="company: ожидается объект")
    saved = await asyncio.to_thread(documents.save_company_requisites, values, user["id"])
    from services import async_db as adb

    await adb.add_audit_log(
        user["id"], _actor_name(user), get_role(user["id"]),
        "company_requisites", ", ".join(f"{k}={v}" for k, v in saved.items())[:500],
    )
    return JSONResponse({"ok": True, "company": await asyncio.to_thread(documents.company_requisites)})


@app.post("/api/docs/create")
async def api_docs_create(request: Request):
    """Собрать документ по форме, сохранить, отправить составителю в Telegram."""
    from services import documents, printing

    data = await request.json()
    user = _authorize(
        data, allowed_roles=_DOC_ROLES, rate_limit_scope="api_docs_create",
        rate_limit_max=20, rate_limit_window=60.0,
    )
    res = await documents.create_document(data, created_by=user["id"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Не удалось собрать документ"))
    from services import async_db as adb

    await adb.add_audit_log(
        user["id"], _actor_name(user), get_role(user["id"]),
        "document_created", f"#{res['id']} {res['doc_type']}: {res['client_name']}"[:500],
    )
    doc = await documents.get_document(res["id"])
    bot = await get_notify_bot()
    delivery = await documents.send_to_chat(bot, doc, user["id"]) if doc else {"sent": False}
    return JSONResponse({
        "ok": True, "id": res["id"], "filename": res["filename"],
        "sent": bool(delivery.get("sent")), "send_reason": delivery.get("reason"),
        "can_print": printing.is_available(),
    })


@app.post("/api/docs/list")
async def api_docs_list(request: Request):
    from services import documents, printing

    data = await request.json()
    user = _authorize(data, allowed_roles=_DOC_ROLES, rate_limit_scope="api_docs_list", rate_limit_max=120)
    # Менеджер видит только свои документы (в них паспорт и адрес должника).
    own = None if get_role(user["id"]) in documents.DOC_ADMIN_ROLES else user["id"]
    rows = await documents.list_documents(limit=100, created_by=own)
    return JSONResponse({"documents": rows, "can_print": printing.is_available()})


async def _doc_for_user(data: dict, user: dict) -> dict:
    """Документ по `doc_id` с проверкой владельца. Чужой отвечает тем же 404,
    что и несуществующий: перебором id не узнать, какие документы есть."""
    from services import documents

    doc = await documents.get_document(_doc_id_arg(data))
    if doc is None or not documents.can_access(doc, user["id"], get_role(user["id"])):
        raise HTTPException(status_code=404, detail="Документ не найден")
    return doc


def _doc_id_arg(data: dict) -> int:
    try:
        value = int(data.get("doc_id") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="doc_id: не число")
    if value <= 0:
        raise HTTPException(status_code=400, detail="doc_id обязателен")
    return value


@app.post("/api/docs/send")
async def api_docs_send(request: Request):
    """Прислать PDF документа себе в Telegram ещё раз."""
    from services import documents

    data = await request.json()
    user = _authorize(data, allowed_roles=_DOC_ROLES, rate_limit_scope="api_docs_send", rate_limit_max=30)
    doc = await _doc_for_user(data, user)
    delivery = await documents.send_to_chat(await get_notify_bot(), doc, user["id"])
    if not delivery.get("sent"):
        return JSONResponse({"ok": False, "error": delivery.get("reason")})
    return JSONResponse({"ok": True})


@app.post("/api/docs/print")
async def api_docs_print(request: Request):
    """Напечатать документ из сохранённого файла (CUPS). ok = принято очередью."""
    from services import documents, printing

    data = await request.json()
    user = _authorize(data, allowed_roles=_DOC_ROLES, rate_limit_scope="api_docs_print", rate_limit_max=30)
    doc = await _doc_for_user(data, user)
    if not printing.is_available():
        return JSONResponse({"ok": False, "error": "Печать не настроена на этом сервере"})
    found = await asyncio.to_thread(documents.read_pdf, doc)
    if found is None:
        return JSONResponse({"ok": False, "error": "Файл документа не найден — сформируйте заново"})
    pdf, filename = found
    result = await printing.print_pdf_bytes(
        pdf, filename=filename, label=f"{documents.caption_for(doc)} · {_actor_name(user)}"
    )
    return JSONResponse({"ok": result.ok, "message": result.message, "error": result.error})


@app.post("/api/wh/invoices/get")
async def api_wh_invoice_get(request: Request):
    """Одна накладная с позициями."""
    from services import warehouse

    data = await request.json()
    user = _authorize(data, allowed_roles=("admin", "boss", "manager"))
    try:
        invoice_id = int(data.get("invoice_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invoice_id обязателен")

    inv = await warehouse.get_invoice(invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Накладная не найдена")
    from services.costing import redact_invoice

    return JSONResponse({"invoice": redact_invoice(inv, get_role(user["id"]))})


@app.post("/api/wh/invoices/create")
async def api_wh_invoice_create(request: Request):
    """Провести накладную. Остатки двигаются сразу — draft'а нет.

    Идемпотентность обязательна: накладная меняет остатки, и повторно
    отправленная форма (дрогнула связь, юзер нажал дважды) без ключа
    списала бы товар второй раз.
    """
    from config import BASE_CURRENCY
    from services import async_db as adb
    from services import warehouse

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_invoice_create",
        rate_limit_max=30,
    )

    inv_type = data.get("type")
    if inv_type not in ("incoming", "outgoing"):
        raise HTTPException(status_code=400, detail="type: incoming или outgoing")
    # Расход — только руководство. Отгрузка клиенту идёт через заявку и
    # одобрение (кредит-лимит, override, аудит); прямая расходная накладная
    # менеджером обходила бы весь этот контур: товар уезжал бы без заказа,
    # без долга и без решения босса. Приход менеджеру оставлен — приёмка
    # контейнера это его работа, и остаток от неё только растёт.
    if inv_type == "outgoing" and get_role(user["id"]) not in ("admin", "boss"):
        raise HTTPException(
            status_code=403,
            detail="Расходную накладную проводит руководство — отгрузка клиенту идёт "
            "через заявку на отгрузку и её одобрение",
        )

    invoice_date = str(data.get("invoice_date") or "").strip() or None
    if invoice_date:
        from datetime import datetime as _dt

        try:
            _dt.strptime(invoice_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="invoice_date: ожидается YYYY-MM-DD")
    raw_wh = data.get("warehouse_id")
    try:
        # Склад по умолчанию — из справочника, а не «1»: на базе, где первый
        # склад создан не первым, захардкоженная единица отвергала бы каждую
        # накладную «склад не найден».
        warehouse_id = int(raw_wh) if raw_wh else await warehouse.default_warehouse_id()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="warehouse_id должен быть числом")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise HTTPException(status_code=400, detail="Добавьте хотя бы одну позицию")

    counterparty_id = data.get("counterparty_id")
    if inv_type == "outgoing" and not counterparty_id:
        raise HTTPException(status_code=400, detail="Для расхода нужен контрагент")

    idem = _Idem(adb, "wh_invoice_create", user["id"], data.get("idempotency_key"))
    prev = await idem.claim()
    if prev is not None:
        return JSONResponse(prev)

    try:
        result = await warehouse.create_invoice(
            invoice_type=inv_type,
            warehouse_id=warehouse_id,
            counterparty_id=int(counterparty_id) if counterparty_id else None,
            items=raw_items,
            currency=(data.get("currency") or BASE_CURRENCY),
            invoice_date=invoice_date,
            comment=(data.get("comment") or None),
            created_by=user["id"],
        )
    except Exception:
        # Ключ освобождаем только на НЕОЖИДАННОМ сбое: отказ по бизнес-правилу
        # (нехватка остатка) — это законный результат, и он ниже сохраняется
        # под ключом, чтобы ретрай той же формы отдал тот же ответ.
        await idem.release()
        raise

    if not result.get("ok"):
        await idem.store(result)
        return JSONResponse(result, status_code=409)

    await adb.add_audit_log(
        user["id"],
        user.get("first_name", ""),
        get_role(user["id"]),
        "wh_invoice_create",
        f"Накладная {result['invoice_number']} ({inv_type}), "
        f"позиций {result['positions']}, сумма {result['total_amount_cents']} коп.",
    )

    # PDF клиенту — только по расходу и только после успешного проведения.
    # Сбой доставки не откатывает накладную: она проведена, остатки списаны.
    # Отправляем ДО idem.store, чтобы ретрай той же формы отдал сохранённый
    # ответ и не прислал клиенту второй экземпляр документа.
    if inv_type == "outgoing":
        from services.invoice_delivery import REASON_TEXT, deliver_invoice_pdf

        try:
            invoice = await warehouse.get_invoice(result["invoice_id"])
            if invoice is None:
                # Накладную только что создали в этой же транзакции; None здесь
                # означал бы, что её кто-то успел удалить в обход приложения.
                raise RuntimeError(f"накладная {result['invoice_id']} исчезла после создания")
            bot = await get_notify_bot()
            delivery = await deliver_invoice_pdf(invoice, bot)
        except Exception:
            logger.exception("Доставка PDF накладной %s упала", result["invoice_number"])
            delivery = {"sent": False, "reason": "send_failed"}
        result["pdf_sent"] = delivery["sent"]
        if not delivery["sent"]:
            result["pdf_warning"] = REASON_TEXT.get(delivery["reason"], "PDF не отправлен")

    await idem.store(result)
    return JSONResponse(result)


@app.post("/api/wh/invoices/send")
async def api_wh_invoice_send(request: Request):
    """Отправить (или переотправить) PDF накладной клиенту вручную.

    Нужен для случая из ТЗ, когда у контрагента не был привязан telegram_id
    в момент проведения: накладная сохранена, остатки списаны, отправка
    делается позже — этой кнопкой.
    """
    from services import warehouse
    from services.invoice_delivery import REASON_TEXT, deliver_invoice_pdf

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_invoice_send",
        rate_limit_max=20,
    )
    try:
        invoice_id = int(data.get("invoice_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invoice_id обязателен")

    invoice = await warehouse.get_invoice(invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="Накладная не найдена")

    bot = await get_notify_bot()
    delivery = await deliver_invoice_pdf(invoice, bot, force=bool(data.get("force")))
    if not delivery["sent"]:
        return JSONResponse(
            {
                "ok": False,
                "code": delivery["reason"],
                "reason": REASON_TEXT.get(delivery["reason"], "PDF не отправлен"),
            },
            status_code=409,
        )
    logger.info(
        "PDF накладной #%s отправлен вручную пользователем %s", invoice_id, user["id"]
    )
    return JSONResponse({"ok": True, "sent": True})


async def _invoice_owner_refusal(invoice_id: int) -> dict | None:
    """Отказ для накладной, привязанной к заказу/контейнеру/возврату; None — можно."""
    from services import adb_core, warehouse

    # История МойСклад — первой: иначе первая отгрузка исторического заказа
    # получила бы совет «отмените заказ», а заказ отменить тоже нельзя.
    historical = await warehouse.historical_invoice_refusal(invoice_id)
    if historical:
        return {"ok": False, "code": "historical", "reason": historical, "detail": historical}
    # Приход по возврату: отмена накладной забрала бы товар со склада, а
    # возврат остался бы подтверждённым — с returned_qty и деньгами клиенту.
    ret = await adb_core.fetchrow(
        "SELECT return_id, order_id FROM return_receipt WHERE invoice_id = $1", invoice_id
    )
    if ret is not None:
        reason = (
            f"Эта накладная — приход товара по возврату #{ret['return_id']} "
            f"(заказ #{ret['order_id']}). Отдельно от возврата её не отменить: товар "
            "ушёл бы со склада, а деньги и возвращённое количество остались бы учтены. "
            "Отмените возврат, а не накладную."
        )
        return {"ok": False, "code": "linked_return", "return_id": int(ret["return_id"]),
                "order_id": int(ret["order_id"]), "reason": reason, "detail": reason}

    order_id = await adb_core.fetchval(
        "SELECT order_id FROM order_shipment WHERE invoice_id = $1", invoice_id
    )
    if order_id is not None:
        reason = (
            f"Эта накладная — отгрузка заказа #{order_id}. Отмените заказ: "
            "отмена заказа сама вернёт товар на склад и закроет долг."
        )
        return {"ok": False, "code": "linked_order", "order_id": int(order_id),
                "reason": reason, "detail": reason}
    container = await adb_core.fetchrow(
        "SELECT r.container_id, c.number FROM container_receipt r "
        "LEFT JOIN containers c ON c.id = r.container_id WHERE r.invoice_id = $1",
        invoice_id,
    )
    if container is not None:
        label = container.get("number") or f"#{container['container_id']}"
        reason = (
            f"Эта накладная — приход контейнера {label}. Отмените контейнер "
            "(удалите его или переоприходуйте), а не накладную."
        )
        return {"ok": False, "code": "linked_container",
                "container_id": int(container["container_id"]),
                "reason": reason, "detail": reason}
    return None


@app.post("/api/wh/invoices/cancel")
async def api_wh_invoice_cancel(request: Request):
    """Отменить накладную — откат двигает остатки назад.

    Менеджеру — пока руководитель не включил `delete_requires_boss` (решение
    владельца: «пока может и менеджер, в будущем только руководитель»).
    Накладные заказов и контейнеров отменяются через них — `_invoice_owner_refusal`.
    """
    from services import async_db as adb
    from services import warehouse

    data = await request.json()
    user = _authorize(
        data,
        allowed_roles=("admin", "boss", "manager"),
        rate_limit_scope="api_wh_invoice_cancel",
        rate_limit_max=20,
    )
    await _require_delete_right(get_role(user["id"]))
    try:
        invoice_id = int(data.get("invoice_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invoice_id обязателен")

    # Накладная, которую провёл заказ или контейнер, отменяется ЧЕРЕЗ них.
    # Прямая отмена возвращала остаток, но заказ оставался «отгружен» с долгом
    # за товар, который вернулся на склад, а контейнер — «оприходован» со
    # ссылкой на отменённый приход (переоприходовать его после этого было
    # нельзя, удалить — тоже без ошибки).
    linked = await _invoice_owner_refusal(invoice_id)
    if linked:
        return JSONResponse(linked, status_code=409)

    result = await warehouse.cancel_invoice(invoice_id, cancelled_by=user["id"])
    if not result.get("ok"):
        return JSONResponse(result, status_code=409)

    await adb.add_audit_log(
        user["id"],
        user.get("first_name", ""),
        get_role(user["id"]),
        "wh_invoice_cancel",
        f"Отменена накладная #{invoice_id}, остатки откачены",
    )
    return JSONResponse(result)


# ─── Себестоимость: ручки отдельным роутером (webapp/costing_api.py) ─────────
# Подключаем в конце: роутер берёт `_authorize` и помощники отсюда.
from webapp.costing_api import router as _costing_router  # noqa: E402

app.include_router(_costing_router)

# ─── Бухгалтерия (/api/acc/*) ─────────────────────────────────────────────────
# Отдельный роутер: счета, журнал денег, «Деньги сейчас». Всё за выключателем
# `accounting_enabled` — пока он выключен, ручки отвечают 409, а старые потоки
# не меняются.
from webapp.routes_accounting import router as _accounting_router  # noqa: E402

app.include_router(_accounting_router)


# ─── Запуск ───────────────────────────────────────────────────────────────────


async def start_webapp():
    """Запустить FastAPI сервер в фоне."""
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("WebApp запускается на порту %d", port)
    await server.serve()
