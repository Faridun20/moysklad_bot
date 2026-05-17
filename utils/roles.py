"""
Проверка ролей пользователей
"""

from config import ADMIN_IDS
from services.database import get_role


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or get_role(user_id) == "admin"


def is_boss(user_id: int) -> bool:
    return get_role(user_id) in ("admin", "boss") or user_id in ADMIN_IDS


def can_view_stock(user_id: int) -> bool:
    return get_role(user_id) in ("admin", "boss", "manager") or user_id in ADMIN_IDS


def can_view_analytics(user_id: int) -> bool:
    return get_role(user_id) in ("admin", "boss", "manager") or user_id in ADMIN_IDS


def can_manage_payments(user_id: int) -> bool:
    """Подтверждать платежи и смотреть отчёт."""
    return get_role(user_id) in ("admin", "boss") or user_id in ADMIN_IDS


def can_manage_users(user_id: int) -> bool:
    """Только полный админ."""
    return user_id in ADMIN_IDS or get_role(user_id) == "admin"
