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
# Тег проверен на актуальность 2026-09-15 (`docker manifest inspect
# python:3.11-slim-bookworm`): 3.11.16-slim-bookworm — тот же digest, что и
# плавающий тег 3.11-slim-bookworm на эту дату, Debian 12.15 (bookworm),
# OpenSSL 3.0.20. sha256 закреплён ниже — при следующем патче обновите оба.
# digest: sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
FROM python:3.11.16-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

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
#   postgresql-client-${PG_CLIENT_VERSION} (из репозитория PGDG)
#       pg_dump для tasks/run_backup. В bookworm штатный клиент — 15, а
#       pg_dump отказывается дампить сервер новее себя («server version
#       mismatch»). run_backup тогда откатывается на Python-дамп, но тот
#       data-only: без схемы, восстановление требует `tasks.migrate`. Поэтому
#       ставим клиент из PGDG под версию сервера (postgres:16 в compose).
#       Сервер обновили — поднимите PG_CLIENT_VERSION: клиент может быть
#       новее сервера, но не старее.
#
#   cups-client
#       `lp` и `lpstat` для печати документов на офисный принтер
#       (services/printing.py). Это КЛИЕНТ: демона cupsd в контейнере нет и
#       не нужно — сервер CUPS стоит на хосте, адрес задаётся переменной
#       CUPS_SERVER (см. docker-compose.yml). ~1 МБ; без пакета печать
#       выключается сама (printing.is_available), кнопка не рисуется.
#
#   tzdata
#       Контейнер ОБЯЗАН работать в бизнес-зоне: utils.helpers.local_now()
#       возвращает datetime.now(), и в этом же кадре пишется created_at —
#       единственная согласованная интерпретация (см. его докстринг и WP-18).
#       Без tzdata glibc не разрешает имя зоны и МОЛЧА откатывается на UTC:
#       записи продолжают писаться, но «сегодня» съезжает на 5 часов.
#       Зона задаётся переменной TZ (см. docker-compose.yml).
#
#   libreoffice-writer (обязателен для юридических документов)
#       Конвертация docx → pdf (services/legal_docs.py). Именно -writer, а не
#       один libreoffice-core: без него LibreOffice не умеет открывать .docx
#       вообще и выходит с КОДОМ 0, сообщая «source file could not be loaded»
#       только в stderr — поэтому код проверяет наличие PDF, а не returncode.
#       Это ~400 МБ к образу. Собрать без юр. документов (расписки перестанут
#       формироваться, остальное работает): --build-arg WITH_LIBREOFFICE=0.
ARG WITH_LIBREOFFICE=1
ARG PG_CLIENT_VERSION=16
# Ключ PGDG через ADD: в slim-образе нет ни curl, ни gnupg, а apt принимает
# armored-ключ (.asc) в signed-by как есть.
ADD https://www.postgresql.org/media/keys/ACCC4CF8.asc /usr/share/keyrings/pgdg.asc
# apt-get upgrade -y — security-патчи Debian (openssl/libc6/libexpat1/...)
# подтягиваются на КАЖДОЙ сборке, а не только со следующим патч-тегом базового
# образа: сам базовый тег обновляем вручную (см. комментарий у FROM), а между
# такими обновлениями bookworm успевает выпустить не один security-релиз.
RUN set -eux; \
    chmod 644 /usr/share/keyrings/pgdg.asc; \
    echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get upgrade -y; \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        fonts-dejavu-core \
        fonts-liberation \
        "postgresql-client-${PG_CLIENT_VERSION}" \
        cups-client \
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

# pip/setuptools/wheel из базового образа несут собственные CVE
# (CVE-2024-6345, CVE-2025-47273 и advisory по wheel) независимо от версии
# Python — подтягиваем свежие до установки зависимостей проекта.
RUN pip install --no-cache-dir -U pip setuptools wheel

# Зависимости отдельным слоем: правка кода не пересобирает pip install.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

# Каталог для файлов, переживающих пересборку образа: логотип накладной
# (INVOICE_LOGO_PATH), шаблоны .docx (document_templates.file_path) и
# сгенерированные документы. Монтируйте сюда том.
RUN mkdir -p /app/data && chown app:app /app/data

# Версия работающего кода: её читает services/version.py — и для cache-busting
# статики, и для `/version` в боте, и для `/healthz`. В образе .git нет (см.
# .dockerignore), поэтому единственный источник — эти аргументы сборки; без них
# версия скатывается к таймстампу старта. Тот меняется на каждом рестарте:
# кэш браузера сбрасывается всем на ровном месте, а на вопрос «какой коммит
# работает» ответа по-прежнему нет.
#
# Заголовок коммита едет рядом с SHA не для красоты: восемь шестнадцатеричных
# знаков с GitHub по памяти не сверить, а «Пикеры вместо нативных меню…» —
# сверяется сразу.
#
# Проставляет их `scripts/deploy.sh`; вручную это выглядит так:
#   docker build --build-arg GIT_COMMIT_SHA="$(git rev-parse HEAD)" \
#                --build-arg GIT_COMMIT_MESSAGE="$(git log -1 --pretty=%s)" .
ARG GIT_COMMIT_SHA=""
ENV GIT_COMMIT_SHA=$GIT_COMMIT_SHA
ARG GIT_COMMIT_MESSAGE=""
ENV GIT_COMMIT_MESSAGE=$GIT_COMMIT_MESSAGE
ARG GIT_BRANCH=""
ENV GIT_BRANCH=$GIT_BRANCH

USER app

# Порт WebApp (webapp/server.start_webapp читает PORT, дефолт 8080).
# В BOT_MODE=bot порт не слушается — это нормально.
EXPOSE 8080

# HEALTHCHECK намеренно не задан в образе: /healthz существует только когда
# поднят webapp, а в BOT_MODE=bot HTTP-сервера нет и проверка вечно падала бы.
# Задавайте healthcheck для конкретного сервиса в compose.

CMD ["python", "bot.py"]
