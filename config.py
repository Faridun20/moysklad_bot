# config.py — настройки бота
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
MS_TOKEN = os.environ.get("MS_TOKEN", "")
ALLOWED_USERS = [int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x]
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))