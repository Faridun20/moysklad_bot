"""
Управление webhook-подписками МойСклад.

При старте бота:
  1. Запрашиваем список существующих webhook'ов.
  2. Проверяем, есть ли среди них наши (по URL).
  3. Если нет — регистрируем подписки на события, влияющие на остатки.

Безопасность:
  URL вебхука включает секрет (env MS_WEBHOOK_SECRET) как путь:
      <WEBAPP_URL>/api/ms-webhook/<secret>
  Без знания секрета произвольный клиент не сможет дёргать наш
  endpoint и форсить рефреш стоков.
"""

import logging
import os
import secrets as _secrets

from services.moysklad import ms_get, get_session, MS_BASE

logger = logging.getLogger(__name__)


# Перечень событий, влияющих на остатки. Можно расширять.
SUBSCRIPTIONS = [
    ("demand", "CREATE"),
    ("demand", "UPDATE"),
    ("demand", "DELETE"),
    ("supply", "CREATE"),
    ("supply", "UPDATE"),
    ("loss", "CREATE"),
    ("move", "CREATE"),
    ("inventory", "CREATE"),
    ("retaildemand", "CREATE"),
]


def get_webhook_secret() -> str:
    """Секрет для URL вебхука. Берётся из env, иначе генерируется
    случайный (только для локальной разработки — на проде задайте env)."""
    secret = os.environ.get("MS_WEBHOOK_SECRET", "").strip()
    if secret:
        return secret
    # Fallback — стабильно случайный на время процесса
    if not hasattr(get_webhook_secret, "_cached"):
        get_webhook_secret._cached = _secrets.token_urlsafe(24)
        logger.warning(
            "MS_WEBHOOK_SECRET не задан в env. Сгенерирован временный секрет — "
            "после рестарта вебхуки придётся перерегистрировать. "
            "Установите MS_WEBHOOK_SECRET в Railway."
        )
    return get_webhook_secret._cached


def get_webhook_url() -> str | None:
    """Полный URL для регистрации в МойСклад."""
    base = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
    if not base:
        return None
    return f"{base}/api/ms-webhook/{get_webhook_secret()}"


async def list_webhooks() -> list[dict]:
    data = await ms_get("entity/webhook")
    return data.get("rows", []) if isinstance(data, dict) else []


async def create_webhook(entity_type: str, action: str, url: str) -> dict | None:
    sess = await get_session()
    payload = {"url": url, "action": action, "entityType": entity_type}
    async with sess.post(f"{MS_BASE}/entity/webhook", json=payload) as resp:
        body = await resp.text()
        if resp.status >= 400:
            logger.error(
                "MS create webhook %s.%s -> %s: %s",
                entity_type, action, resp.status, body[:300],
            )
            return None
        try:
            import json
            return json.loads(body)
        except Exception:
            return None


async def delete_webhook(webhook_id: str) -> bool:
    sess = await get_session()
    async with sess.delete(f"{MS_BASE}/entity/webhook/{webhook_id}") as resp:
        if resp.status >= 400:
            body = await resp.text()
            logger.warning("MS delete webhook %s: %s %s",
                           webhook_id, resp.status, body[:200])
            return False
    return True


async def ensure_subscriptions() -> dict:
    """
    Идемпотентно: проверяет существующие подписки и регистрирует
    недостающие. Также удаляет наши же подписки на старые URL
    (после смены WEBAPP_URL или ротации секрета).

    Возвращает summary: {"existing": N, "created": [...], "removed_stale": [...]}.
    """
    target_url = get_webhook_url()
    if not target_url:
        logger.warning(
            "ensure_subscriptions: WEBAPP_URL не задан — пропускаем регистрацию"
        )
        return {"skipped": True, "reason": "no WEBAPP_URL"}

    try:
        existing = await list_webhooks()
    except Exception as e:
        logger.exception("ensure_subscriptions: list_webhooks failed: %s", e)
        return {"error": str(e)}

    # Группируем существующие по (entityType, action)
    by_key: dict[tuple[str, str], list[dict]] = {}
    for w in existing:
        key = (w.get("entityType"), w.get("action"))
        by_key.setdefault(key, []).append(w)

    created: list[str] = []
    removed_stale: list[str] = []

    for entity_type, action in SUBSCRIPTIONS:
        bucket = by_key.get((entity_type, action), [])

        # Если уже есть наша актуальная подписка — пропускаем,
        # лишние с устаревшим URL удаляем.
        matching = [w for w in bucket if w.get("url") == target_url]
        stale = [w for w in bucket if w.get("url") != target_url
                 and "/api/ms-webhook" in (w.get("url") or "")]

        for w in stale:
            wid = w.get("id")
            if wid and await delete_webhook(wid):
                removed_stale.append(f"{entity_type}.{action}:{wid}")

        if not matching:
            res = await create_webhook(entity_type, action, target_url)
            if res:
                created.append(f"{entity_type}.{action}")

    logger.info(
        "ensure_subscriptions: existing=%d, created=%s, removed_stale=%s",
        len(existing), created, removed_stale,
    )
    return {
        "existing": len(existing),
        "target_url": target_url,
        "created": created,
        "removed_stale": removed_stale,
    }
