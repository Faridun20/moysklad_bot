"""
Pytest fixtures.

Что важно про тесты в этом проекте:
- Используем SQLite (DB_PATH в /tmp), Postgres в CI не нужен.
- Заглушаем _trigger_ms_paymentin_sync чтобы тесты не тыкались в МойСклад.
- TELEGRAM_TOKEN — заведомо фейковый, реального бота не дёргаем.
"""

import os

import pytest

# Заглушки секретов на случай запуска без env (локально / pre-commit hook):
# config.py требует TELEGRAM_TOKEN/MS_TOKEN уже на импорте, а часть тест-
# модулей импортируют services на этапе сборки — до фикстур. setdefault не
# перетирает реальные значения из CI.
os.environ.setdefault("TELEGRAM_TOKEN", "0:fake-token-for-tests")
os.environ.setdefault("MS_TOKEN", "fake-ms-token")


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """Свежая SQLite-БД на каждый тест, чтобы тесты не влияли друг на друга.

    Возвращает модуль services.database с инициализированной схемой.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # Telegram-токен заглушка — нужен для импорта config
    monkeypatch.setenv("TELEGRAM_TOKEN", "0:fake-token-for-tests")
    monkeypatch.setenv("MS_TOKEN", "fake-ms-token")

    # Перезагружаем модули чтобы перечитали env var DB_PATH
    import importlib
    import config
    import services.database as db

    importlib.reload(config)
    importlib.reload(db)

    db.init_db()
    # Прогоняем миграции тоже — на случай если CREATE TABLE отстал
    # от run_migrations (мы держим обе нотации в sync, но fixture
    # должен работать даже на «старой» схеме).
    db.run_migrations()
    # Глушим хук синхронизации с МойСклад — тесты не должны звонить в боевой
    # API. Через monkeypatch (а не прямое присваивание!), чтобы заглушка
    # СНИМАЛАСЬ после теста: иначе она протекала на весь прогон и ломала
    # тесты, которым нужна настоящая _trigger (см. test_ms_crossloop).
    monkeypatch.setattr(db, "_trigger_ms_paymentin_sync", lambda *a, **k: None)

    return db
