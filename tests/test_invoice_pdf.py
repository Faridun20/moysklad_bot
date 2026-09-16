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
    assert "Товарная накладная" in html and "Товар накладноси" in html  # по умолчанию рус + узб
    assert "OUT-2026-0001" in html
    assert "«11» сентября 2026 г." in html
    assert "ООО Ромашка" in html
    # Числа — по-русски: пробел разделяет тысячи, запятая — копейки.
    assert "750,00" in html  # итог
    assert "250,00" in html  # цена за единицу
    assert "Всего отпущено на сумму:" in html and "Жами топширилди:" in html
    assert "Цена за ед. (USD)" in html and "Нархи (USD)" in html
    assert "(семьсот пятьдесят) долларов США." in html
    assert "(етти юз эллик) АҚШ доллари." in html


def test_html_names_the_party_by_direction():
    """«Контрагент» одинаково называл того, кто покупает, и того, у кого
    покупаем мы. В накладной сторона называется по существу: расход —
    грузоотправитель и грузополучатель (бланк владельца), приход — поставщик
    и склад, без слов отгрузки, которые в приходе читались бы наоборот."""
    out = invoice_pdf.build_invoice_html(_invoice(doc_lang="ru"))
    assert "Грузоотправитель" in out and "Грузополучатель" in out and "Контрагент" not in out
    incoming = invoice_pdf.build_invoice_html(_invoice(type="incoming", doc_lang="ru_uz"))
    assert "Поставщик" in incoming and "Контрагент" not in incoming
    assert "Грузо" not in incoming and "Юк " not in incoming, "приход — внутренний, только по-русски"
    assert "Приходная накладная" in incoming


def _outgoing_full(**over):
    base = dict(
        doc_lang="ru_uz",
        currency="UZS", total_amount_cents=109_200_000,
        items=[{"product_name": "Болт М8", "sku": "", "unit": "шт", "quantity": 1092, "price_cents": 100_000}],
        company={"company_name": "ООО «FARID IMPEKS»", "company_address": "Ташкент, ул. Амира Темура, 107Б",
                 "company_phone": "+998 71 200-00-00", "company_director": "Масуджанов Фаридун"},
        buyer={"address": "Самарканд, ул. Регистан, 1", "phone": "+998 90 123-45-67"},
        basis={"order_id": 31, "date": "2026-09-16 10:00:00"},
    )
    base.update(over)
    return _invoice(**base)


def test_waybill_prints_basis_only_for_an_order():
    """«Основание: Счёт на оплату № {заказ} от {даты заказа}» — у накладной,
    выписанной отгрузкой заказа. Накладная со склада без заказа основания не
    печатает (строки нет вовсе, а не «Счёт № ___»)."""
    html = invoice_pdf.build_invoice_html(_outgoing_full())
    assert "Основание: Счёт на оплату № 31 от «16» сентября 2026 г." in html
    assert "Асос: Тўлов учун ҳисоб № 31 «16» сентябрь 2026 йилги" in html
    standalone = invoice_pdf.build_invoice_html(_outgoing_full(basis=None))
    assert "Основание" not in standalone and "Асос:" not in standalone


def test_waybill_sender_receiver_words_and_signatures():
    html = invoice_pdf.build_invoice_html(_outgoing_full())
    # Грузоотправитель — наша компания, грузополучатель — клиент.
    assert "ООО «FARID IMPEKS»" in html and "Ташкент, ул. Амира Темура, 107Б" in html
    assert "Самарканд, ул. Регистан, 1" in html and "+998 90 123-45-67" in html
    # Сумы — целыми, валюта сумовая, пропись на языке страницы.
    assert "1 092 000 (один миллион девяносто две тысячи) сум." in html
    assert "1 092 000 (бир миллион тўқсон икки минг) сўм." in html
    assert "Цена за ед. (сум)" in html and "Нархи (сўм)" in html
    # «Отпуск разрешил» — руководитель, если отдельного не задали.
    assert "Отпуск разрешил:" in html and "Масуджанов Фаридун" in html
    assert "Отпустил:" in html and "Груз получил:" in html
    assert "Юк беришга рухсат берди:" in html and "Юкни қабул қилди:" in html
    assert "электронную товарно-транспортную накладную (ЭТТН)" in html
    assert "электрон товар-транспорт накладнойси (ЭТТН)ни алмаштирмайди" in html


@pytest.mark.parametrize("lang,ru,uz", [("ru_uz", True, True), ("ru", True, False), ("uz", False, True)])
def test_waybill_language_choice(lang, ru, uz):
    html = invoice_pdf.build_invoice_html(_outgoing_full(doc_lang=lang))
    assert ("Товарная накладная" in html) is ru
    assert ("Товар накладноси" in html) is uz
    assert html.count('<section class="doc">') == int(ru) + int(uz)


def test_waybill_empty_requisites_print_a_line_for_handwriting():
    """Накладная не блокируется пустым реквизитом: вместо него — черта."""
    html = invoice_pdf.build_invoice_html(_invoice(doc_lang="ru", company={}, buyer={}))
    assert "Адрес:" in html and 'class="blank blank--wide"' in html


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
    assert "99,99" in html


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
    assert "2,5" in frac


def test_html_marks_cancelled_invoice():
    assert "ОТМЕНЕНА" in invoice_pdf.build_invoice_html(_invoice(status="cancelled"))
    assert "БЕКОР ҚИЛИНГАН" in invoice_pdf.build_invoice_html(_invoice(status="cancelled", doc_lang="uz"))
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


@pytest.mark.parametrize("lang,pages", [("ru_uz", 2), ("ru", 1), ("uz", 1)])
def test_render_waybill_pages_per_language(lang, pages):
    """Рус + узб — две страницы одного PDF; один язык — одна. Текст страниц
    читается из самого PDF, а не из HTML."""
    pytest.importorskip("weasyprint", reason="нет weasyprint/системных pango")
    import io

    from pypdf import PdfReader

    pdf = invoice_pdf.render_invoice_pdf(_outgoing_full(doc_lang=lang))
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == pages
    text = " ".join(" ".join(p.extract_text().split()) for p in reader.pages)
    assert "OUT-2026-0001" in text
    if lang != "uz":
        assert "Основание: Счёт на оплату № 31" in text
        assert "один миллион девяносто две тысячи" in text


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
