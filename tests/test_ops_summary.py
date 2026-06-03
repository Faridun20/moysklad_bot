"""
Операционная сводка (services.ops_summary) — общий источник для WebApp
/api/ops-summary и дневного пинга бота. Проверяем сбор на реальной БД
(isolated_db) и чистые билдеры пинга (tasks.run_ops_monitor).
"""

import asyncio

from services.ops_summary import gather_ops_summary
from tasks.run_ops_monitor import (
    _section_count,
    build_daily_ping,
    build_ping_keyboard,
)

_SECTION_KEYS = (
    "stale_orders",
    "overdue_undeposited",
    "deposits",
    "returns",
    "expiring_batches",
    "low_stock",
    "stale_crons",
    "ms_anomalies",
)


def test_gather_ops_summary_quiet_db(isolated_db):
    """Тихая БД (нет висяков, cron'ы свежие): структура есть, всё по нулям."""
    db = isolated_db
    from datetime import datetime

    from services.ops_summary import CRON_THRESHOLDS_HOURS

    # Без свежих cron-runs все мониторимые задачи попадут в stale_crons как
    # «ни разу не запускался» — это корректно, но для теста «тихо» прогреем их.
    fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for task in CRON_THRESHOLDS_HOURS:
        db.record_cron_run(task, "ok", fresh, fresh, 100)

    summary = asyncio.run(gather_ops_summary())
    for key in _SECTION_KEYS:
        assert key in summary
    assert summary["total"] == 0
    assert summary["stale_orders"]["count"] == 0
    assert summary["stale_crons"]["count"] == 0


def test_gather_ops_summary_counts_stale_order(isolated_db):
    """Зависшая pending-заявка старше порога попадает в stale_orders + total."""
    db = isolated_db
    db.set_role(1, "m", "M", "manager")
    oid = db.create_order(1, "M", "")
    db.update_order_agent(oid, "A", "Client")
    db.update_order_status(oid, "pending")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q("UPDATE orders SET created_at=?, submitted_at=NULL WHERE id=?"),
            ("2000-01-01 00:00:00", oid),
        )
        conn.commit()
    summary = asyncio.run(gather_ops_summary())
    assert summary["stale_orders"]["count"] == 1
    assert summary["stale_orders"]["items"][0]["id"] == oid
    assert summary["total"] >= 1


# ─── Пинг бота (чистые билдеры) ───────────────────────────────────────────────


def _summary(**counts):
    """Минимальный summary-dict с заданными count по секциям."""
    base = {k: {"count": 0} for k in _SECTION_KEYS}
    base["ms_anomalies"] = {"drift": 0, "deleted": 0, "demand_failed": 0}
    for k, v in counts.items():
        base[k] = v if k == "ms_anomalies" else {"count": v}
    return base


def test_section_count_ms_anomalies_sums():
    s = _summary(ms_anomalies={"drift": 2, "deleted": 1, "demand_failed": 3})
    assert _section_count(s, "ms_anomalies") == 6
    assert _section_count(s, "deposits") == 0


def test_build_daily_ping_boss_lists_nonzero():
    txt = build_daily_ping("boss", _summary(stale_orders=2, deposits=3))
    assert txt is not None
    assert "Требует внимания" in txt
    assert "5" in txt  # 2 + 3
    assert "Зависшие заявки" in txt and "Сдачи" in txt
    assert "Возвраты" not in txt  # 0 — не показываем


def test_build_daily_ping_none_when_all_zero():
    assert build_daily_ping("boss", _summary()) is None


def test_build_daily_ping_role_scoping():
    s = _summary(deposits=1, returns=1, stale_orders=1)
    # bookkeeper видит только сдачи
    book = build_daily_ping("bookkeeper", s)
    assert book is not None and "Сдачи" in book
    assert "Зависшие" not in book and "Возвраты" not in book
    # warehouse — возвраты/партии/низкий остаток (не сдачи/зависшие)
    wh = build_daily_ping("warehouse_keeper", s)
    assert wh is not None and "Возвраты" in wh
    assert "Сдачи" not in wh and "Зависшие" not in wh
    # manager сводку не получает вовсе
    assert build_daily_ping("manager", s) is None


def test_build_ping_keyboard():
    kb = build_ping_keyboard("https://app.example.com")
    assert kb is not None
    btn = kb["inline_keyboard"][0][0]
    assert btn["web_app"]["url"] == "https://app.example.com"
    assert build_ping_keyboard("") is None
    assert build_ping_keyboard(None) is None
