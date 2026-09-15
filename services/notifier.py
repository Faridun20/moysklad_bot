"""
Отправка сообщений в Telegram из любого процесса.

Общая aiohttp-сессия к Bot API + список получателей уведомлений
(admin/boss). Здесь же жил поллер новых отгрузок МойСклад: он опрашивал
`entity/demand` и рассказывал руководству о документах, созданных в чужом
интерфейсе. Отгрузки теперь наши — их проводит бот, и рассказывать самому
себе о собственном действии не нужно.
"""

import asyncio
import logging

import aiohttp

from config import TELEGRAM_TOKEN
from services.database import get_all_users
from utils.helpers import redact_token

logger = logging.getLogger(__name__)


# ─── Общая aiohttp-сессия для прямых POST в Telegram Bot API ─────────────
#
# Используется webapp-эндпоинтами (api_payments_send, api_submit_order),
# где у нас нет под рукой aiogram.Bot, и проще POST'нуть напрямую.
# Раньше каждый вызов создавал свой ClientSession — это новое TCP+TLS-
# рукопожатие к api.telegram.org. На активном чате с десятком уведомлений
# в минуту это ощутимо.

_TG_TIMEOUT = aiohttp.ClientTimeout(total=10)
_tg_session: aiohttp.ClientSession | None = None
_tg_session_lock = asyncio.Lock()


async def get_tg_session() -> aiohttp.ClientSession:
    """Вернуть общую сессию для запросов в api.telegram.org.

    base_url держит ТОЛЬКО origin (`https://api.telegram.org`), токен идёт в
    пути запроса. Path-часть в base_url не работает: aiohttp 3.9 её запрещал
    (ValueError / AssertionError на sess.post), а с 3.10 требует `/` на конце
    и всё равно ОТБРАСЫВАЕТ её, если путь запроса начинается с `/`.

    SECURITY.md H8: чтобы TELEGRAM_TOKEN не утёк в Railway logs через
    repr(exception), в except'е tg_send_message строка ошибки
    прогоняется через _redact_token().
    """
    global _tg_session
    if _tg_session is None or _tg_session.closed:
        async with _tg_session_lock:
            if _tg_session is None or _tg_session.closed:
                connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
                _tg_session = aiohttp.ClientSession(
                    base_url="https://api.telegram.org",
                    connector=connector,
                    timeout=_TG_TIMEOUT,
                )
    return _tg_session


def _redact_token(text: str) -> str:
    """Убрать TELEGRAM_TOKEN из строки (для логов ошибок).

    Реализация переехала в `utils.helpers.redact_token` — тем же понадобился
    прокси фотографий техники в webapp. Имя оставлено как есть: им пользуются
    вызовы ниже и тесты.
    """
    return redact_token(text)


# ─── Видимость массового отказа отправки ────────────────────────────────────
# tg_send_message — best-effort: единичный сбой глушится в warning и тонет в
# логах. Чтобы массовый отказ (как недавний баг с base_url, когда падали ВСЕ
# уведомления) был заметен сразу, считаем подряд-неудачи и эскалируем в error
# при превышении порога.
_send_fail_streak = 0
SEND_FAIL_ALERT_AFTER = 5


def _note_send_ok() -> None:
    global _send_fail_streak
    _send_fail_streak = 0


def _note_send_fail(chat_id: int, detail: str) -> None:
    global _send_fail_streak
    _send_fail_streak += 1
    if _send_fail_streak == SEND_FAIL_ALERT_AFTER:
        logger.error(
            "tg sendMessage: %d неудач подряд — уведомления, похоже, не "
            "доходят (последняя цель %d: %s)",
            _send_fail_streak,
            chat_id,
            detail,
        )
    else:
        logger.warning("tg sendMessage %d failed: %s", chat_id, detail)


async def close_tg_session() -> None:
    global _tg_session
    if _tg_session is not None and not _tg_session.closed:
        await _tg_session.close()
    _tg_session = None


async def tg_send_message(
    chat_id: int,
    text: str,
    *,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
) -> bool:
    """Послать сообщение через Bot API без aiogram.Bot.

    Best-effort: не бросаем исключений (единичный сбой не должен ронять
    рассылку). Возвращаем True при успешной доставке, False при ошибке —
    вызывающий может это игнорировать (большинство), либо использовать для
    решения (R5: дедуп-слот отгрузки столбим только при успехе).
    """
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        sess = await get_tg_session()
        async with sess.post(f"/bot{TELEGRAM_TOKEN}/sendMessage", json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                _note_send_fail(chat_id, f"HTTP {resp.status}: {_redact_token(body[:200])}")
                return False
            _note_send_ok()
            return True
    except Exception as e:
        # repr(e) у aiohttp-ошибок может содержать URL с токеном — редактим.
        _note_send_fail(chat_id, _redact_token(repr(e)))
        return False


# ─── Получатели уведомлений ───
def get_notify_recipients() -> list[int]:
    """
    Получить список получателей уведомлений об отгрузках:
    все пользователи с ролью admin или boss.

    Sync-версия — для использования из bot-процесса в фоновых задачах
    (notifier loop, scheduled reports), где блокировка event loop
    некритична (всё равно ждём следующего тика sleep'а).
    """
    try:
        users = get_all_users()
        recipients = [
            u["user_id"]
            for u in users
            if u["role"] in ("admin", "boss") and not u.get("deactivated_at")
        ]
        if recipients:
            logger.info(
                "notify recipients (DB): %s (boss/admin из %d users)",
                recipients,
                len(users),
            )
            return recipients
        else:
            logger.warning(
                "notify recipients (DB): пусто! users в БД: %d, "
                "ни у одного нет роли admin/boss — переключаемся на config fallback",
                len(users),
            )
    except Exception as e:
        logger.warning("Не удалось получить список получателей из БД: %s", e)

    # Фолбэк — берём из config если БД недоступна
    try:
        from config import ADMIN_IDS, BOSS_IDS

        recipients = list(set(ADMIN_IDS + BOSS_IDS))
        if recipients:
            logger.info("notify recipients (config fallback): %s", recipients)
        else:
            logger.error(
                "notify recipients ПУСТО: в БД нет admin/boss и "
                "ADMIN_IDS/BOSS_IDS в config не заданы. "
                "Уведомления никому не уйдут!",
            )
        return recipients
    except Exception:
        return []


async def aget_notify_recipients() -> list[int]:
    """Async-версия для webapp endpoint'ов: SQL уходит в thread pool,
    event loop остаётся свободным для других одновременных запросов."""
    return await asyncio.to_thread(get_notify_recipients)


# Рассылка параллельно, но с ограничением: у Telegram ~30 сообщений/сек
# глобально. Semaphore сглаживает пик и не даёт словить 429 на большом списке.
_BROADCAST_CONCURRENCY = 20


async def _gather_limited(coros: list) -> list:
    """Выполнить корутины с ограничением параллелизма; ошибки не пробрасываем
    (рассылка best-effort, единичный сбой не должен ронять остальные).
    Возвращает список результатов (Exception для упавших) — вызывающий может
    игнорировать или проверить (R5)."""
    sem = asyncio.Semaphore(_BROADCAST_CONCURRENCY)

    async def _run(coro):
        async with sem:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros), return_exceptions=True)
