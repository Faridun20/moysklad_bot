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

describe('под-вкладки Заказы/Каталог переживают ре-рендер (UI-BUG-04)', () => {
  // Регресс: шелл вставлялся поверх готового DOM через insertAdjacentHTML, и
  // любой полный ре-рендер внутри вкладки (смена статуса, выбор периода,
  // ошибка сети) переписывал innerHTML и уносил его вместе с обработчиками —
  // пользователь не мог уйти в Каталог, не выходя из раздела.
  // Драйвер выполняем В ТОМ ЖЕ eval, что и app.js: его состояние объявлено
  // через `let` на верхнем уровне скрипта, а такие привязки в область
  // следующего eval не попадают — снаружи их не выставить.
  const boot = (driver = '') => {
    const window = makeWindow();
    window.eval(read('helpers.js'));
    window.eval(`${read('app.js')}\n${driver}`);
    return window;
  };

  it('шелл входит в шаблон, а не накладывается сверху', () => {
    const window = boot();
    const html = window.ordersShellHtml();
    expect(html).toContain('data-sub="orders"');
    expect(html).toContain('data-sub="stock"');
  });

  it('после полного ре-рендера списка заказов вкладки на месте', () => {
    const window = boot('ordersData = { orders: [], role: "manager" }; renderOrdersMain();');
    const content = window.document.getElementById('content');
    expect(content.querySelector('[data-sub="stock"]')).not.toBeNull();
    expect(content.querySelector('[data-sub="orders"]')).not.toBeNull();
  });

  it('вкладки остаются даже когда экран показывает ошибку сети', () => {
    const window = boot();
    const content = window.document.getElementById('content');
    content.innerHTML = window.ordersShellHtml() + window.errorBox('Нет подключения к интернету');
    expect(content.querySelector('[data-sub="orders"]')).not.toBeNull();
    expect(content.textContent).toContain('Нет подключения');
  });
});
