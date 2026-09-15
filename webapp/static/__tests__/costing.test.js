// costing.js — блоки себестоимости руководства: грузится после app.js, рисует
// курсовую разницу словами владельца, экранирует чужой ввод и НЕ ходит в ручки
// под менеджером (они ответили бы 403).
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

function boot(driver = '') {
  const dom = new JSDOM('<!DOCTYPE html><body><div id="content"></div>'
    + '<nav class="bottom-nav" id="bottom-nav"></nav></body>', {
    url: 'https://example.org/', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const { window } = dom;
  const noop = () => {};
  window.Telegram = { WebApp: {
    ready: noop, expand: noop, onEvent: noop, colorScheme: 'light', themeParams: {},
    initData: 'tgWebAppData=stub', initDataUnsafe: {},
    HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
    showAlert: noop, showConfirm: (m, cb) => cb(true), setHeaderColor: noop, setBackgroundColor: noop,
    MainButton: { show: noop, hide: noop, setText: noop, onClick: noop, offClick: noop },
    BackButton: { show: noop, hide: noop, onClick: noop, offClick: noop },
  } };
  window.fetch = () => new Promise(() => {});
  window.eval(read('helpers.js'));
  // Один eval с app.js: `let currentUser`/`api` объявлены на верхнем уровне его
  // скрипта, и из отдельного eval их не видно (в браузере теги делят scope).
  window.eval(`${read("app.js")}\n;\n${read("costing.js")}\n;\n${driver}`);
  return window;
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe('costing.js', () => {
  it('грузится после app.js и отдаёт точки монтирования', () => {
    const window = boot();
    for (const name of ['mountContainerCosting', 'mountSalesCosting', 'mountProductCostHistory']) {
      expect(typeof window[name]).toBe('function');
    }
    expect(read('index.html').indexOf('costing.js')).toBeGreaterThan(read('index.html').indexOf('app.js'));
  });

  it('курсовая разница — словами, со знаком и без «$»', () => {
    const window = boot();
    const text = window.costingFxText({ at_arrival_cents: 20800, at_sale_cents: 20000, diff_cents: -800 }, 'USD');
    expect(text).toContain('по курсу дня прибытия товара стоила бы 208 USD');
    expect(text).toContain('по курсу дня продажи — 200 USD');
    expect(text).toContain('потеряли на курсе 8 USD');
    expect(window.costingFxText({ at_arrival_cents: 0, at_sale_cents: 0, diff_cents: 0 }, 'USD')).toBe('');
    expect(window.costingSigned(-800, 'USD')).toBe('−8 USD');
    expect(window.costingMonthLabel('2026-09')).toBe('Сентябрь 2026');
  });

  it('менеджеру блоки не рисуются и в ручки не ходят', async () => {
    const window = boot(`currentUser = { role: 'manager' };
      window.__calls = [];
      api = async (p) => { window.__calls.push(p); return {}; };
      const host = document.createElement('div'); document.body.appendChild(host);
      window.__host = host;
      mountContainerCosting(1, host); mountSalesCosting(host); mountProductCostHistory(host, 1);`);
    await tick();
    expect(window.__calls).toEqual([]);
    expect(window.__host.innerHTML).toBe('');
  });

  it('отчёт боссу: выключенный учёт предлагает включить', async () => {
    const window = boot(`currentUser = { role: 'boss' };
      api = async () => ({ ok: true, enabled: false });
      const host = document.createElement('div'); document.getElementById('content').appendChild(host);
      window.__host = host; mountSalesCosting(host);`);
    await tick();
    expect(window.__host.querySelector('#costing-enable')).not.toBeNull();
    expect(window.__host.textContent).toContain('после переноса истории');
  });

  it('отчёт боссу: прибыль, курс и сделки в минус, имена экранированы', async () => {
    const rep = {
      ok: true, enabled: true, base_currency: 'USD', started_at: '2026-09-01',
      totals: { revenue_cents: 20000, cogs_cents: 2000, profit_cents: 18000, margin_pct: 90,
                unknown_cost_lines: 0, unknown_cost_revenue_cents: 0 },
      fx: { at_arrival_cents: 20800, at_sale_cents: 20000, diff_cents: -800, by_currency: [] },
      by_month: [{ month: '2026-09', revenue_cents: 20000, cogs_cents: 2000, profit_cents: 18000, fx_diff_cents: -800 }],
      top_products: [{ product_id: 1, name: 'Фильтр', qty: 2, revenue_cents: 20000, cogs_cents: 2000, profit_cents: 18000, margin_pct: 90 }],
      negative_deals: [{ invoice_id: 5, invoice_number: 'OUT-2026-0005', date: '2026-09-10',
                         counterparty: '<img src=x onerror=alert(1)>', revenue_cents: 100, cogs_cents: 300, profit_cents: -200 }],
      containers: [],
    };
    const window = boot(`currentUser = { role: 'boss' };
      api = async () => (${JSON.stringify(rep)});
      const host = document.createElement('div'); document.getElementById('content').appendChild(host);
      window.__host = host; mountSalesCosting(host);`);
    await tick();
    const text = window.__host.textContent;
    expect(text).toContain('+180 USD');
    expect(text).toContain('−8 USD');
    expect(text).toContain('Сентябрь 2026');
    expect(text).toContain('Сделки в минус');
    expect(window.__host.querySelector('img')).toBeNull();
    expect(window.__host.querySelector('#costing-fx-text').textContent).toContain('потеряли на курсе 8 USD');
  });

  it('иконки costing.js есть в спрайте', () => {
    const html = read('index.html');
    const names = [...read('costing.js').matchAll(/\bicon\(\s*'([a-z][a-z-]*)'/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(0);
    expect(names.filter((n) => !html.includes(`id="ic-${n}"`))).toEqual([]);
  });
});
