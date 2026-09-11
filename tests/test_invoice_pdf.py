"""Печатная форма накладной: сборка HTML, экранирование, доставка в Telegram.

HTML-сборщик — чистая функция, её проверяем подробно. Сам рендер weasyprint
дёргаем одним smoke-тестом и пропускаем, если системных pango/cairo нет.
Доставку тестируем с фейковым ботом: мокаем границу (Telegram), не свой код.
"""

import asyncio

import pytest

from services import invoice_pdf


def _invoice(**over):
    inv = {
        "id": 1,
        "type": "outgoing",
        "invoice_number": "OUT-2026-0001",
        "invoice_date": "2026-09-11",
        "currency": "USD",
        "status": "confirmed",
        "total_amount_cents": 75000,
        "counterparty_id": 1,
        "counterparty_name": "ООО Ромашка",
        "counterparty_telegram_id": 555,
        "warehouse_name": "Основной склад",
        "comment": None,
        "telegram_sent": 0,
        "items": [
            {
                "product_name": "Болт М8",
                "sku": "B8",
                "unit": "шт",
                "quantity": 3.0,
                "price_cents": 25000,
            }
        ],
    }
    inv.update(over)
    return inv


# ─── HTML ─────────────────────────────────────────────────────────────────────


def test_html_contains_header_and_totals():
    html = invoice_pdf.build_invoice_html(_invoice())
    assert "РАСХОДНАЯ НАКЛАДНАЯ" in html
    assert "OUT-2026-0001" in html
    assert "ООО Ромашка" in html
    assert "750.00" in html  # итог
    assert "250.00" in html  # цена за единицу


def test_html_escapes_user_content():
    """Имя товара и контрагента экранируются.

    «Уголок 50<60» без экранирования открывает несуществующий тег и ломает
    вёрстку документа; комментарий с разметкой подменяет её целиком.
    """
    html = invoice_pdf.build_invoice_html(
        _invoice(
            counterparty_name="ООО <Ромашка> & Co",
            comment='<script>alert("x")</script>',
            items=[
                {
                    "product_name": "Уголок 50<60",
                    "sku": 'A"1',
                    "unit": "шт",
                    "quantity": 1,
                    "price_cents": 100,
                }
            ],
        )
    )
    assert "&lt;Ромашка&gt;" in html
    assert "Уголок 50&lt;60" in html
    assert "<script>" not in html
    assert "&amp; Co" in html


def test_html_line_total_uses_decimal_rounding():
    """Сумма строки — money.mul_qty, а не float-умножение."""
    html = invoice_pdf.build_invoice_html(
        _invoice(
            total_amount_cents=9999,
            items=[
                {
                    "product_name": "X",
                    "sku": "",
                    "unit": "шт",
                    "quantity": 3,
                    "price_cents": 3333,
                }
            ],
        )
    )
    assert "99.99" in html


def test_html_quantity_has_no_trailing_zeros():
    html = invoice_pdf.build_invoice_html(_invoice())
    assert ">3<" in html.replace(" ", "")
    frac = invoice_pdf.build_invoice_html(
        _invoice(
            items=[
                {"product_name": "X", "sku": "", "unit": "кг",
                 "quantity": 2.5, "price_cents": 100}
            ]
        )
    )
    assert "2.5" in frac


def test_html_marks_cancelled_invoice():
    assert "ОТМЕНЕНА" in invoice_pdf.build_invoice_html(_invoice(status="cancelled"))
    assert "ОТМЕНЕНА" not in invoice_pdf.build_invoice_html(_invoice())


def test_html_without_logo_still_renders():
    html = invoice_pdf.build_invoice_html(_invoice(), logo_data_uri=None)
    assert "<img" not in html
    assert "FARID IMPEKS" in html or invoice_pdf.COMPANY_NAME in html


def test_html_with_logo_embeds_data_uri():
    html = invoice_pdf.build_invoice_html(_invoice(), logo_data_uri="data:image/png;base64,AAA")
    assert 'src="data:image/png;base64,AAA"' in html


def test_missing_logo_file_is_not_fatal(monkeypatch, tmp_path):
    """Отсутствующий файл логотипа не должен ронять выписку документа."""
    monkeypatch.setattr(invoice_pdf, "_logo_cache", None)
    monkeypatch.setenv("INVOICE_LOGO_PATH", str(tmp_path / "nope.png"))
    assert invoice_pdf._logo_data_uri() is None


def test_logo_file_is_read_and_cached(monkeypatch, tmp_path):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    monkeypatch.setattr(invoice_pdf, "_logo_cache", None)
    monkeypatch.setenv("INVOICE_LOGO_PATH", str(logo))
    uri = invoice_pdf._logo_data_uri()
    assert uri and uri.startswith("data:image/png;base64,")
    logo.unlink()
    assert invoice_pdf._logo_data_uri() == uri  # второй вызов — из кэша


def test_filename_is_filesystem_safe():
    assert invoice_pdf.invoice_filename(_invoice()) == "OUT-2026-0001.pdf"
    assert invoice_pdf.invoice_filename({"invoice_number": "../../etc/passwd"}) == "etcpasswd.pdf"


# ─── Рендер ───────────────────────────────────────────────────────────────────


def test_render_produces_pdf():
    weasyprint = pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    assert weasyprint
    pdf = invoice_pdf.render_invoice_pdf(_invoice())
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000


# ─── Доставка ─────────────────────────────────────────────────────────────────


class _FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_document(self, chat_id, document, caption=None):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append({"chat_id": chat_id, "caption": caption})


@pytest.fixture
def delivery(isolated_db, monkeypatch):
    import importlib

    import services.invoice_delivery as d
    import services.warehouse as warehouse

    importlib.reload(warehouse)
    importlib.reload(d)
    # mark_telegram_sent пишет в БД по id; в юнит-тестах доставки накладной
    # там нет — глушим, поведение отметки проверяется в API-тестах.
    monkeypatch.setattr(warehouse, "mark_telegram_sent", _noop)
    monkeypatch.setattr(d.warehouse, "mark_telegram_sent", _noop)
    return d


async def _noop(*a, **k):
    return True


def test_delivery_skips_incoming(delivery):
    res = asyncio.run(delivery.deliver_invoice_pdf(_invoice(type="incoming"), _FakeBot()))
    assert res == {"sent": False, "reason": "not_outgoing"}


def test_delivery_without_telegram_id_warns_not_fails(delivery):
    """Нет telegram_id — накладная остаётся проведённой, менеджер видит warning."""
    bot = _FakeBot()
    res = asyncio.run(
        delivery.deliver_invoice_pdf(_invoice(counterparty_telegram_id=None), bot)
    )
    assert res == {"sent": False, "reason": "no_telegram_id"}
    assert bot.sent == []
    assert "вручную" in delivery.REASON_TEXT["no_telegram_id"]


def test_delivery_sends_pdf(delivery):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    bot = _FakeBot()
    res = asyncio.run(delivery.deliver_invoice_pdf(_invoice(), bot))
    assert res["sent"] is True
    assert bot.sent[0]["chat_id"] == 555
    assert "OUT-2026-0001" in bot.sent[0]["caption"]


def test_delivery_does_not_resend_already_sent(delivery):
    bot = _FakeBot()
    res = asyncio.run(delivery.deliver_invoice_pdf(_invoice(telegram_sent=1), bot))
    assert res == {"sent": False, "reason": "already_sent"}
    assert bot.sent == []


def test_delivery_force_resends(delivery):
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    bot = _FakeBot()
    res = asyncio.run(delivery.deliver_invoice_pdf(_invoice(telegram_sent=1), bot, force=True))
    assert res["sent"] is True
    assert len(bot.sent) == 1


def test_telegram_failure_is_swallowed(delivery):
    """Сбой Telegram не бросает исключение — накладная уже проведена."""
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    res = asyncio.run(delivery.deliver_invoice_pdf(_invoice(), _FakeBot(fail=True)))
    assert res == {"sent": False, "reason": "send_failed"}


def test_render_failure_is_swallowed(delivery, monkeypatch):
    """Не собрался PDF (нет системных библиотек) — тоже только предупреждение."""
    import services.invoice_pdf as pdfmod

    def _boom(_inv):
        raise RuntimeError("no pango")

    monkeypatch.setattr(pdfmod, "render_invoice_pdf", _boom)
    res = asyncio.run(delivery.deliver_invoice_pdf(_invoice(), _FakeBot()))
    assert res == {"sent": False, "reason": "render_failed"}
