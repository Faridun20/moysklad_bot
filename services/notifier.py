"""
Фоновая задача: мониторинг новых отгрузок и уведомления
"""

import asyncio
import logging
from datetime import datetime

from aiogram import Bot

from config import ALLOWED_USERS, CHECK_INTERVAL_SEC
from services.moysklad import get_shipments, get_shipment_positions
from utils.helpers import extract_id_from_href
from utils.formatters import format_shipment

logger = logging.getLogger(__name__)


async def shipment_notifier(bot: Bot):
    """Раз в CHECK_INTERVAL_SEC проверяет новые отгрузки и рассылает уведомления."""
    last_check: datetime = datetime.utcnow()
    logger.info("Мониторинг отгрузок запущен (интервал %d с)", CHECK_INTERVAL_SEC)

    while True:
        await asyncio.sleep(CHECK_INTERVAL_SEC)
        try:
            shipments = await get_shipments(last_check)
            last_check = datetime.utcnow()

            if not shipments:
                continue

            logger.info("Новых отгрузок: %d", len(shipments))

            for s in shipments[:5]:
                demand_id = extract_id_from_href(s.get("meta", {}).get("href", ""))
                positions = []
                if demand_id:
                    try:
                        positions = await get_shipment_positions(demand_id)
                    except Exception:
                        pass

                txt = "🔔 <b>Новая отгрузка!</b>\n" + format_shipment(s, positions)

                for uid in ALLOWED_USERS or []:
                    try:
                        await bot.send_message(uid, txt, parse_mode="HTML")
                    except Exception as e:
                        logger.warning("Не удалось отправить %d: %s", uid, e)

        except Exception as e:
            logger.error("Ошибка мониторинга: %s", e)
