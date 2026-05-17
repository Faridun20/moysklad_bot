"""
Вспомогательные функции — форматирование, индикаторы, даты
"""


def extract_id_from_href(href: str) -> str:
    """Извлечь UUID из конца href-ссылки МойСклад."""
    if not href:
        return ""
    href = href.split("?")[0]
    return href.rstrip("/").split("/")[-1]


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
