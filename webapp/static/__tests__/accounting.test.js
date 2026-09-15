// Бухгалтерия (accounting.js): чистые хелперы формы «Получил деньги» и точки
// входа из app.js. Каркас — как у app-load.smoke: helpers.js + app.js +
// accounting.js в одном jsdom-окне. app.js и accounting.js идут ОДНИМ eval:
// верхнеуровневые `let` (currentUser, moneyTab) в отдельном косвенном eval
// друг другу не видны, а в браузере отдельные <script> делят их — так тест
// повторяет браузер, а не спорит с ним.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

function boot(driver = '') {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="content"></div><nav class="bottom-nav" id="bottom-nav"></nav></body>',
    { url: 'https://example.org/', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  const noop = () => {};
  window.Telegram = { WebApp: {
    ready: noop, expand: noop, onEvent: noop, colorScheme: 'light', themeParams: {},
    initData: 'tgWebAppData=stub', initDataUnsafe: {},
    HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
    showAlert: noop, showConfirm: noop, setHeaderColor: noop, setBackgroundColor: noop,
    MainButton: { show: noop, hide: noop, setText: noop, onClick: noop, offClick: noop },
    BackButton: { show: noop, hide: noop, onClick: noop, offClick: noop },
  } };
  window.fetch = () => new Promise(() => {});
  window.eval(read('helpers.js'));
  window.eval(`${read('app.js')}\n${read('accounting.js')}\n${driver}`);
  return window;
}

describe('accounting.js грузится вместе с app.js', () => {
  it('без исключения и с точками входа', () => {
    const w = boot();
    for (const fn of ['accEnabled', 'renderAccTab', 'accDecorateDebts', 'accOpenReceipt', 'accMountToggle']) {
      expect(typeof w[fn]).toBe('function');
    }
  });

  it('подключён в index.html ПОСЛЕ app.js', () => {
    const html = read('index.html');
    expect(html.indexOf('/static/accounting.js')).toBeGreaterThan(html.indexOf('/static/app.js'));
  });
});

describe('хелперы суммы и курса', () => {
  const w = boot();

  it('accMoney: копейки только когда есть, знак минус типографский', () => {
    expect(w.accMoney(762000000, 'UZS').replace(/\s/g, ' ')).toBe('7 620 000 UZS');
    expect(w.accMoney(99999, 'USD').replace(/\s/g, ' ')).toBe('999,99 USD');
    expect(w.accMoney(-1000, 'USD')).toBe('−10 USD');
  });

  it('accParseCents: пробелы, запятая, ноль только когда разрешён', () => {
    expect(w.accParseCents('7 620 000')).toBe(762000000);
    expect(w.accParseCents('1500,50')).toBe(150050);
    expect(w.accParseCents('0')).toBeNull();
    expect(w.accParseCents('0', true)).toBe(0);
    expect(w.accParseCents('abc')).toBeNull();
    expect(w.accParseCents('-5')).toBeNull();
  });

  it('accConvert: сумы в доллары и обратно по курсу «сум за доллар»', () => {
    const q = { UZS: 12700 };
    expect(w.accConvert(762000000, 'UZS', 'USD', q, 'USD')).toBe(60000);
    expect(w.accConvert(50000, 'USD', 'UZS', q, 'USD')).toBe(635000000);
    expect(w.accConvert(100, 'USD', 'USD', q, 'USD')).toBe(100);
    expect(w.accConvert(100, 'UZS', 'USD', {}, 'USD')).toBeNull();
  });

  it('итог «Получил деньги»: две валюты закрывают долг, переплата не проходит', () => {
    const target = { currency: 'USD', available_cents: 100000 };
    const ok = w.accReceiptPreview(
      [{ cents: 40000, currency: 'USD' }, { cents: 762000000, currency: 'UZS' }],
      target, { UZS: 12700 }, 'USD');
    expect(ok).toMatchObject({ total: 100000, left: 0, over: false, valid: true });
    const over = w.accReceiptPreview([{ cents: 110000, currency: 'USD' }], target, {}, 'USD');
    expect(over.over).toBe(true);
    expect(over.valid).toBe(false);
    const noRate = w.accReceiptPreview([{ cents: 100, currency: 'UZS' }], target, {}, 'USD');
    expect(noRate.missingRate).toBe(true);
  });

  it('курс спрашиваем только там, где строка пересчитывается в другую валюту', () => {
    expect(w.accRatesNeeded(['USD', 'UZS'], 'USD', 'USD')).toEqual(['UZS']);
    expect(w.accRatesNeeded(['USD'], 'USD', 'USD')).toEqual([]);
    expect(w.accRatesNeeded(['UZS'], 'UZS', 'USD')).toEqual([]);
    expect(w.accRatesNeeded(['USD'], 'UZS', 'USD')).toEqual(['UZS']);
  });

  it('период журнала считается от серверной даты', () => {
    expect(w.accPeriodSince('today', '2026-09-15')).toBe('2026-09-15');
    expect(w.accPeriodSince('week', '2026-09-15')).toBe('2026-09-09');
    expect(w.accPeriodSince('all', '2026-09-15')).toBe('');
  });
});

describe('точки входа в старые экраны', () => {
  const debtsHtml = `<div class="pay-input-row">
    <input class="pay-amount-input" data-id="42"><button class="btn-mark-paid" data-id="42">Отметить</button></div>`;

  it('выключено — «Долги» остаются как были', () => {
    const w = boot("currentUser = { role: 'manager', accounting_enabled: false };");
    const c = w.document.getElementById('content');
    c.innerHTML = debtsHtml;
    w.accDecorateDebts(c);
    expect(c.querySelector('.btn-mark-paid')).not.toBeNull();
    expect(c.querySelector('.acc-pay')).toBeNull();
  });

  it('включено — вместо суммы и «Отметить» кнопка «Получил деньги»', () => {
    const w = boot("currentUser = { role: 'manager', accounting_enabled: true };");
    const c = w.document.getElementById('content');
    c.innerHTML = debtsHtml;
    w.accDecorateDebts(c);
    expect(c.querySelector('.btn-mark-paid')).toBeNull();
    expect(c.querySelector('.acc-pay[data-acc-order="42"]').textContent).toContain('Получил деньги');
  });

  it('кнопка включения — только руководителю и только пока выключено', () => {
    for (const [user, expected] of [
      ["{ role: 'boss', accounting_enabled: false }", 1],
      ["{ role: 'manager', accounting_enabled: false }", 0],
      ["{ role: 'boss', accounting_enabled: true }", 0],
    ]) {
      const w = boot(`currentUser = ${user};`);
      const c = w.document.getElementById('content');
      w.accMountToggle(c);
      expect(c.querySelectorAll('#acc-enable').length).toBe(expected);
    }
  });

  it('пикер поверх другой формы рисует список в СВОЕЙ шторке', () => {
    const w = boot("currentUser = { role: 'boss' };");
    w.openMachineSheet({ title: 'Форма', fields: [{ key: 'x', label: 'X' }], onSubmit: async () => true });
    w.openListPicker({ title: 'Счёт', items: [{ id: 1, name: 'Касса' }], onPick: () => {} });
    const overlays = w.document.querySelectorAll('.c-overlay');
    expect(overlays.length).toBe(2);
    expect(overlays[0].querySelector('.picker-list')).toBeNull();
    expect(overlays[1].querySelector('.picker-list').textContent).toContain('Касса');
  });
});
