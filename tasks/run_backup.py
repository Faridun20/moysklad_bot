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
    """pg_dump → gzip → out_path. Возвращает размер байт.

    Опции pg_dump:
      --no-owner / --no-acl — backup без owner/acl-инфо (восстанавливается
        в любую учётную запись)
      --clean — DROP-statement'ы перед CREATE, чтобы восстановление в
        существующую БД переписало
      --if-exists — DROP IF EXISTS чтобы первый restore не упал
    """
    cmd = ["pg_dump", "--no-owner", "--no-acl", "--clean", "--if-exists", db_url]
    with open(out_path, "wb") as fout:
        gz = gzip.GzipFile(fileobj=fout, mode="wb")
        try:
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
        except Exception:
            logger.exception("Upload в Telegram упал")
            return 1


if __name__ == "__main__":
    from tasks._cron_runner import run_cron

    sys.exit(run_cron("backup", main))
