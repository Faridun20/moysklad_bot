"""
Проверка ролей пользователей
"""

from config import ADMIN_IDS
from services.database import get_role


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or get_role(user_id) == "admin"


def is_manager(user_id: int) -> bool:
    return get_role(user_id) in ("admin", "manager") or user_id in ADMIN_IDS


def is_employee(user_id: int) -> bool:
    """Все пользователи могут отправлять платежи."""
    return True


def can_view_stock(user_id: int) -> bool:
    return get_role(user_id) in ("admin", "manager") or user_id in ADMIN_IDS


def can_view_analytics(user_id: int) -> bool:
    return get_role(user_id) in ("admin", "manager") or user_id in ADMIN_IDS


def can_manage_payments(user_id: int) -> bool:
    return user_id in ADMIN_IDS or get_role(user_id) == "admin"
