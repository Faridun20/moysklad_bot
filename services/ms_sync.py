"""
Автоматическая привязка менеджеров Telegram к сотрудникам МойСклад.

Логика:
1. При первом /start менеджера — ищем его по имени в МойСклад.
2. Если найден — привязываем по ID.
3. Если не найден — создаём нового сотрудника в МойСклад.
4. Сохраняем связку tg_user_id ↔ ms_employee_id в БД.

Если МойСклад вернул ошибку (например, тариф не позволяет создавать
сотрудников через API, или у токена нет права), мы возвращаем её
текст в поле "reason", чтобы хендлер /start мог показать его юзеру —
раньше провал был молчаливым и пользователь узнавал об этом только
когда пытался открыть аналитику.
"""

import logging

from services.database import (
    set_moysklad_employee,
    get_moysklad_employee_id,
    add_audit_log,
    get_role,
)
from services.moysklad import get_session, MS_BASE

logger = logging.getLogger(__name__)


async def get_ms_employees() -> list[dict]:
    """Получить список всех сотрудников из МойСклад."""
    sess = await get_session()
    async with sess.get(f"{MS_BASE}/entity/employee", params={"limit": 100}) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data.get("rows", [])


async def create_ms_employee(full_name: str, username: str) -> tuple[dict | None, str]:
    """
    Создать нового сотрудника в МойСклад.
    Возвращает (employee_dict, reason). При ошибке employee=None,
    reason — текст ошибки для отображения пользователю.
    """
    parts = full_name.strip().split()
    first_name = parts[0] if parts else full_name
    last_name = parts[1] if len(parts) > 1 else ""
    middle_name = parts[2] if len(parts) > 2 else ""

    payload = {
        "firstName": first_name,
        "lastName": last_name,
        "middleName": middle_name,
        "description": f"Telegram: @{username}" if username else "Telegram пользователь",
    }

    try:
        sess = await get_session()
        async with sess.post(f"{MS_BASE}/entity/employee", json=payload) as resp:
            body_text = await resp.text()
            if resp.status >= 400:
                logger.error(
                    "MS create employee HTTP %s: %s", resp.status, body_text[:500]
                )
                return None, f"HTTP {resp.status}: {body_text[:200]}"
            try:
                employee = await resp.json(content_type=None)
            except Exception:
                return None, f"Невалидный JSON от МойСклад: {body_text[:200]}"
        logger.info(
            "Создан сотрудник МойСклад: %s (ID: %s)", full_name, employee.get("id")
        )
        return employee, ""
    except Exception as e:
        logger.exception("Ошибка создания сотрудника МойСклад")
        return None, f"{type(e).__name__}: {e}"


async def sync_manager(user_id: int, full_name: str, username: str) -> dict:
    """
    Привязать менеджера к сотруднику МойСклад.

    Возвращает dict:
        {"status": "already_linked"|"linked"|"created"|"failed",
         "ms_id": "..." | None,
         "ms_name": "..." (для linked),
         "reason": "..." (для failed)}
    """
    existing = get_moysklad_employee_id(user_id)
    if existing:
        return {"status": "already_linked", "ms_id": existing}

    try:
        employees = await get_ms_employees()
    except Exception as e:
        logger.exception("Ошибка загрузки сотрудников МойСклад")
        return {"status": "failed", "ms_id": None, "reason": f"Не удалось получить список сотрудников: {e}"}

    # Ищем по имени (полное или частичное совпадение)
    ms_employee = None
    name_lower = full_name.lower().strip()
    for emp in employees:
        emp_name = emp.get("name", "").strip().lower()
        if not emp_name:
            continue
        if name_lower in emp_name or emp_name in name_lower:
            ms_employee = emp
            logger.info(
                "Найден сотрудник МойСклад по имени: %s → %s",
                full_name, emp.get("name"),
            )
            break

    if ms_employee:
        ms_id = ms_employee["id"]
        set_moysklad_employee(user_id, ms_id, "linked")
        add_audit_log(
            user_id, full_name, get_role(user_id),
            "ms_linked",
            f"Привязан к сотруднику МойСклад: {ms_employee.get('name')} (ID: {ms_id})",
        )
        return {
            "status": "linked",
            "ms_id": ms_id,
            "ms_name": ms_employee.get("name"),
        }

    # Не найден — пробуем создать
    logger.info("Сотрудник не найден в МойСклад, создаём: %s", full_name)
    new_employee, reason = await create_ms_employee(full_name, username)

    if new_employee:
        ms_id = new_employee["id"]
        set_moysklad_employee(user_id, ms_id, "created")
        add_audit_log(
            user_id, full_name, get_role(user_id),
            "ms_created",
            f"Создан новый сотрудник в МойСклад: {full_name} (ID: {ms_id})",
        )
        return {"status": "created", "ms_id": ms_id}

    # Не удалось создать — сохраняем статус "failed" с причиной
    set_moysklad_employee(user_id, None, "failed")
    return {"status": "failed", "ms_id": None, "reason": reason or "Неизвестная ошибка"}


async def sync_all_managers(users: list[dict]) -> dict:
    """Синхронизировать всех менеджеров без привязки."""
    results = {"linked": 0, "created": 0, "failed": 0}
    for user in users:
        if user["role"] != "manager":
            continue
        result = await sync_manager(
            user["user_id"],
            user.get("full_name", ""),
            user.get("username", ""),
        )
        status = result.get("status", "failed")
        if status in ("linked", "already_linked"):
            results["linked"] += 1
        elif status == "created":
            results["created"] += 1
        else:
            results["failed"] += 1
    return results
