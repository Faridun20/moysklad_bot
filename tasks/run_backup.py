"""
CLI: ежедневный backup БД в приватный Telegram-канал.

Самый простой serious backup для одного-двух-микро бизнеса:
  * единственная новая зависимость — приватный TG-канал (бот уже есть)
  * Postgres → `pg_dump --clean` | gzip; SQLite → плоский gzip файла
  * upload через aiogram.Bot.send_document (тот же токен что у самого бота)
  * хранится в TG бесконечно, восстановление = download + `gunzip | psql`

Лимиты:
  * Telegram Bot API: 50 MB на send_document. У нас БД сейчас <10 MB,
    с запасом >×5. Если БД вырастет — warning в логе + retry на B2/R2.
  * 1 файл = 1 backup, не делим. Если упрётся в 50MB — отдельная задача.

Use:
  python -m tasks.run_backup

Railway Cron (suggested): 0 3 * * *  (3:00 UTC = 8:00 Ташкент)

Env:
  BACKUP_TG_CHAT_ID — id приватного канала (формат -100xxxx или числовой)
  DATABASE_URL      — Postgres connection string (если есть)
  DB_PATH           — путь к SQLite (если без Postgres)
  TELEGRAM_TOKEN    — токен бота (тот же что для основного flow)

Чтобы запустить:
  1. Создать приватный канал в Telegram
  2. Добавить бота как Administrator (right: Post Messages)
  3. Получить chat_id канала (forward любое сообщение из канала в @userinfobot)
  4. Поставить BACKUP_TG_CHAT_ID в Railway env cron-сервиса
  5. Расписание Railway Cron Job: 0 3 * * *
"""

from __future__ import annotations

import gzip
import logging
import io
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backup")

# Telegram Bot API max document size — 50 MB (через api.telegram.org).
# Self-hosted Bot API server поднимает до 2 GB, но мы используем cloud.
TG_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024

# Если файл больше этого — WARNING (близко к лимиту), нужен план миграции
# на B2/R2 или разбивка/архивирование исторических данных.
TG_SIZE_WARNING_BYTES = 40 * 1024 * 1024


def _create_backup_postgres(db_url: str, out_path: Path) -> int:
    """Backup Postgres → gzip → out_path. Возвращает размер байт.

    Стратегия:
      1. Сначала `pg_dump` (полный schema + data, best). Требует
         postgresql client в образе.
      2. Если pg_dump отсутствует в PATH (Railway/Nixpacks без
         postgresql client — реальный прод-кейс) → fallback на
         pure-Python COPY-dump через psycopg2 (data-only, но работает
         БЕЗ системных бинарей).

    Pure-Python fallback гарантирует что backup не падает из-за
    окружения. Полный dump (с pg_dump) даёт schema, но для нашего
    DR-сценария схему держит `tasks/migrate` (CREATE TABLE), достаточно
    данных.

    Fallback срабатывает в ДВУХ случаях:
      • pg_dump отсутствует в PATH (FileNotFoundError);
      • pg_dump есть, но упал (RuntimeError) — самый частый прод-кейс это
        «server version mismatch»: версия бинаря pg_dump < версии сервера
        (pg_dump отказывается дампить сервер новее себя). railpack apt даёт
        postgresql-client из дефолтного Debian-репо (старее, чем апгрейженный
        managed-Postgres), поэтому без PGDG-репозитория версии расходятся.
        Pure-Python COPY-dump через libpq версионно-независим.
    """
    try:
        return _pg_dump_native(db_url, out_path)
    except FileNotFoundError:
        logger.warning(
            "pg_dump не найден в PATH — fallback на pure-Python COPY-dump "
            "(data-only). Restore: `python -m tasks.migrate` + psql < dump. "
            "Для полного schema+data dump поставь postgresql client в образ."
        )
        return _pg_dump_pure_python(db_url, out_path)
    except RuntimeError as e:
        logger.warning(
            "pg_dump упал (%s) — fallback на pure-Python COPY-dump (data-only). "
            "Частая причина: server version mismatch (версия pg_dump < версии "
            "сервера). Restore: `python -m tasks.migrate` + psql < dump.",
            e,
        )
        return _pg_dump_pure_python(db_url, out_path)


def _pg_dump_native(db_url: str, out_path: Path) -> int:
    """pg_dump → gzip. Бросает FileNotFoundError если бинарь отсутствует.

    Опции:
      --no-owner / --no-acl — restore в любую учётную запись
      --clean --if-exists — DROP IF EXISTS перед CREATE (idempotent restore)
    """
    cmd = ["pg_dump", "--no-owner", "--no-acl", "--clean", "--if-exists", db_url]
    with open(out_path, "wb") as fout:
        gz = gzip.GzipFile(fileobj=fout, mode="wb")
        try:
            # Popen бросит FileNotFoundError если pg_dump нет в PATH —
            # ловим выше для fallback.
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                gz.write(chunk)
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(f"pg_dump упал rc={proc.returncode}")
        finally:
            gz.close()
    return out_path.stat().st_size


def _pg_dump_pure_python(db_url: str, out_path: Path) -> int:
    """Pure-Python data-dump через psycopg2 COPY — не требует pg_dump.

    Формат — pg-native COPY (как data-секция pg_dump). На restore:
      1. `python -m tasks.migrate`   (создаёт схему: CREATE TABLE + ALTER)
      2. `psql $DATABASE_URL < dump.sql`   (TRUNCATE + COPY данные)

    Data-only: схему держит код (init_db/migrate), здесь только строки.
    Для DR это полноценно — потеряли Postgres → migrate воссоздаёт
    структуру → backup заливает данные.

    TRUNCATE ... CASCADE перед COPY делает restore идемпотентным. У нас
    нет FK-constraints (CLAUDE.md: «без FK»), порядок таблиц не важен.
    """
    import psycopg2

    conn = psycopg2.connect(db_url)
    try:
        cur = conn.cursor()
        # Список таблиц public-схемы. Порядок алфавитный — без FK
        # зависимостей он не важен.
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        tables = [r[0] for r in cur.fetchall()]

        rows_by_table: dict[str, int] = {}
        with gzip.open(str(out_path), "wt", encoding="utf-8") as gz:
            gz.write("-- Pure-Python COPY dump (data-only) — fallback без pg_dump\n")
            gz.write("-- Restore: python -m tasks.migrate && psql $DATABASE_URL < this.sql\n")
            gz.write(f"-- Tables: {len(tables)}\n\n")
            gz.write("BEGIN;\n")
            for t in tables:
                # Идентификатор таблицы оборачиваем в кавычки на случай
                # reserved word / спецсимволов (наши имена простые, но
                # безопаснее).
                qt = f'"{t}"'
                gz.write(f"\nTRUNCATE TABLE {qt} CASCADE;\n")
                gz.write(f"COPY {qt} FROM stdin;\n")
                # copy_expert пишет str в text-mode file (наш gzip wt).
                buf = io.StringIO()
                cur.copy_expert(f"COPY {qt} TO STDOUT", buf)
                payload = buf.getvalue()
                gz.write(payload)
                gz.write("\\.\n")
                # В COPY text-формате перевод строки внутри значения
                # экранируется как «\n» двумя символами, поэтому реальные
                # переводы строк — это ровно строки таблицы.
                rows_by_table[t] = payload.count("\n")
            gz.write("\nCOMMIT;\n")
    finally:
        conn.close()

    _log_dump_contents(rows_by_table)
    return out_path.stat().st_size


def _log_dump_contents(rows_by_table: dict[str, int]) -> None:
    """Что именно попало в дамп — иначе «Backup создан: 0.01 MB» ничего не значит.

    Размер файла не отвечает на единственный важный вопрос: это маленькая база
    или пустой дамп. Печатаем число строк и крупнейшие таблицы — по ним видно
    сразу, а пустой дамп кричит о себе сам, не дожидаясь дня восстановления.
    """
    total = sum(rows_by_table.values())
    filled = {t: n for t, n in rows_by_table.items() if n}
    if not total:
        logger.error(
            "Дамп ПУСТОЙ: %d таблиц, 0 строк. Файл создан, но восстанавливать из "
            "него нечего — проверьте, что DATABASE_URL указывает на боевую базу.",
            len(rows_by_table),
        )
        return
    top = sorted(filled.items(), key=lambda kv: kv[1], reverse=True)[:5]
    logger.info(
        "Дамп: %d таблиц (%d непустых), %d строк. Крупнейшие: %s",
        len(rows_by_table),
        len(filled),
        total,
        ", ".join(f"{t}={n}" for t, n in top),
    )


def _create_backup_sqlite(db_path: str, out_path: Path) -> int:
    """Плоский gzip SQLite файла. Возвращает размер байт.

    SQLite — single-file, копия = бэкап (при отсутствии активных
    transactions). На проде у нас Postgres, эта ветка для dev/тестов.
    """
    src = Path(db_path)
    if not src.exists():
        raise RuntimeError(f"SQLite файл не найден: {db_path}")
    with open(src, "rb") as fin, gzip.open(str(out_path), "wb") as fout:
        while True:
            chunk = fin.read(64 * 1024)
            if not chunk:
                break
            fout.write(chunk)
    return out_path.stat().st_size


# Расшифровка отказов Telegram при загрузке. Без неё в логе остаётся
# «Bad Request: chat not found» и стек aiogram на десять кадров — по нему не
# видно, что чинить надо переменную окружения, а не код. Ключ — подстрока
# ответа Bot API в нижнем регистре.
_UPLOAD_HINTS: tuple[tuple[str, str], ...] = (
    (
        "chat not found",
        "чата {chat_id} бот не видит. Обычно одно из двух: бота не добавили в "
        "канал (или удалили), либо id записан без префикса -100. Перешлите "
        "сообщение из канала в @userinfobot, сверьте id и добавьте бота "
        "администратором.",
    ),
    (
        "not enough rights",
        "бот состоит в канале {chat_id}, но публиковать не может — дайте ему "
        "право Post Messages.",
    ),
    (
        "bot was kicked",
        "бота удалили из канала {chat_id} — верните администратором.",
    ),
    (
        "bot is not a member",
        "бот не состоит в канале {chat_id} — добавьте его администратором.",
    ),
    (
        "chat_id is empty",
        "BACKUP_TG_CHAT_ID пуст или нечитаем.",
    ),
)


def upload_failure_hint(exc: BaseException, chat_id: int) -> str | None:
    """Человеческое объяснение отказа Telegram или None, если причина незнакомая."""
    text = str(exc).lower()
    for needle, hint in _UPLOAD_HINTS:
        if needle in text:
            return hint.format(chat_id=chat_id)
    return None


async def _upload_to_telegram(token: str, chat_id: int, file_path: Path, caption: str) -> None:
    """Send_document через aiogram. Файл закроется автоматически."""
    from aiogram import Bot
    from aiogram.types import FSInputFile

    bot = Bot(token=token)
    try:
        document = FSInputFile(path=str(file_path), filename=file_path.name)
        await bot.send_document(
            chat_id=chat_id,
            document=document,
            caption=caption[:1024],  # Telegram caption limit
        )
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass


async def main() -> int:
    tg_chat_id_raw = os.environ.get("BACKUP_TG_CHAT_ID", "").strip()
    if not tg_chat_id_raw:
        logger.error(
            "BACKUP_TG_CHAT_ID не задан — некуда отправлять backup. "
            "Создайте приватный TG-канал, добавьте бота админом, "
            "получите chat_id и задайте env-переменную."
        )
        return 1
    try:
        tg_chat_id = int(tg_chat_id_raw)
    except ValueError:
        logger.error("BACKUP_TG_CHAT_ID должен быть целым числом, получено: %r", tg_chat_id_raw)
        return 1

    # Telegram-токен берём через config, чтобы прошла единая валидация
    # (TELEGRAM_TOKEN с ':' и т.д.).
    from config import TELEGRAM_TOKEN

    db_url = os.environ.get("DATABASE_URL", "").strip()
    db_path = os.environ.get("DB_PATH", "").strip()
    use_postgres = bool(db_url)
    if not use_postgres and not db_path:
        logger.error("Ни DATABASE_URL, ни DB_PATH не заданы — нечего бэкапить")
        return 1

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "postgres" if use_postgres else "sqlite"
    fname = f"moysklad-bot-{suffix}-{ts}.sql.gz"

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / fname
        logger.info("Создаю backup: %s → %s", suffix, fname)
        try:
            if use_postgres:
                size = _create_backup_postgres(db_url, out_path)
            else:
                size = _create_backup_sqlite(db_path, out_path)
        except Exception:
            logger.exception("Создание backup упало")
            return 1

        size_mb = size / 1024 / 1024
        logger.info("Backup создан: %.2f MB", size_mb)

        if size > TG_MAX_FILE_SIZE_BYTES:
            logger.error(
                "Файл %.2f MB > Telegram Bot API limit 50 MB. "
                "Upload точно упадёт. План миграции: B2/R2 или разбивка БД.",
                size_mb,
            )
            return 1
        if size > TG_SIZE_WARNING_BYTES:
            logger.warning(
                "Файл %.2f MB близок к Telegram limit 50 MB. Скоро понадобится "
                "переход на B2/R2 или разбивка БД по партициям.",
                size_mb,
            )

        caption = (
            f"📦 Backup МойСклад-бота\n"
            f"Тип: {suffix}\n"
            f"Размер: {size_mb:.2f} MB\n"
            f"Время: {ts}"
        )

        try:
            await _upload_to_telegram(TELEGRAM_TOKEN, tg_chat_id, out_path, caption)
            logger.info("Backup загружен в Telegram-канал %s", tg_chat_id)
            return 0
        except Exception as e:
            from utils.helpers import redact_token

            hint = upload_failure_hint(e, tg_chat_id)
            if hint:
                # Причина известна и чинится настройкой — стек тут только шумит.
                logger.error("Backup собран, но не отправлен: %s", hint)
            else:
                logger.exception("Upload в Telegram упал: %s", redact_token(repr(e)))
            return 1


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("backup", main))
