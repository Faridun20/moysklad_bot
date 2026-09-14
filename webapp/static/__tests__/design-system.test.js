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

  it.each(['.c-row--tap', '.seg-item', '.cat-btn', '.btn-agent'])(
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

// ─── UI-бриф п.1: единые правила для всего ─────────────────────────────────
// Правила проверяются по ФАЙЛУ, а не по списку известных классов: новая
// строка CSS с «padding: 10px» или «border-radius: 14px» валит CI сама.
describe('дизайн-система: шкала отступов', () => {
  const code = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const SCALE = new Set([0, 4, 8, 12, 16, 24, 32]);

  it('шкала объявлена токенами 4 / 8 / 12 / 16 / 24 / 32', () => {
    const tokens = [...code.matchAll(/--sp-(\d):\s*(\d+)px/g)].map((m) => Number(m[2]));
    expect(tokens).toEqual([4, 8, 12, 16, 24, 32]);
  });

  it('padding / margin / gap — только из шкалы', () => {
    const bad = [];
    const re = /(?:^|[\s;{])((?:padding|margin|gap|row-gap|column-gap)(?:-(?:top|right|bottom|left|block|inline))?):([^;{}]*);/g;
    for (const m of code.matchAll(re)) {
      const value = m[2];
      if (value.includes('calc(')) continue;   // формулы с токенами/env() — свои
      for (const px of value.matchAll(/(-?)(\d+(?:\.\d+)?)px/g)) {
        const n = Number(px[2]);
        if (!SCALE.has(n)) bad.push(`${m[1]}: ${value.trim()}`);
      }
    }
    expect(bad).toEqual([]);
  });
});

describe('дизайн-система: скругления', () => {
  const code = css.replace(/\/\*[\s\S]*?\*\//g, '');

  it('два значения: 8px (поля, кнопки, чипы) и 12px (карточки, панели)', () => {
    expect(code).toMatch(/--radius-sm:\s*8px/);
    expect(code).toMatch(/--radius:\s*12px/);
    // Старые промежуточные токены (10 / 16 / 20 / pill) удалены.
    for (const t of ['--radius-lg', '--radius-chip', '--radius-pill']) {
      expect(code.includes(t), `лишний токен ${t}`).toBe(false);
    }
  });

  it('литеральных радиусов нет — только токены, 50% для кругов и 0', () => {
    const bad = [];
    for (const m of code.matchAll(/border-radius:\s*([^;]+);/g)) {
      const parts = m[1].trim().split(/\s+/);
      for (const part of parts) {
        const ok = part === '0' || part === '50%' || /^var\(--radius(-sm)?\)$/.test(part);
        if (!ok) bad.push(m[1].trim());
      }
    }
    expect(bad).toEqual([]);
  });

  it('поля и кнопки — 8px, карточки и панели — 12px', () => {
    const radiusOf = (sel) => {
      const at = code.indexOf(sel);
      expect(at, `правило ${sel} пропало`).toBeGreaterThan(-1);
      const body = code.slice(at, code.indexOf('}', at));
      const m = body.match(/border-radius:\s*([^;]+);/);
      return m ? m[1].trim() : null;
    };
    // Селектор ищется с начала строки: `.c-row > .form-input {` и
    // `.error-card .btn-primary {` стоят в файле раньше самих правил.
    for (const sel of ['.form-input', '.btn-primary', '.btn-secondary', '.cat-btn', '.seg-item',
      '.stock-badge', '.search-input']) {
      expect(radiusOf(`\n${sel} {`), sel).toBe('var(--radius-sm)');
    }
    for (const sel of ['.hero {', '.bottom-nav {', '.seg {', '.c-sheet,', '.toast {', '.wh-total {']) {
      expect(radiusOf(`\n${sel}`), sel).toBe('var(--radius)');
    }
    // Общая поверхность (.c-surface и алиасы) — карточка.
    const surface = code.slice(code.indexOf('.c-surface,'), code.indexOf('}', code.indexOf('.c-surface,')));
    expect(surface).toMatch(/border-radius:\s*var\(--radius\)/);
  });
});

describe('целостность файла стилей', () => {
  // Тело @media/@supports смотрим отдельно: там переопределение — это и есть
  // задача блока, и считать его столкновением имён нельзя.
  const stripAtRules = (src) => {
    let out = '';
    let i = 0;
    while (i < src.length) {
      const at = src.indexOf('@', i);
      if (at < 0) { out += src.slice(i); break; }
      const open = src.indexOf('{', at);
      if (open < 0) { out += src.slice(i); break; }
      out += src.slice(i, at);
      let depth = 0;
      let j = open;
      for (; j < src.length; j++) {
        if (src[j] === '{') depth++;
        else if (src[j] === '}') { depth--; if (!depth) { j++; break; } }
      }
      i = j;
    }
    return out;
  };

  it('нет обрывков селекторов — они молча глушат следующее правило', () => {
    // Одинокая `.` — остаток удалённого правила. Парсер CSS склеивает её со
    // СЛЕДУЮЩИМ селектором (`. .stat-grid`), и то правило перестаёт
    // применяться целиком. Так умерла сетка показателей: `.stat-grid` с
    // `display:grid` в файле был, а плитки отчёта вставали в столбик и
    // «налезали друг на друга» — жалоба с площадки. Ни ruff, ни vitest, ни
    // глаз в диффе этого не ловят: файл остаётся валидным CSS.
    const code = css.replace(/\/\*[\s\S]*?\*\//g, '');
    const broken = [];
    for (const m of code.matchAll(/([^{}]+)\{/g)) {
      const sel = m[1].trim();
      if (/(^|[\s,>+~])\.(?![-_a-zA-Z\\])/.test(sel)) {
        broken.push(sel.replace(/\s+/g, ' ').slice(0, 80));
      }
    }
    expect(broken).toEqual([]);
  });

  it('правила, на которые опираются экраны, реально объявлены', () => {
    // Обратная сторона того же бага: класс есть в разметке, правило «есть» в
    // файле, но не применяется. Проверяем сам факт объявления для тех, чью
    // пропажу видно глазом на экране.
    for (const cls of ['.stat-grid', '.order-meta', '.debts-summary', '.debt-hint',
      '.due-date-wrap', '.u-fs-11']) {
      const re = new RegExp(`(^|[\\s,}])\\${cls}\\s*[,{]`, 'm');
      expect(re.test(css), `правило ${cls} не объявлено`).toBe(true);
    }
  });

  it('одно имя класса — один компонент', () => {
    // `.qty-input` был объявлен дважды: сверху — крупное поле диалога
    // количества, ниже — узкое поле в строке приёмки контейнера. Второе
    // правило переопределяло у первого ВОСЕМЬ свойств (ширину, размер, рамку,
    // выравнивание) — то есть это было не уточнение, а другой компонент,
    // случайно занявший то же имя. Файл при этом валиден, и увидеть такое
    // можно только открыв оба экрана.
    //
    // Уточнять базовое правило законно (`.search-item` мельчает в выдаче,
    // `.debt-stat` берёт свой радиус) — там переопределяется одно свойство.
    // Порог в три и означает «переписали компонент целиком».
    const code = css.replace(/\/\*[\s\S]*?\*\//g, '');
    const topLevel = stripAtRules(code);
    const seen = new Map();
    const clashes = [];
    for (const m of topLevel.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      const props = m[2].split(';')
        .map((d) => (d.split(':')[0] || '').trim())
        .filter((d) => /^[-a-z]+$/.test(d));
      for (const part of m[1].split(',')) {
        const sel = part.trim();
        if (!/^\.[-_a-zA-Z0-9]+$/.test(sel)) continue;
        const before = seen.get(sel);
        if (before) {
          const again = props.filter((p) => before.has(p));
          if (again.length > 2) clashes.push(`${sel}: ${again.join(', ')}`);
        }
        seen.set(sel, new Set([...(before || []), ...props]));
      }
    }
    expect(clashes).toEqual([]);
  });

  it('сетка показателей — именно сетка 2×2', () => {
    const at = css.indexOf('.stat-grid {');
    const body = css.slice(at, css.indexOf('}', at));
    expect(body).toMatch(/display:\s*grid/);
    expect(body).toMatch(/grid-template-columns:\s*1fr 1fr/);
    expect(body).toMatch(/gap:\s*8px/);
  });
});

describe('дизайн-система: цвета', () => {
  const code = css.replace(/\/\*[\s\S]*?\*\//g, '');

  it('hex-цвета живут только в блоках токенов (:root и тёмные варианты)', () => {
    // Захардкоженный цвет вне токенов не переключится вместе с темой Telegram.
    // Разрешены: :root, [data-theme="dark"], @media (prefers-color-scheme).
    const blocks = [];
    for (const m of code.matchAll(/(:root|\[data-theme="dark"\]|:root:not\(\[data-theme="light"\]\))\s*\{[^}]*\}/g)) {
      blocks.push([m.index, m.index + m[0].length]);
    }
    const bad = [];
    for (const m of code.matchAll(/#[0-9a-fA-F]{3,8}\b/g)) {
      const inside = blocks.some(([a, b]) => m.index >= a && m.index < b);
      // color-mix с #000 в hero — затемнение АКЦЕНТА темы, а не свой цвет.
      const line = code.slice(code.lastIndexOf('\n', m.index) + 1, code.indexOf('\n', m.index));
      if (!inside && !/color-mix\(/.test(line)) bad.push(line.trim());
    }
    expect(bad).toEqual([]);
  });

  it('базовые цвета — из темы Telegram, с фолбэком', () => {
    for (const [token, tg] of [
      ['--bg-page', '--tg-theme-secondary-bg-color'],
      ['--bg-card', '--tg-theme-bg-color'],
      ['--text', '--tg-theme-text-color'],
      ['--text-mute', '--tg-theme-hint-color'],
      ['--accent', '--tg-theme-button-color'],
      ['--accent-fg', '--tg-theme-button-text-color'],
    ]) {
      const decl = code.match(new RegExp(`${token}:\\s*([^;]+);`));
      expect(decl, `нет токена ${token}`).not.toBeNull();
      expect(decl[1]).toContain(`var(${tg}`);
    }
  });

  it('жёлтая плашка заявок ушла — цвет предупреждения только для семантики', () => {
    expect(code).not.toMatch(/\.requests-btn/);
  });
});

describe('дизайн-система: заголовки секций и тап-цели', () => {
  const code = css.replace(/\/\*[\s\S]*?\*\//g, '');

  it('.section-label — капс, 12px, hint-цвет, letter-spacing 0.5px, снизу 8px', () => {
    const at = code.indexOf('.section-label {');
    const body = code.slice(at, code.indexOf('}', at));
    expect(body).toMatch(/text-transform:\s*uppercase/);
    expect(body).toMatch(/font-size:\s*var\(--text-sm\)/);
    expect(code).toMatch(/--text-sm:\s*12px/);
    expect(body).toMatch(/color:\s*var\(--text-mute\)/);
    expect(body).toMatch(/letter-spacing:\s*0\.5px/);
    expect(body).toMatch(/margin:\s*var\(--sp-5\) 0 var\(--sp-2\)/);
  });

  it('всё кликабельное — не ниже 44px: общее правило на button и [role=button]', () => {
    expect(code).toMatch(/button,\s*\[role="button"\]\s*\{\s*min-height:\s*44px/);
  });

  it('ни одно правило не занижает тап-цель ниже 44px', () => {
    // min-height у кликабельного класса меньше 44 перекрыл бы общее правило.
    const bad = [];
    for (const m of code.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      const sel = m[1].trim();
      if (!/\.(btn|cat-btn|seg-item|subseg-item|nav-item|c-row--tap|card-row|search-item|toast-close|cal-nav|cal-day|editor-header button|pay-toggle|photo-del)/.test(sel)) continue;
      for (const mh of m[2].matchAll(/min-height:\s*(\d+)px/g)) {
        if (Number(mh[1]) < 44) bad.push(`${sel}: ${mh[0]}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it('скруглённые элементы в списках не стоят вплотную: зазор 8px', () => {
    for (const sel of ['.stock-list,', '.editor-items {', '.debts-list {', '.stat-grid {', '.cat-row {', '.search-results {']) {
      const at = code.indexOf(sel);
      expect(at, `правило ${sel} пропало`).toBeGreaterThan(-1);
      const body = code.slice(at, code.indexOf('}', at));
      expect(body, sel).toMatch(/gap:\s*8px/);
    }
    const grid = code.slice(code.indexOf('.stat-grid {'), code.indexOf('}', code.indexOf('.stat-grid {')));
    expect(grid).toMatch(/grid-template-columns:\s*1fr 1fr/);
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
    // `.u-glass` убран: утилита без потребителей. Стекло живёт там, где оно
    // и нужно — на неподвижной панели и на треке вкладок.
    const allowed = ['.bottom-nav', '.seg'];
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
    // красится непрозрачной поверхностью, а стекло включается только внутри
    // @supports. Проверяем на реальном потребителе — нижней панели.
    const base = withoutComments.match(/\n\.bottom-nav\s*\{([^}]*)\}/);
    expect(base, 'базовое правило .bottom-nav пропало').not.toBeNull();
    expect(base[1]).toMatch(/background:\s*var\(--bg-card\)/);
    expect(base[1]).not.toMatch(/backdrop-filter/);
  });

  it('у стекла есть тёмный вариант, а не инверсия светлого', () => {
    const darkStart = withoutComments.indexOf('[data-theme="dark"]');
    const darkBlock = withoutComments.slice(
      darkStart, withoutComments.indexOf('}', darkStart) + 1,
    );
    for (const token of ['--glass-bg', '--glass-edge', '--glass-spec']) {
      expect(darkBlock.includes(`${token}:`), `нет тёмного варианта: ${token}`).toBe(true);
    }
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

  it('фона-текстуры нет: страница — ровный цвет темы (UI-бриф п.1)', () => {
    // Точечная сетка рисовалась псевдоэлементом body::before; на площадке она
    // читалась шумом. Сторож — чтобы украшение не вернулось под другим именем.
    const code = css.replace(/\/\*[\s\S]*?\*\//g, '');
    expect(code).not.toMatch(/body::before/);
    expect(code).not.toMatch(/radial-gradient\(circle at 1px 1px/);
    const body = code.match(/\nbody\s*\{([^}]*)\}/)[1];
    expect(body).toMatch(/background:\s*var\(--bg-page\)/);
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

  it('запас под панель = высота панели + 16px + safe-area (UI-бриф п.1)', () => {
    // Иначе контент прячется под меню или под ним остаётся дыра: последняя
    // строка фильтров каталога и график по дням прятались за панелью.
    const at = code.indexOf('.app {');
    const body = code.slice(at, code.indexOf('}', at));
    expect(body).toMatch(/padding-bottom:[^;]*var\(--nav-gap\)/);
    expect(body).toMatch(/padding-bottom:\s*calc\(var\(--nav-h\) \+ var\(--sp-4\) \+ var\(--nav-gap\)\)/);
    expect(code).toMatch(/--nav-h:\s*66px/);
    // Высота панели — из её же правил: padding 8+8, кнопка 48, рамка 1+1.
    const nav = code.slice(code.indexOf('.bottom-nav {'), code.indexOf('}', code.indexOf('.bottom-nav {')));
    expect(nav).toMatch(/padding:\s*8px/);
    const item = code.slice(code.indexOf('.nav-item {'), code.indexOf('}', code.indexOf('.nav-item {')));
    expect(item).toMatch(/min-height:\s*48px/);
  });
});

describe('одно правило — одно значение свойства', () => {
  const code = css.replace(/\/\*[\s\S]*?\*\//g, '');

  it('position не объявлен дважды в одном правиле', () => {
    // Так плавающая нижняя панель перестала быть плавающей: `position: fixed`
    // стоял в начале правила, а добавленный позже `position: relative` (ради
    // блика псевдоэлементом) молча его отменил. Панель ушла в поток, а запас,
    // зарезервированный под неё в каркасе, превратился в пустоту под ней.
    //
    // `fixed` и `absolute` сами по себе задают containing block для
    // абсолютных детей — `relative` рядом с ними не нужен никогда.
    const clashes = [];
    for (const m of code.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      const declared = [...m[2].matchAll(/^\s*position:\s*([a-z-]+)/gm)].map((d) => d[1]);
      if (new Set(declared).size > 1) clashes.push(`${m[1].trim().split('\n').pop()}: ${declared}`);
    }
    expect(clashes).toEqual([]);
  });

  it('нижняя панель плавающая, а не в потоке', () => {
    // Прямая проверка того, что сломалось: запас под панель в каркасе имеет
    // смысл только пока она вынута из потока.
    const at = code.indexOf('.bottom-nav {');
    const body = code.slice(at, code.indexOf('}', at));
    expect(body).toMatch(/position:\s*fixed/);
    expect(body).not.toMatch(/position:\s*(relative|static)/);
  });
});
