"""
Order state machine — единый источник правил переходов и прав.

Вместо разбросанных проверок `is_boss()` + ручного UPDATE status
используется:
  - TRANSITIONS — что за чем может следовать
  - ROLE_FOR_TRANSITION — кто имеет право инициировать переход
  - validate_transition() — синхронная проверка допустимости (без DB)
  - can_transition() — проверка прав роли на конкретный переход
"""

from __future__ import annotations

# Допустимые переходы: текущий_статус → список разрешённых следующих
TRANSITIONS: dict[str, list[str]] = {
    "draft":    ["pending", "rejected"],   # отправить или отменить (удалить)
    "pending":  ["approved", "rejected"],  # решение боса
    "approved": ["shipped"],               # фиксация отгрузки
    "rejected": [],
    "shipped":  [],
}

# Какие роли могут инициировать какие переходы
_ROLE_TRANSITIONS: dict[str, set[str]] = {
    "manager": {"draft→pending"},
    "boss":    {"pending→approved", "pending→rejected"},
    "admin":   {
        "draft→pending", "pending→approved",
        "pending→rejected", "approved→shipped",
        "draft→rejected",
    },
    "guest":   set(),
}


def can_transition(order: dict, new_status: str, role: str) -> bool:
    """True если роль `role` вправе перевести заказ в `new_status`.

    Проверяет сразу две вещи:
    1. Переход допустим по TRANSITIONS (например нельзя shipped→draft)
    2. Роль авторизована для этого перехода
    """
    current = order.get("status", "")
    if new_status not in TRANSITIONS.get(current, []):
        return False
    key = f"{current}→{new_status}"
    return key in _ROLE_TRANSITIONS.get(role, set())


def validate_transition(order: dict, new_status: str) -> str | None:
    """Вернуть строку ошибки если переход недопустим, иначе None.

    Используется для pre-check ДО обращения к БД — быстрый fail-fast.
    """
    current = order.get("status", "")
    if new_status not in TRANSITIONS.get(current, []):
        return f"Переход {current!r}→{new_status!r} недопустим"
    return None
