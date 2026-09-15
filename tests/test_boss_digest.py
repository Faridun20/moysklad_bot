"""
services.boss_digest — вечерний дайджест боссу (то, что notify_policy
отправила НЕ немедленным пушем: платежи/сдачи/возвраты ниже
boss_instant_threshold_usd). Реальная БД (isolated_db); граница с Telegram
(aiogram.Bot) мокается, как в test_money_report.py.
"""

import asyncio
import logging

TOKEN = "123456:AAH-secret-bot-token"


def _run(coro):
    return asyncio.run(coro)


class _FakeBot:
    def __init__(self, *, rich_error=None):
        self.rich_error = rich_error
        self.rich_calls = []

    async def send_rich_message(self, chat_id, rich_message, **kw):
        if self.rich_error:
            raise self.rich_error
        self.rich_calls.append((chat_id, rich_message))
        return object()


def _patch_bot(monkeypatch, bot):
    import services.boss_digest as bd
    import webapp.server as server

    async def _get():
        return bot

    monkeypatch.setattr(server, "get_notify_bot", _get)
    return bd


# ─── Сбор содержимого ──────────────────────────────────────────────────────


def test_empty_digest_is_empty(isolated_db):
    from services import boss_digest as bd

    data = _run(bd.gather())
    assert bd.is_empty(data) is True


def test_payment_below_threshold_shows_in_digest(isolated_db):
    from services import boss_digest as bd

    db = isolated_db
    db.add_payment(10, "u", "Иван", 100.0, "USD", "аренда")

    data = _run(bd.gather())
    assert bd.is_empty(data) is False
    assert data["payments"]["count"] == 1
    assert data["payments"]["waiting_total"] == 1
    assert "Иван" in data["payments"]["lines"][0]


def test_payment_at_or_above_threshold_is_excluded(isolated_db):
    """Крупный платёж уже ушёл немедленным пушем (notify_policy) — дайджест
    его не дублирует, но считает в «сколько всего ждёт»."""
    from services import boss_digest as bd

    db = isolated_db
    db.add_payment(10, "u", "Мелкий", 100.0, "USD", "c")
    db.add_payment(10, "u", "Крупный", 9000.0, "USD", "c")

    data = _run(bd.gather())
    assert data["payments"]["count"] == 1
    assert "Крупный" not in " ".join(data["payments"]["lines"])
    assert data["payments"]["waiting_total"] == 2


def test_pending_cash_deposit_below_threshold_shows_in_digest(isolated_db):
    from services import boss_digest as bd

    db = isolated_db
    db.set_role(10, "u", "Manager", "manager")
    _run(db.create_cash_deposit(10, 300.0))

    data = _run(bd.gather())
    assert data["deposits"]["count"] == 1
    assert data["deposits"]["waiting_total"] == 1


def test_pending_return_below_threshold_shows_in_digest(isolated_db):
    from services import boss_digest as bd

    db = isolated_db
    db.set_role(10, "u", "Manager", "manager")
    oid = db.create_order(10, "Manager", "")
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
                "created_by, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)"
            ),
            (oid, "full", "брак", 20000, 10, db.now_str()),
        )
        conn.commit()

    data = _run(bd.gather())
    assert data["returns"]["count"] == 1
    assert f"заказ #{oid}" in data["returns"]["lines"][0]


def test_confirmed_small_payment_counts_as_received(isolated_db):
    from services import boss_digest as bd

    db = isolated_db
    pid = db.add_payment(10, "u", "Иван", 200.0, "USD", "c")
    _run(db.confirm_payment(pid, confirmed_by=1, confirmed_name="Boss"))

    data = _run(bd.gather())
    assert data["received"]["count"] == 1
    assert data["received"]["by_currency"] == [{"currency": "USD", "total": 200.0}]


def test_received_respects_since_last_run(isolated_db):
    """«Мелкие поступления» — с прошлого дайджеста, не вся история."""
    from services import boss_digest as bd

    db = isolated_db
    pid_old = db.add_payment(10, "u", "Старый", 200.0, "USD", "c")
    _run(db.confirm_payment(pid_old, confirmed_by=1, confirmed_name="Boss"))
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(db.q("UPDATE payments SET confirmed_at = ? WHERE id = ?"), ("2020-01-01 00:00:00", pid_old))
        conn.commit()

    bd.mark_run("2025-01-01 00:00:00")
    pid_new = db.add_payment(11, "u", "Новый", 150.0, "USD", "c")
    _run(db.confirm_payment(pid_new, confirmed_by=1, confirmed_name="Boss"))

    data = _run(bd.gather())
    assert data["received"]["count"] == 1
    assert data["received"]["by_currency"] == [{"currency": "USD", "total": 150.0}]


# ─── is_due / идемпотентность ───────────────────────────────────────────────


def test_is_due_false_before_digest_time(isolated_db, monkeypatch):
    from datetime import datetime

    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "19:00")
    monkeypatch.setattr(
        "utils.helpers.local_now", lambda: datetime(2026, 9, 15, 18, 59)
    )
    assert bd.is_due() is False


def test_is_due_true_after_digest_time_when_not_sent_today(isolated_db, monkeypatch):
    from datetime import datetime

    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "19:00")
    monkeypatch.setattr(
        "utils.helpers.local_now", lambda: datetime(2026, 9, 15, 19, 5)
    )
    assert bd.is_due() is True


def test_is_due_false_after_marked_sent_today(isolated_db, monkeypatch):
    from datetime import datetime

    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "19:00")
    monkeypatch.setattr(
        "utils.helpers.local_now", lambda: datetime(2026, 9, 15, 19, 5)
    )
    bd.mark_run("2026-09-15 19:00:03")
    assert bd.is_due() is False


def test_is_due_true_again_next_day(isolated_db, monkeypatch):
    from datetime import datetime

    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "19:00")
    bd.mark_run("2026-09-15 19:00:03")
    monkeypatch.setattr(
        "utils.helpers.local_now", lambda: datetime(2026, 9, 16, 19, 1)
    )
    assert bd.is_due() is True


def test_is_due_reacts_to_changed_setting(isolated_db, monkeypatch):
    """Расписание — «каждые 15 минут» именно потому, что время дайджеста
    может смениться: смену подхватывает следующий тик без правки crontab."""
    from datetime import datetime

    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "20:00")
    monkeypatch.setattr(
        "utils.helpers.local_now", lambda: datetime(2026, 9, 15, 19, 5)
    )
    assert bd.is_due() is False


# ─── Разметка ────────────────────────────────────────────────────────────────


def test_blocks_carry_a_payments_section(isolated_db):
    from services import boss_digest as bd

    db = isolated_db
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")

    blocks = bd.build_blocks(_run(bd.gather()))
    kinds = [b.type for b in blocks]
    assert "heading" in kinds
    assert "list" in kinds


def test_text_fallback_escapes_names(isolated_db):
    from services import boss_digest as bd

    db = isolated_db
    db.add_payment(10, "u", "ООО <Строй>", 100.0, "USD", "c")

    text = bd.build_text(_run(bd.gather()))
    assert "&lt;Строй&gt;" in text
    assert "<Строй>" not in text


def test_rich_message_is_the_happy_path(isolated_db, monkeypatch):
    db = isolated_db
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")
    bot = _FakeBot()
    bd = _patch_bot(monkeypatch, bot)

    data = _run(bd.gather())
    assert _run(bd.send_report(2, data)) == "rich"
    assert bot.rich_calls and bot.rich_calls[0][0] == 2


def test_falls_back_to_text_when_rich_fails(isolated_db, monkeypatch):
    db = isolated_db
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")
    bot = _FakeBot(rich_error=RuntimeError("METHOD_NOT_AVAILABLE"))
    bd = _patch_bot(monkeypatch, bot)

    sent = []

    async def _send(chat_id, text, **kw):
        sent.append((chat_id, text))
        return True

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "tg_send_message", _send)

    data = _run(bd.gather())
    assert _run(bd.send_report(2, data)) == "text"
    assert sent and sent[0][0] == 2
    assert "Решения за день" in sent[0][1]


def test_fallback_never_logs_the_token(isolated_db, monkeypatch, caplog):
    import config

    db = isolated_db
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", TOKEN)
    bot = _FakeBot(rich_error=RuntimeError(f"POST /bot{TOKEN}/sendRichMessage failed"))
    bd = _patch_bot(monkeypatch, bot)

    async def _send(chat_id, text, **kw):
        return True

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "tg_send_message", _send)

    with caplog.at_level(logging.WARNING):
        _run(bd.send_report(2, _run(bd.gather())))
    assert TOKEN not in caplog.text
    assert "***" in caplog.text


# ─── Кнопка WebApp ───────────────────────────────────────────────────────────


def test_webapp_button_targets_decisions_screen_with_fallback(monkeypatch):
    import config

    from services import boss_digest as bd

    monkeypatch.setattr(config, "WEBAPP_URL", "https://app.example.com")
    markup = bd.webapp_reply_markup()
    url = markup["inline_keyboard"][0][0]["web_app"]["url"]
    assert "screen=decisions" in url
    assert "fallback_screen=money" in url  # money%3Aconfirm, URL-encoded


def test_webapp_button_absent_without_https(monkeypatch):
    import config

    from services import boss_digest as bd

    monkeypatch.setattr(config, "WEBAPP_URL", "")
    assert bd.webapp_reply_markup() is None


# ─── cron: python -m tasks.run_boss_digest ─────────────────────────────────


def test_cron_skips_when_too_early(isolated_db, monkeypatch):
    from datetime import datetime

    import tasks.run_boss_digest as task

    isolated_db.set_setting("boss_digest_time", "19:00")
    monkeypatch.setattr("utils.helpers.local_now", lambda: datetime(2026, 9, 15, 10, 0))
    bot = _FakeBot()
    _patch_bot(monkeypatch, bot)

    assert _run(task.main()) == 0
    assert bot.rich_calls == []


def test_cron_sends_once_and_is_idempotent_on_retry(isolated_db, monkeypatch):
    from datetime import datetime

    import tasks.run_boss_digest as task

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")
    db.set_setting("boss_digest_time", "19:00")
    monkeypatch.setattr("utils.helpers.local_now", lambda: datetime(2026, 9, 15, 19, 5))
    bot = _FakeBot()
    _patch_bot(monkeypatch, bot)

    assert _run(task.main()) == 0
    assert len(bot.rich_calls) == 1

    # Ретрай того же 15-минутного тика — дайджест уже отмечен сегодняшним.
    assert _run(task.main()) == 0
    assert len(bot.rich_calls) == 1


def test_cron_skips_when_nothing_to_report(isolated_db, monkeypatch):
    from datetime import datetime

    import tasks.run_boss_digest as task

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.set_setting("boss_digest_time", "19:00")
    monkeypatch.setattr("utils.helpers.local_now", lambda: datetime(2026, 9, 15, 19, 5))
    bot = _FakeBot()
    _patch_bot(monkeypatch, bot)

    assert _run(task.main()) == 0
    assert bot.rich_calls == []
