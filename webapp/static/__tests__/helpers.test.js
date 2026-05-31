// Юнит-тесты чистых хелперов фронта (webapp/static/helpers.js) — тестируем РЕАЛЬНЫЙ
// код, который грузится в браузере (UMD: в Node даёт module.exports).
import { describe, it, expect } from 'vitest';

import helpers from '../helpers.js';

const { escapeHtml, idemKey, formatDateRU } = helpers;

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
