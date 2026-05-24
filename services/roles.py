"""
Проверка ролей пользователей.

Роль читается из БД, но кэшируется в памяти на _ROLE_TTL секунд,
чтобы один запрос пользователя не тянул `get_role` по 3-5 раз подряд.
При смене роли вызывайте `invalidate_role(user_id)`.

PR #44 (tech debt #3c): добавлен per-user permission override через
`has_permission(uid, code)`. Существующие is_boss/can_* предикаты
оставлены как есть (backward compat) — они продолжат работать через
кэш ролей. Постепенная миграция call-сайтов на has_permission —
отдельный PR; foundation готова сейчас.
"""

import time

from config import ADMIN_IDS
from services.database import (
    VALID_ROLES,
    get_role as _db_get_role,
    get_user_permission_overrides as _db_get_overrides,
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


# ─── Permissions: per-user overrides (PR #44 / tech debt #3c) ────────────────
#
# Каталог известных прав. Имя — стабильный код (snake_case без префикса),
# должно совпадать с тем, что записываем в user_permissions.permission_code.
# Описание — для UI в /api/permissions/list.
#
# ROLE_DEFAULTS: dict[role → set permission_codes]. То что роль имеет «по
# дефолту» (зеркало текущих is_*/can_* предикатов). admin неявно имеет ВСЕ
# права; для остальных перечисляем явно.

PERMISSION_CODES: dict[str, str] = {
    "view_stock": "Видеть склад/каталог товаров",
    "view_analytics": "Видеть аналитику продаж",
    "create_orders": "Создавать заказы и заявки на отгрузку",
    "manage_payments": "Подтверждать платежи, отчёт по платежам",
    "manage_users": "Управлять пользователями и ролями",
    "change_settings": "Менять системные настройки",
    "confirm_deposit": "Подтверждать/отклонять сдачу налички",
    "confirm_shipment": "Подтверждать отгрузку (approved→shipped)",
    "create_return": "Оформлять возврат",
    "confirm_return": "Финальное подтверждение возврата",
    "change_credit_limit": "Менять кредитный лимит контрагента",
    "approve_orders": "Approve/reject pending-заявки (босс)",
}

# Дефолты по роли. admin не перечисляем — он получает всё через is_admin().
ROLE_DEFAULTS: dict[str, frozenset[str]] = {
    "boss": frozenset(PERMISSION_CODES.keys()) - {"manage_users", "change_settings"},
    "manager": frozenset(
        {"view_stock", "view_analytics", "create_orders", "create_return"}
    ),
    "bookkeeper": frozenset({"confirm_deposit", "manage_payments"}),
    "warehouse_keeper": frozenset(
        {"confirm_shipment", "create_return", "confirm_return"}
    ),
    "guest": frozenset(),
}


_PERM_TTL = 60.0  # сек, симметрично _ROLE_TTL
_perm_cache: dict[int, tuple[float, dict[str, bool]]] = {}


def _cached_overrides(user_id: int) -> dict[str, bool]:
    entry = _perm_cache.get(user_id)
    now = time.monotonic()
    if entry is not None and now - entry[0] < _PERM_TTL:
        return entry[1]
    overrides = _db_get_overrides(user_id)
    _perm_cache[user_id] = (now, overrides)
    return overrides


def invalidate_user_permissions(user_id: int) -> None:
    """Сбросить кэш overrides (вызывать после grant/revoke)."""
    _perm_cache.pop(user_id, None)


def invalidate_all_user_permissions() -> None:
    _perm_cache.clear()


def has_permission(user_id: int, permission_code: str) -> bool:
    """Универсальная проверка права: user-override → role-default → False.

    Логика:
      1. Если user_id в ADMIN_IDS — True (admin неубиваем env-переменной).
      2. Если в user_permissions есть запись для (user_id, code) — берём её.
      3. Иначе — есть ли code в ROLE_DEFAULTS[user.role].
      4. Дефолт — False.

    Backward compat: существующие is_boss/can_* функции продолжают работать
    через `_cached_role` без обращения к этой системе. Постепенная
    миграция call-сайтов на has_permission — отдельная задача.
    """
    if not permission_code:
        return False
    if permission_code not in PERMISSION_CODES:
        # Неизвестный код — скорее всего опечатка в caller'е. Логировать
        # не будем (потенциально hot path), просто denied.
        return False
    if user_id in ADMIN_IDS:
        return True
    overrides = _cached_overrides(user_id)
    if permission_code in overrides:
        return overrides[permission_code]
    role = _cached_role(user_id)
    if role == "admin":
        return True
    return permission_code in ROLE_DEFAULTS.get(role, frozenset())


def list_permissions_for_user(user_id: int) -> dict[str, dict]:
    """Развёрнутый отчёт прав юзера для admin-UI.

    Возвращает {code: {description, granted: bool, source: 'override'|'role'|'admin'|'default'}}.
    """
    role = _cached_role(user_id)
    overrides = _cached_overrides(user_id)
    is_global_admin = user_id in ADMIN_IDS
    out: dict[str, dict] = {}
    for code, desc in PERMISSION_CODES.items():
        if is_global_admin or role == "admin":
            granted, source = True, "admin"
        elif code in overrides:
            granted, source = overrides[code], "override"
        elif code in ROLE_DEFAULTS.get(role, frozenset()):
            granted, source = True, "role"
        else:
            granted, source = False, "default"
        out[code] = {"description": desc, "granted": granted, "source": source}
    return out


def grant_permission(user_id: int, permission_code: str, granted_by: int) -> bool:
    """Явно дать (granted=True) право юзеру. Возвращает True если код валидный.

    Audit-log запись пишет caller (webapp/handler) — у нас тут нет
    контекста имени admin'а и его роли. Кэш инвалидируется автоматом.
    """
    if permission_code not in PERMISSION_CODES:
        return False
    from services.database import set_user_permission_override

    set_user_permission_override(user_id, permission_code, True, granted_by)
    invalidate_user_permissions(user_id)
    return True


def revoke_permission(user_id: int, permission_code: str, revoked_by: int) -> bool:
    """Явно отозвать право у юзера (даже если роль его имеет по дефолту).

    granted=0 — это explicit revoke, отличается от «нет записи» (last fallback
    к role-default).
    """
    if permission_code not in PERMISSION_CODES:
        return False
    from services.database import set_user_permission_override

    set_user_permission_override(user_id, permission_code, False, revoked_by)
    invalidate_user_permissions(user_id)
    return True


def reset_permission(user_id: int, permission_code: str, reset_by: int | None = None) -> bool:
    """Удалить override — юзер вернётся к role-default'у.

    `reset_by` принят для симметрии с grant/revoke (admin-actor); не
    используется внутри (delete не пишет updated_by), audit-log дёргает
    caller через add_audit_log.
    """
    if permission_code not in PERMISSION_CODES:
        return False
    from services.database import clear_user_permission_override

    deleted = clear_user_permission_override(user_id, permission_code)
    invalidate_user_permissions(user_id)
    return deleted
