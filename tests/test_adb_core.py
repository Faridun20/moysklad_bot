"""
Stage 0 миграции на asyncpg: фундамент services.adb_core.

Тестируем на SQLite-бэкенде (aiosqlite) — без реального Postgres. Проверяем
транслятор плейсхолдеров $N→? и read/exec/transaction-примитивы. Тот же
async-код-путь работает на asyncpg в проде.
"""

import asyncio

import pytest

from services import adb_core


# ─── транслятор плейсхолдеров ───────────────────────────────────────────────────


def test_to_sqlite_basic():
    sql, params = adb_core._to_sqlite("SELECT * FROM t WHERE a=$1 AND b=$2", (10, 20))
    assert sql == "SELECT * FROM t WHERE a=? AND b=?"
    assert params == [10, 20]


def test_to_sqlite_repeated_param_expands():
    # asyncpg допускает повтор $1; sqlite получает два ? с одним значением.
    sql, params = adb_core._to_sqlite("SELECT $1 WHERE x=$1 OR y=$2", ("v", "w"))
    assert sql == "SELECT ? WHERE x=? OR y=?"
    assert params == ["v", "v", "w"]


def test_rowcount_from_status():
    assert adb_core._rowcount_from_status("UPDATE 3") == 3
    assert adb_core._rowcount_from_status("INSERT 0 1") == 1
    assert adb_core._rowcount_from_status("DELETE 2") == 2
    assert adb_core._rowcount_from_status("WAT") == -1


# ─── read/exec примитивы на aiosqlite ──────────────────────────────────────────


@pytest.fixture
def sqlite_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)  # форсим SQLite-ветку
    monkeypatch.setenv("DB_PATH", str(tmp_path / "adb.db"))
    return tmp_path


def test_execute_fetch_roundtrip(sqlite_env):
    async def scenario():
        await adb_core.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, n INTEGER)")
        rc1 = await adb_core.execute("INSERT INTO t (name, n) VALUES ($1, $2)", "a", 5)
        await adb_core.execute("INSERT INTO t (name, n) VALUES ($1, $2)", "b", 7)
        assert rc1 == 1

        rows = await adb_core.fetch("SELECT name, n FROM t ORDER BY n")
        assert rows == [{"name": "a", "n": 5}, {"name": "b", "n": 7}]

        row = await adb_core.fetchrow("SELECT name FROM t WHERE n=$1", 7)
        assert row == {"name": "b"}
        assert await adb_core.fetchrow("SELECT name FROM t WHERE n=$1", 999) is None

        total = await adb_core.fetchval("SELECT SUM(n) FROM t")
        assert total == 12

        rc = await adb_core.execute("UPDATE t SET n = n + 1 WHERE name=$1", "a")
        assert rc == 1
        assert await adb_core.fetchval("SELECT n FROM t WHERE name=$1", "a") == 6

    asyncio.run(scenario())


def test_transaction_commit_and_rollback(sqlite_env):
    async def scenario():
        await adb_core.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)")

        # commit
        async with adb_core.transaction() as tx:
            await tx.execute("INSERT INTO t (n) VALUES ($1)", 1)
            await tx.execute("INSERT INTO t (n) VALUES ($1)", 2)
            assert await tx.fetchval("SELECT COUNT(*) FROM t") == 2
        assert await adb_core.fetchval("SELECT COUNT(*) FROM t") == 2

        # rollback при исключении
        with pytest.raises(RuntimeError):
            async with adb_core.transaction() as tx:
                await tx.execute("INSERT INTO t (n) VALUES ($1)", 3)
                raise RuntimeError("boom")
        # вставка 3 откатилась — по-прежнему 2 строки
        assert await adb_core.fetchval("SELECT COUNT(*) FROM t") == 2

    asyncio.run(scenario())
