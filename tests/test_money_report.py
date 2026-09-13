"""
Отчёт «Где деньги» в чат.

Главное здесь — не разметка, а доставка: Rich Message это надстройка, и при
любом её отказе руководство обязано получить те же цифры текстом. Мок стоит на
ГРАНИЦЕ с Telegram (aiogram.Bot), а не на нашей функции отправки — иначе тест
проверял бы сам себя.
"""

import asyncio
import importlib
import logging

import services.roles as roles

TOKEN = "123456:AAH-secret-bot-token"


def _run(coro):
    return asyncio.run(coro)


def _setup(db):
    roles.invalidate_all_roles()
    db.set_role(1, "mgr", "Manager", "manager")
    db.set_role(2, "boss", "Boss", "boss")


def _credit(vin="A-1", price=2_500_000, down=500_000, months=5, buyer="Иванов"):
    from services import machines

    m = _run(machines.create_machine(vin=vin, name="JCB", created_by=2, price_cents=price))
    assert m["ok"], m
    deal = _run(machines.create_deal(
        m["machine_id"], kind="credit", price_cents=price, buyer_name=buyer,
        created_by=2, down_payment_cents=down, months=months,
    ))
    assert deal["ok"], deal
    return deal["deal_id"]


class _FakeBot:
    """Граница с Telegram."""

    def __init__(self, *, rich_error=None):
        self.rich_error = rich_error
        self.rich_calls = []

    async def send_rich_message(self, chat_id, rich_message, **kw):
        if self.rich_error:
            raise self.rich_error
        self.rich_calls.append((chat_id, rich_message))
        return object()


def _patch_bot(monkeypatch, bot):
    import services.money_report as mr
    import webapp.server as server

    async def _get():
        return bot

    monkeypatch.setattr(server, "get_notify_bot", _get)
    return mr


# ─── Сбор данных ──────────────────────────────────────────────────────────────


def test_empty_report_is_not_sent(isolated_db):
    """Еженедельное «долгов нет» превращает сводку в шум, который перестают
    читать вместе с той неделей, когда цифры важны."""
    from services import money_report

    db = isolated_db
    _setup(db)
    data = _run(money_report.gather())
    assert money_report.is_empty(data) is True


def test_report_gathers_both_streams(isolated_db):
    from services import money_report

    db = isolated_db
    _setup(db)
    _credit()

    data = _run(money_report.gather())
    assert money_report.is_empty(data) is False
    assert data["totals"]["machines"]["count"] == 5
    assert data["totals"]["all"]["base_total"] == 20000
    assert data["top"][0]["name"] == "Иванов"


# ─── Разметка ─────────────────────────────────────────────────────────────────


def test_blocks_carry_a_table_of_buckets(isolated_db):
    from services import money_report

    db = isolated_db
    _setup(db)
    _credit()

    blocks = money_report.build_blocks(_run(money_report.gather()))
    kinds = [b.type for b in blocks]
    assert "heading" in kinds
    assert "table" in kinds


def test_empty_buckets_stay_out_of_the_table(isolated_db):
    """В чате пустая строка занимает столько же места, сколько содержательная."""
    from services import money_report

    db = isolated_db
    _setup(db)
    _credit()

    blocks = money_report.build_blocks(_run(money_report.gather()))
    table = next(b for b in blocks if b.type == "table")
    labels = [row[0].text for row in table.cells[1:]]
    assert labels  # хоть одна корзина есть
    assert "Просрочено >90 дней" not in labels  # график только что создан


def test_text_fallback_escapes_names(isolated_db):
    """Контрагент «ООО <Строй>» иначе рушит HTML-разметку сообщения целиком."""
    from services import money_report

    db = isolated_db
    _setup(db)
    _credit(buyer="ООО <Строй>")

    text = money_report.build_text(_run(money_report.gather()))
    assert "&lt;Строй&gt;" in text
    assert "<Строй>" not in text


def test_text_fallback_has_the_same_numbers(isolated_db):
    from services import money_report

    db = isolated_db
    _setup(db)
    _credit()

    data = _run(money_report.gather())
    text = money_report.build_text(data)
    assert "Где деньги" in text
    assert "20 000" in text.replace(" ", " ").replace("\xa0", " ")


# ─── Доставка ─────────────────────────────────────────────────────────────────


def test_rich_message_is_the_happy_path(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    _credit()
    bot = _FakeBot()
    mr = _patch_bot(monkeypatch, bot)

    data = _run(mr.gather())
    assert _run(mr.send_report(2, data)) == "rich"
    assert bot.rich_calls and bot.rich_calls[0][0] == 2


def test_falls_back_to_text_when_rich_fails(isolated_db, monkeypatch):
    """Отчёт, который не дошёл, хуже некрасивого."""
    db = isolated_db
    _setup(db)
    _credit()
    bot = _FakeBot(rich_error=RuntimeError("METHOD_NOT_AVAILABLE"))
    mr = _patch_bot(monkeypatch, bot)

    sent = []

    async def _send(chat_id, text, **kw):
        sent.append((chat_id, text))
        return True

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "tg_send_message", _send)

    data = _run(mr.gather())
    assert _run(mr.send_report(2, data)) == "text"
    assert sent and sent[0][0] == 2
    assert "Где деньги" in sent[0][1]


def test_fallback_never_logs_the_token(isolated_db, monkeypatch, caplog):
    """Текст ошибки Bot API может содержать токен — в лог он идёт только через
    redact_token."""
    import config

    db = isolated_db
    _setup(db)
    _credit()
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", TOKEN)
    bot = _FakeBot(rich_error=RuntimeError(f"POST /bot{TOKEN}/sendRichMessage failed"))
    mr = _patch_bot(monkeypatch, bot)

    async def _send(chat_id, text, **kw):
        return True

    import services.notifier as notifier

    monkeypatch.setattr(notifier, "tg_send_message", _send)

    with caplog.at_level(logging.WARNING):
        _run(mr.send_report(2, _run(mr.gather())))
    assert TOKEN not in caplog.text
    assert "***" in caplog.text


def test_cron_skips_when_nothing_to_report(isolated_db, monkeypatch):
    db = isolated_db
    _setup(db)
    importlib.reload(roles)

    import tasks.run_money_report as task

    bot = _FakeBot()
    _patch_bot(monkeypatch, bot)
    assert _run(task.main()) == 0
    assert bot.rich_calls == []
