"""
Тесты ранее непокрытых риск-модулей: маршрутизация MS-вебхук-событий
(ms_sync_handler) и снапшот-статистика/dirty-флаг (snapshot).

ms_sync_handler трогает деньги/заказы по событиям МойСклад — важно, чтобы
диспетчер звал правильный обработчик для пары (тип, действие) и игнорировал
прочее. Сами обработчики (DB/MS) замоканы — проверяем только роутинг.
"""

import asyncio

import services.ms_sync_handler as h

_BASE = "https://api.moysklad.ru/api/remap/1.2/entity"


def test_handle_ms_events_routes_by_type_and_action(monkeypatch):
    calls: list[tuple] = []

    async def _pd(eid):
        calls.append(("paymentin_deleted", eid))

    async def _pu(eid):
        calls.append(("paymentin_updated", eid))

    async def _cu(eid):
        calls.append(("co_updated", eid))

    async def _cd(eid):
        calls.append(("co_deleted", eid))

    monkeypatch.setattr(h, "_handle_paymentin_deleted", _pd)
    monkeypatch.setattr(h, "_handle_paymentin_updated", _pu)
    monkeypatch.setattr(h, "_handle_customerorder_updated", _cu)
    monkeypatch.setattr(h, "_handle_customerorder_deleted", _cd)

    events = [
        {"action": "DELETE", "meta": {"type": "paymentin", "href": f"{_BASE}/paymentin/P1"}},
        {"action": "UPDATE", "meta": {"type": "paymentin", "href": f"{_BASE}/paymentin/P2"}},
        {"action": "UPDATE", "meta": {"type": "customerorder", "href": f"{_BASE}/customerorder/C1"}},
        {"action": "DELETE", "meta": {"type": "customerorder", "href": f"{_BASE}/customerorder/C2"}},
        # Не обрабатываемые — должны игнорироваться:
        {"action": "CREATE", "meta": {"type": "demand", "href": f"{_BASE}/demand/D1"}},
        {"action": "CREATE", "meta": {"type": "paymentin", "href": f"{_BASE}/paymentin/P3"}},
    ]

    asyncio.run(h.handle_ms_events(events))

    assert ("paymentin_deleted", "P1") in calls
    assert ("paymentin_updated", "P2") in calls
    assert ("co_updated", "C1") in calls
    assert ("co_deleted", "C2") in calls
    assert len(calls) == 4  # demand.CREATE и paymentin.CREATE не маршрутизируются


def test_handle_ms_events_ignores_events_without_href(monkeypatch):
    calls: list[tuple] = []

    async def _pd(eid):
        calls.append(eid)

    monkeypatch.setattr(h, "_handle_paymentin_deleted", _pd)

    events = [{"action": "DELETE", "meta": {"type": "paymentin"}}]  # нет href → нет entity_id
    asyncio.run(h.handle_ms_events(events))
    assert calls == []


# ─── snapshot ────────────────────────────────────────────────────────────────


def test_snapshot_stats_on_empty_db(isolated_db):
    import services.snapshot as snap

    st = snap.stats()
    assert st["ms_products"] == 0
    assert st["ms_stock"] == 0
    assert st["meta"] == []


def test_mark_stock_dirty_sets_flag(isolated_db):
    import services.snapshot as snap

    snap._stock_dirty = False
    snap.mark_stock_dirty()
    assert snap._stock_dirty is True
