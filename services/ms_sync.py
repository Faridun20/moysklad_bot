"""
Автоматическая привязка менеджеров Telegram к сотрудникам МойСклад.

Логика:
1. При первом /start менеджера — ищем его по имени в МойСклад
2. Если найден — привязываем по ID
3. Если не найден — создаём нового сотрудника в МойСклад
4. Сохраняем связку tg_user_id ↔ ms_employee_id в БД
"""

import logging

import aiohttp

from config import MS_TOKEN
from services.database import (
    set_moysklad_employee,
    get_moysklad_employee_id,
    add_audit_log,
    get_role,
)

logger = logging.getLogger(__name__)

MS_BASE = "https://api.moysklad.ru/api/remap/1.2"
MS_HEADERS = {
    "Authorization": f"Bearer {MS_TOKEN}",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
}


async def get_ms_employees() -> list[dict]:
    """Получить список всех сотрудников из МойСклад."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{MS_BASE}/entity/employee",
            headers=MS_HEADERS,
            params={"limit": 100},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return data.get("rows", [])


async def create_ms_employee(full_name: str, username: str) -> dict | None:
    """Создать нового сотрудника в МойСклад."""
    # Разбиваем имя на части
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
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{MS_BASE}/entity/employee",
                headers=MS_HEADERS,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                employee = await resp.json()
        logger.info("Создан сотрудник МойСклад: %s (ID: %s)", full_name, employee.get("id"))
        return employee
    except Exception as e:
        logger.error("Ошибка создания сотрудника МойСклад: %s", e)
        return None


async def sync_manager(user_id: int, full_name: str, username: str) -> dict:
    """
    Привязать менеджера к сотруднику МойСклад.
    Возвращает результат: {"status": "linked"|"created"|"failed", "ms_id": "..."}
    """
    # Уже привязан — пропускаем
    existing = get_moysklad_employee_id(user_id)
    if existing:
        return {"status": "already_linked", "ms_id": existing}

    try:
        employees = await get_ms_employees()

        # Ищем по имени (сравниваем полное имя)
        ms_employee = None
        for emp in employees:
            emp_name = emp.get("name", "").strip()
            # Проверяем совпадение имени (полное или частичное)
            if (
                full_name.lower() in emp_name.lower()
                or emp_name.lower() in full_name.lower()
            ):
                ms_employee = emp
                logger.info(
                    "Найден сотрудник МойСклад по имени: %s → %s",
                    full_name, emp_name,
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
            return {"status": "linked", "ms_id": ms_id, "ms_name": ms_employee.get("name")}

        # Не найден — создаём нового
        logger.info("Сотрудник не найден в МойСклад, создаём: %s", full_name)
        new_employee = await create_ms_employee(full_name, username)

        if new_employee:
            ms_id = new_employee["id"]
            set_moysklad_employee(user_id, ms_id, "created")
            add_audit_log(
                user_id, full_name, get_role(user_id),
                "ms_created",
                f"Создан новый сотрудник в МойСклад: {full_name} (ID: {ms_id})",
            )
            return {"status": "created", "ms_id": ms_id}

        # Не удалось создать
        set_moysklad_employee(user_id, None, "failed")
        return {"status": "failed", "ms_id": None}

    except Exception as e:
        logger.error("Ошибка синхронизации менеджера %d: %s", user_id, e)
        return {"status": "failed", "ms_id": None}


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
