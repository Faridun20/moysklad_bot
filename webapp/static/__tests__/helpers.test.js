// Юнит-тесты чистых хелперов фронта (webapp/static/helpers.js) — тестируем РЕАЛЬНЫЙ
// код, который грузится в браузере (UMD: в Node даёт module.exports).
import { readFileSync } from 'node:fs';

import { describe, it, expect } from 'vitest';

import helpers from '../helpers.js';

const {
  escapeHtml, idemKey, formatDateRU, icon, opsAmount, plural, categoryTree, categoryMatches,
  parseAmount, parsePaymentItems, renderMoneyTotalsHtml, periodSegHtml, rangeLabel,
  navSections, defaultSection, sectionNavHtml, salesTabs, stockTabs, moneyTabs, clientsTabs,
  roleSectionTabs,
  formatMoney, emptyState, skeleton, errorBoxHtml,
  machineStatusLabel, machineSubtitle, machineStatusSegHtml,
  moneyBlockLabel, agingBarsHtml, forecastRowsHtml, buyerKey, leadFunnelHtml,
  firstTouchHtml, replySpeedHtml, durationLabel, postEffectLabel,
  whMoney, whQty, whStockBadge,
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

  it('«Период…» — последний пункт ТОГО ЖЕ сегмента, с подписью', () => {
    // Иконка часов справа от группы, без подписи, была непонятна (UI-бриф п.5):
    // вне группы она читалась как «что-то ещё». Теперь это пункт группы.
    const html = periodSegHtml(presets, 'month', 'data-period', false, '');
    expect(html).not.toContain('seg-aux');
    const items = [...html.matchAll(/class="seg-item[^"]*"/g)];
    expect(items.length).toBe(3);
    expect(html).toContain('Период…');
    // Один .seg-row — доп-кнопки за пределами трека нет.
    expect(html.match(/<button/g).length).toBe(3);
  });

  it('custom активен → пункт подсвечен и показывает выбранный диапазон', () => {
    const html = periodSegHtml(presets, 'custom', 'data-operiod', true, '01.06—12.06');
    expect(html).toContain('data-operiod="custom"');
    expect(html).toContain('01.06—12.06');  // показывает выбранный диапазон
    expect(html).toMatch(/seg-item seg-item--custom active/);
    expect(html).not.toContain('Период…');
  });
});

describe('plural — склонение числительных (UI-бриф п.8)', () => {
  const f = ['клиент', 'клиента', 'клиентов'];
  it('1 / 2 / 5 / 11 / 21', () => {
    expect(plural(1, f)).toBe('1 клиент');
    expect(plural(2, f)).toBe('2 клиента');
    expect(plural(5, f)).toBe('5 клиентов');
    expect(plural(11, f)).toBe('11 клиентов');
    expect(plural(21, f)).toBe('21 клиент');
  });
  it('12–14 и 111–114 — родительный множественного, 22/23/24 — единственного', () => {
    expect(plural(12, f)).toBe('12 клиентов');
    expect(plural(14, f)).toBe('14 клиентов');
    expect(plural(112, f)).toBe('112 клиентов');
    expect(plural(22, f)).toBe('22 клиента');
    expect(plural(0, f)).toBe('0 клиентов');
  });
  it('мусор на входе — ноль, а не NaN; крупные числа с разрядами', () => {
    expect(plural(null, f)).toBe('0 клиентов');
    expect(plural('7', f)).toBe('7 клиентов');
    expect(plural(1234, ['позиция', 'позиции', 'позиций'])).toBe('1\u00a0234 позиции');
  });
});

describe('categoryTree — два уровня из «Запчасти/Адаптер» (UI-бриф п.4)', () => {
  const cats = [
    { id: 'Запчасти Экскаватор/Адаптер', name: 'Запчасти Экскаватор/Адаптер' },
    { id: 'Запчасти Экскаватор/Ковш', name: 'Запчасти Экскаватор/Ковш' },
    { id: 'Масло', name: 'Масло' },
    { id: 'Солнечные панели', name: 'Солнечные панели' },
  ];
  it('первый уровень — до первого «/», второй — после', () => {
    const tree = categoryTree(cats);
    expect(tree.map(r => r.name)).toEqual(['Запчасти Экскаватор', 'Масло', 'Солнечные панели']);
    expect(tree[0].children.map(c => c.name)).toEqual(['Адаптер', 'Ковш']);
    expect(tree[1].children).toEqual([]);
  });
  it('названия не переписываются — ключи второго уровня равны исходным id', () => {
    const tree = categoryTree(cats);
    expect(tree[0].children[0].key).toBe('Запчасти Экскаватор/Адаптер');
    // Категория без «/» — сама себе первый уровень, её id и есть ключ.
    expect(tree[1].key).toBe('Масло');
  });
  it('фильтр: корень собирает все свои подкатегории, подуровень — точное совпадение', () => {
    const tree = categoryTree(cats);
    expect(categoryMatches('Запчасти Экскаватор/Ковш', tree[0], '')).toBe(true);
    expect(categoryMatches('Масло', tree[0], '')).toBe(false);
    expect(categoryMatches('Запчасти Экскаватор/Ковш', tree[0], 'Запчасти Экскаватор/Адаптер')).toBe(false);
    expect(categoryMatches('anything', null, '')).toBe(true);  // «Все»
  });
  it('пусто и мусор', () => {
    expect(categoryTree([])).toEqual([]);
    expect(categoryTree(null)).toEqual([]);
    expect(categoryTree([{ id: '', name: '' }])).toEqual([]);
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

describe('parseAmount — сумма, как её вводят на телефоне', () => {
  it('понимает пробелы-разряды и десятичную запятую', () => {
    expect(parseAmount('1 500')).toBe(1500);
    expect(parseAmount('1,5')).toBe(1.5);
    expect(parseAmount('1 500,50')).toBe(1500.5);
    expect(parseAmount('1\u00a0500')).toBe(1500);   // неразрывный пробел из буфера
    expect(parseAmount(' 12.25 ')).toBe(12.25);
    expect(parseAmount(40)).toBe(40);
  });

  it('мусор — NaN, а не «первые цифры»: «12abc» не превращается в 12', () => {
    for (const bad of ['', '   ', 'abc', '12abc', '-5', '1,2,3', '1.2.3', null, undefined]) {
      expect(Number.isNaN(parseAmount(bad))).toBe(true);
    }
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
    expect(html).toContain('3 платежа');
    expect(html).toContain('Наличные (сдачи) · 500 USD');
    expect(html).toContain('2 сдачи');
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
  it('пересчёт чужой валюты подписан: по курсу на день поступления', () => {
    // Итог в базовой считается по снимку курса подтверждения, а не по
    // сегодняшнему — иначе прошлые деньги «плыли» бы вместе с курсом.
    const withUzs = renderMoneyTotalsHtml({
      payments: [{ currency: 'UZS', total_cents: 125000000, count: 1 }],
      deposits: { total_cents: 0, count: 0 },
      base_total: 100, base_currency: 'USD', missing_rates: [],
    });
    expect(withUzs).toContain('по курсу на день поступления');
    expect(withUzs).not.toContain('money-total-note');
    const onlyUsd = renderMoneyTotalsHtml({
      payments: [{ currency: 'USD', total_cents: 10000, count: 1 }],
      deposits: { total_cents: 0, count: 0 },
      base_total: 100, base_currency: 'USD', missing_rates: [],
    });
    expect(onlyUsd).not.toContain('по курсу');
  });
  it('без base_total — баннера итога нет', () => {
    const html = renderMoneyTotalsHtml({ payments: [{ currency: 'USD', total_cents: 100, count: 1 }], deposits: { count: 0 } });
    expect(html).not.toContain('money-total');
  });
});

describe('вкладки разделов', () => {
  const keys = (fn, f) => fn(f).map(t => t.key);

  it('Деньги у руководителя: подтвердить/долги/касса/сверка/отчёт', () => {
    expect(keys(moneyTabs, { isBoss: true, isConfirmer: true, canSeeDebts: true, hasOps: true,
                             canReconcile: true }))
      .toEqual(['confirm', 'debts', 'ops', 'reconcile', 'report']);
  });

  it('бухгалтер видит только то, на что у него есть ручки', () => {
    // /api/deposits/* ему отвечают, /api/debts, /api/money/summary и
    // /api/cash/reconcile* — нет.
    expect(keys(moneyTabs, { isBoss: false, isConfirmer: true, canSeeDebts: false, hasOps: false }))
      .toEqual(['confirm']);
  });

  it('менеджер: долги, касса и сверка, без подтверждений и отчёта', () => {
    expect(keys(moneyTabs, { isBoss: false, isConfirmer: false, canSeeDebts: true, hasOps: true,
                             canReconcile: true }))
      .toEqual(['debts', 'ops', 'reconcile']);
  });

  it('«Поставщикам» — только руководству: сумма прихода это закупочная цена', () => {
    // /api/suppliers/debts отвечает admin/boss; у менеджера вкладка вернула бы
    // 403, а заодно показала бы ему себестоимость.
    expect(keys(moneyTabs, { canSeeDebts: true, canSeeSuppliers: false })).toEqual(['debts']);
    expect(keys(moneyTabs, { canSeeDebts: true, canSeeSuppliers: true }))
      .toEqual(['debts', 'suppliers']);
    expect(roleSectionTabs('money', 'manager', { work: true }).map(t => t.key))
      .not.toContain('suppliers');
    for (const r of ['boss', 'admin']) {
      expect(roleSectionTabs('money', r, { work: true }).map(t => t.key))
        .toEqual(['debts', 'suppliers', 'ops', 'report']);
      // «Рабочие действия» выключены — контроль остаётся, работа уходит.
      expect(roleSectionTabs('money', r, { work: false }).map(t => t.key))
        .toEqual(['debts', 'suppliers', 'report']);
    }
  });

  it('Продажи: отчёт только тем, кому отвечает /api/analytics', () => {
    expect(keys(salesTabs, { canSeeReport: true })).toEqual(['orders', 'report']);
    expect(keys(salesTabs, { canSeeReport: false })).toEqual(['orders']);
  });

  it('Продажи: «Документы» — тем же ролям, что создают заказы (/api/docs/*)', () => {
    expect(keys(salesTabs, { canSeeReport: true, canDocs: true })).toEqual(['orders', 'report', 'docs']);
    // Кладовщик: ручка ответила бы 403 — вкладки нет.
    expect(keys(salesTabs, { canSeeReport: false, canDocs: false })).toEqual(['orders']);
  });

  it('Склад: контейнеры, техника и накладные — та же тройка ролей, что у их ручек', () => {
    expect(keys(stockTabs, { canSeeGoods: true }))
      .toEqual(['catalog', 'containers', 'machines', 'invoices']);
    expect(keys(stockTabs, { canSeeGoods: false })).toEqual(['catalog']);
  });

  it('Склад: «Залежалось» — не вкладка, а фильтр каталога (UI-бриф п.4)', () => {
    // Вкладкой был четвёртый пункт, упиравшийся в край; накладным место
    // нужнее — у них своё действие «создать». Залежалое — срез того же списка.
    expect(keys(stockTabs, { canSeeGoods: true, isBoss: true })).not.toContain('stale');
  });

  it('Клиенты: лиды всем, воронка, лимиты и канал — руководству', () => {
    expect(keys(clientsTabs, { isBoss: true })).toEqual(['funnel', 'list', 'limits', 'channel']);
    // /api/leads/funnel отвечает только admin/boss — у менеджера «Воронка»
    // была вкладкой с гарантированным 403 (регресс сверки вкладок с ручками).
    expect(keys(clientsTabs, { isBoss: false })).toEqual(['list']);
  });

  it('ни одна РОЛЬ не получает больше 4 вкладок в разделе', () => {
    // Пятая не влезает в ряд на 360dp и уезжает в скролл, который не виден.
    // Считаем по НАСТОЯЩИМ сочетаниям ролей (`roleSectionTabs`), а не по набору
    // флагов «все сразу»: у руководителя `isConfirmer` не бывает (он
    // подтверждает в «Решениях»), и придуманный худший случай заставлял бы
    // вырезать вкладку, которой ни у кого на экране нет.
    for (const role of ['admin', 'boss', 'manager', 'warehouse_keeper', 'bookkeeper']) {
      for (const section of ['money', 'sales', 'stock', 'clients']) {
        for (const work of [true, false]) {
          const tabs = roleSectionTabs(section, role, { work });
          expect(tabs.length, `${role}/${section}/work=${work}`).toBeLessThanOrEqual(4);
        }
      }
    }
  });
});

describe('разделы нижней панели', () => {
  it('руководитель: сначала «смотреть и решать», склад и клиенты — дальше', () => {
    // Решение владельца: панель руководителя — «Сегодня · Решения · Деньги ·
    // Продажи · Меню»; навBarLayout берёт первые четыре.
    for (const r of ['boss', 'admin']) {
      expect(navSections(r).map(s => s.key))
        .toEqual(['today', 'decisions', 'money', 'sales', 'stock', 'clients', 'settings']);
    }
  });

  it('менеджер: порядок и состав разделов не менялись', () => {
    expect(navSections('manager').map(s => s.key))
      .toEqual(['today', 'sales', 'stock', 'money', 'clients']);
  });

  it('кладовщик и бухгалтер не видят склад и клиентов', () => {
    // Роли режем по матрице ручек: раздел, где всё ответит 403, — дверь,
    // которая не открывается. «Сегодня» им доступна — очередь считает
    // /api/today, а не /api/home.
    expect(navSections('warehouse_keeper').map(s => s.key)).toEqual(['today', 'sales', 'money']);
    expect(navSections('bookkeeper').map(s => s.key)).toEqual(['today', 'sales', 'money']);
  });

  it('стартовый экран — первый доступный роли', () => {
    expect(defaultSection('bookkeeper')).toBe('today');
    expect(defaultSection('boss')).toBe('today');
  });

  it('у guest разделов нет вовсе', () => {
    expect(navSections('guest')).toEqual([]);
    expect(defaultSection('guest')).toBeNull();
  });
});

describe('sectionNavHtml', () => {
  const tabs = [{ key: 'orders', label: 'Заказы' }, { key: 'report', label: 'Отчёт' }];

  it('активная вкладка получает .active и aria-pressed', () => {
    const html = sectionNavHtml(tabs, 'report');
    expect(html).toContain('class="seg-item active" data-sect="report"');
    expect(html).toContain('data-sect="report" aria-pressed="true"');
    expect(html).toContain('data-sect="orders" aria-pressed="false"');
  });

  it('одна вкладка — переключателя нет: нечего переключать', () => {
    expect(sectionNavHtml([{ key: 'orders', label: 'Заказы' }], 'orders')).toBe('');
    expect(sectionNavHtml([], 'x')).toBe('');
  });

  it('четыре вкладки уезжают в скролл, а не сплющиваются (UI-BUG-01)', () => {
    const four = tabs.concat([{ key: 'a', label: 'A' }, { key: 'b', label: 'B' }]);
    expect(sectionNavHtml(four, 'orders')).toContain('seg seg--scroll');
    expect(sectionNavHtml(tabs, 'orders')).not.toContain('seg--scroll');
  });

  it('бейдж рисуется числом и экранируется', () => {
    const html = sectionNavHtml([{ key: 'confirm', label: 'Подтвердить', badge: 3 }, tabs[0]], 'confirm');
    expect(html).toContain('stock-badge badge-yellow">3<');
  });
});

describe('formatMoney (UI-WP-05)', () => {
  // toLocaleString разделяет разряды НЕразрывным пробелом (U+00A0/U+202F).
  // Нам важна группировка, а не кодпойнт пробела, — нормализуем.
  const norm = (s) => String(s).replace(/[  ]/g, ' ');

  it('валюту экранирует: результат вставляют в innerHTML, а строка пришла с сервера', () => {
    expect(norm(formatMoney(5, '<img src=x onerror=alert(1)>'))).toBe('5 &lt;img src=x onerror=alert(1)&gt;');
    expect(norm(formatMoney(5, 'UZS'))).toBe('5 UZS');
  });

  it('форматирует тысячи по-русски и клеит валюту через пробел', () => {
    expect(norm(formatMoney(1234567, 'USD'))).toBe('1 234 567 USD');
  });

  it('показывает копейки, когда они есть: 12.50 USD — не «13 USD»', () => {
    expect(norm(formatMoney(12.5, 'USD'))).toBe('12,50 USD');
    expect(norm(formatMoney(1234.56))).toBe('1 234,56');
  });

  it('целая сумма — без хвоста «,00»', () => {
    expect(norm(formatMoney(1500, 'USD'))).toBe('1 500 USD');
    // Шум плавающей точки не превращается в копейки.
    expect(norm(formatMoney(1500.0000001, 'USD'))).toBe('1 500 USD');
    expect(norm(formatMoney(0.1 + 0.2))).toBe('0,30');
  });

  it('сумы — всегда целые (тийины не считают)', () => {
    expect(norm(formatMoney(1234567.4, 'UZS'))).toBe('1 234 567 UZS');
    expect(norm(formatMoney(99.5, 'uzs'))).toBe('100 uzs');
  });

  it('отрицательная сумма с копейками', () => {
    expect(norm(formatMoney(-12.5, 'USD'))).toBe('-12,50 USD');
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

describe('пункт «Период…» (UI-BUG-02 → UI-бриф п.5)', () => {
  const presets = [{ id: 'week', label: 'Неделя' }];

  it('при выбранном пресете — подпись «Период…», доступная и глазу, и скринридеру', () => {
    const html = periodSegHtml(presets, 'week', 'data-period', false, '');
    expect(html).toContain('aria-label="Выбрать период"');
    expect(html).toContain('Период…');
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

describe('деньги: подпись суммы по валютам', () => {
  const block = (over) => ({
    by_currency: [], base_total: null, base_currency: 'USD', partial: false, count: 0, ...over,
  });

  it('пустой блок — прочерк, а не «0 USD»', () => {
    expect(moneyBlockLabel(block())).toBe('—');
    expect(moneyBlockLabel(null)).toBe('—');
  });

  it('одна валюта показывается как есть, без псевдоточного «≈»', () => {
    const html = moneyBlockLabel(block({
      count: 2, base_total: 1000, by_currency: [{ currency: 'USD', total: 1000 }],
    }));
    expect(html).toContain('USD');
    expect(html).not.toContain('≈');
  });

  it('несколько валют сводятся к базовой через ≈', () => {
    expect(moneyBlockLabel(block({
      count: 2, base_total: 1080,
      by_currency: [{ currency: 'USD', total: 1000 }, { currency: 'UZS', total: 1000000 }],
    }))).toContain('≈');
  });

  it('о несчитанной части говорим вслух — по этой цифре принимают решения', () => {
    const html = moneyBlockLabel(block({
      count: 2, base_total: 1000, partial: true,
      by_currency: [{ currency: 'USD', total: 1000 }, { currency: 'UZS', total: 5000 }],
    }));
    expect(html).toContain('без курса');
  });

  it('без единого курса перечисляем валюты, а не молчим', () => {
    const html = moneyBlockLabel(block({
      count: 2, base_total: null,
      by_currency: [{ currency: 'UZS', total: 5000 }, { currency: 'KZT', total: 700 }],
    }));
    expect(html).toContain('UZS');
    expect(html).toContain('KZT');
  });
});

describe('деньги: бары дебиторки', () => {
  const bucket = (key, label, total, count) => ({
    key, label, count, base_total: total, base_currency: 'USD', partial: false,
    by_currency: total == null ? [] : [{ currency: 'USD', total }],
  });
  const aging = {
    buckets: [
      bucket('overdue_90', 'Просрочено >90 дней', 1000, 2),
      bucket('overdue_60', 'Просрочено 60—90', null, 0),
      bucket('not_due', 'Срок не наступил', 4000, 8),
    ],
  };

  it('ширина считается от самой большой корзины, а не от суммы', () => {
    // Иначе при одной доминирующей все прочие схлопываются в невидимую полоску.
    const html = agingBarsHtml(aging);
    expect(html).toContain('width:25%');   // 1000 из 4000
    expect(html).toContain('width:100%');  // сама большая
  });

  it('пустая корзина остаётся строкой с нулевым баром', () => {
    const html = agingBarsHtml(aging);
    expect(html).toContain('Просрочено 60—90');
    expect(html).toContain('width:0%');
  });

  it('полностью пустая дебиторка — не пустые бары, а «долгов нет»', () => {
    const html = agingBarsHtml({ buckets: [bucket('not_due', 'Срок не наступил', null, 0)] });
    expect(html).toContain('Долгов нет');
    expect(html).not.toContain('aging-bar');
  });

  it('подпись корзины экранируется', () => {
    const html = agingBarsHtml({ buckets: [bucket('x', '<img src=x>', 10, 1)] });
    expect(html).not.toContain('<img');
  });

  it('склонение по числу документов', () => {
    expect(agingBarsHtml({ buckets: [bucket('a', 'A', 10, 1)] })).toContain('1 документ');
    expect(agingBarsHtml({ buckets: [bucket('a', 'A', 10, 5)] })).toContain('5 документов');
  });
});

describe('деньги: прогноз поступлений', () => {
  const month = (m, total, count, machines) => ({
    month: m, count, base_total: total, base_currency: 'USD', partial: false,
    by_currency: total == null ? [] : [{ currency: 'USD', total }],
    machines: machines || { count: 0, by_currency: [], base_total: null, partial: false },
  });

  it('месяц без поступлений не выбрасывается — это тоже ответ', () => {
    const html = forecastRowsHtml([month('2026-08', 3000, 2), month('2026-09', null, 0)]);
    expect(html).toContain('авг 26');
    expect(html).toContain('сен 26');
  });

  it('доля техники подписывается отдельно', () => {
    const html = forecastRowsHtml([month('2026-08', 5000, 2, {
      count: 1, base_total: 4000, base_currency: 'USD', partial: false,
      by_currency: [{ currency: 'USD', total: 4000 }],
    })]);
    expect(html).toContain('техника');
  });

  it('пустой прогноз объясняет себя', () => {
    expect(forecastRowsHtml([])).toContain('Нечего прогнозировать');
  });
});

describe('деньги: ключ покупателя', () => {
  it('схлопывает регистр и лишние пробелы', () => {
    // Иначе один человек выглядит как двое должников.
    expect(buyerKey('Иванов  П.')).toBe(buyerKey('иванов п.'));
    expect(buyerKey('  Иванов П. ')).toBe('иванов п.');
  });

  it('пустое имя не роняет', () => {
    expect(buyerKey(null)).toBe('');
    expect(buyerKey(undefined)).toBe('');
  });
});

describe('воронка обращений', () => {
  const f = (over) => ({
    contacted: 10, replied: 8, won: 3, lost: 2, in_progress: 5,
    never_answered: 2, awaiting_reply: 1, silent: 1,
    reengaged: 2, reengaged_won: 1, reply_rate: 0.8, win_rate: 0.3, ...over,
  });

  it('ступени считаются от первой, а не от максимума', () => {
    // Воронка по определению сужается: «обратились» — это её 100%.
    const html = leadFunnelHtml(f());
    expect(html).toContain('width:100%');   // обратились
    expect(html).toContain('width:80%');    // ответили
    expect(html).toContain('width:30%');    // купили
  });

  it('показывает и число, и долю — одно без другого обманывает', () => {
    const html = leadFunnelHtml(f());
    expect(html).toContain('8 · 80%');
  });

  it('хвосты объясняют потери и требуют действия', () => {
    const html = leadFunnelHtml(f());
    expect(html).toContain('ждут ответа: 1');
    expect(html).toContain('вернулись: 2');
    expect(html).toContain('купили 1');
  });

  it('нулевые хвосты не печатаются', () => {
    const html = leadFunnelHtml(f({ silent: 0, lost: 0, reengaged: 0, never_answered: 0 }));
    expect(html).not.toContain('замолчали');
    expect(html).not.toContain('вернулись');
  });

  it('без обращений — объяснение, а не пустые полоски', () => {
    expect(leadFunnelHtml(f({ contacted: 0 }))).toContain('Обращений пока нет');
    expect(leadFunnelHtml(null)).toContain('Обращений пока нет');
  });
});

describe('кто заговорил первым', () => {
  const f = {
    contacted: 5,
    by_direction: {
      inbound: { contacted: 3, replied: 3, won: 2, lost: 0, win_rate: 0.667 },
      outbound: { contacted: 2, replied: 1, won: 0, lost: 1, win_rate: 0 },
    },
  };

  it('показывает обе половины с их собственной конверсией', () => {
    // Смешивать нельзя: у написавшего самому интерес уже есть, второго ещё надо
    // заинтересовать — одной цифрой эти две работы не описать.
    const html = firstTouchHtml(f);
    expect(html).toContain('Клиент написал сам');
    expect(html).toContain('Написали мы первыми');
    expect(html).toContain('67%');
    expect(html).toContain('0%');
  });

  it('пустую половину не рисует', () => {
    const only = { by_direction: { inbound: { contacted: 4, won: 1, win_rate: 0.25 }, outbound: { contacted: 0 } } };
    const html = firstTouchHtml(only);
    expect(html).toContain('Клиент написал сам');
    expect(html).not.toContain('Написали мы первыми');
  });

  it('без данных не рисует ничего, а не пустую карточку', () => {
    expect(firstTouchHtml({})).toBe('');
    expect(firstTouchHtml(null)).toBe('');
  });
});

describe('скорость ответа', () => {
  it('показывает типичный случай и хвост, а не среднее', () => {
    const html = replySpeedHtml({ answered: 12, median_minutes: 40, p90_minutes: 380 });
    expect(html).toContain('Обычно отвечаем за');
    expect(html).toContain('40 мин');
    expect(html).toContain('Каждый десятый ждёт дольше');
    expect(html).toContain('6.3 ч');
    expect(html).not.toContain('среднее');
  });

  it('без ответов блок не рисуется', () => {
    // «Обычно отвечаем за —» хуже, чем отсутствие строки.
    expect(replySpeedHtml({ answered: 0, median_minutes: null, p90_minutes: null })).toBe('');
    expect(replySpeedHtml(null)).toBe('');
  });

  it('длительность читается словами', () => {
    expect(durationLabel(0)).toBe('0 мин');
    expect(durationLabel(45)).toBe('45 мин');
    expect(durationLabel(60)).toBe('1 ч');
    expect(durationLabel(150)).toBe('2.5 ч');
    expect(durationLabel(60 * 30)).toBe('1.3 дн');
    expect(durationLabel(null)).toBe('—');
    expect(durationLabel(-5)).toBe('—');
  });
});

describe('отклик на пост в канале', () => {
  it('говорит «после поста», а не «из поста»', () => {
    // Ссылка под постом ведёт прямо в личку менеджера и метки не несёт —
    // кто именно пришёл с публикации, мы не знаем и делать вид не будем.
    const label = postEffectLabel({ after: 7, baseline: 1.7, window_hours: 24 });
    expect(label).toContain('после поста');
    expect(label).not.toContain('из поста');
    expect(label).toContain('7');
    expect(label).toContain('1,7');
  });

  it('при полном нуле строки нет', () => {
    expect(postEffectLabel({ after: 0, baseline: 0, window_hours: 24 })).toBe('');
    expect(postEffectLabel(null)).toBe('');
  });
});

describe('склад: форматирование', () => {
  it('whMoney печатает копейки как деньги с двумя знаками', () => {
    // Два знака обязательны: «750 USD» в накладной читается как другая сумма.
    expect(whMoney(75000, 'USD')).toBe('750,00 USD');
    expect(whMoney(9999, 'USD')).toBe('99,99 USD');
    expect(whMoney(1, 'UZS')).toBe('0,01 UZS');
  });

  it('whMoney не теряет копейку на дробных суммах', () => {
    expect(whMoney(253, 'USD')).toBe('2,53 USD');
  });

  it('whMoney: пустое/нулевое — ноль, а не NaN', () => {
    expect(whMoney(0, 'USD')).toBe('0,00 USD');
    expect(whMoney(null, 'USD')).toBe('0,00 USD');
    expect(whMoney(undefined, 'USD')).toBe('0,00 USD');
  });

  it('whMoney экранирует валюту (приходит из БД)', () => {
    expect(whMoney(100, '<b>')).toBe('1,00 &lt;b&gt;');
  });

  it('whMoney без валюты печатает только число (строка «Итого»)', () => {
    expect(whMoney(75000, '')).toBe('750,00');
  });

  it('whQty срезает хвостовые нули и держит дробные', () => {
    expect(whQty(3)).toBe('3');
    expect(whQty(3.0)).toBe('3');
    expect(whQty(2.5)).toBe('2,5');
    expect(whQty(0)).toBe('0');
    expect(whQty(null)).toBe('0');
  });

  it('whStockBadge: нет остатка / мало / достаточно', () => {
    // Состояние — в data-status, цвет выводится из переменных CSS.
    expect(whStockBadge(0)).toContain('нет');
    expect(whStockBadge(0)).toContain('data-status="out"');
    expect(whStockBadge(-1)).toContain('нет');
    expect(whStockBadge(5)).toContain('data-status="low"');
    expect(whStockBadge(500)).toContain('data-status="in_stock"');
    expect(whStockBadge(2.5)).toContain('2,5');
  });
});

describe('navBarLayout / navDrawerHtml (шторка «Меню»)', () => {
  const { navBarLayout, navDrawerHtml, NAV_BAR_MAX } = helpers;
  const S = (keys) => keys.map((key) => ({ key, label: key, icon: 'box' }));

  it('пять разделов с вкладками — четыре в панели и «Меню»', () => {
    const l = navBarLayout(S(['a', 'b', 'c', 'd', 'e']), (k) => (k === 'c' ? 4 : 1));
    expect(NAV_BAR_MAX).toBe(4);
    expect(l.menu).toBe(true);
    expect(l.bar.map((s) => s.key)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('шторка, повторяющая панель, не нужна', () => {
    const l = navBarLayout(S(['a', 'b', 'c']), () => 1);
    expect(l).toEqual({ bar: S(['a', 'b', 'c']), menu: false });
    // Разделов больше пяти — «Меню» даже без вкладок: панель не резиновая.
    expect(navBarLayout(S(['a', 'b', 'c', 'd', 'e', 'f']), () => 0).bar.length).toBe(4);
    expect(navBarLayout(S(['a', 'b']), (k) => (k === 'b' ? 2 : 0)).bar.length).toBe(2);
  });

  it('дерево «раздел → вкладки», подсвечен ровно один пункт', () => {
    const groups = [
      { key: 'today', label: 'Сегодня', icon: 'home', tabs: [] },
      { key: 'stock', label: 'Склад', icon: 'box',
        tabs: [{ key: 'catalog', label: 'Каталог' }, { key: 'invoices', label: 'Накладные' }] },
      { key: 'sales', label: 'Продажи', icon: 'cart', tabs: [{ key: 'orders', label: 'Заказы' }] },
    ];
    const html = navDrawerHtml(groups, { screen: 'stock', tab: 'invoices' }, { subtitle: 'Менеджер' });
    expect(html.match(/aria-current="page"/g)).toHaveLength(1);
    expect(html).toMatch(/is-current" aria-current="page" data-screen="stock" data-tab="invoices"/);
    expect(html).toContain('nav-link--section is-within" data-screen="stock"');
    // Одна вкладка — подпунктов нет, как и ряда .seg.
    expect(html).not.toContain('data-tab="orders"');
    expect(html).toContain('id="nav-drawer-title"');
    expect(html).toContain('Менеджер');
    expect(navDrawerHtml(groups, { screen: 'today' })).toMatch(/is-current" aria-current="page" data-screen="today"/);
  });

  it('подписи экранируются', () => {
    const html = navDrawerHtml([{ key: 'x', label: '<b>', icon: 'box', tabs: [] }], {}, { subtitle: '<i>' });
    expect(html).not.toContain('<b>');
    expect(html).not.toContain('<i>');
  });
});


describe('руководитель: «Рабочие действия» (решение владельца)', () => {
  const {
    roleSectionTabs, workActionsOn, deleteActionsOn, resolveScreen, navBarLayout,
    navDrawerHtml, workSwitchHtml, deleteSwitchHtml, isBossLike,
  } = helpers;
  const tabs = (section, role, work) => roleSectionTabs(section, role, { work }).map(t => t.key);
  const SECTIONS = ['sales', 'stock', 'money', 'clients'];

  it('выключатель — только у руководства, по умолчанию выключен', () => {
    expect(workActionsOn('boss', undefined)).toBe(false);
    expect(workActionsOn('boss', { work_actions: false })).toBe(false);
    expect(workActionsOn('boss', { work_actions: true })).toBe(true);
    expect(workActionsOn('admin', {})).toBe(false);
    // Остальным ролям кнопки видны всегда: их интерфейс не менялся.
    for (const r of ['manager', 'warehouse_keeper', 'bookkeeper']) {
      expect(workActionsOn(r, undefined)).toBe(true);
      expect(workActionsOn(r, { work_actions: false })).toBe(true);
    }
    expect(isBossLike('admin')).toBe(true);
    expect(isBossLike('manager')).toBe(false);
  });

  it('выключено: только смотреть, решать, контролировать', () => {
    for (const r of ['boss', 'admin']) {
      expect(tabs('sales', r, false)).toEqual(['orders', 'report']);
      expect(tabs('stock', r, false)).toEqual(['catalog', 'containers', 'machines']);
      // «Сверка кассы» остаётся и без «Рабочих действий»: для руководителя
      // это контроль, а не работа склада (см. moneyTabs).
      expect(tabs('money', r, false)).toEqual(['debts', 'reconcile', 'report']);
      expect(tabs('clients', r, false)).toEqual(['funnel', 'limits']);
    }
  });

  it('включено: работа менеджера возвращается, подтверждения остаются в «Решениях»', () => {
    expect(tabs('sales', 'boss', true)).toEqual(['orders', 'report', 'docs']);
    expect(tabs('stock', 'boss', true)).toEqual(['catalog', 'containers', 'machines', 'invoices']);
    expect(tabs('money', 'boss', true)).toEqual(['debts', 'ops', 'reconcile', 'report']);
    expect(tabs('clients', 'boss', true)).toEqual(['funnel', 'list', 'limits', 'channel']);
  });

  it('менеджер и склад: вкладки те же при любом значении выключателя', () => {
    const expected = {
      manager: {
        sales: ['orders', 'report', 'docs'],
        stock: ['catalog', 'containers', 'machines', 'invoices'],
        money: ['confirm', 'debts', 'ops', 'reconcile'],
        clients: ['list'],
      },
      warehouse_keeper: { sales: ['orders'], stock: ['catalog'], money: ['confirm'], clients: ['list'] },
      bookkeeper: { sales: ['orders'], stock: ['catalog'], money: ['confirm'], clients: ['list'] },
    };
    for (const [r, bySection] of Object.entries(expected)) {
      for (const sec of SECTIONS) {
        expect(tabs(sec, r, false), `${r}/${sec}`).toEqual(bySection[sec]);
        expect(tabs(sec, r, true), `${r}/${sec}`).toEqual(bySection[sec]);
      }
    }
  });

  it('панель руководителя: Сегодня · Решения · Деньги · Продажи · Меню', () => {
    for (const work of [false, true]) {
      const layout = navBarLayout(helpers.navSections('boss'),
        (k) => roleSectionTabs(k, 'boss', { work }).length);
      expect(layout.bar.map(s => s.key)).toEqual(['today', 'decisions', 'money', 'sales']);
      expect(layout.menu).toBe(true);
    }
    // Менеджер — как было.
    const mgr = navBarLayout(helpers.navSections('manager'),
      (k) => roleSectionTabs(k, 'manager', {}).length);
    expect(mgr.bar.map(s => s.key)).toEqual(['today', 'sales', 'stock', 'money']);
  });

  it('удаление: руководству всегда, менеджеру — пока не требуется руководитель', () => {
    expect(deleteActionsOn('boss', {})).toBe(true);
    expect(deleteActionsOn('boss', { delete_requires_boss: true })).toBe(true);
    expect(deleteActionsOn('admin', null)).toBe(true);
    expect(deleteActionsOn('manager', undefined)).toBe(true);      // поля нет — выключено
    expect(deleteActionsOn('manager', { delete_requires_boss: false })).toBe(true);
    expect(deleteActionsOn('manager', { delete_requires_boss: true })).toBe(false);
  });

  it('старые адреса подтверждений ведут руководителя в «Решения», остальных — на место', () => {
    expect(resolveScreen('boss', 'money', 'confirm')).toEqual({ screen: 'decisions', tab: '' });
    expect(resolveScreen('admin', 'requests', '')).toEqual({ screen: 'decisions', tab: '' });
    expect(resolveScreen('boss', 'money', 'debts')).toEqual({ screen: 'money', tab: 'debts' });
    expect(resolveScreen('manager', 'money', 'confirm')).toEqual({ screen: 'money', tab: 'confirm' });
    expect(resolveScreen('manager', 'decisions', '')).toEqual({ screen: 'money', tab: 'confirm' });
    expect(resolveScreen('bookkeeper', 'settings', '')).toEqual({ screen: 'money', tab: 'confirm' });
  });

  it('шторка: бейдж «Решений» и выключатель с состоянием словами', () => {
    const groups = [
      { key: 'today', label: 'Сегодня', icon: 'home', tabs: [] },
      { key: 'decisions', label: 'Решения', icon: 'decide', tabs: [], badge: 3 },
    ];
    const off = navDrawerHtml(groups, { screen: 'today' }, { workSwitch: { on: false } });
    expect(off).toMatch(/data-screen="decisions"[^>]*>.*Решения.*<span class="stock-badge badge-yellow">3<\/span>/);
    expect(off).toContain('role="switch" aria-checked="false"');
    expect(off).toContain('только решения и контроль');
    const on = navDrawerHtml(groups, { screen: 'today' }, { workSwitch: { on: true } });
    expect(on).toContain('aria-checked="true"');
    expect(on).toContain('видны кнопки менеджера');
    // Без opts.workSwitch (менеджер) выключателя нет.
    expect(navDrawerHtml(groups, { screen: 'today' }, {})).not.toContain('data-work-switch');
    expect(workSwitchHtml(true, 'c-row" onclick="x')).not.toContain('" onclick');
  });

  it('deleteSwitchHtml: положение словами и бегунком, класс не ломает разметку', () => {
    const off = deleteSwitchHtml(false, 'c-row');
    expect(off).toContain('role="switch" aria-checked="false"');
    expect(off).toContain('data-delete-switch');
    expect(off).not.toContain('data-work-switch');
    expect(off).toContain('может и менеджер');
    expect(deleteSwitchHtml(true)).toContain('только руководитель');
    expect(deleteSwitchHtml(true, 'c-row" onclick="x')).not.toContain('" onclick');
  });
});

describe('скидка к прайсу (C2/C5)', () => {
  const {
    discountPctLabel, discountLineSuffix, discountSummaryText, discountBlockHtml,
    discountPendingNote,
  } = helpers;

  it('подпись процента — как в карточке сделки по технике', () => {
    expect(discountPctLabel(4)).toBe('скидка 4%');
    expect(discountPctLabel(-4)).toBe('выше прайса на 4%');
    expect(discountPctLabel(0)).toBe('по прайсу');
    expect(discountPctLabel(null)).toBe('—');
    expect(discountPctLabel(undefined)).toBe('—');
  });

  it('хвост строки: прайс и скидка; без прайса хвоста нет', () => {
    expect(discountLineSuffix({ ref_price: 100, discount_pct: 30 }, 'USD'))
      .toBe(' · прайс 100 USD · скидка 30%');
    // Новый товар без прайса: «скидка 0%» соврала бы, поэтому пусто.
    expect(discountLineSuffix({ ref_price: null, discount_pct: null }, 'USD')).toBe('');
    expect(discountLineSuffix(null, 'USD')).toBe('');
  });

  it('сводка по заказу: средняя, максимум и позиции без прайса', () => {
    expect(discountSummaryText({ avg_pct: 29.7, max_pct: 30, covered_lines: 2, total_lines: 2 }))
      .toBe('Скидка по заказу: скидка 29.7% · максимум по позиции 30%');
    expect(discountSummaryText({ avg_pct: 4, max_pct: 4, covered_lines: 1, total_lines: 2 }))
      .toBe('Скидка по заказу: скидка 4% · без прайса: 1');
    // Сравнивать не с чем — блока нет вовсе.
    expect(discountSummaryText({ covered_lines: 0, total_lines: 3 })).toBe('');
    expect(discountBlockHtml({ covered_lines: 0 })).toBe('');
  });

  it('помеченная скидка красится тем же credit-ctx--bad, что превышение лимита', () => {
    const bad = discountBlockHtml({
      avg_pct: 30, max_pct: 30, covered_lines: 1, total_lines: 1, flagged: true, threshold_pct: 15,
    });
    expect(bad).toContain('credit-ctx--bad');
    expect(bad).toContain('порог 15% — нужно явное решение');
    const ok = discountBlockHtml({
      avg_pct: 4, max_pct: 4, covered_lines: 1, total_lines: 1, flagged: false, threshold_pct: 15,
    });
    expect(ok).toContain('credit-ctx--ok');
    expect(ok).not.toContain('нужно явное решение');
  });

  it('менеджеру — почему заявка ждёт; ниже порога строки нет', () => {
    expect(discountPendingNote({ flagged: true, max_pct: 30, threshold_pct: 15 }))
      .toBe('Ждёт одобрения из-за скидки 30% (порог 15%) — решение принимает руководитель.');
    expect(discountPendingNote({ flagged: false, max_pct: 4, threshold_pct: 15 })).toBe('');
    expect(discountPendingNote(null)).toBe('');
  });
});
