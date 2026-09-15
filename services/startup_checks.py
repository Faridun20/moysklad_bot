"""
Проверки окружения при старте процесса: схема БД и часовой пояс.

Обе ошибки из тех, что НЕ падают сразу, а тихо портят данные:

* **Схема отстала от кода.** `init_db()` делает только `CREATE TABLE IF NOT
  EXISTS`, и колонка, дописанная в определение таблицы, до существующей базы
  не доезжает (догоняет её разовый `scripts/apply_legacy_columns`, который
  легко забыть). Узнавали об этом по 500-й на первой ручке, которая колонку
  читает, — иногда через дни после выката.
* **Процесс не в бизнес-зоне.** `created_at` пишется `datetime.now()`
  процесса, «сегодня» и границы периодов — тоже. Контейнер без `TZ` (или без
  tzdata — glibc молча откатывается на UTC) продолжает работать, но сутки
  съезжают на 5 часов: вечерние продажи попадают в завтра, отчёты врут.

Решения:

* **Процесс не падает.** Упавший старт — это лежащий бот и WebApp из-за
  колонки, которую, может быть, читает одна редкая ручка. Громкий ERROR в лог
  и сообщение админам (`error_alerts.report_problem`, с дросселем) дают
  узнать о проблеме в момент выката, не останавливая работу.
* **Ожидаемая схема — из тех же определений**, что создают таблицы
  (`database._table_ddls`), а не отдельный список: второй список разошёлся бы
  с первым при первой же новой колонке.
* **Сверяются только НЕДОСТАЮЩИЕ таблицы и колонки.** Лишние законны: базы
  эпохи МойСклад несут исторические колонки, и тревога на каждом старте из-за
  них научила бы игнорировать тревоги.
* **Зона сверяется по смещению**, а не по имени: `TZ=Asia/Samarkand` даёт те
  же +5 и ничего не ломает, а `TZ=Asia/Tashkent` без tzdata даёт UTC — имя
  верное, время нет.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Бизнес-зона проекта. Та же, что `ENV TZ` в Dockerfile и дефолт в
# docker-compose; переопределяется env `BUSINESS_TZ` (не `TZ`: `TZ` — это то,
# что ПРОВЕРЯЕМ, и сравнивать его с самим собой бессмысленно).
DEFAULT_BUSINESS_TZ = "Asia/Tashkent"

_CONSTRAINT_WORDS = {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}
_CREATE_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*)\)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def business_tz() -> str:
    return (os.environ.get("BUSINESS_TZ") or DEFAULT_BUSINESS_TZ).strip()


# ─── Схема ────────────────────────────────────────────────────────────────────


def _strip_sql_comments(body: str) -> str:
    """Убрать `-- комментарии` (вне строковых литералов)."""
    out: list[str] = []
    for line in body.splitlines():
        in_str = False
        cut = len(line)
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_str = not in_str
            elif not in_str and line.startswith("--", i):
                cut = i
                break
            i += 1
        out.append(line[:cut])
    return "\n".join(out)


def _split_top_level(body: str) -> list[str]:
    """Разбить тело CREATE TABLE по запятым верхнего уровня (скобки CHECK/
    NUMERIC(10,2)/составного PK внутри не режем)."""
    parts: list[str] = []
    depth = 0
    in_str = False
    cur: list[str] = []
    for ch in body:
        if ch == "'":
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def expected_schema(ddls: list[str]) -> dict[str, set[str]]:
    """{таблица: {колонки}} из текстов CREATE TABLE IF NOT EXISTS."""
    schema: dict[str, set[str]] = {}
    for sql in ddls:
        m = _CREATE_RE.match(sql)
        if not m:
            continue
        table = m.group(1).lower()
        cols: set[str] = set()
        for part in _split_top_level(_strip_sql_comments(m.group(2))):
            first = part.split(None, 1)[0].strip('"')
            if first.upper() in _CONSTRAINT_WORDS:
                continue
            cols.add(first.lower())
        schema[table] = cols
    return schema


def actual_schema() -> dict[str, set[str]]:
    """{таблица: {колонки}} живой базы. Postgres — information_schema
    текущей схемы; SQLite — sqlite_master + PRAGMA table_info."""
    from services import database as db

    result: dict[str, set[str]] = {}
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        if db.USE_POSTGRES:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            )
            for row in cur.fetchall():
                result.setdefault(str(row["table_name"]).lower(), set()).add(
                    str(row["column_name"]).lower()
                )
        else:
            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            tables = [str(r[0]) for r in cur.fetchall()]
            for t in tables:
                cur.execute(f'PRAGMA table_info("{t}")')
                result[t.lower()] = {str(r[1]).lower() for r in cur.fetchall()}
    return result


def check_schema(
    expected: dict[str, set[str]] | None = None, actual: dict[str, set[str]] | None = None
) -> list[str]:
    """Список расхождений человеческим текстом; пусто — схема сходится."""
    if expected is None:
        from services import database as db

        expected = expected_schema(db._table_ddls())
    if actual is None:
        actual = actual_schema()
    problems: list[str] = []
    for table in sorted(expected):
        if table not in actual:
            problems.append(f"нет таблицы {table}")
            continue
        missing = sorted(expected[table] - actual[table])
        if missing:
            problems.append(f"{table}: нет колонок {', '.join(missing)}")
    return problems


# ─── Часовой пояс ─────────────────────────────────────────────────────────────


def _fmt_offset(delta: timedelta | None) -> str:
    total = int((delta or timedelta(0)).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def check_timezone(now_utc: datetime | None = None) -> list[str]:
    """Расхождение локальной зоны процесса с бизнес-зоной; пусто — всё верно."""
    expected_name = business_tz()
    now_utc = now_utc or datetime.now(UTC)
    tz_env = os.environ.get("TZ")
    try:
        expected = ZoneInfo(expected_name)
    except Exception:  # noqa: BLE001 — нет tzdata / опечатка в имени зоны
        return [
            f"Бизнес-зона {expected_name!r} не разрешается (нет tzdata или опечатка "
            "в BUSINESS_TZ) — проверить часовой пояс процесса нечем"
        ]
    # astimezone() без аргумента — локальная зона ПРОЦЕССА (TZ + tzdata),
    # ровно та, в которой datetime.now() пишет created_at.
    local_offset = now_utc.astimezone().utcoffset()
    expected_offset = now_utc.astimezone(expected).utcoffset()
    if local_offset != expected_offset:
        return [
            f"Часовой пояс процесса {_fmt_offset(local_offset)} "
            f"(TZ={tz_env or 'не задан'}), а бизнес-зона {expected_name} — "
            f"{_fmt_offset(expected_offset)}. created_at и «сегодня» съезжают: "
            "задайте TZ=Asia/Tashkent и проверьте, что в образе есть tzdata"
        ]
    if tz_env and tz_env.lstrip(":") != expected_name:
        # Смещение то же — данные не портятся; тревогу не поднимаем, но след оставляем.
        logger.info(
            "TZ=%s отличается по имени от бизнес-зоны %s, смещение совпадает (%s)",
            tz_env, expected_name, _fmt_offset(local_offset),
        )
    return []


# ─── Запуск ───────────────────────────────────────────────────────────────────


async def run_startup_checks(process: str = "") -> dict[str, list[str]]:
    """Прогнать обе проверки, при расхождениях — ERROR + алерт админам.

    Никогда не бросает: вызывается из старта процесса после `init_db()`.
    Схема читается в потоке — синхронный драйвер не держит event loop.
    """
    from services import error_alerts

    where = f" ({process})" if process else ""
    result: dict[str, list[str]] = {"schema": [], "timezone": []}
    try:
        result["schema"] = await asyncio.to_thread(check_schema)
    except Exception as e:  # noqa: BLE001
        logger.exception("Сверка схемы БД не выполнилась")
        result["schema"] = [f"сверка не выполнилась: {type(e).__name__}: {e}"]
    try:
        result["timezone"] = check_timezone()
    except Exception as e:  # noqa: BLE001
        logger.exception("Проверка часового пояса не выполнилась")
        result["timezone"] = [f"проверка не выполнилась: {type(e).__name__}: {e}"]

    if result["schema"]:
        await error_alerts.report_problem(
            f"Схема БД отстаёт от кода{where}",
            result["schema"]
            + [
                "Существующую базу догоняет разовый scripts/apply_legacy_columns: "
                "колонка должна быть в его списке, прогон --dry-run, затем --apply"
            ],
            key="startup-schema",
        )
    else:
        logger.info("Сверка схемы БД: расхождений нет ✓")
    if result["timezone"]:
        await error_alerts.report_problem(
            f"Процесс не в бизнес-зоне{where}", result["timezone"], key="startup-timezone"
        )
    else:
        logger.info("Часовой пояс процесса совпадает с бизнес-зоной %s ✓", business_tz())
    return result
