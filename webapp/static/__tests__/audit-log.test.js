// Журнал действий (C1, «Настройки → Журнал действий»): рендер ленты, перевод
// кода действия в ярлык (пришедший с сервера — фронт его не переводит сам,
// только показывает), пагинация «Показать ещё», фильтр по периоду шлёт
// date_from/date_to. Каркас как у orders-editor.test.js: helpers.js + net.js +
// app.js + audit_log.js в одном окне, драйвер в том же eval.
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
  window.eval(`${read('app.js')}\n${read('audit_log.js')}\n${driver}`);
  return window;
}

const tick = () => new Promise(r => setTimeout(r, 0));

const ENTRY = {
  id: 1, created_at: '2026-01-10 09:30', user_id: 3, full_name: 'Менеджер Иван',
  role: 'manager', action: 'payment_sent', action_label: 'Платёж внесён',
  details: 'Платёж №1: 100 USD',
};

function driverWith({ entries = [ENTRY], total = 1, hasMore = false, users = [] } = {}) {
  return `
    currentUser = { role: 'boss' };
    currentScreen = 'settings';
    window.__calls = [];
    api = async (path, body) => {
      window.__calls.push([path, body]);
      return { entries: ${JSON.stringify(entries)}, total: ${total}, offset: body.offset || 0,
                limit: body.limit, has_more: ${hasMore}, users: ${JSON.stringify(users)} };
    };
    window.__ready = renderAuditLogScreen();
  `;
}

describe('Журнал действий', () => {
  it('показывает ярлык действия и детали записи', async () => {
    const window = boot(driverWith());
    await window.__ready;
    await tick();
    const html = window.document.getElementById('content').innerHTML;
    expect(html).toContain('Платёж внесён');
    expect(html).toContain('Платёж №1: 100 USD');
    expect(html).toContain('Менеджер Иван');
  });

  it('первый запрос уходит без фильтра пользователя и с периодом «Всё»', async () => {
    const window = boot(driverWith());
    await window.__ready;
    await tick();
    const [path, body] = window.__calls[0];
    expect(path).toBe('/api/audit_log');
    expect(body.user_id).toBeUndefined();
    expect(body.date_from).toBe('');
  });

  it('«Показать ещё» запрашивает следующую страницу с offset', async () => {
    const window = boot(driverWith({ hasMore: true, total: 2 }));
    await window.__ready;
    await tick();
    const more = window.document.getElementById('audit-log-more');
    expect(more).toBeTruthy();
    more.click();
    await tick();
    const last = window.__calls[window.__calls.length - 1];
    expect(last[1].offset).toBe(1);
  });

  it('фильтр периода «Сегодня» шлёт date_from=date_to=сегодня', async () => {
    const window = boot(driverWith());
    await window.__ready;
    await tick();
    window.document.querySelector('[data-alperiod="today"]').click();
    await tick();
    const last = window.__calls[window.__calls.length - 1];
    // Тот же расчёт, что у _ymd в app.js (локальная дата, не UTC) — иначе
    // тест плавает на часовых поясах вокруг полуночи.
    const now = new Date();
    const p = (n) => String(n).padStart(2, '0');
    const today = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
    expect(last[1].date_from).toBe(today);
    expect(last[1].date_to).toBe(today);
  });

  it('неизвестный код действия показывается как есть, не прячется', async () => {
    const window = boot(driverWith({
      entries: [{ ...ENTRY, action: 'brand_new_code', action_label: 'brand_new_code' }],
    }));
    await window.__ready;
    await tick();
    expect(window.document.getElementById('content').innerHTML).toContain('brand_new_code');
  });

  it('пустой список — понятная подсказка, а не голый экран', async () => {
    const window = boot(driverWith({ entries: [], total: 0 }));
    await window.__ready;
    await tick();
    expect(window.document.getElementById('content').innerHTML).toContain('Нет записей');
  });
});
