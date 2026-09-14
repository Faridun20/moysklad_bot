"""E2E-обходчик: под каждой ролью открыть КАЖДЫЙ экран и нажать КАЖДУЮ кнопку.

Страховочная сетка, а не сценарий. Сценарные тесты проверяют, что нужное
действие даёт нужное последствие; этот — что ни одно видимое действие не
роняет приложение. Падением считается:

* исключение в странице (`pageerror`: неперехваченная ошибка или отклонённый
  промис);
* `console.error` (кроме «Failed to load resource» по ответам < 500 — отказ
  бизнес-правила вроде 400/403/409 приложение показывает человеку, это не сбой);
* ответ сервера со статусом >= 500;
* экран без содержимого или с `.error-card` («Не удалось загрузить»).

Как устроен обход. Разделы — из нижней панели, вкладки — из переключателя
раздела; на каждом экране собираются видимые кликабельные элементы `#content`
и нажимаются по одному (дедупликация по тексту без цифр + id + data-атрибутам,
не больше MAX_CLICKS_PER_SCREEN). После нажатия: открылась форма — закрываем
её «Отменой» или Escape, НЕ отправляя; ушли на другой экран или карточку —
открываем экран заново. Деструктивные кнопки жмутся честно: данные свежие на
каждый тест, заглушка `showConfirm` отвечает «да», а проверяем мы отсутствие
падений, а не сохранность данных.

Найденный настоящий баг не глушится молча: для него есть отдельный тест-репро
с `xfail(strict=True)`, а в KNOWN_BUGS — точечное исключение с тем же текстом,
чтобы остальной обход продолжал ловить новое.

Сводка по роли (экраны, клики, отказы 4xx) печатается всегда; подробности по
экранам — с `E2E_CRAWL_VERBOSE=1`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from tests.e2e.conftest import go, seed_order, settled, tab

ROLES = ["boss", "admin", "mgr", "keeper", "book"]
MAX_CLICKS_PER_SCREEN = 40
MAX_PER_SHAPE = 3
MAX_DEPTH = 2
CLICK_TIMEOUT_MS = 5_000

# Что считаем «кнопкой». Вкладки раздела (`.seg-item[data-sect]`) обходятся
# отдельно, как экраны, — в списке кликов их нет.
CLICKABLE = "button, [role=button], .c-row--tap, .cat-btn, .seg-item"

# Известные баги: (роли, подстроки сообщения — все обязательны, текст BUG,
# тест-репро). Исключение точечное — по роли, кнопке и тексту ошибки; всё
# остальное ловится как было. Починили баг — репро станет XPASS(strict) и
# упадёт: тогда убрать и его, и строку отсюда.
KNOWN_BUGS: list[tuple[tuple[str, ...], tuple[str, ...], str, str]] = [
    # Пусто. Первый найденный баг («Новый заказ» у кладовщика и бухгалтера вёл в
    # «Нет доступа») починен — регресс держит test_new_order_button_for_role_without_orders.
]


# ─── Засев: чтобы кнопкам было на чём появиться ─────────────────────────────


def _seed_rich(e2e, tmp_path: Path) -> dict:
    """Заказы во всех статусах, долги, техника, контейнеры, лиды, документ, сдача.

    Всё через сервисы, как в сценарных тестах: браузер здесь только жмёт.
    """
    from services import channel, containers, lead_calls, leads, machines, warehouse
    from services.database import (
        confirm_all_pending_payments_for_order,
        create_cash_deposit,
        create_return,
        mark_order_shipped,
        set_credit_limit,
        set_currency_rate,
    )
    from services.order_workflow import reject_shipment_request, return_order_to_draft

    ids = e2e.ids
    out: dict = {}
    # Запас под все одобрения ниже: сид даёт 20 шт., заказы списывают больше.
    e2e.run(warehouse.create_invoice(
        invoice_type="incoming", warehouse_id=ids["warehouse"],
        items=[{"product_id": ids["product"], "quantity": 50, "price_cents": 900}],
    ))
    ok, err = set_currency_rate("UZS", 12500.0, ids["admin"])
    assert ok, err

    # Долг в срок, отгружен — «Долги», сдача наличных гасит его FIFO.
    debt = seed_order(e2e)
    assert e2e.run(mark_order_shipped(debt["order_id"], ids["keeper"], "Keeper")).get("ok")
    out["debt"] = debt["order_id"]
    # Просроченный долг, одобрен, но не отгружен — кладовщику есть что отгрузить.
    overdue = seed_order(e2e, qty=1, price=150.0)
    e2e.exec("UPDATE orders SET due_date = ? WHERE id = ?", ("2020-01-01", overdue["order_id"]))
    # «Оплачено сразу» — платёж ждёт подтверждения руководства.
    seed_order(e2e, payment_type="paid", due_date=None, qty=1, price=90.0)
    # Заявка ждёт решения.
    seed_order(e2e, payment_type="paid", due_date=None, approve=False, qty=1, price=70.0)
    # Отклонённая заявка и заявка, возвращённая на доработку.
    rej = seed_order(e2e, approve=False, qty=1, price=40.0)
    assert e2e.run(reject_shipment_request(rej["req_id"], ids["boss"], "Boss", e2e.bot)).get("ok")
    back = seed_order(e2e, approve=False, qty=1, price=50.0)
    r = e2e.run(return_order_to_draft(back["req_id"], ids["boss"], "Boss", "Уточните цену", e2e.bot))
    assert r.get("ok"), r
    # Пустой черновик.
    e2e.db.create_order(ids["mgr"], "Manager", "")
    # Возврат по оплаченному заказу: ждёт приёмки товара и подтверждения.
    ret = seed_order(e2e, payment_type="paid", due_date=None, qty=1, price=60.0)
    e2e.run(confirm_all_pending_payments_for_order(ret["order_id"], ids["boss"], "Boss"))
    item = e2e.rows("SELECT id FROM order_items WHERE order_id = ?", (ret["order_id"],))[0]["id"]
    r = e2e.run(create_return(ret["order_id"], "full", "Брак", [(item, 1, 60.0)],
                              "debt_reduction", ids["mgr"]))
    assert r.get("ok"), r
    # Сдача наличных — ждёт подтверждения.
    r = e2e.run(create_cash_deposit(ids["mgr"], 50.0))
    assert r.get("ok"), r
    # Кредитный лимит — после заказов, чтобы submit не просил превышения.
    cp = debt["counterparty_id"]
    e2e.run(set_credit_limit(str(cp), "ООО Ромашка", 5000.0, set_by=ids["boss"]))

    # Техника: на складе в рассрочке и в пути.
    m1 = e2e.run(machines.create_machine(
        vin="JCB3CX7788", name="JCB 3CX 2019", year=2019, hours=1500, price_cents=2_500_000,
        cost_cents=2_000_000, created_by=ids["boss"], status="in_stock",
    ))
    assert m1.get("ok"), m1
    d = e2e.run(machines.create_deal(
        m1["machine_id"], kind="credit", price_cents=2_400_000, buyer_name="Азиз Рахимов",
        buyer_phone="+998901112233", buyer_passport="AA1234567", created_by=ids["boss"],
        down_payment_cents=400_000, months=4,
    ))
    assert d.get("ok"), d
    m2 = e2e.run(machines.create_machine(
        vin="ZX200-1", name="Hitachi ZX200", price_cents=3_000_000, created_by=ids["boss"],
    ))
    assert m2.get("ok"), m2

    # Контейнеры: прибывший с расхождением и едущий.
    c1 = e2e.run(containers.create_container(number="MSKU1234567", created_by=ids["boss"]))
    assert c1.get("ok"), c1
    e2e.run(containers.add_item(c1["container_id"], name="Кабель ВВГ 3x2.5", expected_qty=10,
                                unit="м", product_id=ids["product"]))
    e2e.run(containers.mark_arrived(c1["container_id"], user_id=ids["boss"]))
    citem = e2e.rows("SELECT id FROM container_items WHERE container_id = ?", (c1["container_id"],))[0]["id"]
    e2e.run(containers.set_arrived_quantities(c1["container_id"], {citem: "7"}, user_id=ids["boss"]))
    c2 = e2e.run(containers.create_container(number="TGHU7654321", created_by=ids["boss"]))
    assert c2.get("ok"), c2
    e2e.run(containers.add_item(c2["container_id"], name="Автомат 16А", expected_qty=5, unit="шт"))

    # Лиды и звонок без лида.
    for n, name in enumerate(["Азиз Р.", "Бахтиёр"]):
        e2e.run(leads.record_message(
            tg_user_id=555_001 + n, manager_id=ids["mgr"], inbound=True,
            username=f"lead{n}", display_name=name,
        ))
    e2e.run(lead_calls.add_call(manager_id=ids["mgr"], phone="+998901112233",
                                display_name="Звонивший", interest="Кабель"))

    # Лид, которому не отвечают двое суток, — строка в «Воронке» и фильтрах.
    stale = (datetime.fromisoformat(e2e.db.now_str()) - timedelta(days=2)).isoformat(sep=" ")
    e2e.exec("UPDATE leads SET last_inbound_at = ?, first_seen_at = ? WHERE tg_user_id = ?",
             (stale[:len(e2e.db.now_str())], stale[:len(e2e.db.now_str())], 555_001))
    e2e.run(channel.save_post(kind="showcase", ref=str(ids["product"]), message_id=1, posted_by=ids["boss"]))

    # Документ с настоящим файлом: кнопки «В Telegram» и «Распечатать» есть,
    # только если PDF лежит на диске.
    pdf = tmp_path / "raspiska.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%e2e\n")
    e2e.exec(
        "INSERT INTO generated_documents (client_name, product_name, total_amount_cents, currency, "
        "start_date, term_months, payment_type, installments_count, file_path, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("Иванов Иван", "Экскаватор JCB", 2_500_000, "USD", "2030-01-01", 6, "installment", 6,
         str(pdf), ids["mgr"], e2e.db.now_str()),
    )
    return out


# ─── Обходчик ────────────────────────────────────────────────────────────────

# Счётчик запросов в полёте: fetch + чтение тела. Без него «после клика»
# пришлось бы ждать наугад. Ставится после загрузки — app.js берёт
# `fetch` из window в момент вызова, а не при старте.
_INFLIGHT_JS = """
() => {
  if (window.__e2eInflight !== undefined) return;
  window.__e2eInflight = 0;
  const track = (p) => {
    window.__e2eInflight++;
    return p.finally(() => { window.__e2eInflight--; });
  };
  const origFetch = window.fetch;
  window.fetch = function (...a) { return track(origFetch.apply(this, a)); };
  for (const m of ['json', 'text', 'blob', 'arrayBuffer']) {
    const orig = Response.prototype[m];
    Response.prototype[m] = function (...a) { return track(orig.apply(this, a)); };
  }
}
"""

# Ключ элемента: тег + id + классы + data-атрибуты + текст без цифр. Цифры —
# счётчики и суммы, которые меняются после соседнего клика; идентичность
# строки несут data-атрибуты. «Форма» ключа — то же без значений data-атрибутов:
# тридцать дней календаря или десять одинаковых «Отменить» у накладных — одна
# форма, и жать их все незачем.
_KEY_JS = """
// Классы-состояния (выбран, подсвечен, раскрыт) в ключ не входят: после
// нажатия кнопка остаётся той же кнопкой.
const STATE_CLASS = /^(active|picked|hidden|loading|open|selected|checked|today)$|^is-|-sel|range|--on$|--active$/;
window.__e2eParts = (el) => {
  const skip = new Set(['rowWired', 'hintWired', 'more', 'e2eK']);
  const dk = Object.keys(el.dataset).filter(k => !skip.has(k)).sort();
  const cls = [...el.classList].filter(c => !STATE_CLASS.test(c)).sort().join('.');
  const text = (el.innerText || el.getAttribute('aria-label') || el.title || '')
    .replace(/\\d+/g, '#').replace(/\\s+/g, ' ').trim().slice(0, 60);
  const head = `${el.tagName.toLowerCase()}#${el.id}.${cls}`;
  // У строки-карточки текст — статус и суммы, они меняются от соседних
  // нажатий; форму строки задают классы и имена data-атрибутов. У кнопки
  // текст и есть действие: «Моточасы» и «Рассрочка» — разные формы.
  const shapeText = el.tagName === 'BUTTON' ? text : '';
  return {
    key: `${head}[${dk.map(k => `${k}=${el.dataset[k]}`).join(',')}] ${text}`,
    shape: `${head}[${dk.join(',')}] ${shapeText}`,
    text,
  };
};
window.__e2eKey = (el) => window.__e2eParts(el).key;
window.__e2eClickables = (sel) => {
  const root = document.getElementById('content');
  const out = [];
  if (!root) return out;
  for (const el of root.querySelectorAll(sel)) {
    if (el.matches('.seg-item[data-sect]')) continue;       // вкладки — это экраны
    if (el.closest('.error-card')) continue;                  // «Повторить» = reload
    if (/location\\.reload/.test(el.getAttribute('onclick') || '')) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.pointerEvents === 'none') continue;
    out.push(el);
  }
  return out;
};
"""


@dataclass
class Crawl:
    page: Page
    role: str
    where: str = "старт"
    failures: list[str] = field(default_factory=list)
    known: set[str] = field(default_factory=set)
    rejected_4xx: list[str] = field(default_factory=list)
    unclickable: list[str] = field(default_factory=list)
    per_screen: dict[str, int] = field(default_factory=dict)
    visited: set = field(default_factory=set)
    screens: int = 0
    clicks: int = 0
    reopens: int = 0
    restarts: int = 0
    gone: int = 0

    # ── события страницы ──

    def listen(self) -> None:
        p = self.page
        p.on("pageerror", lambda exc: self.fail(f"JS-исключение: {exc}"))
        p.on("console", self._on_console)
        p.on("response", self._on_response)

    def _on_console(self, msg) -> None:
        if msg.type != "error":
            return
        # Дубль сетевого ответа: 5xx ловит _on_response, 4xx — не сбой.
        if msg.text.startswith("Failed to load resource"):
            return
        self.fail(f"console.error: {msg.text}")

    def _on_response(self, resp) -> None:
        if resp.status >= 500:
            self.fail(f"HTTP {resp.status} {resp.request.method} {resp.url}")
        elif resp.status >= 400 and "/api/" in resp.url:
            self.rejected_4xx.append(f"{self.where}: HTTP {resp.status} /api/{resp.url.split('/api/', 1)[-1]}")

    def fail(self, what: str) -> None:
        msg = f"[{self.role}] {self.where}: {what}"
        for roles, needles, bug, repro in KNOWN_BUGS:
            if self.role in roles and all(n in msg for n in needles):
                self.known.add(f"{bug} (репро: {repro})")
                return
        self.failures.append(msg)

    # ── ожидания и состояние ──

    def idle(self) -> None:
        """Дождаться конца запросов и отрисовки после действия."""
        self.page.evaluate("() => new Promise(r => requestAnimationFrame(() => r()))")
        self.page.wait_for_function("() => window.__e2eInflight === 0")
        settled(self.page)

    def state(self) -> tuple[str, str]:
        return tuple(self.page.evaluate(
            "() => [document.querySelector('#bottom-nav .nav-item.active')?.dataset.screen || '',"
            " document.querySelector('#content .seg-item.active[data-sect]')?.dataset.sect || '']"
        ))

    def check_screen(self) -> None:
        p = self.page
        if p.locator("#content .error-card").count():
            body = p.locator("#content .error-card").first.inner_text().replace("\n", " ")
            self.fail(f"на экране .error-card: {body}")
        if not p.evaluate("() => (document.getElementById('content')?.innerText || '').trim().length > 0"):
            self.fail("#content пустой")

    def keys(self) -> list[tuple[str, str]]:
        """(ключ, форма) кнопок экрана: сперва действия, потом фильтры; не больше
        MAX_PER_SHAPE одинаковых по форме.

        Фильтры (`.seg-item`, `.cat-btn`) — в конце: выбранный фильтр живёт в
        состоянии раздела и переживает переоткрытие экрана, и нажатый первым
        «Отменены» спрятал бы от обхода все остальные строки.
        """
        return [tuple(x) for x in self.page.evaluate(
            """([sel, perShape]) => {
              const seen = new Set(); const shapes = new Map(); const acts = []; const filters = [];
              for (const el of window.__e2eClickables(sel)) {
                const {key, shape} = window.__e2eParts(el);
                if (seen.has(key)) continue;
                seen.add(key);
                const n = shapes.get(shape) || 0;
                if (n >= perShape) continue;
                shapes.set(shape, n + 1);
                (el.matches('.seg-item, .cat-btn') ? filters : acts).push([key, shape]);
              }
              return acts.concat(filters);
            }""",
            [CLICKABLE, MAX_PER_SHAPE],
        )]

    def mark(self, key: str) -> bool:
        """Пометить первый элемент с этим ключом атрибутом data-e2e-k."""
        return self.page.evaluate(
            "([sel, key]) => { document.querySelectorAll('[data-e2e-k]').forEach(e => e.removeAttribute('data-e2e-k'));"
            " const el = window.__e2eClickables(sel).find(e => window.__e2eKey(e) === key);"
            " if (!el) return false; el.setAttribute('data-e2e-k', '1'); return true; }",
            [CLICKABLE, key],
        )

    # ── навигация ──

    def inject(self) -> None:
        self.page.evaluate(_INFLIGHT_JS)
        self.page.evaluate("() => {" + _KEY_JS + "}")

    def close_overlays(self) -> None:
        """Закрыть открытые формы, не отправляя их: «Отмена» → Escape."""
        p = self.page
        for _ in range(4):
            count = p.locator(".c-overlay").count()
            if count == 0:
                return
            cancel = p.locator(".c-overlay").last.locator(
                "#ms-cancel, #pe-cancel, button:text-is('Отмена'), button:text-is('Закрыть')"
            )
            try:
                if cancel.count():
                    cancel.last.click(timeout=CLICK_TIMEOUT_MS)
                else:
                    p.keyboard.press("Escape")
                p.wait_for_function(
                    "(n) => document.querySelectorAll('.c-overlay').length < n", arg=count,
                    timeout=CLICK_TIMEOUT_MS,
                )
            except PlaywrightError:
                p.keyboard.press("Escape")
            self.idle()
        if p.locator(".c-overlay").count():
            self.fail("форма не закрывается ни «Отменой», ни Escape")
            p.evaluate("() => { document.querySelectorAll('.c-overlay').forEach(o => o.remove());"
                       " document.body.classList.remove('page-sheet-open'); }")

    def restart(self) -> None:
        """Перезагрузить приложение: сбросить фильтры, черновики, режимы вкладок.

        Переоткрытие через нижнюю панель состояние раздела сохраняет — редактор
        накладной или выбранный фильтр остаются. Перезагрузка — только когда
        кнопку не нашли и после переоткрытия: она дорогая.
        """
        p = self.page
        p.evaluate("() => document.querySelectorAll('.c-overlay').forEach(o => o.remove())")
        p.reload()
        p.wait_for_selector("#bottom-nav .nav-item", state="attached")
        self.inject()
        self.restarts += 1

    def click_marked(self) -> bool:
        try:
            self.page.click("[data-e2e-k]", timeout=CLICK_TIMEOUT_MS)
        except PlaywrightError as e:
            self.unclickable.append(f"{self.where}: {str(e).splitlines()[0]}")
            return False
        self.idle()
        self.close_overlays()
        return True

    def goto(self, section: str, tab_key: str | None, path: tuple[str, ...]) -> bool:
        """Открыть экран раздела и пройти путь нажатий до вложенного экрана."""
        where = self.where
        self.close_overlays()
        go(self.page, section)
        if tab_key:
            tab(self.page, tab_key)
        self.idle()
        for k in path:
            if not self.mark(k) or not self.click_marked():
                self.where = where
                return False
        self.where = where
        return True

    def find(self, section: str, tab_key: str | None, path: tuple[str, ...], k: str) -> bool:
        """Найти кнопку: как есть → переоткрыть экран → перезагрузить приложение."""
        if self.mark(k):
            return True
        if self.goto(section, tab_key, path) and self.mark(k):
            return True
        self.restart()
        return self.goto(section, tab_key, path) and self.mark(k)

    # ── обход ──

    def run(self) -> None:
        # Слушатели навешаны после open_app — перезагрузка, чтобы и старт
        # приложения прошёл под ними.
        self.restart()
        sections = self.page.eval_on_selector_all(
            "#bottom-nav .nav-item[data-screen]", "els => els.map(e => e.dataset.screen)"
        )
        assert sections, f"[{self.role}] нижняя панель пуста"
        for section in sections:
            self.where = f"экран {section}"
            self.goto(section, None, ())
            tabs = self.page.eval_on_selector_all(
                "#content .seg-item[data-sect]", "els => els.map(e => e.dataset.sect)"
            )
            for tab_key in tabs or [None]:
                self.crawl(section, tab_key)

    def crawl(self, section: str, tab_key: str | None, path: tuple[str, ...] = (),
              labels: tuple[str, ...] = (), parent_shapes: frozenset = frozenset()) -> None:
        """Нажать каждую кнопку экрана; новые кнопки после нажатия — вложенный экран.

        Вложенный экран — карточка, редактор, развернувшаяся панель: всё, где
        после нажатия появились кнопки НОВОЙ формы. Обходим его тем же способом
        (кнопки родителя повторно не жмём), не глубже MAX_DEPTH, и один раз на
        набор новых форм: три карточки машин с одинаковыми кнопками — один обход.
        """
        screen = f"экран {section}" + (f"/{tab_key}" if tab_key else "") + "".join(
            f" › «{lb}»" for lb in labels
        )
        self.where = screen
        if not self.goto(section, tab_key, path):
            return
        self.screens += 1
        self.check_screen()
        home = self.state()
        here = self.keys()
        here_keys = {k for k, _ in here}
        shapes = parent_shapes | {sh for _, sh in here}
        todo = [(k, sh) for k, sh in here if sh not in parent_shapes][:MAX_CLICKS_PER_SCREEN]
        self.per_screen[screen] = 0
        for k, _sh in todo:
            label = k.split("] ", 1)[-1] or k.split("[", 1)[0]
            self.where = screen
            if not self.find(section, tab_key, path, k):
                self.gone += 1  # кнопку убрал предыдущий клик: удалили, подтвердили
                continue
            self.where = f"{screen}, кнопка «{label}» ({k.split('] ', 1)[0]}])"
            if not self.click_marked():
                continue
            self.clicks += 1
            self.per_screen[screen] += 1
            self.check_screen()
            nav, sect = self.state()
            if nav != home[0] or (sect and sect != home[1]):
                # Ушли в другой раздел или вкладку (очередь «Сегодня», ссылка на
                # долг) — их обойдёт свой проход. Пропавший переключатель
                # вкладок — это карточка внутри раздела, её обходим как вложенный экран.
                self.where = screen
                self.goto(section, tab_key, path)
                self.reopens += 1
                continue
            now = self.keys()
            fresh = frozenset(sh for _, sh in now) - shapes
            sig = (section, tab_key, fresh)
            if fresh and len(path) < MAX_DEPTH and sig not in self.visited:
                self.visited.add(sig)
                self.crawl(section, tab_key, path + (k,), labels + (label,), frozenset(shapes))
                self.where = screen
                self.goto(section, tab_key, path)
                self.reopens += 1
            elif len(here_keys & {x for x, _ in now}) * 2 < len(here_keys):
                self.where = screen
                self.goto(section, tab_key, path)
                self.reopens += 1


def _report(crawl: Crawl, capsys) -> None:
    with capsys.disabled():
        print(
            f"\nобходчик [{crawl.role}]: экранов {crawl.screens}, кликов {crawl.clicks}, "
            f"переоткрытий {crawl.reopens}, перезагрузок {crawl.restarts}, исчезло {crawl.gone}, "
            f"не кликнулось {len(crawl.unclickable)}, 4xx {len(crawl.rejected_4xx)}, "
            f"известных багов {len(crawl.known)}"
        )
        if os.environ.get("E2E_CRAWL_VERBOSE"):
            for scr, n in crawl.per_screen.items():
                print(f"  {scr}: {n}")
            for line in crawl.unclickable:
                print(f"  не кликнулось: {line}")
            for line in sorted(set(crawl.rejected_4xx)):
                print(f"  отказ: {line}")
        for line in sorted(crawl.known):
            print(f"  известный: {line}")


@pytest.fixture
def printer(monkeypatch):
    """Принтер — граница с внешним миром: CUPS «есть», задание «принято»."""
    from services import printing
    from services.printing import PrintResult

    async def fake_print(pdf_bytes, *, filename="", printer_name="", label=""):
        return PrintResult(True, job="e2e")

    monkeypatch.setattr(printing, "is_available", lambda: True)
    monkeypatch.setattr(printing, "print_pdf_bytes", fake_print)


@pytest.fixture
def no_rate_limit(monkeypatch):
    """Лимитер отключён: обходчик жмёт в десятки раз быстрее человека.

    Иначе после сотни кликов экраны «Денег» получали 429 и рисовались без
    данных — обход шёл бы по пустым экранам, а не по кнопкам.
    """
    import webapp.server as server

    monkeypatch.setattr(server, "rate_limit_acquire", lambda *a, **kw: True)


@pytest.mark.parametrize("role", ROLES)
def test_click_everything(open_app, e2e, role, tmp_path, printer, no_rate_limit, capsys):
    _seed_rich(e2e, tmp_path)
    page = open_app(e2e.ids[role])
    crawl = Crawl(page, role)
    crawl.listen()
    try:
        crawl.run()
    finally:
        _report(crawl, capsys)
    assert crawl.screens > 0 and crawl.clicks > 0, f"[{role}] обходчик ничего не нажал"
    assert not crawl.failures, "Падения при обходе:\n" + "\n".join(crawl.failures)


# ─── Репро найденных багов ───────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["keeper", "book"])
def test_new_order_button_for_role_without_orders(open_app, e2e, role):
    """Кнопка, которая гарантированно ведёт в «Нет доступа», — дверь, которая не открывается.

    Правильно — либо кнопки нет, либо она открывает редактор. Нашёл обходчик
    (test_click_everything[keeper], [book]).
    """
    page = open_app(e2e.ids[role])
    go(page, "sales")
    page.wait_for_selector("#content .orders-list")  # список отрисован вместе с кнопкой
    if page.locator("#btn-new-order").count():
        page.click("#btn-new-order")
        page.wait_for_selector("#content .error-card, #choose-agent")
    assert page.locator("#content .error-card").count() == 0, (
        page.locator("#content").inner_text()
    )
