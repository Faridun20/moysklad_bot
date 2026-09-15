// История заказа (C3) на карточке в списке «Заказы»: разворачивается по
// нажатию, грузит /api/orders/timeline один раз, повторное нажатие сворачивает
// БЕЗ повторного запроса. Каркас как у orders-editor.test.js.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

function boot(driver = '') {
  const dom = new JSDOM(
    '<!DOCTYPE html><body><div id="content">'
    + '<div id="order-timeline-5" hidden></div>'
    + '</div><nav class="bottom-nav" id="bottom-nav"></nav></body>', {
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

const EVENTS = [
  { ts: '2026-01-01 10:00', actor: 'Менеджер Иван', action: 'order_created', text: 'Заказ создан' },
  { ts: '2026-01-01 11:00', actor: 'Руководитель Пётр', action: 'shipment_approved', text: 'Заявка на отгрузку одобрена' },
];

describe('История заказа', () => {
  it('по нажатию грузит и показывает события', async () => {
    const window = boot(`
      window.__calls = [];
      api = async (path, body) => { window.__calls.push([path, body]); return { order_id: 5, events: ${JSON.stringify(EVENTS)} }; };
    `);
    await window.openOrderTimeline(5);
    await tick();
    expect(window.__calls[0][0]).toBe('/api/orders/timeline');
    expect(window.__calls[0][1].order_id).toBe(5);
    const box = window.document.getElementById('order-timeline-5');
    expect(box.hidden).toBe(false);
    expect(box.innerHTML).toContain('Заказ создан');
    expect(box.innerHTML).toContain('Руководитель Пётр');
  });

  it('повторное нажатие сворачивает без нового запроса', async () => {
    const window = boot(`
      window.__calls = [];
      api = async (path, body) => { window.__calls.push([path, body]); return { order_id: 5, events: ${JSON.stringify(EVENTS)} }; };
    `);
    await window.openOrderTimeline(5);
    await tick();
    await window.openOrderTimeline(5);
    await tick();
    expect(window.__calls.length).toBe(1);
    expect(window.document.getElementById('order-timeline-5').hidden).toBe(true);
  });

  it('пустая лента — понятный текст, а не пустота', async () => {
    const window = boot(`
      api = async () => ({ order_id: 5, events: [] });
    `);
    await window.openOrderTimeline(5);
    await tick();
    expect(window.document.getElementById('order-timeline-5').innerHTML).toContain('Событий пока нет');
  });

  it('ошибка сети — сообщение, а не молчание', async () => {
    const window = boot(`
      api = async () => { throw new Error('нет связи'); };
    `);
    await window.openOrderTimeline(5);
    await tick();
    expect(window.document.getElementById('order-timeline-5').innerHTML).toContain('нет связи');
  });
});
