#!/usr/bin/env python3
"""Заменяет POSTGRES_PASSWORD на POSTGRES_PASSWORD_URLENC внутри трёх строк
DATABASE_URL в docker-compose.yml — пароль содержит '/', который ломает
парсинг postgresql:// URI, если не URL-кодирован."""
from pathlib import Path

path = Path("docker-compose.yml")
text = path.read_text(encoding="utf-8")

old = "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD обязателен}@postgres"
new = "${POSTGRES_PASSWORD_URLENC:?POSTGRES_PASSWORD_URLENC обязателен}@postgres"

count = text.count(old)
if count == 0:
    print("ОШИБКА: искомый текст не найден — файл уже другой?")
    raise SystemExit(1)

print(f"Найдено вхождений: {count} (должно быть 3 — в x-app, bot, webapp)")
text = text.replace(old, new)

backup = path.with_suffix(".yml.bak2")
backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
path.write_text(text, encoding="utf-8")
print(f"Готово. Бэкап: {backup}")
