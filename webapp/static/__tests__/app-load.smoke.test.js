// Smoke: app.js + helpers.js загружаются вместе в браузер-подобном окружении
// (jsdom) без исключения, и чистые хелперы доступны как глобалы — ровно как в
// index.html (helpers.js подключается ПЕРЕД app.js). Это страхует рефакторинг
// «вынес хелперы в helpers.js»: если порядок/глобалы сломаются — тест упадёт.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

function makeWindow() {
  // Каркас как в index.html: нижняя панель — пустой контейнер, кнопки в него
  // кладёт buildNav под роль пользователя.
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="content"></div>'
    + '<nav class="bottom-nav" id="bottom-nav"></nav></body>', {
    url: 'https://example.org/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const noop = () => {};
  // Telegram WebApp SDK — app.js на верхнем уровне зовёт tg.ready/expand/onEvent/…
  window.Telegram = {
    WebApp: {
      ready: noop,
      expand: noop,
      onEvent: noop,
      colorScheme: 'light',
      themeParams: {},
      initData: 'tgWebAppData=stub', // непустой → init() идёт сразу к fetch, без таймера
      initDataUnsafe: {},
      HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
      showAlert: noop,
      showConfirm: noop,
      setHeaderColor: noop,
      setBackgroundColor: noop,
      MainButton: { show: noop, hide: noop, setText: noop, onClick: noop, offClick: noop },
      BackButton: { show: noop, hide: noop, onClick: noop, offClick: noop },
    },
  };
  // fetch никогда не резолвится → init() повисает на await, без синхронного throw.
  window.fetch = () => new Promise(() => {});
  return window;
}

describe('загрузка фронта (helpers.js + app.js)', () => {
  it('грузятся без исключения и определяют глобальные хелперы', () => {
    const window = makeWindow();

    // Порядок как в index.html: helpers.js, net.js, затем app.js.
    expect(() => window.eval(read('helpers.js'))).not.toThrow();
    expect(() => window.eval(read('net.js'))).not.toThrow();
    expect(() => window.eval(read('app.js'))).not.toThrow();

    expect(typeof window.escapeHtml).toBe('function');
    expect(typeof window.idemKey).toBe('function');
    expect(typeof window.formatDateRU).toBe('function');
    // Хелпер реально работает в браузерном scope.
    expect(window.escapeHtml('<x>')).toBe('&lt;x&gt;');
  });

  it('app.js без helpers.js НЕ имеет escapeHtml (доказывает зависимость порядка)', () => {
    const window = makeWindow();
    // net.js грузим: без него init() сразу уходит в catch и рисует ошибку
    // через icon() из helpers.js — тест проверял бы не то.
    window.eval(read('net.js'));
    expect(() => window.eval(read('app.js'))).not.toThrow();
    // escapeHtml вынесён в helpers.js — без него глобал не определён.
    expect(window.escapeHtml).toBeUndefined();
  });
});

// Драйвер выполняем В ТОМ ЖЕ eval, что и app.js: состояние и функции объявлены
// на верхнем уровне скрипта, и подменить `api` снаружи нельзя.
function boot(driver = '') {
  const window = makeWindow();
  window.eval(read('helpers.js'));
  window.eval(read('net.js'));
  window.eval(`${read('app.js')}\n${driver}`);
  return window;
}

describe('вкладки раздела переживают ре-рендер (UI-BUG-04)', () => {
  // Регресс: шелл вставлялся поверх готового DOM через insertAdjacentHTML, и
  // любой полный ре-рендер внутри вкладки (смена статуса, выбор периода,
  // ошибка сети) переписывал innerHTML и уносил его вместе с обработчиками —
  // пользователь не мог уйти в соседнюю вкладку, не выходя из раздела.
  it('шелл входит в шаблон, а не накладывается сверху', () => {
    const window = boot("currentUser = { role: 'boss' };");
    const html = window.salesShellHtml();
    expect(html).toContain('data-sect="orders"');
    expect(html).toContain('data-sect="report"');
  });

  it('после полного ре-рендера списка заказов вкладки на месте', () => {
    const window = boot(`currentUser = { role: 'manager' };
      ordersData = { orders: [], role: "manager" }; renderOrdersMain();`);
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-sect="orders"]')).not.toBeNull();
    expect(content.querySelector('[data-sect="report"]')).not.toBeNull();
  });

  it('вкладки остаются даже когда экран показывает ошибку сети', () => {
    const window = boot("currentUser = { role: 'boss' };");
    const content = window.document.getElementById('content');
    content.innerHTML = window.salesShellHtml() + window.errorBox('Нет подключения к интернету');
    expect(content.querySelector('[data-sect="orders"]')).not.toBeNull();
    expect(content.textContent).toContain('Нет подключения');
  });
});

describe('«Склад» → вкладка «Техника»', () => {
  const LIST = {
    ok: true,
    machines: [{ id: 7, name: 'JCB 3CX', vin: 'JCB7788', status: 'in_stock', hours: 15200,
                 price_cents: 2500000, currency: 'USD' }],
    counts: { all: 1, in_stock: 1 },
    can_manage: true,
    status_labels: { in_stock: '🏗 На складе' },
  };
  const driver = (role = 'boss') => `
    currentUser = { role: '${role}' };
    window.__calls = [];
    api = async (path, body) => { window.__calls.push([path, body]); return ${JSON.stringify(LIST)}; };
    stockTab = 'machines';
    window.__ready = renderStockScreen();
  `;

  it('вкладка есть у менеджера и выше', () => {
    const window = boot("currentUser = { role: 'manager' };");
    expect(window.stockShellHtml()).toContain('data-sect="machines"');
  });

  it('роли без доступа к ручке вкладку не видят', () => {
    // Ручки /api/machines/* отвечают 403 бухгалтеру и кладовщику — вкладка,
    // которая гарантированно упадёт, только сбивает с толку.
    const window = boot("currentUser = { role: 'bookkeeper' };");
    expect(window.stockShellHtml()).not.toContain('data-sect="machines"');
  });

  it('и не могут в неё попасть в обход переключателя', async () => {
    const window = boot(`
      currentUser = { role: 'bookkeeper' };
      window.__calls = [];
      // /api/stock тоже идёт через api() (прямой fetch в обход таймаута убран).
      api = async (p) => {
        window.__calls.push(p);
        return p === '/api/stock' ? { products: [], categories: [] } : { ok: true, orders: [], role: 'bookkeeper' };
      };
      window.fetch = (p) => { window.__calls.push(p); return Promise.reject(new Error('нет сети')); };
      stockTab = 'machines';
      window.__ready = renderStockScreen();
    `);
    await window.__ready;
    expect(window.__calls.some(p => String(p).includes('/api/machines/'))).toBe(false);
  });

  it('список рисуется, а вкладки остаются на месте (UI-BUG-04)', async () => {
    const window = boot(driver());
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-sect="machines"]')).not.toBeNull();
    expect(content.querySelector('[data-sect="catalog"]')).not.toBeNull();
    expect(content.textContent).toContain('JCB 3CX');
    expect(content.querySelector('[data-machine="7"]').dataset.status).toBe('in_stock');
  });

  it('вкладки остаются и когда экран показывает ошибку сети', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => { throw new Error('Нет подключения к интернету'); };
      stockTab = 'machines';
      window.__ready = renderStockScreen();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-sect="machines"]')).not.toBeNull();
    expect(content.textContent).toContain('Нет подключения');
  });

  it('фильтр по статусу уходит на сервер, а не режет список на клиенте', async () => {
    const window = boot(driver());
    await window.__ready;
    const content = window.document.getElementById('content');
    content.querySelector('[data-mstatus="in_stock"]').click();
    await new Promise(r => setTimeout(r, 0));

    const last = window.__calls[window.__calls.length - 1];
    expect(last[0]).toBe('/api/machines/list');
    expect(last[1].status).toBe('in_stock');
  });
});

describe('техника: формы', () => {
  const CARD = {
    ok: true,
    machine: { id: 7, name: 'JCB 3CX', vin: 'JCB7788', status: 'in_stock', hours: 15200 },
    photos: [], hours: [], deals: [],
    next_statuses: [{ status: 'reserved', label: '🔒 Забронировать' }],
    can_manage: true,
    status_labels: { in_stock: '🏗 На складе', reserved: '🔒 Забронирована' },
  };
  // `responses` — очередь ответов apiResult по порядку вызовов.
  const boot7 = (role, responses) => {
    const window = boot(`
      currentUser = { role: '${role}' };
      tg.showConfirm = (text, cb) => { window.__confirmed = text; cb(true); };
      tg.showAlert = (text) => { window.__alerted = text; };
      window.__writes = [];
      const queue = ${JSON.stringify(responses || [])};
      api = async () => (${JSON.stringify(CARD)});
      apiResult = async (path, body) => {
        window.__writes.push([path, body]);
        return queue.shift() || { ok: true, status: 200, body: { ok: true }, error: '' };
      };
      window.__ready = renderMachineCard(7);
    `);
    return window;
  };

  it('менеджер не видит кнопок, которых ему не разрешит сервер', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      api = async () => (${JSON.stringify({ ...CARD, can_manage: false, next_statuses: [] })});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-mact="hours"]')).not.toBeNull();   // моточасы — можно
    expect(content.querySelector('[data-mact="edit"]')).toBeNull();
    expect(content.querySelector('[data-mact="sale"]')).toBeNull();
    expect(content.querySelector('[data-mstatus-to]')).toBeNull();
  });

  it('обязательное поле не пускает запрос на сервер', async () => {
    const window = boot7('boss', []);
    await window.__ready;
    window.document.querySelector('[data-mact="hours"]').click();
    window.document.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes).toHaveLength(0);
    expect(window.document.querySelector('#ms-error').textContent).toContain('Показание');
  });

  it('откат моточасов: 409 → подтверждение → повтор с force', async () => {
    const window = boot7('boss', [
      { ok: false, status: 409, error: 'Показание меньше предыдущего (15200). Опечатка?',
        body: { needs_force: true, previous: 15200 } },
      { ok: true, status: 200, body: { ok: true }, error: '' },
    ]);
    await window.__ready;
    window.document.querySelector('[data-mact="hours"]').click();
    window.document.querySelector('#ms-f-hours').value = '1500';
    window.document.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes).toHaveLength(2);
    expect(window.__writes[0][1].force).toBeUndefined();
    expect(window.__writes[1][1].force).toBe(true);
    expect(window.__confirmed).toContain('1500');
  });

  it('менеджеру откат не предлагают — его подтверждает руководитель', async () => {
    const window = boot7('manager', [
      { ok: false, status: 409, error: 'Показание меньше предыдущего (15200). Опечатка?',
        body: { needs_force: true, previous: 15200 } },
    ]);
    await window.__ready;
    window.document.querySelector('[data-mact="hours"]').click();
    window.document.querySelector('#ms-f-hours').value = '1500';
    window.document.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes).toHaveLength(1);           // повтора с force нет
    expect(window.__confirmed).toBeUndefined();        // и вопроса тоже
    expect(window.document.querySelector('#ms-error').textContent).toContain('руководитель');
  });

  it('смена статуса отправляет expected — тот, что нарисован на экране', async () => {
    const window = boot7('boss', [{ ok: true, status: 200, body: { ok: true }, error: '' }]);
    await window.__ready;
    window.document.querySelector('[data-mstatus-to="reserved"]').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes[0][0]).toBe('/api/machines/status');
    expect(window.__writes[0][1]).toEqual({ machine_id: 7, status: 'reserved', expected: 'in_stock' });
  });

  it('устаревшая карточка (409) перечитывается, а не просто ругается', async () => {
    const window = boot7('boss', [
      { ok: false, status: 409, error: 'Статус уже «Продана»', body: { current: 'sold' } },
    ]);
    await window.__ready;
    const before = window.__writes.length;
    window.document.querySelector('[data-mstatus-to="reserved"]').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes.length).toBe(before + 1);
    expect(window.__alerted).toContain('Продана');
    // Карточку перерисовали: заголовок на месте, экран не остался пустым.
    expect(window.document.getElementById('content').textContent).toContain('JCB 3CX');
  });

  it('форма правки даёт исправить VIN, но не спрашивает марку и модель', async () => {
    // Марка и модель и так входят в название («JCB 3CX 2019») — два поля с
    // теми же словами приходилось заполнять дважды. Контейнер отслеживается
    // отдельно, а не строкой в карточке машины.
    const window = boot7('boss', []);
    await window.__ready;
    window.document.querySelector('[data-mact="edit"]').click();

    expect(window.document.querySelector('#ms-f-vin').value).toBe('JCB7788');
    expect(window.document.querySelector('#ms-f-brand')).toBeNull();
    expect(window.document.querySelector('#ms-f-model')).toBeNull();
    expect(window.document.querySelector('#ms-f-container_no')).toBeNull();
  });

  it('удаление спрашивает подтверждение и уводит со страницы машины', async () => {
    const window = boot7('boss', [{ ok: true, status: 200, body: { ok: true }, error: '' }]);
    await window.__ready;
    window.document.querySelector('[data-mact="delete"]').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__confirmed).toContain('Удалить');
    expect(window.__writes[0][0]).toBe('/api/machines/delete');
    expect(window.__writes[0][1]).toEqual({ machine_id: 7 });
  });

  it('график рассрочки виден целиком, просрочка отмечена', async () => {
    // По графику решают, звонить ли клиенту — прятать его за ещё одним тапом
    // значит не показывать вовсе.
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify({
        ...CARD,
        machine: { ...CARD.machine, status: 'on_credit' },
        today: '2026-07-31',
        deals: [{
          id: 5, kind: 'credit', price_cents: 2500000, currency: 'USD',
          buyer_name: 'Иванов', sold_at: '2026-06-30', due_date: '2026-11-30',
          payments: [
            { id: 10, seq: 0, due_date: '2026-06-30', amount_cents: 500000, paid_at: '2026-06-30' },
            { id: 11, seq: 1, due_date: '2026-07-30', amount_cents: 1000000, paid_at: null },
            { id: 12, seq: 2, due_date: '2026-08-30', amount_cents: 1000000, paid_at: null },
          ],
        }],
      })});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Первоначальный взнос');
    expect(content.textContent).toContain('Получено');
    // 30.07 при «сегодня» 31.07 — просрочен; 30.08 — ещё впереди.
    expect(content.querySelector('[data-payment="11"]').closest('.c-row').dataset.status).toBe('overdue');
    expect(content.querySelector('[data-payment="12"]').closest('.c-row').dataset.status).toBe('upcoming');
    // Взнос переключать нечем — он получен в момент сделки.
    expect(content.querySelector('[data-payment="10"]')).toBeNull();
  });

  it('менеджер график видит, но отметить платёж не может', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      api = async () => (${JSON.stringify({
        ...CARD, can_manage: false, next_statuses: [], today: '2026-07-31',
        deals: [{
          id: 5, kind: 'credit', price_cents: 100000, currency: 'USD', buyer_name: 'И',
          sold_at: '2026-06-30',
          payments: [{ id: 11, seq: 1, due_date: '2026-08-30', amount_cents: 100000, paid_at: null }],
        }],
      })});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Платёж 1');
    expect(content.querySelector('[data-payment]')).toBeNull();
  });

  it('отметка платежа уходит с новым состоянием', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__writes = [];
      tg.showAlert = (t) => { window.__alerted = t; };
      api = async () => (${JSON.stringify({
        ...CARD, today: '2026-07-31',
        deals: [{
          id: 5, kind: 'credit', price_cents: 100000, currency: 'USD', buyer_name: 'И',
          sold_at: '2026-06-30',
          payments: [{ id: 11, seq: 1, due_date: '2026-08-30', amount_cents: 100000, paid_at: null }],
        }],
      })});
      apiResult = async (path, body) => {
        window.__writes.push([path, body]);
        return { ok: true, status: 200, body: { ok: true, deal_closed: true }, error: '' };
      };
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    window.document.querySelector('[data-payment="11"]').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes[0]).toEqual(['/api/machines/payment', {
      payment_id: 11, paid: true, idempotency_key: expect.any(String),
    }]);
  });

  it('у машины со сделкой кнопки удаления нет', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify({
        ...CARD,
        deals: [{ id: 1, kind: 'sale', price_cents: 100, sold_at: '2026-01-01', buyer_name: 'A' }],
      })});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    expect(window.document.querySelector('[data-mact="delete"]')).toBeNull();
  });

  it('рассрочка спрашивает взнос и срок, продажа — нет', async () => {
    // Дату последнего платежа не спрашиваем вовсе: её считает сервер по
    // графику, а введённая руками она рано или поздно с ним разошлась бы.
    const window = boot7('boss', []);
    await window.__ready;
    window.document.querySelector('[data-mact="sale"]').click();
    expect(window.document.querySelector('#ms-f-months')).toBeNull();
    expect(window.document.querySelector('#ms-f-down_payment')).toBeNull();
    window.document.querySelector('#ms-cancel').click();

    window.document.querySelector('[data-mact="credit"]').click();
    expect(window.document.querySelector('#ms-f-months')).not.toBeNull();
    expect(window.document.querySelector('#ms-f-down_payment')).not.toBeNull();
    expect(window.document.querySelector('#ms-f-due_date')).toBeNull();
  });

  it('срок рассрочки обязателен — без него запрос не уходит', async () => {
    const window = boot7('boss', []);
    await window.__ready;
    window.document.querySelector('[data-mact="credit"]').click();
    window.document.querySelector('#ms-f-price').value = '25000';
    window.document.querySelector('#ms-f-buyer_name').value = 'Иванов';
    window.document.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes).toHaveLength(0);
    expect(window.document.querySelector('#ms-error').textContent).toContain('Срок');
  });

  it('Esc закрывает форму, не отправляя ничего', async () => {
    const window = boot7('boss', []);
    await window.__ready;
    window.document.querySelector('[data-mact="hours"]').click();
    expect(window.document.querySelector('.c-overlay')).not.toBeNull();
    window.document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(window.document.querySelector('.c-overlay')).toBeNull();
    expect(window.__writes).toHaveLength(0);
  });

  it('тап мимо полей форму не закрывает — набранное не теряется', async () => {
    // Форма теперь страница целиком: «клик по фону закрывает» превратился бы
    // в потерю всего набранного от промаха пальцем между полями.
    const window = boot7('boss', []);
    await window.__ready;
    window.document.querySelector('[data-mact="hours"]').click();
    const ov = window.document.querySelector('.c-overlay');
    ov.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    expect(window.document.querySelector('.c-overlay')).not.toBeNull();
    expect(window.document.body.classList.contains('page-sheet-open')).toBe(true);
    window.document.querySelector('#ms-cancel').click();
    expect(window.document.body.classList.contains('page-sheet-open')).toBe(false);
  });
});

describe('карточка клиента: состав отгрузки', () => {
  const INVOICE_ID = 17;
  const DETAIL = {
    ok: true, agent_id: 'AG-1', name: 'Acme', phone: '',
    debt: 0, limit: 0, free: 0, orders: [], money_history: [], base_currency: 'USD',
    purchases: {
      count: 1, total_cents: 232000, top_products: [],
      recent: [{ id: INVOICE_ID, date: '2026-04-24', sum_cents: 232000 }],
    },
  };
  const POSITIONS = {
    ok: true, currency: 'USD',
    positions: [{ name: 'Кабель PV 0.6', quantity: 29, unit: 'шт', price_cents: 8000, sum_cents: 232000 }],
    sum_cents: 232000,
  };
  const bootAgent = () => boot(`
    currentUser = { role: 'boss' };
    window.__calls = [];
    api = async (path, body) => {
      window.__calls.push([path, body]);
      return path === '/api/clients/detail' ? ${JSON.stringify(DETAIL)} : ${JSON.stringify(POSITIONS)};
    };
    window.__ready = renderAgentDetail('AG-1');
  `);

  it('отгрузка не тянет состав, пока её не открыли', async () => {
    // Десять отгрузок — это десять запросов ради строк, которые чаще всего
    // никто не раскроет.
    const window = bootAgent();
    await window.__ready;
    expect(window.__calls.map(c => c[0])).toEqual(['/api/clients/detail']);
    expect(window.document.querySelector(`[data-shipment="${INVOICE_ID}"]`)).not.toBeNull();
  });

  it('тап раскрывает позиции', async () => {
    const window = bootAgent();
    await window.__ready;
    window.document.querySelector(`[data-shipment="${INVOICE_ID}"]`).click();
    await new Promise(r => setTimeout(r, 0));

    const box = window.document.getElementById(`shipment-${INVOICE_ID}`);
    expect(box.hidden).toBe(false);
    expect(box.textContent).toContain('Кабель PV 0.6');
    expect(window.__calls[1]).toEqual(['/api/clients/shipment', { invoice_id: INVOICE_ID }]);
  });

  it('позиции выстроены строками с колонкой сумм, а не абзацем текста', () => {
    // Регресс: состав печатался списком «• Товар: 16 шт × 360 USD = 5 760 USD»
    // тем же мелким серым текстом, что и подзаголовок раскрытой строки —
    // сравнить суммы глазами было нельзя.
    const window = boot('');
    const html = window.itemsBoxHtml(
      [
        { name: 'ThinkPower 6kw', quantity: 16, unit: 'шт', price_cents: 36000, sum_cents: 576000 },
        { name: 'Штекер', quantity: 200, unit: 'шт', price_cents: 100, sum_cents: 20000 },
      ],
      'USD',
    );
    expect(html).toContain('items-box');
    expect((html.match(/items-row/g) || []).length).toBe(2);
    expect(html).toContain('items-sum');
    expect(html).toContain('Итого · 2 позиции');
  });

  it('под единственной позицией итог не печатается — он её повторяет', () => {
    const window = boot('');
    const html = window.itemsBoxHtml(
      [{ name: 'Кабель', quantity: 1, unit: 'шт', price_cents: 8000, sum_cents: 8000 }], 'USD');
    expect(html).not.toContain('items-total');
  });

  it('сумма позиции считается, когда сервер её не прислал', () => {
    // У заказа в ответе только цена и количество — итог строки считает фронт.
    const window = boot('');
    const html = window.itemsBoxHtml(
      [{ name: 'Кабель', quantity: 3, unit: 'шт', price_cents: 8000 }], 'USD');
    expect(html).toContain('240 USD');
  });

  it('название товара экранируется — его вводит человек', () => {
    const window = boot('');
    expect(window.itemsBoxHtml([{ name: '<img src=x>', quantity: 1 }], 'USD')).not.toContain('<img');
  });

  it('повторное открытие не ходит за составом второй раз', async () => {
    const window = bootAgent();
    await window.__ready;
    const row = window.document.querySelector(`[data-shipment="${INVOICE_ID}"]`);
    row.click();
    await new Promise(r => setTimeout(r, 0));
    row.click();  // свернули
    row.click();  // раскрыли снова
    await new Promise(r => setTimeout(r, 0));

    expect(window.__calls.filter(c => c[0] === '/api/clients/shipment')).toHaveLength(1);
  });

  it('сбой показывается в строке и не блокирует повтор', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__tries = 0;
      api = async (path) => {
        if (path === '/api/clients/detail') return ${JSON.stringify(DETAIL)};
        window.__tries++;
        throw new Error('Сервер не ответил, попробуйте позже');
      };
      window.__ready = renderAgentDetail('AG-1');
    `);
    await window.__ready;
    const row = window.document.querySelector(`[data-shipment="${INVOICE_ID}"]`);
    row.click();
    await new Promise(r => setTimeout(r, 0));
    expect(
      window.document.getElementById(`shipment-${INVOICE_ID}`).textContent
    ).toContain('не ответил');

    row.click();  // свернули
    row.click();  // ошибку не кэшируем — вторая попытка должна уйти
    await new Promise(r => setTimeout(r, 0));
    expect(window.__tries).toBe(2);
  });
});

describe('техника: фотографии', () => {
  const CARD = (over = {}) => ({
    ok: true,
    machine: { id: 7, name: 'JCB 3CX', vin: 'JCB7788', status: 'in_stock' },
    photos: [{ id: 11, caption: 'перед', sort_order: 0, uploaded_at: '' }],
    hours: [], deals: [], next_statuses: [], can_manage: true,
    can_upload_photo: true, status_labels: {},
    ...over,
  });
  const bootCard = (card) => boot(`
    currentUser = { role: 'boss' };
    window.__revoked = [];
    URL.createObjectURL = () => 'blob:photo-' + Math.random();
    URL.revokeObjectURL = (u) => window.__revoked.push(u);
    window.fetch = async () => ({ ok: true, blob: async () => ({}) });
    api = async () => (${JSON.stringify(card)});
    window.__ready = renderMachineCard(7);
  `);

  it('blob-URL освобождаются — иначе снимки копятся в памяти WebView', () => {
    const window = boot(`
      window.__revoked = [];
      URL.revokeObjectURL = (u) => window.__revoked.push(u);
      _photoUrls = ['blob:a', 'blob:b'];
      revokePhotoUrls();
      window.__left = _photoUrls.length;
    `);
    expect(window.__revoked).toEqual(['blob:a', 'blob:b']);
    expect(window.__left).toBe(0);   // повторный revoke не должен их отзывать снова
  });

  it('повторный рендер карточки освобождает прошлые снимки', async () => {
    const window = bootCard(CARD());
    await window.__ready;
    await new Promise(r => setTimeout(r, 0));
    const before = window.__revoked.length;
    await window.renderMachineCard(7);
    expect(window.__revoked.length).toBeGreaterThan(before);
  });

  it('кнопку загрузки не рисуем, если канал-хранилище не настроен', async () => {
    const window = bootCard(CARD({ can_upload_photo: false }));
    await window.__ready;
    expect(window.document.querySelector('#machine-photo-add')).toBeNull();
    // Сами фото при этом показываются: отдача от загрузки не зависит.
    expect(window.document.querySelector('.machine-photo')).not.toBeNull();
  });

  it('фото тянутся POST-запросом, а не прямой ссылкой Telegram', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__fetched = [];
      URL.createObjectURL = () => 'blob:x';
      URL.revokeObjectURL = () => {};
      window.fetch = async (path, opts) => { window.__fetched.push([path, opts.method]); return { ok: true, blob: async () => ({}) }; };
      api = async () => (${JSON.stringify(CARD())});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    await new Promise(r => setTimeout(r, 0));
    expect(window.__fetched[0]).toEqual(['/api/machines/photo', 'POST']);
  });

  it('недоступное фото убирает плитку, а не ломает ленту', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      URL.createObjectURL = () => 'blob:x';
      URL.revokeObjectURL = () => {};
      window.fetch = async () => ({ ok: false, status: 404 });
      api = async () => (${JSON.stringify(CARD())});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    await new Promise(r => setTimeout(r, 0));
    expect(window.document.querySelector('.machine-photo')).toBeNull();
    expect(window.document.getElementById('content').textContent).toContain('JCB 3CX');
  });
});

describe('контейнеры', () => {
  const LIST = {
    ok: true,
    containers: [
      { id: 3, number: 'MSKU1234567', status: 'arrived', arrived_at: '2026-08-12',
        diff: { total: 4, unchecked: 0, short: 1, extra: 1, mismatch: 2 } },
      { id: 4, number: 'TCLU7654321', status: 'in_transit', eta_date: '2026-09-01',
        diff: { total: 2, unchecked: 2, short: 0, extra: 0, mismatch: 0 } },
    ],
    counts: { all: 2, in_transit: 1, arrived: 1 },
    can_manage: true,
    status_labels: { in_transit: '🚢 В пути', arrived: '📦 Прибыл' },
  };
  const CARD = (over = {}) => ({
    ok: true,
    container: { id: 3, number: 'MSKU1234567', status: 'arrived', arrived_at: '2026-08-12' },
    items: [
      { id: 10, name: 'Кабель PV 0.6', unit: 'шт', expected_qty: 500, arrived_qty: 500,
        delta: 0, state: 'match' },
      { id: 11, name: 'ThinkPower 6kw', unit: 'шт', expected_qty: 20, arrived_qty: 18,
        delta: -2, state: 'short' },
    ],
    diff: { total: 2, unchecked: 0, short: 1, extra: 0, mismatch: 1 },
    can_manage: true,
    status_labels: { in_transit: '🚢 В пути', arrived: '📦 Прибыл' },
    ...over,
  });

  it('вкладка есть у тех же ролей, что и техника', () => {
    expect(boot("currentUser = { role: 'manager' };").stockShellHtml())
      .toContain('data-sect="containers"');
    expect(boot("currentUser = { role: 'bookkeeper' };").stockShellHtml())
      .not.toContain('data-sect="containers"');
  });

  it('расхождение видно в списке — открывать каждый контейнер не нужно', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(LIST)});
      stockTab = 'containers';
      window.__ready = renderStockScreen();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('расхождений: 2');
    // Контейнер с расхождением подсвечен как проблемный, а не как «прибыл».
    expect(content.querySelector('[data-container="3"]').dataset.status).toBe('rejected');
    expect(content.querySelector('[data-container="4"]').dataset.status).toBe('in_transit');
    // Шелл вкладок раздела на месте (UI-BUG-04).
    expect(content.querySelector('[data-sect="catalog"]')).not.toBeNull();
  });

  it('не сверенный прибывший контейнер так и подписан', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify({
        ...LIST,
        containers: [{ id: 5, number: 'X', status: 'arrived', arrived_at: '2026-08-12',
                       diff: { total: 3, unchecked: 3, short: 0, extra: 0, mismatch: 0 } }],
      })});
      stockTab = 'containers';
      window.__ready = renderStockScreen();
    `);
    await window.__ready;
    expect(window.document.getElementById('content').textContent).toContain('не сверен');
  });

  it('в карточке прибывшего есть поля факта и итог сверки', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(CARD())});
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Расхождений: 1');
    expect(content.querySelector('.qty-input[data-item="11"]').value).toBe('18');
    expect(content.querySelector('#cont-save')).not.toBeNull();
    // Пока не прибыл — отмечать нечего, поэтому кнопки прибытия здесь нет.
    expect(content.querySelector('#cont-arrive')).toBeNull();
  });

  it('пока контейнер в пути, полей факта нет — заполнять их нечем', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(CARD({
        container: { id: 4, number: 'TCLU7654321', status: 'in_transit', eta_date: '2026-09-01' },
        items: [{ id: 12, name: 'Кабель', unit: 'шт', expected_qty: 500, arrived_qty: null,
                  delta: null, state: 'unchecked' }],
        diff: { total: 1, unchecked: 1, short: 0, extra: 0, mismatch: 0 },
      }))});
      window.__ready = renderContainerCard(4);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('.qty-input')).toBeNull();
    expect(content.querySelector('#cont-arrive')).not.toBeNull();
    expect(content.querySelector('#cont-del')).not.toBeNull();
  });

  it('сверка уходит одним запросом на весь состав', async () => {
    // Приёмщик считает подряд и не должен ждать сети после каждой позиции.
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__writes = [];
      api = async () => (${JSON.stringify(CARD())});
      apiResult = async (path, body) => { window.__writes.push([path, body]); return { ok: true, status: 200, body: { ok: true }, error: '' }; };
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    window.document.querySelector('.qty-input[data-item="11"]').value = '19';
    window.document.querySelector('#cont-save').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes).toHaveLength(1);
    expect(window.__writes[0][0]).toBe('/api/containers/check');
    expect(window.__writes[0][1].quantities).toEqual({ 10: '500', 11: '19' });
  });

  it('прибывший контейнер удаляется, пока открыто окно правки', async () => {
    // Приёмку могли завести не на тот контейнер — запрет означал бы вечную
    // неверную строку в списке.
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(CARD())});
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    expect(window.document.querySelector('#cont-del')).not.toBeNull();
  });

  it('после закрытия окна карточка только читается', async () => {
    const closed = { ...CARD(), edit_window: { open: false, hours_left: 0 } };
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify({ ...CARD(), edit_window: { open: false, hours_left: 0 } })});
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('#cont-del')).toBeNull();
    expect(content.querySelector('#cont-save')).toBeNull();
    expect(content.querySelector('#cont-item-add')).toBeNull();
    expect(content.textContent).toContain('Приёмка закрыта');
    void closed;
  });
});

describe('курсы валют: «Сохранить» действительно отправляет курс', () => {
  // Регресс: обработчик поднимался от кнопки к `.debt-card`, которого после
  // пересборки экрана на дизайн-систему в разметке курсов нет. closest отдавал
  // null, чтение `.value` бросало TypeError ДО try/catch — кнопка молча не
  // работала. Проверяем следствие (ушёл ли запрос), а не текст селектора.
  const RATES = {
    base: 'USD',
    rates: [
      { currency_code: 'USD', rate_to_base: 1, updated_at: '2026-07-01 10:00' },
      { currency_code: 'UZS', rate_to_base: 0.00008, updated_at: '2026-07-01 10:00' },
    ],
  };
  const driver = `
    currentUser = { role: 'boss', base_currency: 'USD' };
    window.__calls = [];
    api = async (path, body) => {
      window.__calls.push([path, body]);
      return path === '/api/currency/rates' ? ${JSON.stringify(RATES)} : { ok: true };
    };
    window.__ready = renderCurrencyRates();
  `;

  it('редактор курса отрисован для руководителя', async () => {
    const window = boot(driver);
    await window.__ready;
    const content = window.document.getElementById('content');
    // Базовую валюту не редактируют — редактор ровно один, у UZS.
    expect(content.querySelectorAll('.rate-save').length).toBe(1);
    expect(content.querySelector('.rate-save').dataset.code).toBe('UZS');
  });

  it('мелкий курс показан и вводится перевёрнутым: «сум за 1 USD»', async () => {
    // 0.00008 USD за сум человек не читает и не введёт без ошибки в нулях.
    const window = boot(driver);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toMatch(/1 USD = 12\s?500,00 сум/);
    expect(content.querySelector('.rate-input').value).toBe('12500');
  });

  it('клик по «Сохранить» отправляет новое значение из поля рядом', async () => {
    const window = boot(driver);
    await window.__ready;
    const content = window.document.getElementById('content');
    // В поле — сумы за 1 USD; в базу уходит rate_to_base = 1 / значение.
    content.querySelector('.rate-input').value = '10000';
    content.querySelector('.rate-save').click();
    await new Promise(r => setTimeout(r, 0));

    const set = window.__calls.filter(([p]) => p === '/api/currency/rates/set');
    expect(set).toHaveLength(1);
    expect(set[0][1]).toEqual({ currency_code: 'UZS', rate_to_base: 0.0001 });
  });

  it('нечисловой курс отсекается до запроса', async () => {
    const window = boot(driver);
    await window.__ready;
    const content = window.document.getElementById('content');
    content.querySelector('.rate-input').value = '-1';
    content.querySelector('.rate-save').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__calls.filter(([p]) => p.endsWith('/set'))).toHaveLength(0);
  });
});

describe('деньги: рассрочки в долгах и карточка покупателя', () => {
  const DEBTS = {
    debts: [], role: 'boss', scope: 'company', today: '2026-08-01',
    money_received: [], money_pending: [], remaining_by_currency: [],
    base_currency: 'USD',
    machine_debts: [{
      deal_id: 3, machine_name: 'JCB 3CX', buyer_name: 'Иванов П.', currency: 'USD',
      remaining: 20000, next_due: '2026-07-01', next_amount: 4000, state: 'overdue',
    }],
    totals: {
      orders: { count: 1, base_total: 1000, base_currency: 'USD', partial: false,
                by_currency: [{ currency: 'USD', total: 1000 }] },
      machines: { count: 5, base_total: 20000, base_currency: 'USD', partial: false,
                  by_currency: [{ currency: 'USD', total: 20000 }] },
      all: { count: 6, base_total: 21000, base_currency: 'USD', partial: false,
             by_currency: [{ currency: 'USD', total: 21000 }] },
    },
  };

  it('блок рассрочек виден и красится по ближайшему платежу', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(DEBTS)});
      window.__ready = renderDebts(document.getElementById('content'));
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Рассрочки по технике');
    const row = content.querySelector('[data-buyer]');
    expect(row.dataset.status).toBe('overdue');
    expect(row.textContent).toContain('JCB 3CX');
  });

  it('итог «нам должны» разложен по источникам', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(DEBTS)});
      window.__ready = renderDebts(document.getElementById('content'));
    `);
    await window.__ready;
    const text = window.document.getElementById('content').textContent;
    expect(text).toContain('Нам должны');
    expect(text).toContain('По заказам');
    expect(text).toContain('По технике');
  });

  it('у менеджера (totals: null) блока итогов нет', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      api = async () => (${JSON.stringify({ ...DEBTS, role: 'manager', scope: 'personal',
        machine_debts: [], totals: null })});
      window.__ready = renderDebts(document.getElementById('content'));
    `);
    await window.__ready;
    const text = window.document.getElementById('content').textContent;
    expect(text).not.toContain('Нам должны');
    expect(text).not.toContain('Рассрочки по технике');
  });

  it('карточка покупателя показывает остаток и график', async () => {
    const CARD = {
      ok: true, buyer: 'Иванов П.',
      outstanding: { count: 2, base_total: 8000, base_currency: 'USD', partial: false,
                     by_currency: [{ currency: 'USD', total: 8000 }] },
      aging: { buckets: [{ key: 'not_due', label: 'Срок не наступил', count: 2,
                           base_total: 8000, base_currency: 'USD', partial: false,
                           by_currency: [{ currency: 'USD', total: 8000 }] }] },
      deals: [{
        id: 3, machine_name: 'JCB 3CX', sold_at: '2026-07-01', currency: 'USD',
        payments: [
          { id: 10, seq: 0, due_date: '2026-07-01', amount_cents: 500000, paid_at: '2026-07-01' },
          { id: 11, seq: 1, due_date: '2026-09-01', amount_cents: 400000, paid_at: null },
        ],
      }],
    };
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(CARD)});
      window.__ready = renderBuyerCard('Иванов П.');
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Иванов П.');
    expect(content.textContent).toContain('Первоначальный взнос');
    // Взнос переключать нечем — кнопка только у планового платежа.
    expect(content.querySelectorAll('[data-payment]').length).toBe(1);
  });
});

describe('рассрочка: частичные поступления', () => {
  // Срок в будущем относительно дня прогона тестов.
  const FUTURE_DUE = new Date(Date.now() + 30 * 864e5).toISOString().slice(0, 10);
  const CARD = (over) => ({
    ok: true,
    machine: { id: 7, name: 'JCB 3CX', vin: 'JCB7788', status: 'on_credit' },
    photos: [], hours: [], next_statuses: [], can_manage: true,
    can_upload_photo: false, status_labels: {},
    deals: [{
      id: 3, kind: 'credit', currency: 'USD', price_cents: 2500000,
      sold_at: '2026-07-01', buyer_name: 'Иванов', closed_at: null,
      progress: { paid_cents: 700000, planned_cents: 2500000, left_cents: 1800000 },
      receipts: [{ id: 90, amount_cents: 200000, received_at: '2026-07-20 10:00', note: '' }],
      payments: [
        { id: 10, seq: 0, due_date: '2026-07-01', amount_cents: 500000,
          paid_at: '2026-07-01', covered_cents: 500000, is_paid: true },
        { id: 11, seq: 1, due_date: '2026-08-01', amount_cents: 400000,
          paid_at: null, covered_cents: 200000, is_paid: false },
        // Срок считается от «сегодня», а не зашит константой: тест проверяет
        // состояние «срок ещё впереди», и с фиксированной датой он ломается сам
        // собой в день её наступления — так и вышло с 2026-09-01.
        { id: 12, seq: 2, due_date: FUTURE_DUE, amount_cents: 400000,
          paid_at: null, covered_cents: 0, is_paid: false },
      ],
    }],
    ...over,
  });
  const boot7 = (card) => boot(`
    currentUser = { role: 'boss' };
    tg.showConfirm = (t, cb) => cb(true);
    window.__writes = [];
    api = async () => (${JSON.stringify(card)});
    apiResult = async (p, b) => { window.__writes.push([p, b]); return { ok: true, status: 200, body: { ok: true }, error: '' }; };
    window.__ready = renderMachineCard(7);
  `);

  it('частично внесённый платёж не выглядит неоплаченным', async () => {
    // Клиент принёс часть — если показать «не оплачен», ему позвонят как
    // ничего не заплатившему.
    const window = boot7(CARD());
    await window.__ready;
    const row = window.document.querySelector('[data-payment="11"]').closest('.c-row');
    expect(row.dataset.status).toBe('partial');
    expect(row.textContent).toContain('внесено');
  });

  it('нетронутый платёж остаётся в своём состоянии', async () => {
    const window = boot7(CARD());
    await window.__ready;
    const row = window.document.querySelector('[data-payment="12"]').closest('.c-row');
    expect(row.dataset.status).toBe('upcoming');
  });

  it('итог показывает полученное, включая взнос', async () => {
    const window = boot7(CARD());
    await window.__ready;
    const text = window.document.getElementById('content').textContent;
    expect(text).toContain('Получено');
    expect(text).toContain('осталось');
  });

  it('лента поступлений видна — иначе «сколько внесено» не проверить', async () => {
    const window = boot7(CARD());
    await window.__ready;
    const text = window.document.getElementById('content').textContent;
    expect(text).toContain('Поступления');
    expect(window.document.querySelector('[data-receipt-del="90"]')).not.toBeNull();
  });

  it('оплата вводится суммой, а не только кнопкой «оплачен»', async () => {
    const window = boot7(CARD());
    await window.__ready;
    window.document.querySelector('[data-receipt-add="3"]').click();
    window.document.querySelector('#ms-f-amount').value = '1500';
    window.document.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));

    const call = window.__writes.find(([p]) => p === '/api/machines/receipt');
    expect(call[1].deal_id).toBe(3);
    expect(call[1].amount).toBe('1500');
    expect(call[1].idempotency_key).toBeTruthy();
  });

  it('у закрытой сделки оплату не вносят', async () => {
    const closed = CARD();
    closed.deals[0].closed_at = '2026-09-01';
    const window = boot7(closed);
    await window.__ready;
    expect(window.document.querySelector('[data-receipt-add]')).toBeNull();
  });
});

describe('позиция контейнера: товар выбирают из каталога', () => {
  const ITEMS = [
    { id: 1, name: 'Штекер тип C', unit: 'шт', expected_qty: 5, state: 'unchecked' },
    { id: 2, name: 'Кабель PV 0.6', unit: 'м', expected_qty: 500, state: 'unchecked',
      product_id: 11, product_name: 'Кабель PV 0.6' },
  ];

  it('о позиции вне каталога говорят сразу, а не в момент оприходования', () => {
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.containerItemsHtml(ITEMS, false, true);

    const rows = box.querySelectorAll('.c-row');
    expect(rows[0].textContent).toContain('нет в каталоге');
    expect(rows[1].textContent).not.toContain('нет в каталоге');
    // Привязку можно исправить у любой строки: ошибочный выбор тоже правят.
    expect(box.querySelectorAll('[data-item-link]').length).toBe(2);
  });

  it('без права правки кнопки привязки нет', () => {
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.containerItemsHtml(ITEMS, true, false);
    expect(box.querySelector('[data-item-link]')).toBeNull();
  });

  const formDriver = `
    currentUser = { role: 'manager' };
    window.__sent = null;
    api = async () => ({ ok: true, products: [
      { product_id: 11, name: 'Кабель PV 0.6', unit: 'м' },
    ] });
    apiResult = async (path, body) => { window.__sent = body; return { ok: true, body: {} }; };
    renderContainerCard = async () => {};
    openContainerItemForm(7, false);
  `;

  // Подсказка приходит по debounce'у — ждём его и микротаск ответа.
  const settle = () => new Promise(r => setTimeout(r, 400));

  it('выбор из каталога подставляет название, единицу и уезжает с позицией', async () => {
    const window = boot(formDriver);
    const doc = window.document;
    const name = doc.querySelector('#ms-f-name');
    name.value = 'кабель';
    name.dispatchEvent(new window.Event('input'));
    await settle();

    doc.querySelector('.product-suggest [data-product="11"]').click();
    expect(name.value).toBe('Кабель PV 0.6');
    expect(doc.querySelector('#ms-f-unit').value).toBe('м');

    doc.querySelector('#ms-f-expected_qty').value = '500';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(window.__sent.product_id).toBe(11);
    expect(window.__sent.name).toBe('Кабель PV 0.6');
  });

  it('правка названия после выбора отвязывает товар', async () => {
    // Иначе человек уверен, что вписал новую позицию, а приход уйдёт на
    // прежнюю карточку — молча и не туда.
    const window = boot(formDriver);
    const doc = window.document;
    const name = doc.querySelector('#ms-f-name');
    name.value = 'кабель';
    name.dispatchEvent(new window.Event('input'));
    await settle();
    doc.querySelector('.product-suggest [data-product="11"]').click();

    name.value = 'Кабель PV 0.6 чёрный';
    name.dispatchEvent(new window.Event('input'));
    await settle();

    doc.querySelector('#ms-f-expected_qty').value = '10';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(window.__sent.product_id).toBe('');
    expect(window.__sent.name).toBe('Кабель PV 0.6 чёрный');
  });

  it('свободный ввод остаётся законным: товара может ещё не быть', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__sent = null;
      api = async () => ({ ok: true, products: [] });
      apiResult = async (path, body) => { window.__sent = body; return { ok: true, body: {} }; };
      renderContainerCard = async () => {};
      openContainerItemForm(7, false);
    `);
    const doc = window.document;
    doc.querySelector('#ms-f-name').value = 'Штекер тип C';
    doc.querySelector('#ms-f-name').dispatchEvent(new window.Event('input'));
    await settle();
    expect(doc.querySelector('.product-suggest').textContent).toContain('вписать своё название');

    doc.querySelector('#ms-f-expected_qty').value = '5';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(window.__sent.name).toBe('Штекер тип C');
    expect(window.__sent.product_id).toBe('');
  });
});

describe('пять разделов вместо четырёх', () => {
  const nav = (window) => Array.from(
    window.document.querySelectorAll('#bottom-nav .nav-item[data-screen]')
  ).map(b => b.dataset.screen);

  it('нижняя панель строится под роль: четыре раздела и «Меню» со всеми пятью', () => {
    // Пятый раздел ушёл из панели в шторку — пятый слот занимает «Меню»
    // (см. navBarLayout): вкладки и будущие разделы растут там, а не в ряду.
    const window = boot("currentUser = { role: 'boss' }; buildNav(); openNavDrawer();");
    expect(nav(window)).toEqual(['today', 'sales', 'stock', 'money']);
    expect(window.document.querySelector('#bottom-nav [data-action="menu"]')).not.toBeNull();
    const drawer = Array.from(
      window.document.querySelectorAll('#nav-drawer .nav-link--section'),
    ).map(b => b.dataset.screen);
    expect(drawer).toEqual(['today', 'sales', 'stock', 'money', 'clients']);
  });

  it('кладовщику не рисуют дверь, которая не открывается', () => {
    // «Склад» и «Клиенты» ответят ему 403 по всем вкладкам. Вкладок у его
    // разделов по одной — шторка повторила бы панель, «Меню» нет.
    const window = boot("currentUser = { role: 'warehouse_keeper' }; buildNav();");
    expect(nav(window)).toEqual(['today', 'sales', 'money']);
    expect(window.document.querySelector('#bottom-nav [data-action="menu"]')).toBeNull();
  });

  it('лупа поиска — только ролям, которым отвечает /api/search', () => {
    // Каркас makeWindow без шапки — кнопку кладём, как она стоит в index.html.
    const hidden = (r) => boot(`currentUser = { role: '${r}' };
      document.body.insertAdjacentHTML('afterbegin', '<button id="search-btn"></button>');
      initNav();`)
      .document.getElementById('search-btn').classList.contains('hidden');
    expect(hidden('warehouse_keeper')).toBe(true);
    expect(hidden('bookkeeper')).toBe(true);
    expect(hidden('manager')).toBe(false);
    expect(hidden('boss')).toBe(false);
  });

  it('старые адреса экранов продолжают работать', async () => {
    // Ссылки из бота, пушей и закладок ведут на прежние имена. Алиас обязан
    // перевести и на раздел, и на вкладку, куда содержимое переехало.
    const window = boot(`
      currentUser = { role: 'boss' };
      buildNav();
      renderHome = async () => {}; renderSalesScreen = async () => {};
      renderStockScreen = async () => {}; renderMoneyScreen = async () => {};
      renderClientsScreen = async () => {};
      window.__go = async (s) => { await showScreen(s); return [currentScreen, salesTab, stockTab, moneyTab, clientsTab]; };
    `);
    expect(await window.__go('home')).toEqual(['today', 'orders', 'catalog', 'confirm', 'funnel']);
    expect((await window.__go('analytics')).slice(0, 2)).toEqual(['sales', 'report']);
    expect((await window.__go('stock'))[0]).toBe('stock');
    expect((await window.__go('stock'))[2]).toBe('catalog');
    expect((await window.__go('containers'))[2]).toBe('containers');
    // Имя раздела — не старый адрес: вкладка, выбранная до перехода, остаётся
    // (очередь «Контейнеры не сверены», «Назад» из карточки контейнера).
    expect((await window.__go('stock'))[2]).toBe('containers');
    expect((await window.__go('debts')).slice(0, 1)).toEqual(['money']);
    expect((await window.__go('debts'))[3]).toBe('debts');
    expect((await window.__go('limits'))[4]).toBe('limits');
  });

  it('старый адрес «Накладные» ведёт во вкладку «Склада», таб подсвечен', async () => {
    // Накладные стали вкладкой раздела (UI-бриф п.4); ссылки из бота и пушей
    // на прежний экран `whinvoices` продолжают работать через алиас.
    const window = boot(`
      currentUser = { role: 'boss' };
      buildNav();
      api = async () => ({ invoices: [] });
      window.__ready = showScreen('whinvoices');
    `);
    await window.__ready;
    const active = window.document.querySelector('#bottom-nav .nav-item.active');
    expect(active.dataset.screen).toBe('stock');
    expect(window.document.querySelector('.seg-item[data-sect="invoices"].active')).not.toBeNull();
  });

  it('воронка обращений живёт в «Клиентах», а не в отчёте о деньгах', async () => {
    const FUNNEL = {
      ok: true,
      funnel: { contacted: 64, replied: 51, won: 19, awaiting_reply: 4 },
      awaiting: [{ id: 3, display_name: 'Азиз Р.', last_inbound_at: '2026-08-01 18:40:00' }],
      by_manager: [],
    };
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__calls = [];
      api = async (path) => { window.__calls.push(path); return ${JSON.stringify(FUNNEL)}; };
      clientsTab = 'funnel';
      window.__ready = renderClientsScreen();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(window.__calls).toContain('/api/leads/funnel');
    expect(content.textContent).toContain('Воронка обращений');
    expect(content.textContent).toContain('Азиз Р.');
    // И переключатель раздела на месте (UI-BUG-04).
    expect(content.querySelector('[data-sect="limits"]')).not.toBeNull();
  });

  it('пустая воронка объясняет, что дело в подключении, а не в клиентах', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => ({ ok: true, funnel: { contacted: 0 }, awaiting: [], by_manager: [] });
      clientsTab = 'funnel';
      window.__ready = renderClientsScreen();
    `);
    await window.__ready;
    const text = window.document.getElementById('content').textContent;
    expect(text).toContain('Telegram');
    expect(text).toContain('читать сообщения');
  });
});

describe('«Сегодня» — очередь дел', () => {
  const QUEUE = {
    ok: true,
    total: 9,
    queue: [
      { key: 'overdue_debts', count: 2, title: 'Долги просрочены',
        hint: 'срок оплаты уже прошёл', severity: 'crit', screen: 'money:debts' },
      { key: 'awaiting_reply', count: 4, title: 'Клиенты ждут ответа',
        hint: 'написали и не получили ответа', severity: 'warn', screen: 'clients:funnel' },
      { key: 'unchecked_containers', count: 3, title: 'Контейнеры не сверены',
        hint: 'прибыли, но состав не посчитан', severity: 'info', screen: 'stock:containers' },
    ],
  };

  it('срочность видна формой строки, а не только порядком', () => {
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.workQueueHtml(QUEUE.queue);
    const rows = box.querySelectorAll('[data-queue]');
    expect(rows[0].dataset.status).toBe('overdue');
    expect(rows[1].dataset.status).toBe('pending');
    expect(rows[2].dataset.status).toBe('draft');
    expect(box.textContent).toContain('Требует вас · 9');
  });

  it('пустая очередь — это ответ, а не пустое место', () => {
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.workQueueHtml([]);
    expect(box.textContent).toContain('Всё разобрано');
  });

  it('«дел нет» — строка, а не полэкрана', () => {
    // Полноэкранный .empty-state занимает треть экрана телефона, и «дел нет»
    // выглядело как «экран не загрузился». Такая пустота уместна там, где она
    // И ЕСТЬ весь экран, а очередь — блок среди других.
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.workQueueHtml([]);
    expect(box.querySelector('.empty-state')).toBeNull();
    expect(box.querySelector('.queue-empty')).not.toBeNull();
  });

  it('строка ведёт туда, где дело закрывается — вместе с вкладкой', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      buildNav();
      renderMoneyScreen = async () => {};
      document.getElementById('content').innerHTML = workQueueHtml(${JSON.stringify(QUEUE.queue)});
      wireWorkQueue(document.getElementById('content'));
      window.__where = () => [currentScreen, moneyTab];
    `);
    window.document.querySelector('[data-queue="money:debts"]').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__where()).toEqual(['money', 'debts']);
  });

  it('роль без сводки получает экран из одной очереди, а не отказ', async () => {
    // /api/home отвечает только admin/boss/manager. Раньше кладовщик видел
    // errorBox вместо всего раздела.
    const window = boot(`
      currentUser = { role: 'warehouse_keeper', first_name: 'Пётр' };
      window.__calls = [];
      api = async (p) => { window.__calls.push(p); return ${JSON.stringify(QUEUE)}; };
      window.fetch = (p) => { window.__calls.push(p); return Promise.reject(new Error('403')); };
      window.__ready = renderHome();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(window.__calls).toContain('/api/today');
    expect(window.__calls).not.toContain('/api/home');
    expect(content.textContent).toContain('Долги просрочены');
    expect(content.querySelector('.error-card, .error')).toBeNull();
  });
});

describe('высота окна', () => {
  it('каркас меряется высотой от Telegram, а не 100dvh', () => {
    // WebView не знает про нативную шапку клиента: `dvh` больше видимой
    // области, низ приложения уходит за край и страница прокручивается на
    // пустоту.
    const window = makeWindow();
    window.Telegram.WebApp.viewportStableHeight = 640;
    window.Telegram.WebApp.viewportHeight = 700;
    window.eval(read('helpers.js'));
    window.eval(read('net.js'));
    window.eval(read('app.js'));
    expect(window.document.documentElement.style.getPropertyValue('--tg-viewport'))
      .toBe('640px');
  });

  it('вне Telegram переменной нет — остаётся фолбэк из CSS', () => {
    const window = makeWindow();
    delete window.Telegram.WebApp.viewportStableHeight;
    delete window.Telegram.WebApp.viewportHeight;
    window.eval(read('helpers.js'));
    window.eval(read('net.js'));
    window.eval(read('app.js'));
    expect(window.document.documentElement.style.getPropertyValue('--tg-viewport'))
      .toBe('');
  });

  it('высота пересчитывается по viewportChanged, а не только на старте', () => {
    // Клиент меняет её при развороте окна и повороте экрана; без подписки
    // каркас остался бы в размере первого кадра.
    const handlers = {};
    const window = makeWindow();
    window.Telegram.WebApp.viewportStableHeight = 500;
    window.Telegram.WebApp.onEvent = (name, fn) => { handlers[name] = fn; };
    window.eval(read('helpers.js'));
    window.eval(read('net.js'));
    window.eval(read('app.js'));
    expect(typeof handlers.viewportChanged).toBe('function');

    window.Telegram.WebApp.viewportStableHeight = 812;
    handlers.viewportChanged();
    expect(window.document.documentElement.style.getPropertyValue('--tg-viewport'))
      .toBe('812px');
  });
});

describe('звонки и причина отказа', () => {
  const LIST = {
    ok: true,
    leads: [],
    scope: 'company',
    status_labels: { new: '🆕 В работе', won: '✅ Купил', lost: '🚫 Не купил' },
    connections: [],
    unlinked_calls: [
      { id: 5, display_name: 'Азиз', phone: '901234567', at: '2026-08-04 11:20:00',
        interest: 'кабель', direction: 'in' },
    ],
  };

  it('звонившие без переписки — свой блок, а не строки среди лидов', async () => {
    // Это люди, которых в Telegram ещё нет. Показать их вперемешку с перепиской
    // значит выдать за клиентов, которым можно написать.
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(LIST)});
      clientsTab = 'list';
      window.__ready = renderClientsScreen();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Звонили, но не пишут');
    expect(content.textContent).toContain('Азиз');
    expect(content.querySelector('#call-new')).not.toBeNull();
  });

  it('форма звонка не требует ничего, кроме нажатия', async () => {
    // Половину звонков заносят постфактум, когда номера уже нет под рукой.
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__sent = null;
      api = async () => (${JSON.stringify(LIST)});
      apiResult = async (path, body) => { window.__sent = [path, body]; return { ok: true, body: {} }; };
      openCallForm({});
    `);
    const doc = window.document;
    expect(doc.querySelector('#ms-f-phone')).not.toBeNull();
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__sent[0]).toBe('/api/leads/call_add');
    expect(window.__sent[1].direction).toBe('in');
  });

  it('направление и источник уезжают выбранными', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__sent = null;
      apiResult = async (path, body) => { window.__sent = body; return { ok: true, body: {} }; };
      openCallForm({});
    `);
    const doc = window.document;
    doc.querySelector('[data-dir="out"]').click();
    doc.querySelector('[data-src="channel"]').click();
    doc.querySelector('#ms-f-phone').value = '901234567';
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__sent.direction).toBe('out');
    expect(window.__sent.source).toBe('channel');
    expect(window.__sent.phone).toBe('901234567');
  });

  const REASONS = [
    { key: 'price', label: 'Дорого' },
    { key: 'no_stock', label: 'Нет в наличии' },
    { key: 'other', label: 'Другое' },
  ];

  it('«Не купил» спрашивает причину, но не требует её', async () => {
    // Обязательное поле на редко нажимаемой кнопке приводит к тому, что её
    // перестают нажимать вовсе — и теряется сам факт отказа.
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__sent = null;
      apiResult = async (path, body) => { window.__sent = body; return { ok: true, body: {} }; };
      renderLeadCard = async () => {};
      openLostReasonSheet(7, ${JSON.stringify(REASONS)});
    `);
    const doc = window.document;
    expect(doc.querySelectorAll('[data-reason]').length).toBe(3);

    const skip = Array.from(doc.querySelectorAll('.c-overlay button'))
      .find(b => b.textContent === 'Без причины');
    expect(skip, 'кнопка «Без причины» пропала').toBeTruthy();
    skip.click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__sent.status).toBe('lost');
    expect(window.__sent.reason).toBe('');
  });

  it('выбранная причина уезжает вместе с уточнением', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__sent = null;
      apiResult = async (path, body) => { window.__sent = body; return { ok: true, body: {} }; };
      renderLeadCard = async () => {};
      openLostReasonSheet(7, ${JSON.stringify(REASONS)});
    `);
    const doc = window.document;
    doc.querySelector('[data-reason="no_stock"]').click();
    doc.querySelector('#ms-f-note').value = 'ждал неделю';
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__sent.reason).toBe('no_stock');
    expect(window.__sent.note).toBe('ждал неделю');
  });

  it('без выбора и без «Без причины» форма не отправляется молча', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__sent = null;
      apiResult = async (path, body) => { window.__sent = body; return { ok: true, body: {} }; };
      renderLeadCard = async () => {};
      openLostReasonSheet(7, ${JSON.stringify(REASONS)});
    `);
    const doc = window.document;
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__sent).toBeNull();
    expect(doc.querySelector('#ms-error').textContent).toContain('причину');
  });
});

describe('загрузка фото пачкой', () => {
  // Драйвер подменяет shrinkImage (canvas в jsdom не рисует) и apiResult,
  // и открывает выбор файлов не кликом, а прямым вызовом обработчика.
  const boot2 = (files, apiImpl) => boot(`
    currentUser = { role: 'boss' };
    window.__uploads = [];
    window.__done = 0;
    window.__alerts = [];
    tg.showAlert = (m) => { window.__alerts.push(m); };
    shrinkImage = async (f) => {
      if (f.name === 'битый.png') throw new Error('Это не изображение');
      return 'data:image/jpeg;base64,' + f.name;
    };
    apiResult = async (path, body) => {
      window.__uploads.push(body.data_url);
      return (${apiImpl})(window.__uploads.length, body.data_url);
    };
    // Перехватываем создание input: клик в jsdom диалог не открывает.
    const realCreate = document.createElement.bind(document);
    document.createElement = (tag) => {
      const el = realCreate(tag);
      if (tag === 'input') {
        el.click = () => {
          Object.defineProperty(el, 'files', {
            value: ${files}.map(n => ({ name: n })), configurable: true,
          });
          el.dispatchEvent(new window.Event('change'));
        };
      }
      return el;
    };
    window.__ready = new Promise(res => {
      pickPhotos('/api/products/photo_upload', { ms_id: 'p-1' }, () => {
        window.__done += 1; res();
      });
    });
  `);

  const settle = () => new Promise(r => setTimeout(r, 50));

  it('выбор нескольких файлов уходит несколькими запросами', async () => {
    const window = boot2("['a.jpg','b.jpg','c.jpg']", '() => ({ ok: true, body: {} })');
    await window.__ready;
    expect(window.__uploads.length).toBe(3);
    expect(window.__uploads[0]).toContain('a.jpg');
    expect(window.__uploads[2]).toContain('c.jpg');
  });

  it('экран перерисовывается один раз, а не на каждом снимке', async () => {
    // Перерисовка на каждом сбрасывает прокрутку и мигает половиной списка.
    const window = boot2("['a.jpg','b.jpg','c.jpg','d.jpg']", '() => ({ ok: true, body: {} })');
    await window.__ready;
    await settle();
    expect(window.__done).toBe(1);
  });

  it('частичный сбой называется вслух, а не прячется', async () => {
    // «Загружено 3» при двух упавших — ложь, из-за которой недостающие снимки
    // заметят через неделю.
    // Отказ привязан к файлу, а не к номеру запроса: иначе повторная попытка
    // сдвигает нумерацию и тест проверяет не то, что описывает.
    const window = boot2(
      "['a.jpg','b.jpg','c.jpg']",
      '(n, url) => url.includes("b.jpg") ? { ok: false, error: "Telegram отказал" } : { ok: true, body: {} }',
    );
    await window.__ready;
    await settle();
    const text = window.document.getElementById('toast-host').textContent;
    expect(text).toContain('Загружено: 2');
    expect(text).toContain('не прошли: 1');
    expect(window.__alerts.join(' ')).toContain('b.jpg');
  });

  it('повторная попытка вытягивает сбой, который прошёл сам', async () => {
    // На длинной пачке Telegram притормаживает отправку — это проходит за
    // секунду-другую, и терять из-за этого снимок незачем.
    const window = boot2(
      "['a.jpg']",
      '(n) => n === 1 ? { ok: false, error: "слишком часто" } : { ok: true, body: {} }',  // первая попытка падает
    );
    await window.__ready;
    await settle();
    expect(window.__uploads.length).toBe(2);
    const text = window.document.getElementById('toast-host').textContent;
    expect(text).not.toContain('не прошли');
  });

  it('посторонний файл не роняет остальную пачку', async () => {
    const window = boot2("['a.jpg','битый.png','c.jpg']", '() => ({ ok: true, body: {} })');
    await window.__ready;
    await settle();
    expect(window.__uploads.length).toBe(2);
    expect(window.__alerts.join(' ')).toContain('битый.png');
  });

  it('дубликаты считаются отдельно от новых', async () => {
    const window = boot2(
      "['a.jpg','b.jpg']",
      '(n, url) => ({ ok: true, body: { duplicate: url.includes("a.jpg") } })',
    );
    await window.__ready;
    await settle();
    const text = window.document.getElementById('toast-host').textContent;
    expect(text).toContain('уже были');
    expect(text).toContain('Загружено: 1');
  });

  it('один файл не превращается в отчёт о пачке', async () => {
    const window = boot2("['a.jpg']", '() => ({ ok: true, body: {} })');
    await window.__ready;
    await settle();
    const text = window.document.getElementById('toast-host').textContent;
    expect(text).toContain('Фото добавлено');
    expect(text).not.toContain('Загружено:');
  });
});

describe('тост с ходом дела', () => {
  it('обновляется на месте, а не плодит по строке на шаг', () => {
    // Пачка из десяти снимков иначе завалила бы экран десятью тостами.
    const window = boot();
    const t = window.toast('Загружаю 1 из 3…', 'info', { sticky: true });
    t.update('Загружаю 2 из 3…');
    const host = window.document.getElementById('toast-host');
    expect(host.querySelectorAll('.toast').length).toBe(1);
    expect(host.textContent).toContain('Загружаю 2 из 3…');
    t.dismiss();
  });

  it('обычный тост гаснет сам, липкий — нет', () => {
    const window = boot();
    window.toast('обычный');
    const sticky = window.toast('липкий', 'info', { sticky: true });
    expect(typeof sticky.dismiss).toBe('function');
    expect(window.document.querySelectorAll('.toast').length).toBe(2);
  });
});

describe('снятие фото', () => {
  const PHOTOS = [{ id: 11, caption: 'спереди' }, { id: 12, caption: '' }];

  it('крестик есть у того, кому отвечает ручка удаления', () => {
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.photoStripHtml(PHOTOS, { canDelete: true, alt: 'Фото' });
    expect(box.querySelectorAll('[data-photo-del]').length).toBe(2);
  });

  it('и не рисуется тому, кому ручка ответит отказом', () => {
    // Кнопка, которая гарантированно вернёт 403, только сбивает с толку.
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.photoStripHtml(PHOTOS, { canDelete: false, alt: 'Фото' });
    expect(box.querySelector('[data-photo-del]')).toBeNull();
    expect(box.querySelectorAll('[data-photo]').length).toBe(2);
  });

  it('крестик — сосед снимка, а не кнопка внутри кнопки', () => {
    // Вложенная кнопка это невалидная разметка, и на части клиентов она
    // перестаёт нажиматься вовсе.
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.photoStripHtml(PHOTOS, { canDelete: true, alt: 'Фото' });
    const del = box.querySelector('[data-photo-del]');
    expect(del.closest('.machine-photo')).toBeNull();
    expect(del.closest('.machine-photo-wrap')).not.toBeNull();
  });

  it('снятие спрашивает подтверждение и шлёт id снимка', async () => {
    // Промах по крестику на плитке — обычное дело, а вернуть снимок нечем.
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__asked = 0;
      window.__sent = null;
      window.__done = 0;
      confirmDialog = async () => { window.__asked += 1; return true; };
      apiResult = async (path, body) => { window.__sent = [path, body]; return { ok: true, body: {} }; };
      const box = document.getElementById('content');
      box.innerHTML = photoStripHtml(${JSON.stringify(PHOTOS)}, { canDelete: true, alt: 'Фото' });
      wirePhotoDelete(box, '/api/machines/photo_delete',
        (id) => ({ machine_id: 7, photo_id: id }), () => { window.__done += 1; });
    `);
    window.document.querySelector('[data-photo-del="12"]').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__asked).toBe(1);
    expect(window.__sent[0]).toBe('/api/machines/photo_delete');
    expect(window.__sent[1]).toEqual({ machine_id: 7, photo_id: 12 });
    expect(window.__done).toBe(1);
  });

  it('отказ от подтверждения ничего не удаляет', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__sent = null;
      confirmDialog = async () => false;
      apiResult = async (path, body) => { window.__sent = [path, body]; return { ok: true, body: {} }; };
      const box = document.getElementById('content');
      box.innerHTML = photoStripHtml(${JSON.stringify(PHOTOS)}, { canDelete: true, alt: 'Фото' });
      wirePhotoDelete(box, '/api/machines/photo_delete', (id) => ({ photo_id: id }), () => {});
    `);
    window.document.querySelector('[data-photo-del="11"]').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__sent).toBeNull();
  });

  it('отказ ручки не выдаётся за успех', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__done = 0;
      window.__alerts = [];
      tg.showAlert = (m) => { window.__alerts.push(m); };
      confirmDialog = async () => true;
      apiResult = async () => ({ ok: false, error: 'Фото не найдено' });
      const box = document.getElementById('content');
      box.innerHTML = photoStripHtml(${JSON.stringify(PHOTOS)}, { canDelete: true, alt: 'Фото' });
      wirePhotoDelete(box, '/api/machines/photo_delete', (id) => ({ photo_id: id }),
        () => { window.__done += 1; });
    `);
    window.document.querySelector('[data-photo-del="11"]').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__alerts.join(' ')).toContain('Фото не найдено');
    expect(window.__done).toBe(0);
  });
});

describe('«Залежалось» — фильтр каталога (UI-бриф п.4)', () => {
  const STALE = {
    ok: true,
    days: 60,
    items: [
      { name: 'Кабель PV 0.6', stock: 480, unit: 'м' },
      { name: 'Штекер тип C', stock: 90, unit: 'шт' },
    ],
  };
  const STOCK = { categories: [], products: [
    { product_id: 1, name: 'Кабель PV 0.6', unit: 'м', stock: 480, available: 480, folder_id: '' },
    { product_id: 2, name: 'Штекер тип C', unit: 'шт', stock: 90, available: 90, folder_id: '' },
    { product_id: 3, name: 'Болт М8', unit: 'шт', stock: 12, available: 12, folder_id: '' },
  ]};

  it('вкладки больше нет: у руководства это чип в фильтрах, у менеджера его нет', () => {
    // Ручка отвечает только admin/boss — у менеджера это была бы дверь,
    // которая гарантированно вернёт отказ.
    expect(boot("currentUser = { role: 'boss' };").stockShellHtml()).not.toContain('data-sect="stale"');
    const boss = boot(`currentUser = { role: 'boss' }; stockData = ${JSON.stringify(STOCK)}; renderStockContent();`);
    expect(boss.document.querySelector('[data-stale]')).not.toBeNull();
    const mgr = boot(`currentUser = { role: 'manager' }; stockData = ${JSON.stringify(STOCK)}; renderStockContent();`);
    expect(mgr.document.querySelector('[data-stale]')).toBeNull();
  });

  const bootStale = (extra = '') => boot(`
    currentUser = { role: 'boss' };
    window.__composer = null;
    window.__alerts = [];
    tg.showAlert = (m) => { window.__alerts.push(m); };
    api = async () => (${JSON.stringify(STALE)});
    openChannelComposer = (kind, params) => { window.__composer = [kind, params]; };
    stockData = ${JSON.stringify(STOCK)};
    ${extra}
    renderStockContent();
    window.__ready = (async () => {
      document.querySelector('[data-stale]').click();
      await new Promise(r => setTimeout(r, 0));
    })();
  `);

  it('остаток на экране виден — по нему и решают, что выносить', async () => {
    const window = await bootStale();
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Кабель PV 0.6');
    expect(content.textContent).toContain('480');
    expect(content.textContent).toContain('Без продаж больше 60 дней');
    // Не залежавшийся товар из списка ушёл.
    expect(content.textContent).not.toContain('Болт М8');
    // Шелл раздела на месте (UI-BUG-04).
    expect(content.querySelector('[data-sect="catalog"]')).not.toBeNull();
  });

  it('в пост уходят только отмеченные и только названия', async () => {
    const window = await bootStale();
    await window.__ready;
    const doc = window.document;
    doc.querySelector('.stale-check[value="Кабель PV 0.6"]').click();
    doc.querySelector('#stale-post').click();

    expect(window.__composer[0]).toBe('stale');
    expect(window.__composer[1]).toEqual({ names: ['Кабель PV 0.6'] });
    // Ни остатка, ни единицы измерения в параметрах поста нет.
    expect(JSON.stringify(window.__composer[1])).not.toContain('480');
  });

  it('без отметок пост не собирается', async () => {
    const window = await bootStale();
    await window.__ready;
    window.document.querySelector('#stale-post').click();
    expect(window.__composer).toBeNull();
    expect(window.__alerts.join(' ')).toContain('Отметьте');
  });

  it('пустой список — это ответ «всё продаётся», а не ошибка', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => ({ ok: true, days: 60, items: [] });
      stockData = ${JSON.stringify(STOCK)};
      renderStockContent();
      window.__ready = (async () => {
        document.querySelector('[data-stale]').click();
        await new Promise(r => setTimeout(r, 0));
      })();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Всё продаётся');
    expect(content.querySelector('#stale-post')).toBeNull();
  });

  it('отказ ручки не ломает каталог: фильтр откатывается, список на месте', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__toasts = [];
      toast = (m) => window.__toasts.push(String(m));
      api = async () => { throw new Error('сервер не ответил'); };
      stockData = ${JSON.stringify(STOCK)};
      renderStockContent();
      window.__ready = (async () => {
        document.querySelector('[data-stale]').click();
        await new Promise(r => setTimeout(r, 0));
      })();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(window.__toasts.join(' ')).toContain('сервер не ответил');
    expect(content.textContent).toContain('Болт М8');
    expect(content.querySelector('[data-sect="catalog"]')).not.toBeNull();
  });
});

describe('каталог: категории в два уровня (UI-бриф п.4)', () => {
  const STOCK = {
    categories: [
      { id: 'Запчасти Экскаватор/Адаптер', name: 'Запчасти Экскаватор/Адаптер' },
      { id: 'Запчасти Экскаватор/Ковш', name: 'Запчасти Экскаватор/Ковш' },
      { id: 'Масло', name: 'Масло' },
    ],
    products: [
      { product_id: 1, name: 'Адаптер 20', unit: 'шт', stock: 5, available: 5, folder_id: 'Запчасти Экскаватор/Адаптер' },
      { product_id: 2, name: 'Ковш 0.8', unit: 'шт', stock: 2, available: 2, folder_id: 'Запчасти Экскаватор/Ковш' },
      { product_id: 3, name: 'Масло 10W', unit: 'л', stock: 40, available: 40, folder_id: 'Масло' },
    ],
  };

  it('первый ряд — корни без счётчиков, счётчик только у «Все»', () => {
    const window = boot(`currentUser = { role: 'manager' }; stockData = ${JSON.stringify(STOCK)}; renderStockContent();`);
    const chips = [...window.document.querySelectorAll('[data-cat]')].map(b => b.textContent.trim());
    expect(chips).toEqual(['Все (3)', 'Запчасти Экскаватор', 'Масло']);
    expect(window.document.querySelector('#stock-subcats')).toBeNull();
  });

  it('выбор корня открывает второй ряд и фильтрует; подкатегория — точное совпадение', () => {
    const window = boot(`currentUser = { role: 'manager' }; stockData = ${JSON.stringify(STOCK)}; renderStockContent();`);
    const doc = window.document;
    doc.querySelector('[data-cat="Запчасти Экскаватор"]').click();
    const subs = [...doc.querySelectorAll('[data-subcat]')].map(b => b.textContent.trim());
    expect(subs).toEqual(['Все', 'Адаптер', 'Ковш']);
    let text = doc.getElementById('stock-list').textContent;
    expect(text).toContain('Адаптер 20');
    expect(text).toContain('Ковш 0.8');
    expect(text).not.toContain('Масло 10W');
    doc.querySelector('[data-subcat="Запчасти Экскаватор/Ковш"]').click();
    text = doc.getElementById('stock-list').textContent;
    expect(text).toContain('Ковш 0.8');
    expect(text).not.toContain('Адаптер 20');
  });

  it('фильтры наличия стоят рядом с поиском, а не под списком', () => {
    const window = boot(`currentUser = { role: 'manager' }; stockData = ${JSON.stringify(STOCK)}; renderStockContent();`);
    const content = window.document.getElementById('content');
    const order = [...content.querySelectorAll('#stock-search, #stock-filters, #stock-list')].map(e => e.id);
    expect(order).toEqual(['stock-search', 'stock-filters', 'stock-list']);
  });
});

describe('правка контейнера', () => {
  const CARD = { number: 'MSKU1234567', eta_date: '2026-08-20', notes: 'Запчасти для JCB' };

  it('форма открывается заполненной тем, что есть', () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      openContainerEditForm(7, ${JSON.stringify(CARD)});
    `);
    const doc = window.document;
    expect(doc.querySelector('#ms-f-eta_date').value).toBe('2026-08-20');
    expect(doc.querySelector('#ms-f-notes').value).toBe('Запчасти для JCB');
  });

  it('номер не правится — по нему контейнер ищут', () => {
    // Ошиблись номером — это другой контейнер, а не опечатка в этом.
    const window = boot(`
      currentUser = { role: 'boss' };
      openContainerEditForm(7, ${JSON.stringify(CARD)});
    `);
    expect(window.document.querySelector('#ms-f-number')).toBeNull();
    expect(window.document.querySelector('.c-sheet').textContent).toContain('MSKU1234567');
  });

  it('правка уходит на сервер и перерисовывает карточку', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__sent = null;
      window.__redrawn = 0;
      apiResult = async (path, body) => { window.__sent = [path, body]; return { ok: true, body: {} }; };
      renderContainerCard = async () => { window.__redrawn += 1; };
      openContainerEditForm(7, ${JSON.stringify(CARD)});
    `);
    const doc = window.document;
    doc.querySelector('#ms-f-notes').value = 'Запчасти и кабель';
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__sent[0]).toBe('/api/containers/update');
    expect(window.__sent[1].container_id).toBe(7);
    expect(window.__sent[1].fields.notes).toBe('Запчасти и кабель');
    expect(window.__redrawn).toBe(1);
  });

  it('отказ ручки остаётся в форме, а не закрывает её с потерей ввода', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      apiResult = async () => ({ ok: false, error: 'Приёмка закрыта' });
      renderContainerCard = async () => {};
      openContainerEditForm(7, ${JSON.stringify(CARD)});
    `);
    const doc = window.document;
    doc.querySelector('#ms-f-notes').value = 'новая заметка';
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));

    expect(doc.querySelector('#ms-error').textContent).toContain('Приёмка закрыта');
    expect(doc.querySelector('#ms-f-notes').value).toBe('новая заметка');
  });
});

describe('выбор из справочника листом с поиском', () => {
  // Нативный <select> в Telegram-WebView разворачивается системным списком во
  // весь экран: без поиска, с обрезанными именами. На сотне контрагентов это
  // пролистывание вслепую — отсюда openListPicker.
  const boot4 = (extra = '') => boot(`
    currentUser = { role: 'boss' };
    window.__picked = null;
    ${extra}
    openListPicker({
      title: 'Контрагент',
      items: [
        { id: 1, name: 'ООО Ромашка', sub: 'без Telegram — PDF не отправить' },
        { id: 2, name: 'Джони Ака' },
        { id: 3, name: 'Голиб Ака' },
      ],
      selectedId: 2,
      emptyText: 'Контрагенты не найдены',
      onPick: (item) => { window.__picked = item; },
    });
  `);

  it('показывает весь справочник и помечает уже выбранное', () => {
    const doc = boot4().document;
    const rows = [...doc.querySelectorAll('.picker-list [data-pick]')];
    expect(rows.map(r => r.querySelector('.card-row-title').textContent.trim()))
      .toEqual(['ООО Ромашка', 'Джони Ака', 'Голиб Ака']);
    expect(doc.querySelector('[data-pick="2"]').className).toContain('picked');
    // Подстрочник несёт то, что решает выбор: без Telegram PDF не уйдёт.
    expect(doc.querySelector('[data-pick="1"]').textContent).toContain('без Telegram');
  });

  it('поиск фильтрует по названию, пустой результат — текстом', async () => {
    const window = boot4();
    const doc = window.document;
    const input = doc.querySelector('#ms-f-search');
    input.value = 'ака';
    input.dispatchEvent(new window.Event('input'));
    await new Promise(r => setTimeout(r, 200));
    expect([...doc.querySelectorAll('[data-pick]')].map(r => r.dataset.pick)).toEqual(['2', '3']);

    input.value = 'нет такого';
    input.dispatchEvent(new window.Event('input'));
    await new Promise(r => setTimeout(r, 200));
    expect(doc.querySelectorAll('[data-pick]').length).toBe(0);
    expect(doc.querySelector('.picker-list').textContent).toContain('не найдены');
  });

  it('выбор уходит в onPick только после подтверждения', async () => {
    const window = boot4();
    const doc = window.document;
    doc.querySelector('[data-pick="3"]').click();
    expect(window.__picked).toBeNull();          // клик по строке — ещё не выбор
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__picked).toEqual({ id: 3, name: 'Голиб Ака' });
  });

  it('без выбранной строки подтверждение показывает ошибку в форме', async () => {
    const window = boot4('');
    const doc = window.document;
    // Снимаем предвыбор, как если бы форма открылась пустой.
    doc.querySelector('[data-pick="2"]').classList.remove('picked');
    const w2 = boot(`
      currentUser = { role: 'boss' };
      window.__picked = null;
      openListPicker({ title: 'Товар', items: [{ id: 7, name: 'Болт' }],
                       onPick: (i) => { window.__picked = i; } });
      document.querySelector('#ms-submit').click();
    `);
    await new Promise(r => setTimeout(r, 0));
    expect(w2.__picked).toBeNull();
    expect(w2.document.querySelector('#ms-error').textContent).toContain('Выберите');
  });
});

describe('сегмент вместо нативного списка в форме', () => {
  // Нативный `<select>` всегда стоял на первом пункте; сегмент рисует кнопки
  // невыбранными, и обязательное поле уходило пустым — форма отвечала
  // «Заполните: Тип документа», хотя варианты лежали перед человеком.
  it('первый вариант выбран сразу, если значение не задано', () => {
    const doc = boot(`
      currentUser = { role: 'boss' };
      openMachineSheet({
        title: 'Документ',
        fields: [{ key: 'doc_type', label: 'Тип документа', type: 'select',
                   required: true,
                   options: [['raspiska_ru', 'Расписка'], ['tilxat_uz', 'Тилхат']] }],
        onSubmit: () => true,
      });
    `).document;
    expect(doc.querySelector('#ms-f-doc_type').value).toBe('raspiska_ru');
    expect(doc.querySelector('[data-opt="raspiska_ru"]').className).toContain('active');
    expect(doc.querySelectorAll('.c-sheet select').length).toBe(0);
  });

  it('заданное значение не перебивается первым вариантом', () => {
    const doc = boot(`
      currentUser = { role: 'boss' };
      openMachineSheet({
        title: 'Документ',
        fields: [{ key: 'doc_type', label: 'Тип', type: 'select', value: 'tilxat_uz',
                   options: [['raspiska_ru', 'Расписка'], ['tilxat_uz', 'Тилхат']] }],
        onSubmit: () => true,
      });
    `).document;
    expect(doc.querySelector('#ms-f-doc_type').value).toBe('tilxat_uz');
    expect(doc.querySelector('[data-opt="tilxat_uz"]').className).toContain('active');
  });

  it('клик по варианту кладёт значение в скрытое поле', () => {
    const doc = boot(`
      currentUser = { role: 'boss' };
      openMachineSheet({
        title: 'Документ',
        fields: [{ key: 'doc_type', label: 'Тип', type: 'select',
                   options: [['raspiska_ru', 'Расписка'], ['tilxat_uz', 'Тилхат']] }],
        onSubmit: () => true,
      });
    `).document;
    doc.querySelector('[data-opt="tilxat_uz"]').click();
    expect(doc.querySelector('#ms-f-doc_type').value).toBe('tilxat_uz');
    expect(doc.querySelector('[data-opt="raspiska_ru"]').className).not.toContain('active');
  });
});

describe('контрагента заводят, не выходя из накладной', () => {
  // Справочник пополнялся только из карточки клиента в «Воронке»: приезжал
  // новый покупатель — выписать на него расход было не на кого, и отгрузка
  // вставала. Кнопка стоит ПОД списком: её находят там, где ищут и не находят.
  it('кнопка отдаёт набранное в поиске как название', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__added = null;
      openListPicker({
        title: 'Контрагент',
        items: [{ id: 1, name: 'ООО Ромашка' }],
        emptyText: 'Контрагенты не найдены',
        addLabel: 'Новый контрагент',
        onAdd: (typed) => { window.__added = typed; },
        onPick: () => {},
      });
    `);
    const doc = window.document;
    const input = doc.querySelector('#ms-f-search');
    input.value = 'ООО Бахор Савдо';
    input.dispatchEvent(new window.Event('input'));
    await new Promise(r => setTimeout(r, 200));
    expect(doc.querySelector('.picker-list').textContent).toContain('не найдены');

    doc.querySelector('.picker-add').click();
    expect(window.__added).toBe('ООО Бахор Савдо');
    // Пикер закрылся: форма заведения открывается на его месте, а не поверх.
    expect(doc.querySelector('.c-overlay')).toBeNull();
  });

  it('без onAdd кнопки нет — там, где заводить нечего', () => {
    const doc = boot(`
      currentUser = { role: 'boss' };
      openListPicker({ title: 'Товар', items: [{ id: 7, name: 'Болт' }], onPick: () => {} });
    `).document;
    expect(doc.querySelector('.picker-add')).toBeNull();
  });
});

describe('склад: остатки в «Каталоге» и накладные', () => {
  // Накладные — четвёртая вкладка раздела «Склад» (UI-бриф п.4). Раньше это
  // был дочерний экран с кнопкой между табами и поиском.

  it('список накладных рисуется с шеллом раздела (UI-BUG-04)', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      buildNav();
      api = async () => ({ invoices: [] });
      stockTab = 'invoices';
      window.__ready = renderStockScreen();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('.seg-item[data-sect="invoices"].active')).not.toBeNull();
    expect(content.querySelector('#wh-new')).not.toBeNull();
  });

  it('«Накладные» — вкладка раздела «Склад», кнопки между табами и поиском нет', () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      stockData = { products: [], categories: [] };
      renderStockContent();
    `);
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-wh-go]')).toBeNull();
    expect(content.querySelector('.seg-item[data-sect="invoices"]')).not.toBeNull();
  });

  it('«Каталог» показывает доступный остаток и резерв', () => {
    // Остатки склада и есть каталог: отдельного экрана для них нет, и обещать
    // клиенту то, что уже обещано другому, нельзя — поэтому в строке
    // доступное, а не полный остаток.
    const window = boot(`
      currentUser = { role: 'boss' };
      stockData = { categories: [], products: [
        { product_id: 1, name: 'Болт М8', unit: 'шт', stock: 12, reserve: 4, available: 8,
          folder_name: 'Крепёж' },
        { product_id: 2, name: 'Гайка М8', unit: 'шт', stock: 0, reserve: 0, available: 0,
          folder_name: 'Крепёж' },
      ]};
      renderStockContent();
    `);
    const text = window.document.getElementById('content').textContent;
    expect(text).toContain('Болт М8');
    expect(text).toContain('в резерве 4');
    expect(text).toContain('8');
    // Нулевой остаток показывается словом, а не «0» — иначе его не отличить
    // от неизвестного.
    expect(text).toContain('нет');
  });

  it('список накладных показывает номер, сумму и статус отправки', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => ({ invoices: [
        { id: 5, type: 'outgoing', invoice_number: 'OUT-2026-0001', invoice_date: '2026-09-11',
          status: 'confirmed', currency: 'USD', total_amount_cents: 75000,
          telegram_sent: 0, counterparty_name: 'ООО Ромашка' },
      ]});
      window.__ready = renderWhInvoiceList();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    const text = content.textContent;
    expect(text).toContain('OUT-2026-0001');
    expect(text).toContain('750,00 USD');
    expect(text).toContain('PDF не отправлен');
    // Отмена — только руководству, и кнопка создания на месте.
    expect(content.querySelector('[data-wh-cancel="5"]')).not.toBeNull();
    expect(content.querySelector('#wh-new')).not.toBeNull();
  });

  it('менеджеру кнопку отмены не показывают', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      api = async () => ({ invoices: [
        { id: 5, type: 'outgoing', invoice_number: 'OUT-2026-0001', invoice_date: '2026-09-11',
          status: 'confirmed', currency: 'USD', total_amount_cents: 75000,
          telegram_sent: 1, counterparty_name: 'ООО Ромашка' },
      ]});
      window.__ready = renderWhInvoiceList();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-wh-cancel="5"]')).toBeNull();
    expect(content.textContent).toContain('PDF отправлен');
  });
});

describe('склад: отказ сервера доходит до менеджера', () => {
  // Ручки склада отвечают на отказ 409 с телом {ok:false, code, reason}.
  // api() ищет в теле поле `detail` и, не найдя, показывает «Ошибка сервера
  // (409)» — посчитанная сервером причина терялась ровно там, где она нужна.
  // Поэтому запись идёт через apiResult(). Регресс молчаливый: накладная
  // действительно не проводится, и по поведению отказ неотличим от сбоя.

  const boot409 = (body) => boot(`
    currentUser = { role: 'boss' };
    window.__toasts = [];
    toast = (m) => window.__toasts.push(String(m));
    api = async (p) => {
      // Остатка на клиенте хватает (форма валидна, кнопка активна) — отказ
      // 409 моделирует гонку: кто-то списал товар, пока форму заполняли.
      if (p === '/api/wh/stock') return { products: [
        { product_id: 1, name: 'Болт М8', sku: 'B8', unit: 'шт', quantity: 100 } ]};
      if (p === '/api/wh/counterparties') return { counterparties: [
        { id: 1, name: 'ООО Ромашка', telegram_id: 555 } ]};
      return {};
    };
    apiResult = async () => ({ ok: false, status: 409,
                               body: ${JSON.stringify(body)},
                               error: 'Ошибка сервера (409)' });
    window.__ready = (async () => {
      whView = 'new';
      whDraft = { type: 'outgoing', counterparty_id: '1', comment: '',
                  items: [{ product_id: 1, quantity: 99, price_cents: 100 }] };
      await renderWhInvoiceNew();
      if (document.getElementById('wh-save').disabled) throw new Error('форма валидна, кнопка должна быть активна');
      document.getElementById('wh-save').click();
      await new Promise(r => setTimeout(r, 0));
    })();
  `);

  it('нехватка остатка показывается текстом причины, а не «ошибкой сервера»', async () => {
    const window = boot409({
      ok: false, code: 'insufficient_stock',
      reason: 'Не хватает остатка: #1 (нужно 99, есть 3)',
    });
    await window.__ready;
    const toasts = window.__toasts.join(' | ');
    expect(toasts).toContain('Не хватает остатка');
    expect(toasts).not.toContain('Ошибка сервера');
  });

  it('форма после отказа остаётся на экране — её надо править, а не набирать заново', async () => {
    const window = boot409({ ok: false, code: 'insufficient_stock', reason: 'Не хватает' });
    await window.__ready;
    const content = window.document.getElementById('content');
    // Позиция на месте, кнопка снова активна: отказ — это возврат к правке,
    // а не потеря введённого.
    expect(content.querySelector('.wh-pos')).not.toBeNull();
    expect(content.querySelector('#wh-save').disabled).toBe(false);
  });
});

// ─── Аудит, п.1: stored-XSS в экране заявок босса ───────────────────────────
//
// Имя менеджера, клиент и названия позиций в заявке — ввод ДРУГИХ людей, а
// рендерится всё в сессии босса, у которой есть initData и право одобрять с
// превышением лимита. Без escapeHtml вставленный <img onerror> выполнялся бы.

describe('заявки босса: пользовательский текст экранируется', () => {
  const evil = '<img src=x onerror="window.__pwned=1">';

  async function bootRequests() {
    const window = boot(`
      currentUser = { role: 'boss' };
      buildNav();
      api = async () => ({ requests: [{
        id: 7, full_name: ${JSON.stringify(evil)}, agent_name: ${JSON.stringify(evil)},
        created_at: '2026-03-14', payment_type: 'paid', total: 0,
        items: [{ name: ${JSON.stringify(evil)}, quantity: 1, unit: ${JSON.stringify(evil)} }],
      }] });
      window.__ready = renderPendingRequests();
    `);
    await window.__ready;
    return window;
  }

  it('ни имя менеджера, ни клиент, ни позиции не становятся разметкой', async () => {
    const window = await bootRequests();
    const content = window.document.getElementById('content');
    expect(content.querySelector('img')).toBeNull();
    expect(window.__pwned).toBeUndefined();
    // Текст при этом на месте — экранирован, а не вырезан.
    expect(content.textContent).toContain('<img src=x');
    expect(content.querySelectorAll('.order-card')).toHaveLength(1);
  });
});

// ─── Аудит, п.5: расход проводит только руководство ─────────────────────────

describe('форма накладной: «Расход» показывается только руководству', () => {
  function bootForm(role) {
    const window = boot(`
      currentUser = { role: ${JSON.stringify(role)} };
      whDraft = { type: 'outgoing', counterparty_id: '', items: [], comment: '' };
      api = async (path) => path === '/api/wh/stock'
        ? { products: [{ product_id: 1, name: 'Труба', quantity: 5, unit: 'шт' }] }
        : { counterparties: [] };
      // let whDraft — не свойство window: состояние читаем из того же scope.
      window.__ready = renderWhInvoiceNew().then(() => { window.__type = whDraft.type; });
    `);
    return window;
  }

  it('менеджер видит только приход — ручка ответила бы ему 403 на расход', async () => {
    const window = bootForm('manager');
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-whtype="outgoing"]')).toBeNull();
    expect(content.textContent).toContain('Приход на склад');
    expect(window.__type).toBe('incoming');
  });

  it('боссу доступны оба типа', async () => {
    const window = bootForm('boss');
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-whtype="outgoing"]')).not.toBeNull();
    expect(content.querySelector('[data-whtype="incoming"]')).not.toBeNull();
  });
});

// ─── Сверка вкладок с ролями ручек: вкладка не должна вести к 403 ───────────

describe('вкладки под роль совпадают с тем, кому отвечают ручки', () => {
  it('менеджер в «Клиентах» открывает «Лиды», а не воронку с 403', () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__tabs = sectionTabsFor('clients').map(t => t.key);
    `);
    expect(window.__tabs).toEqual(['list']);
  });

  it('у кладовщика в «Деньгах» нет «Кассы» — /api/deposits/my ему не отвечает', () => {
    const window = boot(`
      currentUser = { role: 'warehouse_keeper' };
      window.__tabs = sectionTabsFor('money').map(t => t.key);
    `);
    expect(window.__tabs).not.toContain('ops');
    expect(window.__tabs).toContain('confirm');
  });
});

describe('диалог количества дописывает позицию в черновик', () => {
  // Регресс, найденный E2E (test_manager_order_to_boss_approval_moves_stock):
  // после переезда на разделы защита «пользователь ушёл с экрана» сравнивала
  // currentScreen с 'orders', а раздел зовётся 'sales'. Позиция уходила на
  // сервер и там оставалась, а редактор её не показывал — «Отправить заявку»
  // не включалась ни у кого и никогда. Здесь — быстрый jsdom-вариант того же
  // сценария: подтверждение MainButton при живом черновике обязано дописать
  // позицию в currentDraftOrder и перерисовать редактор.
  const driver = (screen) => `
    currentUser = { role: 'manager' };
    currentScreen = ${JSON.stringify(screen)};
    currentDraftOrder = { id: 5, items: [], currency: null };
    window.__calls = [];
    api = async (path, body) => { window.__calls.push([path, body]); return { ok: true, item_id: 9 }; };
    renderOrderEditor = () => { window.__rendered = (window.__rendered || 0) + 1; };
    toast = () => {};
    window.Telegram.WebApp.MainButton.onClick = (f) => { window.__confirm = f; };
    openQuantityInput('Кабель ВВГ 3x2.5', 'м', 20, 'p1');
    window.__items = () => currentDraftOrder.items;
    window.__currency = () => currentDraftOrder.currency;
  `;

  async function confirm(window, qty, price) {
    const doc = window.document;
    doc.querySelector('#qty-input').value = qty;
    doc.querySelector('#price-input').value = price;
    await window.__confirm();
    await new Promise(r => setTimeout(r, 0));
  }

  it('в разделе «Продажи» позиция попадает в черновик и редактор перерисовывается', async () => {
    const window = boot(driver('sales'));
    await confirm(window, '2', '100');
    expect(window.__calls[0][0]).toBe('/api/orders/add_item');
    expect(window.__calls[0][1]).toMatchObject({ order_id: 5, quantity: 2, price: 100, product_id: 'p1' });
    expect(window.__items()).toEqual([
      { name: 'Кабель ВВГ 3x2.5', quantity: 2, unit: 'м', price: 100, item_id: 9 },
    ]);
    expect(window.__currency()).toBe('USD');
    expect(window.__rendered).toBe(1);
  });

  it('если пользователь ушёл из раздела, ответ сервера не пишется в чужой DOM', async () => {
    // Сама защита нужна: ответ пришёл, а на экране уже «Деньги».
    const window = boot(driver('money'));
    await confirm(window, '2', '100');
    expect(window.__calls.length).toBe(1);
    expect(window.__items()).toEqual([]);
    expect(window.__rendered).toBeUndefined();
  });
});

describe('одобрение заявки при превышении кредитного лимита', () => {
  // Регресс, найденный E2E (test_over_limit_request_is_approved_only_with_override):
  // сервер отвечает 200 с needs_override — это вопрос, а не успех. Фронт
  // показывал «Заявка одобрена», заявка оставалась висеть, а пути «одобрить с
  // превышением» в WebApp не было вовсе.
  const driver = (answer) => `
    currentUser = { role: 'boss', base_currency: 'USD' };
    window.__calls = [];
    api = async (path, body) => {
      window.__calls.push([path, body]);
      if (body.override) return { ok: true, req_id: body.req_id };
      return { ok: false, needs_override: true, over: { limit: 100, projected: 300 }, req_id: body.req_id };
    };
    renderPendingRequests = async () => { window.__rerendered = true; };
    window.__alerts = [];
    window.Telegram.WebApp.showAlert = (m) => window.__alerts.push(m);
    window.Telegram.WebApp.showConfirm = (m, cb) => { window.__confirmMsg = m; cb(${answer}); };
    window.__done = handleRequest(7, 'approve');
  `;

  it('показывает цифры лимита и повторяет запрос с override тем же ключом', async () => {
    const window = boot(driver(true));
    await window.__done;
    expect(window.__confirmMsg).toContain('лимит');
    expect(window.__confirmMsg).toContain('100');
    expect(window.__confirmMsg).toContain('300');
    expect(window.__calls.length).toBe(2);
    expect(window.__calls[1][1].override).toBe(true);
    expect(window.__calls[1][1].idempotency_key).toBe(window.__calls[0][1].idempotency_key);
    expect(window.__alerts.some(a => a.includes('одобрена'))).toBe(true);
  });

  it('отказ босса не одобряет и не рапортует об успехе', async () => {
    const window = boot(driver(false));
    await window.__done;
    expect(window.__calls.length).toBe(1);
    expect(window.__alerts).toEqual([]);
    expect(window.__rerendered).toBeUndefined();
  });
});

describe('вход: 403 от /api/me', () => {
  // Деактивированный сотрудник получал «Нет связи · Ошибка сервера (403)» с
  // кнопкой «Повторить» — как будто проблема в интернете. Это отказ в
  // доступе, и говорить надо прямо (нашёл E2E test_deactivated_manager_loses_access).
  it('рисует экран «доступ не выдан», а не «нет связи»', async () => {
    const window = makeWindow();
    window.fetch = async () => ({ ok: false, status: 403, json: async () => ({ detail: 'deactivated' }) });
    window.eval(read('helpers.js'));
    window.eval(read('net.js'));
    window.eval(read('app.js'));
    await new Promise(r => setTimeout(r, 0));
    const text = window.document.getElementById('content').textContent;
    expect(text).toContain('Доступ не выдан');
    expect(text).toContain('отключён');
    expect(text).not.toContain('Нет связи');
  });
});

describe('карточка покупателя техники', () => {
  // E2E (test_cov_money): «Внести оплату» в карточке покупателя не нажималась —
  // renderBuyerCard вешал обработчик только на [data-payment], а кнопка
  // поступления жила лишь в карточке машины. И «Получено» считалось по
  // пустому covered_cents: «Получено 0 USD из 20 000 USD».
  const CARD = {
    ok: true, buyer: 'Азиз Рахимов',
    outstanding: { count: 2, by_currency: [{ currency: 'USD', total: 10000 }], base_total: 10000, base_currency: 'USD', partial: false },
    aging: { buckets: [] },
    deals: [{
      id: 5, kind: 'credit', currency: 'USD', machine_name: 'JCB 3CX', sold_at: '2026-06-30',
      payments: [
        { id: 10, seq: 0, due_date: '2026-06-30', amount_cents: 500000, paid_at: '2026-06-30', covered_cents: 500000 },
        { id: 11, seq: 1, due_date: '2026-07-30', amount_cents: 500000, paid_at: '2026-07-29', covered_cents: 500000 },
        { id: 12, seq: 2, due_date: '2026-08-30', amount_cents: 500000, paid_at: null, covered_cents: 0 },
        { id: 13, seq: 3, due_date: '2026-09-30', amount_cents: 500000, paid_at: null, covered_cents: 0 },
      ],
      progress: { received_cents: 500000, down_payment_cents: 500000, paid_cents: 1000000, planned_cents: 2000000, left_cents: 1000000 },
      receipts: [{ id: 3, amount_cents: 500000, received_at: '2026-07-29 10:00:00', note: 'платёж 1' }],
    }],
  };
  const bootBuyer = () => boot(`
    currentUser = { role: 'boss' };
    window.__writes = [];
    window.__renders = 0;
    api = async (path) => { window.__renders += 1; return ${JSON.stringify(CARD)}; };
    apiResult = async (path, body) => {
      window.__writes.push([path, body]);
      return { ok: true, status: 200, body: { ok: true }, error: '' };
    };
    window.__ready = renderBuyerCard('Азиз Рахимов');
  `);

  it('«Получено» берёт прогресс сервера: взнос + поступления', async () => {
    const window = bootBuyer();
    await window.__ready;
    const total = window.document.querySelector('.schedule-total').textContent.replace(/\s+/g, '');
    expect(total).toContain('Получено10000USDиз20000USD');
    expect(total).toContain('осталось10000USD');
  });

  it('«Внести оплату» открывает форму и после записи перерисовывает карточку', async () => {
    const window = bootBuyer();
    await window.__ready;
    window.document.querySelector('[data-receipt-add="5"]').click();
    const amount = window.document.querySelector('.c-overlay #ms-f-amount');
    expect(amount).not.toBeNull();
    amount.value = '1500';
    const before = window.__renders;
    window.document.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes[0][0]).toBe('/api/machines/receipt');
    expect(window.__writes[0][1].deal_id).toBe(5);
    expect(window.__renders).toBeGreaterThan(before);
  });
});

// ─── Безопасность, п.1: единица позиции — ввод другого человека ─────────────
//
// `unit` приходил в /api/orders/add_item как есть и выводился в списке
// заказов, в редакторе черновика, на экране количества и в каталоге склада
// БЕЗ escapeHtml. Руководство открывает эти экраны со своим initData — stored-XSS.

describe('единица позиции (unit) не становится разметкой', () => {
  const evil = '<img src=x onerror="window.__pwned=1">';
  const J = JSON.stringify(evil);

  function expectInert(window, root) {
    expect(root.querySelector('img')).toBeNull();
    expect(window.__pwned).toBeUndefined();
    expect(root.textContent).toContain('<img src=x');
  }

  it('список заказов', () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      ordersData = { role: 'boss', orders: [{
        id: 5, status: 'pending', created_at: '2026-03-14', total: 0, currency: 'USD',
        agent_name: 'Клиент', payment_type: 'paid',
        items: [{ name: 'Труба', quantity: 2, unit: ${J}, price: 1 }],
      }] };
      renderOrdersMain();
    `);
    expectInert(window, window.document.getElementById('content'));
  });

  it('редактор черновика', () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      currentDraftOrder = { order_id: 5, agent_name: '', items: [
        { name: 'Труба', quantity: 2, unit: ${J}, price: 1, item_id: 0 },
      ] };
      renderOrderEditor();
    `);
    expectInert(window, window.document.getElementById('content'));
  });

  it('экран количества', () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      currentDraftOrder = { order_id: 5, items: [] };
      openQuantityInput('Труба', ${J}, 10, '1');
    `);
    expectInert(window, window.document.getElementById('content'));
  });

  it('каталог склада', () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      document.getElementById('content').innerHTML = '<div id="stock-list"></div>';
      stockData = { products: [
        { product_id: 1, name: 'Труба', folder_name: 'Трубы', unit: ${J}, stock: 3, reserve: 0 },
      ], categories: [] };
      renderStockList();
    `);
    expectInert(window, window.document.getElementById('content'));
  });
});

// ─── Безопасность, п.4: «оплачен» по рассрочке — один тап, одно поступление ──

describe('отметка платежа рассрочки: двойной тап не шлёт второй запрос', () => {
  it('пока запрос в полёте, повтор игнорируется; ключ живёт до успеха', async () => {
    const window = boot(`
      window.__calls = [];
      let fail = true;
      apiResult = (path, body) => new Promise(resolve => {
        window.__calls.push(body);
        setTimeout(() => {
          resolve(fail ? { ok: false, status: 500, error: 'сеть' } : { ok: true, status: 200, body: {} });
        }, 5);
      });
      window.__btn = document.createElement('button');
      window.__run = async () => {
        const p1 = sendMachinePayment(7, false, window.__btn);
        window.__disabledDuring = window.__btn.disabled;
        const p2 = sendMachinePayment(7, false, window.__btn);
        const [r1, r2] = await Promise.all([p1, p2]);
        window.__second = r2;
        fail = false;
        await sendMachinePayment(7, false, window.__btn);   // ретрай после отказа
        await sendMachinePayment(7, false, window.__btn);   // новая отметка после успеха
        return r1;
      };
    `);
    await window.__run();
    const calls = window.__calls;
    expect(window.__disabledDuring).toBe(true);
    expect(window.__second).toBeNull();
    expect(calls).toHaveLength(3);
    expect(calls[0].idempotency_key).toBeTruthy();
    expect(calls[1].idempotency_key).toBe(calls[0].idempotency_key);
    expect(calls[2].idempotency_key).not.toBe(calls[0].idempotency_key);
    expect(window.__btn.disabled).toBe(false);
  });
});

// ─── Безопасность, п.8: ключ идемпотентности — на форму, а не на клик ───────
//
// Ключ генерировался в обработчике клика: повтор после обрыва связи (запрос
// дошёл, ответ потерялся) уходил С НОВЫМ ключом и создавал второй платёж,
// вторую сдачу, вторую накладную. Теперь ключ живёт с формой и меняется
// только после успеха.

describe('ключ идемпотентности живёт с формой', () => {
  const flush = () => new Promise(r => setTimeout(r, 0));

  it('платёж и сдача: обрыв → тот же ключ, успех → новый', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__calls = [];
      let fail = true;
      renderMoneyScreen = async () => {};
      tg.showAlert = () => {};
      api = async (path, body) => {
        if (path === '/api/deposits/my') return { deposits: [] };
        if (path === '/api/payments/send' || path === '/api/deposits/create') {
          window.__calls.push([path, body.idempotency_key]);
          if (fail) throw new Error('Нет подключения к интернету');
          return { deposit_id: 1, payment_ids: [1] };
        }
        return {};
      };
      window.__setFail = (v) => { fail = v; };
      window.__ready = renderCashbox(document.getElementById('content'), 'ops');
    `);
    await window.__ready;
    const doc = window.document;
    doc.querySelector('.pay-row-amount').value = '100';
    doc.querySelector('#pay-comment').value = 'аренда';
    doc.querySelector('#dep-amount').value = '50';

    const clickPay = async () => { doc.querySelector('#pay-submit').disabled = false; doc.querySelector('#pay-submit').click(); await flush(); };
    const clickDep = async () => { doc.querySelector('#dep-create').disabled = false; doc.querySelector('#dep-create').click(); await flush(); };

    await clickPay(); await clickPay();
    await clickDep(); await clickDep();
    window.__setFail(false);
    await clickPay(); await clickPay();
    await clickDep(); await clickDep();

    const keys = (p) => window.__calls.filter(c => c[0] === p).map(c => c[1]);
    for (const p of ['/api/payments/send', '/api/deposits/create']) {
      const k = keys(p);
      expect(k).toHaveLength(4);
      expect(k[0]).toBeTruthy();
      expect(k[1]).toBe(k[0]);   // ретрай после обрыва
      expect(k[2]).toBe(k[0]);   // успех — тем же ключом
      expect(k[3]).not.toBe(k[0]); // следующий — уже новым
    }
  });

  it('отправка заказа: ключ черновика переживает отказ', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__keys = [];
      let fail = true;
      tg.showAlert = () => {};
      renderOrders = async () => {};
      api = async (path, body) => {
        window.__keys.push(body.idempotency_key);
        if (fail) throw new Error('Нет подключения к интернету');
        return { req_id: 1 };
      };
      currentDraftOrder = { id: 5, items: [], payment_type: 'paid' };
      window.__ready = (async () => {
        await submitOrder();
        await submitOrder();
        fail = false;
        await submitOrder();
      })();
    `);
    await window.__ready;
    expect(window.__keys).toHaveLength(3);
    expect(window.__keys[0]).toBeTruthy();
    expect(new Set(window.__keys).size).toBe(1);
  });

  it('накладная: обрыв → тот же ключ, отказ по существу → новый', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__keys = [];
      const answers = [
        { ok: false, status: 0, body: {}, error: 'Нет подключения к интернету' },
        { ok: false, status: 409, body: { ok: false, code: 'insufficient_stock', reason: 'мало' }, error: '' },
        { ok: true, status: 200, body: { invoice_number: 'П-1' }, error: '' },
      ];
      toast = () => {};
      renderWhInvoicesTab = () => {};
      api = async (p) => {
        if (p === '/api/wh/stock') return { products: [
          { product_id: 1, name: 'Болт М8', sku: 'B8', unit: 'шт', quantity: 100 } ]};
        if (p === '/api/wh/counterparties') return { counterparties: [] };
        return {};
      };
      apiResult = async (path, body) => { window.__keys.push(body.idempotency_key); return answers.shift(); };
      window.__ready = (async () => {
        whView = 'new';
        whDraft = { type: 'incoming', counterparty_id: '', comment: '',
                    items: [{ product_id: 1, quantity: 2, price_cents: null }] };
        await renderWhInvoiceNew();
        for (let i = 0; i < 3; i++) {
          document.getElementById('wh-save').disabled = false;
          document.getElementById('wh-save').click();
          await new Promise(r => setTimeout(r, 0));
        }
      })();
    `);
    await window.__ready;
    const k = window.__keys;
    expect(k).toHaveLength(3);
    expect(k[0]).toBeTruthy();
    expect(k[1]).toBe(k[0]);
    expect(k[2]).not.toBe(k[0]);
  });
});

describe('шторка «Меню»', () => {
  // Экраны заглушены: проверяется навигация (куда ведёт пункт, что с «Назад»,
  // фокусом и inert), а не рендеры разделов — их покрывают свои тесты.
  const bootDrawer = (role) => {
    const window = boot(`
      currentUser = { role: '${role}' };
      renderHome = async () => {}; renderSalesScreen = async () => {};
      renderStockScreen = async () => { stockTab = sectionShell('stock', stockTab).active; };
      renderMoneyScreen = async () => {}; renderClientsScreen = async () => {
        clientsTab = sectionShell('clients', clientsTab).active; };
      buildNav();
      window.__state = () => ({ screen: currentScreen, stockTab, clientsTab, open: !!_navDrawer });
    `);
    const back = { visible: false, handler: null };
    window.Telegram.WebApp.BackButton = {
      show() { back.visible = true; }, hide() { back.visible = false; },
      onClick(f) { back.handler = f; }, offClick() { back.handler = null; },
    };
    return { window, back, doc: window.document };
  };
  const tick = () => new Promise(r => setTimeout(r, 0));

  it('пункт-вкладка ведёт в раздел и вкладку, шторка закрывается', async () => {
    const { window, doc } = bootDrawer('manager');
    await window.showScreen('today');
    doc.getElementById('nav-menu-btn').click();
    expect(doc.getElementById('nav-drawer').classList.contains('is-open')).toBe(true);
    expect(doc.getElementById('nav-menu-btn').getAttribute('aria-expanded')).toBe('true');
    // Текущий раздел без вкладок подсвечен сам.
    expect(doc.querySelector('#nav-drawer [aria-current="page"]').dataset.screen).toBe('today');

    doc.querySelector('#nav-drawer .nav-link[data-screen="stock"][data-tab="invoices"]').click();
    await tick();
    expect(window.__state()).toMatchObject({ screen: 'stock', stockTab: 'invoices', open: false });
    expect(doc.getElementById('nav-drawer').classList.contains('is-open')).toBe(false);
    expect(doc.getElementById('nav-drawer').hasAttribute('inert')).toBe(true);
    expect(doc.getElementById('bottom-nav').dataset.current).toBe('stock');

    // При следующем открытии подсвечена именно вкладка, раздел — «внутри».
    window.openNavDrawer();
    const cur = doc.querySelectorAll('#nav-drawer [aria-current="page"]');
    expect(cur.length).toBe(1);
    expect(cur[0].dataset.tab).toBe('invoices');
    expect(doc.querySelector('#nav-drawer .nav-link--section[data-screen="stock"]').classList
      .contains('is-within')).toBe(true);
  });

  it('раздел, которого нет в панели, подсвечивает «Меню»', async () => {
    const { window, doc } = bootDrawer('boss');
    window.openNavDrawer();
    doc.querySelector('#nav-drawer .nav-link--section[data-screen="clients"]').click();
    await tick();
    expect(window.__state().screen).toBe('clients');
    expect(doc.getElementById('nav-menu-btn').classList.contains('active')).toBe(true);
    expect(doc.querySelector('#bottom-nav .nav-item[data-screen].active')).toBeNull();
    // У менеджера нет «Воронки» — в шторке её тоже нет (403 не рисуем).
    const mgr = bootDrawer('manager');
    mgr.window.openNavDrawer();
    expect(mgr.doc.querySelector('#nav-drawer [data-tab="funnel"]')).toBeNull();
    expect(mgr.doc.querySelector('#nav-drawer [data-tab="invoices"]')).not.toBeNull();
  });

  it('«Назад» Telegram закрывает шторку и возвращает «Назад» экрана', () => {
    const { window, back } = bootDrawer('boss');
    const screenBack = () => {};
    window.showBack(screenBack);
    window.openNavDrawer();
    expect(back.visible).toBe(true);
    expect(back.handler).not.toBe(screenBack);
    back.handler();
    expect(window.__state().open).toBe(false);
    expect(back.visible).toBe(true);
    expect(back.handler).toBe(screenBack);
  });

  it('без «Назад» экрана закрытие её прячет; Esc, фон и крестик закрывают', () => {
    const { window, back, doc } = bootDrawer('boss');
    const esc = () => doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    for (const close of [
      esc,
      () => doc.querySelector('#nav-drawer .nav-drawer-scrim').click(),
      () => doc.querySelector('#nav-drawer .nav-drawer-close').click(),
    ]) {
      doc.getElementById('nav-menu-btn').click();
      expect(window.__state().open).toBe(true);
      close();
      expect(window.__state().open).toBe(false);
      expect(back.visible).toBe(false);
      expect(doc.getElementById('nav-menu-btn').getAttribute('aria-expanded')).toBe('false');
    }
  });

  it('тот же пункт из вложенного экрана ведёт к списку раздела', async () => {
    const { window, doc } = bootDrawer('boss');
    await window.showScreen('stock');
    window.showBack(() => {});        // открыта, например, карточка машины
    window.openNavDrawer();
    doc.querySelector('#nav-drawer .nav-link[data-tab="catalog"]').click();
    await tick();
    expect(window.__state()).toMatchObject({ screen: 'stock', stockTab: 'catalog', open: false });
  });
});
