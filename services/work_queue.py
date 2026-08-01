"""
Очередь дел: что ждёт именно этого человека и в каком порядке.

Раньше главный экран показывал выручку и четыре кнопки-ярлыка, а «Требует
внимания» было списком счётчиков без порядка: пять строк, и решай сам, за что
хвататься. Порядок — это и есть ответ на вопрос «с чего начать», поэтому он
живёт ЗДЕСЬ, а не в шаблоне: в шаблоне он разъедется с ролями при первой правке.

Правила, вынесенные в этот слой намеренно:

* **Порядок — по срочности, а не по разделам.** Просроченные деньги выше того,
  что ждёт решения, а оно выше «клиент молчит»: у первого срок уже нарушен, у
  второго ещё нет, третье вообще не про сроки.
* **Роль видит только то, на что может подействовать.** Пункт, ведущий туда, где
  ручка ответит 403, — не напоминание, а тупик.
* **Каждый пункт знает, куда ведёт** (`screen`). Счётчик без адреса заставляет
  искать руками то, о чём сам же сообщил.
* **Пустая очередь — это факт, а не ошибка.** «Всё разобрано» — нормальный
  ответ, и экран обязан его показать, а не пустоту.
* **Считаем только локальную БД.** Экран открывают десятки раз в день, и запрос
  в МойСклад на каждый заход съел бы бюджет, который ужимается по календарю.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from services import adb_core
from utils.helpers import local_now

logger = logging.getLogger(__name__)

# Срочность. Порядок кортежа — это и есть порядок в очереди.
SEVERITIES = ("crit", "warn", "info")

_BOSS = ("admin", "boss")
_WITH_MANAGER = ("admin", "boss", "manager")


async def _unchecked_containers() -> int:
    """Прибывшие контейнеры, в которых остались непосчитанные позиции.

    `arrived_qty IS NULL` — «ещё не считали», а не «приехало ноль» (см.
    `services.containers`). Именно такие контейнеры и требуют человека.
    """
    value = await adb_core.fetchval(
        "SELECT COUNT(DISTINCT c.id) FROM containers c "
        "JOIN container_items i ON i.container_id = c.id "
        "WHERE c.status = 'arrived' AND i.arrived_qty IS NULL"
    )
    return int(value or 0)


async def _overdue_debts(user_id: int | None) -> int:
    """Долги, у которых срок УЖЕ прошёл.

    Порог считаем в Python от `local_now()`: `due_date` хранится в локальном
    кадре, а `NOW()` в SQL отдаёт UTC — сравнение в разных кадрах молча всегда
    ложно (CLAUDE.md).
    """
    from services.database import get_open_debts

    yesterday = (local_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = await get_open_debts(user_id=user_id, due_through=yesterday)
    return len(rows)


async def _awaiting_reply(user_id: int | None) -> int:
    """Клиенты, которые ждут ответа дольше порога (`leads.NO_REPLY_HOURS`)."""
    from services import leads

    rows = await leads.awaiting_reply(limit=200)
    if user_id is None:
        return len(rows)
    return sum(1 for r in rows if r.get("manager_id") == user_id)


async def gather(user_id: int, role: str) -> list[dict]:
    """Очередь дел для пользователя. Пустой список — «всё разобрано».

    Каждый пункт: `{key, count, title, hint, severity, screen}`. `screen` — уже
    готовый адрес для `showScreen`, включая вкладку (`money:debts`).
    """
    from services.database import count_boss_attention, get_pending_requests

    boss = role in _BOSS
    # Менеджер видит свои долги и своих клиентов; руководство — все.
    scope_uid = None if boss else user_id
    items: list[dict] = []

    def add(key, count, title, hint, severity, screen):
        if count > 0:
            items.append({
                "key": key, "count": int(count), "title": title,
                "hint": hint, "severity": severity, "screen": screen,
            })

    if role in _WITH_MANAGER:
        try:
            add("overdue_debts", await _overdue_debts(scope_uid),
                "Долги просрочены", "срок оплаты уже прошёл", "crit", "money:debts")
        except Exception:
            logger.warning("work_queue: просроченные долги не посчитаны", exc_info=True)

    if boss:
        try:
            counts = await count_boss_attention()
            pending = await get_pending_requests()
            add("requests", len(pending), "Заявки ждут решения",
                "отгрузка не поедет, пока не одобрите", "warn", "requests")
            add("payments", counts["payments"], "Платежи на подтверждение",
                "менеджер отметил оплату", "warn", "money:confirm")
            add("deposits", counts["deposits"], "Сдачи наличных",
                "деньги приняты, но не подтверждены", "warn", "money:confirm")
            add("returns", counts["returns"], "Возвраты на подтверждение",
                "товар вернулся, решение за вами", "warn", "money:confirm")
        except Exception:
            logger.warning("work_queue: счётчики руководителя не посчитаны", exc_info=True)
    else:
        # Бухгалтер подтверждает сдачи, кладовщик — возвраты. Показываем каждому
        # ровно то, что он может закрыть.
        try:
            counts = await count_boss_attention()
            if role == "bookkeeper":
                add("deposits", counts["deposits"], "Сдачи наличных",
                    "ждут вашего подтверждения", "warn", "money:confirm")
            if role == "warehouse_keeper":
                add("returns", counts["returns"], "Возвраты на подтверждение",
                    "ждут вашего решения", "warn", "money:confirm")
        except Exception:
            logger.warning("work_queue: счётчики подтверждений не посчитаны", exc_info=True)

    if role in _WITH_MANAGER:
        try:
            add("awaiting_reply", await _awaiting_reply(scope_uid),
                "Клиенты ждут ответа", "написали и не получили ответа", "warn",
                "clients:funnel")
        except Exception:
            logger.warning("work_queue: ожидающие ответа не посчитаны", exc_info=True)
        try:
            add("unchecked_containers", await _unchecked_containers(),
                "Контейнеры не сверены", "прибыли, но состав не посчитан", "info",
                "stock:containers")
        except Exception:
            logger.warning("work_queue: несверенные контейнеры не посчитаны", exc_info=True)

    # Один сбойный счётчик не должен уносить всю очередь — поэтому каждый блок
    # выше падает молча, в лог. Экран с четырьмя пунктами из пяти полезнее, чем
    # ошибка вместо всех пяти.
    items.sort(key=lambda it: (SEVERITIES.index(it["severity"]), -it["count"]))
    return items
