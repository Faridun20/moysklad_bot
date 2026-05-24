"""
Observability — Sentry hook + helpers.

Дизайн:
  * **opt-in через SENTRY_DSN** — без env-переменной все функции no-op,
    зависимость `sentry-sdk` тоже опциональна (если не установлена —
    модуль импортируется, но `init_sentry` молча возвращается).
    Это значит: текущий прод без DSN работает идентично сегодняшнему,
    добавление DSN включает трассировки без релиза кода.
  * **scrubbing PII** — `before_send` хук режет `initData`/`secret`/
    `token`/`password` из events и breadcrumbs. Telegram initData
    содержит `hash=...&user=...&auth_date=...` — это секрет бота-
    юзера, его НИ В КОЕМ СЛУЧАЕ нельзя отправлять во внешнюю SaaS.
    MS_TOKEN / TELEGRAM_TOKEN — то же.
  * **тонкие обёртки** — `capture_exception`, `capture_message`,
    `set_user`, `set_tag` — не падают если Sentry не инициализирован,
    чтобы вызывающий код не оборачивал каждый вызов в `if has_sentry`.

Где вызывать `init_sentry`:
  * bot.py — компонент `bot`
  * webapp/server.py — компонент `webapp` (FastAPI integration ловит
    необработанные exceptions из endpoint'ов)
  * tasks/run_*.py — компонент `cron-<name>` (через `run_cron_main`)

Где явно вызывать `capture_exception`:
  * любые `except Exception:` где сейчас стоит `logger.exception(...)`
    — добавляется параллельно, лог остаётся
  * в particular: notify_new_shipment, MS webhook handler, hand-off
    в МС (create_demand, create_paymentin, create_salesreturn)
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Ключи, которые scrub'аем из events/breadcrumbs/contexts.
# Match — case-insensitive по подстроке (см. `_scrub_mapping`).
_SENSITIVE_KEYS = (
    "initdata",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "ms_token",
    "telegram_token",
    "tg_webhook_secret",
    "ms_webhook_secret",
    "database_url",  # содержит password
)

_REDACTED = "***REDACTED***"

# Включён ли Sentry. Устанавливается в init_sentry().
_initialized = False


def _scrub_mapping(d: Any) -> Any:
    """Рекурсивно подменить значения подозрительных ключей на ***REDACTED***.

    Sentry SDK сам делает default scrubbing, но он ловит фиксированный
    набор (Authorization, Cookie, X-Api-Key). Здесь добавляем доменно-
    специфичные: initData (Telegram WebApp HMAC), MS_TOKEN, DATABASE_URL.
    """
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            kl = str(k).lower()
            if any(s in kl for s in _SENSITIVE_KEYS):
                out[k] = _REDACTED
            else:
                out[k] = _scrub_mapping(v)
        return out
    if isinstance(d, list):
        return [_scrub_mapping(x) for x in d]
    return d


def _before_send(event: dict, hint: dict) -> dict | None:  # type: ignore[type-arg]
    """Sentry before_send: режем PII, отбрасываем шум.

    Возвращаем `None` чтобы отбросить event целиком (не используем —
    шлём всё, scrub'ив).
    """
    try:
        # Request data — особо опасно (query/body с initData).
        request = event.get("request")
        if isinstance(request, dict):
            for k in ("data", "query_string", "headers", "cookies", "env"):
                if k in request:
                    request[k] = _scrub_mapping(request[k])
        # Extra / contexts — могут содержать payload с тегами.
        if "extra" in event:
            event["extra"] = _scrub_mapping(event["extra"])
        if "contexts" in event:
            event["contexts"] = _scrub_mapping(event["contexts"])
        # tags — теги обычно безопасны, но clamp на длину чтобы не
        # утечь длинные значения.
        if isinstance(event.get("tags"), dict):
            event["tags"] = {k: str(v)[:200] for k, v in event["tags"].items()}
    except Exception:
        # before_send не должен ронять отправку.
        logger.debug("sentry before_send scrub failed", exc_info=True)
    return event


def _before_breadcrumb(crumb: dict, hint: dict) -> dict | None:  # type: ignore[type-arg]
    """Scrub breadcrumb data (logger messages с initData/токенами)."""
    try:
        if "data" in crumb:
            crumb["data"] = _scrub_mapping(crumb["data"])
        # message может содержать сырой URL с tg-токеном (notifier
        # _redact_token уже подменяет на ***, но второй слой не помешает).
        msg = crumb.get("message")
        if isinstance(msg, str):
            for tok in (os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("MS_TOKEN", "")):
                if tok and len(tok) >= 8 and tok in msg:
                    crumb["message"] = msg.replace(tok, _REDACTED)
                    msg = crumb["message"]
    except Exception:
        pass
    return crumb


def init_sentry(component: str) -> bool:
    """Инициализировать Sentry SDK. Возвращает True если включился.

    No-op если:
      * SENTRY_DSN не задан (намеренно — это переключатель)
      * `sentry-sdk` не установлен (не должно быть в проде, но защита
        от случайного pip-rollback'а)
      * уже инициализирован в этом процессе (повторный вызов — no-op)
    """
    global _initialized
    if _initialized:
        return True
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("SENTRY_DSN не задан — observability отключён (%s)", component)
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.warning("sentry-sdk не установлен — observability недоступен (%s)", component)
        return False

    # Уровни логов → Sentry. WARNING+ как breadcrumbs, ERROR+ как events.
    # exception=False в logger.error → не дублируем с capture_exception.
    logging_integration = LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR)

    integrations: list[Any] = [logging_integration]
    # FastAPI integration ставится автоматически если установлен fastapi —
    # доп. ничего не нужно.

    sentry_sdk.init(
        dsn=dsn,
        # Release / environment — берём из Railway env, дефолты безопасные.
        environment=os.environ.get("SENTRY_ENVIRONMENT")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or "local",
        release=os.environ.get("SENTRY_RELEASE") or os.environ.get("RAILWAY_GIT_COMMIT_SHA"),
        # 0.0 — отключает performance trace (мы используем Sentry только
        # для error tracking, не для APM). Можно поднять через env если
        # понадобится.
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        # send_default_pii=False — НЕ слать IP/cookies/headers по умолчанию.
        # В Telegram-боте user.id — единственное что хотим видеть, его
        # привязываем явно через set_user в middleware.
        send_default_pii=False,
        attach_stacktrace=True,
        # Шлём sample 100% events с уровня WARNING — поток низкий, фильтр
        # на стороне Sentry проще чем теряет на источнике.
        sample_rate=1.0,
        max_breadcrumbs=50,
        before_send=_before_send,
        before_breadcrumb=_before_breadcrumb,
        integrations=integrations,
    )
    sentry_sdk.set_tag("component", component)
    _initialized = True
    logger.info("Sentry инициализирован: component=%s, env=%s", component, os.environ.get("RAILWAY_ENVIRONMENT") or "local")
    return True


def is_enabled() -> bool:
    return _initialized


def capture_exception(exc: BaseException | None = None) -> None:
    """No-op без Sentry. Внутри: scrub + send."""
    if not _initialized:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except Exception:
        logger.debug("sentry capture_exception failed", exc_info=True)


def capture_message(message: str, level: str = "info") -> None:
    if not _initialized:
        return
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level=level)  # type: ignore[arg-type]
    except Exception:
        logger.debug("sentry capture_message failed", exc_info=True)


def set_user(user_id: int | None, username: str | None = None) -> None:
    """Привязать current user_id к scope. Без Sentry — no-op.

    В Telegram-боте вызывается из RateLimitMiddleware и из webapp
    _authorize — единая точка идентификации юзера.
    """
    if not _initialized:
        return
    try:
        import sentry_sdk

        if user_id is None:
            sentry_sdk.set_user(None)
        else:
            sentry_sdk.set_user({"id": str(user_id), "username": username or ""})
    except Exception:
        logger.debug("sentry set_user failed", exc_info=True)


def set_tag(key: str, value: Any) -> None:
    if not _initialized:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_tag(key, str(value)[:200])
    except Exception:
        logger.debug("sentry set_tag failed", exc_info=True)


def set_context(name: str, ctx: dict) -> None:  # type: ignore[type-arg]
    if not _initialized:
        return
    try:
        import sentry_sdk

        sentry_sdk.set_context(name, _scrub_mapping(ctx))
    except Exception:
        logger.debug("sentry set_context failed", exc_info=True)
