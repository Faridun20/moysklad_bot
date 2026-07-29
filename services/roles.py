"""
Проверка ролей пользователей.

Роль читается из БД, но кэшируется в памяти на _ROLE_TTL секунд,
чтобы один запрос пользователя не тянул `get_role` по 3-5 раз подряд.
При смене роли вызывайте `invalidate_role(user_id)`.

Права выводятся ИЗ РОЛИ — предикатами is_*/can_* ниже и списком
allowed_roles в `_authorize` каждого /api/*. Другого источника нет:
система per-user overrides (user_permissions + has_permission) удалена
в T1.6, потому что `has_permission` не вызывалась ни из одной точки
авторизации, а UI при этом рапортовал «право выдано».
"""

import time

from config import ADMIN_IDS
from services.database import (
    VALID_ROLES,
    get_role as _db_get_role,
    is_user_deactivated as _db_is_deactivated,
)

# Re-export единого whitelist ролей (определён в services.database, чтобы не
# было циклического импорта). Используется и в handlers/users для валидации.
__all__ = ["VALID_ROLES"]

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


# Публичный алиас — для прямого использования в webapp/handlers,
# когда нужна именно строка-роль (а не bool-предикат).
# Раньше webapp/server.py звал services.database.get_role напрямую,
# обходя кэш и делая отдельный SELECT на каждый API-запрос.
def cached_role(user_id: int) -> str:
    return _cached_role(user_id)


def invalidate_role(user_id: int) -> None:
    """Сбросить кэш роли (вызывать после set_role/delete_user)."""
    _role_cache.pop(user_id, None)


def invalidate_all_roles() -> None:
    _role_cache.clear()


# ─── Кэш флага деактивации ────────────────────────────────────────────────────
#
# `_authorize` в webapp проверяет деактивацию на КАЖДЫЙ /api/* запрос (R1: чтобы
# уволенный мгновенно терял доступ во всех процессах, минуя 60с-кэш ролей). Без
# кэша это некэшированный SELECT на каждый запрос — заметная латентность под
# нагрузкой. Кэшируем на короткий TTL: в процессе-инициаторе деактивация
# мгновенна (инвалидация ниже), кросс-процессно задержка ≤ _DEACT_TTL — короче
# прежнего 60с-окна ролевого пути.
_DEACT_TTL = 30.0  # сек
_deact_cache: dict[int, tuple[float, bool]] = {}


def cached_is_deactivated(user_id: int) -> bool:
    entry = _deact_cache.get(user_id)
    now = time.monotonic()
    if entry is not None and now - entry[0] < _DEACT_TTL:
        return entry[1]
    flag = bool(_db_is_deactivated(user_id))
    _deact_cache[user_id] = (now, flag)
    return flag


def invalidate_deactivated(user_id: int) -> None:
    """Сбросить кэш флага деактивации (вызывать после deactivate/reactivate)."""
    _deact_cache.pop(user_id, None)


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


# ─── IMPLEMENTATION.md §4: новые роли и права ────────────────────────────────


def is_bookkeeper(user_id: int) -> bool:
    return _cached_role(user_id) == "bookkeeper"


def is_warehouse_keeper(user_id: int) -> bool:
    return _cached_role(user_id) == "warehouse_keeper"


def can_confirm_deposit(user_id: int) -> bool:
    """Подтверждать/отклонять сдачу налички (cash deposit)."""
    return _has_role(user_id, "admin", "boss", "bookkeeper")


def can_confirm_shipment(user_id: int) -> bool:
    """Подтверждать физическую отгрузку (APPROVED→SHIPPED)."""
    return _has_role(user_id, "admin", "boss", "warehouse_keeper")


def can_create_return(user_id: int) -> bool:
    """Оформить возврат."""
    return _has_role(user_id, "admin", "boss", "warehouse_keeper", "manager")


def can_confirm_return(user_id: int) -> bool:
    """Финальное подтверждение возврата."""
    return _has_role(user_id, "admin", "boss")


def can_change_credit_limit(user_id: int) -> bool:
    return _has_role(user_id, "admin", "boss")


def can_change_settings(user_id: int) -> bool:
    return _has_role(user_id, "admin")
