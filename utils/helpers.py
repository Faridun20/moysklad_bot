"""
Вспомогательные функции — форматирование, индикаторы, даты
"""


def extract_id_from_href(href: str) -> str:
    """Извлечь UUID из конца href-ссылки МойСклад."""
    if not href:
        return ""
    href = href.split("?")[0]
    return href.rstrip("/").split("/")[-1]


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
    """Конвертировать копейки МойСклад в читаемую цену."""
    return f"{raw / 100:,.0f}"


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


from datetime import datetime, timezone, timedelta


def local_now() -> datetime:
    """Текущее время по Ташкенту (UTC+5)."""
    from config import TZ_OFFSET

    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)
