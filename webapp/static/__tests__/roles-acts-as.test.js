// Временное совмещение ролей: менеджер = + кладовщик + бухгалтер (решение
// владельца; сервер — services/roles.py::ROLE_ALSO_ACTS_AS). Фронт обязан
// рисовать менеджеру кнопки и вкладки этих ролей: сервер его в ручки пускает, и
// без кнопки работа просто некуда нажать. Отдельным файлом — при откате
// совмещения он удаляется целиком.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

import helpers from '../helpers.js';

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
  window.eval(`${read('app.js')}\n${driver}`);
  return window;
}

const { roleIn, ROLE_ALSO_ACTS_AS } = helpers;

describe('roleIn', () => {
  it('менеджер проходит в списки кладовщика и бухгалтера', () => {
    expect(ROLE_ALSO_ACTS_AS.manager).toEqual(['warehouse_keeper', 'bookkeeper']);
    expect(roleIn('manager', ['warehouse_keeper'])).toBe(true);
    expect(roleIn('manager', ['bookkeeper'])).toBe(true);
  });

  it('но не в руководские, и совмещение одностороннее', () => {
    expect(roleIn('manager', ['admin', 'boss'])).toBe(false);
    expect(roleIn('warehouse_keeper', ['manager'])).toBe(false);
    expect(roleIn('guest', ['admin', 'boss', 'manager'])).toBe(false);
    expect(roleIn('boss', null)).toBe(false);
  });
});

describe('менеджер в интерфейсе', () => {
  it('в «Деньгах» есть «Подтвердить», но нет руководского «Отчёта»', () => {
    const window = boot(`
      currentUser = { role: 'manager' };
      window.__tabs = sectionTabsFor('money').map(t => t.key);
    `);
    expect(window.__tabs).toEqual(['confirm', 'debts', 'ops']);
  });

  it('у одобренного заказа есть «Отгрузить», «Отменить» — нет', () => {
    const order = {
      id: 9, status: 'approved', full_name: 'Manager2', agent_name: 'ООО Ромашка', comment: '',
      currency: 'USD', payment_type: 'credit', due_date: '2030-01-15',
      created_at: '2030-01-01 10:00', items_count: 1, total: 100, frozen: false,
      items: [{ id: 1, name: 'Кабель', quantity: 1, unit: 'м', price: 100 }],
    };
    const window = boot(`
      currentUser = { role: 'manager' };
      ordersData = { orders: [${JSON.stringify(order)}], role: 'manager' };
      renderOrdersMain();
    `);
    const content = window.document.getElementById('content');
    expect(content.querySelector('.btn-ship-order[data-id="9"]')).not.toBeNull();
    expect(content.querySelector('.btn-cancel-order')).toBeNull();
  });
});
