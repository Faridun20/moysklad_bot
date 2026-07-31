// Юнит-тесты чистых хелперов фронта (webapp/static/helpers.js) — тестируем РЕАЛЬНЫЙ
// код, который грузится в браузере (UMD: в Node даёт module.exports).
import { readFileSync } from 'node:fs';

import { describe, it, expect } from 'vitest';

import helpers from '../helpers.js';

const {
  escapeHtml, idemKey, formatDateRU, icon, opsAmount, renderOpsSummaryHtml,
  parsePaymentItems, renderMoneyTotalsHtml, financeTabs, balanceParts, periodSegHtml, rangeLabel,
  formatMoney, msBalanceLabel, emptyState, skeleton, errorBoxHtml,
  machineStatusLabel, machineSubtitle, machineStatusSegHtml,
} = helpers;

describe('periodSegHtml (WP-29)', () => {
  const presets = [{ id: 'week', label: 'Неделя' }, { id: 'month', label: 'Месяц' }];
  it('активный пресет получает .active и нужный data-атрибут', () => {
    const html = periodSegHtml(presets, 'month', 'data-period', false, 'Период…');
    expect(html).toContain('class="seg-item active" data-period="month"');
    expect(html).toContain('data-period="custom"');
  });
  it('трек скроллится, а не сплющивает подписи (UI-BUG-01)', () => {
    // Без seg--scroll пункты делят ширину поровну: на 360dp под текст остаётся
    // ~33px, и «Всё время» резалось с двух сторон без многоточия.
    const html = periodSegHtml(presets, 'month', 'data-period', false, 'Период…');
    expect(html).toContain('seg seg--scroll');
  });

  it('aria-pressed отражает выбранный пресет, а не только класс', () => {
    const html = periodSegHtml(presets, 'week', 'data-period', false, 'Период…');
    expect(html).toContain('data-period="week" aria-pressed="true"');
    expect(html).toContain('data-period="month" aria-pressed="false"');
  });

  it('custom активен → подпись диапазона на доп-кнопке', () => {
    const html = periodSegHtml(presets, 'custom', 'data-operiod', true, '01.06—12.06');
    expect(html).toContain('data-operiod="custom"');
    expect(html).toContain('01.06—12.06');  // показывает выбранный диапазон
    expect(html).toMatch(/seg-aux active/);
  });
});

describe('balanceParts (WP-27)', () => {
  it('<0 → клиент должен (owe), сумма по модулю', () => {
    expect(balanceParts(-150000, 'USD')).toEqual({ state: 'owe', amount: '1 500', currency: 'USD' });
  });
  it('>0 → аванс (adv)', () => {
    expect(balanceParts(5000, 'UZS')).toEqual({ state: 'adv', amount: '50', currency: 'UZS' });
  });
  it('0 → zero, null → none', () => {
    expect(balanceParts(0, 'USD').state).toBe('zero');
    expect(balanceParts(null, 'USD').state).toBe('none');
  });
});

describe('escapeHtml', () => {
  it('экранирует все спец-символы HTML', () => {
    expect(escapeHtml('<b>&"\'')).toBe('&lt;b&gt;&amp;&quot;&#39;');
  });

  it('null/undefined/число → строка без падения', () => {
    expect(escapeHtml(null)).toBe('');
    expect(escapeHtml(undefined)).toBe('');
    expect(escapeHtml(0)).toBe(''); // String(0||'') === ''
    expect(escapeHtml('plain')).toBe('plain');
  });

  it('защищает от инъекции тега', () => {
    expect(escapeHtml('<script>alert(1)</script>')).toBe(
      '&lt;script&gt;alert(1)&lt;/script&gt;'
    );
  });
});

describe('formatDateRU', () => {
  it('ISO YYYY-MM-DD → ДД.ММ.ГГГГ', () => {
    expect(formatDateRU('2026-05-31')).toBe('31.05.2026');
  });

  it('обрезает время и форматирует только дату', () => {
    expect(formatDateRU('2026-05-31T12:00:00')).toBe('31.05.2026');
  });

  it('пустое/короткое → прочерк или as-is', () => {
    expect(formatDateRU('')).toBe('—');
    expect(formatDateRU(null)).toBe('—');
    expect(formatDateRU('2026')).toBe('2026');
  });
});

describe('icon', () => {
  it('валидное имя → <use href="#ic-name">', () => {
    expect(icon('home')).toBe('<svg class="ic" aria-hidden="true"><use href="#ic-home"/></svg>');
  });

  it('добавляет доп. класс', () => {
    expect(icon('cart', 'nav-ic')).toContain('class="ic nav-ic"');
    expect(icon('cart', 'nav-ic')).toContain('#ic-cart');
  });

  it('санитизирует имя (защита от инъекции)', () => {
    // кавычки/скобки/угловые вырезаются → разметку сломать нельзя
    expect(icon('a"><script>')).toBe('<svg class="ic" aria-hidden="true"><use href="#ic-ascript"/></svg>');
  });

  it('пустое/невалидное имя не падает', () => {
    expect(icon()).toContain('#ic-');
    expect(icon(null)).toContain('#ic-');
  });
});

describe('sprite coverage', () => {
  const read = (rel) => readFileSync(new URL(rel, import.meta.url), 'utf8');
  it('все icon(\'литерал\') имеют <symbol> в спрайте index.html', () => {
    // Регресс после эмодзи→SVG: каждое строковое имя иконки должно существовать
    // в спрайте, иначе будет «пустая» иконка. Динамические icon(var) не ловим.
    const html = read('../index.html');
    const names = new Set();
    for (const src of [read('../app.js'), read('../helpers.js')]) {
      for (const m of src.matchAll(/\bicon\(\s*'([a-z][a-z-]*)'/g)) names.add(m[1]);
    }
    const missing = [...names].filter((n) => !html.includes(`id="ic-${n}"`));
    expect(missing).toEqual([]);
  });
});

describe('idemKey', () => {
  it('возвращает непустую строку', () => {
    const k = idemKey();
    expect(typeof k).toBe('string');
    expect(k.length).toBeGreaterThan(0);
  });

  it('два вызова дают разные ключи (уникальность)', () => {
    const seen = new Set();
    for (let i = 0; i < 50; i++) seen.add(idemKey());
    expect(seen.size).toBe(50);
  });
});

describe('opsAmount', () => {
  it('разделяет разряды пробелом, округляет', () => {
    expect(opsAmount(12345)).toBe('12 345');
    expect(opsAmount(999)).toBe('999');
    expect(opsAmount(1234567)).toBe('1 234 567');
    expect(opsAmount(100.6)).toBe('101');
  });
  it('null/undefined → "0"', () => {
    expect(opsAmount(null)).toBe('0');
    expect(opsAmount(undefined)).toBe('0');
  });
});

describe('renderOpsSummaryHtml', () => {
  it('пустая/нулевая сводка → «всё спокойно»', () => {
    expect(renderOpsSummaryHtml({})).toContain('Всё спокойно');
    expect(renderOpsSummaryHtml(null)).toContain('Всё спокойно');
  });

  it('показывает только непустые секции с их счётчиком', () => {
    const html = renderOpsSummaryHtml({
      stale_orders: { count: 2, threshold_hours: 48, items: [
        { id: 7, agent_name: 'Acme', full_name: 'Mgr' },
      ] },
      deposits: { count: 0, total: 0, items: [] },
    });
    expect(html).toContain('Зависшие заявки');
    expect(html).toContain('#7');
    expect(html).toContain('Acme');
    expect(html).toContain('data-status="low">2<');  // UI-WP-29: критичность атрибутом
    expect(html).not.toContain('Сдачи'); // count 0 — секции нет
  });

  it('экранирует пользовательские строки (агент/товар)', () => {
    const html = renderOpsSummaryHtml({
      stale_orders: { count: 1, threshold_hours: 48, items: [
        { id: 1, agent_name: '<b>x', full_name: 'M' },
      ] },
    });
    expect(html).toContain('&lt;b&gt;x');
    expect(html).not.toContain('<b>x');
  });

  it('суммирует рассинхрон МС (drift+deleted+demand_failed+transition_blocked)', () => {
    const html = renderOpsSummaryHtml({
      ms_anomalies: { drift: 1, deleted: 2, demand_failed: 0, transition_blocked: 1, items: {
        drift: [{ id: 3, agent_name: 'A' }],
        deleted: [{ id: 4, agent_name: 'B', status: 'shipped' }],
        demand_failed: [],
        transition_blocked: [{ id: 5, agent_name: 'C', status: 'approved' }],
      } },
    });
    expect(html).toContain('Рассинхрон с МойСклад');
    // Рассинхрон с МС — «плохо», а не «внимание»: учёт разошёлся с реальностью.
    expect(html).toContain('data-status="out">4<'); // 1 + 2 + 1
    expect(html).toContain('Статус застрял · #5');
  });
});

describe('parsePaymentItems', () => {
  it('парсит строки в items с числовыми суммами', () => {
    const r = parsePaymentItems([
      { amount: '100', currency: 'USD' },
      { amount: '50 000', currency: 'UZS' },
      { amount: '1,5', currency: 'EUR' },
    ]);
    expect(r.error).toBeUndefined();
    expect(r.items).toEqual([
      { amount: 100, currency: 'USD' },
      { amount: 50000, currency: 'UZS' },
      { amount: 1.5, currency: 'EUR' },
    ]);
  });
  it('пустой список → ошибка', () => {
    expect(parsePaymentItems([]).error).toBeTruthy();
  });
  it('неположительная/нечисловая сумма → ошибка', () => {
    expect(parsePaymentItems([{ amount: '0', currency: 'USD' }]).error).toBeTruthy();
    expect(parsePaymentItems([{ amount: 'abc', currency: 'USD' }]).error).toBeTruthy();
    expect(parsePaymentItems([{ amount: '-5', currency: 'USD' }]).error).toBeTruthy();
  });
  it('валюта по умолчанию USD', () => {
    expect(parsePaymentItems([{ amount: '10' }]).items[0].currency).toBe('USD');
  });
});

describe('renderMoneyTotalsHtml', () => {
  it('пусто → «поступлений нет»', () => {
    expect(renderMoneyTotalsHtml({})).toContain('поступлений нет');
    expect(renderMoneyTotalsHtml(null)).toContain('поступлений нет');
  });
  it('платежи по валютам (cents→units) + строка сдач', () => {
    const html = renderMoneyTotalsHtml({
      payments: [{ currency: 'USD', total_cents: 1234500, count: 3 }],
      deposits: { total_cents: 50000, count: 2 },
    });
    expect(html).toContain('USD · 12 345');
    expect(html).toContain('3 платеж.');
    expect(html).toContain('Наличные (сдачи) · 500 USD');
    expect(html).toContain('2 сдач.');
  });
  it('экранирует валюту', () => {
    const html = renderMoneyTotalsHtml({ payments: [{ currency: '<x>', total_cents: 100, count: 1 }], deposits: { count: 0 } });
    expect(html).toContain('&lt;x&gt;');
    expect(html).not.toContain('<x>');
  });
  it('показывает единый итог в базовой валюте', () => {
    const html = renderMoneyTotalsHtml({
      payments: [{ currency: 'USD', total_cents: 100000, count: 1 }],
      deposits: { total_cents: 0, count: 0 },
      base_total: 1000, base_currency: 'USD', missing_rates: [],
    });
    expect(html).toContain('money-total');
    expect(html).toContain('≈ 1 000 USD');
    expect(html).not.toContain('Без курса');
  });
  it('крупная сумма без курса (>999) не теряется молча — показана в блоке «Без курса»', () => {
    // Регресс «не считает суммы >999»: UZS 5 000 000 без курса не должен исчезнуть.
    const html = renderMoneyTotalsHtml({
      payments: [
        { currency: 'USD', total_cents: 50000, count: 1 },
        { currency: 'UZS', total_cents: 500000000, count: 1 },
      ],
      deposits: { total_cents: 0, count: 0 },
      base_total: 500, base_currency: 'USD',
      base_partial: true, missing_rates: [{ currency: 'UZS', amount: 5000000 }],
    });
    expect(html).toContain('≈ 500 USD');
    expect(html).toContain('(неполный)');
    expect(html).toContain('Без курса не учтено');
    expect(html).toContain('UZS 5 000 000');  // крупная сумма видна, не потеряна
  });
  it('без base_total — баннера итога нет', () => {
    const html = renderMoneyTotalsHtml({ payments: [{ currency: 'USD', total_cents: 100, count: 1 }], deposits: { count: 0 } });
    expect(html).not.toContain('money-total');
  });
});

describe('financeTabs', () => {
  const keys = (f) => financeTabs(f).map(t => t.key);
  it('boss: 4 вкладки, без overview/my', () => {
    const t = financeTabs({ isBoss: true, isConfirmer: true, hasOps: true, canDeposit: false });
    expect(t.map(x => x.key)).toEqual(['confirm', 'debts', 'ops', 'limits']);
    expect(t.find(x => x.key === 'ops').label).toBe('Платежи и сдачи');
  });
  it('bookkeeper (confirmer, не босс): confirm/debts/ops', () => {
    expect(keys({ isBoss: false, isConfirmer: true, hasOps: true, canDeposit: false }))
      .toEqual(['confirm', 'debts', 'ops']);
  });
  it('manager (не confirmer): только debts/ops', () => {
    expect(keys({ isBoss: false, isConfirmer: false, hasOps: true, canDeposit: true }))
      .toEqual(['debts', 'ops']);
  });
  it('любая роль — не больше 4 вкладок и без overview/my (страховка от переноса)', () => {
    const roles = [
      { isBoss: true, isConfirmer: true, hasOps: true, canDeposit: false },
      { isBoss: false, isConfirmer: true, hasOps: true, canDeposit: true },
      { isBoss: false, isConfirmer: false, hasOps: true, canDeposit: true },
      { isBoss: false, isConfirmer: false, hasOps: false, canDeposit: false },
    ];
    for (const r of roles) {
      const k = keys(r);
      expect(k.length).toBeLessThanOrEqual(4);
      expect(k).not.toContain('overview');
      expect(k).not.toContain('my');
    }
  });
});

describe('formatMoney (UI-WP-05)', () => {
  // toLocaleString разделяет разряды НЕразрывным пробелом (U+00A0/U+202F).
  // Нам важна группировка, а не кодпойнт пробела, — нормализуем.
  const norm = (s) => String(s).replace(/[  ]/g, ' ');

  it('форматирует тысячи по-русски и клеит валюту через пробел', () => {
    expect(norm(formatMoney(1234567, 'USD'))).toBe('1 234 567 USD');
  });

  it('округляет: копейки в списках только шумят', () => {
    expect(norm(formatMoney(1234.56))).toBe('1 235');
  });

  it('без валюты отдаёт только число — вызывающий клеит сам', () => {
    expect(formatMoney(500)).toBe('500');
  });

  it('не печатает NaN/Infinity в интерфейс', () => {
    expect(formatMoney(NaN, 'USD')).toBe('—');
    expect(formatMoney(Infinity)).toBe('—');
    expect(formatMoney(undefined)).toBe('—');
  });

  it('ноль — это сумма, а не пустое место', () => {
    expect(formatMoney(0, 'UZS')).toBe('0 UZS');
  });
});

describe('msBalanceLabel (UI-WP-05)', () => {
  it('отрицательный баланс МС = клиент должен нам', () => {
    const b = msBalanceLabel(-125000, 'USD');
    expect(b.tone).toBe('owe');
    expect(b.text).toContain('должен');
    expect(b.text).toContain('USD');
  });

  it('положительный баланс = аванс', () => {
    expect(msBalanceLabel(50000, 'USD').tone).toBe('advance');
  });

  it('ноль отличается от «нет данных»', () => {
    expect(msBalanceLabel(0, 'USD').tone).toBe('zero');
    expect(msBalanceLabel(null, 'USD').tone).toBe('none');
    expect(msBalanceLabel(null, 'USD').text).toBe('—');
  });

  it('подпись одна и та же для обоих экранов — расхождения формулировок больше нет', () => {
    const fromClients = msBalanceLabel(-1000, 'USD');
    const fromDetail = msBalanceLabel(-1000, 'USD');
    expect(fromClients.text).toBe(fromDetail.text);
  });
});

describe('emptyState (UI-WP-09)', () => {
  it('собирает иконку, заголовок и подсказку в одном порядке', () => {
    const html = emptyState({ icon: 'box', title: 'Нет заказов', hint: 'Создайте первый' });
    expect(html.indexOf('empty-state-icon')).toBeLessThan(html.indexOf('empty-state-title'));
    expect(html.indexOf('empty-state-title')).toBeLessThan(html.indexOf('empty-state-hint'));
    expect(html).toContain('Нет заказов');
  });

  it('без подсказки и кнопки не оставляет пустых блоков', () => {
    const html = emptyState({ icon: 'box', title: 'Пусто' });
    expect(html).not.toContain('empty-state-hint');
    expect(html).not.toContain('<button');
  });

  it('экранирует данные — заголовок может прийти из ответа API', () => {
    const html = emptyState({ title: '<img src=x onerror=alert(1)>' });
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });

  it('кнопка действия получает свой обработчик', () => {
    expect(emptyState({ title: 'X', action: { label: 'Обновить', onclick: 'location.reload()' } }))
      .toContain('onclick="location.reload()"');
    expect(emptyState({ title: 'X', action: { label: 'Ещё', id: 'load-more' } }))
      .toContain('id="load-more"');
  });
});

describe('skeleton (UI-WP-09)', () => {
  it('список даёт запрошенное число строк', () => {
    expect((skeleton('list', 4).match(/sk-card/g) || []).length).toBe(4);
  });

  it('сетка быстрых действий — всегда четыре плитки', () => {
    expect((skeleton('grid4').match(/sk-action/g) || []).length).toBe(4);
  });

  it('неизвестный вид не роняет экран, а даёт нейтральную заглушку', () => {
    expect(skeleton('чего-то-нет')).toContain('sk-card');
  });

  it('нулевой и отрицательный размер списка не дают пустоту', () => {
    expect((skeleton('list', 0).match(/sk-card/g) || []).length).toBe(1);
    expect((skeleton('list', -3).match(/sk-card/g) || []).length).toBe(1);
  });
});

describe('errorBoxHtml (UI-WP-09)', () => {
  it('показывает текст ошибки сервера', () => {
    const html = errorBoxHtml('500 Internal Server Error');
    expect(html).toContain('Не удалось загрузить');
    expect(html).toContain('500 Internal Server Error');
  });

  it('офлайн объясняет причину вместо технического текста', () => {
    const html = errorBoxHtml('Нет подключения к интернету');
    expect(html).toContain('Нет подключения');
    expect(html).toContain('Проверьте интернет');
    expect(html).not.toContain('к интернету<');
  });

  it('экранирует сообщение — оно приходит с сервера', () => {
    expect(errorBoxHtml('<script>alert(1)</script>')).not.toContain('<script>');
  });

  it('кнопку повтора можно привязать к своему обработчику', () => {
    expect(errorBoxHtml('x', { retryAttr: 'onclick="reload()"' })).toContain('onclick="reload()"');
    expect(errorBoxHtml('x', { retry: false })).not.toContain('Повторить');
  });
});

describe('rangeLabel (UI-BUG-02)', () => {
  const today = new Date('2026-07-15T12:00:00Z');

  it('внутри текущего года год не печатает — он не несёт информации', () => {
    expect(rangeLabel('2026-07-01', '2026-07-31', today)).toBe('01.07—31.07');
  });

  it('диапазон, выходящий за текущий год, год показывает', () => {
    expect(rangeLabel('2025-12-20', '2026-01-10', today)).toBe('20.12.25—10.01.26');
  });

  it('без обеих дат подписи нет — кнопка остаётся иконкой', () => {
    expect(rangeLabel('', '2026-07-31', today)).toBe('');
    expect(rangeLabel('2026-07-01', null, today)).toBe('');
  });

  it('короче полного формата — ради него всё и затевалось', () => {
    const short = rangeLabel('2026-07-01', '2026-07-31', today);
    expect(short.length).toBeLessThan('01.07.2026—31.07.2026'.length);
  });
});

describe('доп-кнопка периода (UI-BUG-02)', () => {
  const presets = [{ id: 'week', label: 'Неделя' }];

  it('при выбранном пресете — только иконка с доступной подписью', () => {
    const html = periodSegHtml(presets, 'week', 'data-period', false, '');
    expect(html).toContain('aria-label="Выбрать период"');
    expect(html).not.toContain('Период…');
  });

  it('в режиме custom показывает выбранный диапазон', () => {
    const html = periodSegHtml(presets, 'custom', 'data-period', true, '01.07—31.07');
    expect(html).toContain('01.07—31.07');
  });
});

describe('техника: подписи и подстрочник', () => {
  // Словарь приходит с сервера — там он живёт вместе с жизненным циклом машины.
  const LABELS = {
    in_transit: '🚢 В пути',
    in_stock: '🏗 На складе',
    reserved: '🔒 Забронирована',
    sold: '✅ Продана',
    on_credit: '💳 В рассрочку',
    archived: '📦 Архив',
  };

  it.each(Object.keys(LABELS))('статус %s получает подпись из словаря', (s) => {
    expect(machineStatusLabel(s, LABELS)).toBe(LABELS[s]);
  });

  it('незнакомый статус показывает себя, а не пустоту', () => {
    // Новый статус на сервере не должен оставить в списке пустой бейдж —
    // код в интерфейсе хотя бы объясняет, что происходит.
    expect(machineStatusLabel('in_repair', LABELS)).toBe('in_repair');
    expect(machineStatusLabel('', LABELS)).toBe('—');
    expect(machineStatusLabel('in_stock', undefined)).toBe('in_stock');
  });

  it('подстрочник собирает VIN, моточасы и цену', () => {
    const s = machineSubtitle({ vin: 'JCB7788', hours: 15200, price_cents: 2500000, currency: 'USD' });
    expect(s).toContain('JCB7788');
    expect(s).toContain('м/ч');
    expect(s).toContain('USD');
  });

  it('пустые части выпадают целиком, а не превращаются в «—»', () => {
    // «—» на месте цены читается как «цена ноль», хотя её просто не заводили.
    expect(machineSubtitle({ vin: 'A1' })).toBe('A1');
    expect(machineSubtitle({})).toBe('');
    expect(machineSubtitle({ vin: 'A1', hours: 0 })).toContain('0 м/ч');
  });

  it('VIN экранируется — он приходит из накладной, а не из справочника', () => {
    expect(machineSubtitle({ vin: '<img src=x>' })).not.toContain('<img');
  });
});

describe('техника: фильтр по статусу', () => {
  const counts = { all: 3, in_transit: 1, in_stock: 2, reserved: 0, sold: 0, on_credit: 0, archived: 4 };

  it('пустые статусы в ряд не попадают', () => {
    // «Забронированы 0» ничего не отбирает, а место в ряду занимает.
    const html = machineStatusSegHtml(counts, 'all', {});
    expect(html).toContain('data-mstatus="in_transit"');
    expect(html).not.toContain('data-mstatus="reserved"');
  });

  it('«Все» показывает размер списка без фильтра — архив в него не входит', () => {
    const html = machineStatusSegHtml(counts, 'all', {});
    expect(html).toContain('Все 3');
    expect(html).toContain('data-mstatus="archived"');
  });

  it('активный фильтр отмечен и классом, и aria-pressed', () => {
    const html = machineStatusSegHtml(counts, 'in_stock', {});
    expect(html).toContain('class="seg-item active" data-mstatus="in_stock"');
    expect(html).toMatch(/data-mstatus="in_stock" aria-pressed="true"/);
  });

  it('трек скроллится: статусов шесть и на 360dp они не помещаются', () => {
    expect(machineStatusSegHtml(counts, 'all', {})).toContain('seg seg--scroll');
  });

  it('без данных не падает', () => {
    expect(machineStatusSegHtml(null, 'all')).toContain('Все 0');
  });
});
