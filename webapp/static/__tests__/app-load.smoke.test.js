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

    // Порядок как в index.html: helpers.js ПЕРЕД app.js.
    expect(() => window.eval(read('helpers.js'))).not.toThrow();
    expect(() => window.eval(read('app.js'))).not.toThrow();

    expect(typeof window.escapeHtml).toBe('function');
    expect(typeof window.idemKey).toBe('function');
    expect(typeof window.formatDateRU).toBe('function');
    // Хелпер реально работает в браузерном scope.
    expect(window.escapeHtml('<x>')).toBe('&lt;x&gt;');
  });

  it('app.js без helpers.js НЕ имеет escapeHtml (доказывает зависимость порядка)', () => {
    const window = makeWindow();
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
      api = async (p) => { window.__calls.push(p); return { ok: true, orders: [], role: 'bookkeeper' }; };
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

    expect(window.__writes[0]).toEqual(['/api/machines/payment', { payment_id: 11, paid: true }]);
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
});

describe('карточка клиента: состав отгрузки', () => {
  const DEMAND = 'a1b2c3d4-1111-2222-3333-444455556666';
  const DETAIL = {
    ok: true, agent_id: 'AG-1', name: 'Acme', phone: '', balance_cents: 0,
    debt: 0, limit: 0, free: 0, orders: [], money_history: [], base_currency: 'USD',
    purchases: {
      count: 1, total_cents: 232000, top_products: [],
      recent: [{ id: DEMAND, date: '2026-04-24 09:03', sum_cents: 232000 }],
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
    // Десять отгрузок — это десять запросов в МойСклад ради строк, которые
    // чаще всего никто не раскроет, а бюджет запросов к МС общий на всех.
    const window = bootAgent();
    await window.__ready;
    expect(window.__calls.map(c => c[0])).toEqual(['/api/clients/detail']);
    expect(window.document.querySelector(`[data-shipment="${DEMAND}"]`)).not.toBeNull();
  });

  it('тап раскрывает позиции', async () => {
    const window = bootAgent();
    await window.__ready;
    window.document.querySelector(`[data-shipment="${DEMAND}"]`).click();
    await new Promise(r => setTimeout(r, 0));

    const box = window.document.getElementById(`shipment-${DEMAND}`);
    expect(box.hidden).toBe(false);
    expect(box.textContent).toContain('Кабель PV 0.6');
    expect(window.__calls[1]).toEqual(['/api/clients/shipment', { demand_id: DEMAND }]);
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
    expect(html).toContain('Итого · 2 поз.');
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

  it('название товара экранируется — оно приходит из МойСклад', () => {
    const window = boot('');
    expect(window.itemsBoxHtml([{ name: '<img src=x>', quantity: 1 }], 'USD')).not.toContain('<img');
  });

  it('повторное открытие не ходит в МойСклад второй раз', async () => {
    const window = bootAgent();
    await window.__ready;
    const row = window.document.querySelector(`[data-shipment="${DEMAND}"]`);
    row.click();
    await new Promise(r => setTimeout(r, 0));
    row.click();  // свернули
    row.click();  // раскрыли снова
    await new Promise(r => setTimeout(r, 0));

    expect(window.__calls.filter(c => c[0] === '/api/clients/shipment')).toHaveLength(1);
  });

  it('сбой МойСклад показывается в строке и не блокирует повтор', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      window.__tries = 0;
      api = async (path) => {
        if (path === '/api/clients/detail') return ${JSON.stringify(DETAIL)};
        window.__tries++;
        throw new Error('МойСклад не ответил, попробуйте позже');
      };
      window.__ready = renderAgentDetail('AG-1');
    `);
    await window.__ready;
    const row = window.document.querySelector(`[data-shipment="${DEMAND}"]`);
    row.click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.document.getElementById(`shipment-${DEMAND}`).textContent).toContain('не ответил');

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

  it('клик по «Сохранить» отправляет новое значение из поля рядом', async () => {
    const window = boot(driver);
    await window.__ready;
    const content = window.document.getElementById('content');
    content.querySelector('.rate-input').value = '0.00009';
    content.querySelector('.rate-save').click();
    await new Promise(r => setTimeout(r, 0));

    const set = window.__calls.filter(([p]) => p === '/api/currency/rates/set');
    expect(set).toHaveLength(1);
    expect(set[0][1]).toEqual({ currency_code: 'UZS', rate_to_base: 0.00009 });
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
        { id: 12, seq: 2, due_date: '2026-09-01', amount_cents: 400000,
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
      ms_id: 'p-1', ms_name: 'Кабель PV 0.6' },
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
      { ms_id: 'p-1', name: 'Кабель PV 0.6', unit: 'м' },
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

    doc.querySelector('.product-suggest [data-ms="p-1"]').click();
    expect(name.value).toBe('Кабель PV 0.6');
    expect(doc.querySelector('#ms-f-unit').value).toBe('м');

    doc.querySelector('#ms-f-expected_qty').value = '500';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(window.__sent.ms_id).toBe('p-1');
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
    doc.querySelector('.product-suggest [data-ms="p-1"]').click();

    name.value = 'Кабель PV 0.6 чёрный';
    name.dispatchEvent(new window.Event('input'));
    await settle();

    doc.querySelector('#ms-f-expected_qty').value = '10';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(window.__sent.ms_id).toBe('');
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
    expect(window.__sent.ms_id).toBe('');
  });
});

describe('пять разделов вместо четырёх', () => {
  const nav = (window) => Array.from(
    window.document.querySelectorAll('#bottom-nav .nav-item')
  ).map(b => b.dataset.screen);

  it('нижняя панель строится под роль', () => {
    const window = boot("currentUser = { role: 'boss' }; buildNav();");
    expect(nav(window)).toEqual(['today', 'sales', 'stock', 'money', 'clients']);
  });

  it('кладовщику не рисуют дверь, которая не открывается', () => {
    // «Склад» и «Клиенты» ответят ему 403 по всем вкладкам.
    const window = boot("currentUser = { role: 'warehouse_keeper' }; buildNav();");
    expect(nav(window)).toEqual(['today', 'sales', 'money']);
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
    expect((await window.__go('debts')).slice(0, 1)).toEqual(['money']);
    expect((await window.__go('debts'))[3]).toBe('debts');
    expect((await window.__go('limits'))[4]).toBe('limits');
  });

  it('подсветка таба переживает вложенный экран', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      buildNav();
      renderOpsSummary = async () => {};
      window.__ready = showScreen('ops');
    `);
    await window.__ready;
    const active = window.document.querySelector('#bottom-nav .nav-item.active');
    expect(active.dataset.screen).toBe('today');
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
    window.eval(read('app.js'));
    expect(window.document.documentElement.style.getPropertyValue('--tg-viewport'))
      .toBe('640px');
  });

  it('вне Telegram переменной нет — остаётся фолбэк из CSS', () => {
    const window = makeWindow();
    delete window.Telegram.WebApp.viewportStableHeight;
    delete window.Telegram.WebApp.viewportHeight;
    window.eval(read('helpers.js'));
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
