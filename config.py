import os

try:
    from config_local import *
    if 'MANAGER_IDS' not in dir():
        MANAGER_IDS = []
    if 'BOSS_IDS' not in dir():
        BOSS_IDS = []
except ImportError:
    TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
    MS_TOKEN           = os.environ.get("MS_TOKEN", "")
    CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))
    DB_PATH            = os.environ.get("DB_PATH", "payments.db")
    TZ_OFFSET          = int(os.environ.get("TZ_OFFSET", "5"))

    def _parse_ids(key: str) -> list[int]:
        val = os.environ.get(key, "")
        return [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]

    ALLOWED_USERS = _parse_ids("ALLOWED_USERS")
    ADMIN_IDS     = _parse_ids("ADMIN_IDS")
    BOSS_IDS      = _parse_ids("BOSS_IDS")
    MANAGER_IDS   = _parse_ids("MANAGER_IDS")
