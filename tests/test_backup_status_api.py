"""
Статус бэкапа в WebApp (B10, «Настройки → Резервные копии»).

Единственный бэкап-путь, видимый из БД приложения: `tasks/run_backup.py`
(дамп → gzip → приватный TG-канал), обёрнутый общим cron-раннером
(`tasks/_cron_runner.run_cron`, `task_name='backup'`), который пишет в
`cron_runs`. Хост-скрипт `/srv/backups/pg-backup.sh` крутится в системном cron
ОС мимо приложения и в `cron_runs` не пишет — эту часть панель принципиально
не видит (см. отчёт агента), поэтому здесь тестируется только видимый путь.
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

import services.roles as roles


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")
    db.set_role(3, "admin", "Admin", "admin")


def _client(monkeypatch):
    import webapp.server as server

    importlib.reload(roles)
    monkeypatch.setattr(server, "verify_init_data", lambda s: {"id": int(s), "first_name": "U"})
    return TestClient(server.app)


def _post(client, path, uid, **body):
    return client.post(path, json={"initData": str(uid), **body})


def test_manager_cannot_see_backup_status(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    r = _post(client, "/api/settings/backup_status", 1)
    assert r.status_code == 403


def test_boss_sees_not_found_when_backup_never_ran(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    client = _client(monkeypatch)
    r = _post(client, "/api/settings/backup_status", 2)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "found": False}


def test_boss_sees_last_successful_backup_run(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    db.record_cron_run(
        task_name="backup", status="ok",
        started_at="2026-09-15 03:00:00", finished_at="2026-09-15 03:00:12",
        duration_ms=12000,
    )
    client = _client(monkeypatch)
    r = _post(client, "/api/settings/backup_status", 2)
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "ok": True, "found": True, "status": "ok",
        "started_at": "2026-09-15 03:00:00", "finished_at": "2026-09-15 03:00:12",
        "error_message": "",
    }


def test_admin_sees_last_failed_backup_run_with_error_text(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    db.record_cron_run(
        task_name="backup", status="failed",
        started_at="2026-09-15 03:00:00", finished_at="2026-09-15 03:00:02",
        duration_ms=2000, error_message="BACKUP_TG_CHAT_ID не задан",
    )
    client = _client(monkeypatch)
    r = _post(client, "/api/settings/backup_status", 3)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"
    assert body["error_message"] == "BACKUP_TG_CHAT_ID не задан"


def test_only_the_latest_backup_run_is_reported(isolated_db, monkeypatch):
    """cron_runs — INSERT-only лог; ручка отдаёт ПОСЛЕДНИЙ запуск, а не первый
    (`get_last_cron_runs`, DISTINCT/MAX по started_at)."""
    db = isolated_db
    _setup(db)
    db.record_cron_run(
        task_name="backup", status="failed",
        started_at="2026-09-14 03:00:00", finished_at="2026-09-14 03:00:02",
        duration_ms=2000, error_message="старый сбой",
    )
    db.record_cron_run(
        task_name="backup", status="ok",
        started_at="2026-09-15 03:00:00", finished_at="2026-09-15 03:00:10",
        duration_ms=10000,
    )
    client = _client(monkeypatch)
    r = _post(client, "/api/settings/backup_status", 2)
    body = r.json()
    assert body["status"] == "ok"
    assert body["started_at"] == "2026-09-15 03:00:00"


def test_unrelated_cron_tasks_do_not_leak_into_backup_status(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    db.record_cron_run(
        task_name="ops_monitor", status="ok",
        started_at="2026-09-15 06:00:00", finished_at="2026-09-15 06:00:01",
        duration_ms=1000,
    )
    client = _client(monkeypatch)
    r = _post(client, "/api/settings/backup_status", 2)
    assert r.json() == {"ok": True, "found": False}
