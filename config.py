import os

try:
    from config_local import *

    if "MANAGER_IDS" not in dir():
        MANAGER_IDS = []
    if "BOSS_IDS" not in dir():
        BOSS_IDS = []
    if "ALLOWED_CURRENCIES" not in dir():
        ALLOWED_CURRENCIES = ("USD", "UZS", "RUB", "EUR")
except ImportError:
    TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
    MS_TOKEN = os.environ.get("MS_TOKEN", "").strip()
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

    # ─── Безопасность ──────────────────────────────────────────────
    # LEGACY_OPEN_BOT=1 — пускать любого нового юзера как 'manager'
    # когда ALLOWED_USERS пуст. По умолчанию ВЫКЛЮЧЕНО: новые юзеры
    # получают роль 'guest' (нулевые права), админ повышает вручную
    # через /addrole.
    #
    # Раньше при пустом ALLOWED_USERS все новички автоматически
    # становились manager'ами. Это значило: достаточно узнать @username
    # бота → /start → ты внутри с почти полными правами. Найдено в
    # security-аудите (см. SECURITY.md C1).
    LEGACY_OPEN_BOT = os.environ.get("LEGACY_OPEN_BOT", "").lower() in ("1", "true", "yes")

    # ─── UI ────────────────────────────────────────────────────────
    # Размер страницы в пагинаторах: каталог товаров, список заказов,
    # история платежей. Раньше был раскопирован в 3 местах (handlers/
    # stock, utils/formatters, utils/keyboards) — теперь единая точка.
    PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "10"))

    # Список поддерживаемых валют — единственный источник истины.
    # Раньше был продублирован в handlers/payments, handlers/orders
    # и дважды в webapp/server как inline-литерал. Теперь один список.
    ALLOWED_CURRENCIES = ("USD", "UZS", "RUB", "EUR")

    # ─── Расписанные отчёты ────────────────────────────────────────
    # 1 (default) — daily/weekly/monthly работают внутри бот-процесса
    #               через asyncio.sleep-циклы. Удобно для one-process
    #               сетапа, но отчёт пропускается, если бот рестартанул
    #               ровно в момент срабатывания.
    # 0           — отчёты внутри бота отключены. Запускай их через
    #               Railway Cron Jobs: python -m tasks.run_report {daily|weekly|monthly}.
    #               Так надёжнее: cron не зависит от состояния бота.
    ENABLE_SCHEDULED_REPORTS = os.environ.get(
        "ENABLE_SCHEDULED_REPORTS", "1",
    ).lower() not in ("0", "false", "no")
