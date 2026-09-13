#!/usr/bin/env python3
"""Правит docker-compose.yml: убирает локальный postgres, подключает
migrate/bot/webapp/cron-* к внешней сети backend, где уже живёт реальный
контейнер postgres с продовыми данными.

Каждая правка ищет точный, уникальный кусок текста — если он не найден
(например, файл уже отличается от ожидаемого), скрипт останавливается
с понятной ошибкой вместо того, чтобы молча испортить файл.
"""
import sys
from pathlib import Path

EDITS = [
    (
        "  depends_on:\n"
        "    postgres: {condition: service_healthy}\n"
        "    redis: {condition: service_healthy}\n"
        "  restart: unless-stopped\n"
        "\n"
        "services:\n",
        "  depends_on:\n"
        "    redis: {condition: service_healthy}\n"
        "  restart: unless-stopped\n"
        "  networks:\n"
        "    - backend\n"
        "\n"
        "services:\n",
    ),
    (
        "  # ─── Хранилища ────────────────────────────────────────────────────────────\n"
        "  postgres:\n"
        "    image: postgres:16-alpine\n"
        "    environment:\n"
        "      POSTGRES_USER: ${POSTGRES_USER:-bot}\n"
        "      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD обязателен}\n"
        "      POSTGRES_DB: ${POSTGRES_DB:-moysklad_bot}\n"
        "      TZ: ${TZ:-Asia/Tashkent}\n"
        "    volumes:\n"
        "      - pgdata:/var/lib/postgresql/data\n"
        "    healthcheck:\n"
        "      # Без healthcheck migrate стартует раньше, чем Postgres примет коннекты,\n"
        "      # и падает на первом же запросе.\n"
        "      test: [\"CMD-SHELL\", \"pg_isready -U ${POSTGRES_USER:-bot} -d ${POSTGRES_DB:-moysklad_bot}\"]\n"
        "      interval: 5s\n"
        "      timeout: 5s\n"
        "      retries: 10\n"
        "    restart: unless-stopped\n"
        "    # Наружу не публикуем: к БД ходят только контейнеры этой сети. Нужен\n"
        "    # доступ с хоста для psql — раскомментируйте, но лучше через\n"
        "    # `docker compose exec postgres psql`.\n"
        "    # ports: [\"5432:5432\"]\n"
        "\n"
        "  redis:\n",
        "  redis:\n",
    ),
    (
        "    depends_on:\n"
        "      postgres: {condition: service_healthy}\n"
        "      redis: {condition: service_healthy}\n"
        "      migrate: {condition: service_completed_successfully}\n"
        "\n"
        "  webapp:\n",
        "    depends_on:\n"
        "      redis: {condition: service_healthy}\n"
        "      migrate: {condition: service_completed_successfully}\n"
        "\n"
        "  webapp:\n",
    ),
    (
        "    depends_on:\n"
        "      postgres: {condition: service_healthy}\n"
        "      redis: {condition: service_healthy}\n"
        "      migrate: {condition: service_completed_successfully}\n"
        "    ports:\n",
        "    depends_on:\n"
        "      redis: {condition: service_healthy}\n"
        "      migrate: {condition: service_completed_successfully}\n"
        "    ports:\n",
    ),
    (
        "volumes:\n"
        "  pgdata:\n"
        "  redisdata:\n"
        "  appdata:\n",
        "volumes:\n"
        "  redisdata:\n"
        "  appdata:\n"
        "\n"
        "networks:\n"
        "  backend:\n"
        "    external: true\n",
    ),
]


def main() -> None:
    path = Path("docker-compose.yml")
    if not path.is_file():
        print("ОШИБКА: docker-compose.yml не найден в текущей директории.")
        print("Запусти скрипт из корня проекта (там же, где .git).")
        sys.exit(1)

    text = path.read_text(encoding="utf-8")
    original = text

    for i, (old, new) in enumerate(EDITS, start=1):
        count = text.count(old)
        if count == 0:
            print(f"ОШИБКА в правке {i}: искомый текст не найден.")
            print("Файл, видимо, уже отличается от ожидаемого — правь вручную.")
            sys.exit(1)
        if count > 1:
            print(f"ОШИБКА в правке {i}: искомый текст встречается {count} раз, "
                  f"а должен быть уникален. Останавливаюсь, чтобы не сломать файл.")
            sys.exit(1)
        text = text.replace(old, new)

    if text == original:
        print("Файл не изменился — что-то не так.")
        sys.exit(1)

    backup = path.with_suffix(".yml.bak")
    backup.write_text(original, encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    print(f"Готово. Бэкап оригинала: {backup}")
    print("Применено правок:", len(EDITS))


if __name__ == "__main__":
    main()
