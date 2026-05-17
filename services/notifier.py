"""
Фоновая задача: мониторинг новых отгрузок и уведомления
Отправляет уведомления всем пользователям с ролью admin и boss
"""

import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from config import CHECK_INTERVAL_SEC as _CHECK_INTERVAL_SEC

CHECK_INTERVAL_SEC = int(_CHECK_INTERVAL_SEC)
from services.moysklad import get_shipments, get_shipment_positions
from services.database import get_all_users
from utils.helpers import extract_id_from_href
from utils.formatters import format_shipment, DIV

logger = logging.getLogger(__name__)

# ─── Получатели уведомлений ───
def get_notify_recipients() -> list[int]:
    """
    Получить список получателей уведомлений об отгрузках:
    все пользователи с ролью admin или boss.
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
