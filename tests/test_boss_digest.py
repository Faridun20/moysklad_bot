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


def test_payment_at_or_above_threshold_is_marked_not_dropped(isolated_db):
    """Крупный платёж уже ушёл немедленным пушем (notify_policy) — дайджест
    его не дублирует свежим пунктом, но и не выбрасывает молча: финдинг #6
    (аудит) — событие может провалиться между мгновенной карточкой и
    дайджестом, если порог/курс сменился ПОСЛЕ создания. Показываем ВСЁ
    ждущее, крупное — с пометкой «уже показывали»."""
    from services import boss_digest as bd

    db = isolated_db
    db.add_payment(10, "u", "Мелкий", 100.0, "USD", "c")
    db.add_payment(10, "u", "Крупный", 9000.0, "USD", "c")

    data = _run(bd.gather())
    assert data["payments"]["count"] == 2
    small_line = next(line for line in data["payments"]["lines"] if "Мелкий" in line)
    big_line = next(line for line in data["payments"]["lines"] if "Крупный" in line)
    assert "уже показывали" not in small_line
    assert "уже показывали" in big_line
    assert data["payments"]["waiting_total"] == 2


def test_threshold_change_after_creation_does_not_drop_pending_payment(isolated_db):
    """Финдинг #6 сценарий: платёж создан ниже старого порога (остался
    pending, мгновенная карточка не уходила), владелец ПОНИЗИЛ порог до
    дайджеста — раньше платёж перефильтровывался ТЕКУЩИМ порогом, «выглядел»
    уже отправленным и пропадал из дайджеста НАСОВСЕМ (ни карточкой, ни
    сводкой). Теперь он всё равно виден — с пометкой «уже показывали»."""
    from services import boss_digest as bd

    db = isolated_db
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")  # порог 5000 по умолчанию — pending
    db.set_setting("boss_instant_threshold_usd", 10.0)  # понизили уже ПОСЛЕ создания

    data = _run(bd.gather())
    assert data["payments"]["count"] == 1
    assert "Иван" in data["payments"]["lines"][0]


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


def test_pending_cash_payment_part_excluded_from_payments_block(isolated_db):
    """Наличная строка разбивки подтверждается СДАЧЕЙ, а не кнопкой
    подтверждения — как и `get_money_totals`, дайджест не должен показывать
    её в «Платежах на подтверждение»: иначе она дублирует «Сдачи» (одни и те
    же наличные посчитаны и там, и там)."""
    from services import boss_digest as bd
    from services import order_payments

    db = isolated_db
    db.set_role(10, "u", "Manager", "manager")
    oid = db.create_order(10, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    actor = order_payments.Actor(user_id=10, name="Manager", role="manager")
    _run(order_payments.record_payment_parts(
        oid, actor, [{"method": "cash", "currency": "USD", "amount": "100"}]
    ))

    data = _run(bd.gather())
    assert data["payments"]["count"] == 0
    assert bd.is_empty(data) is True


def test_confirmed_cash_deposit_part_excluded_from_received(isolated_db):
    """Подтверждённая наличная строка разбивки не считается в «Получено» —
    эти деньги уже посчитаны в «Сдачах» (`get_money_totals` не считает
    наличные строки платежами по той же причине)."""
    from services import boss_digest as bd
    from services import order_payments

    db = isolated_db
    db.set_role(10, "u", "Manager", "manager")
    db.set_role(1, "b", "Boss", "boss")
    oid = db.create_order(10, "Manager", "")
    db.update_order_agent(oid, "A-1", "Клиент")
    db.add_order_item(oid, "Товар", "", 1, "шт", 100.0)
    db.update_order_status(oid, "approved")
    actor = order_payments.Actor(user_id=10, name="Manager", role="manager")
    _run(order_payments.record_payment_parts(
        oid, actor, [{"method": "cash", "currency": "USD", "amount": "100"}]
    ))
    dep = _run(db.create_cash_deposit(10, 100.0))
    assert _run(db.confirm_cash_deposit(dep["deposit_id"], 1, "Boss"))["ok"]

    data = _run(bd.gather())
    assert data["received"]["count"] == 0


def test_pending_return_uses_order_currency_not_hardcoded_usd(isolated_db):
    """Сумма возврата в дайджесте — в валюте ЗАКАЗА (аудит, финдинг #5), а не
    всегда USD: хардкод "USD" считал 6000 сум (~$0.47) как $6000 (выше порога
    $5000) и молча исключал возврат из дайджеста, думая, что он уже ушёл
    мгновенной карточкой — хотя на самом деле он не ушёл никуда."""
    from services import boss_digest as bd

    db = isolated_db
    db.set_role(10, "u", "Manager", "manager")
    assert db.set_currency_rate("UZS", 1 / 12700, updated_by=1)[0]
    oid = db.create_order(10, "Manager", "")
    assert db.update_order_currency(oid, "UZS", require_draft=True)
    with db.get_conn() as conn:
        cur = db.get_cursor(conn)
        cur.execute(
            db.q(
                "INSERT INTO returns (order_id, return_type, reason, total_amount_cents, "
                "created_by, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)"
            ),
            (oid, "full", "брак", 600000, 10, db.now_str()),  # 6000.00 UZS
        )
        conn.commit()

    data = _run(bd.gather())
    assert data["returns"]["count"] == 1
    assert "UZS" in data["returns"]["lines"][0]
    assert "USD" not in data["returns"]["lines"][0]


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


# ─── boss_digest_time: валидация HH:MM ──────────────────────────────────────


def test_invalid_digest_time_falls_back_to_default_with_warning(isolated_db, caplog):
    """«25:00» не парсится в разумные часы/минуты — дайджест обязан упасть на
    дефолт 19:00 (иначе `is_due()` навсегда считает «ещё не время»: у суток
    нет часа 25, (now.hour, now.minute) < (25, 0) истинно всегда)."""
    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "25:00")
    with caplog.at_level(logging.WARNING):
        assert bd.digest_time_hhmm() == (19, 0)
    assert "boss_digest_time" in caplog.text


def test_digest_time_after_2345_falls_back_to_default_with_warning(isolated_db, caplog):
    """Крон тикает по :00/:15/:30/:45 — время дайджеста позже 23:45 не
    гарантирует ни одного тика в пределах того же дня до полуночи, и
    is_due() либо никогда не сработает в свой день, либо сработает уже
    следующим числом. Падаем на дефолт."""
    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "23:50")
    with caplog.at_level(logging.WARNING):
        assert bd.digest_time_hhmm() == (19, 0)
    assert "boss_digest_time" in caplog.text


def test_digest_time_exactly_2345_is_accepted(isolated_db):
    """Граница включительно — 23:45 совпадает с последним тиком дня."""
    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "23:45")
    assert bd.digest_time_hhmm() == (23, 45)


def test_garbage_digest_time_falls_back_to_default(isolated_db, caplog):
    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "не время")
    with caplog.at_level(logging.WARNING):
        assert bd.digest_time_hhmm() == (19, 0)
    assert "boss_digest_time" in caplog.text


def test_negative_minutes_fall_back_to_default(isolated_db):
    from services import boss_digest as bd

    isolated_db.set_setting("boss_digest_time", "10:-5")
    assert bd.digest_time_hhmm() == (19, 0)


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


def test_send_report_returns_failed_when_both_channels_fail(isolated_db, monkeypatch):
    """Rich упал, а текстовый фолбэк `tg_send_message` тоже вернул False
    (Telegram недоступен целиком) — send_report обязан сказать об этом
    вызывающему, а не притвориться, что дайджест ушёл текстом."""
    db = isolated_db
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")
    bot = _FakeBot(rich_error=RuntimeError("METHOD_NOT_AVAILABLE"))
    bd = _patch_bot(monkeypatch, bot)

    async def _send(chat_id, text, **kw):
        return False

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "tg_send_message", _send)

    data = _run(bd.gather())
    assert _run(bd.send_report(2, data)) == "failed"


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


def test_webapp_button_opens_decisions_by_canonical_deep_link(monkeypatch):
    """Кнопка дайджеста — тот же deep link, что у остальных уведомлений
    (`utils.keyboards.webapp_screen_url`, `?startapp=decisions`), а не свой
    `?screen=…&fallback_screen=…`, которого фронт не читает."""
    from urllib.parse import parse_qs, urlsplit

    import config

    from services import boss_digest as bd
    from utils.keyboards import DECISIONS_SCREEN, webapp_screen_url

    monkeypatch.setattr(config, "WEBAPP_URL", "https://app.example.com/?v=3")
    markup = bd.webapp_reply_markup()
    url = markup["inline_keyboard"][0][0]["web_app"]["url"]
    assert url == webapp_screen_url(DECISIONS_SCREEN)
    query = parse_qs(urlsplit(url).query)
    assert query == {"v": ["3"], "startapp": ["decisions"]}
    assert "fallback_screen" not in url


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


def test_cron_does_not_mark_run_when_delivery_fails_everywhere(isolated_db, monkeypatch):
    """День потерян, если дайджест «отправлен» отметкой, хотя ни один босс
    его не получил (Rich упал, текстовый фолбэк тоже вернул False). rc=1,
    last_run_at не тронут — следующий 15-минутный тик обязан попробовать
    снова, а не молчать до завтра (is_due() всё ещё True)."""
    from datetime import datetime

    import tasks.run_boss_digest as task

    db = isolated_db
    db.set_role(1, "b", "Boss", "boss")
    db.add_payment(10, "u", "Иван", 100.0, "USD", "c")
    db.set_setting("boss_digest_time", "19:00")
    monkeypatch.setattr("utils.helpers.local_now", lambda: datetime(2026, 9, 15, 19, 5))
    bot = _FakeBot(rich_error=RuntimeError("METHOD_NOT_AVAILABLE"))
    _patch_bot(monkeypatch, bot)

    async def _send(chat_id, text, **kw):
        return False

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "tg_send_message", _send)

    rc = _run(task.main())
    assert rc == 1

    from services import boss_digest as bd

    assert bd.is_due() is True


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
