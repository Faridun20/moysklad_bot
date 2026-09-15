// Сверка кассы (helpers.js + cash_reconcile.js): предпросмотр разницы, подписи
// расхождения, склейка истории и сам экран. Каркас как у payments.test.js:
// helpers.js + net.js + app.js + cash_reconcile.js в одном окне.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');
const H = require('../helpers.js');

const CONTEXT = {
  ok: true, date: '2026-09-15', currencies: ['USD', 'UZS'],
  system: [{ currency: 'USD', amount_cents: 50000 }, { currency: 'UZS', amount_cents: 120000000 }],
  done_today: false, can_see_all: false,
};
const HISTORY = {
  ok: true, scope: 'mine', items: [
    { id: 2, count_date: '2026-09-15', counted_by: 200, counted_by_name: 'Фаридун М.',
      currency: 'USD', counted_cents: 40000, system_cents: 50000, diff_cents: -10000,
      note: 'забыл занести оплату', request_key: 'k2', created_at: '2026-09-15 18:00:00' },
    { id: 3, count_date: '2026-09-15', counted_by: 200, counted_by_name: 'Фаридун М.',
      currency: 'UZS', counted_cents: 120000000, system_cents: 120000000, diff_cents: 0,
      note: 'забыл занести оплату', request_key: 'k2', created_at: '2026-09-15 18:00:00' },
    { id: 1, count_date: '2026-09-14', counted_by: 200, counted_by_name: 'Фаридун М.',
      currency: 'USD', counted_cents: 50000, system_cents: 50000, diff_cents: 0,
      note: null, request_key: 'k1', created_at: '2026-09-14 18:00:00' },
  ],
};

function boot(driver = '') {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="content"></div><nav class="bottom-nav" id="bottom-nav"></nav></body>',
    { url: 'https://example.org/', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  const noop = () => {};
  window.__alerts = [];
  window.__toasts = [];
  window.Telegram = { WebApp: {
    ready: noop, expand: noop, onEvent: noop, colorScheme: 'light', themeParams: {},
    initData: 'tgWebAppData=stub', initDataUnsafe: { user: { id: 200 } },
    HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
    showAlert: (m) => window.__alerts.push(String(m)),
    showConfirm: (m, cb) => { window.__alerts.push('confirm:' + m); cb(true); },
    setHeaderColor: noop, setBackgroundColor: noop,
    enableClosingConfirmation: noop, disableClosingConfirmation: noop,
    MainButton: { show: noop, hide: noop, setText: noop, onClick: noop, offClick: noop },
    BackButton: { show: noop, hide: noop, onClick: noop, offClick: noop },
  } };
  window.fetch = () => new Promise(() => {});
  window.eval(read('helpers.js'));
  window.eval(read('net.js'));
  window.eval(`${read('app.js')}\n${read('cash_reconcile.js')}\n${driver}`);
  return window;
}

const flush = () => new Promise((r) => setTimeout(r, 0));
const norm = (s) => String(s).replace(/[\s  ]+/g, ' ');

describe('cash_reconcile.js подключён', () => {
  it('в index.html после app.js, точки входа на месте', () => {
    const html = read('index.html');
    expect(html.indexOf('/static/cash_reconcile.js')).toBeGreaterThan(html.indexOf('/static/app.js'));
    const w = boot();
    for (const fn of ['reconRenderTab', 'reconLines', 'reconPreview', 'reconHistoryHtml']) {
      expect(typeof w[fn]).toBe('function');
    }
  });

  it('вкладка «Сверка» есть у менеджера и руководства, но не у кладовщика', () => {
    const keys = (role, work) => H.roleSectionTabs('money', role, { work }).map((t) => t.key);
    expect(keys('manager', true)).toContain('reconcile');
    // У руководителя — и без «Рабочих действий»: это контроль, а не работа склада.
    expect(keys('boss', false)).toContain('reconcile');
    expect(keys('warehouse_keeper', true)).not.toContain('reconcile');
  });
});

describe('разница считается по каждой валюте отдельно', () => {
  const SYS = CONTEXT.system;

  it('сошлось — ноль, недостача и излишек названы точной суммой', () => {
    const lines = H.reconLines({ USD: '400', UZS: '1 300 000' }, SYS);
    const by = Object.fromEntries(lines.map((l) => [l.currency, l]));
    expect(by.USD.diff_cents).toBe(-10000);
    expect(by.UZS.diff_cents).toBe(10000000);
    expect(norm(H.reconDiffLabel(by.USD.diff_cents, 'USD'))).toBe('недостача 100 USD');
    expect(H.reconDiffLabel(0, 'USD')).toBe('сходится');
    expect(H.reconDiffClass(0)).toBe('recon-ok');
    expect(H.reconDiffClass(-1)).toBe('recon-warn');
  });

  it('валюты не влияют друг на друга: одна сошлась — вторая всё равно расхождение', () => {
    const pv = H.reconPreview(H.reconLines({ USD: '500', UZS: '1300000' }, SYS));
    expect(pv.matched).toBe(false);
    expect(pv.mismatched.map((l) => l.currency)).toEqual(['UZS']);
    expect(H.reconPreview(H.reconLines({ USD: '500' }, SYS)).matched).toBe(true);
  });

  it('пересчитанный ноль — результат, пустое поле — «не считал», мусор — отказ', () => {
    const zero = H.reconLines({ USD: '0' }, SYS);
    expect(zero[0].counted_cents).toBe(0);
    expect(zero[0].diff_cents).toBe(-50000);
    expect(H.reconLines({ USD: '', UZS: '  ' }, SYS)).toEqual([]);
    expect(H.reconPreview(H.reconLines({}, SYS)).valid).toBe(false);
    const bad = H.reconLines({ USD: '12о' }, SYS);
    expect(bad[0].invalid).toBe(true);
    expect(H.reconPreview(bad).valid).toBe(false);
  });

  it('наличные, которых в системе нет, целиком идут в расхождение', () => {
    const [line] = H.reconLines({ USD: '300' }, []);
    expect(line.system_cents).toBe(0);
    expect(line.diff_cents).toBe(30000);
  });
});

describe('история: строки одного пересчёта — одна карточка', () => {
  it('группирует по ключу и помечает расхождение', () => {
    const groups = H.reconGroupHistory(HISTORY.items);
    expect(groups.length).toBe(2);
    expect(groups[0].lines.map((l) => l.currency)).toEqual(['USD', 'UZS']);
    expect(groups[0].matched).toBe(false);
    expect(groups[1].matched).toBe(true);
    const html = H.reconHistoryHtml(HISTORY.items, { showWho: true });
    expect(html).toContain('recon-warn');
    expect(html).toContain('recon-ok');
    expect(html).toContain('15.09.2026');
    expect(norm(html)).toContain('недостача 100 USD');
    expect(html).toContain('забыл занести оплату');
  });

  it('пустая история — фраза, а не пустота', () => {
    expect(H.reconHistoryHtml([], { emptyText: 'Вы ещё не записывали пересчёты.' }))
      .toContain('Вы ещё не записывали пересчёты.');
  });

  it('текст человека экранируется — историю рисует чужой ввод', () => {
    const html = H.reconHistoryHtml([{
      count_date: '2026-09-15', counted_by: 1, counted_by_name: '<img src=x onerror=alert(1)>',
      currency: 'USD', counted_cents: 0, system_cents: 0, diff_cents: 0,
      note: '<script>alert(2)</script>', request_key: 'k',
    }], { showWho: true });
    expect(html).not.toContain('<img');
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;img');
  });
});

describe('экран сверки', () => {
  const driver = `
    window.__posted = [];
    api = async (p) => {
      if (p === '/api/cash/reconcile/context') return ${JSON.stringify(CONTEXT)};
      if (p === '/api/cash/reconcile/history') return ${JSON.stringify(HISTORY)};
      throw new Error('нежданная ручка ' + p);
    };
    apiResult = async (p, body) => {
      window.__posted.push([p, body]);
      return { ok: true, status: 200, body: {
        ok: true, date: '2026-09-15', matched: false,
        message: 'Записано с расхождением: недостача 100 USD',
        lines: [{ currency: 'USD', counted_cents: 40000, system_cents: 50000, diff_cents: -10000 }],
      } };
    };
    toast = (m, t) => window.__toasts.push([String(m), t || 'success']);
  `;

  it('показывает «по системе» рядом с полем и считает разницу на ввод', async () => {
    const w = boot(driver);
    const box = w.document.getElementById('content');
    await w.reconRenderTab(box);
    await flush();
    expect(norm(box.textContent)).toContain('По системе: 500 USD');
    const usd = box.querySelector('[data-recon-cur="USD"] .recon-amount');
    usd.value = '400';
    usd.dispatchEvent(new w.Event('input'));
    const diff = box.querySelector('[data-recon-diff="USD"]');
    expect(norm(diff.textContent)).toBe('недостача 100 USD');
    expect(diff.className).toContain('recon-warn');
    // Сошлось — зелёная подпись, и это тоже разрешённая к записи сверка.
    usd.value = '500';
    usd.dispatchEvent(new w.Event('input'));
    expect(box.querySelector('[data-recon-diff="USD"]').className).toContain('recon-ok');
    expect(box.querySelector('#recon-submit').disabled).toBe(false);
  });

  it('пустая форма не отправляется, заполненная уходит с примечанием', async () => {
    const w = boot(driver);
    const box = w.document.getElementById('content');
    await w.reconRenderTab(box);
    await flush();
    expect(box.querySelector('#recon-submit').disabled).toBe(true);

    const usd = box.querySelector('[data-recon-cur="USD"] .recon-amount');
    usd.value = '400';
    usd.dispatchEvent(new w.Event('input'));
    const note = box.querySelector('#recon-note');
    note.value = 'забыл занести оплату вчера';
    note.dispatchEvent(new w.Event('input'));
    box.querySelector('#recon-submit').dispatchEvent(new w.Event('click'));
    await flush();
    await flush();

    expect(w.__posted.length).toBe(1);
    const [path_, body] = w.__posted[0];
    expect(path_).toBe('/api/cash/reconcile');
    expect(body.counts).toEqual([{ currency: 'USD', amount: '400' }]);
    expect(body.note).toBe('забыл занести оплату вчера');
    expect(body.idempotency_key).toBeTruthy();
    // Расхождение унести с экрана обязаны: тревожный тост с точной суммой.
    expect(w.__toasts.at(-1)).toEqual(['Записано с расхождением: недостача 100 USD', 'error']);
  });

  it('менеджеру — своя история без фильтра «только расхождения»', async () => {
    const w = boot(driver);
    const box = w.document.getElementById('content');
    await w.reconRenderTab(box);
    await flush();
    expect(box.querySelector('[data-recon-filter]')).toBeNull();
    expect(norm(box.textContent)).toContain('Мои пересчёты');
  });
});
