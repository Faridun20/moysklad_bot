"""
PR C (Tier 2.4): снапшот-рефреши вставляют пакетно (один executemany) вместо
N построчных execute. Проверяем и корректность данных, и что батч реально один.
"""

import asyncio

from services import adb_core, snapshot


def test_adb_executemany_inserts_all_rows(isolated_db):
    """Примитив executemany в adb_core: N строк одним вызовом → N в таблице."""

    async def _run():
        async with adb_core.transaction() as tx:
            await tx.execute("DELETE FROM ms_employees")
            n = await tx.executemany(
                "INSERT INTO ms_employees (ms_id, name, href, updated_at) "
                "VALUES ($1, $2, $3, $4)",
                [(f"id{i}", f"E{i}", "href", "ts") for i in range(5)],
            )
        rows = await adb_core.fetch("SELECT ms_id FROM ms_employees ORDER BY ms_id")
        return n, rows

    n, rows = asyncio.run(_run())
    assert n == 5
    assert len(rows) == 5


def test_refresh_counterparties_single_batch(isolated_db, monkeypatch):
    """refresh_counterparties: N строк МС → ровно N в снапшоте, ОДИН executemany
    (а не N построчных insert'ов)."""
    n_rows = 7

    async def fake_ms_get(path, params=None):
        # одна страница (< limit=1000) → _fetch_all завершает цикл сразу
        return {"rows": [{"id": f"cp{i}", "name": f"C{i}", "phone": ""} for i in range(n_rows)]}

    monkeypatch.setattr(snapshot, "ms_get", fake_ms_get)

    # Шпион на executemany: считаем вызовы и суммарные строки.
    calls = {"many": 0, "rows": 0}
    orig = adb_core._SqliteTxn.executemany

    async def spy(self, q, rows):
        calls["many"] += 1
        calls["rows"] += len(rows)
        return await orig(self, q, rows)

    monkeypatch.setattr(adb_core._SqliteTxn, "executemany", spy)

    inserted = asyncio.run(snapshot.refresh_counterparties())

    assert inserted == n_rows
    assert calls["many"] == 1  # ровно один батч, не N построчных вызовов
    assert calls["rows"] == n_rows
    total = asyncio.run(adb_core.fetchval("SELECT COUNT(*) FROM ms_counterparties"))
    assert total == n_rows
