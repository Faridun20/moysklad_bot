// B9 (фото к заказу) и B10 (статус бэкапа в «Настройки») — jsdom-регрессы на
// чистые функции разметки. Полный HTTP-поток (загрузка/удаление/видимость по
// ролям) проверяет tests/test_order_photos.py и tests/test_backup_status_api.py
// на настоящем сервере; здесь — что фронт рисует ровно то, что вернул сервер.
import fs from 'node:fs';
import path from 'node:path';

import { JSDOM } from 'jsdom';
import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

// `ordersData` — `let` на верхнем уровне app.js: отдельный, ПОСЛЕДУЮЩИЙ вызов
// `window.eval(...)` не видит эту лексическую переменную (у indirect eval своя
// декларативная область на каждый вызов, в отличие от <script>) и присваивание
// без объявления создаёт отдельное свойство window, а не трогает биндинг,
// который держат замыкания app.js (`orderPhotosHtml` продолжает видеть старое
// значение). Поэтому, как у `driver` в orders-editor.test.js, установка
// `ordersData` идёт ТЕМ ЖЕ eval'ом, что и сам app.js.
function boot(driver = '') {
  const dom = new JSDOM('<!DOCTYPE html><body><div id="content"></div></body>', {
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

function fragment(window, html) {
  const doc = new window.DOMParser().parseFromString(`<div>${html}</div>`, 'text/html');
  return doc.body.firstElementChild;
}

function ordersDataDriver(value) {
  return `ordersData = ${JSON.stringify(value)};`;
}

describe('лента фото — крестик удаления по каждому снимку отдельно', () => {
  it('data-photo-del рисуется только там, где сервер разрешил (can_delete)', () => {
    const window = boot();
    const html = window.photoStripHtml(
      [{ id: 1, caption: '', can_delete: true }, { id: 2, caption: '', can_delete: false }],
      { addId: 'add-x', canUpload: false, canDelete: (p) => !!p.can_delete, alt: 'Фото' },
    );
    const root = fragment(window, html);
    expect(root.querySelectorAll('[data-photo-del]').length).toBe(1);
    expect(root.querySelector('[data-photo-del="1"]')).not.toBeNull();
    expect(root.querySelector('[data-photo-del="2"]')).toBeNull();
  });

  it('булев canDelete по-прежнему применяется ко всей ленте (техника/товары)', () => {
    const window = boot();
    const html = window.photoStripHtml(
      [{ id: 1 }, { id: 2 }], { addId: 'add-y', canUpload: false, canDelete: true, alt: 'Фото' },
    );
    const root = fragment(window, html);
    expect(root.querySelectorAll('[data-photo-del]').length).toBe(2);
  });
});

describe('orderPhotosHtml — кто видит кнопку «Добавить фото»', () => {
  it('владелец заказа — если хранилище включено', () => {
    const window = boot(ordersDataDriver({ photos_enabled: true }));
    const html = window.orderPhotosHtml({ id: 5, is_mine: true, photos: [] }, false);
    expect(html).toContain('order-photo-add-5');
  });

  it('чужой заказ у менеджера — кнопки нет', () => {
    const window = boot(ordersDataDriver({ photos_enabled: true }));
    const html = window.orderPhotosHtml({ id: 6, is_mine: false, photos: [] }, false);
    expect(html).toBe('');
  });

  it('руководству — любой заказ', () => {
    const window = boot(ordersDataDriver({ photos_enabled: true }));
    const html = window.orderPhotosHtml({ id: 7, is_mine: false, photos: [] }, true);
    expect(html).toContain('order-photo-add-7');
  });

  it('хранилище выключено на сервере — кнопки нет даже владельцу', () => {
    const window = boot(ordersDataDriver({ photos_enabled: false }));
    const html = window.orderPhotosHtml({ id: 8, is_mine: true, photos: [] }, false);
    expect(html).toBe('');
  });

  it('снимки показываются независимо от возможности загрузки', () => {
    const window = boot(ordersDataDriver({ photos_enabled: false }));
    const html = window.orderPhotosHtml(
      { id: 9, is_mine: true, photos: [{ id: 1, caption: 'Расписка', can_delete: false }] }, false,
    );
    expect(html).toContain('Фото');
    expect(html).not.toContain('order-photo-add-9');
  });
});

describe('backupStatusHtml — «Настройки → Резервные копии» (B10)', () => {
  it('без данных — секция не рисуется (менеджеру ручку не дёргаем вовсе)', () => {
    const window = boot();
    expect(window.backupStatusHtml(null)).toBe('');
  });

  it('ни разу не запускался', () => {
    const window = boot();
    const html = window.backupStatusHtml({ ok: true, found: false });
    expect(html).toContain('Копию ещё не делали');
  });

  it('последний запуск успешен', () => {
    const window = boot();
    const html = window.backupStatusHtml({
      ok: true, found: true, status: 'ok',
      started_at: '2026-09-15 03:00:00', finished_at: '2026-09-15 03:00:12', error_message: '',
    });
    expect(html).toContain('успешно');
    expect(html).toContain('2026-09-15 03:00');
  });

  it('последний запуск упал — текст ошибки виден', () => {
    const window = boot();
    const html = window.backupStatusHtml({
      ok: true, found: true, status: 'failed',
      started_at: '2026-09-15 03:00:00', finished_at: '2026-09-15 03:00:02',
      error_message: 'BACKUP_TG_CHAT_ID не задан',
    });
    expect(html).toContain('ошибка');
    expect(html).toContain('BACKUP_TG_CHAT_ID');
  });
});
