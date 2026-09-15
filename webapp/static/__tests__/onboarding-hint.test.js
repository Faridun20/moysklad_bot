// D2 продуктового аудита: на «Сегодня» руководителю без «Рабочих действий»
// показываем ненавязчивую подсказку («Работаете один?..»), не модалку.
// Максимум WORK_ACTIONS_HINT_MAX_SHOWS показов (счётчик — user_prefs,
// persist на сервере). Каркас — как в orders-editor.test.js.
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

describe('workActionsHintVisible — гейт показа', () => {
  const check = (role, prefs) => {
    const window = boot(`
      currentUser = { role: '${role}', prefs: ${JSON.stringify(prefs)} };
      window.__visible = workActionsHintVisible();
    `);
    return window.__visible;
  };

  it('менеджеру — никогда (это подсказка про кнопку руководителя)', () => {
    expect(check('manager', { work_actions: false, work_actions_hint_shown: 0 })).toBe(false);
  });

  it('руководителю с выключенными «Рабочими действиями» и малым счётчиком — да', () => {
    expect(check('boss', { work_actions: false, work_actions_hint_shown: 0 })).toBe(true);
    expect(check('admin', { work_actions: false, work_actions_hint_shown: 2 })).toBe(true);
  });

  it('«Рабочие действия» уже включены — подсказка не нужна', () => {
    expect(check('boss', { work_actions: true, work_actions_hint_shown: 0 })).toBe(false);
  });

  it('счётчик показов достиг предела — подсказка перестаёт показываться', () => {
    expect(check('boss', { work_actions: false, work_actions_hint_shown: 3 })).toBe(false);
    expect(check('boss', { work_actions: false, work_actions_hint_shown: 10 })).toBe(false);
  });
});

describe('«Сегодня»: подсказка о «Рабочих действиях» (D2)', () => {
  const HOME = {
    currency: 'USD', role: 'boss',
    today: { revenue: 0, prev_revenue: 0, shipments: 0, clients: 0, scope: 'company' },
    my_orders: { total: 0, draft: 0, pending: 0, approved: 0, recent: [] },
    top_employees: [],
  };

  const homeDriver = (prefs, role = 'boss') => `
    currentUser = { role: '${role}', prefs: ${JSON.stringify(prefs)} };
    window.__calls = [];
    api = async (p, body) => {
      window.__calls.push([p, body]);
      if (p === '/api/today') return { queue: [] };
      if (p === '/api/home') return ${JSON.stringify({ ...HOME, role })};
      if (p === '/api/currency/rates') return { rates: [] };
      if (p === '/api/prefs/set') return { prefs: { ...${JSON.stringify(prefs)}, work_actions_hint_shown: (body.value) } };
      return { ok: true };
    };
    window.__ready = renderHome();
  `;

  it('выключены — подсказка на экране, и сервер узнаёт о показе', async () => {
    const window = boot(homeDriver({ work_actions: false, work_actions_hint_shown: 0 }));
    await window.__ready;
    await tick();
    const el = window.document.getElementById('work-actions-hint');
    expect(el).not.toBeNull();
    expect(el.textContent).toContain('Работаете один?');
    expect(el.textContent).toContain('Рабочие действия');
    expect(window.__calls).toContainEqual(
      ['/api/prefs/set', { key: 'work_actions_hint_shown', value: 1 }],
    );
  });

  it('включены — подсказки нет, счётчик не трогаем', async () => {
    const window = boot(homeDriver({ work_actions: true, work_actions_hint_shown: 0 }));
    await window.__ready;
    await tick();
    expect(window.document.getElementById('work-actions-hint')).toBeNull();
    expect(window.__calls.some(([p]) => p === '/api/prefs/set')).toBe(false);
  });

  it('менеджеру подсказки не рисуем (не боевая ветка «Сегодня»)', async () => {
    const window = boot(homeDriver({}, 'manager'));
    await window.__ready;
    await tick();
    expect(window.document.getElementById('work-actions-hint')).toBeNull();
  });

  it('счётчик уже на пределе — подсказка молчит', async () => {
    const window = boot(homeDriver({ work_actions: false, work_actions_hint_shown: 3 }));
    await window.__ready;
    await tick();
    expect(window.document.getElementById('work-actions-hint')).toBeNull();
    expect(window.__calls.some(([p]) => p === '/api/prefs/set')).toBe(false);
  });

  it('«Понятно» просто убирает строку — не ведёт в Настройки', async () => {
    const window = boot(homeDriver({ work_actions: false, work_actions_hint_shown: 0 }));
    await window.__ready;
    await tick();
    window.__shown = [];
    // stopPropagation в обработчике дисмисса не должен дать всплыть до клика
    // по всей строке (который и открывает «Настройки»).
    window.showScreen = async (s) => { window.__shown.push(s); };
    window.document.getElementById('work-actions-hint-dismiss').click();
    expect(window.document.getElementById('work-actions-hint')).toBeNull();
    expect(window.__shown).toEqual([]);
  });

  it('тап по строке (не по «Понятно») ведёт в «Настройки»', async () => {
    const window = boot(homeDriver({ work_actions: false, work_actions_hint_shown: 0 }));
    await window.__ready;
    await tick();
    window.__shown = [];
    window.showScreen = async (s) => { window.__shown.push(s); };
    window.document.getElementById('work-actions-hint').click();
    await tick();
    expect(window.__shown).toEqual(['settings']);
  });
});
