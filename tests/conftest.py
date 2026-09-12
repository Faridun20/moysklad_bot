"""
Pytest fixtures.

Что важно про тесты в этом проекте:
- Используем SQLite (DB_PATH в /tmp), Postgres в CI не нужен.
- TELEGRAM_TOKEN — заведомо фейковый, реального бота не дёргаем.
"""

import os

import pytest

# Заглушка секрета на случай запуска без env (локально / pre-commit hook):
# config.py требует TELEGRAM_TOKEN уже на импорте, а часть тест-модулей
# импортируют services на этапе сборки — до фикстур. setdefault не перетирает
# реальные значения из CI.
os.environ.setdefault("TELEGRAM_TOKEN", "0:fake-token-for-tests")


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

    # Перезагружаем модули чтобы перечитали env var DB_PATH
    import importlib
    import config
    import services.database as db

    importlib.reload(config)
    importlib.reload(db)

    db.init_db()
    # Склад по умолчанию. На проде его сеет `run_backfills` (через
    # `tasks/migrate`), в тестах — фикстура: без единой строки в `warehouses`
    # любая накладная отвергается «склад не найден», и половина сценариев
    # падала бы на инфраструктуре, а не на проверяемом поведении.
    db.seed_warehouses()
    return db
