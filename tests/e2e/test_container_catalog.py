"""E2E: состав контейнера из каталога и «Прибыла» у техники — глазами менеджера.

Две жалобы с площадки (владелец сейчас работает под ролью менеджера):
1. товар в контейнер приходилось ВПИСЫВАТЬ — ошибка в названии становилась
   второй карточкой товара при оприходовании;
2. у машины «в пути» не было кнопки «Прибыла» — только «Моточасы».

Проверяем последствия в БД (id товаров в накладной, остаток, число карточек,
статус и локация машины, аудит), а не только то, что кнопки нарисовались.
"""

from __future__ import annotations

from tests.e2e.conftest import go, tab


def _stock(e2e, pid: int) -> float:
    rows = e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (pid,))
    return float(rows[0]["quantity"]) if rows else 0.0


def _open_container(page, cid: int) -> None:
    go(page, "stock")
    tab(page, "containers")
    page.wait_for_selector(f'[data-container="{cid}"]')
    page.click(f'[data-container="{cid}"]')
    page.wait_for_selector("#cont-item-add")


def _no_overlay(page) -> None:
    page.wait_for_function("() => !document.querySelector('.c-overlay')")


def test_manager_fills_container_from_catalog_and_receives_without_duplicates(open_app, e2e):
    from services import container_receipt, containers

    ids = e2e.ids
    bracket = e2e.run(container_receipt.create_product("Ёлочный кронштейн", unit="компл"))["product_id"]
    cid = e2e.run(containers.create_container(number="CATU0000001", created_by=ids["boss"]))["container_id"]
    products_before = e2e.rows("SELECT COUNT(*) AS n FROM products")[0]["n"]

    mgr = open_app(ids["mgr"])
    _open_container(mgr, cid)

    # 1. Выбор из каталога: список виден сразу, с остатком; поиск «елоч» находит «Ёлочный».
    mgr.click("#cont-item-add")
    mgr.wait_for_selector(f'.picker-list [data-product="{ids["product"]}"]')
    assert "остаток 20 шт" in mgr.inner_text(f'.picker-list [data-product="{ids["product"]}"]')
    mgr.fill("#ms-f-search", "елоч")
    mgr.wait_for_function(
        "(id) => { const rows = document.querySelectorAll('.picker-list [data-product]');"
        " return rows.length === 1 && rows[0].dataset.product === String(id); }", arg=bracket)
    mgr.click(f'.picker-list [data-product="{bracket}"]')
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#cont-item-product:has-text('Ёлочный кронштейн')")
    assert mgr.locator("#ms-f-name").count() == 0, "название не вписывают"
    mgr.fill("#ms-f-expected_qty", "4")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Позиция добавлена')")
    _no_overlay(mgr)

    # 2. «Новый товар» с именем, которое в каталоге уже есть (регистр/пробелы):
    #    сервер не принимает молча и предлагает карточку.
    mgr.wait_for_selector("#cont-item-add")
    mgr.click("#cont-item-add")
    mgr.wait_for_selector("#picker-new-product")
    mgr.fill("#ms-f-search", "кабель  ввг 3X2.5")
    mgr.click("#picker-new-product")
    mgr.wait_for_selector("#ms-f-name")
    mgr.fill("#ms-f-expected_qty", "6")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".c-overlay #ms-error:has-text('уже есть')")
    mgr.click(f'[data-existing="{ids["product"]}"]')
    mgr.wait_for_selector("#cont-item-product:has-text('Кабель ВВГ 3x2.5')")
    assert mgr.input_value("#ms-f-expected_qty") == "6", "количество не потерялось"
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Позиция добавлена')")
    _no_overlay(mgr)

    # 3. Действительно новый товар — свободным текстом, карточка пока не заводится.
    mgr.click("#cont-item-add")
    mgr.wait_for_selector("#picker-new-product")
    mgr.fill("#ms-f-search", "Гидроцилиндр ковша")
    mgr.wait_for_selector(".picker-list:has-text('В каталоге не найдено')")
    mgr.click("#picker-new-product")
    mgr.wait_for_selector("#ms-f-name")
    mgr.fill("#ms-f-expected_qty", "2")
    mgr.click("#ms-submit")
    mgr.wait_for_function("() => document.querySelectorAll('[data-item-del]').length === 3")
    _no_overlay(mgr)

    items = {r["name"]: r["id"] for r in e2e.rows(
        "SELECT id, name FROM container_items WHERE container_id = ?", (cid,))}
    links = {r["item_id"]: r["product_id"] for r in e2e.rows(
        "SELECT item_id, product_id FROM container_item_products WHERE container_id = ?", (cid,))}
    assert links == {items["Ёлочный кронштейн"]: bracket, items["Кабель ВВГ 3x2.5"]: ids["product"]}
    assert e2e.rows("SELECT COUNT(*) AS n FROM products")[0]["n"] == products_before

    # 4. Прибытие → сверка → подтверждение нового товара → приход.
    mgr.click("#cont-arrive")
    mgr.wait_for_selector(".qty-input[data-item]")
    for name, qty in (("Ёлочный кронштейн", "4"), ("Кабель ВВГ 3x2.5", "5"), ("Гидроцилиндр ковша", "2")):
        mgr.fill(f'.qty-input[data-item="{items[name]}"]', qty)
    mgr.click("#cont-save")
    new_item = items["Гидроцилиндр ковша"]
    mgr.wait_for_selector(f'#receipt-review [data-review="{new_item}"]')
    assert mgr.locator("#receipt-review [data-review]").count() == 1, "в выборе только непривязанная"
    mgr.click(f'[data-review-new="{new_item}"]')
    mgr.click(".c-overlay #ms-submit")
    mgr.wait_for_selector(".toast:has-text('новых товаров в каталоге: 1')")
    _no_overlay(mgr)

    cyl = e2e.rows("SELECT id, unit FROM products WHERE name = 'Гидроцилиндр ковша'")
    assert len(cyl) == 1
    assert e2e.rows("SELECT COUNT(*) AS n FROM products")[0]["n"] == products_before + 1, "дублей нет"
    inv = e2e.rows("SELECT invoice_id FROM container_receipt WHERE container_id = ?", (cid,))[0]["invoice_id"]
    lines = {r["product_id"]: float(r["quantity"]) for r in e2e.rows(
        "SELECT product_id, quantity FROM invoice_items WHERE invoice_id = ?", (inv,))}
    assert lines == {bracket: 4.0, ids["product"]: 5.0, cyl[0]["id"]: 2.0}
    assert _stock(e2e, bracket) == 4
    assert _stock(e2e, ids["product"]) == 25
    assert _stock(e2e, cyl[0]["id"]) == 2
    mgr.wait_for_selector("#cont-supply:has-text('Переоприходовать')")
    assert "Приходная накладная проведена" in mgr.inner_text("#content")


def test_manager_marks_machine_in_transit_arrived(open_app, e2e):
    from services import machines

    ids = e2e.ids
    mid = e2e.run(machines.create_machine(
        vin="CAT320D-0001", name="CAT 320D", created_by=ids["boss"], location="Порт",
    ))["machine_id"]
    other = e2e.run(machines.create_machine(
        vin="CAT320D-0002", name="CAT 320D №2", created_by=ids["boss"], status="in_stock",
    ))["machine_id"]

    mgr = open_app(ids["mgr"])
    go(mgr, "stock")
    tab(mgr, "machines")
    mgr.wait_for_selector(f'[data-machine="{other}"]')
    mgr.click(f'[data-machine="{mid}"]')
    mgr.wait_for_selector('[data-mact="hours"]')
    assert mgr.locator('[data-mact="arrive"]').count() == 1, "«Прибыла» у машины в пути"
    assert "Прибыла" in mgr.inner_text('[data-mact="arrive"]')

    mgr.click('[data-mact="arrive"]')
    mgr.wait_for_selector("#ms-f-location")
    assert mgr.input_value("#ms-f-location") == "Порт"
    mgr.fill("#ms-f-location", "Ташкент, Сергели")
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('Машина на складе')")
    _no_overlay(mgr)
    mgr.wait_for_function("() => !document.querySelector('[data-mact=\"arrive\"]')")
    assert "На складе" in mgr.inner_text("#content")
    assert "Ташкент, Сергели" in mgr.inner_text("#content")

    row = e2e.rows("SELECT status, location FROM machines WHERE id = ?", (mid,))[0]
    assert row == {"status": "in_stock", "location": "Ташкент, Сергели"}
    audit = e2e.rows("SELECT user_id, details FROM audit_log WHERE action = 'machine_arrived'")
    assert len(audit) == 1 and audit[0]["user_id"] == ids["mgr"]

    # У машины на складе кнопки нет.
    go(mgr, "stock")
    tab(mgr, "machines")
    mgr.wait_for_selector(f'[data-machine="{other}"]')
    mgr.click(f'[data-machine="{other}"]')
    mgr.wait_for_selector('[data-mact="hours"]')
    assert mgr.locator('[data-mact="arrive"]').count() == 0
