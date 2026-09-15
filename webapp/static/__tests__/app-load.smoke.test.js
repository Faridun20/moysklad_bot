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
    can_request: ['reserve', 'sale', 'credit'],
    can_delete: true,
    request: null,
    status_labels: { in_stock: '🏗 На складе', reserved: '🔒 Забронирована' },
  };
  // `responses` — очередь ответов apiResult по порядку вызовов.
  const boot7 = (role, responses, card) => {
    const window = boot(`
      currentUser = { role: '${role}', prefs: { work_actions: true } };
      tg.showConfirm = (text, cb) => { window.__confirmed = text; cb(true); };
      tg.showAlert = (text) => { window.__alerted = text; };
      window.__writes = [];
      const queue = ${JSON.stringify(responses || [])};
      api = async () => (${JSON.stringify(card || CARD)});
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
      api = async () => (${JSON.stringify({ ...CARD, can_manage: false, next_statuses: [], can_request: [], can_delete: false })});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-mact="hours"]')).not.toBeNull();   // моточасы — можно
    expect(content.querySelector('[data-mact="edit"]')).toBeNull();
    expect(content.querySelector('[data-mact="sale"]')).toBeNull();
    expect(content.querySelector('[data-mact="delete"]')).toBeNull();
    expect(content.querySelector('[data-mstatus-to]')).toBeNull();
  });

  it('менеджер оформляет бронь, продажу и рассрочку — заявкой руководителю', async () => {
    const window = boot7('manager', [
      { ok: true, status: 200, body: { ok: true, pending: true, request_id: 3 }, error: '' },
    ], { ...CARD, can_manage: false, next_statuses: [] });
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-mact="reserve"]')).not.toBeNull();
    expect(content.querySelector('[data-mact="sale"]')).not.toBeNull();
    content.querySelector('[data-mact="credit"]').click();
    expect(window.document.querySelector('#ms-submit').textContent).toContain('на одобрение');
    window.document.querySelector('#ms-f-price').value = '24000';
    window.document.querySelector('#ms-f-down_payment').value = '0';
    window.document.querySelector('#ms-f-months').value = '6';
    window.document.querySelector('#ms-f-buyer_name').value = 'Азиз';
    window.document.querySelector('#ms-f-buyer_passport').value = 'AA1';
    window.document.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes[0][0]).toBe('/api/machines/deal');
    expect(window.__writes[0][1]).toMatchObject({ machine_id: 7, kind: 'credit', down_payment: '0', months: '6' });
    expect(window.document.querySelector('.toast').textContent).toContain('руководителю');
  });

  it('заявка на одобрении: руководитель видит условия и кнопки решения, пользовательский текст экранируется', async () => {
    const request = {
      id: 3, machine_id: 7, kind: 'credit', kind_label: 'Рассрочка', status: 'pending',
      status_label: '⏳ Ждёт одобрения', price_cents: 2400000, list_price_cents: 2500000,
      currency: 'USD', discount_pct: 4, buyer_name: '<img src=x onerror=alert(1)>',
      buyer_passport: 'AA1', created_by: 1, creator_name: 'Manager', attempts: 1,
      schedule_preview: { down_payment_cents: 0, months: 6, monthly_cents: 400000, first_due: '2026-10-15', last_due: '2027-03-15' },
    };
    const window = boot(`
      currentUser = { role: 'boss' };
      tg.showConfirm = (text, cb) => cb(true);
      window.__writes = [];
      api = async () => (${JSON.stringify({ ...CARD, request, can_request: [], next_statuses: [], can_decide: true, viewer_id: 2 })});
      apiResult = async (path, body) => { window.__writes.push([path, body]); return { ok: true, status: 200, body: { ok: true }, error: '' }; };
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('скидка 4%');
    expect(content.textContent).toContain('6 месяцев');
    expect(content.querySelector('img')).toBeNull();
    expect(content.querySelector('[data-mact="sale"]')).toBeNull();
    expect(content.querySelector('[data-mact="delete"]')).toBeNull();
    content.querySelector('[data-mreq-approve="3"]').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes[0][0]).toBe('/api/machines/deals/approve');
    expect(window.__writes[0][1].request_id).toBe(3);
    expect(window.__writes[0][1].idempotency_key).toBeTruthy();
  });

  it('менеджер без права решать видит «ждёт решения» и может отозвать свою заявку', async () => {
    const request = {
      id: 4, machine_id: 7, kind: 'sale', kind_label: 'Продажа', status: 'pending',
      status_label: '⏳ Ждёт одобрения', price_cents: 2400000, currency: 'USD',
      buyer_name: 'ООО Карьер', created_by: 1, creator_name: 'Manager',
    };
    const window = boot(`
      currentUser = { role: 'manager' };
      api = async () => (${JSON.stringify({ ...CARD, request, can_manage: false, can_request: [], next_statuses: [], can_decide: false, decide_hint: 'решит Boss', viewer_id: 1 })});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-mreq-approve]')).toBeNull();
    expect(content.querySelector('[data-mreq-cancel="4"]')).not.toBeNull();
    expect(content.textContent).toContain('решит Boss');
  });

  it('группа решений рендерится в любой контейнер и возвращает число заявок', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async (path) => path === '/api/machines/deals/pending'
        ? { ok: true, can_decide: true, viewer_id: 2, my_rework: [], requests: [
            { id: 9, machine_id: 7, machine_name: 'CAT 320', kind: 'sale', kind_label: 'Продажа',
              status: 'pending', status_label: '⏳ Ждёт одобрения', price_cents: 100, currency: 'USD',
              buyer_name: 'Покупатель', created_by: 1 } ] }
        : {};
      const box = document.createElement('div');
      document.body.appendChild(box);
      window.__box = box;
      window.__ready = renderMachineDealDecisions(box);
    `);
    const n = await window.__ready;
    expect(n).toBe(1);
    expect(window.__box.textContent).toContain('CAT 320');
    expect(window.__box.querySelector('[data-mreq-reject="9"]')).not.toBeNull();
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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

  // Состав, привязанный к каталогу: сверка идёт сразу, без формы выбора товара.
  const LINKED = () => CARD().items.map((it, i) => ({ ...it, product_id: 70 + i }));

  it('сверка уходит одним запросом на весь состав', async () => {
    // Приёмщик считает подряд и не должен ждать сети после каждой позиции.
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: true } };
      window.__writes = [];
      api = async () => (${JSON.stringify(CARD({ items: LINKED() }))});
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

  it('переоприходование не прошло (409): текст сервера виден, карточка перечитана', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__cards = 0;
      api = async () => { window.__cards++; return ${JSON.stringify(CARD({ items: LINKED() }))}; };
      tg.showAlert = (text) => { window.__alerted = text; };
      apiResult = async () => ({ ok: false, status: 409,
        error: 'Сверка не сохранена: товар из прежнего прихода уже отгружен. Количества оставлены прежними.',
        body: { ok: false, reverted: true } });
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    window.document.querySelector('.qty-input[data-item="11"]').value = '5';
    window.document.querySelector('#cont-save').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__alerted).toContain('уже отгружен');
    expect(window.__cards).toBe(2);
    expect(window.document.body.textContent).not.toContain('Сверка сохранена');
  });

  it('сверка сохранена, а приход не проведён — это сказано красным, а не «сохранено»', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      api = async () => (${JSON.stringify(CARD({ items: LINKED() }))});
      apiResult = async () => ({ ok: true, status: 200, error: '',
        body: { ok: true, receipt: { ok: false, error: 'Нечего оприходовать' } } });
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    window.document.querySelector('#cont-save').click();
    await new Promise(r => setTimeout(r, 0));
    const toasts = Array.from(window.document.querySelectorAll('.toast')).map(t => t.textContent).join(' | ');
    expect(toasts).toContain('на склад не пошло: Нечего оприходовать');
  });

  it('позиции без карточки: сверка сначала спрашивает, что это за товар', async () => {
    // Сверка сразу проводит приход. Раньше непривязанная позиция молча
    // выпадала из накладной — теперь выбор делается ДО неё и едет тем же запросом.
    const items = CARD().items;
    items[0].catalog_matches = [{ product_id: 5, name: 'Кабель PV 0.6', unit: 'шт' }];
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__writes = [];
      api = async () => (${JSON.stringify(CARD({ items }))});
      apiResult = async (path, body) => { window.__writes.push([path, body]); return { ok: true, status: 200, body: { ok: true }, error: '' }; };
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    const doc = window.document;
    doc.querySelector('#cont-save').click();
    await new Promise(r => setTimeout(r, 0));

    expect(window.__writes).toHaveLength(0);
    const review = doc.querySelector('#receipt-review');
    expect(review).not.toBeNull();
    // Совпадение по названию предвыбрано и названо; без совпадения — выбор обязателен.
    expect(review.querySelector('[data-review="10"]').textContent).toContain('из каталога');
    expect(review.querySelector('[data-review-new="10"]')).toBeNull();
    doc.querySelector('.c-overlay #ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes).toHaveLength(0);
    expect(doc.querySelector('.c-overlay #ms-error').textContent).toContain('ThinkPower 6kw');

    review.querySelector('[data-review-new="11"]').click();
    expect(review.querySelector('[data-review="11"]').textContent).toContain('новый товар');
    doc.querySelector('.c-overlay #ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes).toHaveLength(1);
    expect(window.__writes[0][0]).toBe('/api/containers/check');
    expect(window.__writes[0][1].resolve).toEqual({ 10: { product_id: 5 }, 11: { new: true } });
  });

  it('«Оприходовать» с непривязанными позициями — через ту же форму выбора', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__writes = [];
      api = async () => (${JSON.stringify(CARD())});
      apiResult = async (path, body) => { window.__writes.push([path, body]); return { ok: true, status: 200, body: { ok: true, matched: 2 }, error: '' }; };
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    const doc = window.document;
    doc.querySelector('#cont-supply').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes).toHaveLength(0);
    doc.querySelector('[data-review-new="10"]').click();
    doc.querySelector('[data-review-new="11"]').click();
    doc.querySelector('.c-overlay #ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes[0][0]).toBe('/api/containers/supply');
    expect(window.__writes[0][1].resolve).toEqual({ 10: { new: true }, 11: { new: true } });
    expect(window.__writes[0][1].idempotency_key).toBeTruthy();
  });

  it('прибывший контейнер удаляется, пока открыто окно правки', async () => {
    // Приёмку могли завести не на тот контейнер — запрет означал бы вечную
    // неверную строку в списке.
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: true } };
      api = async () => (${JSON.stringify(CARD())});
      window.__ready = renderContainerCard(3);
    `);
    await window.__ready;
    expect(window.document.querySelector('#cont-del')).not.toBeNull();
  });

  it('после закрытия окна карточка только читается', async () => {
    const closed = { ...CARD(), edit_window: { open: false, hours_left: 0 } };
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
    currentUser = { role: 'boss', prefs: { work_actions: true } };
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

  it('позиция с совпадением по названию в каталоге так и подписана', () => {
    const window = boot();
    const box = window.document.createElement('div');
    box.innerHTML = window.containerItemsHtml([
      { ...ITEMS[0], catalog_matches: [{ product_id: 9, name: 'Штекер тип C' }] },
    ], false, true);
    expect(box.textContent).toContain('в каталоге по названию');
    expect(box.textContent).not.toContain('нет в каталоге');
  });

  const formDriver = (result = "{ ok: true, body: {} }") => `
    currentUser = { role: 'manager' };
    window.__sent = null;
    window.__queries = [];
    api = async (path, body) => { window.__queries.push(body); return { ok: true, products: [
      { product_id: 11, name: 'Кабель PV 0.6', unit: 'м', quantity: 40, sku: 'PV06' },
    ] }; };
    apiResult = async (path, body) => { window.__sent = body; return ${result}; };
    renderContainerCard = async () => {};
    openContainerItemForm(7, false);
  `;

  // Список приходит по debounce'у — ждём его и микротаск ответа.
  const settle = () => new Promise(r => setTimeout(r, 400));

  it('список каталога виден сразу, до первой буквы, с остатком и единицей', async () => {
    const window = boot(formDriver());
    await settle();
    const row = window.document.querySelector('.picker-list [data-product="11"]');
    expect(row).not.toBeNull();
    expect(row.textContent).toContain('остаток 40 м');
    expect(row.textContent).toContain('PV06');
    expect(window.__queries[0]).toEqual({ query: '', browse: true, limit: 50 });
    // Ввод новой позиции — отдельной кнопкой, а не поведением по умолчанию.
    expect(window.document.querySelector('#picker-new-product')).not.toBeNull();
  });

  it('выбор из каталога: количество — и позиция уезжает с карточкой товара', async () => {
    const window = boot(formDriver());
    const doc = window.document;
    await settle();
    doc.querySelector('#ms-submit').click();   // без выбора — не уходит
    await new Promise(r => setTimeout(r, 0));
    expect(doc.querySelector('#ms-error').textContent).toContain('Выберите товар');

    doc.querySelector('.picker-list [data-product="11"]').click();
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(doc.querySelectorAll('.c-overlay')).toHaveLength(1);
    expect(doc.querySelector('#cont-item-product').textContent).toContain('Кабель PV 0.6');
    expect(doc.querySelector('#ms-f-name')).toBeNull();   // название не вписывают

    doc.querySelector('#ms-f-expected_qty').value = '500';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(window.__sent.product_id).toBe(11);
    expect(window.__sent.name).toBe('Кабель PV 0.6');
    expect(window.__sent.unit).toBe('м');
    expect(window.__sent.expected_qty).toBe('500');
  });

  it('новый товар: набранное в поиске уезжает в название, без карточки', async () => {
    const window = boot(formDriver());
    const doc = window.document;
    await settle();
    doc.querySelector('#ms-f-search').value = 'Штекер тип C';
    doc.querySelector('#picker-new-product').click();
    expect(doc.querySelectorAll('.c-overlay')).toHaveLength(1);
    expect(doc.querySelector('#ms-f-name').value).toBe('Штекер тип C');

    doc.querySelector('#ms-f-expected_qty').value = '5';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(window.__sent.name).toBe('Штекер тип C');
    expect(window.__sent.product_id).toBeUndefined();
  });

  it('тёзка в каталоге: сервер предлагает существующую карточку, её выбирают', async () => {
    const conflict = JSON.stringify({
      ok: false, status: 409, error: 'В каталоге уже есть «Кабель PV 0.6» — выберите его',
      body: { needs_choice: true, existing: [{ product_id: 11, name: 'Кабель PV 0.6', unit: 'м' }] },
    });
    const window = boot(formDriver(conflict));
    const doc = window.document;
    await settle();
    doc.querySelector('#ms-f-search').value = 'кабель  pv 0.6';
    doc.querySelector('#picker-new-product').click();
    doc.querySelector('#ms-f-expected_qty').value = '7';
    doc.querySelector('#ms-submit').click();
    await settle();
    expect(doc.querySelector('#ms-error').textContent).toContain('уже есть');

    doc.querySelector('[data-existing="11"]').click();
    expect(doc.querySelectorAll('.c-overlay')).toHaveLength(1);
    expect(doc.querySelector('#cont-item-product').textContent).toContain('Кабель PV 0.6');
    expect(doc.querySelector('#ms-f-expected_qty').value).toBe('7');   // количество не потерялось
  });
});

describe('техника: «Прибыла»', () => {
  const CARD = (over = {}) => ({
    ok: true,
    machine: { id: 9, name: 'CAT 320D', vin: 'CAT123', status: 'in_transit', location: 'Порт' },
    photos: [], hours: [], deals: [],
    next_statuses: [{ status: 'in_stock', label: '✅ Прибыла' }],
    can_manage: false, can_arrive: true,
    status_labels: { in_transit: '🚢 В пути', in_stock: '🏗 На складе' },
    ...over,
  });
  const bootCard = (role, card) => boot(`
    currentUser = { role: '${role}', prefs: { work_actions: true } };
    window.__writes = [];
    api = async () => (${JSON.stringify(card)});
    apiResult = async (path, body) => { window.__writes.push([path, body]); return { ok: true, status: 200, body: { ok: true }, error: '' }; };
    window.__ready = renderMachineCard(9);
  `);

  it('менеджер отмечает прибытие машины в пути — с локацией и ключом', async () => {
    const window = bootCard('manager', CARD());
    await window.__ready;
    const doc = window.document;
    const btn = doc.querySelector('[data-mact="arrive"]');
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain('Прибыла');
    btn.click();
    expect(doc.querySelector('#ms-f-location').value).toBe('Порт');
    doc.querySelector('#ms-f-location').value = 'Склад Сергели';
    doc.querySelector('#ms-submit').click();
    await new Promise(r => setTimeout(r, 0));
    expect(window.__writes[0][0]).toBe('/api/machines/arrive');
    expect(window.__writes[0][1].machine_id).toBe(9);
    expect(window.__writes[0][1].location).toBe('Склад Сергели');
    expect(window.__writes[0][1].idempotency_key).toBeTruthy();
  });

  it('у руководства тот же переход не дублируется второй кнопкой', async () => {
    const window = bootCard('boss', CARD({ can_manage: true }));
    await window.__ready;
    const doc = window.document;
    expect(doc.querySelectorAll('[data-mact="arrive"]')).toHaveLength(1);
    expect(doc.querySelector('[data-mstatus-to="in_stock"]')).toBeNull();
  });

  it('у машины на складе «Прибыла» нет', async () => {
    const window = bootCard('manager', CARD({
      machine: { id: 9, name: 'CAT 320D', vin: 'CAT123', status: 'in_stock' },
      can_arrive: false, next_statuses: [],
    }));
    await window.__ready;
    expect(window.document.querySelector('[data-mact="arrive"]')).toBeNull();
  });
});

describe('пять разделов вместо четырёх', () => {
  const nav = (window) => Array.from(
    window.document.querySelectorAll('#bottom-nav .nav-item[data-screen]')
  ).map(b => b.dataset.screen);

  it('нижняя панель строится под роль: четыре раздела и «Меню» со всеми остальными', () => {
    // Пятый раздел ушёл из панели в шторку — пятый слот занимает «Меню»
    // (см. navBarLayout): вкладки и будущие разделы растут там, а не в ряду.
    const window = boot("currentUser = { role: 'manager' }; buildNav(); openNavDrawer();");
    expect(nav(window)).toEqual(['today', 'sales', 'stock', 'money']);
    expect(window.document.querySelector('#bottom-nav [data-action="menu"]')).not.toBeNull();
    const drawer = Array.from(
      window.document.querySelectorAll('#nav-drawer .nav-link--section'),
    ).map(b => b.dataset.screen);
    expect(drawer).toEqual(['today', 'sales', 'stock', 'money', 'clients']);
    // Выключатель «Рабочие действия» менеджеру не рисуется.
    expect(window.document.querySelector('#nav-drawer [data-work-switch]')).toBeNull();
  });

  it('руководитель: «Сегодня · Решения · Деньги · Продажи · Меню», остальное — в шторке', () => {
    const window = boot("currentUser = { role: 'boss' }; buildNav(); openNavDrawer();");
    expect(nav(window)).toEqual(['today', 'decisions', 'money', 'sales']);
    const drawer = Array.from(
      window.document.querySelectorAll('#nav-drawer .nav-link--section'),
    ).map(b => b.dataset.screen);
    expect(drawer).toEqual(['today', 'decisions', 'money', 'sales', 'stock', 'clients', 'settings']);
    const tabsOf = (sec) => Array.from(
      window.document.querySelectorAll(`#nav-drawer .nav-link--tab[data-screen="${sec}"]`),
    ).map(b => b.dataset.tab);
    expect(tabsOf('money')).toEqual(['debts', 'report']);
    expect(tabsOf('sales')).toEqual(['orders', 'report']);
    expect(tabsOf('stock')).toEqual(['catalog', 'containers', 'machines']);
    expect(tabsOf('clients')).toEqual(['funnel', 'limits']);
    const sw = window.document.querySelector('#nav-drawer [data-work-switch]');
    expect(sw).not.toBeNull();
    expect(sw.getAttribute('aria-checked')).toBe('false');
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
      buildNav();
      api = async () => ({ invoices: [] });
      window.__ready = showScreen('whinvoices');
    `);
    await window.__ready;
    // «Склада» в панели руководителя нет — раздел из «Меню» (data-current).
    expect(window.document.getElementById('bottom-nav').dataset.current).toBe('stock');
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
    expect(boot("currentUser = { role: 'boss', prefs: { work_actions: true } };").stockShellHtml()).not.toContain('data-sect="stale"');
    const boss = boot(`currentUser = { role: 'boss', prefs: { work_actions: true } }; stockData = ${JSON.stringify(STOCK)}; renderStockContent();`);
    expect(boss.document.querySelector('[data-stale]')).not.toBeNull();
    const mgr = boot(`currentUser = { role: 'manager' }; stockData = ${JSON.stringify(STOCK)}; renderStockContent();`);
    expect(mgr.document.querySelector('[data-stale]')).toBeNull();
  });

  const bootStale = (extra = '') => boot(`
    currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
      openContainerEditForm(7, ${JSON.stringify(CARD)});
    `);
    const doc = window.document;
    expect(doc.querySelector('#ms-f-eta_date').value).toBe('2026-08-20');
    expect(doc.querySelector('#ms-f-notes').value).toBe('Запчасти для JCB');
  });

  it('номер не правится — по нему контейнер ищут', () => {
    // Ошиблись номером — это другой контейнер, а не опечатка в этом.
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: true } };
      openContainerEditForm(7, ${JSON.stringify(CARD)});
    `);
    expect(window.document.querySelector('#ms-f-number')).toBeNull();
    expect(window.document.querySelector('.c-sheet').textContent).toContain('MSKU1234567');
  });

  it('правка уходит на сервер и перерисовывает карточку', async () => {
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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
      currentUser = { role: 'boss', prefs: { work_actions: true } };
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

  it('менеджеру кнопку отмены не показывают, когда удаление оставлено руководителю', async () => {
    const window = boot(`
      currentUser = { role: 'manager', delete_requires_boss: true };
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

  it('менеджеру отмена накладной доступна, пока руководитель не оставил удаление себе', async () => {
    const window = boot(`
      currentUser = { role: 'manager', delete_requires_boss: false };
      api = async () => ({ invoices: [
        { id: 5, type: 'incoming', invoice_number: 'IN-2026-0001', invoice_date: '2026-09-11',
          status: 'confirmed', currency: 'USD', total_amount_cents: 75000,
          telegram_sent: 0, counterparty_name: 'Поставщик' },
      ]});
      window.__ready = renderWhInvoiceList();
    `);
    await window.__ready;
    expect(window.document.getElementById('content').querySelector('[data-wh-cancel="5"]')).not.toBeNull();
  });

  it('у накладной из переноса МойСклад кнопки отмены нет даже у босса', async () => {
    // Склад по исторической накладной не двигался — сервер отмену отвергнет
    // (warehouse.historical_invoice_refusal), и кнопка была бы ложным обещанием.
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: true } };
      api = async () => ({ invoices: [
        { id: 7, type: 'outgoing', invoice_number: 'MS-D-00042', invoice_date: '2025-03-11',
          status: 'confirmed', currency: 'UZS', total_amount_cents: 1265000000,
          telegram_sent: 0, counterparty_name: 'ООО Ромашка', historical: true },
        { id: 8, type: 'outgoing', invoice_number: 'OUT-2026-0002', invoice_date: '2026-09-11',
          status: 'confirmed', currency: 'USD', total_amount_cents: 1000,
          telegram_sent: 0, counterparty_name: 'ООО Ромашка', historical: false },
      ]});
      window.__ready = renderWhInvoiceList();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('MS-D-00042');
    expect(content.querySelector('[data-wh-cancel="7"]')).toBeNull();
    expect(content.querySelector('[data-wh-cancel="8"]')).not.toBeNull();
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
    currentUser = { role: 'boss', prefs: { work_actions: true } };
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

describe('клавиатура и прокрутка при смене вида (вёрстка на телефоне)', () => {
  function loaded() {
    const window = makeWindow();
    window.document.body.insertAdjacentHTML('afterbegin', '<input id="kb-field" type="text">');
    window.scrollTo = () => { window.__scrolls = (window.__scrolls || 0) + 1; };
    window.eval(read('helpers.js'));
    window.eval(read('net.js'));
    window.eval(read('app.js'));
    return window;
  }
  const setHeight = (window, h) => {
    Object.defineProperty(window, 'innerHeight', { value: h, configurable: true });
    window.dispatchEvent(new window.Event('resize'));
  };

  it('панель прячется, только когда поле в фокусе И окно сжато клавиатурой', () => {
    // Android WebView сжимает окно под клавиатуру, и fixed-панель садилась на
    // форму «Количество и цена» поверх переключателя валюты.
    const window = loaded();
    const html = window.document.documentElement;
    setHeight(window, 800);
    const field = window.document.getElementById('kb-field');
    field.focus();
    expect(html.classList.contains('kb-open'), 'фокус без клавиатуры панель не прячет').toBe(false);
    setHeight(window, 430);
    expect(html.classList.contains('kb-open')).toBe(true);
    field.blur();
    window.eval('syncKeyboard()');
    expect(html.classList.contains('kb-open'), 'поле ушло из фокуса — панель вернулась').toBe(false);
    setHeight(window, 800);
    expect(html.classList.contains('kb-open')).toBe(false);
  });

  it('новый вид начинается сверху, перерисовка того же — нет', () => {
    // Прокрутка пролистанного списка переезжала на открытую карточку, и её
    // верх оставался под липкой шапкой.
    const window = loaded();
    window.__scrolls = 0;
    window.eval("showBack(() => { stockTab = 'containers'; showScreen('stock'); })");
    expect(window.__scrolls).toBe(1);
    window.eval("showBack(() => { stockTab = 'containers'; showScreen('stock'); })");
    expect(window.__scrolls, 'перерисовка карточки после действия не прыгает в начало').toBe(1);
    window.eval("setSectionTab('money', 'debts')");
    expect(window.__scrolls).toBe(2);
    window.eval("setSectionTab('money', 'debts')");
    expect(window.__scrolls).toBe(2);
  });
});

// ─── Руководитель: «смотреть, решать, контролировать» (решение владельца) ────
//
// Работа менеджера у руководителя — за выключателем «Рабочие действия» в
// «Меню». Выключатель — вид, а не права: хранит сервер (/api/prefs/set),
// ручки не меняются. Удаление — контроль, у руководителя видно всегда.
describe('руководитель без «Рабочих действий»', () => {
  const tick = () => new Promise(r => setTimeout(r, 0));
  const ORDER = (over = {}) => ({
    id: 41, status: 'approved', agent_name: 'ООО Ромашка', full_name: 'Менеджер',
    items_count: 1, created_at: '2026-08-01 10:00', total: 100, currency: 'USD',
    payment_type: 'credit', items: [], ...over,
  });

  it('на заказе одно действие — «Отменить»; включил — вернулись «Отгрузить» и оплата', () => {
    const orders = [ORDER(), ORDER({ id: 42, needs_payment: true, payment_type: 'paid', payment_gap: 100 })];
    const render = (prefs) => boot(`
      currentUser = { role: 'boss', prefs: ${JSON.stringify(prefs)} };
      ordersData = { orders: ${JSON.stringify(orders)}, role: 'boss', pending_count: 2 };
      renderOrdersMain();
    `).document.getElementById('content');
    const off = render({ work_actions: false });
    expect(off.querySelector('.btn-ship-order')).toBeNull();
    expect(off.querySelector('.btn-pay-order')).toBeNull();
    expect(off.querySelectorAll('.btn-cancel-order')).toHaveLength(2);
    expect(off.querySelector('#btn-new-order')).toBeNull();
    const on = render({ work_actions: true });
    expect(on.querySelector('.btn-ship-order')).not.toBeNull();
    expect(on.querySelector('.btn-pay-order')).not.toBeNull();
    // Менеджер — как было.
    const mgr = boot(`
      currentUser = { role: 'manager' };
      ordersData = { orders: ${JSON.stringify(orders.map(o => ({ ...o, is_mine: true })))}, role: 'manager' };
      renderOrdersMain();
    `).document.getElementById('content');
    expect(mgr.querySelector('.btn-ship-order')).not.toBeNull();
    expect(mgr.querySelector('.btn-cancel-order')).toBeNull();
  });

  it('строка «Заявки на рассмотрении» ведёт в «Решения»', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      buildNav();
      window.__shown = [];
      const _show = showScreen;
      showScreen = async (s, o) => { window.__shown.push(s); };
      ordersData = { orders: [], role: 'boss', pending_count: 3 };
      renderOrdersMain();
    `);
    window.document.getElementById('show-requests').click();
    await tick();
    expect(window.__shown).toEqual(['decisions']);
  });

  it('карточка машины — просмотр; удаление — всегда', async () => {
    const CARD = {
      ok: true,
      machine: { id: 7, name: 'JCB 3CX', vin: 'JCB7788', status: 'in_transit', hours: 15200,
                 price_cents: 2500000, cost_cents: 2000000, currency: 'USD' },
      photos: [], hours: [], deals: [],
      next_statuses: [{ status: 'reserved', label: '🔒 Забронировать' }],
      can_manage: true, can_arrive: true, can_upload_photo: true,
      can_request: ['sale', 'credit'],
      status_labels: { in_transit: '🚢 В пути' },
    };
    const card = async (prefs, role = 'boss', over = {}, cardOver = {}) => {
      const window = boot(`
        currentUser = { role: '${role}', prefs: ${JSON.stringify(prefs)}, ...${JSON.stringify(over)} };
        api = async () => (${JSON.stringify({ ...CARD, ...cardOver })});
        window.__ready = renderMachineCard(7);
      `);
      await window.__ready;
      return window.document.getElementById('content');
    };
    const off = await card({});
    for (const act of ['hours', 'edit', 'sale', 'credit', 'arrive']) {
      expect(off.querySelector(`[data-mact="${act}"]`), act).toBeNull();
    }
    expect(off.querySelector('[data-mstatus-to]')).toBeNull();
    expect(off.querySelector('#machine-photo-add')).toBeNull();
    expect(off.querySelector('[data-mact="delete"]')).not.toBeNull();
    expect(off.textContent).toContain('Себестоимость');
    expect(off.textContent).toMatch(/25\s000 USD/);

    const on = await card({ work_actions: true });
    for (const act of ['hours', 'edit', 'sale', 'credit', 'arrive', 'delete']) {
      expect(on.querySelector(`[data-mact="${act}"]`), act).not.toBeNull();
    }
    // Менеджер: удаление гаснет при delete_requires_boss, если сервер его пустит.
    const mgrOpen = await card({}, 'manager', {});
    expect(mgrOpen.querySelector('[data-mact="hours"]')).not.toBeNull();
    const mgrAllowed = await card({}, 'manager', { delete_requires_boss: false });
    expect(mgrAllowed.querySelector('[data-mact="delete"]')).not.toBeNull();   // can_manage в стабе
    const mgrLocked = await card({}, 'manager', { delete_requires_boss: true });
    expect(mgrLocked.querySelector('[data-mact="delete"]')).toBeNull();
    // Сервер сказал «нельзя» (`can_delete: false`) — кнопки нет и у руководителя.
    const bossDenied = await card({}, 'boss', {}, { can_delete: false });
    expect(bossDenied.querySelector('[data-mact="delete"]')).toBeNull();
  });

  it('заявка на сделку в карточке машины: решение — без «Рабочих действий», оформление — за ними', async () => {
    const request = {
      id: 5, machine_id: 7, kind: 'credit', kind_label: 'Рассрочка', status: 'pending',
      status_label: '⏳ Ждёт одобрения', price_cents: 2400000, currency: 'USD',
      buyer_name: 'Азиз', created_by: 1, creator_name: 'Manager',
    };
    const CARD = {
      ok: true,
      machine: { id: 7, name: 'JCB 3CX', vin: 'JCB7788', status: 'in_stock', currency: 'USD' },
      photos: [], hours: [], deals: [], next_statuses: [],
      can_manage: true, can_request: [], can_delete: true,
      request, can_decide: true, viewer_id: 2,
      status_labels: { in_stock: '🏗 На складе' },
    };
    const window = boot(`
      currentUser = { role: 'boss', prefs: {} };
      api = async () => (${JSON.stringify(CARD)});
      window.__ready = renderMachineCard(7);
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-mreq-approve="5"]')).not.toBeNull();
    expect(content.querySelector('[data-mreq-reject="5"]')).not.toBeNull();
    expect(content.querySelector('[data-mreq-rework="5"]')).not.toBeNull();
    expect(content.querySelector('[data-mact="sale"]')).toBeNull();
    // Живая заявка держит машину — удалять её нельзя, кнопку не рисуем.
    expect(content.querySelector('[data-mact="delete"]')).toBeNull();
  });

  it('рассрочка у руководителя без «Рабочих действий» — график без кнопок денег', async () => {
    const deal = {
      id: 11, kind: 'credit', currency: 'USD', price_cents: 1200000, buyer_name: 'Азиз',
      sold_at: '2026-09-01', can_record: true, can_undo: true,
      payments: [
        { id: 1, seq: 0, amount_cents: 200000, due_date: '2026-09-01', paid_at: '2026-09-01', covered_cents: 200000 },
        { id: 2, seq: 1, amount_cents: 500000, due_date: '2026-10-01', paid_at: null, covered_cents: 0 },
        { id: 3, seq: 2, amount_cents: 500000, due_date: '2026-11-01', paid_at: '2026-09-10', covered_cents: 500000 },
      ],
      receipts: [{ id: 21, amount_cents: 500000, received_at: '2026-09-10', method: 'cash' }],
    };
    const view = async (prefs) => {
      const window = boot(`
        currentUser = { role: 'boss', prefs: ${JSON.stringify(prefs)} };
        window.__html = machineDealsHtml([${JSON.stringify(deal)}], '2026-09-15');
      `);
      const box = window.document.createElement('div');
      box.innerHTML = window.__html;
      return box;
    };
    const off = await view({});
    expect(off.textContent).toContain('Платёж 1');
    expect(off.querySelector('[data-receipt-add]')).toBeNull();
    expect(off.querySelector('[data-payment]')).toBeNull();
    expect(off.querySelector('[data-receipt-del]')).toBeNull();
    expect(off.textContent).toContain('наличные');
    const on = await view({ work_actions: true });
    expect(on.querySelector('[data-receipt-add="11"]')).not.toBeNull();
    expect(on.querySelector('[data-payment="2"]')).not.toBeNull();
    expect(on.querySelector('[data-receipt-del="21"]')).not.toBeNull();
  });

  it('карточка контейнера — себестоимость и удаление, без приёмки', async () => {
    const CARD = {
      ok: true,
      container: { id: 3, number: 'MSKU1', status: 'arrived', arrived_at: '2026-08-12' },
      items: [{ id: 10, name: 'Кабель', unit: 'шт', expected_qty: 5, arrived_qty: null, state: 'unchecked' }],
      diff: { total: 1, unchecked: 1, short: 0, extra: 0, mismatch: 0 },
      can_manage: true, edit_window: { open: true, hours_left: 10 },
      receipt: {}, status_labels: { arrived: '📦 Прибыл' },
    };
    const card = async (prefs) => {
      const window = boot(`
        currentUser = { role: 'boss', prefs: ${JSON.stringify(prefs)} };
        window.mountContainerCosting = () => { window.__costing = 1; };
        api = async () => (${JSON.stringify(CARD)});
        window.__ready = renderContainerCard(3);
      `);
      await window.__ready;
      return window;
    };
    const off = await card({});
    const c = off.document.getElementById('content');
    for (const id of ['#cont-edit', '#cont-supplier', '#cont-item-add', '#cont-save', '#cont-supply', '#cont-post']) {
      expect(c.querySelector(id), id).toBeNull();
    }
    expect(c.querySelector('.qty-input')).toBeNull();
    expect(c.querySelector('[data-item-del]')).toBeNull();
    expect(c.querySelector('#cont-del')).not.toBeNull();
    expect(off.__costing).toBe(1);
    const on = (await card({ work_actions: true })).document.getElementById('content');
    for (const id of ['#cont-edit', '#cont-item-add', '#cont-save', '#cont-supply', '#cont-post', '#cont-del']) {
      expect(on.querySelector(id), id).not.toBeNull();
    }
  });

  it('каталог: «Залежалось» есть, «Собрать пост» и «Пост в канал» — нет', async () => {
    const STOCK = { categories: [], products: [
      { product_id: 1, name: 'Кабель', unit: 'м', stock: 480, available: 480, folder_id: '' },
    ] };
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => ({ ok: true, days: 60, items: [{ name: 'Кабель', stock: 480, unit: 'м' }] });
      apiResult = async () => ({ ok: true, body: { photos: [] } });
      stockData = ${JSON.stringify(STOCK)};
      renderStockContent();
    `);
    const doc = window.document;
    doc.querySelector('[data-stale]').click();
    await tick(); await tick();
    expect(doc.querySelector('.stale-check')).toBeNull();
    expect(doc.querySelector('#stale-post')).toBeNull();
    // Строка открывает цену — это контроль руководителя.
    doc.querySelector('[data-price-idx]').click();
    expect(doc.querySelector('#pe-save')).not.toBeNull();
    expect(doc.querySelector('#pe-post')).toBeNull();
    expect(doc.querySelector('#pe-photo-add')).toBeNull();
  });

  it('«Долги» — просмотр: без «Внести оплату» и без подтверждения', async () => {
    const DEBTS = {
      role: 'boss', scope: 'company', today: '2026-08-01', can_confirm: true,
      debts: [
        { id: 5, state: 'overdue', agent_name: 'А', full_name: 'М', total: 100, remaining: 100,
          currency: 'USD', due_date: '2026-07-01', items_count: 1 },
        { id: 6, state: 'awaiting_confirmation', agent_name: 'Б', full_name: 'М', total: 50,
          pending: 50, pending_confirmable: 50, claimable: 10, currency: 'USD', items_count: 1,
          parts: [{ method: 'card', amount_cents: 5000, currency: 'USD', state: 'awaiting_bank' },
                  { method: 'cash', amount_cents: 1000, currency: 'USD', state: 'on_hand' }] },
      ],
    };
    const render = async (prefs) => {
      const window = boot(`
        currentUser = { role: 'boss', prefs: ${JSON.stringify(prefs)} };
        api = async () => (${JSON.stringify(DEBTS)});
        window.__ready = renderDebts(document.getElementById('content'));
      `);
      await window.__ready;
      return window.document.getElementById('content');
    };
    const off = await render({});
    expect(off.querySelector('.btn-pay-debt')).toBeNull();
    expect(off.querySelector('.btn-confirm-pay')).toBeNull();
    // Разбивка «наличные у менеджера / карта» — на месте.
    expect(off.textContent).toContain('у менеджера');
    expect(off.textContent).toContain('на карту');
    const on = await render({ work_actions: true });
    expect(on.querySelector('.btn-pay-debt')).not.toBeNull();
    expect(on.querySelector('.btn-confirm-pay')).not.toBeNull();
  });

  it('возврат без приёмки: «Товар получен» — работа склада, руководитель ждёт', () => {
    const html = (prefs) => boot(`currentUser = { role: 'boss', prefs: ${JSON.stringify(prefs)} };`)
      .returnCardsHtml([{ id: 9, order_id: 5, total_amount: 20, reason: 'брак', goods_received: 0 }],
        { role: 'boss', isBoss: true, confirmersExist: true });
    expect(html({})).not.toContain('ret-goods');
    expect(html({})).toContain('Ждёт отметки склада');
    expect(html({ work_actions: true })).toContain('ret-goods');
  });

  it('«Воронка» без переходов в карточку лида', async () => {
    const FUNNEL = {
      ok: true, funnel: { contacted: 3, replied: 1, won: 0 },
      awaiting: [{ id: 3, display_name: 'Азиз', last_inbound_at: '2026-08-01 18:40:00' }], by_manager: [],
    };
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(FUNNEL)});
      clientsTab = 'funnel';
      window.__ready = renderClientsScreen();
    `);
    await window.__ready;
    const c = window.document.getElementById('content');
    expect(c.textContent).toContain('Азиз');
    expect(c.querySelector('[data-lead]')).toBeNull();
    expect(c.querySelector('[data-sect="list"]')).toBeNull();
    expect(c.querySelector('[data-sect="channel"]')).toBeNull();
  });
});

describe('экран «Решения»', () => {
  const tick = () => new Promise(r => setTimeout(r, 0));
  const LISTS = {
    '/api/orders/requests': { requests: [{
      id: 7, full_name: 'Менеджер', agent_name: 'ООО Ромашка', created_at: '2026-08-01',
      payment_type: 'credit', due_date: '2026-09-01', total: 120, currency: 'USD',
      items: [{ name: 'Кабель', quantity: 2, unit: 'шт', price: 60 }],
    }] },
    '/api/payments/pending': { pending: [{ order_id: 11, agent_name: 'Б', total: 50, pending: 50,
      confirmable: 50, currency: 'USD', parts: [] }] },
    '/api/deposits/pending': { deposits: [{ id: 3, amount: 10, orders: [] }] },
    '/api/returns/pending': { returns: [{ id: 9, order_id: 5, total_amount: 20, reason: 'брак', goods_received: 1 }] },
  };
  const bootDecisions = (extra = '') => boot(`
    currentUser = { role: 'boss' };
    buildNav();
    window.__calls = [];
    window.__lists = ${JSON.stringify(LISTS)};
    api = async (p, body) => {
      window.__calls.push([p, body]);
      if (window.__lists[p]) return window.__lists[p];
      return { ok: true };
    };
    tg.showConfirm = (t, cb) => cb(true);
    tg.showAlert = () => {};
    ${extra}
    window.__ready = showScreen('decisions');
  `);

  it('все виды решений — одним экраном, с общим бейджем в панели', async () => {
    const window = bootDecisions();
    await window.__ready;
    const doc = window.document;
    const groups = Array.from(doc.querySelectorAll('[data-decision-group]')).map(g => g.dataset.decisionGroup);
    expect(groups).toEqual(['requests', 'payments', 'deposits', 'returns']);
    expect(doc.querySelector('.btn-approve[data-req="7"]')).not.toBeNull();
    expect(doc.querySelector('.pay-confirm[data-id="11"]')).not.toBeNull();
    expect(doc.querySelector('.debt-card[data-dep="3"] .dep-confirm')).not.toBeNull();
    expect(doc.querySelector('.debt-card[data-ret="9"] .ret-confirm')).not.toBeNull();
    const badge = doc.querySelector('#bottom-nav .nav-item[data-screen="decisions"] [data-decisions-badge]');
    expect(badge.hidden).toBe(false);
    expect(badge.textContent).toBe('4');
    expect(doc.getElementById('bottom-nav').dataset.current).toBe('decisions');
  });

  it('решение уходит в ручку и экран перечитывается', async () => {
    const window = bootDecisions();
    await window.__ready;
    window.__calls.length = 0;
    window.document.querySelector('.debt-card[data-dep="3"] .dep-confirm').click();
    await tick(); await tick();
    const paths = window.__calls.map(c => c[0]);
    expect(paths[0]).toBe('/api/deposits/confirm');
    expect(paths).toContain('/api/orders/requests');   // перерисовка «Решений», не «Денег»
  });

  it('пусто — «Решений не ждёт», бейдж гаснет', async () => {
    const window = bootDecisions(`
      window.__lists = { '/api/orders/requests': { requests: [] }, '/api/payments/pending': { pending: [] },
        '/api/deposits/pending': { deposits: [] }, '/api/returns/pending': { returns: [] } };
      setDecisionsBadge(5);
    `);
    await window.__ready;
    expect(window.document.getElementById('content').textContent).toContain('Решений не ждёт');
    expect(window.document.querySelector('[data-decisions-badge]').hidden).toBe(true);
  });

  it('новый вид решения подключается провайдером без правки экрана', async () => {
    const window = bootDecisions(`
      registerDecisionGroup({
        key: 'demo', title: 'Новый вид', icon: 'truck',
        load: async () => [{ id: 1 }, { id: 2 }],
        html: (items) => items.map(i => '<div class="md" data-md="' + i.id + '">x</div>').join(''),
        wire: (root, items, ctx) => { window.__wired = root.querySelectorAll('.md').length; },
      }, 'payments');
    `);
    await window.__ready;
    const groups = Array.from(window.document.querySelectorAll('[data-decision-group]')).map(g => g.dataset.decisionGroup);
    expect(groups).toEqual(['requests', 'demo', 'payments', 'deposits', 'returns']);
    expect(window.__wired).toBe(2);
    expect(window.document.querySelector('[data-decisions-badge]').textContent).toBe('6');
  });

  it('сделки по технике — группой в «Решениях»: условия, кнопки решения, общий бейдж', async () => {
    const window = bootDecisions(`
      window.__lists['/api/machines/deals/pending'] = {
        ok: true, can_decide: true, decide_hint: null, viewer_id: 2, my_rework: [],
        requests: [{
          id: 31, machine_id: 7, machine_name: 'Hitachi ZX200', kind: 'credit', kind_label: 'Рассрочка',
          status: 'pending', status_label: '⏳ Ждёт одобрения', price_cents: 2400000,
          list_price_cents: 2500000, discount_pct: 4, currency: 'USD',
          buyer_name: '<b>Азиз</b>', buyer_passport: 'AA1234567', created_by: 1, creator_name: 'Manager',
          schedule_preview: { down_payment_cents: 0, months: 6, monthly_cents: 400000,
                              first_due: '2026-10-15', last_due: '2027-03-15' },
        }],
      };
      window.__writes = [];
      apiResult = async (p, body) => { window.__writes.push([p, body]); return { ok: true, status: 200, body: { ok: true }, error: '' }; };
    `);
    await window.__ready;
    const doc = window.document;
    const groups = Array.from(doc.querySelectorAll('[data-decision-group]')).map(g => g.dataset.decisionGroup);
    expect(groups).toEqual(['requests', 'machine_deals', 'payments', 'deposits', 'returns']);
    const box = doc.querySelector('[data-decision-group="machine_deals"]');
    expect(box.textContent).toContain('Сделки по технике (1)');
    expect(box.textContent).toContain('Hitachi ZX200');
    expect(box.textContent).toContain('скидка 4%');
    expect(box.textContent).toContain('AA1234567');
    expect(box.querySelector('b')).toBeNull();                       // ввод менеджера экранирован
    expect(box.querySelector('[data-mreq-rework="31"]')).not.toBeNull();
    expect(box.querySelector('[data-mreq-reject="31"]')).not.toBeNull();
    expect(doc.querySelector('[data-decisions-badge]').textContent).toBe('5');
    window.__calls.length = 0;
    box.querySelector('[data-mreq-approve="31"]').click();
    await tick(); await tick(); await tick();
    expect(window.__writes[0][0]).toBe('/api/machines/deals/approve');
    expect(window.__writes[0][1]).toMatchObject({ request_id: 31 });
    expect(window.__writes[0][1].idempotency_key).toBeTruthy();
    // После решения перечитываются «Решения», а не «Склад».
    expect(window.__calls.map(c => c[0])).toContain('/api/machines/deals/pending');
    expect(window.__calls.map(c => c[0])).not.toContain('/api/machines/list');
  });

  it('сбой одной группы — ошибка в её секции, остальные на месте', async () => {
    const window = bootDecisions(`
      const _api = api;
      api = async (p, b) => { if (p === '/api/returns/pending') throw new Error('Сломалось'); return _api(p, b); };
    `);
    await window.__ready;
    const doc = window.document;
    expect(doc.querySelector('[data-decision-group="returns"] .error-card')).not.toBeNull();
    expect(doc.querySelector('.btn-approve[data-req="7"]')).not.toBeNull();
  });

  it('старые адреса подтверждений у руководителя открывают «Решения», у менеджера — «Подтвердить»', async () => {
    const go = (role, screen, tab) => {
      const window = boot(`
        currentUser = { role: '${role}' };
        buildNav();
        renderDecisionsScreen = async () => {}; renderMoneyScreen = async () => {};
        renderSalesScreen = async () => {};
        window.__ready = showScreen('${screen}'${tab ? `, { tab: '${tab}' }` : ''}).then(() => [currentScreen, moneyTab]);
      `);
      return window.__ready;
    };
    expect(await go('boss', 'money', 'confirm')).toEqual(['decisions', 'confirm']);
    expect((await go('boss', 'requests'))[0]).toBe('decisions');
    expect(await go('manager', 'decisions')).toEqual(['money', 'confirm']);
  });
});

describe('выключатель «Рабочие действия» и «Настройки»', () => {
  const tick = () => new Promise(r => setTimeout(r, 0));

  it('переключение из шторки — запрос на сервер, вкладки в шторке сразу меняются', async () => {
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: false } };
      buildNav();
      window.__calls = [];
      api = async (p, body) => { window.__calls.push([p, body]); return { ok: true, prefs: { work_actions: body.value } }; };
      toast = () => {};
      openNavDrawer();
    `);
    const doc = window.document;
    expect(doc.querySelector('#nav-drawer [data-tab="invoices"]')).toBeNull();
    doc.querySelector('#nav-drawer [data-work-switch]').click();
    await tick(); await tick();
    expect(window.__calls[0]).toEqual(['/api/prefs/set', { key: 'work_actions', value: true }]);
    expect(window.eval('workActionsVisible()')).toBe(true);
    expect(doc.querySelector('#nav-drawer [data-work-switch]').getAttribute('aria-checked')).toBe('true');
    expect(doc.querySelector('#nav-drawer [data-tab="invoices"]')).not.toBeNull();
    expect(doc.querySelector('#nav-drawer [data-tab="channel"]')).not.toBeNull();
  });

  it('отказ сервера возвращает бегунок на место', async () => {
    const window = boot(`
      currentUser = { role: 'boss', prefs: { work_actions: false } };
      buildNav();
      api = async () => { throw new Error('Нет связи'); };
      window.__toasts = [];
      toast = (m) => window.__toasts.push(m);
      openNavDrawer();
    `);
    const doc = window.document;
    doc.querySelector('#nav-drawer [data-work-switch]').click();
    await tick(); await tick();
    expect(doc.querySelector('#nav-drawer [data-work-switch]').getAttribute('aria-checked')).toBe('false');
    expect(window.eval('workActionsVisible()')).toBe(false);
    expect(window.__toasts).toContain('Нет связи');
  });

  it('«Настройки»: выключатель, реквизиты, курсы, сотрудники', async () => {
    const window = boot(`
      currentUser = { role: 'boss', version: 'abc' };
      buildNav();
      api = async (p) => (p === '/api/docs/types'
        ? { company: { company_name: 'Импекс', company_city: 'Ташкент' }, can_edit_company: true, company_fields: [] }
        : {});
      window.__ready = showScreen('settings');
    `);
    await window.__ready;
    const c = window.document.getElementById('content');
    expect(c.querySelector('[data-work-switch]')).not.toBeNull();
    expect(c.querySelector('#set-company').textContent).toContain('Импекс');
    expect(c.querySelector('#set-rates')).not.toBeNull();
    expect(c.textContent).toContain('Сотрудники и роли');
    // Удаление — выключателем рядом с «Рабочими действиями»; поля нет — выключено.
    expect(c.querySelector('[data-delete-switch]').getAttribute('aria-checked')).toBe('false');
  });

  it('«Настройки»: «Удаление — только руководитель» — запрос на сервер, флаг в currentUser', async () => {
    const window = boot(`
      currentUser = { role: 'boss', delete_requires_boss: false };
      window.__me = () => currentUser;
      buildNav();
      window.__calls = [];
      api = async (p, body) => {
        window.__calls.push([p, body]);
        if (p === '/api/settings/delete_requires_boss') return { ok: true, delete_requires_boss: body.enabled };
        return {};
      };
      window.__toasts = [];
      toast = (m) => window.__toasts.push(m);
      window.__ready = showScreen('settings');
    `);
    await window.__ready;
    const doc = window.document;
    const sw = doc.querySelector('#content [data-delete-switch]');
    expect(sw.textContent).toContain('Удаление — только руководитель');
    sw.click();
    await tick(); await tick(); await tick();
    expect(window.__calls).toContainEqual(['/api/settings/delete_requires_boss', { enabled: true }]);
    expect(window.__me().delete_requires_boss).toBe(true);
    expect(doc.querySelector('#content [data-delete-switch]').getAttribute('aria-checked')).toBe('true');
    expect(window.__toasts).toContain('Удаление — только руководитель');
    // Руководителю удаление видно всегда, менеджеру — уже нет.
    expect(window.eval('deleteActionsVisible()')).toBe(true);
    expect(window.eval('deleteActionsOn')('manager', window.__me())).toBe(false);
  });

  it('«Настройки»: отказ сервера возвращает выключатель удаления на место', async () => {
    const window = boot(`
      currentUser = { role: 'boss', delete_requires_boss: true };
      window.__me = () => currentUser;
      buildNav();
      api = async (p) => { if (p === '/api/settings/delete_requires_boss') throw new Error('Нет связи'); return {}; };
      window.__toasts = [];
      toast = (m) => window.__toasts.push(m);
      window.__ready = showScreen('settings');
    `);
    await window.__ready;
    const doc = window.document;
    expect(doc.querySelector('#content [data-delete-switch]').getAttribute('aria-checked')).toBe('true');
    doc.querySelector('#content [data-delete-switch]').click();
    await tick(); await tick();
    expect(doc.querySelector('#content [data-delete-switch]').getAttribute('aria-checked')).toBe('true');
    expect(window.__me().delete_requires_boss).toBe(true);
    expect(window.__toasts).toContain('Нет связи');
  });

  it('deep link: ?startapp=decisions открывает «Решения», чужой адрес — нет', () => {
    const at = (role, url, startParam) => {
      const window = boot(`currentUser = { role: '${role}' };`);
      window.Telegram.WebApp.initDataUnsafe = startParam ? { start_param: startParam } : {};
      window.history.replaceState(null, '', url);
      return window.eval('launchScreen()');
    };
    expect(at('boss', '/?startapp=decisions')).toBe('decisions');
    expect(at('boss', '/', 'decisions')).toBe('decisions');
    expect(at('admin', '/#decisions')).toBe('decisions');
    expect(at('boss', '/?startapp=requests')).toBe('requests');
    expect(at('boss', '/?startapp=nope')).toBe('');
    expect(at('boss', '/?startapp=<script>')).toBe('');
    // Менеджеру «Решения» ведут в его «Подтвердить» — раздел «Деньги» у него есть.
    expect(at('manager', '/?startapp=decisions')).toBe('decisions');
    expect(at('warehouse_keeper', '/?startapp=stock')).toBe('');
  });
});
