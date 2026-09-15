// Глобальный поиск (A1/A2/A3 из продуктового аудита):
//   A1 — русские подписи статусов заказа/платежа вместо сырых кодов;
//   A2 — новые группы результатов (каталог/контейнеры/техника/лиды);
//   A3 — карточка контрагента открыта на чтение и менеджеру.
// Каркас — как у orders-editor.test.js: helpers.js + app.js в одном eval,
// `api` переопределяется драйвером в том же top-level scope.
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

// Полный ответ /api/search — все группы сразу, чтобы один рендер проверял всё.
const RESPONSE = {
  ok: true,
  orders: [{
    id: 7, status: 'approved', agent_name: 'ООО Ромашка', full_name: 'Менеджер',
    currency: 'USD', created_at: '2030-01-01 10:00',
  }],
  payments: [{
    id: 3, amount: 100, currency: 'USD', status: 'pending',
    full_name: 'ООО Ромашка', comment: '', order_id: 7,
  }],
  agents: [{ id: 'a1', name: 'ООО Ромашка', phone: '+998901234567' }],
  products: [{ id: 1, name: 'Кабель ВВГ', sku: 'VVG-3x2.5', unit: 'м', quantity: 120.5 }],
  containers: [{ id: 2, number: 'MSCU1234567', status: 'in_transit', eta_date: '2030-02-01', arrived_at: null }],
  machines: [{ id: 9, vin: 'JCB123456', name: 'Экскаватор JCB', status: 'in_stock' }],
  leads: [{ id: 4, display_name: 'Азиз', username: 'aziz01', status: 'new' }],
  container_status_labels: { in_transit: 'В пути', arrived: 'Прибыл' },
  machine_status_labels: { in_stock: 'На складе', reserved: 'Забронирован' },
  lead_status_labels: { new: '🆕 В работе', won: '✅ Купил', lost: '🚫 Не купил' },
};

function searchDriver(role, response) {
  return `
    currentUser = { role: '${role}' };
    api = async (path, body) => {
      window.__calls = window.__calls || [];
      window.__calls.push([path, body]);
      return ${JSON.stringify(response)};
    };
    openSearch();
    // runSearch сверяет query с текущим значением поля (защита от гонки при
    // смене запроса) — без этого ответ молча отбрасывается.
    document.getElementById('search-input').value = 'ромашка';
    window.__ready = runSearch('ромашка');
  `;
}

describe('глобальный поиск: рендер результатов', () => {
  it('A1: статус заказа и платежа — русский текст, не сырой код', async () => {
    const window = boot(searchDriver('boss', RESPONSE));
    await window.__ready;
    const html = window.document.getElementById('search-results').innerHTML;
    // Заказ approved → «Одобрено» (STATUS_NAME), не голое 'approved'.
    expect(html).toContain('Одобрено');
    expect(html).not.toMatch(/>\s*approved\s*</);
    // Платёж pending → человеческая подпись, не сырой код.
    expect(html).toContain('Ожидает подтверждения');
    expect(html).not.toMatch(/>\s*pending\s*</);
  });

  it('A2: каталог/контейнеры/техника/лиды — свои группы с подписями с сервера', async () => {
    const window = boot(searchDriver('boss', RESPONSE));
    await window.__ready;
    const html = window.document.getElementById('search-results').innerHTML;

    expect(html).toContain('Каталог');
    expect(html).toContain('Кабель ВВГ');
    expect(html).toContain('VVG-3x2.5');

    expect(html).toContain('Контейнеры');
    expect(html).toContain('MSCU1234567');
    expect(html).toContain('В пути'); // container_status_labels.in_transit

    expect(html).toContain('Техника');
    expect(html).toContain('Экскаватор JCB');
    expect(html).toContain('JCB123456');
    expect(html).toContain('На складе'); // machine_status_labels.in_stock

    expect(html).toContain('Лиды');
    expect(html).toContain('Азиз');
    expect(html).toContain('🆕 В работе'); // lead_status_labels.new
  });

  it('пустой ответ по всем новым группам — не рисует их секции', async () => {
    const empty = {
      ok: true, orders: [], payments: [], agents: [],
      products: [], containers: [], machines: [], leads: [],
    };
    const window = boot(searchDriver('boss', empty));
    await window.__ready;
    const html = window.document.getElementById('search-results').innerHTML;
    expect(html).toContain('Ничего не найдено');
  });

  it('A3: менеджер тоже видит клиента кликабельным (data-agent), не просто строкой', async () => {
    const window = boot(searchDriver('manager', RESPONSE));
    await window.__ready;
    const box = window.document.getElementById('search-results');
    const clickable = box.querySelector('.search-item[data-agent="a1"]');
    expect(clickable).not.toBeNull();
  });
});
