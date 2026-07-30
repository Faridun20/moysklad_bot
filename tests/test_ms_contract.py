"""
Контракт с МойСклад: форма meta/payload собирается правильно.

Документированный класс багов (CLAUDE.md): кастомные атрибуты привязаны к
сущности (demand vs customerorder) — переиспользование meta даёт HTTP 400,
видимый только в проде. Здесь:
  - проверяем структуру _meta() в обоих модулях;
  - статически гарантируем, что demand-модуль не лезет в metadata
    customerorder и наоборот (источник «переиспила meta»);
  - реально собираем тело demand-документа (мок ТРАНСПОРТА через
    aioresponses) и проверяем типы ссылок и цену в минорных единицах.
"""

import asyncio
from pathlib import Path

from aioresponses import aioresponses

import services.ms_demand as ms_demand
import services.ms_customerorder as ms_customerorder
from services.moysklad import MS_BASE


# ─── Структура _meta() ──────────────────────────────────────────────────────


def test_meta_structure_demand():
    m = ms_demand._meta(f"{MS_BASE}/entity/store/X", "store")
    assert m == {
        "meta": {
            "href": f"{MS_BASE}/entity/store/X",
            "type": "store",
            "mediaType": "application/json",
        }
    }


def test_meta_structure_customerorder():
    m = ms_customerorder._meta(f"{MS_BASE}/entity/organization/Y", "organization")
    assert m["meta"]["type"] == "organization"
    assert m["meta"]["mediaType"] == "application/json"


# ─── Статический гард на «переиспил meta» между сущностями ───────────────────


def test_demand_module_never_queries_customerorder_metadata():
    src = Path(ms_demand.__file__).read_text(encoding="utf-8")
    assert "entity/customerorder/metadata" not in src, (
        "ms_demand тянет metadata из customerorder — это даст HTTP 400 "
        "при использовании чужих атрибутов на demand"
    )


def test_customerorder_module_never_queries_demand_metadata():
    src = Path(ms_customerorder.__file__).read_text(encoding="utf-8")
    assert "entity/demand/metadata" not in src, (
        "ms_customerorder тянет metadata из demand — meta атрибутов demand "
        "нельзя ставить на customerorder (HTTP 400)"
    )


# ─── Реальная сборка тела demand-документа ───────────────────────────────────


def _prime_ctx(monkeypatch):
    """Минимально достаточный _CTX, как после init_demand_context()."""
    monkeypatch.setitem(ms_demand._CTX, "ready", True)
    monkeypatch.setitem(
        ms_demand._CTX,
        "org_meta",
        {"href": f"{MS_BASE}/entity/organization/ORG", "type": "organization"},
    )
    monkeypatch.setitem(
        ms_demand._CTX,
        "store_meta",
        {"href": f"{MS_BASE}/entity/store/STORE", "type": "store"},
    )
    monkeypatch.setitem(
        ms_demand._CTX,
        "attribute_name_meta",
        {"href": f"{MS_BASE}/entity/demand/metadata/attributes/ATTR_NAME"},
    )
    monkeypatch.setitem(
        ms_demand._CTX,
        "attribute_uid_meta",
        {"href": f"{MS_BASE}/entity/demand/metadata/attributes/ATTR_UID"},
    )


def test_create_demand_builds_correct_payload(monkeypatch):
    _prime_ctx(monkeypatch)

    order = {"id": 42, "agent_id": "AGENT-UUID", "comment": "тест"}
    items = [
        {
            "product_name": "Товар",
            "product_href": f"{MS_BASE}/entity/product/PROD-UUID",
            "quantity": 3,
            "price_cents": 15000,  # копейки — в payload уходят как есть
        }
    ]
    co_href = f"{MS_BASE}/entity/customerorder/CO-UUID"

    captured = {}

    async def scenario():
        from services import moysklad

        moysklad._session = None
        try:
            with aioresponses() as m:

                def _cb(url, **kwargs):
                    captured["payload"] = kwargs["json"]

                m.post(
                    f"{MS_BASE}/entity/demand",
                    status=200,
                    payload={"id": "DEMAND-NEW", "name": "Заявка #42 (бот)"},
                    callback=_cb,
                )
                return await ms_demand.create_demand_from_request(
                    order,
                    items,
                    "Менеджер",
                    telegram_user_id=777,
                    customerorder_href=co_href,
                )
        finally:
            await moysklad.close_session()

    result = asyncio.run(scenario())
    assert result["ok"] is True, result
    assert result["demand_id"] == "DEMAND-NEW"

    p = captured["payload"]
    # Ссылки на сущности — с правильными type.
    assert p["organization"]["meta"]["type"] == "organization"
    assert p["store"]["meta"]["type"] == "store"
    assert p["agent"]["meta"]["type"] == "counterparty"
    assert p["agent"]["meta"]["href"].endswith("/entity/counterparty/AGENT-UUID")
    # Связь с заказом покупателя.
    assert p["customerOrder"]["meta"]["type"] == "customerorder"
    # Позиция: цена в минорных единицах (×100), ассортимент — product.
    pos = p["positions"][0]
    assert pos["price"] == 15000
    assert pos["quantity"] == 3
    assert pos["assortment"]["meta"]["type"] == "product"
    # Кастомные атрибуты demand: meta type == attributemetadata, значения проброшены.
    attr_values = {a["value"] for a in p["attributes"]}
    assert "Менеджер" in attr_values
    assert 777 in attr_values
    assert all(a["meta"]["type"] == "attributemetadata" for a in p["attributes"])


def test_create_demand_refuses_without_agent(monkeypatch):
    _prime_ctx(monkeypatch)
    order = {"id": 1, "comment": ""}  # нет agent_id
    items = [{"product_href": f"{MS_BASE}/entity/product/P", "quantity": 1, "price_cents": 100}]

    result = asyncio.run(ms_demand.create_demand_from_request(order, items, "M"))
    assert result["ok"] is False
    assert "клиент" in result["reason"].lower() or "agent" in result["reason"].lower()
