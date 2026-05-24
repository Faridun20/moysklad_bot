"""
Тесты tasks/run_backup.py — backup БД в Telegram канал.

Покрываем:
  * _create_backup_sqlite: gzip-валидный файл, содержимое читается обратно
  * main без BACKUP_TG_CHAT_ID → rc=1
  * main с невалидным chat_id → rc=1
  * main с SQLite: создаёт backup, вызывает send_document (mock)
  * Размер > TG_MAX_FILE_SIZE_BYTES → rc=1 с error

Не тестируем pg_dump (нет Postgres в CI) и сам HTTP upload в Telegram
(только что send_document вызвался с правильными аргументами).
"""

import asyncio
import gzip
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def reset_backup_module(monkeypatch):
    """Сбросить env переменные между тестами, иначе утечка между ними."""
    monkeypatch.delenv("BACKUP_TG_CHAT_ID", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    # Telegram-токен заглушка для config
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token")
    monkeypatch.setenv("MS_TOKEN", "fake-ms")


# ─── _create_backup_sqlite ───────────────────────────────────────────────────


def test_create_backup_sqlite_produces_valid_gzip(tmp_path):
    from tasks.run_backup import _create_backup_sqlite

    # Создаём настоящую SQLite БД с одной записью
    db = tmp_path / "src.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO t (name) VALUES (?)", ("test-row",))
    conn.commit()
    conn.close()

    out = tmp_path / "backup.sql.gz"
    size = _create_backup_sqlite(str(db), out)

    assert out.exists()
    assert size > 0

    # gzip-валидность: открыть и прочесть
    with gzip.open(str(out), "rb") as f:
        raw = f.read()
    # SQLite файл начинается с "SQLite format 3\x00"
    assert raw.startswith(b"SQLite format 3")
    # Наш test-row внутри
    assert b"test-row" in raw


def test_create_backup_sqlite_raises_on_missing_file(tmp_path):
    from tasks.run_backup import _create_backup_sqlite

    with pytest.raises(RuntimeError, match="не найден"):
        _create_backup_sqlite(str(tmp_path / "nonexistent.db"), tmp_path / "out.gz")


# ─── main flow ───────────────────────────────────────────────────────────────


def test_main_fails_without_chat_id(reset_backup_module, caplog):
    import logging

    caplog.set_level(logging.ERROR, logger="backup")
    from tasks.run_backup import main

    rc = asyncio.run(main())
    assert rc == 1
    assert any("BACKUP_TG_CHAT_ID не задан" in r.message for r in caplog.records)


def test_main_fails_with_non_numeric_chat_id(reset_backup_module, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("BACKUP_TG_CHAT_ID", "not-a-number")
    monkeypatch.setenv("DB_PATH", "/tmp/whatever.db")
    caplog.set_level(logging.ERROR, logger="backup")
    from tasks.run_backup import main

    rc = asyncio.run(main())
    assert rc == 1
    assert any("должен быть целым числом" in r.message for r in caplog.records)


def test_main_fails_without_db_path_or_url(reset_backup_module, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("BACKUP_TG_CHAT_ID", "-100123456789")
    caplog.set_level(logging.ERROR, logger="backup")
    from tasks.run_backup import main

    rc = asyncio.run(main())
    assert rc == 1
    assert any("DATABASE_URL" in r.message and "DB_PATH" in r.message for r in caplog.records)


def test_main_sqlite_happy_path(reset_backup_module, monkeypatch, tmp_path):
    """SQLite backup + успешный mock upload в Telegram."""
    # Создаём тестовую БД
    db = tmp_path / "live.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, comment TEXT)")
    conn.execute("INSERT INTO orders (comment) VALUES ('test order #1')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("BACKUP_TG_CHAT_ID", "-100123456789")
    monkeypatch.setenv("DB_PATH", str(db))

    # Мокаем upload — не идём в Telegram
    captured = {}

    async def fake_upload(token, chat_id, file_path, caption):
        # Filesystem-вызов внутри async — здесь OK (test mock, не hot path).
        # ASYNC240 рекомендует trio.Path/anyio.path для прод-кода с асинхронным
        # I/O; для тестов overkill.
        captured["token"] = token
        captured["chat_id"] = chat_id
        captured["file_path"] = Path(file_path)
        captured["caption"] = caption
        captured["file_size"] = Path(file_path).stat().st_size  # noqa: ASYNC240

    monkeypatch.setattr("tasks.run_backup._upload_to_telegram", fake_upload)

    from tasks.run_backup import main

    rc = asyncio.run(main())
    assert rc == 0
    # Проверки upload-аргументов
    assert captured["chat_id"] == -100123456789
    # Не проверяем точное значение токена — config мог быть закэширован
    # между тестами с другим значением. Достаточно что не пустой и валидной формы.
    assert captured["token"] and ":" in captured["token"]
    assert captured["file_path"].name.startswith("moysklad-bot-sqlite-")
    assert captured["file_path"].name.endswith(".sql.gz")
    assert captured["file_size"] > 0
    assert "Backup МойСклад-бота" in captured["caption"]
    assert "Тип: sqlite" in captured["caption"]


def test_main_rejects_oversized_file(reset_backup_module, monkeypatch, tmp_path):
    """Файл > 50 MB → rc=1 с error (не пытаемся upload — точно упадёт)."""
    db = tmp_path / "big.db"
    db.write_bytes(b"x")  # маленький файл, мокаем размер

    monkeypatch.setenv("BACKUP_TG_CHAT_ID", "-100123456789")
    monkeypatch.setenv("DB_PATH", str(db))

    # Мокаем _create_backup_sqlite чтобы он вернул «огромный» размер
    def big_backup(db_path, out_path):
        Path(out_path).write_bytes(b"fake-content")
        return 100 * 1024 * 1024  # 100 MB

    monkeypatch.setattr("tasks.run_backup._create_backup_sqlite", big_backup)

    # Mock upload — он не должен вызваться
    upload_called = []

    async def fake_upload(*a, **kw):
        upload_called.append(True)

    monkeypatch.setattr("tasks.run_backup._upload_to_telegram", fake_upload)

    from tasks.run_backup import main

    rc = asyncio.run(main())
    assert rc == 1
    assert upload_called == []  # upload не вызвался


def test_main_warns_at_size_threshold(reset_backup_module, monkeypatch, tmp_path, caplog):
    """Файл 40+ MB → WARNING, но upload идёт (не fatal)."""
    import logging

    db = tmp_path / "med.db"
    db.write_bytes(b"x")

    monkeypatch.setenv("BACKUP_TG_CHAT_ID", "-100123456789")
    monkeypatch.setenv("DB_PATH", str(db))

    def medium_backup(db_path, out_path):
        Path(out_path).write_bytes(b"x")
        return 45 * 1024 * 1024  # 45 MB

    monkeypatch.setattr("tasks.run_backup._create_backup_sqlite", medium_backup)

    upload_called = []

    async def fake_upload(*a, **kw):
        upload_called.append(True)

    monkeypatch.setattr("tasks.run_backup._upload_to_telegram", fake_upload)

    caplog.set_level(logging.WARNING, logger="backup")
    from tasks.run_backup import main

    rc = asyncio.run(main())
    assert rc == 0  # warning не fatal
    assert upload_called == [True]
    assert any("близок к Telegram limit" in r.message for r in caplog.records)
