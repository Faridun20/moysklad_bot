// B8 — несколько складов: фронт не меняется byte-в-byte, пока склад один
// (`currentUser.multi_warehouse` не выставлен /api/me), и добавляет ровно то,
// что нужно, когда их больше одного. Каркас — как в orders-editor.test.js.
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
  id: 9, status: 'draft', full_name: 'Manager', agent_name: 'ООО Ромашка', comment: '',
  currency: 'USD', payment_type: 'paid', due_date: null,
  created_at: '2030-01-01 10:00', items_count: 0, total: 0, frozen: false,
  rejection_count: 0, rejection_comment: '', items: [],
};

describe('Заказ: пикер склада отгрузки (B8)', () => {
  it('один склад (multi_warehouse не выставлен) — ни одного лишнего похода в сеть, пикера нет', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__calls = [];
      api = async (p, body) => { window.__calls.push([p, body]); return { ok: true }; };
      ordersData = { orders: [${JSON.stringify(DRAFT)}], role: 'manager' };
      window.__ready = openOrderEditor(9);
    `);
    await window.__ready;
    expect(window.__calls).toEqual([]);
    expect(window.document.getElementById('warehouse-selector')).toBeNull();
  });

  it('несколько складов — пикер виден и меняет выбор через /api/orders/set_warehouse', async () => {
    const window = boot(`
      currentUser = { role: 'manager', multi_warehouse: true };
      window.__calls = [];
      api = async (p, body) => {
        window.__calls.push([p, body]);
        if (p === '/api/warehouses/active') {
          return { warehouses: [{ id: 1, name: 'Основной склад' }, { id: 2, name: 'Склад Б' }],
                    last_used_warehouse_id: 1 };
        }
        return { ok: true };
      };
      ordersData = { orders: [${JSON.stringify(DRAFT)}], role: 'manager' };
      window.__ready = openOrderEditor(9);
    `);
    await window.__ready;
    const doc = window.document;
    expect(doc.getElementById('warehouse-selector').textContent).toContain('Основной склад');
    expect(window.__calls.some(c => c[0] === '/api/warehouses/active')).toBe(true);

    doc.getElementById('change-warehouse').click();
    await tick();
    // Пикер (openListPicker) открывает свою шторку со списком складов.
    const rows = doc.querySelectorAll('.picker-list .c-row');
    expect(rows.length).toBe(2);
  });
});

describe('Накладная: пикер склада (B8)', () => {
  it('один склад — форма выглядит как раньше, без строки «Склад»', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__calls = [];
      api = async (p, body) => {
        window.__calls.push([p, body]);
        if (p === '/api/wh/stock') return { products: [] };
        return { counterparties: [] };
      };
      whView = 'new'; whDraft = null;
      window.__ready = renderWhInvoicesTab();
    `);
    await window.__ready;
    expect(window.document.getElementById('wh-warehouse')).toBeNull();
    expect(window.__calls.some(c => c[0] === '/api/warehouses/active')).toBe(false);
  });

  it('несколько складов — строка «Склад» появляется, warehouse_id уходит в create', async () => {
    const window = boot(`
      currentUser = { role: 'manager', multi_warehouse: true };
      window.__calls = [];
      api = async (p, body) => {
        window.__calls.push([p, body]);
        if (p === '/api/wh/stock') return { products: [
          { product_id: 1, name: 'Болт М8', unit: 'шт', quantity: 10 },
        ] };
        if (p === '/api/warehouses/active') {
          return { warehouses: [{ id: 1, name: 'Основной склад' }, { id: 2, name: 'Склад Б' }],
                    last_used_warehouse_id: 2 };
        }
        return { counterparties: [] };
      };
      whView = 'new'; whDraft = null;
      window.__ready = renderWhInvoicesTab();
    `);
    await window.__ready;
    const btn = window.document.getElementById('wh-warehouse');
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain('Склад Б');
  });
});

describe('Каталог: разбивка по складам (B8)', () => {
  const STOCK_ONE = { categories: [], multi_warehouse: false, products: [
    { product_id: 1, name: 'Болт М8', unit: 'шт', stock: 12, available: 12, folder_id: '' },
  ]};
  const STOCK_TWO = { categories: [], multi_warehouse: true, products: [
    { product_id: 1, name: 'Болт М8', unit: 'шт', stock: 12, available: 12, folder_id: '',
      by_warehouse: [
        { warehouse_id: 1, warehouse_name: 'Основной склад', quantity: 7 },
        { warehouse_id: 2, warehouse_name: 'Склад Б', quantity: 5 },
      ] },
  ]};

  it('один склад — без строки разбивки', () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      stockData = ${JSON.stringify(STOCK_ONE)};
      renderStockContent();
    `);
    expect(window.document.querySelector('.stock-by-warehouse')).toBeNull();
  });

  it('несколько складов — разбивка видна в строке товара', () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      stockData = ${JSON.stringify(STOCK_TWO)};
      renderStockContent();
    `);
    const el = window.document.querySelector('.stock-by-warehouse');
    expect(el).not.toBeNull();
    expect(el.textContent).toContain('Основной склад: 7');
    expect(el.textContent).toContain('Склад Б: 5');
  });
});
