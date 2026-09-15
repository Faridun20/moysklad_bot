// «Как получены деньги» (helpers.js + payments.js): итог разбивки, подписи
// ожидающей оплаты, форма перед отгрузкой, карточки «Долги» и «Касса».
// Каркас как у audit-front: helpers.js + net.js + app.js + payments.js в одном
// окне; app.js и payments.js — ОДНИМ eval (верхнеуровневые `let` видны обоим).
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');
const H = require('../helpers.js');

function boot(driver = '') {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="content"></div><nav class="bottom-nav" id="bottom-nav"></nav></body>',
    { url: 'https://example.org/', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  const noop = () => {};
  window.__alerts = [];
  window.Telegram = { WebApp: {
    ready: noop, expand: noop, onEvent: noop, colorScheme: 'light', themeParams: {},
    initData: 'tgWebAppData=stub', initDataUnsafe: { user: { id: 42 } },
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
  window.eval(`${read('app.js')}\n${read('payments.js')}\n${driver}`);
  return window;
}

const flush = () => new Promise(r => setTimeout(r, 0));
const norm = (s) => String(s).replace(/[\s  ]+/g, ' ');

describe('payments.js подключён', () => {
  it('в index.html после app.js, точки входа на месте', () => {
    const html = read('index.html');
    expect(html.indexOf('/static/payments.js')).toBeGreaterThan(html.indexOf('/static/app.js'));
    const w = boot();
    for (const fn of ['payOpenForm', 'payShipOrOpenForm', 'payPreview', 'payAwaitingText']) {
      expect(typeof w[fn]).toBe('function');
    }
  });
});

describe('итог разбивки (payPreview) — та же арифметика, что на сервере', () => {
  const USD_ORDER = { currency: 'USD', base_currency: 'USD', due_cents: 1213000, exact: true, cbu: { UZS: '12700' } };

  it('5 000 наличными + 7 130 картой закрывают 12 130 ровно', () => {
    const pv = H.payPreview([
      { method: 'cash', currency: 'USD', amount: '5 000' },
      { method: 'card', currency: 'USD', amount: '7130' },
    ], USD_ORDER);
    expect(pv.total).toBe(1213000);
    expect(pv.valid).toBe(true);
    expect(pv.tolerance).toBe(0);
  });

  it('оплата сразу: недостача не проходит, остаток назван', () => {
    const pv = H.payPreview([{ method: 'cash', currency: 'USD', amount: '12000' }], USD_ORDER);
    expect(pv.short).toBe(true);
    expect(pv.valid).toBe(false);
    expect(pv.left).toBe(13000);
  });

  it('в долг: часть законна, больше остатка — нет', () => {
    const credit = { ...USD_ORDER, exact: false };
    expect(H.payPreview([{ method: 'bank', currency: 'USD', amount: '100' }], credit).valid).toBe(true);
    expect(H.payPreview([{ method: 'bank', currency: 'USD', amount: '12131' }], credit).over).toBe(true);
  });

  it('сумы по курсу: 90 551 000 UZS при 12 700 = 7 130 USD; без курса — просьба указать', () => {
    const pv = H.payPreview([
      { method: 'cash', currency: 'USD', amount: '5000' },
      { method: 'card', currency: 'UZS', amount: '90551000' },
    ], USD_ORDER);
    expect(pv.total).toBe(1213000);
    expect(pv.valid).toBe(true);
    const noRate = H.payPreview([{ method: 'card', currency: 'UZS', amount: '1000' }], { ...USD_ORDER, cbu: {} });
    expect(noRate.missingRate).toBe(true);
  });

  it('копейки пересчёта в пределах допуска (1 USD) не блокируют', () => {
    const pv = H.payPreview([
      { method: 'cash', currency: 'USD', amount: '5000' },
      { method: 'card', currency: 'UZS', amount: '90550000', rate: '12700' },
    ], USD_ORDER);
    expect(pv.total).toBe(1212992);
    expect(pv.tolerance).toBe(100);
    expect(pv.valid).toBe(true);
  });

  it('сумовой заказ, оплаченный долларами: курс заказа, допуск в сумах', () => {
    const uzs = { currency: 'UZS', base_currency: 'USD', due_cents: 1270000000, exact: true, cbu: { UZS: '12700' } };
    const pv = H.payPreview([{ method: 'cash', currency: 'USD', amount: '1000' }], uzs);
    expect(pv.total).toBe(1270000000);
    expect(pv.tolerance).toBe(1270000);
    expect(pv.valid).toBe(true);
  });
});

describe('подпись ожидающей оплаты в «Долгах»', () => {
  it('«Ждёт 12к / Осталось 0» больше не встречается — одна фраза с причиной', () => {
    const t = norm(H.payAwaitingText({ currency: 'USD', pending: 12130, remaining: 12130, remaining_after_pending: 0 }));
    expect(t).toBe('Оплата 12 130 USD ждёт подтверждения · после подтверждения долг: 0 USD');
  });

  it('частичная оплата: после подтверждения остаётся долг', () => {
    const t = norm(H.payAwaitingText({ currency: 'USD', pending: 5000, remaining: 12130, remaining_after_pending: 7130 }));
    expect(t).toContain('после подтверждения долг: 7 130 USD');
  });

  it('после возврата ждущее больше долга — это сказано', () => {
    const t = norm(H.payAwaitingText({ currency: 'USD', pending: 1000, remaining: 600, remaining_after_pending: 0, overpending: 400 }));
    expect(t).toContain('из них 400 USD сверх долга');
  });

  it('строка разбивки: способ, сумма в своей валюте, состояние', () => {
    expect(norm(H.payPartLine({ method: 'cash', amount_cents: 500000, currency: 'USD', state: 'on_hand' })))
      .toBe('наличные 5 000 USD — у менеджера, ждут сдачи в кассу');
    expect(norm(H.payPartLine({ method: 'card', amount_cents: 713000, currency: 'USD', state: 'awaiting_bank' })))
      .toBe('на карту 7 130 USD — ждёт проверки банка');
  });

  it('сдача показывает заказы с валютой: «#27 — 5 000 USD»', () => {
    expect(norm(H.payDepositOrdersText([{ order_id: 27, amount_allocated: 5000, currency: 'USD' }]))).toBe('#27 — 5 000 USD');
    expect(H.payDepositOrdersText([])).toBe('—');
  });
});

const DEBT_AWAITING = {
  role: 'manager', scope: 'personal', today: '2026-09-15', can_confirm: true,
  confirm_hint: 'подтверждаете вы — руководителя и бухгалтера в системе нет',
  debts: [{
    id: 27, state: 'awaiting_confirmation', agent_name: 'ООО Ромашка', total: 12130, remaining: 12130,
    confirmed: 0, pending: 12130, pending_cash: 5000, pending_confirmable: 7130,
    remaining_after_pending: 0, overpending: 0, claimable: 0,
    currency: 'USD', due_date: '2026-09-15', items_count: 1, is_mine: true, full_name: 'М',
    parts: [
      { method: 'cash', amount_cents: 500000, currency: 'USD', state: 'on_hand' },
      { method: 'card', amount_cents: 713000, currency: 'USD', state: 'awaiting_bank' },
    ],
  }],
  money_received: [], money_pending: [{ currency: 'USD', total: 12130 }], remaining_by_currency: [],
  machine_debts: [], totals: null,
};

describe('карточка долга', () => {
  it('ждёт подтверждения: однозначная фраза, разбивка и кто подтверждает', async () => {
    const w = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      api = async (p) => (p === '/api/debts' ? ${JSON.stringify(DEBT_AWAITING)} : {});
      window.__ready = renderDebts(document.getElementById('content'));
    `);
    await w.__ready;
    const text = norm(w.document.getElementById('content').textContent);
    expect(text).toContain('Оплата 12 130 USD ждёт подтверждения · после подтверждения долг: 0 USD');
    expect(text).not.toMatch(/Останется/);
    expect(text).toContain('наличные 5 000 USD — у менеджера, ждут сдачи в кассу');
    expect(text).toContain('руководителя и бухгалтера в системе нет');
    // Кнопка подтверждает только карту — наличные подтверждает сдача.
    expect(norm(w.document.querySelector('.btn-confirm-pay').textContent)).toContain('7 130 USD');
  });

  it('открытый долг: «Внести оплату» открывает форму разбивки, а не голую сумму', async () => {
    const debts = {
      ...DEBT_AWAITING,
      debts: [{ ...DEBT_AWAITING.debts[0], state: 'upcoming', pending: 0, parts: [], claimable: 3000, remaining: 3000, total: 3000 }],
    };
    const w = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      window.__calls = [];
      api = async (p, b) => {
        window.__calls.push([p, b]);
        if (p === '/api/debts') return ${JSON.stringify(debts)};
        if (p === '/api/orders/payment_context') return {
          order_id: 27, agent_name: 'ООО Ромашка', status: 'shipped', payment_type: 'credit', currency: 'USD',
          total_cents: 300000, due_cents: 300000, exact: false, base_currency: 'USD', currencies: ['USD', 'UZS'],
          cbu: { UZS: '12700' }, parts: [], open: true,
        };
        return {};
      };
      window.__ready = renderDebts(document.getElementById('content'));
    `);
    await w.__ready;
    expect(w.document.querySelector('.btn-mark-paid')).toBeNull();
    w.document.querySelector('.btn-pay-debt').click();
    await flush(); await flush();
    const sheet = w.document.querySelector('.c-overlay');
    expect(sheet).not.toBeNull();
    expect(norm(sheet.textContent)).toContain('Как клиент заплатил');
    // Предзаполнено всей суммой — «Записать» доступна.
    expect(sheet.querySelector('.pay-part-amount').value).toBe('3000');
    expect(sheet.querySelector('#ms-submit').disabled).toBe(false);
  });
});

describe('форма перед отгрузкой «оплаты сразу»', () => {
  const CTX = {
    order_id: 27, agent_name: 'ООО Ромашка', status: 'approved', payment_type: 'paid', currency: 'USD',
    total_cents: 1213000, due_cents: 1213000, exact: true, base_currency: 'USD', currencies: ['USD', 'UZS'],
    cbu: { UZS: '12700' }, parts: [], open: true,
  };

  it('5 000 наличными + 7 130 картой → запись разбивки, затем отгрузка', async () => {
    const w = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      window.__calls = [];
      const answer = async (p, b) => {
        window.__calls.push([p, b]);
        if (p === '/api/orders/payment_context') return ${JSON.stringify(CTX)};
        if (p === '/api/orders/payment') return { ok: true, total_cents: 1213000, parts: [] };
        if (p === '/api/orders/ship') return { ok: true };
        return {};
      };
      api = answer;
      apiResult = async (p, b) => ({ ok: true, status: 200, body: await answer(p, b) });
      window.__done = 0;
      window.__ready = payOpenForm({ orderId: 27, ship: true, onDone: () => { window.__done += 1; } });
    `);
    await w.__ready;
    const doc = w.document;
    const amount = () => doc.querySelectorAll('.pay-part-amount');
    amount()[0].value = '5000';
    amount()[0].dispatchEvent(new w.Event('input'));
    expect(doc.querySelector('#ms-submit').disabled).toBe(true);  // не хватает 7 130
    expect(norm(doc.querySelector('.pay-total').textContent)).toContain('Осталось внести: 7 130 USD');
    doc.querySelector('.pay-add-part').click();
    expect(amount()[1].value).toBe('7130');                        // остаток подставлен
    expect(doc.querySelectorAll('.pay-part')[1].querySelector('[data-pay-method="card"]').classList.contains('active')).toBe(true);
    doc.querySelector('#ms-submit').click();
    await flush(); await flush(); await flush();
    const pay = w.__calls.find(c => c[0] === '/api/orders/payment');
    expect(pay[1].parts).toEqual([
      { method: 'cash', currency: 'USD', amount: '5000' },
      { method: 'card', currency: 'USD', amount: '7130' },
    ]);
    expect(pay[1].idempotency_key).toBeTruthy();
    expect(w.__calls.some(c => c[0] === '/api/orders/ship')).toBe(true);
    expect(w.__alerts.some(a => a.includes('отгружен'))).toBe(true);
    expect(w.__done).toBe(1);
  });

  it('строка в сумах показывает курс ЦБ и отправляет его', async () => {
    const w = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      window.__calls = [];
      api = async (p) => (${JSON.stringify(CTX)});
      apiResult = async (p, b) => { window.__calls.push([p, b]); return { ok: true, status: 200, body: { ok: true, total_cents: 1213000 } }; };
      window.__ready = payOpenForm({ orderId: 27 });
    `);
    await w.__ready;
    const doc = w.document;
    doc.querySelector('[data-pay-cur="UZS"]').click();
    const rate = doc.querySelector('.pay-part-rate');
    expect(rate.value).toBe('12700');
    const amt = doc.querySelector('.pay-part-amount');
    amt.value = '154051000';
    amt.dispatchEvent(new w.Event('input'));
    expect(doc.querySelector('#ms-submit').disabled).toBe(false);
    doc.querySelector('#ms-submit').click();
    await flush(); await flush();
    const [, body] = w.__calls.find(c => c[0] === '/api/orders/payment');
    expect(body.parts).toEqual([{ method: 'cash', currency: 'UZS', amount: '154051000', rate: '12700' }]);
  });

  it('отгрузка без оплаты: сервер ответил payment_required — открылась форма', async () => {
    const w = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      api = async (p) => (${JSON.stringify(CTX)});
      apiResult = async (p) => (p === '/api/orders/ship'
        ? { ok: false, status: 409, body: { code: 'payment_required', detail: 'сначала оплата' }, error: 'сначала оплата' }
        : { ok: true, status: 200, body: {} });
      window.__ready = payShipOrOpenForm(27, () => {});
    `);
    await w.__ready;
    await flush();
    expect(norm(w.document.querySelector('.c-overlay').textContent)).toContain('Оплата перед отгрузкой');
  });
});

describe('«Касса»: сдача наличных по заказам', () => {
  const ON_HAND = {
    ok: true, base_currency: 'USD', currencies: ['USD', 'UZS'],
    by_currency: [{ currency: 'USD', amount_cents: 600000, amount: 6000 }],
    orders: [
      { order_id: 27, currency: 'USD', amount_cents: 500000, amount: 5000, agent_name: 'Ромашка', since: '2026-09-15' },
      { order_id: 31, currency: 'USD', amount_cents: 100000, amount: 1000, agent_name: 'Лютик', since: '2026-09-15' },
    ],
  };

  it('на руках по заказам, сумма по отмеченным, сдача уходит с валютой и выбором', async () => {
    const w = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      window.__calls = [];
      api = async (p, b) => {
        window.__calls.push([p, b]);
        if (p === '/api/deposits/on_hand') return ${JSON.stringify(ON_HAND)};
        if (p === '/api/deposits/my') return { deposits: [] };
        if (p === '/api/deposits/create') return { ok: true, deposit_id: 9, currency: 'USD', orders: [{ order_id: 31, amount_allocated: 1000, currency: 'USD' }], unallocated: 0 };
        return {};
      };
      renderMoneyScreen = async () => {};
      window.__ready = renderCashbox(document.getElementById('content'), 'ops');
    `);
    await w.__ready;
    const doc = w.document;
    expect(norm(doc.querySelector('.pay-handover').textContent)).toContain('Заказ #27');
    expect(doc.getElementById('dep-amount').value).toBe('6000');
    doc.querySelector('.dep-order[data-order="27"]').click();   // сдаю только по #31
    expect(doc.getElementById('dep-amount').value).toBe('1000');
    doc.getElementById('dep-create').click();
    await flush(); await flush();
    const [, body] = w.__calls.find(c => c[0] === '/api/deposits/create');
    expect(body.currency).toBe('USD');
    expect(body.order_ids).toEqual([31]);
    expect(body.amount).toBe(1000);
  });

  it('карточка сдачи на подтверждении: заказы с валютой и пометка «ваша сдача»', async () => {
    const w = boot(`
      currentUser = { role: 'manager', user_id: 42 };
      api = async (p) => {
        if (p === '/api/deposits/pending') return { confirmers_exist: false, deposits: [{ id: 3, amount: 5000, currency: 'USD', is_own: true,
          orders: [{ order_id: 27, amount_allocated: 5000, currency: 'USD' }], manager_name: 'М' }] };
        return { returns: [], pending: [] };
      };
      window.__ready = renderCashbox(document.getElementById('content'), 'confirm');
    `);
    await w.__ready;
    const text = norm(w.document.getElementById('content').textContent);
    expect(text).toContain('Заказы: #27 — 5 000 USD');
    expect(text).toContain('Это ваша сдача');
  });
});
