// Smoke: app.js + helpers.js загружаются вместе в браузер-подобном окружении
// (jsdom) без исключения, и чистые хелперы доступны как глобалы — ровно как в
// index.html (helpers.js подключается ПЕРЕД app.js). Это страхует рефакторинг
// «вынес хелперы в helpers.js»: если порядок/глобалы сломаются — тест упадёт.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

function makeWindow() {
  const dom = new JSDOM('<!DOCTYPE html><body><div id="content"></div></body>', {
    url: 'https://example.org/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const noop = () => {};
  // Telegram WebApp SDK — app.js на верхнем уровне зовёт tg.ready/expand/onEvent/…
  window.Telegram = {
    WebApp: {
      ready: noop,
      expand: noop,
      onEvent: noop,
      colorScheme: 'light',
      themeParams: {},
      initData: 'tgWebAppData=stub', // непустой → init() идёт сразу к fetch, без таймера
      initDataUnsafe: {},
      HapticFeedback: { impactOccurred: noop, notificationOccurred: noop },
      showAlert: noop,
      showConfirm: noop,
      setHeaderColor: noop,
      setBackgroundColor: noop,
      MainButton: { show: noop, hide: noop, setText: noop, onClick: noop, offClick: noop },
      BackButton: { show: noop, hide: noop, onClick: noop, offClick: noop },
    },
  };
  // fetch никогда не резолвится → init() повисает на await, без синхронного throw.
  window.fetch = () => new Promise(() => {});
  return window;
}

describe('загрузка фронта (helpers.js + app.js)', () => {
  it('грузятся без исключения и определяют глобальные хелперы', () => {
    const window = makeWindow();

    // Порядок как в index.html: helpers.js ПЕРЕД app.js.
    expect(() => window.eval(read('helpers.js'))).not.toThrow();
    expect(() => window.eval(read('app.js'))).not.toThrow();

    expect(typeof window.escapeHtml).toBe('function');
    expect(typeof window.idemKey).toBe('function');
    expect(typeof window.formatDateRU).toBe('function');
    // Хелпер реально работает в браузерном scope.
    expect(window.escapeHtml('<x>')).toBe('&lt;x&gt;');
  });

  it('app.js без helpers.js НЕ имеет escapeHtml (доказывает зависимость порядка)', () => {
    const window = makeWindow();
    expect(() => window.eval(read('app.js'))).not.toThrow();
    // escapeHtml вынесён в helpers.js — без него глобал не определён.
    expect(window.escapeHtml).toBeUndefined();
  });
});
