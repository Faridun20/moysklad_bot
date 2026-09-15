// D1 продуктового аудита: «Выбор товара» показывает сверху товары, которые
// уже заказывали — этому клиенту (по давности покупки) или, без клиента/у
// нового клиента, самому менеджеру (по частоте за ~30 дней). Каркас — как в
// orders-editor.test.js: helpers.js + net.js + app.js в одном окне.
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

const PRODUCTS = [
  { product_id: 1, name: 'Аккумулятор', unit: 'шт', stock: 10, available: 10, folder_id: null, folder_name: null },
  { product_id: 2, name: 'Инвертор', unit: 'шт', stock: 4, available: 4, folder_id: null, folder_name: null },
  { product_id: 3, name: 'Кабель', unit: 'м', stock: 200, available: 200, folder_id: null, folder_name: null },
  { product_id: 4, name: 'Щит', unit: 'шт', stock: 2, available: 2, folder_id: null, folder_name: null },
];

function rowOrder(doc) {
  return [...doc.querySelectorAll('.prod-row')].map(r => Number(r.dataset.product));
}

describe('_pickerSections — чистая функция сортировки/секции', () => {
  it('без подсказки список не трогает', () => {
    const window = boot();
    const { ordered, hintCount } = window._pickerSections(
      PRODUCTS, { kind: 'none', label: '', product_ids: [] }, { search: '', selectedCat: 'all' },
    );
    expect(ordered).toEqual(PRODUCTS);
    expect(hintCount).toBe(0);
  });

  it('подсказку выносит вперёд в её порядке, остальное — как было', () => {
    const window = boot();
    const hints = { kind: 'client_recent', label: 'Недавно у этого клиента', product_ids: [4, 1] };
    const { ordered, hintCount, label } = window._pickerSections(
      PRODUCTS, hints, { search: '', selectedCat: 'all' },
    );
    expect(ordered.map(p => p.product_id)).toEqual([4, 1, 2, 3]);
    expect(hintCount).toBe(2);
    expect(label).toBe('Недавно у этого клиента');
  });

  it('поиск отключает сортировку по подсказке', () => {
    const window = boot();
    const hints = { kind: 'manager_frequent', label: 'Часто заказываемое', product_ids: [4, 1] };
    const { ordered, hintCount } = window._pickerSections(
      PRODUCTS, hints, { search: 'кабель', selectedCat: 'all' },
    );
    expect(ordered).toEqual(PRODUCTS);
    expect(hintCount).toBe(0);
  });

  it('выбранная категория (не «Все») тоже отключает секцию', () => {
    const window = boot();
    const hints = { kind: 'manager_frequent', label: 'Часто заказываемое', product_ids: [4, 1] };
    const { ordered, hintCount } = window._pickerSections(
      PRODUCTS, hints, { search: '', selectedCat: 'cat-1' },
    );
    expect(ordered).toEqual(PRODUCTS);
    expect(hintCount).toBe(0);
  });

  it('товары из подсказки, которых уже нет в каталоге, тихо пропускаются', () => {
    const window = boot();
    const hints = { kind: 'client_recent', label: 'Недавно у этого клиента', product_ids: [999, 4] };
    const { ordered, hintCount } = window._pickerSections(
      PRODUCTS, hints, { search: '', selectedCat: 'all' },
    );
    expect(ordered.map(p => p.product_id)).toEqual([4, 1, 2, 3]);
    expect(hintCount).toBe(1);
  });
});

describe('openProductPicker — секция подсказки на экране', () => {
  const driver = (hints, agentId) => `
    currentUser = { role: 'manager' };
    currentDraftOrder = { id: 1, items: [], agent_id: ${JSON.stringify(agentId || null)}, agent_name: null };
    window.__calls = [];
    api = async (p, body) => {
      window.__calls.push([p, body]);
      if (p === '/api/stock') return { products: ${JSON.stringify(PRODUCTS)}, categories: [] };
      if (p === '/api/products/picker_hints') return ${JSON.stringify(hints)};
      return { ok: true };
    };
    window.__ready = openProductPicker();
  `;

  it('клиент с историей — секция «Недавно у этого клиента» сверху, полный список ниже', async () => {
    const hints = { kind: 'client_recent', label: 'Недавно у этого клиента', product_ids: [4, 1] };
    const window = boot(driver(hints, '77'));
    await window.__ready;
    const doc = window.document;
    expect(window.__calls).toContainEqual(['/api/products/picker_hints', { agent_id: '77' }]);
    const labels = [...doc.querySelectorAll('#prod-list .section-label')].map(e => e.textContent);
    expect(labels).toEqual(['Недавно у этого клиента', 'Все товары']);
    expect(rowOrder(doc)).toEqual([4, 1, 2, 3]);
  });

  it('нет клиента — секция «Часто заказываемое», запрос без agent_id', async () => {
    const hints = { kind: 'manager_frequent', label: 'Часто заказываемое', product_ids: [3] };
    const window = boot(driver(hints, null));
    await window.__ready;
    const doc = window.document;
    expect(window.__calls).toContainEqual(['/api/products/picker_hints', {}]);
    const labels = [...doc.querySelectorAll('#prod-list .section-label')].map(e => e.textContent);
    expect(labels).toEqual(['Часто заказываемое', 'Все товары']);
    expect(rowOrder(doc)).toEqual([3, 1, 2, 4]);
  });

  it('без истории — обычный алфавитный список, ни одной секции', async () => {
    const hints = { kind: 'none', label: '', product_ids: [] };
    const window = boot(driver(hints, null));
    await window.__ready;
    const doc = window.document;
    expect(doc.querySelectorAll('#prod-list .section-label').length).toBe(0);
    expect(rowOrder(doc)).toEqual([1, 2, 3, 4]);
  });

  it('ввод в поиске возвращает чистый алфавитный/фильтрованный порядок без секций', async () => {
    const hints = { kind: 'manager_frequent', label: 'Часто заказываемое', product_ids: [4, 1] };
    const window = boot(driver(hints, null));
    await window.__ready;
    const doc = window.document;
    doc.getElementById('prod-search').value = 'а';
    doc.getElementById('prod-search').dispatchEvent(new window.Event('input'));
    await new Promise(r => setTimeout(r, 300));  // дебаунс поиска (250мс)
    expect(doc.querySelectorAll('#prod-list .section-label').length).toBe(0);
    // «Аккумулятор» и «Кабель» содержат «а» — порядок алфавитный, как отдал каталог.
    expect(rowOrder(doc)).toEqual([1, 3]);
  });

  it('сбой подсказки не ломает сам выбор товара — просто без секции', async () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      currentDraftOrder = { id: 1, items: [], agent_id: null, agent_name: null };
      api = async (p) => {
        if (p === '/api/stock') return { products: ${JSON.stringify(PRODUCTS)}, categories: [] };
        throw new Error('Нет связи');
      };
      window.__ready = openProductPicker();
    `);
    await window.__ready;
    const doc = window.document;
    expect(doc.querySelectorAll('#prod-list .section-label').length).toBe(0);
    expect(rowOrder(doc)).toEqual([1, 2, 3, 4]);
  });
});
