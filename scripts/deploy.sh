#!/usr/bin/env sh
# Выкат на свой сервер: подтянуть код, собрать образ и пересоздать контейнеры.
#
# Существует ради одной строчки — подстановки GIT_COMMIT_SHA в сборку. В образе
# истории нет (`.dockerignore`), поэтому версия работающего кода приезжает
# ТОЛЬКО аргументом сборки. Забыли его — `/version` в боте и `/healthz` отвечают
# таймстампом старта вместо коммита, а кэш статики слетает у всех на каждом
# рестарте. Помнить такое каждый выкат нельзя, поэтому скрипт.
#
#   ./scripts/deploy.sh            # git pull + сборка + up -d
#   ./scripts/deploy.sh --no-pull  # собрать то, что уже лежит в рабочей копии
#
# Пересоздаёт ОБА контейнера (bot и webapp). Поднять один из двух технически
# можно, но тогда половина правок работает, а половина нет — ровно то
# расхождение, которое потом ловит `/version`.

set -eu

cd "$(dirname "$0")/.."

if [ "${1:-}" != "--no-pull" ]; then
  echo "→ git pull"
  git pull --ff-only
fi

GIT_COMMIT_SHA="$(git rev-parse HEAD)"
GIT_COMMIT_MESSAGE="$(git log -1 --pretty=%s)"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
export GIT_COMMIT_SHA GIT_COMMIT_MESSAGE GIT_BRANCH

echo "→ версия: $(echo "$GIT_COMMIT_SHA" | cut -c1-8) ($GIT_BRANCH) — $GIT_COMMIT_MESSAGE"

# Рабочая копия грязная — предупреждаем, но не мешаем: правка конфига прямо на
# сервере это законный сценарий, а вот молча выкатить её под чужим SHA нельзя.
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠ рабочая копия не чистая: в образ уедет НЕ ровно этот коммит"
fi

echo "→ сборка"
docker compose build

echo "→ пересоздание контейнеров"
docker compose up -d

echo "→ проверка"
docker compose ps
echo
echo "Версия у WebApp:  curl -s localhost:${WEBAPP_PORT:-8080}/healthz"
echo "Версия у бота:    /version в Telegram (админ или босс)"
