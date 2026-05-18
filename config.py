import os

try:
    from config_local import *

    if "MANAGER_IDS" not in dir():
        MANAGER_IDS = []
    if "BOSS_IDS" not in dir():
        BOSS_IDS = []
except ImportError:
    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
    MS_TOKEN = os.environ.get("MS_TOKEN", "")
    CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "300"))
    # Валюта для отображения цен в боте/WebApp (отображательная — на
    # стороне МойСклад валюта берётся из организации). Дефолт — USD.
    BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "USD")
    import tempfile
    _default_db = os.path.join(tempfile.gettempdir(), "payments.db")
    DB_PATH = os.environ.get("DB_PATH", _default_db)
    TZ_OFFSET = int(os.environ.get("TZ_OFFSET", "5"))

    def _parse_ids(key: str) -> list[int]:
        val = os.environ.get(key, "")
        return [int(x.strip()) for x in val.split(",") if x.strip().isdigit()]

    ALLOWED_USERS = _parse_ids("ALLOWED_USERS")
    ADMIN_IDS = _parse_ids("ADMIN_IDS")
    BOSS_IDS = _parse_ids("BOSS_IDS")
    MANAGER_IDS = _parse_ids("MANAGER_IDS")

    # ─── Telegram webhook (опционально) ────────────────────────────
    # Если TG_USE_WEBHOOK=1 и заданы WEBAPP_URL + TG_WEBHOOK_SECRET —
    # бот переключается с long-polling на webhook. Это снимает
    # 1-2 запроса/сек к api.telegram.org и даёт мгновенную реакцию.
    # Иначе работаем по-старому (polling) — обратной совместимости
    # хватает, чтобы выкатить deploy без правки env.
    TG_USE_WEBHOOK = os.environ.get("TG_USE_WEBHOOK", "").lower() in ("1", "true", "yes")
    TG_WEBHOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "")
    WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")

    # ─── Redis (опционально) ──────────────────────────────────────
    # Если задан REDIS_URL — FSM aiogram использует RedisStorage,
    # и черновики/состояния переживают редеплой. Без Redis
    # работает MemoryStorage (теряется при рестарте).
    REDIS_URL = os.environ.get("REDIS_URL", "")

    # ─── Режим процесса для разнесения на 2 сервиса в Railway ─────
    # all    — единый процесс: бот (polling/webhook) + WebApp + фон.
    #          Поведение по умолчанию, ничего не сломается.
    # bot    — только Telegram-loop и фоновые задачи. Без FastAPI.
    #          Удобно поставить вторым сервисом, который не подвержен
    #          падению webapp и не отдаёт публичных endpoint'ов.
    # webapp — только FastAPI (МойСклад webhook, /healthz, WebApp API).
    #          Telegram-апдейты НЕ обрабатываются (нет dispatcher'а),
    #          фоновые задачи не запускаются. Pair'ится с BOT_MODE=bot
    #          через общую БД и Redis.
    BOT_MODE = os.environ.get("BOT_MODE", "all").lower().strip()
    if BOT_MODE not in ("all", "bot", "webapp"):
        BOT_MODE = "all"
