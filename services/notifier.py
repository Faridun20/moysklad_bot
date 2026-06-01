"""
Фоновая задача: мониторинг новых отгрузок и уведомления
Отправляет уведомления всем пользователям с ролью admin и boss
"""

import asyncio
import logging
from datetime import datetime

import aiohttp
from aiogram import Bot

from config import CHECK_INTERVAL_SEC as _CHECK_INTERVAL_SEC, TELEGRAM_TOKEN

CHECK_INTERVAL_SEC = int(_CHECK_INTERVAL_SEC)
from services.moysklad import get_shipments, get_shipment, get_shipment_positions
from services.database import (
    get_all_users,
    get_pool_stats,
    mark_shipment_notified,
    prune_notified_shipments,
)
from services import metrics
from utils.helpers import extract_id_from_href
from utils.formatters import format_shipment, DIV

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

    base_url держит ТОЛЬКО origin (`https://api.telegram.org`) — aiohttp
    запрещает path-часть в base_url (ValueError / AssertionError на
    sess.post), поэтому токен идёт в относительном пути запроса.

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
    """Убрать TELEGRAM_TOKEN из строки (для логов ошибок)."""
    if TELEGRAM_TOKEN and TELEGRAM_TOKEN in text:
        return text.replace(TELEGRAM_TOKEN, "***")
    return text


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


async def send_to_recipients(bot: Bot, text: str, recipients: list[int]):
    """Разослать сообщение списку получателей (параллельно, с лимитом)."""

    async def _one(uid: int):
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось отправить %d: %s", uid, e)

    await _gather_limited([_one(uid) for uid in recipients])


def _is_bot_created(shipment: dict) -> bool:
    """True, если demand создан этим ботом — у него проставлены кастом-атрибуты
    telegram_full_name/telegram_user_id (см. ms_demand.create_demand_from_request).
    Такие отгрузки босс уже одобрил, повторное «новая отгрузка» — шум."""
    attrs = shipment.get("attributes")
    if not isinstance(attrs, list):
        return False
    marker_names = {"telegram_full_name", "telegram_user_id"}
    try:
        from services.ms_demand import _CTX

        marker_names |= {_CTX.get("attribute_name"), _CTX.get("attribute_uid")}
    except Exception:
        pass
    marker_names.discard(None)
    return any(
        isinstance(a, dict) and a.get("name") in marker_names and a.get("value") not in (None, "")
        for a in attrs
    )


async def notify_new_shipment(
    demand_id: str,
    shipment: dict | None = None,
    recipients: list[int] | None = None,
) -> bool:
    """Уведомить boss/admin об ОДНОЙ новой отгрузке. Возвращает True, если
    уведомление отправлено.

    Дедуп через БД (`mark_shipment_notified`): и MS-вебхук (webapp-процесс),
    и поллер (bot-процесс) зовут эту функцию — на каждый demand уйдёт ровно
    одно уведомление, кто бы ни успел первым.

    Слот «застолбляется» ПЕРЕД отправкой (но после того как объект и
    получатели готовы) — если получить demand не удалось, слот свободен и
    поллер-резерв сможет повторить позже.
    """
    if not demand_id:
        return False
    if shipment is None:
        try:
            shipment = await get_shipment(demand_id)
        except Exception as e:
            logger.warning("notify_new_shipment: demand %s fetch failed: %s", demand_id, e)
            return False
    if not shipment:
        return False

    # Бот-созданные отгрузки (одобрение заявки → бот сам создал demand) босс
    # уже видел — повторно не уведомляем. Слот застолбляем, чтобы и поллер
    # промолчал.
    if _is_bot_created(shipment):
        mark_shipment_notified(demand_id)
        return False

    if recipients is None:
        recipients = get_notify_recipients()
    if not recipients:
        logger.warning("notify_new_shipment: нет получателей")
        return False

    # Атомарно застолбить отправку — гонка вебхука и поллера решается здесь.
    if not mark_shipment_notified(demand_id):
        metrics.incr("notify.shipment.dedup_skip")
        return False

    positions = []
    try:
        positions = await get_shipment_positions(demand_id)
    except Exception:
        pass

    txt = f"{DIV}\n🔔 <b>Новая отгрузка!</b>\n\n" + format_shipment(shipment, positions)
    async with metrics.atimer("notify.shipment.broadcast"):
        results = await _gather_limited([tg_send_message(uid, txt) for uid in recipients])
    # R5: если НИ ОДНО уведомление не доставлено (Telegram down) — освобождаем
    # дедуп-слот, чтобы резервный поллер повторил позже (иначе потеря навсегда).
    if not any(r is True for r in results):
        from services.database import unmark_shipment_notified

        unmark_shipment_notified(demand_id)
        metrics.incr("notify.shipment.all_failed")
        return False
    metrics.incr("notify.shipment.sent")
    return True


async def shipment_notifier(bot: Bot | None = None):
    """Резервный поллер: раз в CHECK_INTERVAL_SEC добирает отгрузки, которые
    не пришли через MS-вебхук. Дедуп общий с вебхуком (notify_new_shipment),
    поэтому дублей нет. `bot` больше не нужен (шлём через tg_send_message),
    оставлен для совместимости со стартом фоновых задач."""
    last_check: datetime = datetime.now()
    logger.info("Мониторинг отгрузок (резерв) запущен, интервал %s с", CHECK_INTERVAL_SEC)

    # Чистим дедуп-таблицу примерно раз в сутки (а не каждый цикл).
    _prune_every = max(1, 86400 // CHECK_INTERVAL_SEC)
    # Логируем pool-stats примерно раз в час, чтобы не засорять логи
    # (при CHECK_INTERVAL_SEC=900 это каждые 4 цикла).
    _pool_every = max(1, 3600 // CHECK_INTERVAL_SEC)
    _cycle = 0
    # Чтобы не повторять алерт каждый цикл при устойчиво высокой
    # загрузке пула — взводим флаг при первой засветке выше порога,
    # сбрасываем когда util падает ниже него.
    _pool_alert_armed = False

    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        _cycle += 1
        if _cycle % _prune_every == 0:
            try:
                deleted = await asyncio.to_thread(prune_notified_shipments)
                if deleted:
                    logger.info("notified_shipments: вычищено %d старых записей", deleted)
            except Exception as e:
                logger.warning("prune_notified_shipments failed: %s", e)
        if _cycle % _pool_every == 0:
            try:
                stats = await asyncio.to_thread(get_pool_stats)
                if stats:
                    logger.info(
                        "pg_pool: used=%d/free=%d (max=%d, util=%.1f%%)",
                        stats.get("used", 0),
                        stats.get("free", 0),
                        stats.get("max", 0),
                        stats.get("util_pct", 0.0),
                    )
                    util = float(stats.get("util_pct", 0.0))
                    if util >= 80.0 and not _pool_alert_armed:
                        _pool_alert_armed = True
                        logger.warning(
                            "pg_pool: загрузка %.1f%% (used=%d/max=%d) — близко к "
                            "исчерпанию, увеличь PG_POOL_MAX или ищи утечку коннектов",
                            util,
                            stats.get("used", 0),
                            stats.get("max", 0),
                        )
                    elif util < 60.0 and _pool_alert_armed:
                        _pool_alert_armed = False
                        logger.info("pg_pool: загрузка вернулась к норме (%.1f%%)", util)
            except Exception as e:
                logger.debug("pool stats failed: %s", e)
        try:
            shipments = await get_shipments(last_check)
            last_check = datetime.now()

            if not shipments:
                continue

            recipients = get_notify_recipients()
            if not recipients:
                logger.warning("Нет получателей для уведомлений об отгрузках")
                continue

            for s in shipments[:5]:
                demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
                if not demand_id:
                    continue
                await notify_new_shipment(demand_id, shipment=s, recipients=recipients)

        except Exception as e:
            logger.error("Ошибка мониторинга: %s", e)
