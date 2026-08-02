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
    // Техника: статус машины тоже приходит с сервера выражением.
    for (const s of ['in_transit', 'in_stock', 'reserved', 'sold', 'on_credit', 'archived']) {
      expect(declaredStatuses.has(s), `нет цвета для статуса техники: ${s}`).toBe(true);
    }
  });

  it('статусы техники объявлены в ОБЩЕЙ матрице, а не только у бейджа склада', () => {
    // `.stock-badge[data-status="in_stock"]` красит только склад. Строка
    // техники берёт цвет через --status-c, и без правила в общей матрице
    // бейдж остался бы бесцветным — при этом проверка «статус объявлен»
    // прошла бы, потому что селектор в файле есть.
    const generic = new Set(
      [...css.matchAll(/(^|\n)\s*\[data-status="([a-z_]+)"\]/g)].map((m) => m[2]),
    );
    for (const s of ['in_transit', 'in_stock', 'reserved', 'sold', 'on_credit', 'archived']) {
      expect(generic.has(s), `статус техники вне общей матрицы: ${s}`).toBe(true);
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
  // Берём ТЕЛО правила, а не окно фиксированной длины от первого упоминания.
  // Первым `[data-theme="dark"]` в файле идёт ссылка из комментария в :root, и
  // окно в 2000 символов доставало до настоящего блока только пока :root был
  // достаточно коротким — то есть тест держался на удаче, а не на инварианте.
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const darkStart = withoutComments.indexOf('[data-theme="dark"]');
  const darkBlock = withoutComments.slice(
    darkStart, withoutComments.indexOf('}', darkStart) + 1,
  );

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

  it('скроллящийся сегмент разбирает свободное место, а не жмётся влево', () => {
    // `flex: 0 0 auto` оставлял половину полосы пустой подложкой, когда пункты
    // узкие (пять иконок-статусов на широком экране). `1 0 auto` растит их по
    // свободному месту, но базис остаётся по содержимому — то есть при
    // переполнении подписи по-прежнему не сплющиваются (UI-BUG-01).
    const rule = css.match(/\.seg--scroll \.seg-item \{([^}]*)\}/);
    expect(rule, 'правило .seg--scroll .seg-item пропало').not.toBeNull();
    expect(rule[1]).toMatch(/flex:\s*1\s+0\s+auto/);
  });

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

describe('стекло (S7)', () => {
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, '');

  // Селектор перед объявлением: наивный `[^{}]*` не годится — правила лежат
  // внутри @supports, и скобки вложены.
  const selectorBefore = (idx) => {
    const head = withoutComments.slice(0, idx);
    const open = head.lastIndexOf('{');
    const prev = Math.max(head.lastIndexOf('}', open), head.lastIndexOf('{', open - 1));
    return head.slice(prev + 1, open).trim();
  };

  it('размытие стоит только на неподвижном', () => {
    // backdrop-filter на строках списка роняет прокрутку в WebView, а текст на
    // полупрозрачном фоне теряет контраст. Карточки остаются плотными.
    const allowed = ['.u-glass', '.bottom-nav', '.seg'];
    const seen = [];
    for (const m of withoutComments.matchAll(/backdrop-filter:/g)) {
      const sel = selectorBefore(m.index);
      seen.push(sel);
      expect(allowed.some((a) => sel.includes(a)), `размытие на «${sel}»`).toBe(true);
    }
    expect(seen.length).toBeGreaterThan(0);
  });

  it('без поддержки backdrop-filter остаётся плотный фон', () => {
    // Полупрозрачный тинт без размытия нечитаем, поэтому базовое правило
    // .u-glass красится непрозрачной поверхностью, а стекло включается
    // только внутри @supports.
    const base = withoutComments.match(/\.u-glass\s*\{([^}]*)\}/);
    expect(base, 'базовое правило .u-glass пропало').not.toBeNull();
    expect(base[1]).toMatch(/background:\s*var\(--bg-card\)/);
    expect(base[1]).not.toMatch(/backdrop-filter/);
  });

  it('у стекла есть тёмный вариант, а не инверсия светлого', () => {
    const darkStart = withoutComments.indexOf('[data-theme="dark"]');
    const darkBlock = withoutComments.slice(
      darkStart, withoutComments.indexOf('}', darkStart) + 1,
    );
    for (const token of ['--glass-bg', '--glass-edge', '--glass-spec', '--field-dot']) {
      expect(darkBlock.includes(`${token}:`), `нет тёмного варианта: ${token}`).toBe(true);
    }
  });

  it('поле под стеклом производно от темы, а не второй набор цветов', () => {
    // Иначе подложка спорит с темой пользователя: у него зелёный акцент, а
    // страница синеет. Вывод из темы живёт внутри @supports (color-mix), а
    // первое объявление — фолбэк для WebView без него.
    const declarations = [...withoutComments.matchAll(/--field-1:\s*([^;]+);/g)].map(m => m[1]);
    expect(declarations.length).toBeGreaterThan(1);
    expect(declarations.some(v => /var\(--accent\)/.test(v))).toBe(true);
  });

  it('у color-mix есть фолбэк — иначе фон отваливается целиком', () => {
    // Невалидное значение делает custom property guaranteed-invalid, и
    // `background`, ссылающийся на неё через var(), не применяется вовсе:
    // старый WebView остался бы без фона, а не «без украшения». Поэтому
    // базовые объявления обязаны быть без color-mix, а вывод из темы — под
    // @supports.
    const baseRoot = withoutComments.slice(
      withoutComments.indexOf(':root {'), withoutComments.indexOf('\n}'),
    );
    expect(baseRoot).not.toMatch(/color-mix/);
    const baseHero = withoutComments.match(/\.hero\s*\{([^}]*)\}/)[1];
    expect(baseHero).toMatch(/background:\s*var\(--accent\)/);
    expect(baseHero).not.toMatch(/color-mix/);
    // И при этом вывод из темы всё-таки есть.
    expect(withoutComments).toMatch(/@supports \(color: color-mix/);
  });

  it('состояние строки видно формой, а не только цветом текста', () => {
    // На площадке при ярком солнце цвет теряется первым.
    const stripe = withoutComments.match(/\.c-row\[data-status\]::before\s*\{([^}]*)\}/);
    expect(stripe, 'полоса состояния у строки списка пропала').not.toBeNull();
    expect(stripe[1]).toMatch(/background:\s*var\(--status-c\)/);
  });

  it('украшения отключаются при запросе повышенного контраста', () => {
    expect(withoutComments).toMatch(/@media \(prefers-contrast: more\)/);
  });
});

describe('фон не мешает sticky-шапке', () => {
  it('background-attachment: fixed не используется', () => {
    // Он уводит Chromium/WebView на медленный путь композитинга, и sticky-шапка
    // на прокрутке начинает рисоваться со смещением: заголовок уезжает под
    // шапку Telegram. Поле рисует фиксированный псевдоэлемент.
    // Комментарии вырезаем — в них это правило как раз и объясняется.
    const code = css.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(code).not.toMatch(/background-attachment:\s*fixed/);
  });

  it('поле лежит за контентом и не ловит нажатия', () => {
    const rule = css.match(/body::before\s*\{([^}]*)\}/);
    expect(rule, 'поле под стеклом пропало').not.toBeNull();
    expect(rule[1]).toMatch(/position:\s*fixed/);
    expect(rule[1]).toMatch(/z-index:\s*-1/);
    expect(rule[1]).toMatch(/pointer-events:\s*none/);
  });
});

describe('плавающие элементы у нижнего края', () => {
  const code = css.replace(/\/\*[\s\S]*?\*\//g, '');

  it('отступ от низа — большее из двух, а не сумма', () => {
    // `12px + env(safe-area-inset-bottom)` складывал собственный зазор с
    // высотой жестовой полосы Android, и панель зависала заметно выше края.
    expect(code).toMatch(/--nav-gap:\s*max\(\s*\d+px\s*,\s*env\(safe-area-inset-bottom\)\s*\)/);
  });

  it('у каждого плавающего элемента есть фолбэк без max()', () => {
    // Без max() объявление невалидно, и элемент теряет `bottom` целиком —
    // панель уехала бы в поток. Поэтому сначала простое значение, потом токен.
    for (const cls of ['.bottom-nav', '.editor-footer', '.toast-host']) {
      const at = code.indexOf(cls + ' {');
      expect(at, `правило ${cls} пропало`).toBeGreaterThan(-1);
      const body = code.slice(at, code.indexOf('}', at));
      expect(body, `${cls}: нет фолбэка`).toMatch(/bottom:[^;]*env\(safe-area-inset-bottom\)/);
      expect(body, `${cls}: не использует общий токен`).toMatch(/bottom:[^;]*var\(--nav-gap\)/);
    }
  });

  it('запас под панель считается тем же токеном, что и её отступ', () => {
    // Иначе контент прячется под меню или под ним остаётся дыра.
    const at = code.indexOf('.app {');
    const body = code.slice(at, code.indexOf('}', at));
    expect(body).toMatch(/padding-bottom:[^;]*var\(--nav-gap\)/);
  });
});
