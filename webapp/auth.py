"""
Проверка подписи Telegram WebApp initData.
Telegram подписывает данные пользователя — нужно убедиться что они настоящие.

Документация: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from config import TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

# Максимальный возраст initData — 24 часа. Перехваченный initData старше
# 24 часов считается просроченным и отвергается, защита от replay-атак.
# Telegram официально рекомендует <= 86400 секунд.
MAX_INIT_DATA_AGE = 86400


def verify_init_data(init_data: str) -> dict | None:
    """
    Проверить подпись initData.
    Возвращает словарь с данными пользователя если всё ок, иначе None.

    Проверки:
      1. Подпись HMAC-SHA256 совпадает (timing-safe сравнение)
      2. auth_date не старше MAX_INIT_DATA_AGE (защита от replay)
      3. Поле user парсится как JSON
    """
    if not init_data:
        logger.warning("verify_init_data: initData пустой — открыт не через Telegram?")
        return None

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            logger.warning("verify_init_data: нет поля hash в initData")
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

        # Секретный ключ = HMAC-SHA256(token, "WebAppData")
        secret_key = hmac.new(
            key=b"WebAppData",
            msg=TELEGRAM_TOKEN.encode(),
            digestmod=hashlib.sha256,
        ).digest()

        expected_hash = hmac.new(
            key=secret_key,
            msg=data_check_string.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            logger.warning("Неверная подпись initData")
            return None

        # Проверка свежести: defence against replay. Раньше не проверяли
        # вовсе — перехваченный (например, из логов прокси) initData
        # работал бесконечно.
        try:
            auth_date = int(parsed.get("auth_date", "0"))
        except ValueError:
            logger.warning("initData: некорректный auth_date")
            return None
        age = int(time.time()) - auth_date
        if age > MAX_INIT_DATA_AGE:
            logger.warning("initData просрочен: возраст %s сек > %s", age, MAX_INIT_DATA_AGE)
            return None

        user_str = parsed.get("user", "")
        if not user_str:
            return None
        user = json.loads(user_str)
        return user

    except Exception as e:
        logger.warning("Ошибка проверки initData: %s", e)
        return None
