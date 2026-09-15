#!/usr/bin/env bash
# Локальная CI: все проверки проекта в одноразовых контейнерах на своём сервере.
#
#   scripts/local_ci.sh [опции] <каталог-с-исходниками>
#
#   scripts/local_ci.sh .                       # вся проверка текущей рабочей копии
#   scripts/local_ci.sh --skip perf .           # без perf-слоя
#   scripts/local_ci.sh --jobs lint,unit .      # только выбранные задачи
#   scripts/local_ci.sh --log /tmp/ci.log /tmp/checkout
#
# Зачем, если есть GitHub Actions: у раннера GitHub нет LibreOffice (тест
# юр. документов с настоящим soffice пропускается), нет Postgres (все
# *_postgres.py и сценарии tests/scenarios пропускаются), а системные
# библиотеки не те, что в боевом образе. Здесь тесты идут в образе, собранном
# из Dockerfile ЭТОГО коммита, и с настоящим Postgres 16 (как на проде).
#
# Задачи (все по умолчанию):
#   lint      ruff (строгий гейт + полный набор) и mypy
#   frontend  vitest (node:20, npm ci по package-lock.json)
#   unit      pytest -m "not e2e and not perf" + Postgres (TEST_PG_URL),
#             шардами параллельно; покрытие склеивается, порог как в GitHub CI
#   e2e       pytest tests/e2e -m e2e (Chromium, E2E_REQUIRED=1), шардами
#   perf      pytest tests/perf -m perf — ПОСЛЕ остальных: там нагрузочные
#             тесты с порогами масштабирования, соседи по CPU их бы шумели
#
# Исходники монтируются только на чтение и копируются внутрь контейнера (на
# tmpfs): рабочая копия не обрастает кэшами от root, а SQLite-базы тестов не
# платят за fsync overlayfs — на диске тот же набор тестов шёл вчетверо дольше.
# Контейнеры, сеть и том покрытия помечены RUN_ID и удаляются на любом выходе.
#
# Образ: moysklad-bot-ci:<хэш Dockerfile + requirements*.txt + Dockerfile.ci>.
# Поменялись зависимости — образ пересобирается, иначе берётся готовый.
#
# Код выхода: 0 — всё зелёное, 1 — упала хотя бы одна проверка,
# 2 — сломалась сама инфраструктура (docker, сборка образа, Postgres).
set -uo pipefail

usage() { sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; }

ALL_JOBS="lint frontend unit e2e perf"
JOBS="$ALL_JOBS"
SKIP=""
LOG=""
UNIT_SHARDS="${LOCAL_CI_UNIT_SHARDS:-6}"
E2E_SHARDS="${LOCAL_CI_E2E_SHARDS:-6}"
KEEP_IMAGES="${LOCAL_CI_KEEP_IMAGES:-3}"
JOB_TIMEOUT_MIN="${LOCAL_CI_JOB_TIMEOUT_MIN:-40}"
COV_FAIL_UNDER="${LOCAL_CI_COV_FAIL_UNDER:-55}"
LABEL="${LOCAL_CI_LABEL:-}"
HEAVY_TESTS_RE="${LOCAL_CI_HEAVY_TESTS_RE:-click_everything}"

while [ $# -gt 0 ]; do
    case "$1" in
        --jobs) JOBS="${2//,/ }"; shift 2 ;;
        --skip) SKIP="${2//,/ }"; shift 2 ;;
        --log) LOG="$2"; shift 2 ;;
        --unit-shards) UNIT_SHARDS="$2"; shift 2 ;;
        --e2e-shards) E2E_SHARDS="$2"; shift 2 ;;
        --label) LABEL="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Неизвестная опция: $1" >&2; usage >&2; exit 2 ;;
        *) break ;;
    esac
done
[ $# -eq 1 ] || { usage >&2; exit 2; }
SRC="$(cd "$1" 2>/dev/null && pwd)" || { echo "Нет каталога: $1" >&2; exit 2; }
[ -f "$SRC/Dockerfile" ] && [ -f "$SRC/requirements.txt" ] || { echo "$SRC — не исходники moysklad_bot" >&2; exit 2; }

want() { case " $JOBS " in *" $1 "*) case " $SKIP " in *" $1 "*) return 1 ;; esac; return 0 ;; esac; return 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CI_DOCKERFILE="$SRC/scripts/ci/Dockerfile.ci"
[ -f "$CI_DOCKERFILE" ] || CI_DOCKERFILE="$SCRIPT_DIR/ci/Dockerfile.ci"

RUN_ID="mci-$(date +%s)-$$-$RANDOM"
NET="$RUN_ID-net"
PG="$RUN_ID-pg"
COV_VOL="$RUN_ID-cov"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/local-ci.XXXXXX")"
LOG="${LOG:-${TMPDIR:-/tmp}/local-ci-$(date +%Y%m%d-%H%M%S).log}"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"
SHA="${LOCAL_CI_SHA:-$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo '')}"
T0=$(date +%s)

say() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

cleanup() {
    local ids
    # Сначала гасим фоновые задачи этого прогона, потом их контейнеры.
    jobs -p | xargs -r kill 2>/dev/null
    ids="$(docker ps -aq --filter "label=moysklad.local-ci=$RUN_ID" 2>/dev/null)"
    [ -n "$ids" ] && docker rm -f $ids >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
    docker volume rm "$COV_VOL" >/dev/null 2>&1
    rm -rf "$WORK"
}
trap cleanup EXIT
trap 'say "прервано сигналом"; exit 130' INT TERM

docker info >/dev/null 2>&1 || { say "docker недоступен"; exit 2; }

say "Локальная CI: $SRC ${SHA:+(коммит ${SHA:0:12})} ${LABEL:+— $LABEL}"
say "Задачи: $(for j in $ALL_JOBS; do want "$j" && printf '%s ' "$j"; done)· лог: $LOG"

# ─── Образ ───────────────────────────────────────────────────────────────────
HASH="$(cat "$SRC/Dockerfile" "$SRC/requirements.txt" "$SRC/requirements-dev.txt" "$CI_DOCKERFILE" \
    | sha256sum | cut -c1-16)"
IMAGE="moysklad-bot-ci:$HASH"
BASE_IMAGE="moysklad-bot-ci-base:$HASH"

build_image() {
    local ctx="$WORK/image-ctx"
    mkdir -p "$ctx"
    cp "$SRC/Dockerfile" "$SRC/requirements.txt" "$SRC/requirements-dev.txt" "$ctx/"
    cp "$CI_DOCKERFILE" "$ctx/Dockerfile.ci"
    # Контекст — только Dockerfile и зависимости: код в образ не нужен (его
    # монтируем), и правка кода не должна пересобирать образ.
    say "Собираю $IMAGE (зависимости изменились или образа ещё нет)…"
    nice -n 10 docker build -t "$BASE_IMAGE" -f "$ctx/Dockerfile" "$ctx" >> "$WORK/image.log" 2>&1 || return 1
    nice -n 10 docker build --build-arg "BASE_IMAGE=$BASE_IMAGE" -t "$IMAGE" -f "$ctx/Dockerfile.ci" "$ctx" \
        >> "$WORK/image.log" 2>&1 || return 1
    # Базовый тег не нужен: слои держит дочерний образ.
    docker image rm "$BASE_IMAGE" >/dev/null 2>&1
    # Старые образы CI — вон, кроме последних KEEP_IMAGES.
    docker images "moysklad-bot-ci" --format '{{.CreatedAt}}\t{{.Repository}}:{{.Tag}}' \
        | sort -r | awk -F'\t' -v keep="$KEEP_IMAGES" 'NR > keep {print $2}' \
        | grep -vx "$IMAGE" | xargs -r docker image rm >/dev/null 2>&1
    return 0
}

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    # Две CI одновременно (ручная и автодеплой) не собирают один образ дважды.
    exec 8>"${TMPDIR:-/tmp}/moysklad-local-ci-build.lock"
    flock 8
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        tb=$(date +%s)
        if ! build_image; then
            say "ОШИБКА: образ не собрался (хвост лога ниже)"
            tail -n 40 "$WORK/image.log" | tee -a "$LOG"
            exit 2
        fi
        say "Образ собран за $(( $(date +%s) - tb )) с"
    fi
    flock -u 8
else
    say "Образ $IMAGE уже есть"
fi

# ─── Общая обвязка задач ─────────────────────────────────────────────────────
docker network create --label "moysklad.local-ci=$RUN_ID" "$NET" >/dev/null || { say "сеть не создалась"; exit 2; }

COPY_SRC='mkdir -p /work && tar -C /src --exclude=./node_modules --exclude=./.git --exclude=./.tools -cf - . | tar -C /work -xf - && cd /work'

# ctr <имя> <cpus> [docker-опции…] -- <команда bash>
# --cpu-shares 256 (у боевых контейнеров 1024): CI делит сервер с продом, и при
# нехватке CPU уступает боту и WebApp, а не наоборот.
ctr() {
    local name="$1" cpus="$2"; shift 2
    local opts=()
    while [ "$1" != "--" ]; do opts+=("$1"); shift; done
    shift
    timeout -k 30 "${JOB_TIMEOUT_MIN}m" docker run --rm --name "$RUN_ID-$name" \
        --label "moysklad.local-ci=$RUN_ID" --network "$NET" --cpus "$cpus" --cpu-shares 256 --shm-size 1g \
        --tmpfs /tmp:rw,exec,size=4g --tmpfs /work:rw,exec,size=2g \
        -v "$SRC:/src:ro" -e TELEGRAM_TOKEN=0:fake-ci-token "${opts[@]}" \
        "$IMAGE" bash -c "$COPY_SRC && $*"
    local rc=$?
    [ $rc -eq 124 ] && { echo "ТАЙМАУТ ${JOB_TIMEOUT_MIN} мин"; docker rm -f "$RUN_ID-$name" >/dev/null 2>&1; }
    return $rc
}

# job <имя> <функция…>: фоном, вывод в $WORK/<имя>.log, итог в $WORK/<имя>.rc
job() {
    local name="$1"; shift
    (
        s=$(date +%s)
        "$@" > "$WORK/$name.log" 2>&1
        echo "$? $(( $(date +%s) - s ))" > "$WORK/$name.rc"
    ) &
}

# Раскладка тестов по шардам: собираем node id внутри образа (pytest
# --collect-only) и раздаём по кругу. По кругу, а не по файлам: тяжёлые
# параметризованные тесты (обходчик E2E по ролям) иначе садятся в один шард.
# collect_shards <префикс> <N> <аргументы pytest…> → $WORK/shards/<префикс><i>.txt
collect_shards() {
    local prefix="$1" n="$2" rc; shift 2
    mkdir -p "$WORK/shards"
    ctr "collect-$prefix" 2 -- "pytest --collect-only -q -p no:cacheprovider $*" > "$WORK/collect-$prefix.log" 2>&1
    rc=$?
    grep '::' "$WORK/collect-$prefix.log" > "$WORK/shards/$prefix.all"
    if [ $rc -ne 0 ] || [ ! -s "$WORK/shards/$prefix.all" ]; then
        # Тест не импортируется — это красная проверка, а не сломанная CI.
        echo "${rc/#0/1} 0" > "$WORK/collect-$prefix.rc"
        JOB_ORDER+=("collect-$prefix")
        return 1
    fi
    # Заведомо долгие (обходчик «кликнуть всё» — минуты на роль) раздаём первыми,
    # чтобы они не собрались в одном шарде.
    { grep -E "$HEAVY_TESTS_RE" "$WORK/shards/$prefix.all"; grep -vE "$HEAVY_TESTS_RE" "$WORK/shards/$prefix.all"; } \
        | awk -v n="$n" -v dir="$WORK/shards" -v p="$prefix" \
            '{ print > (dir "/" p (NR - 1) % n ".txt") }'
    rm -f "$WORK/shards/$prefix.all"
}

JOB_ORDER=()

# ─── lint ────────────────────────────────────────────────────────────────────
lint_job() {
    ctr lint 2 -- '
        rc=0
        echo "── ruff (строгий гейт)"; ruff check --no-cache --select=E9,F63,F7,F82 --statistics . || rc=1
        echo "── ruff (полный набор)"; ruff check --no-cache --output-format=concise . || rc=1
        echo "── mypy"; MYPY_CACHE_DIR=/tmp/mypy mypy || rc=1
        exit $rc'
}

# ─── frontend ────────────────────────────────────────────────────────────────
frontend_job() {
    timeout -k 30 "${JOB_TIMEOUT_MIN}m" docker run --rm --name "$RUN_ID-frontend" \
        --label "moysklad.local-ci=$RUN_ID" --cpus 2 --cpu-shares 256 -v "$SRC:/src:ro" \
        --tmpfs /tmp:rw,exec --tmpfs /work:rw,exec,size=2g \
        -v moysklad-local-ci-npm:/root/.npm node:20-alpine sh -c "$COPY_SRC"' &&
        npm ci --no-audit --no-fund --loglevel=error && npm test'
}

# ─── unit (+ Postgres) ───────────────────────────────────────────────────────
start_pg() {
    docker run -d --rm --name "$PG" --label "moysklad.local-ci=$RUN_ID" --network "$NET" \
        -e POSTGRES_PASSWORD=ci --tmpfs /var/lib/postgresql/data:rw \
        postgres:16-alpine -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
        -c max_connections=400 >/dev/null || return 1
    for _ in $(seq 1 60); do
        docker exec "$PG" pg_isready -U postgres -q 2>/dev/null \
            && docker exec "$PG" psql -U postgres -qtAc 'SELECT 1' >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

unit_shard_job() {  # unit_shard_job <i>: тесты из $WORK/shards/unit<i>.txt
    local i="$1"
    ctr "unit$i" 3 -v "$COV_VOL:/cov" -v "$WORK/shards:/shards:ro" \
        -e "TEST_PG_URL=postgresql://postgres:ci@$PG:5432/postgres" \
        -e "COVERAGE_FILE=/cov/.coverage.unit$i" -- \
        "mapfile -t T < /shards/unit$i.txt && pytest -m 'not e2e and not perf' -q --tb=short -p no:cacheprovider --cov --cov-report= \
         --durations=10 \"\${T[@]}\""
}

coverage_job() {
    ctr coverage 1 -v "$COV_VOL:/cov" -- \
        "set -o pipefail; coverage combine --data-file=/cov/.coverage /cov/.coverage.unit* >/dev/null && \
         coverage report --data-file=/cov/.coverage --fail-under=$COV_FAIL_UNDER | tail -n 1"
}

# ─── e2e / perf ──────────────────────────────────────────────────────────────
e2e_shard_job() {  # e2e_shard_job <i>: тесты из $WORK/shards/e2e<i>.txt
    local i="$1"
    ctr "e2e$i" 2 -v "$WORK/shards:/shards:ro" -e E2E_REQUIRED=1 -- \
        "mapfile -t T < /shards/e2e$i.txt && \
         pytest -m e2e -q --tb=short -p no:cacheprovider --durations=10 \"\${T[@]}\""
}

perf_job() {
    ctr perf 4 -- 'pytest tests/perf -m perf -q --tb=short -p no:cacheprovider -s \
        --benchmark-min-rounds=3 --benchmark-columns=min,mean,max,rounds --benchmark-sort=mean'
}

# ─── Фаза 1: всё, что не мешает друг другу, — параллельно ────────────────────
if want lint; then job lint lint_job; JOB_ORDER+=(lint); fi
if want frontend; then job frontend frontend_job; JOB_ORDER+=(frontend); fi

if want unit; then
    docker volume create --label "moysklad.local-ci=$RUN_ID" "$COV_VOL" >/dev/null
    if ! start_pg; then
        say "ОШИБКА: Postgres не поднялся"; docker logs "$PG" 2>&1 | tail -20 >> "$LOG"; exit 2
    fi
    collect_shards unit "$UNIT_SHARDS" -m "'not e2e and not perf'" tests \
        || say "pytest не собрал юнит-тесты — см. collect-unit в логе"
    for list in "$WORK"/shards/unit[0-9]*.txt; do
        [ -f "$list" ] || continue
        idx="$(basename "$list" .txt)"; idx="${idx#unit}"
        job "unit$idx" unit_shard_job "$idx"
        JOB_ORDER+=("unit$idx")
    done
fi

if want e2e; then
    collect_shards e2e "$E2E_SHARDS" -m e2e tests/e2e \
        || say "pytest не собрал E2E-тесты — см. collect-e2e в логе"
    for list in "$WORK"/shards/e2e[0-9]*.txt; do
        [ -f "$list" ] || continue
        idx="$(basename "$list" .txt)"; idx="${idx#e2e}"
        job "e2e$idx" e2e_shard_job "$idx"
        JOB_ORDER+=("e2e$idx")
    done
fi

say "Фаза 1 запущена: ${JOB_ORDER[*]}"
wait

if want unit && ls "$WORK"/unit[0-9]*.rc >/dev/null 2>&1; then
    job coverage coverage_job; JOB_ORDER+=(coverage); wait
fi

# ─── Фаза 2: perf в одиночестве ──────────────────────────────────────────────
if want perf; then
    say "Фаза 2: perf"
    job perf perf_job; JOB_ORDER+=(perf); wait
fi

# ─── Итог ────────────────────────────────────────────────────────────────────
FAILED=()
SUMMARY="$WORK/summary.txt"
{
    printf '%-10s %-6s %6s  %s\n' "задача" "итог" "сек" "последняя строка"
    for name in "${JOB_ORDER[@]}"; do
        read -r rc secs < "$WORK/$name.rc" 2>/dev/null || { rc=255; secs=0; }
        last="$(grep -E 'passed|failed|error|Found|All checks|Test Files|TOTAL|ТАЙМАУТ|Success' "$WORK/$name.log" 2>/dev/null \
            | tail -n 1 | cut -c1-110)"
        if [ "$rc" = 0 ]; then st="OK"; else st="FAIL"; FAILED+=("$name"); fi
        printf '%-10s %-6s %6s  %s\n' "$name" "$st" "$secs" "$last"
    done
    echo "Всего: $(( $(date +%s) - T0 )) с"
} > "$SUMMARY"

{
    echo
    echo "════════ ИТОГ ════════"
    cat "$SUMMARY"
    for name in "${JOB_ORDER[@]}"; do
        echo
        echo "════════ $name ════════"
        cat "$WORK/$name.log" 2>/dev/null
    done
    [ -f "$WORK/image.log" ] && { echo; echo "════════ сборка образа ════════"; cat "$WORK/image.log"; }
} >> "$LOG"

echo
cat "$SUMMARY"
if [ ${#FAILED[@]} -eq 0 ]; then
    say "ЗЕЛЁНАЯ ${SHA:+${SHA:0:12}} · лог: $LOG"
    exit 0
fi
say "КРАСНАЯ ${SHA:+${SHA:0:12}}: упали ${FAILED[*]} · лог: $LOG"
exit 1
