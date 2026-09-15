"""E2E, покрытие раздела «Продажи»: каждая кнопка — и её последствие.

Первые две волны (`test_webapp_flows.py`, `test_sales_money.py`) прошли
главные дороги: заказ → заявка → одобрение, отказ, доработка, отгрузка
кладовщиком, удаление черновика, расписка. Здесь — всё остальное, что
нажимается в «Продажах»: фильтры по статусу и периоду, календарь, выбор и
смена клиента, поиск товара, валюта, удаление позиции, проверки диалога
количества, заморозка после серии доработок, отмена заказа с возвратом
остатка, отгрузка боссом и её гонка, периоды и выгрузка отчёта, реквизиты
компании, все три типа документов, повторная отправка и печать — и матрица
ролей: что видно и что сервер отказывает.

Проверяем не разметку, а последствия: строки в БД, остаток, статус заказа,
тексты диалогов, исходящие сообщения бота. Внешний мир (Telegram, CUPS,
LibreOffice) подменён, как и в соседних файлах.
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, timedelta
from pathlib import Path

from tests.e2e.conftest import go, seed_order, settled, tab

import pytest

# Руководитель здесь делает работу менеджера — с «Рабочими действиями»
# (conftest.boss_work_actions). Вид по умолчанию — test_boss_ui.py.
pytestmark = pytest.mark.usefixtures("boss_work_actions")

# ─── Хелперы ─────────────────────────────────────────────────────────────────


def _orders(page) -> None:
    """Открыть «Продажи → Заказы» и дождаться списка с живого сервера."""
    go(page, "sales")
    page.wait_for_selector('.seg-item[data-filter="all"]')
    settled(page)


def _card_ids(page) -> set[int]:
    return set(page.eval_on_selector_all(".order-card[data-id]", "els => els.map(e => +e.dataset.id)"))


def _filter(page, key: str) -> None:
    page.click(f'.seg-item[data-filter="{key}"]')
    page.wait_for_selector(f'.seg-item.active[data-filter="{key}"]')
    # Фильтр уходит на сервер (страницы /api/orders) — ждём ответ, а не кадр
    # со скелетоном.
    settled(page)


def _period(page, key: str) -> None:
    page.click(f'[data-operiod="{key}"]')
    page.wait_for_selector(f'.seg-item.active[data-operiod="{key}"]')
    settled(page)


def _digits(text: str) -> str:
    """Цифры подряд: formatMoney разделяет разряды неразрывным пробелом."""
    return re.sub(r"\D", "", text or "")


def _wait_alert(page, needle: str, count: int = 1) -> None:
    """Дождаться `count`-го диалога Telegram, содержащего `needle`."""
    page.wait_for_function(
        "([n, c]) => window.__tgAlerts.filter(a => a.includes(n)).length >= c", arg=[needle, count]
    )


def _confirm_answers_no(page) -> None:
    """Заглушка отвечает «да» всегда; для ветки «Отмена» переучиваем её на месте."""
    page.evaluate(
        "() => { window.Telegram.WebApp.showConfirm = (m, cb) => {"
        " window.__tgAlerts.push('confirm-no:' + m); if (cb) cb(false); }; }"
    )


def _confirm_answers_yes(page) -> None:
    page.evaluate(
        "() => { window.Telegram.WebApp.showConfirm = (m, cb) => {"
        " window.__tgAlerts.push('confirm:' + m); if (cb) cb(true); }; }"
    )


def _back(page) -> None:
    """Нативная «Назад» Telegram: в заглушке onClick — noop, поэтому зовём
    запомненный app.js обработчик напрямую (тот же, что нажала бы кнопка)."""
    page.evaluate("() => { if (_backHandler) _backHandler(); }")


def _api(page, path: str, body: dict | None = None) -> dict:
    """Запрос к серверу от имени пользователя этой страницы — как это делает app.js."""
    return page.evaluate(
        """async ([path, body]) => {
            const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({initData: window.Telegram.WebApp.initData, ...body})});
            let data = {};
            try { data = await r.json(); } catch (e) {}
            return {status: r.status, body: data};
        }""",
        [path, body or {}],
    )


def _browser_today(page) -> date:
    return date.fromisoformat(page.evaluate(
        "() => { const d = new Date(), p = n => String(n).padStart(2, '0');"
        " return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; }"
    ))


def _order(e2e, oid: int) -> dict:
    return e2e.rows("SELECT * FROM orders WHERE id = ?", (oid,))[0]


def _stock(e2e) -> float:
    return e2e.rows("SELECT quantity FROM stock WHERE product_id = ?", (e2e.ids["product"],))[0]["quantity"]


def _cp(e2e) -> int:
    return e2e.rows("SELECT id FROM counterparties ORDER BY id LIMIT 1")[0]["id"]


def _seed_draft(e2e, *qtys: float, currency: str | None = None) -> int:
    """Черновик менеджера на «Ромашку» с позициями заданных количеств."""
    db, ids = e2e.db, e2e.ids
    oid = db.create_order(ids["mgr"], "Manager", "")
    db.update_order_agent(oid, str(_cp(e2e)), "ООО Ромашка")
    if currency:
        db.update_order_currency(oid, currency)
    for q in qtys:
        db.add_order_item(oid, "Кабель ВВГ 3x2.5", "", q, "м", 100.0, product_id=ids["product"])
    return oid


def _fake_documents(monkeypatch, tmp_path) -> list[dict]:
    """LibreOffice → файл-заглушка; контексты шаблона копятся в список."""
    from services import documents

    monkeypatch.setenv("DOCUMENTS_DIR", str(tmp_path / "docs"))
    contexts: list[dict] = []

    def _write(doc_type, context, out_dir):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"{doc_type}-{len(contexts)}.pdf"
        p.write_bytes(b"%PDF-1.4\n" + context["debtor_full_name"].encode())
        return p

    async def fake_render(doc_type, context, out_dir, template_override=None):
        contexts.append({"doc_type": doc_type, **context})
        return await asyncio.to_thread(_write, doc_type, context, out_dir)

    monkeypatch.setattr(documents, "render_pdf", fake_render)
    return contexts


_FULL_COMPANY = {
    "company_name": "ООО Тест", "company_tin": "305123456", "company_address": "Самарканд, ул. Регистан 1",
    "company_representative": "Петров Пётр", "company_city": "Самарканд",
    "company_position": "Директор", "company_position_uz": "Директор",
    "company_representative_gen": "директора Петрова Петра", "company_city_uz": "Самарқанд",
}


# ─── Роли: вкладки, кнопки, отказы сервера ───────────────────────────────────


def _sect_tabs(page) -> list[str]:
    return page.eval_on_selector_all(".seg-item[data-sect]", "els => els.map(e => e.dataset.sect)")


def _filters(page) -> list[str]:
    return page.eval_on_selector_all(".seg-item[data-filter]", "els => els.map(e => e.dataset.filter)")


def test_sales_tabs_and_controls_follow_role(open_app, e2e):
    """Кто что видит в «Продажах».

    Руководство: три вкладки, заявки вместо «Нового заказа», фильтр
    «Отгружены» вместо «Черновиков». Менеджер: три вкладки, «Новый заказ»,
    черновики. Кладовщик и бухгалтер: только «Заказы» — отчёт и документы им
    отвечают 403, и вкладок нет.
    """
    seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    ids = e2e.ids
    for role in ("boss", "admin"):
        page = open_app(ids[role])
        _orders(page)
        assert _sect_tabs(page) == ["orders", "report", "docs"], role
        assert _filters(page) == ["all", "pending", "approved", "shipped", "rejected"], role
        assert page.locator("#btn-new-order").count() == 0, role
        # Счётчик заявок на строке-ссылке совпадает с числом ожидающих.
        assert page.locator("#show-requests .queue-count").inner_text().strip() == "1", role

    mgr = open_app(ids["mgr"])
    _orders(mgr)
    assert _sect_tabs(mgr) == ["orders", "report", "docs"]
    assert _filters(mgr) == ["all", "draft", "pending", "approved", "rejected"]
    assert mgr.locator("#btn-new-order").count() == 1
    assert mgr.locator("#show-requests").count() == 0, "заявки разбирает только руководство"
    # Имя менеджера на карточке — только у руководства; своему — незачем.
    assert "Manager" not in mgr.locator(".order-card .order-sub").first.inner_text()

    for role in ("keeper", "book"):
        page = open_app(ids[role])
        _orders(page)
        assert _sect_tabs(page) == [], f"{role}: единственная вкладка — переключателя нет"
        assert page.locator("#show-requests").count() == 0, role
        text = page.locator("#content").inner_text()
        assert "Нет доступа" not in text and "Ошибка" not in text, role


def test_new_order_button_hidden_for_roles_that_cannot_create(open_app, e2e):
    """Кнопка, которая гарантированно упрётся в 403, — дверь, которая не
    открывается (правило из helpers.js NAV_SECTIONS). «Новый заказ» рисовался
    по условию `!isBoss`, то есть и кладовщику, и бухгалтеру."""
    for role in ("keeper", "book"):
        page = open_app(e2e.ids[role])
        _orders(page)
        assert page.locator("#btn-new-order").count() == 0, role


def test_forbidden_sales_actions_are_refused_by_server(open_app, e2e):
    """Скрытая кнопка — не защита: те же действия прямым запросом получают 403,
    и в БД ничего не меняется."""
    ids = e2e.ids
    pending = seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    approved = seed_order(e2e)
    draft = _seed_draft(e2e, 1)
    orders_before = e2e.rows("SELECT COUNT(*) AS n FROM orders")[0]["n"]
    docs_before = len(e2e.bot.documents)

    matrix = {
        "keeper": [
            ("/api/orders/create", {}),
            ("/api/orders/cancel", {"order_id": approved["order_id"], "reason": "Просто так"}),
            ("/api/requests/approve", {"req_id": pending["req_id"]}),
            ("/api/orders/requests", {}),
            ("/api/analytics", {"period": "month"}),
            ("/api/docs/types", {}),
            ("/api/docs/create", {"doc_type": "raspiska_ru"}),
        ],
        "book": [
            ("/api/orders/create", {}),
            ("/api/orders/ship", {"order_id": approved["order_id"]}),
            ("/api/docs/list", {}),
            ("/api/analytics", {"period": "month"}),
        ],
        "mgr": [
            ("/api/requests/approve", {"req_id": pending["req_id"]}),
            ("/api/requests/reject", {"req_id": pending["req_id"]}),
            ("/api/requests/return_to_draft", {"req_id": pending["req_id"], "comment": "Исправьте"}),
            # /api/orders/ship менеджеру пока открыт: он замещает кладовщика
            # (ROLE_ALSO_ACTS_AS, tests/e2e/test_manager_acts_as.py).
            ("/api/orders/cancel", {"order_id": approved["order_id"], "reason": "Просто так"}),
            ("/api/orders/requests", {}),
            ("/api/orders/unfreeze", {"order_id": draft}),
            ("/api/analytics/export", {"period": "month"}),
            ("/api/docs/company/set", {"company": {"company_name": "Взлом"}}),
        ],
        "boss": [
            # Разморозка — только администратор.
            ("/api/orders/unfreeze", {"order_id": draft}),
            # Чужой черновик не правит и не удаляет даже руководство.
            ("/api/orders/add_item", {"order_id": draft, "product_name": "X", "quantity": 1}),
            ("/api/orders/delete_draft", {"order_id": draft}),
        ],
    }
    for role, calls in matrix.items():
        page = open_app(ids[role])
        for path, body in calls:
            res = _api(page, path, body)
            assert res["status"] == 403, f"{role} {path}: {res}"

    assert e2e.rows("SELECT status FROM shipment_requests WHERE id = ?", (pending["req_id"],))[0]["status"] == "pending"
    assert _order(e2e, approved["order_id"])["status"] == "approved"
    assert _order(e2e, draft)["status"] == "draft"
    assert e2e.rows("SELECT COUNT(*) AS n FROM order_items WHERE order_id = ?", (draft,))[0]["n"] == 1
    assert e2e.rows("SELECT COUNT(*) AS n FROM orders")[0]["n"] == orders_before
    assert e2e.db.get_setting("company_name", "") != "Взлом"
    assert len(e2e.bot.documents) == docs_before, "выгрузка Excel не ушла"

    # Бухгалтеру список отвечает — пустой; кладовщику — только его работа.
    book = open_app(ids["book"])
    assert _api(book, "/api/orders")["body"]["orders"] == []
    keeper = open_app(ids["keeper"])
    statuses = {o["status"] for o in _api(keeper, "/api/orders")["body"]["orders"]}
    assert statuses == {"approved"}


# ─── Редактор заказа ─────────────────────────────────────────────────────────


def test_manager_builds_uzs_credit_order_step_by_step(open_app, e2e):
    """Каждая кнопка редактора с проверкой того, что доехало до сервера.

    Поиск и смена клиента, поиск товара, валюта UZS и её фиксация после первой
    позиции, удаление позиции, «В долг» без даты (отказ фронта) и с датой.
    """
    ids = e2e.ids
    e2e.exec(
        "INSERT INTO counterparties (name, type, phone, created_at) VALUES (?, ?, ?, ?)",
        ("ИП Васильев Сергей", "customer", "+998907654321", e2e.db.now_str()),
    )
    cp2 = e2e.rows("SELECT id FROM counterparties WHERE name = ?", ("ИП Васильев Сергей",))[0]["id"]

    mgr = open_app(ids["mgr"])
    _orders(mgr)
    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#choose-agent")
    assert mgr.locator("#btn-submit").is_disabled(), "без клиента и товаров отправлять нечего"
    oid = int(re.search(r"#(\d+)", mgr.locator(".editor-title").inner_text()).group(1))

    # Клиент: поиск сужает список до одного.
    mgr.click("#choose-agent")
    mgr.wait_for_selector(".agent-row")
    mgr.fill("#agent-search", "Васил")
    mgr.wait_for_function(
        "() => { const r = document.querySelectorAll('.agent-row');"
        " return r.length === 1 && r[0].textContent.includes('Васильев'); }"
    )
    mgr.click(".agent-row")
    mgr.wait_for_selector("#change-agent")
    o = _order(e2e, oid)
    assert (o["agent_id"], o["agent_name"]) == (str(cp2), "ИП Васильев Сергей")

    # «Изменить» — клиент меняется и на сервере.
    mgr.click("#change-agent")
    mgr.wait_for_selector('.agent-row:has-text("Ромашка")')
    mgr.fill("#agent-search", "нет такого клиента")
    mgr.wait_for_selector(".loader:has-text('Клиенты не найдены')")
    mgr.fill("#agent-search", "Ром")
    mgr.wait_for_function("() => document.querySelectorAll('.agent-row').length === 1")
    mgr.click('.agent-row:has-text("Ромашка")')
    mgr.wait_for_selector(".agent-selected:has-text('Ромашка')")
    o = _order(e2e, oid)
    assert (o["agent_id"], o["agent_name"]) == (str(_cp(e2e)), "ООО Ромашка")

    # Товар: поиск по каталогу.
    mgr.click("#btn-add-product")
    mgr.wait_for_selector(".prod-row")
    mgr.fill("#prod-search", "экскаватор")
    mgr.wait_for_selector(".loader:has-text('Товары не найдены')")
    mgr.fill("#prod-search", "кабель")
    mgr.wait_for_selector(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")
    assert "20" in mgr.locator(".qty-stock").first.inner_text(), "доступный остаток в диалоге"

    mgr.click('.cur-btn[data-cur="UZS"]')
    mgr.wait_for_selector('.cur-btn.active[data-cur="UZS"]')
    mgr.fill("#qty-input", "2")
    mgr.fill("#price-input", "150000")
    mgr.wait_for_function("() => document.querySelector('#line-total').textContent.includes('UZS')")
    assert _digits(mgr.locator("#line-total").inner_text()) == "300000"
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_selector(".toast:has-text('Товар добавлен')")
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    assert _order(e2e, oid)["currency"] == "UZS"
    assert not mgr.evaluate("window.Telegram.WebApp.MainButton.isVisible"), "MainButton снята"

    # Вторая позиция: валюта уже зафиксирована.
    mgr.click("#btn-add-product")
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")
    assert mgr.locator('.cur-btn[data-cur="USD"]').is_disabled()
    assert mgr.locator('.cur-btn.active[data-cur="UZS"]').count() == 1
    mgr.fill("#qty-input", "1")
    mgr.fill("#price-input", "100000")
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_function("() => document.querySelectorAll('.editor-item-del').length === 2")
    assert _digits(mgr.locator(".editor-item--total").inner_text()) == "400000"
    assert e2e.rows("SELECT COUNT(*) AS n FROM order_items WHERE order_id = ?", (oid,))[0]["n"] == 2

    # Удалить вторую позицию — она уходит и с сервера.
    mgr.click('.editor-item-del[data-idx="1"]')
    mgr.wait_for_function("() => document.querySelectorAll('.editor-item-del').length === 1")
    items = e2e.rows("SELECT quantity, price_cents FROM order_items WHERE order_id = ?", (oid,))
    assert items == [{"quantity": 2, "price_cents": 15_000_000}]

    # «В долг» без даты — фронт не отправляет.
    mgr.click('[data-pay="credit"]')
    mgr.wait_for_selector("#due-date-wrap:not(.hidden)")
    mgr.click("#btn-submit")
    _wait_alert(mgr, "Укажите дату возврата")
    assert e2e.rows("SELECT COUNT(*) AS n FROM shipment_requests")[0]["n"] == 0
    # Передумал: «Оплачено сразу» прячет дату, «В долг» — снова показывает.
    mgr.click('[data-pay="paid"]')
    mgr.wait_for_selector("#due-date-wrap.hidden", state="attached")
    mgr.click('[data-pay="credit"]')
    mgr.wait_for_selector("#due-date-wrap:not(.hidden)")
    mgr.fill("#due-date-input", "2030-01-15")
    mgr.click("#btn-submit")
    _wait_alert(mgr, "отправлена")

    req = e2e.rows("SELECT id, status, order_id FROM shipment_requests")
    assert len(req) == 1 and req[0]["status"] == "pending" and req[0]["order_id"] == oid
    o = _order(e2e, oid)
    assert (o["status"], o["payment_type"], o["due_date"], o["currency"]) == ("pending", "credit", "2030-01-15", "UZS")
    # Руководству ушла заявка с кнопками одобрения именно этой заявки.
    push = [p for p in e2e.pushes if f"req_ok:{req[0]['id']}" in str(p.get("reply_markup"))]
    assert push and push[0]["uid"] == ids["boss"]

    # Вернулись в список: карточка показывает статус, долг, валюту.
    card = mgr.locator(f'.order-card[data-id="{oid}"]')
    card.wait_for()
    text = card.inner_text()
    assert card.get_attribute("data-status") == "pending"
    assert "В долг до 15.01.2030" in text and "UZS" in text
    assert _digits(card.locator(".order-total").inner_text()) == "300000"


def test_quantity_dialog_validates_and_min_price_applies(open_app, e2e):
    """Диалог количества: пустое количество и цена ниже минимальной не
    добавляют позицию; пустая цена подставляет минимальную цену продажи."""
    ids = e2e.ids
    ok, err = e2e.db.set_product_price(str(ids["product"]), "Кабель ВВГ 3x2.5", 50.0, None, "USD", ids["boss"])
    assert ok, err

    mgr = open_app(ids["mgr"])
    _orders(mgr)
    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#btn-add-product")
    oid = int(re.search(r"#(\d+)", mgr.locator(".editor-title").inner_text()).group(1))
    mgr.click("#btn-add-product")
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")

    mgr.evaluate("window.__tgMainClick()")
    _wait_alert(mgr, "Введите количество")

    mgr.fill("#qty-input", "1")
    mgr.fill("#price-input", "10")
    mgr.evaluate("window.__tgMainClick()")
    _wait_alert(mgr, "ниже минимальной")
    assert e2e.rows("SELECT COUNT(*) AS n FROM order_items WHERE order_id = ?", (oid,))[0]["n"] == 0
    assert mgr.locator("#qty-input").count() == 1, "диалог остался открытым"

    mgr.fill("#price-input", "")
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_selector(".editor-item-del")
    assert e2e.rows("SELECT price_cents FROM order_items WHERE order_id = ?", (oid,)) == [{"price_cents": 5000}]

    # «Назад» из диалога количества и из каталога — обратно в редактор,
    # MainButton не остаётся висеть на чужом экране.
    mgr.click("#btn-add-product")
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")
    _back(mgr)
    mgr.wait_for_selector("#prod-search")
    assert not mgr.evaluate("window.Telegram.WebApp.MainButton.isVisible")
    _back(mgr)
    mgr.wait_for_selector("#btn-add-product")
    assert mgr.locator(".editor-item-del").count() == 1


def _new_order_for_romashka(mgr, e2e) -> None:
    """Новый черновик на «Ромашку», открытый на форме количества и цены."""
    _orders(mgr)
    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#choose-agent")
    mgr.click("#choose-agent")
    mgr.wait_for_selector('.agent-row:has-text("Ромашка")')
    mgr.click('.agent-row:has-text("Ромашка")')
    mgr.wait_for_selector("#change-agent")
    mgr.click("#btn-add-product")
    mgr.wait_for_selector(f'.prod-row[data-product="{e2e.ids["product"]}"]')
    mgr.click(f'.prod-row[data-product="{e2e.ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")


def test_repeat_client_gets_last_price_prefilled(open_app, e2e):
    """B7/D4: тот же клиент, тот же товар — во второй раз цена уже в поле.

    Первый заказ вводят с нуля (истории нет, цена товара не задана — поле
    пустое, как было до подсказок). Второй заказ тому же клиенту открывает
    форму с прошлой ценой и подписью «Прошлый раз: 45 USD (…)»."""
    mgr = open_app(e2e.ids["mgr"])

    _new_order_for_romashka(mgr, e2e)
    settled(mgr)
    assert mgr.locator("#price-input").input_value() == "", "истории нет — поле пустое"
    assert mgr.locator("#price-hint").text_content() == ""
    mgr.fill("#qty-input", "2")
    mgr.fill("#price-input", "45")
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_selector(".toast:has-text('Товар добавлен')")
    mgr.click('[data-pay="credit"]')
    mgr.wait_for_selector("#due-date-wrap:not(.hidden)")
    mgr.fill("#due-date-input", "2030-01-15")
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    mgr.click("#btn-submit")
    _wait_alert(mgr, "отправлена")

    _new_order_for_romashka(mgr, e2e)
    mgr.wait_for_function("() => document.getElementById('price-input').value === '45'")
    assert "Прошлый раз: 45 USD" in mgr.locator("#price-hint").text_content()
    # Подсказка — дефолт, а не замок: цену правят руками, и сервер её берёт.
    mgr.fill("#price-input", "60")
    mgr.fill("#qty-input", "1")
    mgr.evaluate("window.__tgMainClick()")
    mgr.wait_for_selector(".toast:has-text('Товар добавлен')")
    prices = e2e.rows(
        "SELECT price_cents FROM order_items ORDER BY id DESC LIMIT 1"
    )
    assert prices == [{"price_cents": 6000}]


def _seed_catalog(e2e, pipes: int = 55) -> None:
    """Кабель — в «Кабели», плюс `pipes` труб без остатка в «Трубы»."""
    e2e.exec("UPDATE products SET category = ? WHERE id = ?", ("Кабели", e2e.ids["product"]))
    for n in range(1, pipes + 1):
        e2e.exec(
            "INSERT INTO products (name, category, unit, created_at) VALUES (?, ?, ?, ?)",
            (f"Труба {n:02d}", "Трубы", "шт", e2e.db.now_str()),
        )


def _prod_rows(page) -> int:
    return page.locator(".prod-row").count()


def test_product_picker_categories_search_and_show_more(open_app, e2e):
    """Каталог в заказе: категории, «Показать ещё» по 50, поиск внутри категории."""
    _seed_catalog(e2e)
    mgr = open_app(e2e.ids["mgr"])
    _orders(mgr)
    mgr.click("#btn-new-order")
    mgr.click("#btn-add-product")
    mgr.wait_for_selector(".prod-row")
    cats = mgr.eval_on_selector_all(".cat-btn", "els => els.map(e => e.dataset.cat)")
    assert cats == ["all", "Кабели", "Трубы"]
    assert _prod_rows(mgr) == 50
    assert "(6)" in mgr.locator("#prod-more").inner_text()
    mgr.click("#prod-more")
    mgr.wait_for_function("() => document.querySelectorAll('.prod-row').length === 56")
    assert mgr.locator("#prod-more").count() == 0

    mgr.click('.cat-btn[data-cat="Кабели"]')
    mgr.wait_for_selector('.cat-btn[data-cat="Кабели"][aria-pressed="true"]')
    assert _prod_rows(mgr) == 1
    assert mgr.locator(f'.prod-row[data-product="{e2e.ids["product"]}"]').count() == 1

    mgr.click('.cat-btn[data-cat="Трубы"]')
    mgr.wait_for_function("() => document.querySelectorAll('.prod-row').length === 50")
    assert "(5)" in mgr.locator("#prod-more").inner_text(), "смена категории — список с начала"
    mgr.fill("#prod-search", "труба 5")
    mgr.wait_for_function("() => document.querySelectorAll('.prod-row').length === 6")
    assert mgr.locator("#prod-more").count() == 0
    assert mgr.locator('.cat-btn[data-cat="all"]').get_attribute("aria-pressed") == "false"


def test_product_picker_available_counts_approved_order_once(open_app, e2e):
    """На приходе 20, одобрен заказ на 2. Одобрение уже провело расходную
    накладную (stock = 18). Раньше `_reserved_by_product` ещё раз резервировал
    те же 2 штуки по статусу approved — менеджер видел «доступно 16», и два
    ящика нельзя было продать, пока кладовщик не нажмёт «Отгрузить». Резерв —
    только то, что ещё не списано: одобренная заявка на 3 без накладной
    (отгрузка не прошла) из доступного вычитается."""
    from services import order_shipment

    ids = e2e.ids
    seed_order(e2e)
    assert _stock(e2e) == 18
    unshipped = seed_order(e2e, approve=False, qty=3)["order_id"]
    e2e.exec("UPDATE orders SET status = 'approved' WHERE id = ?", (unshipped,))
    e2e.run(order_shipment._remember_failure(unshipped, "не хватило"))
    mgr = open_app(ids["mgr"])
    _orders(mgr)
    mgr.click("#btn-new-order")
    mgr.click("#btn-add-product")
    row = mgr.locator(f'.prod-row[data-product="{ids["product"]}"]')
    row.wait_for()
    assert float(row.get_attribute("data-stock")) == 15, "18 на складе − 3 в резерве; списанные 2 — не резерв"


def test_new_order_reuses_empty_draft_and_back_returns_to_list(open_app, e2e):
    """«Новый заказ» → «Назад» → «Новый заказ»: тот же пустой черновик, а не второй."""
    mgr = open_app(e2e.ids["mgr"])
    _orders(mgr)
    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#choose-agent")
    first = mgr.locator(".editor-title").inner_text()

    _back(mgr)
    mgr.wait_for_selector("#btn-new-order")
    oid = e2e.rows("SELECT id FROM orders WHERE user_id = ?", (e2e.ids["mgr"],))[0]["id"]
    card = mgr.locator(f'.order-card[data-id="{oid}"]')
    card.wait_for()
    assert "Без клиента" in card.inner_text() and card.get_attribute("data-status") == "draft"

    mgr.click("#btn-new-order")
    mgr.wait_for_selector("#choose-agent")
    assert mgr.locator(".editor-title").inner_text() == first
    assert e2e.rows("SELECT COUNT(*) AS n FROM orders WHERE user_id = ?", (e2e.ids["mgr"],))[0]["n"] == 1


def test_reopened_draft_item_removal_reaches_server(open_app, e2e):
    """Черновик открыт из списка («Редактировать»), позицию удалили крестиком.

    openOrderEditor подставлял позициям `item_id: i` — порядковый номер: у
    первой это 0, и `if (item.item_id)` запрос не слал вовсе; у второй — 1,
    то есть id ЧУЖОЙ позиции (сервер отвечал 403, ошибка проглатывалась).
    На экране позиции не было, а в заявку боссу она уходила. Теперь позиция
    удаляется по своему id, а отказ сервера виден и строку не убирает.
    """
    other = _seed_draft(e2e, 7)  # позиции чужого заказа занимают младшие id
    oid = _seed_draft(e2e, 1, 3, 5)
    mgr = open_app(e2e.ids["mgr"])
    _orders(mgr)
    mgr.click(f'.btn-edit-order[data-id="{oid}"]')
    mgr.wait_for_function("() => document.querySelectorAll('.editor-item-del').length === 3")
    mgr.click('.editor-item-del[data-idx="0"]')
    mgr.wait_for_function("() => document.querySelectorAll('.editor-item-del').length === 2")
    mgr.click('.editor-item-del[data-idx="1"]')
    mgr.wait_for_function("() => document.querySelectorAll('.editor-item-del').length === 1")
    assert e2e.rows("SELECT quantity FROM order_items WHERE order_id = ?", (other,)) == [{"quantity": 7}]

    # Отказ сервера (позицию уже удалили в другой вкладке) — сообщение, а
    # строка остаётся: экран не должен расходиться с заявкой молча.
    e2e.exec("DELETE FROM order_items WHERE order_id = ?", (oid,))
    mgr.click('.editor-item-del[data-idx="0"]')
    _wait_alert(mgr, "Позиция не удалена")
    assert mgr.locator(".editor-item-del").count() == 1
    e2e.db.add_order_item(oid, "Кабель ВВГ 3x2.5", "", 3, "м", 100.0, product_id=e2e.ids["product"])

    mgr.click("#btn-submit")
    _wait_alert(mgr, "отправлена")
    items = e2e.rows("SELECT quantity FROM order_items WHERE order_id = ?", (oid,))
    assert items == [{"quantity": 3}], "в заявке ровно то, что осталось на экране"


def test_reopened_draft_keeps_currency_locked(open_app, e2e):
    """Все позиции заказа — в одной валюте; фронт фиксирует её после первой
    позиции. openOrderEditor не переносил `currency` из списка, и в повторно
    открытом черновике переключатель был снова свободен, а сервер молча
    оставлял прежнюю валюту: цена 250 000 «в сумах» ложилась в долларовый
    заказ как 250 000 USD. Теперь валюта зафиксирована и на экране, и на
    сервере (другая валюта в заказе с позициями — отказ)."""
    oid = _seed_draft(e2e, 1, currency="USD")
    ids = e2e.ids
    mgr = open_app(ids["mgr"])
    _orders(mgr)
    mgr.click(f'.btn-edit-order[data-id="{oid}"]')
    mgr.wait_for_selector(".editor-item-del")
    mgr.click("#btn-add-product")
    mgr.click(f'.prod-row[data-product="{ids["product"]}"]')
    mgr.wait_for_selector("#qty-input")
    assert mgr.locator('.cur-btn[data-cur="UZS"]').is_disabled(), "валюта заказа уже USD"
    assert mgr.locator('.cur-btn.active[data-cur="USD"]').count() == 1

    # Мимо экрана (старый клиент, ретрай) — сервер отказывает, а не пишет доллары.
    res = _api(mgr, "/api/orders/add_item", {
        "order_id": oid, "product_name": "Кабель ВВГ 3x2.5", "product_id": str(ids["product"]),
        "quantity": 1, "unit": "м", "price": 250000, "currency": "UZS",
    })
    assert res["status"] == 409 and "USD" in res["body"]["detail"], res
    assert _order(e2e, oid)["currency"] == "USD"
    assert e2e.rows("SELECT COUNT(*) AS n FROM order_items WHERE order_id = ?", (oid,))[0]["n"] == 1


def test_reopened_draft_keeps_payment_type(open_app, e2e):
    """Заявку «в долг» вернули на доработку. Менеджер открыл черновик и нажал
    «Отправить» — редактор не брал `payment_type`/`due_date` из заказа, и
    заявка уходила как «оплачено сразу» (после одобрения — ожидающий платёж на
    всю сумму вместо долга)."""
    from services.order_workflow import return_order_to_draft

    seeded = seed_order(e2e, payment_type="credit", due_date="2030-01-15", approve=False)
    ids = e2e.ids
    assert e2e.run(return_order_to_draft(seeded["req_id"], ids["boss"], "Boss", "Проверьте цену", e2e.bot))["ok"]

    mgr = open_app(ids["mgr"])
    _orders(mgr)
    mgr.click(f'.btn-edit-order[data-id="{seeded["order_id"]}"]')
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    assert mgr.locator('.seg-item.active[data-pay="credit"]').count() == 1
    assert mgr.input_value("#due-date-input") == "2030-01-15"
    mgr.click("#btn-submit")
    mgr.wait_for_function("() => window.__tgAlerts.some(a => a.includes('отправлена') || a.startsWith('⚠️'))")
    o = _order(e2e, seeded["order_id"])
    assert (o["payment_type"], o["due_date"]) == ("credit", "2030-01-15")


# ─── Удаление черновика: «Отмена» и каскад ───────────────────────────────────


def test_delete_draft_cancel_keeps_it_and_confirm_removes_items(open_app, e2e):
    oid = _seed_draft(e2e, 1, 2)
    mgr = open_app(e2e.ids["mgr"])
    _orders(mgr)
    btn = f'.btn-delete-draft[data-id="{oid}"]'
    mgr.wait_for_selector(btn)

    _confirm_answers_no(mgr)
    mgr.click(btn)
    _wait_alert(mgr, "confirm-no:Удалить черновик")
    assert mgr.locator(btn).is_enabled()
    assert e2e.rows("SELECT COUNT(*) AS n FROM orders WHERE id = ?", (oid,))[0]["n"] == 1

    _confirm_answers_yes(mgr)
    mgr.click(btn)
    mgr.wait_for_function("(id) => !document.querySelector(`.order-card[data-id=\"${id}\"]`)", arg=oid)
    assert e2e.rows("SELECT COUNT(*) AS n FROM orders WHERE id = ?", (oid,))[0]["n"] == 0
    assert e2e.rows("SELECT COUNT(*) AS n FROM order_items WHERE order_id = ?", (oid,))[0]["n"] == 0


# ─── Фильтры списка: статус, период, календарь ───────────────────────────────


def test_boss_status_and_period_filters(open_app, e2e):
    """Фильтры считаются на фронте по данным сервера — проверяем, что каждая
    кнопка оставляет ровно те заказы, что должна."""
    from services.database import mark_order_shipped
    from services.order_workflow import reject_shipment_request

    ids = e2e.ids
    pending = seed_order(e2e, payment_type="paid", due_date=None, approve=False, qty=1)["order_id"]
    approved = seed_order(e2e, qty=1)["order_id"]
    shipped = seed_order(e2e, qty=1)["order_id"]
    assert e2e.run(mark_order_shipped(shipped, ids["keeper"], "Keeper"))["ok"]
    rej = seed_order(e2e, payment_type="paid", due_date=None, approve=False, qty=1)
    assert e2e.run(reject_shipment_request(rej["req_id"], ids["boss"], "Boss", e2e.bot))["ok"]
    rejected = rej["order_id"]
    known = {pending, approved, shipped, rejected}

    boss = open_app(ids["boss"])
    # Даты — от «сегодня» БРАУЗЕРА: фильтр периода считает по его часам.
    today = _browser_today(boss)
    stamps = {
        pending: today, rejected: today,
        approved: today - timedelta(days=10),
        shipped: date(2020, 3, 1),
    }
    for oid, d in stamps.items():
        e2e.exec("UPDATE orders SET created_at = ? WHERE id = ?", (f"{d.isoformat()} 10:00:00", oid))

    _orders(boss)
    assert _card_ids(boss) & known == known
    labels = boss.eval_on_selector_all(".order-date-label", "els => els.map(e => e.textContent.trim())")
    assert "Сегодня" in labels and "01.03.2020" in labels, labels

    for key, expected in (("pending", {pending}), ("approved", {approved}),
                          ("shipped", {shipped}), ("rejected", {rejected})):
        _filter(boss, key)
        assert _card_ids(boss) & known == expected, key
        assert all(s == key for s in boss.eval_on_selector_all(
            ".order-card", "els => els.map(e => e.dataset.status)")), key
    _filter(boss, "all")

    for key, expected in (("today", {pending, rejected}), ("7d", {pending, rejected}),
                          ("30d", {pending, rejected, approved}), ("all", known)):
        _period(boss, key)
        assert _card_ids(boss) & known == expected, key

    # «Период…»: календарь, один день — сегодня.
    _period(boss, "custom")
    boss.wait_for_selector(".cal .cal-apply[disabled]")
    month = boss.locator(".cal-title").inner_text()
    boss.click('.cal-nav[data-nav="1"]')
    boss.wait_for_function("(m) => document.querySelector('.cal-title').textContent !== m", arg=month)
    boss.click('.cal-nav[data-nav="-1"]')
    boss.wait_for_function("(m) => document.querySelector('.cal-title').textContent === m", arg=month)
    day = f'.cal-day[data-day="{today.isoformat()}"]'
    boss.click(day)
    boss.wait_for_selector(".cal-range:has-text('—')")
    boss.click(day)
    boss.wait_for_selector(".cal-apply:not([disabled])")
    boss.click(".cal-apply")
    short = today.strftime("%d.%m")
    boss.wait_for_selector(f".seg-item--custom.active:has-text('{short}—{short}')")
    settled(boss)
    assert _card_ids(boss) & known == {pending, rejected}

    # Сочетание, где пусто, — объяснение, а не голый экран.
    _filter(boss, "shipped")
    assert _card_ids(boss) == set()
    assert "Нет заказов по выбранным фильтрам" in boss.locator(".empty-state").inner_text()


def test_manager_drafts_filter_shows_returned_draft_with_comment(open_app, e2e):
    from services.order_workflow import return_order_to_draft

    ids = e2e.ids
    returned = seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    assert e2e.run(return_order_to_draft(returned["req_id"], ids["boss"], "Boss", "Не та цена", e2e.bot))["ok"]
    approved = seed_order(e2e)["order_id"]

    mgr = open_app(ids["mgr"])
    _orders(mgr)
    _filter(mgr, "draft")
    assert _card_ids(mgr) == {returned["order_id"]}
    card = mgr.locator(f'.order-card[data-id="{returned["order_id"]}"]')
    assert "Не та цена" in card.inner_text(), "причина доработки видна на карточке"
    assert card.locator(".btn-edit-order").count() == 1 and card.locator(".btn-delete-draft").count() == 1
    _filter(mgr, "approved")
    assert _card_ids(mgr) == {approved}
    # Отменить одобренный менеджер не может. Отгрузить — пока может: замещает
    # кладовщика (ROLE_ALSO_ACTS_AS).
    assert mgr.locator(".btn-cancel-order").count() == 0
    assert mgr.locator(f'.btn-ship-order[data-id="{approved}"]').count() == 1


def test_order_cards_show_payment_state(open_app, e2e):
    """Тип оплаты, срок, «На подтверждении» и «Оплачен» — прямо на карточке."""
    from services.database import confirm_all_pending_payments_for_order, mark_order_paid

    ids = e2e.ids
    credit = seed_order(e2e)["order_id"]
    paid = seed_order(e2e, payment_type="paid", due_date=None)["order_id"]
    e2e.run(confirm_all_pending_payments_for_order(paid, ids["boss"], "Boss"))
    marked = seed_order(e2e, qty=1)["order_id"]
    ok, _pid = e2e.run(mark_order_paid(marked, ids["mgr"], "Manager", amount=50))
    assert ok

    boss = open_app(ids["boss"])
    _orders(boss)
    text = {oid: boss.locator(f'.order-card[data-id="{oid}"]').inner_text() for oid in (credit, paid, marked)}
    assert "В долг до 15.01.2030" in text[credit] and "Оплачен" not in text[credit]
    assert "Оплата сразу" in text[paid] and "Оплачен" in text[paid]
    assert "В долг" in text[marked] and "На подтверждении" in text[marked]
    # Позиции и суммы — с сервера, у руководства ещё и имя менеджера.
    assert "Кабель ВВГ 3x2.5" in text[credit] and "Manager" in text[credit]
    assert _digits(boss.locator(f'.order-card[data-id="{credit}"] .order-total').inner_text()) == "200"


# ─── Заявки: кредит-контекст, «Отмена» в диалогах, заморозка ─────────────────


def test_boss_request_credit_context_and_cancel_dialogs(open_app, e2e):
    """Карточка заявки показывает долг/лимит; «Отмена» в подтверждении отказа
    и в подтверждении превышения лимита оставляет заявку ждать."""
    from services.database import set_credit_limit

    ids = e2e.ids
    cp = _cp(e2e)
    e2e.run(set_credit_limit(str(cp), "ООО Ромашка", 1000.0, set_by=ids["boss"]))
    first = seed_order(e2e, approve=False)  # 200 в пределах 1000

    boss = open_app(ids["boss"])
    _orders(boss)
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-approve")
    ctx = boss.locator(".credit-ctx")
    assert "credit-ctx--ok" in ctx.get_attribute("class") and "в пределах" in ctx.inner_text()
    assert "1000" in _digits(ctx.inner_text())

    _confirm_answers_no(boss)
    boss.click(".btn-reject")
    _wait_alert(boss, "confirm-no:Отклонить заявку")
    assert e2e.rows("SELECT status FROM shipment_requests WHERE id = ?", (first["req_id"],))[0]["status"] == "pending"
    assert _order(e2e, first["order_id"])["status"] == "pending"
    _confirm_answers_yes(boss)

    boss.click(".btn-approve")
    _wait_alert(boss, "Заявка одобрена")
    boss.wait_for_selector(".empty-state-title:has-text('Решений не ждёт')")
    o = _order(e2e, first["order_id"])
    assert o["status"] == "approved" and not o["credit_limit_override"]
    assert _stock(e2e) == 18

    # Вторая заявка превышает лимит: 200 уже в долгу + 1000 > 1000.
    second = seed_order(e2e, approve=False, qty=10)
    boss2 = open_app(ids["boss"])
    _orders(boss2)
    boss2.click("#show-requests")
    boss2.wait_for_selector(".credit-ctx--bad:has-text('превышение')")
    _confirm_answers_no(boss2)
    boss2.click(".btn-approve")
    _wait_alert(boss2, "confirm-no:Кредитный лимит превышен")
    boss2.wait_for_selector(".btn-approve:not([disabled])")
    assert e2e.rows("SELECT status FROM shipment_requests WHERE id = ?", (second["req_id"],))[0]["status"] == "pending"
    assert _order(e2e, second["order_id"])["status"] == "pending"
    assert _stock(e2e) == 18, "склад не тронут"
    assert not any("одобрена" in a for a in boss2.evaluate("window.__tgAlerts"))


def test_return_to_draft_freezes_order_until_admin_unfreezes(open_app, e2e):
    """После `reject_max_cycles` доработок заказ замораживается: менеджер
    видит «Заморожен», переотправка отказывает, пока админ не разморозит."""
    ids = e2e.ids
    e2e.db.set_setting("reject_max_cycles", 1, ids["boss"])
    seeded = seed_order(e2e, payment_type="paid", due_date=None, approve=False)
    oid = seeded["order_id"]

    boss = open_app(ids["boss"])
    _orders(boss)
    boss.click("#show-requests")
    boss.wait_for_selector(".btn-draft")
    boss.click(".btn-draft")
    boss.wait_for_selector(".draft-box:not([hidden])")
    boss.fill(".draft-box .draft-comment", "ок")
    boss.click(".draft-send")
    _wait_alert(boss, "минимум 3 символа")
    assert _order(e2e, oid)["status"] == "pending"

    boss.fill(".draft-box .draft-comment", "Цена ниже прайса")
    boss.click(".draft-send")
    _wait_alert(boss, "возвращена на доработку")
    boss.wait_for_selector(".empty-state-title:has-text('Решений не ждёт')")
    o = _order(e2e, oid)
    assert (o["status"], o["frozen"], o["rejection_count"]) == ("draft", 1, 1)
    assert e2e.rows("SELECT status FROM shipment_requests WHERE id = ?", (seeded["req_id"],))[0]["status"] == "returned"

    mgr = open_app(ids["mgr"])
    _orders(mgr)
    card = mgr.locator(f'.order-card[data-id="{oid}"]')
    card.wait_for()
    assert "Заморожен" in card.inner_text() and "Цена ниже прайса" in card.inner_text()
    mgr.click(f'.btn-edit-order[data-id="{oid}"]')
    mgr.wait_for_selector("#btn-submit:not([disabled])")
    mgr.click("#btn-submit")
    _wait_alert(mgr, "заморожен")
    assert e2e.rows("SELECT COUNT(*) AS n FROM shipment_requests WHERE status = 'pending'")[0]["n"] == 0
    assert _order(e2e, oid)["status"] == "draft"

    # Кнопки разморозки в WebApp нет — ручка есть; ею и размораживаем.
    admin = open_app(ids["admin"])
    assert _api(admin, "/api/orders/unfreeze", {"order_id": oid})["status"] == 200
    o = _order(e2e, oid)
    assert (o["frozen"], o["rejection_count"]) == (0, 0)

    mgr.wait_for_selector("#btn-submit:not([disabled])")
    mgr.click("#btn-submit")
    _wait_alert(mgr, "отправлена")
    assert _order(e2e, oid)["status"] == "pending"
    assert e2e.rows("SELECT COUNT(*) AS n FROM shipment_requests WHERE status = 'pending'")[0]["n"] == 1


# ─── Отмена заказа и отгрузка ────────────────────────────────────────────────


def test_boss_cancels_approved_order_and_stock_returns(open_app, e2e):
    """«Отменить заказ»: причина обязательна; отмена откатывает расходную
    накладную — остаток возвращается — и уведомляет менеджера с причиной."""
    ids = e2e.ids
    seeded = seed_order(e2e)
    oid = seeded["order_id"]
    assert _stock(e2e) == 18
    invoice_id = e2e.rows("SELECT invoice_id FROM order_shipment WHERE order_id = ?", (oid,))[0]["invoice_id"]

    boss = open_app(ids["boss"])
    _orders(boss)
    box = f'.cancel-box[data-id="{oid}"]'
    boss.wait_for_selector(box, state="attached")
    assert boss.locator(box).is_hidden()
    boss.click(f'.btn-cancel-order[data-id="{oid}"]')
    boss.wait_for_selector(f"{box}:not([hidden])")

    boss.click(f"{box} .cancel-send")
    _wait_alert(boss, "Укажите причину")
    assert _order(e2e, oid)["status"] == "approved"

    boss.fill(f"{box} .cancel-reason", "Клиент передумал")
    boss.click(f"{box} .cancel-send")
    _wait_alert(boss, f"Заказ #{oid} отменён")
    boss.wait_for_selector(f'.order-card[data-id="{oid}"][data-status="cancelled"]')

    o = _order(e2e, oid)
    assert (o["status"], o["cancellation_reason"], o["cancelled_by"]) == ("cancelled", "Клиент передумал", ids["boss"])
    assert _stock(e2e) == 20, "товар вернулся на склад"
    assert e2e.rows("SELECT status FROM invoices WHERE id = ?", (invoice_id,))[0]["status"] == "cancelled"
    msgs = [m for m in e2e.bot.messages if m["chat_id"] == ids["mgr"] and "отменён" in m["text"]]
    assert msgs and "Клиент передумал" in msgs[-1]["text"]
    card = boss.locator(f'.order-card[data-id="{oid}"]')
    assert card.locator(".btn-cancel-order, .btn-ship-order").count() == 0


def test_cancelled_order_is_listed_under_cancelled_filter(open_app, e2e):
    """«Отменены» — оба исхода «продажа не состоялась»: отклонённая заявка
    (rejected) и отменённый после одобрения заказ (cancelled). Фильтр искал
    только rejected, и отменённый заказ находился лишь во «Всех». Какой из
    двух исходов — говорит бейдж на карточке."""
    from services.order_workflow import cancel_order_full, reject_shipment_request

    ids = e2e.ids
    oid = seed_order(e2e)["order_id"]
    assert e2e.run(cancel_order_full(oid, ids["boss"], "Boss", "Клиент передумал"))["ok"]
    rej = seed_order(e2e, payment_type="paid", due_date=None, approve=False, qty=1)
    assert e2e.run(reject_shipment_request(rej["req_id"], ids["boss"], "Boss", e2e.bot))["ok"]
    live = seed_order(e2e, qty=1)["order_id"]

    for role in ("boss", "mgr"):
        page = open_app(ids[role])
        _orders(page)
        label = page.locator('.seg-item[data-filter="rejected"]').inner_text()
        assert "Отменены" in label, role
        _filter(page, "rejected")
        assert _card_ids(page) == {oid, rej["order_id"]}, role
        assert live not in _card_ids(page), role
        badges = {page.locator(f'.order-card[data-id="{i}"] .order-status').text_content().strip()
                  for i in (oid, rej["order_id"])}
        assert badges == {"Отменён", "Отклонено"}, (role, badges)


def test_boss_ships_order_notifies_manager_and_cancel_is_closed(open_app, e2e):
    """Отгрузка руководителем: статус, уведомление создателю, кнопки уходят,
    заказ в «Отгружены»; отменить отгруженный нельзя (только через возврат)."""
    ids = e2e.ids
    oid = seed_order(e2e)["order_id"]
    boss = open_app(ids["boss"])
    _orders(boss)

    _confirm_answers_no(boss)
    boss.click(f'.btn-ship-order[data-id="{oid}"]')
    _wait_alert(boss, f"confirm-no:Отметить заказ #{oid}")
    assert _order(e2e, oid)["status"] == "approved"
    _confirm_answers_yes(boss)

    boss.click(f'.btn-ship-order[data-id="{oid}"]')
    _wait_alert(boss, f"🚚 Заказ #{oid} отгружен")
    boss.wait_for_selector(f'.order-card[data-id="{oid}"][data-status="shipped"]')
    o = _order(e2e, oid)
    assert (o["status"], o["shipped_by"]) == ("shipped", ids["boss"]) and o["shipped_at"]
    assert any(m["chat_id"] == ids["mgr"] and m["text"] == f"🚚 Ваш заказ #{oid} отгружен." for m in e2e.bot.messages)
    card = boss.locator(f'.order-card[data-id="{oid}"]')
    assert card.locator(".btn-ship-order, .btn-cancel-order").count() == 0
    _filter(boss, "shipped")
    assert oid in _card_ids(boss)

    res = _api(boss, "/api/orders/cancel", {"order_id": oid, "reason": "Поздно"})
    assert res["status"] == 409 and "возврат" in res["body"]["detail"]
    assert _order(e2e, oid)["status"] == "shipped"
    assert _stock(e2e) == 18


def test_keeper_sees_only_approved_and_ship_race_reports_error(open_app, e2e):
    """Кладовщик видит одобренные (и отгруженные), без «Отменить». Если заказ
    отгрузили, пока экран был открыт, — понятная ошибка, а не «отгружен»."""
    from services.database import mark_order_shipped

    ids = e2e.ids
    pending = seed_order(e2e, payment_type="paid", due_date=None, approve=False)["order_id"]
    oid = seed_order(e2e)["order_id"]
    draft = _seed_draft(e2e, 1)

    keeper = open_app(ids["keeper"])
    _orders(keeper)
    keeper.wait_for_selector(f'.btn-ship-order[data-id="{oid}"]')
    assert _card_ids(keeper) == {oid}, "черновики и ожидающие — не его работа"
    assert pending not in _card_ids(keeper) and draft not in _card_ids(keeper)
    assert keeper.locator(".btn-cancel-order").count() == 0

    assert e2e.run(mark_order_shipped(oid, ids["boss"], "Boss"))["ok"]
    keeper.click(f'.btn-ship-order[data-id="{oid}"]')
    _wait_alert(keeper, "❌ Отгрузить можно только одобренный")
    assert not any(a.startswith("🚚") for a in keeper.evaluate("window.__tgAlerts"))
    keeper.wait_for_selector(f'.btn-ship-order[data-id="{oid}"]:not([disabled])')
    assert _order(e2e, oid)["shipped_by"] == ids["boss"]


# ─── Отчёт ───────────────────────────────────────────────────────────────────


def _report(page) -> None:
    _orders(page)
    tab(page, "report")
    page.wait_for_selector("[data-period].active")
    settled(page)


def test_boss_report_periods_calendar_and_excel_export(open_app, e2e):
    """Отчёт руководства: цифры с живого сервера, каждый пресет периода уходит
    своим запросом, «Период…» шлёт since/until (по — включительно), выгрузка
    Excel приходит файлом в чат."""
    from services.database import mark_order_shipped

    ids = e2e.ids
    oid = seed_order(e2e)["order_id"]  # 200 USD в долг «Ромашке», менеджер Manager
    # В разрезе по менеджерам выручка — отгруженные заказы (статус shipped).
    assert e2e.run(mark_order_shipped(oid, ids["keeper"], "Keeper"))["ok"]

    boss = open_app(ids["boss"])
    _report(boss)
    boss.wait_for_selector(".stat-grid .stat")
    assert "200" in _digits(boss.locator(".stat-grid .stat").first.inner_text())
    text = boss.locator("#content").inner_text()
    assert "Кабель ВВГ 3x2.5" in text and "Ромашка" in text and "Manager" in text
    assert "топ менеджеров" in text.lower() and "топ клиентов" in text.lower()
    # Разрез по менеджерам: у Manager — выручка, отгрузка, заказ и открытый долг.
    row = boss.locator(".top-row", has_text="Manager").inner_text()
    assert "200 USD" in row.replace("\xa0", " ") and "1 отгрузка" in row and "1 заказ" in row, row
    assert "долг 200 USD" in row.replace("\xa0", " "), row

    def analytics_request(r):
        return r.url.endswith("/api/analytics") and r.method == "POST"

    for key in ("week", "3month", "year"):
        with boss.expect_request(analytics_request) as req:
            boss.click(f'[data-period="{key}"]')
        assert req.value.post_data_json["period"] == key
        boss.wait_for_selector(f'[data-period="{key}"].active')
        boss.wait_for_selector(".stat-grid .stat")

    today = _browser_today(boss)
    boss.click('[data-period="custom"]')
    boss.wait_for_selector(".loader:has-text('Выберите даты')")
    day = f'.cal-day[data-day="{today.isoformat()}"]'
    boss.click(day)
    boss.click(day)
    with boss.expect_request(analytics_request) as req:
        boss.click(".cal-apply")
    body = req.value.post_data_json
    assert (body["since"], body["until"]) == (today.isoformat(), (today + timedelta(days=1)).isoformat())
    boss.wait_for_selector("#analytics-export")
    assert "Кабель ВВГ 3x2.5" in boss.locator("#content").inner_text(), "сегодняшняя отгрузка в диапазоне"

    docs_before = len(e2e.bot.documents)
    with boss.expect_request(lambda r: r.url.endswith("/api/analytics/export")) as req:
        boss.click("#analytics-export")
    assert req.value.post_data_json["since"] == today.isoformat()
    _wait_alert(boss, "Excel-файл отправлен")
    boss.wait_for_selector("#analytics-export:has-text('Отправлено в чат')")
    assert boss.locator("#analytics-export").is_disabled()
    sent = e2e.bot.documents[docs_before:]
    assert len(sent) == 1 and sent[0]["chat_id"] == ids["boss"]
    assert sent[0]["caption"].startswith("📊 Аналитика")


def test_manager_report_is_personal_without_export(open_app, e2e):
    ids = e2e.ids
    seed_order(e2e)
    mgr = open_app(ids["mgr"])
    _report(mgr)
    mgr.wait_for_selector(".rev-row")
    rev = mgr.locator(".rev-row").first.inner_text()
    assert "200" in _digits(rev) and "USD" in rev
    assert mgr.locator(".stat-grid .stat").count() == 2, "у менеджера — отгрузки и клиенты"
    assert mgr.locator("#analytics-export").count() == 0
    text = mgr.locator("#content").inner_text().lower()
    assert "топ менеджеров" not in text

    with mgr.expect_request(lambda r: r.url.endswith("/api/analytics")) as req:
        mgr.click('[data-period="week"]')
    assert req.value.post_data_json["period"] == "week"
    mgr.wait_for_selector(".rev-row")


def test_manager_report_keeps_sale_after_it_is_paid(open_app, e2e):
    """Клиент рассчитался, сдача подтверждена — заказ стал `paid`. Продажа от
    этого не перестала быть продажей, но `_personal_analytics` фильтровал
    `status in ("approved", "shipped")`, и выручка менеджера за период
    обнулялась (в отчёте руководства та же продажа оставалась)."""
    from services.database import confirm_cash_deposit, create_cash_deposit, mark_order_shipped

    ids = e2e.ids
    oid = seed_order(e2e, qty=1, price=80.0)["order_id"]
    assert e2e.run(mark_order_shipped(oid, ids["keeper"], "Keeper"))["ok"]
    dep = e2e.run(create_cash_deposit(ids["mgr"], 80.0))
    assert e2e.run(confirm_cash_deposit(dep["deposit_id"], ids["boss"], "Boss"))["ok"]
    assert _order(e2e, oid)["status"] == "paid"

    mgr = open_app(ids["mgr"])
    _report(mgr)
    assert mgr.locator(".rev-row").count() == 1, mgr.locator("#content").inner_text()
    assert "80" in _digits(mgr.locator(".rev-row").first.inner_text())


# ─── Документы ───────────────────────────────────────────────────────────────


def _docs(page) -> None:
    _orders(page)
    tab(page, "docs")
    page.wait_for_selector("#doc-new")
    settled(page)


def test_boss_sets_company_requisites_then_creates_ru_uz_receipt(open_app, e2e, monkeypatch, tmp_path):
    """«Реквизиты» сохраняются в настройки и подставляются в расписку RU+UZ:
    кредитор, город (и его узбекское название), подписант в родительном падеже."""
    contexts = _fake_documents(monkeypatch, tmp_path)
    ids = e2e.ids
    boss = open_app(ids["boss"])
    _docs(boss)
    assert boss.locator("#doc-company").count() == 1
    assert "Документов пока нет" in boss.locator("#content").inner_text()

    boss.click("#doc-company")
    boss.wait_for_selector("#ms-f-company_name")
    for key, value in _FULL_COMPANY.items():
        boss.fill(f"#ms-f-{key}", value)
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('Реквизиты сохранены')")
    boss.wait_for_selector("#doc-company-line:has-text('ООО Тест · Самарканд')")
    assert boss.locator(".c-overlay").count() == 0
    for key, value in _FULL_COMPANY.items():
        assert e2e.db.get_setting(key, "") == value, key
    assert e2e.rows("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'company_requisites'")[0]["n"] == 1

    boss.click("#doc-new")
    boss.wait_for_selector("#ms-f-doc_type", state="attached")
    assert boss.input_value("#ms-f-city") == "Самарканд", "город подставлен из реквизитов"
    boss.fill("#ms-f-debtor_full_name", "Каримов Алишер")
    boss.fill("#ms-f-product_name", "Погрузчик XCMG")
    boss.fill("#ms-f-total_amount", "360000000")
    boss.fill("#ms-f-term_months", "12")
    boss.fill("#ms-f-installments_count", "12")
    boss.click("#ms-submit")
    boss.wait_for_selector(".toast:has-text('сформирован и отправлен')")
    boss.wait_for_selector("[data-doc-send]")

    row = e2e.rows(
        "SELECT g.client_name, g.total_amount_cents, g.currency, g.payment_type, g.installments_count, "
        "g.created_by, t.type FROM generated_documents g JOIN document_templates t ON t.id = g.template_id"
    )
    assert row == [{"client_name": "Каримов Алишер", "total_amount_cents": 36_000_000_000, "currency": "UZS",
                    "payment_type": "installment", "installments_count": 12,
                    "created_by": ids["boss"], "type": "raspiska_ru_uz"}]
    ctx = contexts[-1]
    assert (ctx["creditor_name"], ctx["city"], ctx["city_uz"]) == ("ООО Тест", "Самарканд", "Самарқанд")
    assert ctx["creditor_representative_gen"] == "директора Петрова Петра"
    assert len(ctx["schedule"]) == 12 and ctx["schedule"][-1]["balance"] == "0"
    assert (ctx["total_amount"], ctx["schedule"][0]["amount"]) == ("360 000 000", "30 000 000")
    assert [d["chat_id"] for d in e2e.bot.documents] == [ids["boss"]]
    assert "Расписка RU+UZ · Каримов Алишер" in boss.locator('[data-doc] .card-row-title').first.inner_text()

    # Админ — тоже руководство: реквизиты правит.
    admin = open_app(ids["admin"])
    _docs(admin)
    assert admin.locator("#doc-company").count() == 1


def test_manager_creates_tilxat_single_payment(open_app, e2e, monkeypatch, tmp_path):
    """Тилхат (ўзб.) — тот же бланк, узбекская часть: те же поля формы, разовый
    платёж (одна строка графика) и сумма в сумах доезжают до документа и списка.
    Поля прежней формы в запросе сервер пропускает мимо, а не отказывает."""
    contexts = _fake_documents(monkeypatch, tmp_path)
    ids = e2e.ids
    for key, value in _FULL_COMPANY.items():
        e2e.db.set_setting(key, value, ids["boss"])
    mgr = open_app(ids["mgr"])
    _docs(mgr)
    mgr.click("#doc-new")
    mgr.wait_for_selector("#ms-f-doc_type", state="attached")
    assert mgr.locator('.c-overlay [data-opt]').evaluate_all("els => els.map(e => e.dataset.opt)") == [
        "raspiska_ru_uz", "raspiska_ru", "tilxat_uz"]

    mgr.click('[data-opt="tilxat_uz"]')
    mgr.wait_for_function("() => document.querySelector('#ms-f-doc_type').value === 'tilxat_uz'")
    assert mgr.locator("#ms-f-debtor_passport, #ms-f-currency, #ms-f-witness_name").count() == 0
    sheet = {
        "debtor_full_name": "Тошматов Бахтиёр", "product_name": "Бетономешалка",
        "total_amount": "15000000", "term_months": "3", "installments_count": "1", "city": "Ташкент",
    }
    for key, value in sheet.items():
        mgr.fill(f"#ms-f-{key}", value)
    mgr.click("#ms-submit")
    mgr.wait_for_selector(".toast:has-text('сформирован')")
    mgr.wait_for_selector("[data-doc-send]")

    row = e2e.rows(
        "SELECT g.client_name, g.passport_data, g.total_amount_cents, g.currency, g.term_months, "
        "g.payment_type, g.installments_count, t.type FROM generated_documents g "
        "JOIN document_templates t ON t.id = g.template_id"
    )
    assert row == [{"client_name": "Тошматов Бахтиёр", "passport_data": "",
                    "total_amount_cents": 1_500_000_000, "currency": "UZS", "term_months": 3,
                    "payment_type": "single", "installments_count": None, "type": "tilxat_uz"}]
    ctx = contexts[-1]
    assert len(ctx["schedule"]) == 1 and ctx["schedule"][0]["amount"] == "15 000 000"
    assert ctx["total_amount_words_uz"] == "ўн беш миллион" and ctx["city"] == "Ташкент"

    item = mgr.locator("[data-doc]").first.inner_text()
    assert "Тилхат (ўзб.)" in item and "разовый платёж" in item and "UZS" in item
    assert e2e.bot.documents and e2e.bot.documents[-1]["chat_id"] == ids["mgr"]

    # Старый клиент прислал поля прежней формы — документ всё равно создан.
    res = _api(mgr, "/api/docs/create", {
        "doc_type": "raspiska_ru", **sheet, "debtor_passport": "AA1234567", "currency": "USD",
        "penalty_rate": "0.2", "grace_days": "5", "witness_name": "Каримов Рустам", "payment_type": "installment",
    })
    assert res["body"]["ok"] is True, res
    assert "penalty_rate" not in contexts[-1] and "witness_name" not in contexts[-1]


def test_document_form_validation_and_cancel(open_app, e2e, monkeypatch, tmp_path):
    """Обязательные поля проверяет форма, недостающие реквизиты — сервер;
    обе ошибки остаются в форме. «Отмена» закрывает форму без документа."""
    _fake_documents(monkeypatch, tmp_path)
    mgr = open_app(e2e.ids["mgr"])
    _docs(mgr)

    mgr.click("#doc-new")
    mgr.wait_for_selector("#ms-submit")
    mgr.click("#ms-cancel")
    mgr.wait_for_selector(".c-overlay", state="detached")

    mgr.click("#doc-new")
    mgr.wait_for_selector("#ms-f-doc_type", state="attached")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:not([hidden])")
    assert "Заполните: Должник — ФИО" in mgr.locator("#ms-error").inner_text()

    # Без ИНН/адреса/должности в реквизитах — сервер называет, чего нет.
    mgr.fill("#ms-f-debtor_full_name", "Иванов Иван")
    mgr.fill("#ms-f-product_name", "Ковш")
    mgr.fill("#ms-f-total_amount", "1000")
    mgr.fill("#ms-f-term_months", "2")
    mgr.fill("#ms-f-city", "Ташкент")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('Реквизитах компании')")
    err = mgr.locator("#ms-error").inner_text()
    assert "ИНН" in err and "должность подписанта" in err
    assert mgr.locator("#ms-f-debtor_full_name").input_value() == "Иванов Иван"

    # Сумма ноль — отказ сервера, форма та же.
    mgr.click('[data-opt="raspiska_ru"]')
    mgr.fill("#ms-f-total_amount", "0")
    mgr.click("#ms-submit")
    mgr.wait_for_selector("#ms-error:has-text('больше нуля')")

    mgr.click("#ms-cancel")
    mgr.wait_for_selector(".c-overlay", state="detached")
    assert e2e.rows("SELECT COUNT(*) AS n FROM generated_documents")[0]["n"] == 0
    assert e2e.bot.documents == []


def test_document_list_resend_print_error_and_missing_file(open_app, e2e, monkeypatch, tmp_path):
    """Кнопки строки документа: «В Telegram» шлёт сохранённый PDF заново тому,
    кто нажал; ошибка принтера — тостом с причиной; без файла — ни отправки,
    ни печати, а подсказка «сформируйте заново»."""
    from services import documents, printing
    from services.printing import PrintResult

    _fake_documents(monkeypatch, tmp_path)
    for key, value in _FULL_COMPANY.items():
        e2e.db.set_setting(key, value, e2e.ids["boss"])
    base = {"doc_type": "raspiska_ru", "product_name": "Кран", "total_amount": "5000",
            "term_months": "5", "installments_count": "5", "city": "Самарканд"}
    # Менеджер видит только свои документы (в расписке паспорт должника),
    # поэтому составитель — он; чужой документ руководства в списке не появится.
    kept = e2e.run(documents.create_document({**base, "debtor_full_name": "Живой Файл"}, created_by=e2e.ids["mgr"]))
    lost = e2e.run(documents.create_document({**base, "debtor_full_name": "Пропал Файл"}, created_by=e2e.ids["mgr"]))
    foreign = e2e.run(documents.create_document({**base, "debtor_full_name": "Чужой Должник"}, created_by=e2e.ids["boss"]))
    assert kept["ok"] and lost["ok"] and foreign["ok"]
    Path(lost["file"]).unlink()

    printed: list[str] = []

    async def failing_print(pdf_bytes, *, filename="", printer_name="", label=""):
        printed.append(label)
        return PrintResult(False, error="Принтер Canon не отвечает")

    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", failing_print)

    mgr = open_app(e2e.ids["mgr"])
    _docs(mgr)
    live = mgr.locator(f'[data-doc="{kept["id"]}"]')
    gone = mgr.locator(f'[data-doc="{lost["id"]}"]')
    assert gone.get_attribute("data-status") == "rejected"
    assert "файл не найден" in gone.inner_text()
    assert gone.locator("[data-doc-send], [data-doc-print]").count() == 0
    assert mgr.locator(f'[data-doc="{foreign["id"]}"]').count() == 0

    mgr.click(f'[data-doc-send="{kept["id"]}"]')
    mgr.wait_for_selector(".toast:has-text('Документ отправлен вам в Telegram')")
    assert len(e2e.bot.documents) == 1
    sent = e2e.bot.documents[0]
    assert sent["chat_id"] == e2e.ids["mgr"] and "Живой Файл" in sent["caption"]
    assert "prn:doc:" in str(sent["reply_markup"].inline_keyboard[0][0].callback_data)

    mgr.click(f'[data-doc-print="{kept["id"]}"]')
    mgr.wait_for_selector(".toast--error:has-text('Принтер Canon не отвечает')")
    assert printed and "Живой Файл" in printed[0]
    assert live.locator("[data-doc-print]").is_enabled()

    # Отправка файла, которого нет (ссылка из старого экрана), — отказ с причиной.
    res = _api(mgr, "/api/docs/send", {"doc_id": lost["id"]})
    assert res["body"] == {"ok": False, "error": "Файл документа не найден — сформируйте заново"}
    assert len(e2e.bot.documents) == 1

    # Без CUPS кнопки печати нет вовсе.
    monkeypatch.setattr(printing, "is_available", lambda: False)
    mgr2 = open_app(e2e.ids["mgr"])
    _docs(mgr2)
    mgr2.wait_for_selector(f'[data-doc-send="{kept["id"]}"]')
    assert mgr2.locator("[data-doc-print]").count() == 0

