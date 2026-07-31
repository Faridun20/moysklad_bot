// S6 (UI-WP-30/31): инварианты дизайн-системы, проверяемые машиной.
//
// Скриншот-сверку в двух темах и прогон под ролями делает человек — это в
// UI_QA_ROLES.md. Но три вещи ломаются молча и обнаруживаются только глазами
// через недели, поэтому закрыты тестом:
//
//   1. добавили статус в JS, забыли цвет в CSS → бейдж без фона (UI-WP-02
//      затевался ровно из-за этого класса ошибок);
//   2. семантический цвет без тёмного варианта → нечитаемо в тёмной теме;
//   3. интерактивный примитив мельче 44px → промах пальцем.
import fs from 'node:fs';
import path from 'node:path';

import { describe, it, expect } from 'vitest';

const STATIC = path.resolve(process.cwd(), 'webapp', 'static');
const css = fs.readFileSync(path.join(STATIC, 'style.css'), 'utf8');
const app = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');

/** Значения data-status, объявленные в CSS. */
const declaredStatuses = new Set(
  [...css.matchAll(/\[data-status="([a-z_]+)"\]/g)].map((m) => m[1]),
);

describe('статус-система (UI-WP-02)', () => {
  it('каждый статус из разметки имеет правило цвета в CSS', () => {
    // Литеральные data-status="..." в шаблонах app.js.
    const used = [...app.matchAll(/data-status="([a-z_]+)"/g)].map((m) => m[1]);
    const missing = [...new Set(used)].filter((s) => !declaredStatuses.has(s));
    expect(missing).toEqual([]);
  });

  it('весь словарь статусов заказа покрыт (в шаблоны они приходят выражением)', () => {
    // В карточке заказа стоит data-status="${o.status}" — конкретные значения
    // приходят с сервера, поэтому проверяем словарь целиком.
    for (const s of ['draft', 'pending', 'approved', 'rejected', 'shipped']) {
      expect(declaredStatuses.has(s), `нет цвета для статуса заказа: ${s}`).toBe(true);
    }
    // Долги и остатки склада переиспользуют ту же матрицу.
    for (const s of ['overdue', 'due_today', 'upcoming', 'partial', 'in_stock', 'low', 'out']) {
      expect(declaredStatuses.has(s), `нет цвета для состояния: ${s}`).toBe(true);
    }
  });

  it('статусы выводят цвет через переменные, а не хардкодом', () => {
    const rules = [...css.matchAll(/\[data-status="[a-z_]+"\][^{]*\{([^}]*)\}/g)].map((m) => m[1]);
    expect(rules.length).toBeGreaterThan(5);
    for (const body of rules) {
      expect(body, `хардкод цвета в статусе: ${body.trim()}`).not.toMatch(/#[0-9a-f]{3,8}\b/i);
      expect(body).toMatch(/var\(--/);
    }
  });
});

describe('тёмная тема (UI-WP-30)', () => {
  const darkStart = css.indexOf('[data-theme="dark"]');
  const darkBlock = css.slice(darkStart, darkStart + 2000);

  it('семантические цвета имеют тёмный вариант', () => {
    // Эти токены — единственный источник цвета для статусов; без тёмного
    // варианта светлая заливка бейджа осталась бы на тёмном фоне.
    for (const token of [
      '--success', '--success-bg', '--danger', '--danger-bg',
      '--warn', '--warn-strong', '--warn-bg', '--info', '--info-bg',
      '--neutral', '--neutral-bg', '--divider',
    ]) {
      expect(darkBlock.includes(`${token}:`), `нет тёмного варианта: ${token}`).toBe(true);
    }
  });

  it('поверхность и фон страницы берутся из темы Telegram, а не из хардкода', () => {
    const surface = css.match(/--bg-card:\s*([^;]+);/)[1];
    const page = css.match(/--bg-page:\s*([^;]+);/)[1];
    expect(surface).toMatch(/var\(--tg-theme-/);
    expect(page).toMatch(/var\(--tg-theme-/);
  });
});

describe('тач-таргеты и фокус (UI-WP-31)', () => {
  // Класс описан несколькими правилами (базовое + модификаторы + медиазапросы),
  // поэтому собираем ВСЕ тела правил, где селектор упоминает класс: «первое
  // совпадение» здесь врёт — им оказывается, например, `.card-row + .card-row`.
  const bodiesFor = (cls) => {
    const out = [];
    for (const m of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      // Класс целиком, а не как префикс: `.c-row` не должен ловить `.c-row--tap`.
      if (m[1].split(/[\s,>+~]+/).some((part) => part.split(':')[0] === cls)) {
        out.push(m[2]);
      }
    }
    return out.join('\n');
  };

  it.each(['.c-row--tap', '.card-row', '.seg-item', '.subseg-item'])(
    'интерактивный примитив %s не мельче 44px',
    (cls) => {
      expect(bodiesFor(cls)).toMatch(/min-height:\s*44px/);
    },
  );

  it('фокус-кольцо объявлено глобально — новые компоненты его наследуют', () => {
    expect(css).toMatch(/:focus-visible\s*\{[^}]*outline:/);
  });

  it('строки-кнопки активируются с клавиатуры', () => {
    // div[role=button] сам по себе не реагирует на Enter/Space: без
    // делегированного хендлера весь список недоступен с клавиатуры.
    expect(app).toMatch(/keydown[\s\S]{0,400}role'?\)? === 'button'/);
  });
});

describe('токены шкал (UI-WP-03)', () => {
  it('объявлены и используются в новых примитивах', () => {
    for (const token of ['--sp-2', '--sp-3', '--sp-4', '--text-xs', '--text-sm']) {
      expect(css.includes(`${token}:`), `нет токена ${token}`).toBe(true);
    }
    const primitive = css.slice(css.indexOf('.c-row {'), css.indexOf('}', css.indexOf('.c-row {')));
    expect(primitive).toMatch(/var\(--sp-/);
  });
});
