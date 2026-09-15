// Аудит фронта: истёкшая сессия, таймауты и «нет связи», ошибки загрузки
// вместо пустоты, отметка оплаты, форма накладной, страницы заказов, тосты.
// Каркас как в app-load.smoke: helpers.js + net.js + app.js в одном окне,
// драйвер — в том же eval (функции объявлены на верхнем уровне скрипта).
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

function makeWindow(tgOver = {}) {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="content"></div>'
    + '<nav class="bottom-nav" id="bottom-nav"></nav></body>', {
    url: 'https://example.org/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const noop = () => {};
  window.__alerts = [];
  window.__closed = 0;
  window.Telegram = {
    WebApp: {
      ready: noop, expand: noop, onEvent: noop, colorScheme: 'light', themeParams: {},
      initData: 'tgWebAppData=stub', initDataUnsafe: { user: { id: 42 } },
      HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
      showAlert: (m) => window.__alerts.push(String(m)),
      showConfirm: (m, cb) => { window.__alerts.push('confirm:' + m); cb(true); },
      close: () => { window.__closed += 1; },
      setHeaderColor: noop, setBackgroundColor: noop,
      enableClosingConfirmation: noop, disableClosingConfirmation: noop,
      MainButton: { show: noop, hide: noop, setText: noop, onClick: noop, offClick: noop },
      BackButton: { show: noop, hide: noop, onClick: noop, offClick: noop },
      ...tgOver,
    },
  };
  window.fetch = () => new Promise(() => {});
  return window;
}

function boot(driver = '', tgOver) {
  const window = makeWindow(tgOver);
  window.eval(read('helpers.js'));
  window.eval(read('net.js'));
  window.eval(`${read('app.js')}\n${driver}`);
  return window;
}

const flush = () => new Promise(r => setTimeout(r, 0));
const toastsOf = (window) => [...window.document.querySelectorAll('.toast-msg')].map(e => e.textContent);

// ─── 1. Сессия ───────────────────────────────────────────────────────────────

describe('истёкшая сессия (401)', () => {
  it('посреди работы: экран поверх всего, кнопка закрывает приложение', async () => {
    const window = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      window.fetch = async () => ({ ok: false, status: 401, json: async () => ({ detail: 'x' }) });
      window.__ready = api('/api/debts', {}).catch(e => { window.__err = e; });
    `);
    await window.__ready;
    const ov = window.document.querySelector('.session-expired');
    expect(ov).not.toBeNull();
    expect(ov.textContent).toContain('Сессия истекла');
    expect(ov.textContent).not.toContain('Invalid');
    expect(window.__err.sessionExpired).toBe(true);
    ov.querySelector('#session-close').click();
    expect(window.__closed).toBe(1);
  });

  it('несколько запросов разом — один экран, а не стопка', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 42 };
      window.fetch = async () => ({ ok: false, status: 401, json: async () => ({}) });
      window.__ready = Promise.allSettled([api('/a', {}), apiResult('/b', {}), api('/c', {})]);
    `);
    await window.__ready;
    expect(window.document.querySelectorAll('.session-expired')).toHaveLength(1);
  });

  it('401 на входе (/api/me) — «Сессия истекла» в экране, а не «Ошибка авторизации»', async () => {
    const window = makeWindow();
    window.fetch = async () => ({ ok: false, status: 401, json: async () => ({ detail: 'Сессия истекла' }) });
    window.eval(read('helpers.js'));
    window.eval(read('net.js'));
    window.eval(read('app.js'));
    await flush(); await flush();
    const text = window.document.getElementById('content').textContent;
    expect(text).toContain('Сессия истекла');
    expect(text).toContain('Telegram');
  });
});

// ─── 2. Сеть: один слой, таймаут, понятная ошибка ───────────────────────────

describe('сетевой слой', () => {
  it('в app.js нет прямых fetch в обход слоя (кроме самого слоя)', () => {
    const src = read('app.js');
    const uses = src.split('\n').filter(l => /\bfetch\(/.test(l) && !/^\s*\/\//.test(l));
    expect(uses).toEqual(['  fetch: (path, init) => fetch(path, init),'.trim()].map(x => expect.stringContaining(x)));
  });

  it('index.html подключает net.js между helpers.js и app.js', () => {
    const html = read('index.html');
    const h = html.indexOf('/static/helpers.js');
    const n = html.indexOf('/static/net.js');
    const a = html.indexOf('/static/app.js');
    expect(h).toBeGreaterThan(-1);
    expect(n).toBeGreaterThan(h);
    expect(a).toBeGreaterThan(n);
  });

  it('обрыв сети в «Складе» — «Нет подключения» с «Повторить», а не голый TypeError', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      window.fetch = async () => { throw new TypeError('Failed to fetch'); };
      window.__ready = renderStock();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).toContain('Нет подключения');
    expect(content.textContent).toContain('Повторить');
    expect(content.textContent).not.toContain('Failed to fetch');
  });
});

// ─── 3. Ошибка загрузки ≠ «пусто» ────────────────────────────────────────────

describe('«Деньги → Подтвердить»', () => {
  it('без сети — ошибка с «Повторить», а не «Нет записей на подтверждении»', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      api = async () => { throw new Error('Нет связи — проверьте интернет и повторите'); };
      window.__ready = renderCashbox(document.getElementById('content'), 'confirm');
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).not.toContain('Нет записей');
    expect(content.textContent).toContain('Нет подключения');
    expect(content.querySelector('[data-cash-retry]')).not.toBeNull();
  });

  it('роль запрашивает только свои списки: кладовщик — возвраты, бухгалтер — сдачи', async () => {
    const run = async (r) => {
      const window = boot(`
        currentUser = { role: ${JSON.stringify(r)}, user_id: 1 };
        window.__calls = [];
        api = async (p) => { window.__calls.push(p); return { deposits: [], returns: [], pending: [] }; };
        window.__ready = renderCashbox(document.getElementById('content'), 'confirm');
      `);
      await window.__ready;
      return window.__calls.sort();
    };
    expect(await run('warehouse_keeper')).toEqual(['/api/returns/pending']);
    // Бухгалтер сверяет и карту/перечисление по заказам (services.order_payments).
    expect(await run('bookkeeper')).toEqual(['/api/deposits/pending', '/api/payments/pending']);
    expect(await run('boss')).toEqual(['/api/deposits/pending', '/api/payments/pending', '/api/returns/pending']);
  });

  it('вкладка «Подтвердить» не грузит списки дважды (счётчик берётся из тела)', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      currentScreen = 'money';
      moneyTab = 'confirm';
      window.__calls = [];
      api = async (p) => {
        window.__calls.push(p);
        if (p === '/api/deposits/pending') return { deposits: [{ id: 3, amount: 10, orders: [] }] };
        return { deposits: [], returns: [], pending: [] };
      };
      window.__ready = renderMoneyScreen();
    `);
    await window.__ready;
    await flush();
    const n = (p) => window.__calls.filter(c => c === p).length;
    expect(n('/api/deposits/pending')).toBe(1);
    expect(n('/api/returns/pending')).toBe(1);
    expect(n('/api/payments/pending')).toBe(1);
  });

  it('кладовщику «Подтвердить возврат» не рисуется — ручка ответила бы 403', async () => {
    const window = boot(`
      currentUser = { role: 'warehouse_keeper', user_id: 1 };
      api = async () => ({ returns: [{ id: 9, order_id: 5, total_amount: 20, reason: 'брак', goods_received: 0 }] });
      window.__ready = renderCashbox(document.getElementById('content'), 'confirm');
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('.ret-goods')).not.toBeNull();
    expect(content.querySelector('.ret-confirm')).toBeNull();
    expect(content.textContent).toContain('подтверждает руководитель');
  });
});

describe('«Деньги → Отчёт»: сбой ленты не притворяется «движений нет»', () => {
  it('лента упала — ошибка с «Повторить», итоги на месте', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      api = async (p) => {
        if (p === '/api/money/summary') return { payments: [], deposits: { total_cents: 0, count: 0 }, period: { label: 'Месяц' } };
        if (p === '/api/cash/history') throw new Error('Нет связи — проверьте интернет и повторите');
        return null;
      };
      window.__ready = renderMoneyReport(document.getElementById('content'));
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.textContent).not.toContain('Движений пока нет');
    expect(content.querySelector('[data-history-retry]')).not.toBeNull();
    expect(content.textContent).toContain('Поступления');
  });
});

describe('«Сегодня» кладовщика: очередь без сети — ошибка, а не «дел нет»', () => {
  it('рисует ошибку', async () => {
    const window = boot(`
      currentUser = { role: 'warehouse_keeper', user_id: 1 };
      api = async () => { throw new Error('Нет связи — проверьте интернет и повторите'); };
      window.__ready = renderHome();
    `);
    await window.__ready;
    const content = window.document.getElementById('content');
    expect(content.querySelector('.error-card')).not.toBeNull();
  });
});

// ─── 4. Отметка оплаты долга ────────────────────────────────────────────────

// Отметка оплаты долга суммой без способа удалена: оплата вносится разбивкой
// «как получены деньги» — тесты в payments.test.js.

// ─── 6. Форма накладной ─────────────────────────────────────────────────────

describe('накладная: товар не подставляется сам, итог считается на вводе', () => {
  const PRODUCTS = [
    { product_id: 1, name: 'Болт М8', unit: 'шт', quantity: 100 },
    { product_id: 2, name: 'Гайка М8', unit: 'шт', quantity: 100 },
  ];
  const bootForm = (draft = null) => boot(`
    currentUser = { role: 'boss', user_id: 42 };
    ${draft ? `whDraft = ${JSON.stringify(draft)};` : ''}
    api = async (p) => p === '/api/wh/stock' ? { products: ${JSON.stringify(PRODUCTS)} } : { counterparties: [] };
    window.__draft = () => whDraft;
    window.__ready = renderWhInvoiceNew();
  `);

  it('новая строка — без товара, сразу открывается выбор, «Сохранить» неактивна', async () => {
    const window = bootForm({ type: 'incoming', counterparty_id: '', items: [], comment: '' });
    await window.__ready;
    const doc = window.document;
    doc.getElementById('wh-add').click();
    expect(window.__draft().items[0].product_id).toBeNull();
    expect(doc.querySelector('.c-overlay .picker-list')).not.toBeNull();
    expect(doc.querySelector('[data-pick-product]').textContent).toContain('Выберите товар');
    expect(doc.getElementById('wh-save').disabled).toBe(true);
  });

  it('итог пересчитывается на каждый ввод (input), не дожидаясь ухода с поля', async () => {
    const window = bootForm({ type: 'outgoing', counterparty_id: '5', comment: '',
      items: [{ product_id: 1, quantity: 1, price_cents: 0 }] });
    await window.__ready;
    const doc = window.document;
    const price = doc.querySelector('[data-f="price"]');
    price.value = '12,5';
    price.dispatchEvent(new window.Event('input', { bubbles: true }));
    expect(doc.querySelector('.wh-total-sum').textContent.replace(/\s/g, ' ')).toContain('12,50');
    // Поле не перерисовано — фокус и курсор остались у человека.
    expect(doc.querySelector('[data-f="price"]')).toBe(price);
    const qty = doc.querySelector('[data-f="quantity"]');
    qty.value = '3';
    qty.dispatchEvent(new window.Event('input', { bubbles: true }));
    expect(doc.querySelector('.wh-total-sum').textContent.replace(/\s/g, ' ')).toContain('37,50');
    expect(doc.getElementById('wh-save').disabled).toBe(false);
  });

  it('черновик переживает закрытие: сохраняется в localStorage и возвращается', async () => {
    const first = bootForm({ type: 'incoming', counterparty_id: '', comment: 'довоз',
      items: [{ product_id: 2, quantity: 4, price_cents: null }] });
    await first.__ready;
    const qty = first.document.querySelector('[data-f="quantity"]');
    qty.value = '7';
    qty.dispatchEvent(new first.Event('input', { bubbles: true }));
    const saved = [...Array(first.localStorage.length).keys()]
      .map(i => first.localStorage.key(i)).filter(k => k.includes('wh-invoice'));
    expect(saved).toHaveLength(1);
    const raw = first.localStorage.getItem(saved[0]);

    // «Переоткрыли» приложение: новое окно, то же хранилище.
    const second = makeWindow();
    second.localStorage.setItem(saved[0], raw);
    second.eval(read('helpers.js'));
    second.eval(read('net.js'));
    second.eval(`${read('app.js')}
      currentUser = { role: 'boss', user_id: 42 };
      api = async (p) => p === '/api/wh/stock' ? { products: ${JSON.stringify(PRODUCTS)} } : { counterparties: [] };
      window.__draft = () => whDraft;
      window.__ready = renderWhInvoiceNew();
    `);
    await second.__ready;
    expect(second.__draft().items[0]).toMatchObject({ product_id: 2, quantity: 7 });
    expect(second.__draft().comment).toBe('довоз');
    expect(second.document.querySelector('[data-f="quantity"]').value).toBe('7');
  });

  it('без сети справочники — ошибка, а не вечная загрузка', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      api = async () => { throw new Error('Нет связи — проверьте интернет и повторите'); };
      window.__ready = renderWhInvoiceNew();
    `);
    await window.__ready;
    expect(window.document.getElementById('content').textContent).toContain('Нет подключения');
  });
});

// ─── Черновики «Кассы» ──────────────────────────────────────────────────────

describe('форма платежа: черновик возвращается после переоткрытия', () => {
  it('сумма, валюта и комментарий на месте', async () => {
    const drv = `
      currentUser = { role: 'manager', user_id: 42 };
      api = async (p) => p === '/api/deposits/my' ? { deposits: [] } : {};
      window.__ready = renderCashbox(document.getElementById('content'), 'ops');
    `;
    const first = boot(drv);
    await first.__ready;
    const d1 = first.document;
    d1.querySelector('.pay-row-amount').value = '1 500';
    d1.querySelector('.pay-row-amount').dispatchEvent(new first.Event('input', { bubbles: true }));
    d1.querySelector('[data-cur-opt="UZS"]').click();
    d1.querySelector('#pay-comment').value = 'аренда';
    d1.querySelector('#pay-comment').dispatchEvent(new first.Event('input', { bubbles: true }));
    const key = [...Array(first.localStorage.length).keys()].map(i => first.localStorage.key(i))
      .find(k => k.includes('cash-forms'));
    expect(key).toBeTruthy();

    const second = makeWindow();
    second.localStorage.setItem(key, first.localStorage.getItem(key));
    second.eval(read('helpers.js'));
    second.eval(read('net.js'));
    second.eval(`${read('app.js')}\n${drv}`);
    await second.__ready;
    const d2 = second.document;
    expect(d2.querySelector('.pay-row-amount').value).toBe('1 500');
    expect(d2.querySelector('.pay-row-cur').dataset.cur).toBe('UZS');
    expect(d2.querySelector('#pay-comment').value).toBe('аренда');
  });
});

// ─── 7. Подтверждение успеха ────────────────────────────────────────────────

describe('успех денежных действий виден', () => {
  it('платёж: тост с суммой и валютой, черновик стёрт', async () => {
    const window = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      renderMoneyScreen = async () => {};
      api = async (p) => p === '/api/deposits/my' ? { deposits: [] } : { payment_ids: [1] };
      window.__ready = renderCashbox(document.getElementById('content'), 'ops');
    `);
    await window.__ready;
    const doc = window.document;
    doc.querySelector('.pay-row-amount').value = '12,50';
    doc.querySelector('#pay-comment').value = 'аренда';
    doc.querySelector('#pay-comment').dispatchEvent(new window.Event('input', { bubbles: true }));
    doc.querySelector('#pay-submit').click();
    await flush(); await flush();
    expect(toastsOf(window).join(' ').replace(/\s/g, ' ')).toContain('12,50 USD');
    const left = [...Array(window.localStorage.length).keys()].map(i => window.localStorage.key(i))
      .filter(k => k.includes('cash-forms'));
    expect(left).toHaveLength(0);
  });

  it('подтверждение оплаты руководителем — тост, а не молча пропавшая карточка', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      renderMoneyScreen = async () => {};
      api = async (p) => p === '/api/payments/pending'
        ? { pending: [{ order_id: 5, pending: 10, total: 10, currency: 'USD', agent_name: 'А', full_name: 'М' }] }
        : { deposits: [], returns: [], ok: true };
      window.__ready = renderCashbox(document.getElementById('content'), 'confirm');
    `);
    await window.__ready;
    window.document.querySelector('.pay-confirm').click();
    await flush(); await flush();
    expect(toastsOf(window).join(' ')).toContain('подтверждена');
  });
});

// ─── 8. Страницы заказов ────────────────────────────────────────────────────

describe('заказы: страницы и фильтры на сервере', () => {
  const order = (id, status = 'approved', day = '2026-09-10') => ({
    id, status, created_at: `${day} 10:00`, total: 0, currency: 'USD', agent_name: 'К',
    full_name: 'М', payment_type: 'paid', items: [], items_count: 0,
  });

  it('первая страница — с limit/offset, «Показать ещё» дописывает следующую', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      window.__calls = [];
      api = async (p, b) => {
        window.__calls.push(b);
        if (!b.offset) return { orders: [${JSON.stringify(order(3))}, ${JSON.stringify(order(2))}],
          role: 'boss', total: 3, has_more: true, next_offset: 2, pending_count: 4 };
        return { orders: [${JSON.stringify(order(2))}, ${JSON.stringify(order(1))}],
          role: 'boss', total: 3, has_more: false, next_offset: 3, pending_count: 4 };
      };
      window.__ready = renderOrders();
    `);
    await window.__ready;
    const doc = window.document;
    expect(window.__calls[0]).toMatchObject({ limit: 50, offset: 0, statuses: [] });
    const more = doc.getElementById('orders-more');
    expect(more.textContent).toContain('Показать ещё (1)');
    // Счётчик заявок — с сервера, по всем заказам, а не по странице.
    expect(doc.querySelector('#show-requests .queue-count').textContent).toBe('4');
    more.click();
    await flush(); await flush();
    expect(window.__calls[1]).toMatchObject({ offset: 2 });
    const ids = [...doc.querySelectorAll('.order-card')].map(c => c.dataset.id);
    expect(ids).toEqual(['3', '2', '1']);   // повтор #2 не задвоился
    expect(doc.getElementById('orders-more')).toBeNull();
  });

  it('фильтр статуса и период уходят на сервер вместе со страницей', async () => {
    const window = boot(`
      currentUser = { role: 'boss', user_id: 1 };
      window.__calls = [];
      api = async (p, b) => { window.__calls.push(b); return { orders: [], role: 'boss', total: 0, has_more: false, next_offset: 0 }; };
      window.__ready = renderOrders();
    `);
    await window.__ready;
    const doc = window.document;
    doc.querySelector('.seg-item[data-filter="rejected"]').click();
    await flush(); await flush();
    expect(window.__calls.at(-1)).toMatchObject({ statuses: ['rejected', 'cancelled'], offset: 0 });
    doc.querySelector('[data-operiod="today"]').click();
    await flush(); await flush();
    const last = window.__calls.at(-1);
    expect(last.statuses).toEqual(['rejected', 'cancelled']);
    expect(last.date_from).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(last.date_to).toBe(last.date_from);
  });

  it('orderPeriodRange: 7 дней — сегодня и шесть предыдущих', () => {
    const window = boot('');
    const r = window.eval('orderPeriodRange("7d", new Date(2026, 8, 15))');
    expect(r).toEqual({ date_from: '2026-09-09', date_to: '' });
    expect(window.eval('orderPeriodRange("all", new Date(2026, 8, 15))')).toEqual({ date_from: '', date_to: '' });
  });
});
