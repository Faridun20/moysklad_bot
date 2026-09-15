"""
Ручки себестоимости (`services/costing.py`) — отдельным роутером.

Почему не в `webapp/server.py`, как остальные: файл общий для нескольких
параллельных веток, и сотня строк в его середине — гарантированный конфликт
слияния. Роутер подключается ОДНОЙ строкой в конце `server.py`, а общие
помощники (`_authorize`, разбор id, имя для аудита) берутся оттуда же, чтобы
права проверялись тем же кодом. `scripts/gen_role_matrix.py` читает и этот файл.

Все ручки — только руководству (`costing.COST_ROLES`): закупочная цена и есть
себестоимость, и менеджер её не видит и не задаёт.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from services import costing

router = APIRouter()

# Литералом, а не ссылкой на costing.COST_ROLES: генератор матрицы ролей читает
# `allowed_roles` из текста файла. Совпадение с сервисом стережёт тест.
_COST_ROLES = ("admin", "boss")


def _srv() -> Any:
    # Лениво: server.py подключает этот роутер у себя в конце, и импорт на
    # уровне модуля был бы циклическим.
    from webapp import server

    return server


def _response(res: dict, *, not_found: bool = False) -> JSONResponse:
    if res.get("ok"):
        return JSONResponse(res)
    error = str(res.get("error") or "Не удалось выполнить операцию")
    code = 404 if not_found or "не найден" in error.lower() else 400
    if "выключен" in error:
        code = 409
    return JSONResponse({**res, "detail": error}, status_code=code)


@router.post("/api/costing/settings")
async def api_costing_settings(request: Request):
    """Включён ли учёт себестоимости (для экрана отчёта)."""
    data = await request.json()
    _srv()._authorize(data, allowed_roles=_COST_ROLES, rate_limit_scope="api_costing_settings",
                      rate_limit_max=60)
    return JSONResponse({
        "ok": True,
        "enabled": await costing.is_enabled(),
        "base_currency": costing.base_currency(),
        "currencies": list(costing.PURCHASE_CURRENCIES),
    })


@router.post("/api/costing/settings/set")
async def api_costing_settings_set(request: Request):
    """Включить/выключить учёт. Тот же ключ, что у учёта денег."""
    from services import async_db as adb

    server = _srv()
    data = await request.json()
    user = server._authorize(data, allowed_roles=_COST_ROLES,
                             rate_limit_scope="api_costing_settings_set", rate_limit_max=10)
    enabled = bool(data.get("enabled"))
    await costing.set_enabled(enabled, user["id"])
    await adb.add_audit_log(
        user["id"], server._actor_name(user), server.get_role(user["id"]),
        "accounting_switch", "учёт включён" if enabled else "учёт выключен",
    )
    return JSONResponse({"ok": True, "enabled": enabled})


@router.post("/api/costing/container")
async def api_costing_container(request: Request):
    """Блок «Закупка и себестоимость» карточки контейнера."""
    server = _srv()
    data = await request.json()
    server._authorize(data, allowed_roles=_COST_ROLES, rate_limit_scope="api_costing_container",
                      rate_limit_max=120)
    container_id = server._machine_id_arg(data, "container_id")
    return _response(await costing.container_card(container_id))


@router.post("/api/costing/container/save")
async def api_costing_container_save(request: Request):
    """Цены закупки по позициям, валюта и курс на дату прибытия.

    Payload: {container_id, currency, uzs_per_usd, uzs_per_unit?, rate_source?,
    prices: {"<item_id>": "12.50" | ""}}. Пустая цена — «ещё не знаем».
    """
    server = _srv()
    data = await request.json()
    user = server._authorize(data, allowed_roles=_COST_ROLES,
                             rate_limit_scope="api_costing_container_save", rate_limit_max=30)
    container_id = server._machine_id_arg(data, "container_id")
    prices = data.get("prices") or {}
    if not isinstance(prices, dict):
        raise HTTPException(status_code=400, detail="prices: ожидается объект")
    res = await costing.save_container_costing(
        container_id,
        currency=str(data.get("currency") or ""),
        uzs_per_usd=data.get("uzs_per_usd"),
        uzs_per_unit=data.get("uzs_per_unit"),
        rate_source=str(data.get("rate_source") or "manual"),
        prices=prices,
        user_id=user["id"],
        full_name=server._actor_name(user),
    )
    return _response(res)


@router.post("/api/costing/report")
async def api_costing_report(request: Request):
    """Прибыль и курсовая разница за период — тот же период, что у «Продажи → Отчёт»."""
    server = _srv()
    data = await request.json()
    server._authorize(data, allowed_roles=_COST_ROLES, rate_limit_scope="api_costing_report",
                      rate_limit_max=60)
    since, until, _prev, label = server._resolve_analytics_period(data, datetime.now())
    res = await costing.period_report(since, until)
    res["label"] = label
    return JSONResponse(res)


@router.post("/api/costing/product")
async def api_costing_product(request: Request):
    """Себестоимость товара: средняя по остатку, ручная, история партий."""
    server = _srv()
    data = await request.json()
    server._authorize(data, allowed_roles=_COST_ROLES, rate_limit_scope="api_costing_product",
                      rate_limit_max=120)
    product_id = server._machine_id_arg(data, "product_id")
    return JSONResponse(await costing.product_cost(product_id))
