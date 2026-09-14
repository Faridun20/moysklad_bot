#!/bin/bash
# Обёртка для crontab хоста: одноразовый прогон cron-сервиса из docker-compose.yml.
#   scripts/cron.sh cron-backup
# flock — чтобы зависший прогон не наложился на следующий; лог — logs/cron-<сервис>.log
# (у одноразовых контейнеров логи иначе пропадают вместе с --rm).
set -uo pipefail

SERVICE="${1:?Использование: cron.sh <cron-сервис>}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG="$LOG_DIR/$SERVICE.log"
MAX_LOG_BYTES=$((5 * 1024 * 1024))

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

# Простая ротация без logrotate: разрастётся — оставляем хвост.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt "$MAX_LOG_BYTES" ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

{
    echo "===== $(date '+%F %T %z') старт $SERVICE"
    flock -n -E 75 "$LOG_DIR/.$SERVICE.lock" \
        docker compose --profile cron run --rm -T "$SERVICE"
    rc=$?
    [ $rc -eq 75 ] && echo "пропуск: предыдущий прогон ещё идёт"
    echo "===== $(date '+%F %T %z') конец $SERVICE rc=$rc"
} >> "$LOG" 2>&1

exit $rc
