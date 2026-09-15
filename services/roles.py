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

import asyncio
import logging
import time

from config import ADMIN_IDS
from services.database import (
    VALID_ROLES,
    get_role_and_deactivation as _db_role_and_deactivation,
)

# Re-export единого whitelist ролей (определён в services.database, чтобы не
# было циклического импорта). Используется и в handlers/users для валидации.
__all__ = ["VALID_ROLES"]

# ─── Один кэш на роль и деактивацию ──────────────────────────────────────────
#
# Кэш живёт в памяти КАЖДОГО процесса отдельно: bot и webapp его не делят, и
# инвалидация из одного процесса до другого не доходит. Единственная гарантия
# кросс-процессной свежести — TTL. Раньше их было два (роль 60 с, деактивация
# 30 с) и два запроса: понижение роли из бота доезжало до webapp вдвое позже
# блокировки. Теперь запись одна — (роль, деактивирован) одним SELECT'ом — и
# окно одно, 30 с. `_authorize` дёргает оба факта на каждый /api/* запрос, так
# что объединение ещё и вдвое дешевле по БД.
_AUTH_TTL = 30.0  # сек
_ROLE_TTL = _AUTH_TTL  # прежнее имя — на него ссылаются комментарии и доки
_auth_cache: dict[int, tuple[float, str, bool]] = {}


def _auth_entry(user_id: int) -> tuple[str, bool]:
    entry = _auth_cache.get(user_id)
    now = time.monotonic()
    if entry is not None and now - entry[0] < _AUTH_TTL:
        return entry[1], entry[2]
    role, deactivated = _db_role_and_deactivation(user_id)
    _auth_cache[user_id] = (now, role, deactivated)
    return role, deactivated


logger = logging.getLogger(__name__)

# За сколько секунд до истечения TTL прогрев уже обновляет запись. Без запаса
# запись 29,9 с «свежая» для прогрева, но протухает к моменту `_authorize`
# двумя миллисекундами позже — и SELECT снова идёт в потоке loop'а.
_WARM_MARGIN = 5.0


async def warm_auth_cache(user_id: int) -> None:
    """Прогреть кэш роли/деактивации В ПОТОКЕ, не блокируя event loop.

    Предикаты (`is_boss`, `cached_role`, `_authorize` в WebApp) синхронные и
    зовутся из async-кода: при промахе кэша SELECT шёл прямо в потоке loop'а —
    на это время вставали все запросы WebApp и апдейты бота, а при исчерпанном
    пуле Postgres ещё и с ожиданием коннекта. Middleware бота и WebApp зовут
    прогрев ДО хендлера, и синхронный путь дальше попадает в тёплую запись.
    Сбой прогрева не роняет запрос: синхронный путь повторит чтение сам и
    ответит своей ошибкой, как раньше.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return
    entry = _auth_cache.get(uid)
    started = time.monotonic()
    if entry is not None and started - entry[0] < _AUTH_TTL - _WARM_MARGIN:
        return
    try:
        role, deactivated = await asyncio.to_thread(_db_role_and_deactivation, uid)
    except Exception:  # noqa: BLE001 — прогрев best-effort, см. докстринг
        logger.warning("Прогрев кэша роли user_id=%s не удался", uid, exc_info=True)
        return
    # Метка — момент НАЧАЛА чтения: запись не должна жить дольше TTL от
    # момента, когда данные реально были прочитаны.
    _auth_cache[uid] = (started, role, deactivated)


def _cached_role(user_id: int) -> str:
    role, deactivated = _auth_entry(user_id)
    # Деактивированный теряет ВСЕ права (#32) — как и get_role в database.
    return "guest" if deactivated else role


# Публичный алиас — для прямого использования в webapp/handlers,
# когда нужна именно строка-роль (а не bool-предикат).
# Раньше webapp/server.py звал services.database.get_role напрямую,
# обходя кэш и делая отдельный SELECT на каждый API-запрос.
def cached_role(user_id: int) -> str:
    return _cached_role(user_id)


def invalidate_role(user_id: int) -> None:
    """Сбросить кэш роли (вызывать после set_role/delete_user)."""
    _auth_cache.pop(user_id, None)


def invalidate_all_roles() -> None:
    _auth_cache.clear()


def cached_is_deactivated(user_id: int) -> bool:
    """Флаг деактивации — из того же кэша, что и роль."""
    return _auth_entry(user_id)[1]


def invalidate_deactivated(user_id: int) -> None:
    """Сбросить кэш флага деактивации (вызывать после deactivate/reactivate).

    Запись общая с ролью, так что это то же, что invalidate_role, — оставлено
    отдельным именем: его зовёт database._invalidate_role_cache.
    """
    _auth_cache.pop(user_id, None)



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
