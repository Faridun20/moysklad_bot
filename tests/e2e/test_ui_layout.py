"""E2E: вёрстка, на которую жаловались с площадки.

Здесь проверяется геометрия в настоящем Chromium на ширине телефона — jsdom
раскладки не считает, и все четыре бага в нём были бы «зелёными»:

* формы открывались окошком, и клавиатура закрывала поля и «Сохранить»;
* карточки накладных стыковались без зазора и читались наехавшими;
* выбор последней категории отбрасывал ряд чипов в начало;
* курс валют отслеживался, но нигде не был виден.
"""

from __future__ import annotations

from tests.e2e.conftest import go, settled, tab


def _seed_categories(e2e, names):
    from services import container_receipt, warehouse

    for i, name in enumerate(names):
        pid = e2e.run(container_receipt.create_product(f"Товар {i}"))["product_id"]
        e2e.exec("UPDATE products SET category = ? WHERE id = ?", (name, pid))
        e2e.run(
            warehouse.create_invoice(
                invoice_type="incoming",
                warehouse_id=e2e.ids["warehouse"],
                items=[{"product_id": pid, "quantity": 3, "price_cents": 1000}],
            )
        )


def test_form_is_a_full_page_and_keeps_focused_field_visible(open_app, e2e):
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "machines")
    boss.click("#machine-new")
    boss.wait_for_selector(".c-overlay #ms-f-vin")
    box = boss.eval_on_selector(
        ".c-overlay",
        "el => { const r = el.getBoundingClientRect();"
        " return { w: r.width, h: r.height, vw: innerWidth, vh: innerHeight }; }",
    )
    assert box["w"] == box["vw"] and box["h"] == box["vh"], (
        "форма — страница во весь экран, не окно"
    )

    # Клавиатура: видимая область падает, поле внизу формы должно доехать в неё.
    boss.set_viewport_size({"width": 390, "height": 460})
    boss.focus("#ms-f-notes")
    boss.wait_for_function(
        "() => { const r = document.querySelector('#ms-f-notes').getBoundingClientRect();"
        " return r.top >= 0 && r.bottom <= innerHeight; }"
    )
    boss.click("#ms-cancel")
    assert boss.locator(".c-overlay").count() == 0


def test_invoice_cards_have_a_gap(open_app, e2e):
    _seed_categories(e2e, ["А", "Б"])  # ещё две накладные к засеянной
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    tab(boss, "invoices")
    settled(boss)
    boss.wait_for_selector(".order-card >> nth=2")
    gaps = boss.evaluate(
        "() => { const cs = [...document.querySelectorAll('#content .order-card')];"
        " return cs.slice(1).map((c, i) => c.getBoundingClientRect().top - cs[i].getBoundingClientRect().bottom); }"
    )
    assert gaps and min(gaps) >= 8, f"карточки стыкуются: {gaps}"


def test_picking_last_category_keeps_it_in_view(open_app, e2e):
    _seed_categories(
        e2e,
        [
            "Аккумуляторы",
            "Инверторы",
            "Кабели",
            "Крепёж",
            "Освещение",
            "Солнечные панели",
            "Щиты и автоматы",
        ],
    )
    boss = open_app(e2e.ids["boss"])
    go(boss, "stock")
    settled(boss)
    row = ".cat-row:has([data-cat])"
    boss.wait_for_selector(row)
    assert boss.eval_on_selector(row, "el => el.scrollWidth > el.clientWidth"), (
        "ряд должен не влезать"
    )
    boss.eval_on_selector(row, "el => el.scrollLeft = el.scrollWidth")
    boss.locator(f"{row} [data-cat]").last.click()
    boss.wait_for_selector(f"{row} [data-cat].active:last-child")
    visible = boss.eval_on_selector(
        row,
        "el => { const a = el.querySelector('.active').getBoundingClientRect();"
        " const b = el.getBoundingClientRect(); return a.left >= b.left - 1 && a.right <= b.right + 1; }",
    )
    assert visible, "выбранная категория уехала за край — ряд прыгнул в начало"


def test_home_shows_currency_rate_and_opens_rates_screen(open_app, e2e):
    from services.database import set_currency_rate

    set_currency_rate("UZS", 1 / 12000, 0)
    boss = open_app(e2e.ids["boss"])
    boss.wait_for_selector("#home-fx .c-row")
    assert "1 USD = 12" in boss.inner_text("#home-fx")
    boss.click("#home-fx .c-row")
    boss.wait_for_selector(".rate-input")
    assert boss.input_value(".rate-input") == "12000"
