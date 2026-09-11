# syntax=docker/dockerfile:1
#
# Образ для self-hosted развёртывания (Postgres + Docker на своём сервере).
#
# Один образ на все роли — роль выбирается переменной BOT_MODE:
#   BOT_MODE=all     бот + webapp + фоновые задачи в одном процессе
#   BOT_MODE=bot     только бот и фоновые задачи
#   BOT_MODE=webapp  только HTTP-API WebApp
# Cron-задачи (`python -m tasks.run_*`) — тот же образ, другая команда.
#
# Миграции НЕ встроены в CMD намеренно: схему разворачивает ОДИН процесс
# перед стартом сервисов, иначе bot и webapp стартуют наперегонки.
#   docker compose run --rm bot python -m tasks.migrate

# Патч-версия зафиксирована так же, как в runtime.txt (его читает Railway).
FROM python:3.11.9-slim-bookworm

# ─── Системные библиотеки ────────────────────────────────────────────────────
# Каждая — с указанием, кто её требует: иначе через полгода не вспомнить,
# что можно выкинуть.
#
#   libpango-1.0-0, libpangoft2-1.0-0
#       WeasyPrint (PDF накладных, services/invoice_pdf.py). ТОЛЬКО pango:
#       начиная с версии 53 WeasyPrint не использует cairo и gdk-pixbuf, а
#       растровые картинки (логотип) декодирует Pillow. Проверено на
#       weasyprint 70 — libcairo2 и libgdk-pixbuf-2.0-0 в процессе не
#       загружаются вовсе, ставить их незачем. Транзитивно pango тянет
#       glib, gobject, harfbuzz, fontconfig и freetype.
#
#   fonts-dejavu-core
#       Шрифт шаблона накладной (font-family: "DejaVu Sans"). Без него
#       WeasyPrint подставит что найдёт, и кириллица уедет в квадраты.
#
#   fonts-liberation
#       Метрически совместим с Times New Roman — требование юридических
#       документов. Проверено: покрывает узбекскую кириллицу (ў, қ, ғ, ҳ)
#       полностью, как и DejaVu.
#
#   postgresql-client
#       pg_dump для tasks/run_backup. ВНИМАНИЕ: в bookworm это клиент 15.
#       Если сервер Postgres новее, pg_dump откажет с «server version
#       mismatch» — run_backup это ловит и переключается на
#       version-independent дамп на чистом Python, бэкап не теряется.
#       Для полноценного pg_dump подключите репозиторий PGDG и поставьте
#       postgresql-client-<версия вашего сервера>.
#
#   tzdata
#       Контейнер ОБЯЗАН работать в бизнес-зоне: utils.helpers.local_now()
#       возвращает datetime.now(), и в этом же кадре пишется created_at —
#       единственная согласованная интерпретация (см. его докстринг и WP-18).
#       Без tzdata glibc не разрешает имя зоны и МОЛЧА откатывается на UTC:
#       записи продолжают писаться, но «сегодня» съезжает на 5 часов.
#       Зона задаётся переменной TZ (см. docker-compose.yml).
#
#   libreoffice-writer (опционально, WITH_LIBREOFFICE)
#       Конвертация docx → pdf для юридических документов. Это ~400 МБ к
#       образу, а сама функциональность ещё не написана (ждём шаблон
#       расписки). Собрать без неё: --build-arg WITH_LIBREOFFICE=0.
ARG WITH_LIBREOFFICE=1
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        fonts-dejavu-core \
        fonts-liberation \
        postgresql-client \
        tzdata \
        ca-certificates; \
    if [ "$WITH_LIBREOFFICE" = "1" ]; then \
        apt-get install -y --no-install-recommends libreoffice-writer; \
    fi; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Бизнес-зона по умолчанию. Переопределяется в compose — но пустой TZ
# означал бы UTC и разъехавшиеся сутки, поэтому дефолт осмысленный.
ENV TZ=Asia/Tashkent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Пользователь создаётся ДО копирования исходников, чтобы забрать их сразу с
# нужным владельцем через COPY --chown. Отдельный `chown -R` после COPY
# переписал бы каждый файл и удвоил размер слоя с кодом.
RUN useradd --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# Зависимости отдельным слоем: правка кода не пересобирает pip install.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

# Каталог для файлов, переживающих пересборку образа: логотип накладной
# (INVOICE_LOGO_PATH), шаблоны .docx (document_templates.file_path) и
# сгенерированные документы. Монтируйте сюда том.
RUN mkdir -p /app/data && chown app:app /app/data

# Версия статики для cache-busting. webapp/server._compute_app_version читает
# GIT_COMMIT_SHA, иначе пробует git (в образе .git нет — см. .dockerignore) и
# скатывается к таймстампу старта. Тот меняется на каждом рестарте и сбрасывает
# кэш браузера всем пользователям на ровном месте.
#   docker build --build-arg GIT_COMMIT_SHA="$(git rev-parse HEAD)" .
ARG GIT_COMMIT_SHA=""
ENV GIT_COMMIT_SHA=$GIT_COMMIT_SHA

USER app

# Порт WebApp (webapp/server.start_webapp читает PORT, дефолт 8080).
# В BOT_MODE=bot порт не слушается — это нормально.
EXPOSE 8080

# HEALTHCHECK намеренно не задан в образе: /healthz существует только когда
# поднят webapp, а в BOT_MODE=bot HTTP-сервера нет и проверка вечно падала бы.
# Задавайте healthcheck для конкретного сервиса в compose.

CMD ["python", "bot.py"]
