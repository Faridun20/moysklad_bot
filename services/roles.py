"""
Проверка ролей пользователей.

Роль читается из БД, но кэшируется в памяти на _ROLE_TTL секунд,
чтобы один запрос пользователя не тянул `get_role` по 3-5 раз подряд.
При смене роли вызывайте `invalidate_role(user_id)`.
"""

import time

from config import ADMIN_IDS
from services.database import get_role as _db_get_role

_ROLE_TTL = 60.0  # сек
_role_cache: dict[int, tuple[float, str]] = {}


def _cached_role(user_id: int) -> str:
    entry = _role_cache.get(user_id)
    now = time.monotonic()
    if entry is not None and now - entry[0] < _ROLE_TTL:
        return entry[1]
    role = _db_get_role(user_id)
    _role_cache[user_id] = (now, role)
    return role


def invalidate_role(user_id: int) -> None:
    """Сбросить кэш роли (вызывать после set_role/delete_user)."""
    _role_cache.pop(user_id, None)


def invalidate_all_roles() -> None:
    _role_cache.clear()


def _has_role(user_id: int, *roles: str) -> bool:
    """Админ из ADMIN_IDS всегда True. Иначе — сверка с БД через кэш.

    Замечание: 'guest' никогда не входит в список разрешённых ролей
    (это нулевые права по дизайну) — _has_role вернёт False для гостей.
    """
    if user_id in ADMIN_IDS:
        return True
    return _cached_role(user_id) in roles


def is_guest(user_id: int) -> bool:
    """Пользователь без прав. Используется в /start чтобы показать
    «обратитесь к админу» вместо обычного welcome."""
    if user_id in ADMIN_IDS:
        return False
    return _cached_role(user_id) == "guest"


# ─── Публичные предикаты ─────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return _has_role(user_id, "admin")


def is_boss(user_id: int) -> bool:
    return _has_role(user_id, "admin", "boss")


def can_view_stock(user_id: int) -> bool:
    return _has_role(user_id, "admin", "boss", "manager")


def can_view_analytics(user_id: int) -> bool:
    return _has_role(user_id, "admin", "boss", "manager")


def can_manage_payments(user_id: int) -> bool:
    """Подтверждать платежи и смотреть отчёт."""
    return _has_role(user_id, "admin", "boss")


def can_manage_users(user_id: int) -> bool:
    """Только полный админ."""
    return _has_role(user_id, "admin")


def is_manager(user_id: int) -> bool:
    # Менеджер — это именно роль manager (не admin, не boss),
    # поэтому ADMIN_IDS здесь не должен возвращать True.
    return _cached_role(user_id) == "manager"


def can_create_orders(user_id: int) -> bool:
    """Создавать заказы и заявки на отгрузку."""
    return _has_role(user_id, "admin", "boss", "manager")
