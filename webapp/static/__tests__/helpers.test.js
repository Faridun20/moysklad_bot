// Юнит-тесты чистых хелперов фронта (webapp/static/helpers.js) — тестируем РЕАЛЬНЫЙ
// код, который грузится в браузере (UMD: в Node даёт module.exports).
import { describe, it, expect } from 'vitest';

import helpers from '../helpers.js';

const {
  escapeHtml, idemKey, formatDateRU, icon, opsAmount, renderOpsSummaryHtml,
  parsePaymentItems, renderMoneyTotalsHtml,
} = helpers;

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
    expect(html).toContain('badge-yellow">2<');
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

  it('суммирует рассинхрон МС (drift+deleted+demand_failed)', () => {
    const html = renderOpsSummaryHtml({
      ms_anomalies: { drift: 1, deleted: 2, demand_failed: 0, items: {
        drift: [{ id: 3, agent_name: 'A' }],
        deleted: [{ id: 4, agent_name: 'B', status: 'shipped' }],
        demand_failed: [],
      } },
    });
    expect(html).toContain('Рассинхрон с МойСклад');
    expect(html).toContain('badge-yellow">3<'); // 1 + 2
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
  it('показывает единый итог в базовой валюте (+ пометка partial)', () => {
    const html = renderMoneyTotalsHtml({
      payments: [{ currency: 'USD', total_cents: 100000, count: 1 }],
      deposits: { total_cents: 0, count: 0 },
      base_total: 1000, base_currency: 'USD', base_partial: true,
    });
    expect(html).toContain('money-total');
    expect(html).toContain('≈ 1 000 USD');
    expect(html).toContain('без курса');
  });
  it('без base_total — баннера итога нет', () => {
    const html = renderMoneyTotalsHtml({ payments: [{ currency: 'USD', total_cents: 100, count: 1 }], deposits: { count: 0 } });
    expect(html).not.toContain('money-total');
  });
});
