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
  const dom = new JSDOM('<!DOCTYPE html><body><div id="content"></div></body>', {
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

describe('под-вкладки Заказы/Каталог переживают ре-рендер (UI-BUG-04)', () => {
  // Регресс: шелл вставлялся поверх готового DOM через insertAdjacentHTML, и
  // любой полный ре-рендер внутри вкладки (смена статуса, выбор периода,
  // ошибка сети) переписывал innerHTML и уносил его вместе с обработчиками —
  // пользователь не мог уйти в Каталог, не выходя из раздела.
  it('шелл входит в шаблон, а не накладывается сверху', () => {
    const window = boot();
    const html = window.ordersShellHtml();
    expect(html).toContain('data-sub="orders"');
    expect(html).toContain('data-sub="stock"');
  });

  it('после полного ре-рендера списка заказов вкладки на месте', () => {
    const window = boot('ordersData = { orders: [], role: "manager" }; renderOrdersMain();');
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-sub="stock"]')).not.toBeNull();
    expect(content.querySelector('[data-sub="orders"]')).not.toBeNull();
  });

  it('вкладки остаются даже когда экран показывает ошибку сети', () => {
    const window = boot();
    const content = window.document.getElementById('content');
    content.innerHTML = window.ordersShellHtml() + window.errorBox('Нет подключения к интернету');
    expect(content.querySelector('[data-sub="orders"]')).not.toBeNull();
    expect(content.textContent).toContain('Нет подключения');
  });
});

describe('под-вкладка «Техника»', () => {
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
    ordersSubTab = 'machines';
    window.__ready = renderOrdersScreen();
  `;

  it('вкладка есть у менеджера и выше', () => {
    const window = boot("currentUser = { role: 'manager' };");
    expect(window.ordersShellHtml()).toContain('data-sub="machines"');
  });

  it('роли без доступа к ручке вкладку не видят', () => {
    // Ручки /api/machines/* отвечают 403 бухгалтеру и кладовщику — вкладка,
    // которая гарантированно упадёт, только сбивает с толку.
    const window = boot("currentUser = { role: 'bookkeeper' };");
    expect(window.ordersShellHtml()).not.toContain('data-sub="machines"');
  });

  it('и не могут в неё попасть в обход переключателя', async () => {
    const window = boot(`
      currentUser = { role: 'bookkeeper' };
      window.__calls = [];
      api = async (p) => { window.__calls.push(p); return { ok: true, orders: [], role: 'bookkeeper' }; };
      ordersSubTab = 'machines';
      window.__ready = renderOrdersScreen();
    `);
    await window.__ready;
    expect(window.__calls.some(p => String(p).includes('/api/machines/'))).toBe(false);
  });

  it('список рисуется, а вкладки остаются на месте (UI-BUG-04)', async () => {
    const window = boot(driver());
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-sub="machines"]')).not.toBeNull();
    expect(content.querySelector('[data-sub="orders"]')).not.toBeNull();
    expect(content.textContent).toContain('JCB 3CX');
    expect(content.querySelector('[data-machine="7"]').dataset.status).toBe('in_stock');
  });

  it('вкладки остаются и когда экран показывает ошибку сети', async () => {
    const window = boot(`
      currentUser = { role: 'boss' };
      api = async () => { throw new Error('Нет подключения к интернету'); };
      ordersSubTab = 'machines';
      window.__ready = renderOrdersScreen();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-sub="machines"]')).not.toBeNull();
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

  it('форма сделки требует срок оплаты только для рассрочки', async () => {
    const window = boot7('boss', []);
    await window.__ready;
    window.document.querySelector('[data-mact="sale"]').click();
    expect(window.document.querySelector('#ms-f-due_date')).toBeNull();
    window.document.querySelector('#ms-cancel').click();

    window.document.querySelector('[data-mact="credit"]').click();
    expect(window.document.querySelector('#ms-f-due_date')).not.toBeNull();
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
      _machinePhotoUrls = ['blob:a', 'blob:b'];
      revokeMachinePhotoUrls();
      window.__left = _machinePhotoUrls.length;
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
