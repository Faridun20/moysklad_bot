"""E2E, покрытие раздела «Склад» кнопка за кнопкой.

`test_stock.py` прошёл главные пути (приход и отмена, контейнер от заведения до
оприходования, техника до первого платежа). Здесь — всё остальное, что есть на
экране: фильтры и категории каталога, фото и посты, правка и удаление
контейнера, привязка позиций, архив и рассрочка с поступлениями, расход с PDF
клиенту, отказы по ролям. Проверяем последствия — остатки, строки в БД,
отправленное ботом, — а не то, что кнопка нарисовалась.

Граница с Telegram для фото и канала подменяется здесь же (`_photo_storage`):
`FakeBot` из каркаса умеет только сообщения и документы, а загрузка фото — это
`send_photo`, чтение — `get_file`/`download_file`.
"""

from __future__ import annotations

import io
import json
import struct
import zlib
from types import SimpleNamespace

from tests.e2e.conftest import alerts, go, nav_screens, settled, sheet_fill, tab

import pytest

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")

# ─── Хелперы ─────────────────────────────────────────────────────────────────


def _stock(e2e, pid: int | None = None) -> float:
    rows = e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (pid or e2e.ids["product"],))
    return rows[0]["quantity"] if rows else 0


def _post(page, path: str, body: dict | None = None) -> int:
    """HTTP-код ручки, вызванной из страницы с её initData — «отказ запрещённого»
    проверяется на сервере, а не отсутствием кнопки."""
    return page.evaluate(
        """async ([path, body]) => {
            const r = await fetch(path, {
              method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({initData: window.Telegram.WebApp.initData, ...body}),
            });
            return r.status;
        }""",
        [path, body or {}],
    )


def _png(w: int = 8, h: int = 8) -> bytes:
    """Настоящий PNG: снимок в браузере проходит через canvas, мусор он не съест."""
    raw = b"".join(b"\x00" + b"\xd0\x40\x20" * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


PNG = _png()


def _photo_storage(e2e, monkeypatch) -> list[dict]:
    """Канал-хранилище фото и сам Telegram — в память.

    `send_photo` отдаёт лесенку размеров, как настоящий Bot API; `get_file` +
    `download_file` возвращают тот же PNG — лента фото его и покажет.
    """
    monkeypatch.setenv("PHOTOS_TG_CHAT_ID", "-1001")
    sent: list[dict] = []

    async def send_photo(chat_id, photo, caption=None, **kw):
        n = len(sent) + 1
        sent.append({"chat_id": chat_id, "caption": caption, **kw})
        sizes = [
            SimpleNamespace(file_id=f"thumb-{n}", file_unique_id=f"thumb-u-{n}", width=2, height=2),
            SimpleNamespace(file_id=f"file-{n}", file_unique_id=f"e2e-uniq-{n}", width=8, height=8),
        ]
        return SimpleNamespace(photo=sizes, message_id=1000 + n)

    async def get_file(file_id):
        return SimpleNamespace(file_size=len(PNG), file_path=f"photos/{file_id}.png")

    async def download_file(path):
        return io.BytesIO(PNG)

    e2e.bot.send_photo = send_photo
    e2e.bot.get_file = get_file
    e2e.bot.download_file = download_file
    return sent


def _upload_via_picker(page, trigger: str) -> None:
    """Нажать «Фото» и отдать файл системному выбору файлов."""
    with page.expect_file_chooser() as fc:
        page.click(trigger)
    fc.value.set_files(files=[{"name": "shot.png", "mimeType": "image/png", "buffer": PNG}])


def _product(e2e, name: str, qty: float = 0, category: str | None = None) -> int:
    from services import container_receipt, warehouse

    pid = e2e.run(container_receipt.create_product(name))["product_id"]
    if category:
        e2e.exec("UPDATE products SET category = ? WHERE id = ?", (category, pid))
    if qty:
        e2e.run(warehouse.create_invoice(
            invoice_type="incoming", warehouse_id=e2e.ids["warehouse"],
            items=[{"product_id": pid, "quantity": qty, "price_cents": None}],
        ))
    return pid


def _cp_id(e2e) -> int:
    return e2e.rows("SELECT id FROM counterparties WHERE name = 'ООО Ромашка'")[0]["id"]


def _machine(e2e, vin: str, name: str, **kw) -> int:
    from services import machines

    res = e2e.run(machines.create_machine(vin=vin, name=name, created_by=e2e.ids["boss"], **kw))
    assert res.get("ok"), res
    return res["machine_id"]


def _credit(e2e, mid: int, *, price: int = 100_000, down: int = 10_000, months: int = 3) -> int:
    from services import machines

    res = e2e.run(machines.create_deal(
        mid, kind="credit", price_cents=price, buyer_name="Азиз Рахимов",
        buyer_passport="AA7654321", created_by=e2e.ids["boss"],
        down_payment_cents=down, months=months,
    ))
    assert res.get("ok"), res
    return res["deal_id"]


def _container(e2e, number: str = "TGHU1234567", *, notes: str | None = None) -> int:
    from services import containers

    res = e2e.run(containers.create_container(number=number, created_by=e2e.ids["boss"], notes=notes))
    assert res.get("ok"), res
    return res["container_id"]


def _item(e2e, cid: int, name: str, qty: float, product_id: int | None = None) -> int:
    from services import containers

    res = e2e.run(containers.add_item(cid, name=name, expected_qty=qty, unit="шт", product_id=product_id))
    assert res.get("ok"), res
    return e2e.rows("SELECT MAX(id) AS id FROM container_items")[0]["id"]


def _open_machine(page, mid: int) -> None:
    go(page, "stock")
    tab(page, "machines")
    page.wait_for_selector(f'[data-machine="{mid}"]')
    page.click(f'[data-machine="{mid}"]')
    page.wait_for_selector('[data-mact="hours"]')


def _open_container(page, cid: int) -> None:
    go(page, "stock")
    tab(page, "containers")
    page.wait_for_selector(f'[data-container="{cid}"]')
    page.click(f'[data-container="{cid}"]')
    page.wait_for_selector("#content .section-label")
    page.wait_for_function("() => !document.querySelector('#content .sk-label')")


def _no_overlay(page) -> None:
    page.wait_for_function("() => !document.querySelector('.c-overlay')")


def _publish_draft(page) -> str:
    """Черновик собран → предпросмотр → «Опубликовать» (confirm отвечает «да»)."""
    page.click("#ms-submit")
    page.wait_for_selector("#ms-f-text")
    # Шторка черновика закрылась; редактор цены (если пост из него) остаётся под.
    page.wait_for_function("() => document.querySelectorAll('.c-overlay:not(.price-overlay)').length === 1")
    text = page.input_value("#ms-f-text")
    page.click("#ms-submit")
    page.wait_for_selector(".toast:has-text('Опубликовано')")
    return text


# ─── Роли: вкладки и отказ запрещённого ──────────────────────────────────────


def test_stock_tabs_follow_role(open_app, e2e):
    """Руководство и менеджер видят четыре вкладки; у кладовщика и бухгалтера
    раздела нет вовсе — их ручки склада не отвечают."""
    for who in ("boss", "admin", "mgr"):
        page = open_app(e2e.ids[who])
        go(page, "stock")
        page.wait_for_selector(".seg-item[data-sect]")
        tabs = page.eval_on_selector_all(".seg-item[data-sect]", "els => els.map(e => e.dataset.sect)")
        assert tabs == ["catalog", "containers", "machines", "invoices"], (who, tabs)
        page.wait_for_selector(".stock-row")
        # Цены правит только руководство — у менеджера строка не кликается.
        assert (page.locator("[data-price-idx]").count() > 0) == (who != "mgr"), who

    for who in ("keeper", "book"):
        page = open_app(e2e.ids[who])
        screens = nav_screens(page)
        assert "stock" not in screens, (who, screens)


def test_keeper_and_bookkeeper_are_refused_by_stock_endpoints(open_app, e2e):
    pid = e2e.ids["product"]
    for who in ("keeper", "book"):
        page = open_app(e2e.ids[who])
        for path, body in [
            ("/api/stock", {}),
            ("/api/wh/invoices", {}),
            ("/api/wh/stock", {}),
            ("/api/machines/list", {}),
            ("/api/containers/list", {}),
            ("/api/containers/create", {"number": "KEEP1"}),
            ("/api/wh/invoices/create", {"type": "incoming",
                                         "items": [{"product_id": pid, "quantity": 5}]}),
        ]:
            assert _post(page, path, body) == 403, (who, path)
    assert _stock(e2e) == 20
    assert e2e.rows("SELECT COUNT(*) AS n FROM containers")[0]["n"] == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices")[0]["n"] == 1


def test_manager_is_refused_boss_only_stock_actions(open_app, e2e):
    ids = e2e.ids
    mid = _machine(e2e, "MGR-REFUSE", "Hitachi ZX200", status="in_stock", price_cents=3_000_000)
    deal = _credit(e2e, _machine(e2e, "MGR-CREDIT", "CAT 320", status="in_stock"))
    pay = e2e.rows("SELECT id FROM machine_deal_payments WHERE deal_id = ? AND seq = 1", (deal,))[0]["id"]
    cid = _container(e2e, "MGRDEL1")
    inv = e2e.rows("SELECT id FROM invoices")[0]["id"]

    mgr = open_app(ids["mgr"])
    for path, body in [
        ("/api/products/prices/set", {"product_id": ids["product"], "sale_price": 1}),
        ("/api/wh/invoices/create", {"type": "outgoing", "counterparty_id": _cp_id(e2e),
                                     "items": [{"product_id": ids["product"], "quantity": 1,
                                                "price_cents": 100}]}),
        ("/api/machines/status", {"machine_id": mid, "status": "reserved", "expected": "in_stock"}),
        ("/api/machines/update", {"machine_id": mid, "fields": {"name": "Взлом"}}),
        ("/api/machines/payment", {"payment_id": pay, "paid": False}),
        ("/api/machines/receipt_delete", {"receipt_id": 1}),
        ("/api/machines/deal_close", {"deal_id": deal}),
        ("/api/containers/delete", {"container_id": cid}),
        ("/api/channel/stale", {}),
        ("/api/channel/draft", {"kind": "showcase", "product_id": ids["product"]}),
        ("/api/products/photo_upload", {"product_id": ids["product"]}),
        ("/api/products/photo_delete", {"product_id": ids["product"], "photo_id": 1}),
    ]:
        assert _post(mgr, path, body) == 403, path

    assert _stock(e2e) == 20
    assert e2e.rows("SELECT COUNT(*) AS n FROM product_prices")[0]["n"] == 0
    assert e2e.rows("SELECT cancelled_at FROM invoices WHERE id = ?", (inv,))[0]["cancelled_at"] is None
    m = e2e.rows("SELECT name, status FROM machines WHERE id = ?", (mid,))[0]
    assert m == {"name": "Hitachi ZX200", "status": "in_stock"}
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_payment_receipts")[0]["n"] == 0
    assert e2e.rows("SELECT closed_at FROM machine_deals WHERE id = ?", (deal,))[0]["closed_at"] is None
    assert e2e.rows("SELECT COUNT(*) AS n FROM containers WHERE id = ?", (cid,))[0]["n"] == 1

    # Удаление и отмену накладной руководитель оставил себе — менеджеру 403.
    from services.database import set_setting

    set_setting("delete_requires_boss", True, ids["boss"])
    for path, body in [
        ("/api/wh/invoices/cancel", {"invoice_id": inv}),
        ("/api/machines/delete", {"machine_id": mid}),
    ]:
        assert _post(mgr, path, body) == 403, path
    assert e2e.rows("SELECT cancelled_at FROM invoices WHERE id = ?", (inv,))[0]["cancelled_at"] is None
    assert e2e.rows("SELECT COUNT(*) AS n FROM machines WHERE id = ?", (mid,))[0]["n"] == 1


def test_admin_has_boss_controls_in_stock(open_app, e2e):
    """Админ — то же руководство: цена, отмена накладной, «Залежалось»."""
    admin = open_app(e2e.ids["admin"])
    go(admin, "stock")
    admin.wait_for_selector("[data-stale]")
    admin.click("[data-price-idx]")
    admin.wait_for_selector("#pe-sale")
    admin.fill("#pe-sale", "7")
    admin.click("#pe-save")
    admin.wait_for_selector(".toast:has-text('Цена сохранена')")
    assert e2e.rows("SELECT sale_price_cents, updated_by FROM product_prices") == [
        {"sale_price_cents": 700, "updated_by": e2e.ids["admin"]}
    ]

    tab(admin, "invoices")
    inv = e2e.rows("SELECT id FROM invoices")[0]["id"]
    admin.wait_for_selector(f'[data-wh-cancel="{inv}"]')
    admin.click(f'[data-wh-cancel="{inv}"]')
    admin.wait_for_selector(".toast:has-text('Приход отменён')")
    assert e2e.rows("SELECT status FROM invoices WHERE id = ?", (inv,))[0]["status"] == "cancelled"
    assert _stock(e2e) == 0


# ─── Каталог ─────────────────────────────────────────────────────────────────


def test_catalog_categories_and_subcategories_filter_the_list(open_app, e2e):
    _product(e2e, "Адаптер гидравлический", 3, "Запчасти/Адаптер")
    _product(e2e, "Фильтр масляный", 4, "Запчасти/Фильтр")
    e2e.exec("UPDATE products SET category = 'Кабели' WHERE id = ?", (e2e.ids["product"],))

    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.wait_for_selector(".stock-row")
    assert boss.locator(".stock-row").count() == 3
    assert "Все (3)" in boss.inner_text('[data-cat="all"]')
    assert boss.locator("#stock-subcats").count() == 0, "у «Все» второго ряда нет"

    boss.click('[data-cat="Запчасти"]')
    boss.wait_for_selector("#stock-subcats [data-subcat]")
    boss.wait_for_function("() => document.querySelectorAll('.stock-row').length === 2")
    names = boss.eval_on_selector_all(".stock-name", "els => els.map(e => e.textContent)")
    assert sorted(names) == ["Адаптер гидравлический", "Фильтр масляный"]

    boss.click('[data-subcat="Запчасти/Фильтр"]')
    boss.wait_for_function("() => document.querySelectorAll('.stock-row').length === 1")
    assert boss.inner_text(".stock-name") == "Фильтр масляный"
    assert boss.get_attribute('[data-subcat="Запчасти/Фильтр"]', "aria-pressed") == "true"

    boss.click('#stock-subcats [data-subcat=""]')
    boss.wait_for_function("() => document.querySelectorAll('.stock-row').length === 2")

    # Категория без «/» — сама себе уровень, подкатегорий у неё нет.
    boss.click('[data-cat="Кабели"]')
    boss.wait_for_function("() => document.querySelectorAll('.stock-row').length === 1")
    assert boss.locator("#stock-subcats").count() == 0
    assert "Кабель" in boss.inner_text(".stock-name")

    # Поиск работает внутри категории, а не мимо неё.
    boss.click('[data-cat="Запчасти"]')
    boss.wait_for_selector("#stock-subcats")
    boss.fill("#stock-search", "адапт")
    boss.wait_for_function("() => document.querySelectorAll('.stock-row').length === 1")
    boss.fill("#stock-search", "кабель")
    boss.wait_for_selector(".stock-list:has-text('Товары не найдены')")


def test_catalog_in_stock_filter_hides_zero_stock(open_app, e2e):
    _product(e2e, "Ковш 0,8 куба")  # карточка есть, остатка нет
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    mgr.wait_for_function("() => document.querySelectorAll('.stock-row').length === 2")
    zero = mgr.locator(".stock-row:has-text('Ковш')")
    assert zero.locator('.stock-badge[data-status="out"]').inner_text() == "нет"

    mgr.click('[data-instock="1"]')
    mgr.wait_for_function("() => document.querySelectorAll('.stock-row').length === 1")
    assert "Кабель" in mgr.inner_text(".stock-name")
    assert mgr.get_attribute('[data-instock="1"]', "aria-pressed") == "true"
    assert mgr.get_attribute('[data-instock="0"]', "aria-pressed") == "false"

    mgr.click('[data-instock="0"]')
    mgr.wait_for_function("() => document.querySelectorAll('.stock-row').length === 2")


def test_catalog_show_more_reveals_the_rest(open_app, e2e):
    from services import container_receipt

    async def _many():
        for i in range(205):
            await container_receipt.create_product(f"Болт М{i:03d}")

    e2e.run(_many())
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.wait_for_selector("#stock-more")
    assert boss.locator(".stock-row").count() == 200
    assert "Показать ещё (6)" in boss.inner_text("#stock-more")
    boss.click("#stock-more")
    boss.wait_for_function("() => document.querySelectorAll('.stock-row').length === 206")
    assert boss.locator("#stock-more").count() == 0


def test_price_editor_cancel_and_escape_keep_prices_and_manager_sees_no_cost(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.wait_for_selector("[data-price-idx]")

    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-sale")
    boss.fill("#pe-sale", "99")
    boss.click("#pe-cancel")
    boss.wait_for_selector(".price-overlay", state="detached")

    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-sale")
    assert boss.input_value("#pe-sale") == "", "отменённое не осталось в форме"
    boss.fill("#pe-cost", "55")
    boss.keyboard.press("Escape")
    boss.wait_for_selector(".price-overlay", state="detached")
    assert e2e.rows("SELECT COUNT(*) AS n FROM product_prices")[0]["n"] == 0

    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-sale")
    boss.fill("#pe-sale", "14")
    boss.fill("#pe-cost", "10")
    boss.click("#pe-save")
    boss.wait_for_selector(".toast:has-text('Цена сохранена')")
    assert "себест. 10" in boss.inner_text(".stock-row")

    # Сброс себестоимости: пустое поле — null, а не ноль.
    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-cost")
    assert boss.input_value("#pe-cost") == "10"
    boss.fill("#pe-cost", "")
    boss.click("#pe-save")
    boss.wait_for_function("() => !document.querySelector('.price-overlay')")
    boss.wait_for_function("() => !document.querySelector('.stock-row').textContent.includes('себест.')")
    assert e2e.rows("SELECT sale_price_cents, cost_price_cents FROM product_prices") == [
        {"sale_price_cents": 1400, "cost_price_cents": None}
    ]

    # Себестоимость снова задана — менеджер её всё равно не видит.
    e2e.db.set_product_price(str(e2e.ids["product"]), "Кабель ВВГ 3x2.5", 14.0, 10.0, "", e2e.ids["boss"])
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    mgr.wait_for_selector(".stock-row .stock-price")
    text = mgr.inner_text(".stock-row .stock-price")
    assert "мин. 14" in text and "себест" not in text, text


def test_product_photo_upload_and_delete(open_app, e2e, monkeypatch):
    sent = _photo_storage(e2e, monkeypatch)
    pid = str(e2e.ids["product"])
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-photo-add")
    assert boss.locator("#pe-photos .machine-photo").count() == 0

    _upload_via_picker(boss, "#pe-photo-add")
    boss.wait_for_selector(".toast:has-text('Фото добавлено')")
    boss.wait_for_selector("#pe-photos .machine-photo img[src^='blob:']")
    rows = e2e.rows("SELECT ms_id, tg_file_id, uploaded_by FROM product_photos")
    assert rows == [{"ms_id": pid, "tg_file_id": "file-1", "uploaded_by": e2e.ids["boss"]}]
    assert len(sent) == 1 and sent[0]["chat_id"] == -1001, "снимок ушёл в канал-хранилище"
    assert "фото · 1" in boss.inner_text("#pe-photos").lower()

    boss.click("#pe-photos [data-photo-del]")  # confirm → «да»
    boss.wait_for_selector(".toast:has-text('Фото убрано')")
    boss.wait_for_function("() => !document.querySelector('#pe-photos .machine-photo')")
    assert e2e.rows("SELECT COUNT(*) AS n FROM product_photos")[0]["n"] == 0
    assert any("Убрать это фото" in a for a in alerts(boss))


def test_product_photo_without_storage_reports_failure_honestly(open_app, e2e, monkeypatch):
    monkeypatch.delenv("PHOTOS_TG_CHAT_ID", raising=False)
    monkeypatch.delenv("MACHINE_PHOTOS_TG_CHAT_ID", raising=False)
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-photo-add")
    _upload_via_picker(boss, "#pe-photo-add")
    # Одна повторная попытка через 1,5 с — ждём итоговый тост, а не таймер.
    boss.wait_for_selector(".toast--error:has-text('не прошли: 1')")
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('Загрузка фото не настроена'))")
    assert e2e.rows("SELECT COUNT(*) AS n FROM product_photos")[0]["n"] == 0


def test_showcase_post_is_published_with_product_photo(open_app, e2e, monkeypatch):
    from services import product_photos

    _photo_storage(e2e, monkeypatch)
    monkeypatch.setenv("CHANNEL_ID", "-100777")
    pid = str(e2e.ids["product"])
    e2e.run(product_photos.add_photo(pid, tg_file_id="file-shop", file_unique_id="e2e-shop-1",
                                     uploaded_by=e2e.ids["boss"]))
    captions: list[dict] = []
    orig = e2e.bot.send_photo

    async def spy(chat_id, photo, caption=None, **kw):
        captions.append({"chat_id": chat_id, "caption": caption})
        return await orig(chat_id, photo, caption=caption, **kw)

    e2e.bot.send_photo = spy

    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.click("[data-price-idx]")
    boss.wait_for_selector("#pe-post")
    boss.click("#pe-post")
    boss.wait_for_selector("#ms-f-note")
    boss.fill("#ms-f-note", "Доставка по городу")
    text = _publish_draft(boss)
    assert "Доставка по городу" in text

    assert len(captions) == 1 and captions[0]["chat_id"] == -100777
    assert "Кабель ВВГ" in captions[0]["caption"] and "20" not in captions[0]["caption"]
    post = e2e.rows("SELECT kind, ref, posted_by FROM channel_posts")
    assert post == [{"kind": "showcase", "ref": pid, "posted_by": e2e.ids["boss"]}]


def test_post_preview_warns_that_channel_is_not_configured(open_app, e2e, monkeypatch):
    monkeypatch.delenv("CHANNEL_ID", raising=False)
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.click("[data-stale]")
    boss.wait_for_selector(".stale-check")
    boss.check(".stale-check")
    boss.click("#stale-post")
    boss.wait_for_selector("#ms-f-manager_username")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-f-text")
    boss.wait_for_function("() => document.querySelectorAll('.c-overlay').length === 1")
    assert "Нельзя опубликовать" in boss.inner_text("#ms-submit")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Канал не настроен')")
    assert e2e.rows("SELECT COUNT(*) AS n FROM channel_posts")[0]["n"] == 0
    # Причина должна быть видна сразу на предпросмотре, до нажатия: блок
    # «Канал не настроен: нет CHANNEL_ID» вставляется в шторку черновика,
    # которая тут же закрывается, и пропадает вместе с ней.
    assert "не указан, куда публиковать" in boss.inner_text(".c-overlay")


def test_stale_post_needs_selection_and_publishes_names_only(open_app, e2e, monkeypatch):
    monkeypatch.setenv("CHANNEL_ID", "-100777")
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.click("[data-stale]")
    boss.wait_for_selector(".stale-check")
    assert boss.locator("[data-price-idx]").count() == 0, "в режиме «залежалось» строка — не редактор цены"

    boss.click("#stale-post")
    boss.wait_for_function("() => window.__tgAlerts.includes('Отметьте, что выносить в канал')")
    assert boss.locator(".c-overlay").count() == 0

    boss.check(".stale-check")
    boss.click("#stale-post")
    boss.wait_for_selector("#ms-f-manager_username")
    boss.fill("#ms-f-manager_username", "@sklad_mgr")
    text = _publish_draft(boss)
    assert "Кабель ВВГ 3x2.5" in text and "sklad_mgr" in text

    msg = [m for m in e2e.bot.messages if m["chat_id"] == -100777]
    assert len(msg) == 1
    assert "Кабель ВВГ" in msg[0]["text"] and "20" not in msg[0]["text"], "остаток наружу не уходит"
    assert e2e.rows("SELECT kind FROM channel_posts") == [{"kind": "stale"}]

    # «Все» возвращает обычный каталог с редактором цены.
    boss.click('[data-instock="0"]')
    boss.wait_for_selector("[data-price-idx]")
    assert boss.locator(".stale-check").count() == 0


def test_catalog_shows_fresh_stock_after_invoice(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    boss.wait_for_selector(".stock-row .stock-badge")
    assert boss.inner_text(".stock-row .stock-badge") == "20"

    tab(boss, "invoices")
    boss.click("#wh-new")
    boss.wait_for_selector('[data-whtype="incoming"]')
    boss.click('[data-whtype="incoming"]')
    boss.wait_for_selector('[data-whtype="incoming"].active')
    _add_position(boss, e2e.ids["product"])
    boss.fill('.wh-pos [data-f="quantity"]', "5")
    boss.locator('.wh-pos [data-f="quantity"]').dispatch_event("change")
    boss.click("#wh-save")
    boss.wait_for_selector(".toast:has-text('оформлен')")
    assert _stock(e2e) == 25

    tab(boss, "catalog")
    boss.wait_for_selector(".stock-row .stock-badge")
    assert boss.inner_text(".stock-row .stock-badge") == "25"


# ─── Контейнеры ──────────────────────────────────────────────────────────────


def test_container_edit_search_by_note_and_status_filter(open_app, e2e):
    from services import containers

    other = _container(e2e, "MSCU7000002", notes="шины для погрузчика")
    e2e.run(containers.mark_arrived(other, user_id=e2e.ids["boss"]))

    # Менеджер заводит контейнер сам: пустой номер и занятый номер — ошибка в форме.
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    tab(mgr, "containers")
    mgr.click("#container-new")
    mgr.wait_for_selector("#ms-f-number")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('Заполните: Номер контейнера')")
    mgr.fill("#ms-f-number", "mscu 700-0002")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('уже заведён')")
    sheet_fill(mgr, {"number": "mscu 700-0001", "eta_date": "2030-04-20", "notes": "первичная"})
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Контейнер заведён')")
    mgr.wait_for_selector("#cont-edit")
    row = e2e.rows("SELECT id, status, eta_date, notes, created_by FROM containers WHERE number = 'MSCU7000001'")
    assert len(row) == 1
    cid = row[0].pop("id")
    assert row[0] == {"status": "in_transit", "eta_date": "2030-04-20", "notes": "первичная",
                      "created_by": e2e.ids["mgr"]}
    assert e2e.rows("SELECT COUNT(*) AS n FROM containers")[0]["n"] == 2

    mgr.click("#cont-edit")
    mgr.wait_for_function("() => document.querySelector('#ms-f-notes')?.value === 'первичная'")
    mgr.wait_for_selector("#ms-f-notes")
    assert mgr.locator("#ms-f-number").count() == 0, "номер не правится"
    mgr.fill("#ms-f-eta_date", "2030-05-01")
    mgr.fill("#ms-f-notes", "запчасти для JCB")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Сохранено')")
    mgr.wait_for_selector("#content:has-text('запчасти для JCB')")
    assert e2e.rows("SELECT eta_date, notes FROM containers WHERE id = ?", (cid,)) == [
        {"eta_date": "2030-05-01", "notes": "запчасти для JCB"}
    ]

    go(mgr, "stock")
    tab(mgr, "containers")
    mgr.wait_for_selector(f'[data-container="{cid}"]')
    assert "запчасти для JCB" in mgr.inner_text(f'[data-container="{cid}"]'), "заметка видна в списке"

    mgr.fill("#container-search", "шины")
    mgr.wait_for_function(
        "(id) => !document.querySelector(`[data-container=\"${id}\"]`)"
        " && document.querySelectorAll('[data-container]').length === 1", arg=cid)
    assert mgr.locator(f'[data-container="{other}"]').count() == 1
    mgr.fill("#container-search", "нет такого")
    mgr.wait_for_selector("#content:has-text('Ничего не найдено')")
    mgr.fill("#container-search", "")
    mgr.wait_for_function("() => document.querySelectorAll('[data-container]').length === 2")

    mgr.click('[data-cstatus="arrived"]')
    mgr.wait_for_function("() => document.querySelectorAll('[data-container]').length === 1")
    assert mgr.locator(f'[data-container="{other}"]').count() == 1
    mgr.click('[data-cstatus="in_transit"]')
    mgr.wait_for_function(
        "(id) => document.querySelectorAll('[data-container]').length === 1"
        " && !!document.querySelector(`[data-container=\"${id}\"]`)", arg=cid)


def test_container_supplier_becomes_receipt_invoice_counterparty(open_app, e2e):
    from services import containers

    cid = _container(e2e, "SUPP0000001")
    item = _item(e2e, cid, "Кабель ВВГ 3x2.5", 6, product_id=e2e.ids["product"])
    e2e.run(containers.mark_arrived(cid, user_id=e2e.ids["boss"]))
    cp = _cp_id(e2e)

    boss = open_app(e2e.ids["boss"])
    _open_container(boss, cid)
    assert "— не выбран" in boss.inner_text("#content")
    boss.click("#cont-supplier")
    boss.wait_for_selector(f'[data-supplier="{cp}"]')
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Выберите поставщика')")
    boss.click(f'[data-supplier="{cp}"]')
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Поставщик сохранён')")
    boss.wait_for_selector("#content:has-text('ООО Ромашка')")
    assert e2e.rows("SELECT supplier_id, supplier_name FROM container_receipt WHERE container_id = ?",
                    (cid,)) == [{"supplier_id": cp, "supplier_name": "ООО Ромашка"}]

    # Сверка сохраняется и сразу оприходует: остаток растёт на фактическое.
    boss.fill(f'.qty-input[data-item="{item}"]', "6")
    boss.click("#cont-save")
    boss.wait_for_selector(".toast:has-text('Сверка сохранена')")
    boss.wait_for_selector("#content:has-text('Товар принят на склад')")
    assert _stock(e2e) == 26
    inv = e2e.rows("SELECT i.type, i.counterparty_id, i.comment FROM invoices i "
                   "JOIN container_receipt r ON r.invoice_id = i.id WHERE r.container_id = ?", (cid,))
    assert inv == [{"type": "incoming", "counterparty_id": cp, "comment": "Контейнер SUPP0000001"}]
    assert "Принять заново" in boss.inner_text("#cont-supply")


def test_container_new_product_is_confirmed_at_receipt_and_legacy_unmatched_is_linked(open_app, e2e):
    from services import container_receipt, containers

    cid = _container(e2e, "UNMT0000001")
    cable = _item(e2e, cid, "Кабель ВВГ 3x2.5", 10, product_id=e2e.ids["product"])
    boss = open_app(e2e.ids["boss"])
    _open_container(boss, cid)

    # Товара в каталоге нет — «Новый товар» под списком, название из поиска.
    boss.click("#cont-item-add")
    boss.wait_for_selector("#ms-f-search")
    boss.fill("#ms-f-search", "Фильтр масляный JCB")
    boss.wait_for_selector(".picker-list:has-text('В каталоге не найдено')")
    boss.click("#picker-new-product")
    boss.wait_for_selector("#ms-f-name")
    assert boss.input_value("#ms-f-name") == "Фильтр масляный JCB"
    boss.fill("#ms-f-expected_qty", "5")
    boss.click("#ms-submit")
    boss.wait_for_function("() => document.querySelectorAll('[data-item-del]').length === 2")
    _no_overlay(boss)
    assert "нет в каталоге" in boss.inner_text(".c-row:has-text('Фильтр масляный JCB')")
    filt = e2e.rows("SELECT id FROM container_items WHERE name = 'Фильтр масляный JCB'")[0]["id"]
    assert e2e.rows("SELECT COUNT(*) AS n FROM container_item_products WHERE item_id = ?", (filt,))[0]["n"] == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM products WHERE name = 'Фильтр масляный JCB'")[0]["n"] == 0

    boss.click("#cont-arrive")
    boss.wait_for_selector(".qty-input[data-item]")
    boss.fill(f'.qty-input[data-item="{cable}"]', "10")
    boss.fill(f'.qty-input[data-item="{filt}"]', "5")
    # Сверка сразу проводит приход — позицию без карточки подтверждают ДО него.
    boss.click("#cont-save")
    boss.wait_for_selector(f'#receipt-review [data-review="{filt}"]')
    assert _stock(e2e) == 20, "до подтверждения ничего не записано"
    boss.click(f'[data-review-new="{filt}"]')
    boss.wait_for_selector(f'#receipt-review [data-review="{filt}"]:has-text("новый товар")')
    boss.click(".c-overlay #ms-submit")
    boss.wait_for_selector(".toast:has-text('новых товаров в каталоге: 1')")
    _no_overlay(boss)
    new_pid = e2e.rows("SELECT id FROM products WHERE name = 'Фильтр масляный JCB'")[0]["id"]
    assert _stock(e2e, new_pid) == 5
    assert _stock(e2e) == 30
    assert e2e.rows("SELECT product_id FROM container_item_products WHERE item_id = ?", (filt,)) == [
        {"product_id": new_pid}
    ]

    # Старый контейнер: позиция текстом уже выпала из прихода (так было до
    # выбора из каталога). Чинится прямо из строки «не найден в номенклатуре».
    old = _container(e2e, "OLDU0000001")
    belt = _item(e2e, old, "Ремень генератора", 2)
    e2e.run(containers.mark_arrived(old, user_id=e2e.ids["boss"]))
    e2e.run(containers.set_arrived_quantities(old, {belt: 2}, user_id=e2e.ids["boss"]))
    e2e.run(container_receipt.set_supplier(old, supplier_id=None, name=None))
    e2e.exec("UPDATE container_receipt SET unmatched = ? WHERE container_id = ?",
             (json.dumps([{"item_id": belt, "name": "Ремень генератора", "quantity": 2,
                          "reason": "не найден в номенклатуре"}], ensure_ascii=False), old))
    _open_container(boss, old)
    boss.click(f'[data-unmatched="{belt}"]')
    boss.wait_for_selector("#picker-new-product:has-text('Ремень генератора')")
    boss.click("#picker-new-product")
    boss.wait_for_selector(".toast:has-text('Товар заведён')")
    belt_pid = e2e.rows("SELECT id FROM products WHERE name = 'Ремень генератора'")[0]["id"]
    assert e2e.rows("SELECT product_id FROM container_item_products WHERE item_id = ?", (belt,)) == [
        {"product_id": belt_pid}
    ]
    boss.wait_for_selector("#cont-supply")
    boss.click("#cont-supply")
    boss.wait_for_selector(".toast:has-text('Принято на склад: 1 позиция')")
    assert _stock(e2e, belt_pid) == 2
    assert _stock(e2e) == 30, "переоприходование не трогало чужой контейнер"


def test_container_item_is_linked_to_existing_product_and_deleted(open_app, e2e):
    cid = _container(e2e, "LINK0000001")
    linked = _item(e2e, cid, "Кабель медный", 12)
    doomed = _item(e2e, cid, "Лишняя строка", 1)

    mgr = open_app(e2e.ids["mgr"])
    _open_container(mgr, cid)
    mgr.click(f'[data-item-link="{linked}"]')
    mgr.wait_for_selector("#ms-f-search")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('Выберите товар из списка')")
    mgr.fill("#ms-f-search", "Кабель ВВГ")
    pid = e2e.ids["product"]
    mgr.wait_for_selector(f'.c-overlay [data-product="{pid}"]')
    mgr.click(f'.c-overlay [data-product="{pid}"]')
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Товар привязан')")
    _no_overlay(mgr)
    assert e2e.rows("SELECT product_id FROM container_item_products WHERE item_id = ?", (linked,)) == [
        {"product_id": pid}
    ]

    mgr.wait_for_selector(f'[data-item-del="{doomed}"]')
    mgr.click(f'[data-item-del="{doomed}"]')
    mgr.wait_for_function("(id) => !document.querySelector(`[data-item-del=\"${id}\"]`)", arg=doomed)
    assert [r["id"] for r in e2e.rows("SELECT id FROM container_items WHERE container_id = ?", (cid,))] == [linked]
    # Менеджеру контейнер не удалить и в канал не запостить.
    assert mgr.locator("#cont-del").count() == 0


def test_extra_item_in_arrived_container_is_recorded_as_arrived(open_app, e2e):
    from services import containers

    cid = _container(e2e, "EXTR0000001")
    _item(e2e, cid, "Кабель ВВГ 3x2.5", 4, product_id=e2e.ids["product"])
    e2e.run(containers.mark_arrived(cid, user_id=e2e.ids["boss"]))

    mgr = open_app(e2e.ids["mgr"])
    _open_container(mgr, cid)
    assert mgr.locator("#cont-arrive").count() == 0 and mgr.locator("#cont-post").count() == 0
    assert "Лишняя позиция" in mgr.inner_text("#cont-item-add")
    mgr.click("#cont-item-add")
    mgr.wait_for_selector("#picker-new-product")
    mgr.fill("#ms-f-search", "Ремень генератора")
    mgr.click("#picker-new-product")
    mgr.wait_for_selector("#ms-f-arrived_qty")
    assert mgr.locator("#ms-f-expected_qty").count() == 0
    assert mgr.input_value("#ms-f-name") == "Ремень генератора"
    mgr.fill("#ms-f-arrived_qty", "3")
    mgr.click("#ms-submit")
    mgr.wait_for_function("() => document.querySelectorAll('.qty-input[data-item]').length === 2")
    row = e2e.rows("SELECT expected_qty, arrived_qty FROM container_items WHERE name = 'Ремень генератора'")
    assert row == [{"expected_qty": 0, "arrived_qty": 3}]
    # Незаявленная позиция — опись прибывшего, а не расхождение: красным не горит.
    extra = mgr.locator(".c-row:has-text('Ремень генератора')")
    assert "заявлено 0 шт" in extra.inner_text()
    assert "Расхождений" not in mgr.inner_text("#content")

    # Ремня нет в каталоге: «Оприходовать» сначала спрашивает, что это за
    # товар. Передумали — ничего не записано, остаток не тронут.
    mgr.click("#cont-supply")
    mgr.wait_for_selector("#receipt-review:has-text('Ремень генератора')")
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".c-overlay #ms-error:has-text('Выберите товар')")
    mgr.click(".c-overlay #ms-cancel")
    _no_overlay(mgr)
    assert _stock(e2e) == 20
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices")[0]["n"] == 1
    assert e2e.rows("SELECT COUNT(*) AS n FROM products WHERE name = 'Ремень генератора'")[0]["n"] == 0


def test_arrival_post_to_channel_lists_names_without_quantities(open_app, e2e, monkeypatch):
    from services import containers

    monkeypatch.setenv("CHANNEL_ID", "-100777")
    cid = _container(e2e, "POST0000001")
    item = _item(e2e, cid, "Кабель ВВГ 3x2.5", 37, product_id=e2e.ids["product"])
    _item(e2e, cid, "Не доехавший товар", 5)
    e2e.run(containers.mark_arrived(cid, user_id=e2e.ids["boss"]))
    e2e.run(containers.set_arrived_quantities(cid, {item: 37}, user_id=e2e.ids["boss"]))

    boss = open_app(e2e.ids["boss"])
    _open_container(boss, cid)
    boss.click("#cont-post")
    boss.wait_for_selector("#ms-f-note")
    text = _publish_draft(boss)
    assert "Кабель ВВГ" in text and "Не доехавший" not in text

    msg = [m for m in e2e.bot.messages if m["chat_id"] == -100777]
    assert len(msg) == 1
    assert "37" not in msg[0]["text"] and "POST0000001" not in msg[0]["text"], "наружу — только названия"
    assert e2e.rows("SELECT kind, ref FROM channel_posts") == [{"kind": "arrival", "ref": str(cid)}]


def test_boss_deletes_container_with_its_items(open_app, e2e):
    cid = _container(e2e, "DELE0000001")
    _item(e2e, cid, "Кабель ВВГ 3x2.5", 4, product_id=e2e.ids["product"])
    keep = _container(e2e, "KEEP0000001")

    boss = open_app(e2e.ids["boss"])
    _open_container(boss, cid)
    boss.click("#cont-del")
    boss.wait_for_selector(".toast:has-text('Контейнер удалён')")
    boss.wait_for_function("() => document.getElementById('bottom-nav')?.dataset.current === 'stock'")
    assert [r["id"] for r in e2e.rows("SELECT id FROM containers")] == [keep]
    tab(boss, "containers")
    boss.wait_for_selector(f'[data-container="{keep}"]')
    assert boss.locator(f'[data-container="{cid}"]').count() == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM container_items")[0]["n"] == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM container_item_products")[0]["n"] == 0
    assert any("Удалить контейнер DELE0000001" in a for a in alerts(boss))


def test_deleting_received_container_rolls_back_its_stock(open_app, e2e):
    from services import container_receipt, containers

    cid = _container(e2e, "WRNG0000001")
    item = _item(e2e, cid, "Кабель ВВГ 3x2.5", 8, product_id=e2e.ids["product"])
    e2e.run(containers.mark_arrived(cid, user_id=e2e.ids["boss"]))
    e2e.run(containers.set_arrived_quantities(cid, {item: 8}, user_id=e2e.ids["boss"]))
    assert e2e.run(container_receipt.receive(cid, user_id=e2e.ids["boss"]))["ok"]
    assert _stock(e2e) == 28

    boss = open_app(e2e.ids["boss"])
    _open_container(boss, cid)
    boss.click("#cont-del")  # окно правки открыто — удаление разрешено
    boss.wait_for_selector(".toast:has-text('Контейнер удалён')")
    assert e2e.rows("SELECT COUNT(*) AS n FROM containers")[0]["n"] == 0
    # Контейнер завели по ошибке — его приход не должен остаться на складе.
    assert _stock(e2e) == 20
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices WHERE status = 'confirmed' "
                    "AND comment = 'Контейнер WRNG0000001'")[0]["n"] == 0


def test_container_edit_window_closes_after_a_day(open_app, e2e):
    from services import containers

    cid = _container(e2e, "OLD00000001")
    item = _item(e2e, cid, "Кабель ВВГ 3x2.5", 5, product_id=e2e.ids["product"])
    e2e.run(containers.mark_arrived(cid, user_id=e2e.ids["boss"]))
    e2e.run(containers.set_arrived_quantities(cid, {item: 5}, user_id=e2e.ids["boss"]))
    e2e.exec("UPDATE containers SET arrived_at = '2020-01-01 10:00:00' WHERE id = ?", (cid,))

    boss = open_app(e2e.ids["boss"])
    _open_container(boss, cid)
    assert "Приёмка закрыта" in boss.inner_text("#content")
    for sel in ("#cont-edit", "#cont-supplier", "#cont-item-add", "#cont-save", "#cont-del",
                ".qty-input", "[data-item-del]", "[data-item-link]"):
        assert boss.locator(sel).count() == 0, sel
    assert boss.locator("#cont-supply").count() == 1, "оприходовать можно и после закрытия окна"

    # Ручка держит то же окно, что и экран.
    assert _post(boss, "/api/containers/check", {"container_id": cid, "quantities": {str(item): "1"}}) == 400
    assert _post(boss, "/api/containers/delete", {"container_id": cid}) == 400
    assert e2e.rows("SELECT arrived_qty FROM container_items WHERE id = ?", (item,))[0]["arrived_qty"] == 5


# ─── Техника ─────────────────────────────────────────────────────────────────


def test_machine_edit_saves_fields_and_rejects_duplicate_vin(open_app, e2e):
    mid = _machine(e2e, "JCB-3CX-1", "JCB 3CX", price_cents=2_500_000, cost_cents=2_000_000, year=2018)
    _machine(e2e, "CAT320", "CAT 320")

    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.click('[data-mact="edit"]')
    boss.wait_for_selector("#ms-f-vin")
    assert boss.input_value("#ms-f-price") == "25000" and boss.input_value("#ms-f-cost") == "20000"
    assert boss.locator("#ms-f-hours").count() == 0, "моточасы правятся своей формой"

    boss.fill("#ms-f-vin", "cat-320")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('уже заведена')")
    assert e2e.rows("SELECT vin, name FROM machines WHERE id = ?", (mid,)) == [{"vin": "JCB3CX1", "name": "JCB 3CX"}]

    sheet_fill(boss, {"vin": "jcb 3cx 0001", "name": "JCB 3CX Sitemaster", "year": "2019",
                      "price": "26500", "cost": "21000", "location": "Ташкент, Сергели",
                      "notes": "после ТО"})
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Карточка обновлена')")
    boss.wait_for_selector("#content:has-text('JCB 3CX Sitemaster')")
    m = e2e.rows("SELECT vin, name, year, price_cents, cost_cents, location, notes FROM machines WHERE id = ?",
                 (mid,))[0]
    assert m == {"vin": "JCB3CX0001", "name": "JCB 3CX Sitemaster", "year": 2019, "price_cents": 2_650_000,
                 "cost_cents": 2_100_000, "location": "Ташкент, Сергели", "notes": "после ТО"}
    text = boss.inner_text("#content")
    assert "Ташкент, Сергели" in text and "после ТО" in text


def test_machine_hours_increase_and_manager_cannot_roll_back(open_app, e2e):
    mid = _machine(e2e, "HRS-1", "Volvo EC210", hours=1500, status="in_stock")
    mgr = open_app(e2e.ids["mgr"])
    _open_machine(mgr, mid)

    mgr.click('[data-mact="hours"]')
    mgr.wait_for_selector("#ms-f-hours")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('Заполните')")
    mgr.fill("#ms-f-hours", "1400")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('Уменьшить показание может только руководитель')")
    assert not any("счётчик" in a for a in alerts(mgr)), "менеджеру подтверждение не предлагается"
    assert e2e.rows("SELECT hours FROM machines WHERE id = ?", (mid,))[0]["hours"] == 1500

    mgr.fill("#ms-f-hours", "1620")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Моточасы записаны')")
    mgr.wait_for_selector("#content:has-text('1 620 м/ч')")
    assert e2e.rows("SELECT hours FROM machines WHERE id = ?", (mid,))[0]["hours"] == 1620
    hist = e2e.rows("SELECT hours, recorded_by FROM machine_hours WHERE machine_id = ? ORDER BY id DESC", (mid,))
    assert hist[0] == {"hours": 1620, "recorded_by": e2e.ids["mgr"]}
    # Правка и ручные переходы — руководству; бронь, продажа и рассрочка —
    # заявками на одобрение, удаление — пока руководитель не оставил его себе.
    for sel in ('[data-mact="edit"]', "[data-mstatus-to]"):
        assert mgr.locator(sel).count() == 0, sel
    for sel in ('[data-mact="reserve"]', '[data-mact="sale"]', '[data-mact="credit"]',
                '[data-mact="delete"]'):
        assert mgr.locator(sel).count() == 1, sel


def test_manager_creates_machine_without_cost_and_sees_schedule_read_only(open_app, e2e):
    mid = _machine(e2e, "MGR-VIEW", "Hyundai R220", status="in_stock")
    _credit(e2e, mid)

    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    tab(mgr, "machines")
    mgr.click("#machine-new")
    mgr.wait_for_selector("#ms-f-vin")
    assert mgr.locator("#ms-f-cost").count() == 0
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('Заполните: VIN')")
    sheet_fill(mgr, {"vin": "doosan dx225", "name": "Doosan DX225", "price": "41000"})
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Машина заведена')")
    mgr.wait_for_function("() => document.querySelectorAll('[data-machine]').length === 2")
    row = e2e.rows("SELECT status, price_cents, cost_cents, created_by FROM machines WHERE vin = 'DOOSANDX225'")
    assert row == [{"status": "in_transit", "price_cents": 4_100_000, "cost_cents": None,
                    "created_by": e2e.ids["mgr"]}]

    mgr.click(f'[data-machine="{mid}"]')
    mgr.wait_for_selector("#content:has-text('Платёж 3')")
    # Деньги по рассрочке вносит менеджер (решение владельца); закрыть досрочно,
    # снять отметку и удалить поступление — руководитель.
    assert mgr.locator('[data-payment][data-paid="0"]').count() == 3
    assert mgr.locator("[data-receipt-add]").count() == 1
    for sel in ('[data-payment][data-paid="1"]', "[data-deal-close]", "[data-receipt-del]",
                "#machine-photo-add", "[data-photo-del]"):
        assert mgr.locator(sel).count() == 0, sel
    assert "AA7654321" not in mgr.inner_text("#content")


def test_machine_status_reserve_unreserve_and_list_filter(open_app, e2e):
    mid = _machine(e2e, "RSV-1", "Komatsu PC200", status="in_stock")
    other = _machine(e2e, "RSV-2", "Liebherr R920")  # в пути

    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "machines")
    boss.wait_for_selector('[data-mstatus="in_transit"]')
    boss.click('[data-mstatus="in_stock"]')
    boss.wait_for_function("() => document.querySelectorAll('[data-machine]').length === 1")
    assert boss.locator(f'[data-machine="{mid}"]').count() == 1

    boss.click(f'[data-machine="{mid}"]')
    boss.wait_for_selector('[data-mstatus-to="reserved"]')
    assert boss.locator('[data-mstatus-to]').count() == 1
    boss.click('[data-mstatus-to="reserved"]')
    boss.wait_for_selector('[data-mstatus-to="in_stock"]')
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "reserved"
    assert "Снять бронь" in boss.inner_text('[data-mstatus-to="in_stock"]')
    assert any("Забронировать" in a for a in alerts(boss)), "переход подтверждался"
    # Забронированную ещё можно продать.
    assert boss.locator('[data-mact="sale"]').count() == 1

    boss.click('[data-mstatus-to="in_stock"]')
    boss.wait_for_selector('[data-mstatus-to="reserved"]')
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "in_stock"

    # Устаревший экран: статус уже сменили в другом месте — сервер отвечает 409,
    # карточка перечитывается, а не пишет поверх.
    e2e.exec("UPDATE machines SET status = 'reserved' WHERE id = ?", (mid,))
    boss.click('[data-mstatus-to="reserved"]')
    boss.wait_for_function("() => window.__tgAlerts.some(a => a.includes('перевести нельзя') || a.includes('Статус уже'))")
    boss.wait_for_selector('[data-mstatus-to="in_stock"]')
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (other,))[0]["status"] == "in_transit"


def test_machine_sale_then_archive(open_app, e2e):
    mid = _machine(e2e, "SALE-1", "JCB 4CX", status="in_stock", price_cents=3_100_000)
    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)

    boss.click('[data-mact="sale"]')
    boss.wait_for_selector("#ms-f-buyer_name")
    assert boss.input_value("#ms-f-price") == "31000", "цена подставлена из карточки"
    assert boss.locator("#ms-f-months").count() == 0
    boss.fill("#ms-f-price", "")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Заполните: Цена')")
    sheet_fill(boss, {"price": "30500", "buyer_name": "ООО Стройинвест", "buyer_phone": "+998711234567",
                      "buyer_note": "самовывоз"})
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Машина продана')")
    boss.wait_for_selector('[data-mstatus-to="archived"]')
    deal = e2e.rows("SELECT kind, price_cents, buyer_name, buyer_note, closed_at FROM machine_deals")
    assert deal == [{"kind": "sale", "price_cents": 3_050_000, "buyer_name": "ООО Стройинвест",
                     "buyer_note": "самовывоз", "closed_at": None}]
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "sold"
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_deal_payments")[0]["n"] == 0, "у продажи графика нет"
    for sel in ('[data-mact="sale"]', '[data-mact="credit"]', '[data-mact="delete"]'):
        assert boss.locator(sel).count() == 0, sel

    boss.click('[data-mstatus-to="archived"]')
    boss.wait_for_selector(".toast:has-text('Статус изменён')")
    boss.wait_for_function("() => !document.querySelector('[data-mstatus-to]')")
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "archived"

    go(boss, "stock")
    tab(boss, "machines")
    boss.wait_for_selector('[data-mstatus="archived"]')
    boss.click('[data-mstatus="archived"]')
    boss.wait_for_function("() => document.querySelectorAll('[data-machine]').length === 1")
    assert boss.get_attribute(f'[data-machine="{mid}"]', "data-status") == "archived"


def test_machine_without_deals_is_deleted_with_history(open_app, e2e, monkeypatch):
    monkeypatch.delenv("PHOTOS_TG_CHAT_ID", raising=False)
    monkeypatch.delenv("MACHINE_PHOTOS_TG_CHAT_ID", raising=False)
    from services import machines

    mid = _machine(e2e, "DEL-1", "Опечатка в карточке", hours=10)
    keep = _machine(e2e, "KEEP-1", "Нужная машина")
    e2e.run(machines.add_hours(mid, 20, user_id=e2e.ids["boss"]))

    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    assert boss.locator("#machine-photo-add").count() == 0, "без хранилища фото кнопки нет"
    boss.click('[data-mact="delete"]')
    boss.wait_for_selector(".toast:has-text('Машина удалена')")
    boss.wait_for_function("() => !document.querySelector('[data-mact]')")
    assert [r["id"] for r in e2e.rows("SELECT id FROM machines")] == [keep]
    tab(boss, "machines")
    boss.wait_for_selector(f'[data-machine="{keep}"]')
    assert boss.locator(f'[data-machine="{mid}"]').count() == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_hours WHERE machine_id = ?", (mid,))[0]["n"] == 0


def test_after_delete_user_returns_to_the_same_tab(open_app, e2e):
    mid = _machine(e2e, "BACK-1", "Удаляемая машина")
    keep = _machine(e2e, "BACK-2", "Остаётся")
    cid = _container(e2e, "BACK0000001")
    keep_c = _container(e2e, "BACK0000002")

    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.click('[data-mact="delete"]')
    boss.wait_for_selector(".toast:has-text('Машина удалена')")
    boss.wait_for_function("() => !document.querySelector('[data-mact]')")
    settled(boss)
    assert boss.locator(f'[data-machine="{keep}"]').count() == 1, "ожидался список техники"

    _open_container(boss, cid)
    boss.click("#cont-del")
    boss.wait_for_selector(".toast:has-text('Контейнер удалён')")
    settled(boss)
    assert boss.locator(f'[data-container="{keep_c}"]').count() == 1, "ожидался список контейнеров"


def test_deal_form_validation_keeps_machine_unsold(open_app, e2e):
    mid = _machine(e2e, "VAL-1", "Case 580", status="in_stock", price_cents=2_000_000)
    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.click('[data-mact="credit"]')
    boss.wait_for_selector("#ms-f-months")
    boss.fill("#ms-f-buyer_name", "Бобур")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Заполните: Срок, месяцев')")

    sheet_fill(boss, {"months": "6", "down_payment": "20000"})
    boss.click("#ms-submit")
    # Договор рассрочки без паспорта не составить — форма требует его до запроса.
    boss.wait_for_selector("#ms-error:has-text('Заполните: Паспорт')")
    sheet_fill(boss, {"buyer_passport": "AA7654321"})
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Взнос не может покрывать всю цену')")
    sheet_fill(boss, {"months": "121", "down_payment": "1000"})
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Срок рассрочки')")
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "in_stock"

    boss.click("#ms-cancel")
    _no_overlay(boss)
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_deals")[0]["n"] == 0


def test_credit_receipts_partial_overpay_closes_and_delete_reopens(open_app, e2e):
    mid = _machine(e2e, "CRD-1", "JCB 3CX", status="in_stock")
    deal = _credit(e2e, mid, price=100_000, down=10_000, months=3)  # 3 × 300 USD

    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.click(f'[data-receipt-add="{deal}"]')
    boss.wait_for_selector("#ms-f-amount")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Заполните: Сумма')")
    sheet_fill(boss, {"amount": "150", "note": "наличными"})
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Оплата записана')")
    boss.wait_for_selector('.c-row[data-status="partial"]')
    assert e2e.rows("SELECT amount_cents, note, received_by FROM machine_payment_receipts") == [
        {"amount_cents": 15_000, "note": "наличными", "received_by": e2e.ids["boss"]}
    ]
    assert "поступления · 1" in boss.inner_text("#content").lower()
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_deal_payments WHERE paid_at IS NOT NULL")[0]["n"] == 1

    # Остаток долга одной суммой закрывает рассрочку сам.
    boss.wait_for_selector(f'[data-receipt-add="{deal}"]')
    boss.click(f'[data-receipt-add="{deal}"]')
    boss.wait_for_selector("#ms-f-amount")
    boss.fill("#ms-f-amount", "750")
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Рассрочка закрыта — всё получено')")
    boss.wait_for_function("() => !document.querySelector('[data-receipt-add]')")
    assert e2e.rows("SELECT closed_at FROM machine_deals WHERE id = ?", (deal,))[0]["closed_at"]
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "sold"
    assert boss.locator("[data-deal-close]").count() == 0

    big = e2e.rows("SELECT id FROM machine_payment_receipts WHERE amount_cents = 75000")[0]["id"]
    boss.click(f'[data-receipt-del="{big}"]')
    boss.wait_for_selector(".toast:has-text('рассрочка снова открыта')")
    boss.wait_for_selector(f'[data-receipt-add="{deal}"]')
    assert e2e.rows("SELECT closed_at FROM machine_deals WHERE id = ?", (deal,))[0]["closed_at"] is None
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "on_credit"
    assert [r["amount_cents"] for r in e2e.rows("SELECT amount_cents FROM machine_payment_receipts")] == [15_000]
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_deal_payments WHERE paid_at IS NOT NULL")[0]["n"] == 1


def test_credit_payment_mark_is_removed_by_second_tap(open_app, e2e):
    from services import machines

    mid = _machine(e2e, "PAY-1", "Hitachi ZX330", status="in_stock")
    deal = _credit(e2e, mid, price=100_000, down=10_000, months=3)
    first = e2e.rows("SELECT id FROM machine_deal_payments WHERE deal_id = ? AND seq = 1", (deal,))[0]["id"]
    assert e2e.run(machines.pay_installment(first, user_id=e2e.ids["boss"]))["ok"]

    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.wait_for_selector(f'[data-payment="{first}"][data-paid="1"]')
    boss.click(f'[data-payment="{first}"]')
    boss.wait_for_selector(f'[data-payment="{first}"][data-paid="0"]')
    assert e2e.rows("SELECT paid_at FROM machine_deal_payments WHERE id = ?", (first,))[0]["paid_at"] is None
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_payment_receipts")[0]["n"] == 0


def test_credit_is_closed_early_by_button(open_app, e2e):
    mid = _machine(e2e, "CLS-1", "Bobcat S650", status="reserved")
    deal = _credit(e2e, mid, price=50_000, down=0, months=2)
    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.click(f'[data-deal-close="{deal}"]')
    boss.wait_for_selector(".toast:has-text('Рассрочка закрыта')")
    # Ждём перерисованную карточку, а не просто исчезновение кнопки: между
    # ними #content на миг пуст, и на медленном CI проверка текста ловила пустоту.
    boss.wait_for_function(
        "() => !document.querySelector('[data-deal-close]')"
        " && (document.querySelector('#content')?.innerText || '').includes('Закрыта')"
    )
    assert e2e.rows("SELECT closed_at FROM machine_deals WHERE id = ?", (deal,))[0]["closed_at"]
    assert e2e.rows("SELECT status FROM machines WHERE id = ?", (mid,))[0]["status"] == "sold"
    assert any("Закрыть рассрочку" in a for a in alerts(boss))


def test_machine_photo_upload_and_delete(open_app, e2e, monkeypatch):
    sent = _photo_storage(e2e, monkeypatch)
    mid = _machine(e2e, "PHOTO-1", "JCB JS220", status="in_stock")
    boss = open_app(e2e.ids["boss"])
    _open_machine(boss, mid)
    boss.wait_for_selector("#machine-photo-add")

    _upload_via_picker(boss, "#machine-photo-add")
    boss.wait_for_selector(".toast:has-text('Фото добавлено')")
    boss.wait_for_selector(".machine-photo img[src^='blob:']")
    rows = e2e.rows("SELECT machine_id, tg_file_id, file_unique_id FROM machine_photos")
    assert rows == [{"machine_id": mid, "tg_file_id": "file-1", "file_unique_id": "e2e-uniq-1"}], \
        "сохранён самый крупный размер из лесенки"
    assert sent[0]["chat_id"] == -1001 and "PHOTO1" in (sent[0]["caption"] or "")

    boss.click("[data-photo-del]")
    boss.wait_for_selector(".toast:has-text('Фото убрано')")
    boss.wait_for_function("() => !document.querySelector('.machine-photo')")
    assert e2e.rows("SELECT COUNT(*) AS n FROM machine_photos")[0]["n"] == 0


# ─── Накладные ───────────────────────────────────────────────────────────────


def _add_position(page, product_id: int) -> None:
    """«Добавить позицию» сразу открывает выбор товара: сама строка товар не
    подставляет (раньше вставал первый из справочника, и его проводили вместо
    нужного), а без товара «Сохранить» неактивна."""
    page.click("#wh-add")
    page.wait_for_selector(f'.picker-list [data-pick="{product_id}"]')
    page.click(f'.picker-list [data-pick="{product_id}"]')
    page.click("#ms-submit")
    page.wait_for_selector(".c-overlay", state="detached")
    page.wait_for_selector('.wh-pos [data-f="quantity"]')


def _new_outgoing(page, qty: str, price: str, product_id: int) -> None:
    # «Отгрузки» — своя лента второго уровня вкладки «Движения», и кнопка в ней
    # называет, что создаёт («Оформить отгрузку»): вид документа берётся из
    # ленты, а не из последнего выбора формы.
    page.click('[data-whsub="outgoing"]')
    page.wait_for_selector("#wh-new")
    page.click("#wh-new")
    page.wait_for_selector('[data-whtype="outgoing"].active')
    _add_position(page, product_id)
    page.fill('.wh-pos [data-f="quantity"]', qty)
    page.locator('.wh-pos [data-f="quantity"]').dispatch_event("change")
    page.fill('.wh-pos [data-f="price"]', price)
    page.locator('.wh-pos [data-f="price"]').dispatch_event("change")
    page.click("#wh-cp")
    page.wait_for_selector(".picker-list [data-pick]")
    page.click(".picker-list [data-pick]:has-text('ООО Ромашка')")
    page.click("#ms-submit")
    page.wait_for_selector(".c-overlay", state="detached")


def test_outgoing_invoice_moves_stock_and_sends_pdf_to_client(open_app, e2e):
    cp = _cp_id(e2e)
    e2e.exec("UPDATE counterparties SET telegram_id = 777001 WHERE id = ?", (cp,))
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    _new_outgoing(boss, "3", "15", e2e.ids["product"])
    boss.wait_for_function("() => !document.querySelector('#wh-save').disabled")
    boss.fill("#wh-comment", "Отгрузка на объект")
    boss.click("#wh-save")
    boss.wait_for_selector(".toast:has-text('оформлен')")
    boss.wait_for_selector(".order-pay--ok")

    assert _stock(e2e) == 17
    inv = e2e.rows("SELECT id, type, counterparty_id, total_amount_cents, telegram_sent, comment, created_by "
                   "FROM invoices ORDER BY id DESC LIMIT 1")[0]
    assert inv["type"] == "outgoing" and inv["counterparty_id"] == cp
    assert inv["total_amount_cents"] == 4500 and inv["telegram_sent"] == 1
    assert inv["comment"] == "Отгрузка на объект" and inv["created_by"] == e2e.ids["boss"]
    assert e2e.rows("SELECT product_id, quantity, price_cents FROM invoice_items WHERE invoice_id = ?",
                    (inv["id"],)) == [{"product_id": e2e.ids["product"], "quantity": 3, "price_cents": 1500}]
    assert [d["chat_id"] for d in e2e.bot.documents] == [777001], "PDF ушёл клиенту"
    assert "Товарная накладная" in e2e.bot.documents[0].get("caption", "")
    assert boss.locator(f'[data-wh-send="{inv["id"]}"]').count() == 1


def test_outgoing_without_client_telegram_warns_and_is_sent_later(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    _new_outgoing(boss, "2", "10", e2e.ids["product"])
    boss.click("#wh-save")
    boss.wait_for_selector(".toast--error:has-text('не привязан Telegram')")
    boss.wait_for_selector(".order-pay--wait")
    inv = e2e.rows("SELECT id, telegram_sent FROM invoices ORDER BY id DESC LIMIT 1")[0]
    assert inv["telegram_sent"] == 0 and _stock(e2e) == 18, "накладная проведена, несмотря на PDF"
    assert e2e.bot.documents == []

    # Кнопка без Telegram у клиента отвечает причиной, а не «ошибкой сервера».
    boss.click(f'[data-wh-send="{inv["id"]}"]')
    boss.wait_for_function(
        "() => [...document.querySelectorAll('.toast--error')].filter(t => t.textContent.includes('не привязан')).length >= 1"
        " && !document.querySelector('[data-wh-send]').disabled")
    assert e2e.bot.documents == []

    e2e.exec("UPDATE counterparties SET telegram_id = 777002 WHERE id = ?", (_cp_id(e2e),))
    boss.click(f'[data-wh-send="{inv["id"]}"]')
    boss.wait_for_selector(".toast:has-text('PDF отправлен клиенту')")
    boss.wait_for_selector(".order-pay--ok")
    assert [d["chat_id"] for d in e2e.bot.documents] == [777002]
    assert e2e.rows("SELECT telegram_sent FROM invoices WHERE id = ?", (inv["id"],))[0]["telegram_sent"] == 1


def test_invoice_form_short_stock_position_delete_and_cancel(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    _new_outgoing(boss, "25", "10", e2e.ids["product"])
    boss.wait_for_selector(".wh-pos-warn:has-text('На складе только 20')")
    assert boss.locator("#wh-save").is_disabled()

    # Новая строка товар сама не подставляет: выбор закрыли — строка пустая,
    # с подсказкой, и сохранить её нельзя.
    boss.click("#wh-add")
    boss.wait_for_selector(".picker-list [data-pick]")
    boss.click("#ms-cancel")
    boss.wait_for_selector(".c-overlay", state="detached")
    boss.wait_for_function("() => document.querySelectorAll('.wh-pos').length === 2")
    assert boss.locator('.wh-pos[data-i="1"] .wh-pos-warn:has-text("Выберите товар")').count() == 1
    boss.click('[data-del="0"]')
    boss.wait_for_function("() => document.querySelectorAll('.wh-pos').length === 1")
    assert boss.locator(".wh-pos-warn:has-text('На складе')").count() == 0, "удалена именно строка с нехваткой"
    # Оставшаяся строка — свежая, без товара и цены: такой расход не сохранить.
    assert boss.locator("#wh-save").is_disabled()
    boss.click("[data-pick-product]")
    boss.wait_for_selector(f'.picker-list [data-pick="{e2e.ids["product"]}"]')
    boss.click(f'.picker-list [data-pick="{e2e.ids["product"]}"]')
    boss.click("#ms-submit")
    boss.wait_for_selector(".c-overlay", state="detached")
    assert boss.locator("#wh-save").is_disabled(), "расход без цены не сохранить"

    # Смена типа не теряет позиции: у прихода цена не нужна.
    boss.click('[data-whtype="incoming"]')
    boss.wait_for_selector('[data-whtype="incoming"].active')
    assert boss.locator(".wh-pos").count() == 1 and boss.locator("#wh-save").is_enabled()

    boss.click("#wh-cancel-form")
    boss.wait_for_selector("#wh-new")
    assert e2e.rows("SELECT COUNT(*) AS n FROM invoices")[0]["n"] == 1 and _stock(e2e) == 20
    boss.click("#wh-new")
    boss.wait_for_selector("#wh-items .editor-empty")
    assert boss.locator(".wh-pos").count() == 0, "черновик после «Отмена» сброшен"


def test_incoming_with_picked_product_and_boss_cancel_refused_when_stock_left(open_app, e2e):
    from services import warehouse

    bolt = _product(e2e, "Болт М16")
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    boss.click("#wh-new")
    boss.wait_for_selector('[data-whtype="incoming"]')
    boss.click('[data-whtype="incoming"]')
    boss.wait_for_selector('[data-whtype="incoming"].active')
    # Выбор товара открывается сам — строка без товара бесполезна.
    boss.click("#wh-add")
    boss.wait_for_selector(".picker-list [data-pick]")
    boss.fill("#ms-f-search", "болт")
    boss.wait_for_function("() => document.querySelectorAll('.picker-list [data-pick]').length === 1")
    boss.click(f'.picker-list [data-pick="{bolt}"]')
    boss.click("#ms-submit")
    boss.wait_for_selector(".c-overlay", state="detached")
    boss.fill('.wh-pos [data-f="quantity"]', "40")
    boss.locator('.wh-pos [data-f="quantity"]').dispatch_event("change")
    boss.fill('.wh-pos [data-f="price"]', "0.5")
    boss.locator('.wh-pos [data-f="price"]').dispatch_event("change")
    boss.click("#wh-save")
    boss.wait_for_selector(".toast:has-text('оформлен')")
    boss.wait_for_selector("#wh-new")
    inv = e2e.rows("SELECT id FROM invoices ORDER BY id DESC LIMIT 1")[0]["id"]
    assert e2e.rows("SELECT product_id, quantity, price_cents FROM invoice_items WHERE invoice_id = ?", (inv,)) == [
        {"product_id": bolt, "quantity": 40, "price_cents": 50}
    ]
    assert _stock(e2e, bolt) == 40 and _stock(e2e) == 20

    # Болты успели отгрузить — отмена прихода увела бы остаток в минус.
    res = e2e.run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=e2e.ids["warehouse"], counterparty_id=_cp_id(e2e),
        items=[{"product_id": bolt, "quantity": 30, "price_cents": 100}],
    ))
    assert res["ok"], res
    tab(boss, "catalog")
    tab(boss, "invoices")
    boss.wait_for_selector(f'[data-wh-cancel="{inv}"]')
    boss.click(f'[data-wh-cancel="{inv}"]')
    boss.wait_for_selector(".toast--error")
    assert e2e.rows("SELECT status FROM invoices WHERE id = ?", (inv,))[0]["status"] == "confirmed"
    assert _stock(e2e, bolt) == 10
    assert boss.locator(f'[data-wh-cancel="{inv}"]').is_enabled(), "кнопка вернулась — можно повторить"


def test_counterparty_from_picker_reuses_namesake_and_keeps_supplier_type(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    boss.click("#wh-new")
    boss.wait_for_selector("#wh-cp")

    boss.click("#wh-cp")
    boss.wait_for_selector(".picker-add")
    boss.fill("#ms-f-search", "ооо  ромашка")
    boss.click(".picker-add")
    boss.wait_for_selector("#ms-f-name")
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Такой уже есть в справочнике')")
    _no_overlay(boss)
    assert e2e.rows("SELECT COUNT(*) AS n FROM counterparties")[0]["n"] == 1, "тёзку не завели"
    assert "ООО Ромашка" in boss.inner_text("#wh-cp")

    boss.click("#wh-cp")
    boss.wait_for_selector(".picker-add")
    boss.click(".picker-add")
    boss.wait_for_selector("#ms-f-name")
    boss.click("#ms-submit")
    boss.wait_for_selector("#ms-error:has-text('Заполните: Название')")
    boss.fill("#ms-f-name", "Шанхай Трейдинг")
    boss.click('.c-overlay [data-opt="supplier"]')
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Добавили в справочник')")
    _no_overlay(boss)
    assert e2e.rows("SELECT type, phone FROM counterparties WHERE name = 'Шанхай Трейдинг'") == [
        {"type": "supplier", "phone": None}
    ]
    assert "Шанхай Трейдинг" in boss.inner_text("#wh-cp")


def test_manager_posts_incoming_but_cannot_cancel(open_app, e2e):
    """Приход менеджеру — да; отмена — нет, когда руководитель оставил удаление
    себе (`delete_requires_boss`). Без флага отмену видит и менеджер —
    `test_manager_cancels_invoice_while_deletion_is_open`."""
    from services import warehouse
    from services.database import set_setting

    set_setting("delete_requires_boss", True, e2e.ids["boss"])

    e2e.run(warehouse.create_invoice(
        invoice_type="outgoing", warehouse_id=e2e.ids["warehouse"], counterparty_id=_cp_id(e2e),
        items=[{"product_id": e2e.ids["product"], "quantity": 1, "price_cents": 100}],
    ))
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    tab(mgr, "invoices")
    # Отгрузки — своя лента: проводит их только руководство, и кнопки
    # «Оформить отгрузку» у менеджера в ней нет.
    mgr.click('[data-whsub="outgoing"]')
    mgr.wait_for_selector("[data-wh-send]")
    assert mgr.locator("[data-wh-cancel]").count() == 0
    assert mgr.locator("#wh-new").count() == 0

    mgr.click('[data-whsub="incoming"]')
    mgr.wait_for_selector("#wh-new")
    mgr.click("#wh-new")
    mgr.wait_for_selector("#wh-add")
    _add_position(mgr, e2e.ids["product"])
    mgr.fill('.wh-pos [data-f="quantity"]', "4")
    mgr.locator('.wh-pos [data-f="quantity"]').dispatch_event("change")
    mgr.click("#wh-save")
    mgr.wait_for_selector(".toast:has-text('оформлен')")
    mgr.wait_for_selector("#wh-new")
    inv = e2e.rows("SELECT type, created_by FROM invoices ORDER BY id DESC LIMIT 1")[0]
    assert inv == {"type": "incoming", "created_by": e2e.ids["mgr"]}
    assert _stock(e2e) == 23
    assert mgr.locator("[data-wh-cancel]").count() == 0


def test_manager_cancels_invoice_while_deletion_is_open(open_app, e2e):
    """Решение владельца: пока менеджер один, отменять накладные может и он —
    СВОЙ приход (расход и чужой приход — руководству, `_invoice_cancel_allowed`)."""
    inv = e2e.rows("SELECT id FROM invoices")[0]["id"]
    e2e.exec("UPDATE invoices SET created_by = ? WHERE id = ?", (e2e.ids["mgr"], inv))
    mgr = open_app(e2e.ids["mgr"])
    go(mgr, "stock")
    tab(mgr, "invoices")
    mgr.wait_for_selector(f'[data-wh-cancel="{inv}"]')
    mgr.click(f'[data-wh-cancel="{inv}"]')
    mgr.wait_for_selector(".toast:has-text('Приход отменён')")
    assert e2e.rows("SELECT status FROM invoices WHERE id = ?", (inv,))[0]["status"] == "cancelled"


def test_print_failure_is_shown_not_swallowed(open_app, e2e, monkeypatch):
    from services import printing
    from services.printing import PrintResult

    async def jammed(pdf_bytes, *, filename="", printer_name="", label=""):
        return PrintResult(False, error="Принтер не отвечает")

    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", jammed)
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    settled(boss)
    boss.wait_for_selector("[data-wh-print]")
    boss.click("[data-wh-print]")
    boss.wait_for_selector(".toast--error:has-text('Принтер не отвечает')")
