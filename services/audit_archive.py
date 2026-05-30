"""
Архивация журнала аудита перед чисткой janitor'ом (#33, IMPLEMENTATION.md §13).

`tasks/run_maintenance` ДО `prune_audit_log` выгружает уходящие записи в файл
(JSONL) и (опционально) грузит в Google Drive, фиксируя факт в
`audit_archive_exports`. Так история аудита не теряется при ретеншене.

Загрузка в Drive — pluggable и gated на env:
  • GOOGLE_DRIVE_FOLDER_ID
  • GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON  (содержимое service-account .json)

Без креды (или без установленных google-libs) — no-op с логом: экспорт всё равно
считается и фиксируется (drive_file_id/url пустые). Реальная заливка включится,
когда добавишь креды в Railway. Паттерн «best-effort, gated» как у MS-интеграций.
"""

import json
import logging
import os

from services import database as db

logger = logging.getLogger(__name__)


async def _upload_to_drive(file_name: str, content: bytes) -> tuple[str, str] | None:
    """Загрузить файл в Google Drive. Возвращает (file_id, web_url) или None (no-op).

    Gated: без GOOGLE_DRIVE_FOLDER_ID / service-account JSON → None. Без
    установленных google-libs → None (лог)."""
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    sa_json = os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", "")
    if not folder_id or not sa_json:
        logger.info("audit_archive: Drive не настроен (нет GOOGLE_DRIVE_*) — заливку пропускаю")
        return None
    try:
        import io

        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
        from googleapiclient.http import MediaIoBaseUpload  # type: ignore
    except Exception:
        logger.warning("audit_archive: google-libs не установлены — заливка пропущена")
        return None
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive.file"]
        )
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype="application/x-ndjson")
        meta = {"name": file_name, "parents": [folder_id]}
        f = svc.files().create(body=meta, media_body=media, fields="id, webViewLink").execute()
        return f.get("id", ""), f.get("webViewLink", "")
    except Exception:
        logger.exception("audit_archive: ошибка заливки в Drive")
        return None


async def export_audit_archive(before_iso: str, exported_by: int = 0) -> dict:
    """Выгрузить записи аудита старше `before_iso` в JSONL + (опц.) Drive и
    зафиксировать в audit_archive_exports. Идемпотентно по period_end.

    Возвращает {ok, exported, uploaded?, file_name?, skipped?}."""
    rows = await db.get_audit_log_before(before_iso)
    if not rows:
        return {"ok": True, "exported": 0, "skipped": "nothing-to-archive"}

    period_start = str(rows[0].get("created_at") or before_iso)
    period_end = before_iso
    if await db.audit_archive_exists(period_end):
        return {"ok": True, "exported": 0, "skipped": "already-archived"}

    content = (
        "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
    ).encode("utf-8")
    file_name = f"audit_{period_start[:10]}_{period_end[:10]}.jsonl"

    up = await _upload_to_drive(file_name, content)
    drive_id, drive_url = up if up else ("", "")
    await db.record_audit_archive_export(
        period_start,
        period_end,
        file_name,
        drive_id,
        drive_url,
        len(rows),
        len(content),
        exported_by,
    )
    logger.info(
        "audit_archive: экспортировано %d записей (%d байт), drive=%s",
        len(rows),
        len(content),
        "yes" if up else "no",
    )
    return {
        "ok": True,
        "exported": len(rows),
        "uploaded": bool(up),
        "file_name": file_name,
    }
