"""
Проверка подписи Telegram WebApp initData.
Telegram подписывает данные пользователя — нужно убедиться что они настоящие.

Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qsl

from config import TELEGRAM_TOKEN

logger = logging.getLogger(__name__)


def verify_init_data(init_data: str) -> dict | None:
    """
    Проверить подпись initData.
    Возвращает словарь с данными пользователя если всё ок, иначе None.
    """
    if not init_data:
        return None

    try:
        # Парсим query string
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        # Собираем строку для проверки подписи
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )

        # Секретный ключ = HMAC-SHA256(token, "WebAppData")
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=TELEGRAM_TOKEN.encode(),
            digestmod=hashlib.sha256,
        ).digest()

        # Вычисляем ожидаемый hash
        expected_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            logger.warning("Неверная подпись initData")
            return None

        # Подпись валидна — парсим данные пользователя
        user_str = parsed.get("user", "")
        if not user_str:
            return None
        user = json.loads(user_str)
        return user

    except Exception as e:
        logger.warning("Ошибка проверки initData: %s", e)
        return None
