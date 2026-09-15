// «Продажи → Заказы»: быстрые jsdom-регрессы к багам, найденным E2E
// (tests/e2e/test_cov_sales.py). Отдельным файлом, а не в app-load.smoke:
// каркас тот же — helpers.js + app.js в одном окне, драйвер в том же eval.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

function boot(driver = '') {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="content"></div>'
    + '<nav class="bottom-nav" id="bottom-nav"></nav></body>', {
    url: 'https://example.org/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const noop = () => {};
  window.Telegram = {
    WebApp: {
      ready: noop, expand: noop, onEvent: noop, colorScheme: 'light', themeParams: {},
      initData: 'tgWebAppData=stub', initDataUnsafe: {},
      HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
      showAlert: noop, showConfirm: noop, setHeaderColor: noop, setBackgroundColor: noop,
      enableClosingConfirmation: noop, disableClosingConfirmation: noop,
      MainButton: { show: noop, hide: noop, setText: noop, onClick: noop, offClick: noop },
      BackButton: { show: noop, hide: noop, onClick: noop, offClick: noop },
    },
  };
  window.fetch = () => new Promise(() => {});
  window.eval(read('helpers.js'));
  window.eval(read('net.js'));
  window.eval(`${read('app.js')}\n${driver}`);
  return window;
}

const tick = () => new Promise(r => setTimeout(r, 0));

const DRAFT = {
  id: 5, status: 'draft', full_name: 'Manager', agent_name: 'ООО Ромашка', comment: '',
  currency: 'USD', payment_type: 'credit', due_date: '2030-01-15',
  created_at: '2030-01-01 10:00', items_count: 2, total: 400, frozen: false,
  rejection_count: 1, rejection_comment: 'Проверьте цену',
  items: [
    { id: 41, name: 'Кабель', quantity: 1, unit: 'м', price: 100 },
    { id: 42, name: 'Кабель', quantity: 3, unit: 'м', price: 100 },
  ],
};

describe('повторно открытый черновик', () => {
  const driver = (apiBody = 'return { ok: true };') => `
    currentUser = { role: 'manager' };
    currentScreen = 'sales';
    window.__calls = [];
    window.__alerts = [];
    tg.showAlert = (t) => { window.__alerts.push(t); };
    api = async (path, body) => { window.__calls.push([path, body]); ${apiBody} };
    ordersData = { orders: [${JSON.stringify(DRAFT)}], role: 'manager' };
    window.__ready = openOrderEditor(5);
  `;

  it('крестик удаляет позицию по её id, в том числе первую', async () => {
    const window = boot(driver());
    await window.__ready;
    window.document.querySelector('.editor-item-del[data-idx="0"]').click();
    await tick();
    expect(window.__calls).toEqual([['/api/orders/remove_item', { item_id: 41 }]]);
    expect(window.document.querySelectorAll('.editor-item-del').length).toBe(1);
  });

  it('отказ сервера виден и строку не убирает', async () => {
    const window = boot(driver("throw new Error('Нет доступа');"));
    await window.__ready;
    window.document.querySelector('.editor-item-del[data-idx="1"]').click();
    await tick();
    expect(window.__alerts.some(a => a.includes('Нет доступа'))).toBe(true);
    expect(window.document.querySelectorAll('.editor-item-del').length).toBe(2);
  });

  it('валюта, тип оплаты и срок берутся из заказа', async () => {
    const window = boot(driver());
    await window.__ready;
    const doc = window.document;
    expect(doc.querySelector('.seg-item.active[data-pay="credit"]')).not.toBeNull();
    expect(doc.getElementById('due-date-input').value).toBe('2030-01-15');
    window.eval("openQuantityInput('Кабель', 'м', 10, '1')");
    expect(doc.querySelector('.cur-btn[data-cur="UZS"]').disabled).toBe(true);
    expect(doc.querySelector('.cur-btn.active[data-cur="USD"]')).not.toBeNull();
  });

  it('пустой черновик валюту не фиксирует — список отдаёт базовую по умолчанию', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      ordersData = { orders: [${JSON.stringify({ ...DRAFT, items: [], items_count: 0 })}], role: 'manager' };
      window.__ready = openOrderEditor(5);
    `);
    await window.__ready;
    window.eval("openQuantityInput('Кабель', 'м', 10, '1')");
    expect(window.document.querySelector('.cur-btn[data-cur="UZS"]').disabled).toBe(false);
  });

  it('клиент не ставится в редактор, если сервер его не принял', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__alerts = [];
      tg.showAlert = (t) => { window.__alerts.push(t); };
      api = async (path) => {
        if (path === '/api/agents') return { agents: [{ id: 9, name: 'ИП Васильев' }] };
        throw new Error('Заказ уже отправлен');
      };
      ordersData = { orders: [${JSON.stringify({ ...DRAFT, agent_name: '' })}], role: 'manager' };
      window.__draft = () => currentDraftOrder;   // let из app.js снаружи eval не виден
      window.__ready = openOrderEditor(5).then(() => openAgentSearch());
    `);
    await window.__ready;
    await tick();
    window.document.querySelector('.agent-row').click();
    await tick();
    expect(window.__alerts.some(a => a.includes('Заказ уже отправлен'))).toBe(true);
    expect(window.__draft().agent_name).toBe('');
  });
});

describe('фильтр «Отменены»', () => {
  it('показывает и отклонённые заявки, и отменённые заказы', () => {
    const order = (id, status) => ({ ...DRAFT, id, status, items: [] });
    const window = boot(`
      currentUser = { role: 'boss' };
      ordersData = { orders: ${JSON.stringify([order(1, 'rejected'), order(2, 'cancelled'), order(3, 'approved')])},
                     role: 'boss' };
      currentOrderFilter = 'rejected';
      currentOrderPeriod = 'all';
      renderOrdersMain();
    `);
    const ids = [...window.document.querySelectorAll('.order-card')].map(c => c.dataset.id);
    expect(ids.sort()).toEqual(['1', '2']);
  });
});
