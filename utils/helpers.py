"""
Вспомогательные функции — форматирование, индикаторы, даты
"""


def esc(s) -> str:
    """HTML-escape для строк, идущих в Telegram-сообщения с parse_mode='HTML'.

    Без этой обёртки имена клиентов, комментарии менеджеров и прочий
    пользовательский ввод попадали в HTML как есть. Симптомы:
      - Telegram падает с «can't parse entities» если в тексте появляется
        одинокий `<` или `>` (например, контрагент назван «<Не указан>»).
      - XSS-style инъекция через audit_log: менеджер пишет в комментарий
        `<a href="evil">Click</a>` — админ потом видит кликабельную ссылку.

    Совместима с `parse_mode="HTML"` Telegram: экранируем &, <, > —
    кавычки не трогаем (Telegram HTML их не парсит как разметку).
    None / non-str → пустая строка (защита от случайного None).
    """
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_id_from_href(href: str) -> str:
    """Извлечь UUID из конца href-ссылки МойСклад."""
    if not href:
        return ""
    href = href.split("?")[0]
    return href.rstrip("/").split("/")[-1]


def user_safe_error(e: Exception, context: str = "") -> str:
    """
    Сообщение об ошибке, безопасное для отправки пользователю.

    Полное исключение пишется в лог через logger.exception, юзеру
    показываем generic-текст без внутренностей (имена таблиц, MS API
    payload, пути файлов и т.п. могли утечь в `<code>{e}</code>`).
    """
    import logging

    logging.getLogger(__name__).exception("user-visible error (%s): %s", context, e)
    return "❌ Произошла внутренняя ошибка. Попробуйте позже или обратитесь к админу."


def safe_get(obj, *path, default=None):
    """
    Безопасно достать значение по цепочке ключей:
        safe_get(row, "folder", "meta", "href", default="")
    вместо row.get("folder", {}).get("meta", {}).get("href", "").
    """
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def extract_href(obj, *path) -> str:
    """Кратко: вытащить href по вложенному пути (часто `meta.href`)."""
    return safe_get(obj, *path, "meta", "href", default="") or ""


def get_folder_name(folder: dict) -> str:
    return folder.get("name", "") if folder else ""


def stock_indicator(stock: float) -> str:
    """Цветовой индикатор уровня остатка."""
    if stock >= 100:
        return "🟢"
    elif stock >= 20:
        return "🟡"
    else:
        return "🔴"


def format_date(dt_str: str) -> str:
    """2026-05-15T14:32:00 → 15.05.2026 14:32"""
    try:
        dt = dt_str[:16].replace("T", " ")
        d, t = dt.split(" ")
        y, m, day = d.split("-")
        return f"{day}.{m}.{y} {t}"
    except Exception:
        return dt_str[:16]


def format_price(raw: float) -> str:
    """Конвертировать копейки МойСклад в читаемую цену.

    Тонкий алиас services.money.format_cents — единый форматтер денег,
    чтобы конвенция отображения не расходилась по модулям."""
    from services.money import format_cents

    return format_cents(int(round(raw)), decimals=0, sep=",")


def trend_arrow(current: float, previous: float) -> str:
    """Стрелка тренда относительно предыдущего периода."""
    if previous == 0:
        return "🆕"
    diff = (current - previous) / previous * 100
    if diff >= 10:
        return f"📈 +{diff:.0f}%"
    elif diff <= -10:
        return f"📉 {diff:.0f}%"
    else:
        return f"➡️ {diff:+.0f}%"


from datetime import datetime, timedelta, UTC


def utc_now() -> datetime:
    """Текущее UTC-время как naive datetime.

    Замена deprecated `datetime.utcnow()` (которая в Python 3.12+
    кидает DeprecationWarning). Возвращаем naive (без tzinfo) — так
    же как утцnow раньше — чтобы не ломать места кода, которые
    сравнивают/форматируют naive datetimes (например МойСклад
    `moment` строки и сравнения с `created_at` из БД).
    """
    return datetime.now(UTC).replace(tzinfo=None)


def local_now() -> datetime:
    """Текущее время по локальному TZ (TZ_OFFSET часов от UTC).

    Используется когда нужна «вот сейчас по местному времени» — для
    границы «сегодня» в UI и в фильтрах персональной аналитики
    менеджера, чтобы они совпадали с `created_at` в БД (который
    записывается local-time через `now_str`)."""
    from config import TZ_OFFSET

    return utc_now() + timedelta(hours=TZ_OFFSET)


# Telegram limit одного сообщения. Используется в chunk_messages.
TELEGRAM_MESSAGE_MAX = 4000


def filter_records_by_period(records: list[dict], period: str) -> list[dict]:
    """Универсальный фильтр по полю `created_at` (ISO-строка).

    period: 'today' | 'week' | 'month' | что-то ещё (= все записи).
    Используется и в /audit, и в /log handlers — раньше каждый держал
    свою копию.
    """
    now = local_now()
    if period == "today":
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        cutoff = now - timedelta(weeks=1)
    elif period == "month":
        cutoff = now - timedelta(days=30)
    else:
        return records
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    return [r for r in records if r["created_at"] >= cutoff_str]


def chunk_messages(lines: list[str], limit: int = TELEGRAM_MESSAGE_MAX) -> list[str]:
    """Склеить строки в сообщения по limit-символов (Telegram = 4000).

    Раньше эта же логика дублировалась в audit.py:format_audit_log
    и log.py:send_log. Тут — единая реализация.
    """
    if not lines:
        return []
    messages: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            if current:
                messages.append(current)
            current = line
        else:
            current = (current + "\n" + line) if current else line
    if current:
        messages.append(current)
    return messages
