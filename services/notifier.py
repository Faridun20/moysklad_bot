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
from services.moysklad import get_shipments, get_shipment_positions
from services.database import get_all_users
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
    """Вернуть общую сессию для запросов в api.telegram.org."""
    global _tg_session
    if _tg_session is None or _tg_session.closed:
        async with _tg_session_lock:
            if _tg_session is None or _tg_session.closed:
                # limit=10 — нам не нужно больше параллельных соединений
                # к Telegram, чтобы не упереться в их per-bot rate-limit
                connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
                _tg_session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=_TG_TIMEOUT,
                )
    return _tg_session


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
) -> None:
    """Послать сообщение через Bot API без aiogram.Bot.

    Ничего не возвращаем (best-effort): для уведомлений нам важнее
    не упасть из-за единичного бага, чем дождаться ответа. Логируем,
    если что — выгружаем в Railway.
    """
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        sess = await get_tg_session()
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with sess.post(url, json=payload) as resp:
            if resp.status >= 400:
                body = await resp.text()
                logger.warning(
                    "tg sendMessage %d → %s: %s",
                    chat_id, resp.status, body[:200],
                )
    except Exception as e:
        logger.warning("tg sendMessage %d failed: %s", chat_id, e)

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
        recipients = [u["user_id"] for u in users if u["role"] in ("admin", "boss")]
        if recipients:
            return recipients
    except Exception as e:
        logger.warning("Не удалось получить список получателей из БД: %s", e)

    # Фолбэк — берём из config если БД недоступна
    try:
        from config import ADMIN_IDS, BOSS_IDS

        return list(set(ADMIN_IDS + BOSS_IDS))
    except Exception:
        return []


async def aget_notify_recipients() -> list[int]:
    """Async-версия для webapp endpoint'ов: SQL уходит в thread pool,
    event loop остаётся свободным для других одновременных запросов."""
    return await asyncio.to_thread(get_notify_recipients)


async def send_to_recipients(bot: Bot, text: str, recipients: list[int]):
    """Разослать сообщение списку получателей."""
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось отправить %d: %s", uid, e)


async def shipment_notifier(bot: Bot):
    """Раз в CHECK_INTERVAL_SEC проверяет новые отгрузки и рассылает уведомления."""
    last_check: datetime = datetime.now()
    logger.info("Мониторинг отгрузок запущен (интервал %s с)", CHECK_INTERVAL_SEC)

    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        try:
            shipments = await get_shipments(last_check)
            last_check = datetime.now()

            if not shipments:
                continue

            logger.info("Новых отгрузок: %d", len(shipments))

            # Получаем актуальный список получателей при каждой проверке
            # (чтобы учитывать новых пользователей без перезапуска)
            recipients = get_notify_recipients()
            if not recipients:
                logger.warning("Нет получателей для уведомлений об отгрузках")
                continue

            logger.info("Получатели уведомлений: %s", recipients)

            for s in shipments[:5]:
                demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
                positions = []
                if demand_id:
                    try:
                        positions = await get_shipment_positions(demand_id)
                    except Exception:
                        pass

                txt = f"{DIV}\n" f"🔔 <b>Новая отгрузка!</b>\n\n" + format_shipment(
                    s, positions
                )
                await send_to_recipients(bot, txt, recipients)

        except Exception as e:
            logger.error("Ошибка мониторинга: %s", e)
