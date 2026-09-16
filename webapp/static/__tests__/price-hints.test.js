// Подсказки цены в форме позиции заказа (B7/D4).
//
// Две части: чистые хелперы (порядок приоритета, валюта, пустое состояние) и
// jsdom-прогон самой формы — подсказка видна, поле предзаполнено, тап по
// альтернативе подставляет её, а набранное руками не затирается.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

import { priceSuggestions, pricePrefill, priceHintText } from '../helpers.js';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

const LAST = { price: 45, currency: 'USD', date: '12.09' };
const DEFAULT = { price: 50, currency: 'USD' };
const WHOLESALE = { price: 44, currency: 'USD' };

describe('приоритет подсказок цены', () => {
  it('прошлая цена клиента важнее цены товара', () => {
    expect(pricePrefill({ last: LAST, default: DEFAULT }, 'USD')).toBe(45);
  });

  it('без истории клиента подставляется цена товара', () => {
    expect(pricePrefill({ last: null, default: DEFAULT }, 'USD')).toBe(50);
  });

  it('без истории и без цены товара поле остаётся пустым', () => {
    expect(pricePrefill({}, 'USD')).toBeNull();
    expect(pricePrefill(null, 'USD')).toBeNull();
    expect(priceSuggestions(null, 'USD')).toEqual([]);
  });

  it('«для постоянных» сама не префиллит — это альтернатива в один тап', () => {
    expect(pricePrefill({ wholesale: WHOLESALE }, 'USD')).toBeNull();
    const list = priceSuggestions({ wholesale: WHOLESALE }, 'USD');
    expect(list.map(s => s.source)).toEqual(['wholesale']);
  });

  it('порядок подсказок — прошлая, цена товара, для постоянных', () => {
    const list = priceSuggestions({ last: LAST, default: DEFAULT, wholesale: WHOLESALE }, 'USD');
    expect(list.map(s => s.source)).toEqual(['last', 'default', 'wholesale']);
  });

  it('нулевая и отрицательная цена подсказкой не становится', () => {
    expect(priceSuggestions({ last: { price: 0, currency: 'USD' } }, 'USD')).toEqual([]);
    expect(priceSuggestions({ default: { price: -3, currency: 'USD' } }, 'USD')).toEqual([]);
  });
});

describe('валюта подсказки', () => {
  it('чужая валюта видна, но не префиллит', () => {
    const list = priceSuggestions({ last: { price: 500000, currency: 'UZS' } }, 'USD');
    expect(list[0].matches).toBe(false);
    expect(pricePrefill({ last: { price: 500000, currency: 'UZS' } }, 'USD')).toBeNull();
  });

  it('цена без валюты считается ценой в валюте формы (старые строки)', () => {
    expect(pricePrefill({ default: { price: 50 } }, 'USD')).toBe(50);
  });

  it('подпись несёт валюту и дату', () => {
    expect(priceHintText(priceSuggestions({ last: LAST }, 'USD')[0]))
      .toBe('Прошлый раз: 45 USD (12.09)');
    expect(priceHintText(priceSuggestions({ default: DEFAULT }, 'USD')[0]))
      .toBe('Цена: 50 USD');
  });
});

// ─── Форма позиции ───────────────────────────────────────────────────────────

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

/** Форма позиции с подсказками из `hint`; `pre` выполняется до ответа сети. */
function openForm(hint, { pre = '' } = {}) {
  return boot(`
    currentUser = { role: 'manager' };
    currentScreen = 'sales';
    window.__calls = [];
    let _resolve;
    window.__answer = () => _resolve({ ok: true, status: 200, body: ${JSON.stringify(hint)} });
    apiResult = async (path, body) => {
      window.__calls.push([path, body]);
      return new Promise(r => { _resolve = r; });
    };
    currentDraftOrder = { id: 5, agent_id: '7', agent_name: 'ООО Ромашка', currency: null, items: [] };
    openQuantityInput('Кабель', 'м', 20, '42');
    ${pre}
  `);
}

describe('форма позиции: подсказка и префилл', () => {
  it('спрашивает подсказку по заказу и товару', async () => {
    const window = openForm({ last: LAST, default: DEFAULT, wholesale: WHOLESALE });
    expect(window.__calls).toEqual([
      ['/api/orders/price_hint', { order_id: 5, product_id: '42' }],
    ]);
  });

  it('до ответа поле пустое — ввод количества сети не ждёт', () => {
    const window = openForm({ last: LAST });
    expect(window.document.getElementById('price-input').value).toBe('');
    expect(window.document.getElementById('price-hint').innerHTML).toBe('');
  });

  it('прошлая цена клиента попадает в поле и в подпись', async () => {
    const window = openForm({ last: LAST, default: DEFAULT, wholesale: WHOLESALE });
    window.__answer();
    await tick();
    expect(window.document.getElementById('price-input').value).toBe('45');
    const text = window.document.getElementById('price-hint').textContent;
    expect(text).toContain('Прошлый раз: 45 USD (12.09)');
    expect(text).toContain('Постоянным: 44 USD');
    expect(window.document.querySelector('#line-total').textContent).toContain('0');
  });

  it('без истории клиента подставляется цена товара', async () => {
    const window = openForm({ last: null, default: DEFAULT });
    window.__answer();
    await tick();
    expect(window.document.getElementById('price-input').value).toBe('50');
  });

  it('пустой ответ оставляет форму такой, какой она была до B7', async () => {
    const window = openForm({ last: null, default: null, wholesale: null });
    window.__answer();
    await tick();
    expect(window.document.getElementById('price-input').value).toBe('');
    expect(window.document.getElementById('price-hint').innerHTML).toBe('');
  });

  it('тап по «Постоянным» подставляет её цену', async () => {
    const window = openForm({ last: LAST, wholesale: WHOLESALE });
    window.__answer();
    await tick();
    window.document.querySelector('[data-price-hint="wholesale"]').click();
    expect(window.document.getElementById('price-input').value).toBe('44');
  });

  it('цену, набранную руками, ответ сети не затирает', async () => {
    const window = openForm({ last: LAST }, { pre: `
      const el = document.getElementById('price-input');
      el.value = '77';
      el.dispatchEvent(new window.Event('input'));
    ` });
    window.__answer();
    await tick();
    expect(window.document.getElementById('price-input').value).toBe('77');
    // Сама подсказка при этом видна — менеджеру полезно знать прошлую цену.
    expect(window.document.getElementById('price-hint').textContent).toContain('Прошлый раз');
  });

  it('подсказка в чужой валюте не префиллит долларовый заказ', async () => {
    const window = openForm({ last: { price: 500000, currency: 'UZS' } });
    window.__answer();
    await tick();
    expect(window.document.getElementById('price-input').value).toBe('');
    expect(window.document.getElementById('price-hint').textContent).toContain('UZS');
    expect(window.document.querySelector('[data-price-hint]')).toBeNull();
  });
});
