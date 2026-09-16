// Чистые утилиты WebApp-фронта, вынесенные из app.js, чтобы их можно было
// юнит-тестировать (Vitest) в Node без браузера. UMD-обёртка:
//   • в браузере — функции становятся глобальными (как были объявлены в app.js
//     classic-script'ом), helpers.js подключается в index.html ПЕРЕД app.js;
//   • в Node/Vitest — экспортируются через module.exports.
// Поведение функций идентично прежним определениям в app.js (байт-в-байт тела).
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api; // Node / Vitest
  } else {
    for (const k in api) root[k] = api[k]; // Браузер: глобалы (как было в app.js)
  }
})(typeof self !== 'undefined' ? self : this, function () {
  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Ключ идемпотентности для денежных/складских действий: защищает от
  // double-submit. Сервер дедуплицирует по нему.
  function idemKey() {
    try {
      if (self.crypto && self.crypto.randomUUID) return self.crypto.randomUUID();
    } catch (e) { /* старый WebView без crypto.randomUUID / нет self в Node */ }
    return Date.now() + '-' + Math.random().toString(16).slice(2);
  }

  // Дата YYYY-MM-DD → ДД.ММ.ГГГГ
  function formatDateRU(iso) {
    if (!iso || iso.length < 10) return iso || '—';
    const [y, m, d] = iso.slice(0, 10).split('-');
    return `${d}.${m}.${y}`;
  }

  // SVG-иконка из спрайта (см. <defs> в index.html). Возвращает <svg><use>,
  // который красится currentColor → тематизируется под тему/активный таб.
  // Имя санитизируется (только [a-z0-9-]), чтобы name не мог сломать разметку.
  function icon(name, cls) {
    const safe = String(name || '').replace(/[^a-z0-9-]/g, '');
    const extra = cls ? ' ' + String(cls).replace(/[^a-z0-9 _-]/g, '') : '';
    return `<svg class="ic${extra}" aria-hidden="true"><use href="#ic-${safe}"/></svg>`;
  }

  // Целое число с разделением разрядов пробелом: 12345 → "12 345".
  function opsAmount(n) {
    const v = Math.round(Number(n) || 0);
    return String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  }

  // ВРЕМЕННОЕ совмещение ролей — зеркало services/roles.py (ROLE_ALSO_ACTS_AS;
  // расхождение ловит tests/test_roles_manager_acts_as.py). Кладовщика и
  // бухгалтера в штате пока нет, их работу делает менеджер, поэтому кнопки и
  // вкладки этих ролей рисуются и ему — сервер пускает его в те же ручки.
  // Откат — пустой объект здесь и в services/roles.py.
  const ROLE_ALSO_ACTS_AS = { manager: ['warehouse_keeper', 'bookkeeper'] };

  // Входит ли роль (с учётом совмещения) в список. Проверки «кладовщик/
  // бухгалтер» во фронте идут через неё, а не через `includes(role)`.
  function roleIn(role, roles) {
    const all = [role].concat(ROLE_ALSO_ACTS_AS[role] || []);
    return all.some((r) => (roles || []).indexOf(r) !== -1);
  }

  // Сумма, как её вводят на телефоне: «1 500», «1,5», «1 500,50» (пробел —
  // и неразрывный из буфера обмена — разделяет разряды, запятая — дробную
  // часть). Строго: «12abc» — это не 12, а ошибка ввода, иначе опечатка молча
  // уезжает деньгами. Возвращает число или NaN.
  function parseAmount(raw) {
    const t = String(raw == null ? '' : raw).replace(/[\s\u00a0\u202f]/g, '').replace(',', '.');
    if (!/^\d+(\.\d+)?$/.test(t)) return NaN;
    return Number(t);
  }

  // Парсинг строк мульти-валютной формы платежа в payload для /api/payments/send.
  // Чистая функция (тестируется): принимает [{amount, currency}] как ввёл юзер
  // (amount — строка/число), возвращает {items:[{amount:Number, currency}]} или
  // {error}. Запятая как десятичный разделитель, пробелы игнорируются.
  function parsePaymentItems(rows) {
    const items = [];
    for (const r of rows || []) {
      const amt = parseAmount(r && r.amount);
      if (!isFinite(amt) || amt <= 0) {
        return { error: 'Введите положительную сумму во всех строках' };
      }
      items.push({ amount: amt, currency: (r && r.currency) || 'USD' });
    }
    if (!items.length) return { error: 'Добавьте хотя бы одну строку' };
    return { items };
  }

  // ─── Разделы и их вкладки ──────────────────────────────────────────────────
  //
  // Правило структуры: раздел — это ПРЕДМЕТ, о котором думает человек, а не
  // отдел, который им занимается. Поэтому «Аналитики» как раздела нет: каждая
  // цифра лежит вкладкой «Отчёт» внутри того раздела, который она описывает.
  // Раздельность «данные тут, отчёт о них там» и была причиной, по которой
  // воронку обращений нельзя было найти, не зная заранее, где она.
  //
  // Роли режем ПО МАТРИЦЕ РУЧЕК (UI_QA_ROLES.md), а не по вкусу: таб, который
  // гарантированно ответит 403, — это дверь, которая не открывается.
  const NAV_SECTIONS = [
    { key: 'today',   label: 'Сегодня',  icon: 'home',
      roles: ['admin', 'boss', 'manager', 'warehouse_keeper', 'bookkeeper'] },
    // «Решения» — всё, что ждёт слова руководителя, в одном месте с общим
    // бейджем: заявки, оплаты картой/перечислением, сдачи, возвраты (и что
    // допишут провайдерами — DECISION_GROUPS в app.js). Менеджеру не рисуем:
    // /api/orders/requests ему отвечает 403, а его подтверждения — «Деньги →
    // Подтвердить», как и раньше.
    { key: 'decisions', label: 'Решения', icon: 'decide',
      roles: ['admin', 'boss'] },
    { key: 'sales',   label: 'Продажи',  icon: 'cart',
      roles: ['admin', 'boss', 'manager', 'warehouse_keeper', 'bookkeeper'] },
    { key: 'stock',   label: 'Склад',    icon: 'box',
      roles: ['admin', 'boss', 'manager'] },
    { key: 'money',   label: 'Деньги',   icon: 'wallet',
      roles: ['admin', 'boss', 'manager', 'warehouse_keeper', 'bookkeeper'] },
    { key: 'clients', label: 'Клиенты',  icon: 'user',
      roles: ['admin', 'boss', 'manager'] },
    // Реквизиты компании, курсы, выключатель «Рабочие действия». Только в
    // «Меню»: в панель не попадает (последний в порядке руководителя).
    { key: 'settings', label: 'Настройки', icon: 'settings',
      roles: ['admin', 'boss'] },
  ];

  // Руководитель смотрит, решает и контролирует (решение владельца), поэтому
  // его панель — «Сегодня · Решения · Деньги · Продажи · Меню», а склад,
  // клиенты и настройки — через «Меню». Порядок остальных ролей — порядок
  // таблицы: их интерфейс не менялся.
  const BOSS_ROLES = ['admin', 'boss'];
  const BOSS_NAV_ORDER = ['today', 'decisions', 'money', 'sales', 'stock', 'clients', 'settings'];

  function isBossLike(role) {
    return BOSS_ROLES.indexOf(role) !== -1;
  }

  function navSections(role) {
    const list = NAV_SECTIONS.filter((s) => s.roles.indexOf(role) !== -1);
    if (!isBossLike(role)) return list;
    return list.slice().sort((a, b) => BOSS_NAV_ORDER.indexOf(a.key) - BOSS_NAV_ORDER.indexOf(b.key));
  }

  // Экран по умолчанию для роли: первый доступный ей раздел. У кладовщика нет
  // «Сегодня» (ручка /api/home ему не отвечает), и открывать его на экране с
  // ошибкой — худшее, что можно сделать при входе.
  function defaultSection(role) {
    const list = navSections(role);
    return list.length ? list[0].key : null;
  }

  // ─── «Рабочие действия» руководителя ──────────────────────────────────────
  // Работа менеджера (накладные, отгрузка, касса, документы, лиды, канал,
  // моточасы, приёмка контейнера…) у руководителя спрятана за выключателем в
  // «Меню» — на экстренный день, когда менеджер заболел. Выключатель — личная
  // настройка ВИДА (`/api/me → prefs.work_actions`, хранит сервер), а не права:
  // ручки пускают руководителя туда же, куда и раньше. У остальных ролей
  // выключателя нет, и их кнопки видны всегда — интерфейс не менялся.
  function workActionsOn(role, prefs) {
    if (!isBossLike(role)) return true;
    return !!(prefs && prefs.work_actions);
  }

  // Удаление (техника, товары, накладные) — контроль, а не работа: у
  // руководителя видно всегда, за выключатель не прячется. Менеджеру — пока
  // настройка `delete_requires_boss` (app_settings, по умолчанию выкл.) не
  // включена. Права при этом режет сервер; поля нет — считаем выключенной.
  function deleteActionsOn(role, flags) {
    if (isBossLike(role)) return true;
    return !(flags && flags.delete_requires_boss);
  }

  // Продажи: заказы и отчёт по ним. Каталог отсюда уехал на «Склад» — как
  // отдельный экран это остатки, а внутри заказа товар выбирают в форме.
  function salesTabs(f) {
    f = f || {};
    const tabs = [{ key: 'orders', label: 'Заказы' }];
    if (f.canSeeReport) tabs.push({ key: 'report', label: 'Отчёт' });
    // Расписка/тилхат по продаже в долг: /api/docs/* отвечает тем же ролям,
    // что и создание заказов. Кладовщику вкладку не рисуем — ручка ответит 403.
    if (f.canDocs) tabs.push({ key: 'docs', label: 'Документы' });
    return tabs;
  }

  // Склад: всё, что физически лежит или едет. Контейнер в пути — это склад,
  // который ещё не приехал.
  function stockTabs(f) {
    f = f || {};
    const tabs = [{ key: 'catalog', label: 'Каталог' }];
    if (f.canSeeGoods) {
      tabs.push({ key: 'containers', label: 'Контейнеры' });
      tabs.push({ key: 'machines', label: 'Техника' });
    }
    // Накладные — самостоятельный список со своим действием «создать», поэтому
    // вкладка, а не кнопка между табами и поиском. «Залежалось» стало фильтром
    // каталога (только руководству — ручка /api/channel/stale отвечает
    // admin/boss): это срез того же списка товаров, а не другой экран.
    // `canInvoices: false` — руководитель без «Рабочих действий»: накладные —
    // работа склада.
    if (f.canSeeGoods && f.canInvoices !== false) tabs.push({ key: 'invoices', label: 'Накладные' });
    return tabs;
  }

  // Деньги: бывшие «Финансы» + бывшая «Аналитика → Деньги». Долги и дебиторка —
  // один предмет, и держать их в разных разделах значило требовать от человека
  // знать, в каком именно.
  function moneyTabs(f) {
    f = f || {};
    const tabs = [];
    if (f.isConfirmer) tabs.push({ key: 'confirm', label: 'Подтвердить' });
    // «Долги» — это «кто кому должен», обе стороны: нам должны клиенты и
    // должны мы поставщикам. «Поставщикам» отдельной вкладкой НЕ выходит —
    // потолок ряда четыре пункта, а с ней их стало бы пять у руководителя и
    // шесть у менеджера. Она живёт ВТОРЫМ УРОВНЕМ внутри «Долгов» («Клиенты ·
    // Поставщикам», app.js `moneyDebtsSubHtml`) — тот же приём, что у
    // «Склад → Накладные → Списания». Право режется там же, ручкой:
    // `/api/suppliers/debts` отвечает admin/boss (сумма прихода —
    // закупочная цена), и у менеджера второго уровня нет вовсе.
    if (f.canSeeDebts) tabs.push({ key: 'debts', label: 'Долги' });
    if (f.hasOps) tabs.push({ key: 'ops', label: 'Касса' });
    // Сверка кассы — у всех, кто её записывает (менеджер), и у руководства,
    // которое её читает. НЕ за «Рабочими действиями», в отличие от «Кассы»:
    // руководителю это контроль, а не работа склада, и спрятать список
    // расхождений за переключатель значило бы не показать его ровно тому,
    // ради кого он и заводился.
    if (f.canReconcile) tabs.push({ key: 'reconcile', label: 'Сверка' });
    if (f.isBoss) tabs.push({ key: 'report', label: 'Отчёт' });
    return tabs;
  }

  // Клиенты: всё про отношения с покупателем. Воронка переехала сюда из отчёта
  // о деньгах — переписка с клиентом не деньги.
  function clientsTabs(f) {
    f = f || {};
    // Список лидов — не роскошь: до него исход сделки можно было поставить
    // только тому, кто прямо сейчас висит без ответа. Клиент, которому ответили
    // и который потом замолчал, не находился вовсе.
    //
    // «Воронка» — только руководству: /api/leads/funnel отвечает admin/boss,
    // и у менеджера это была вкладка, которая гарантированно возвращала 403.
    // Первой она стояла потому, что раздел рисовали под босса; менеджер
    // открывал «Клиенты» и видел ошибку вместо своих лидов.
    //
    // `work: false` — руководитель без «Рабочих действий»: лиды и звонки,
    // канал — работа менеджера; воронка и лимиты — контроль, остаются.
    const work = f.work !== false;
    const tabs = [];
    if (f.isBoss) tabs.push({ key: 'funnel', label: 'Воронка' });
    if (!f.isBoss || work) tabs.push({ key: 'list', label: 'Лиды' });
    if (f.isBoss) {
      tabs.push({ key: 'limits', label: 'Лимиты' });
      if (work) tabs.push({ key: 'channel', label: 'Канал' });
    }
    return tabs;
  }

  // Вкладки раздела под роль — одна таблица на панель, шторку и ряд .seg
  // (app.js sectionTabsFor). opts.work — «Рабочие действия» (workActionsOn);
  // у ролей, кроме руководства, не влияет ни на что.
  function roleSectionTabs(section, role, opts) {
    const o = opts || {};
    const boss = isBossLike(role);
    const work = boss ? !!o.work : true;
    const working = ['admin', 'boss', 'manager'].indexOf(role) !== -1;
    if (section === 'sales') {
      return salesTabs({ canSeeReport: working, canDocs: working && work });
    }
    if (section === 'stock') {
      return stockTabs({ canSeeGoods: working, canInvoices: working && work });
    }
    if (section === 'money') {
      return moneyTabs({
        isBoss: boss,
        // У руководства подтверждения — в «Решениях», не второй дверью здесь.
        isConfirmer: !boss && roleIn(role, ['admin', 'boss', 'bookkeeper', 'warehouse_keeper']),
        canSeeDebts: working,
        // Касса — сдачи наличных, их создают менеджеры: /api/deposits/my и
        // /create кладовщику не отвечают, и вкладка у него возвращала 403.
        hasOps: working && work,
        // Сверку записывают и читают те же роли, что и `/api/cash/reconcile*`.
        canReconcile: working,
      });
    }
    if (section === 'clients') return clientsTabs({ isBoss: boss, work });
    return [];
  }

  // Куда на самом деле ведёт адрес. Руководителю «Деньги → Подтвердить» и
  // «Заявки» больше не отдельные места — они в «Решениях»; старые ссылки из
  // бота и пушей (`money:confirm`, `requests`) приводим туда. Остальным
  // «Решения» недоступны — ведём в их прежнее место подтверждений.
  function resolveScreen(role, screen, tab) {
    const boss = isBossLike(role);
    if (boss && (screen === 'requests' || (screen === 'money' && tab === 'confirm'))) {
      return { screen: 'decisions', tab: '' };
    }
    if (!boss && (screen === 'decisions' || screen === 'settings')) {
      return { screen: 'money', tab: 'confirm' };
    }
    return { screen, tab: tab || '' };
  }

  // Под-навигация раздела — ОДИН вид на все пять разделов. Раньше «Заказы»
  // рисовали .seg, а «Финансы» — .subseg, хотя уровень вложенности одинаковый.
  // Шелл обязан входить в КАЖДЫЙ innerHTML ветки (UI-BUG-04), включая скелетон
  // и ошибку, иначе первый же ре-рендер уносит переключатель.
  function sectionNavHtml(tabs, active) {
    tabs = tabs || [];
    if (tabs.length < 2) return '';
    const scroll = tabs.length > 3 ? ' seg--scroll' : '';
    const items = tabs.map((t) => {
      const on = t.key === active;
      const badge = t.badge
        ? ` <span class="stock-badge badge-yellow">${escapeHtml(String(t.badge))}</span>` : '';
      return `<button class="seg-item ${on ? 'active' : ''}" data-sect="${escapeHtml(t.key)}" ` +
             `aria-pressed="${on}">${escapeHtml(t.label)}${badge}</button>`;
    }).join('');
    // `scroll-hint` — обёртка, на которой рисуется затенение края (CSS), когда
    // ряд не влез. Ставим только скроллящемуся: у трёх вкладок края нет.
    const hint = scroll ? ' scroll-hint' : '';
    return `<div class="seg-row${hint}"><div class="seg${scroll}">${items}</div></div>`;
  }

  // ─── Нижняя панель + «Меню» (шторка разделов) ─────────────────────────────
  //
  // В панели — не больше NAV_BAR_MAX разделов и кнопка «Меню», если шторке
  // есть что показать сверх панели: раздел, не влезший в панель, или вкладки
  // (ряд из четырёх вкладок на телефоне уже не помещается, и найти четвёртую
  // можно было только пролистав ряд). Пятый слот панели — «Меню»: Apple и
  // Material сходятся на пяти пунктах как пределе нижней панели.
  //
  // Кнопка «Меню» стоит внизу, а не бургером в шапке: верхний левый угол —
  // самое дальнее место от большого пальца, и в Telegram прямо над шапкой
  // WebApp лежит «Закрыть» клиента — промах бургером закрывал бы приложение.
  const NAV_BAR_MAX = 4;

  // sections — navSections(role); tabsCount(key) — число вкладок раздела под
  // роль. Возвращает { bar: [раздел…], menu: bool }. Роли, у которых шторка
  // повторила бы панель один в один (кладовщик, бухгалтер: три раздела без
  // вкладок), получают панель как раньше — без кнопки-дубля.
  function navBarLayout(sections, tabsCount) {
    sections = sections || [];
    const count = (k) => (tabsCount ? Number(tabsCount(k)) || 0 : 0);
    const nested = sections.some((s) => count(s.key) >= 2);
    const menu = nested || sections.length > NAV_BAR_MAX + 1;
    return { bar: menu ? sections.slice(0, NAV_BAR_MAX) : sections.slice(), menu };
  }

  // Содержимое шторки: разделы роли и их вкладки деревом «раздел → пункты».
  // groups: [{ key, label, icon, tabs: [{ key, label, badge? }] }];
  // current: { screen, tab } — где человек сейчас.
  //
  // Подсвечен ровно один пункт (aria-current="page"): вкладка, если у раздела
  // есть подпункты, иначе сам раздел. Раздел, внутри которого человек, отмечен
  // иконкой акцентного цвета — видно, «где я», даже когда список прокручен.
  // Текущий пункт несёт галочку: состояние видно формой, а не только цветом
  // (на солнце цвет теряется первым).
  function navDrawerHtml(groups, current, opts) {
    groups = groups || [];
    current = current || {};
    opts = opts || {};
    const check = icon('check', 'nav-link-check');
    const link = ({ cls, on, screen, tab, inner }) =>
      `<button type="button" class="nav-link ${cls}${on ? ' is-current' : ''}"` +
      `${on ? ' aria-current="page"' : ''} data-screen="${escapeHtml(screen)}"` +
      `${tab ? ` data-tab="${escapeHtml(tab)}"` : ''}>${inner}${on ? check : ''}</button>`;
    const group = (g) => {
      const tabs = (g.tabs || []).length >= 2 ? g.tabs : [];
      const here = g.key === current.screen;
      // Бейдж раздела без вкладок («Решения»: сколько ждёт) — рядом с подписью.
      const headBadge = g.badge
        ? `<span class="stock-badge badge-yellow">${escapeHtml(String(g.badge))}</span>` : '';
      const head = link({
        cls: 'nav-link--section' + (here && tabs.length ? ' is-within' : ''),
        on: here && !tabs.length,
        screen: g.key,
        inner: `${icon(g.icon)}<span class="nav-link-label">${escapeHtml(g.label)}</span>${headBadge}`,
      });
      const sub = tabs.map((t) => {
        const badge = t.badge
          ? `<span class="stock-badge badge-yellow">${escapeHtml(String(t.badge))}</span>` : '';
        return '<li>' + link({
          cls: 'nav-link--tab',
          on: here && t.key === current.tab,
          screen: g.key,
          tab: t.key,
          inner: `<span class="nav-link-label">${escapeHtml(t.label)}</span>${badge}`,
        }) + '</li>';
      }).join('');
      return `<li class="nav-group">${head}${sub ? `<ul class="nav-sub">${sub}</ul>` : ''}</li>`;
    };
    const subtitle = opts.subtitle
      ? `<div class="nav-drawer-sub">${escapeHtml(opts.subtitle)}</div>` : '';
    // Выключатель «Рабочие действия» (только руководству): под деревом, в
    // той же прокрутке — он про то, ЧТО показывать в разделах выше.
    const foot = opts.workSwitch ? workSwitchHtml(!!opts.workSwitch.on, 'nav-switch') : '';
    return '<div class="nav-drawer-head">' +
      `<div><div class="nav-drawer-title" id="nav-drawer-title">Меню</div>${subtitle}</div>` +
      `<button type="button" class="nav-drawer-close" data-drawer-close aria-label="Закрыть меню">${icon('close')}</button>` +
      '</div>' +
      '<nav class="nav-drawer-body" aria-label="Разделы и вкладки">' +
      `<ul class="nav-tree">${groups.map(group).join('')}</ul>${foot}</nav>`;
  }

  // Выключатель «Рабочие действия»: кнопка role="switch" — состояние читается
  // и скринридером (aria-checked), и глазами: подпись под названием говорит,
  // что именно сейчас видно, а не только положение бегунка (на солнце цвет
  // теряется первым). Одна разметка на шторку и экран «Настройки».
  function workSwitchHtml(on, extraClass) {
    const hint = on
      ? 'Включено: видны кнопки менеджера — накладные, отгрузка, касса, лиды'
      : 'Выключено: только решения и контроль. Включите, если менеджера нет';
    const cls = extraClass ? ` ${String(extraClass).replace(/[^a-z0-9 _-]/g, '')}` : '';
    return `<button type="button" class="work-switch${cls}" role="switch" aria-checked="${on ? 'true' : 'false'}" data-work-switch>` +
      `<span class="work-switch-text"><span class="work-switch-title">Рабочие действия</span>` +
      `<span class="work-switch-hint">${escapeHtml(hint)}</span></span>` +
      '<span class="work-switch-track" aria-hidden="true"><span class="work-switch-thumb"></span></span>' +
      '</button>';
  }

  // Выключатель «Удаление — только руководитель» (экран «Настройки»). Это НЕ
  // личный вид, а общая настройка компании (`app_settings.delete_requires_boss`,
  // ручка /api/settings/delete_requires_boss): включена — менеджер больше не
  // удаляет технику и товары и не отменяет накладные. Тот же бегунок, что у
  // «Рабочих действий», — чтобы две настройки рядом читались одинаково.
  function deleteSwitchHtml(on, extraClass) {
    const hint = on
      ? 'Включено: удаляет технику и товары, отменяет накладные только руководитель'
      : 'Выключено: удалять и отменять накладные может и менеджер';
    const cls = extraClass ? ` ${String(extraClass).replace(/[^a-z0-9 _-]/g, '')}` : '';
    return `<button type="button" class="work-switch${cls}" role="switch" aria-checked="${on ? 'true' : 'false'}" data-delete-switch>` +
      `<span class="work-switch-text"><span class="work-switch-title">Удаление — только руководитель</span>` +
      `<span class="work-switch-hint">${escapeHtml(hint)}</span></span>` +
      '<span class="work-switch-track" aria-hidden="true"><span class="work-switch-thumb"></span></span>' +
      '</button>';
  }

  // Выключатель «Напоминания о долге клиенту» (экран «Настройки», B3):
  // `app_settings.client_debt_reminders_enabled`, ручка
  // /api/settings/client_debt_reminders. По умолчанию ВЫКЛ — рассылка живому
  // клиенту в Telegram требует явного решения владельца.
  function debtReminderSwitchHtml(on, extraClass) {
    const hint = on
      ? 'Включено: клиенту с привязанным Telegram шлём напоминание о просрочке'
      : 'Выключено: о долге напоминаем только себе (менеджеру и руководству)';
    const cls = extraClass ? ` ${String(extraClass).replace(/[^a-z0-9 _-]/g, '')}` : '';
    return `<button type="button" class="work-switch${cls}" role="switch" aria-checked="${on ? 'true' : 'false'}" data-debt-reminder-switch>` +
      `<span class="work-switch-text"><span class="work-switch-title">Напоминания о долге клиенту</span>` +
      `<span class="work-switch-hint">${escapeHtml(hint)}</span></span>` +
      '<span class="work-switch-track" aria-hidden="true"><span class="work-switch-thumb"></span></span>' +
      '</button>';
  }

  // Рендер блока «Итоги» раздела «Деньги» (данные /api/money/summary):
  // подтверждённые платежи по валютам + сдачи наличных. Чистая функция.
  function renderMoneyTotalsHtml(summary) {
    summary = summary || {};
    const pays = summary.payments || [];
    const dep = summary.deposits || { total_cents: 0, count: 0 };
    const baseCurrency = summary.base_currency || 'USD';
    const missing = summary.missing_rates || [];
    const fmtC = (cents) => opsAmount((Number(cents) || 0) / 100);
    const row = (title, sub) =>
      `<div class="stock-row"><div class="stock-info">` +
      `<div class="stock-name">${escapeHtml(title)}</div>` +
      `<div class="stock-folder">${escapeHtml(sub)}</div></div></div>`;
    if (!pays.length && !(dep.count > 0)) {
      return '<div class="loader">За период поступлений нет</div>';
    }
    // Итог в базовой валюте. Валюты без курса НЕ теряем молча (был баг: крупные
    // суммы без курса, напр. UZS, исчезали из «≈ …») — показываем их явным блоком.
    let head = '';
    if (summary.base_total != null) {
      const partial = missing.length
        ? ' <span class="money-total-note">(неполный)</span>'
        : '';
      head =
        `<div class="money-total">≈ ${opsAmount(summary.base_total)} ` +
        `${escapeHtml(baseCurrency)}${partial}</div>`;
      // Пересчёт идёт по курсу дня подтверждения (снимок), а не по сегодняшнему:
      // иначе прошлые поступления «плыли» бы вместе с курсом. Говорим об этом,
      // только когда пересчитывать есть что — одна валюта в пояснении не нуждается.
      if (pays.some((p) => p.currency && p.currency !== baseCurrency)) {
        head +=
          `<div class="money-total-hint">В ${escapeHtml(baseCurrency)} — ` +
          `по курсу на день поступления.</div>`;
      }
    }
    if (missing.length) {
      const list = missing.map((m) => `${m.currency} ${opsAmount(m.amount)}`).join(', ');
      head +=
        `<div class="money-total-note">Без курса не учтено: ` +
        `${escapeHtml(list)} — задайте курс валют.</div>`;
    }
    const rows = pays
      .map((p) => row(`${p.currency} · ${fmtC(p.total_cents)}`,
        plural(p.count, ['платёж', 'платежа', 'платежей'])))
      .join('');
    const depRow = row(
      `Наличные (сдачи) · ${fmtC(dep.total_cents)} ${baseCurrency}`,
      plural(dep.count || 0, ['сдача', 'сдачи', 'сдач'])
    );
    return `${head}<div class="stock-list">${rows}${depRow}</div>`;
  }

  // Разбор МС-баланса контрагента (взаиморасчёты) для отображения — ЕДИНЫЙ
  // источник инвертированной конвенции знака (WP-27): <0 — клиент ДОЛЖЕН нам,
  // >0 — аванс/переплата. Раньше тернарники дублировались в renderClients и
  // renderAgentDetail и уже разъезжались (был sign-баг). Возвращает {state,
  // amount, currency}; разметку каждый экран строит сам.

  // ─── Общие состояния экрана (UI-WP-09) ──────────────────────────────────
  // Пустое состояние собиралось инлайн в двадцати девяти местах app.js, и
  // разметка успела разойтись: где-то не было иконки, где-то подсказки, где-то
  // кнопка действия шла до подсказки. Экран без данных пользователь видит чаще
  // всего в первый день работы — именно он и был самым несогласованным.
  //
  // action — {label, onclick} или {label, id}: inline-onclick оставлен для
  // location.reload()-случаев, id — чтобы навесить обработчик после вставки.
  function emptyState(opts) {
    const o = opts || {};
    const parts = [`<div class="empty-state-icon">${icon(o.icon || 'box')}</div>`];
    if (o.title) parts.push(`<div class="empty-state-title">${escapeHtml(o.title)}</div>`);
    if (o.hint) parts.push(`<div class="empty-state-hint">${escapeHtml(o.hint)}</div>`);
    const a = o.action;
    if (a && a.label) {
      const attr = a.onclick ? ` onclick="${a.onclick}"` : (a.id ? ` id="${a.id}"` : '');
      parts.push(`<button class="btn-primary"${attr}>${escapeHtml(a.label)}</button>`);
    }
    return `<div class="empty-state">${parts.join('')}</div>`;
  }

  // Скелетон под КАРКАС конкретного экрана: пользователь должен увидеть форму
  // будущего контента, а не абстрактный спиннер. Виды покрывают то, что реально
  // есть в приложении; список принимает количество строк.
  function skeleton(kind, n) {
    const one = (cls) => `<div class="sk ${cls}"></div>`;
    switch (kind) {
      case 'hero':   return one('sk-hero');
      case 'grid4':  return `<div class="sk-grid">${Array(4).fill(one('sk-action')).join('')}</div>`;
      case 'label':  return one('sk-label');
      case 'stat3':  return `<div class="sk-grid sk-grid--3">${Array(3).fill(one('sk-card')).join('')}</div>`;
      case 'list': {
        // Пропущенный аргумент — три строки; явный 0 или мусор — одна.
        // `Number(n) || 3` считал бы ноль пропуском и рисовал три.
        const rows = n == null ? 3 : Math.max(1, Math.floor(Number(n) || 0));
        return Array(rows).fill(one('sk-card')).join('');
      }
      default:       return one('sk-card');
    }
  }

  // Ошибка загрузки с кнопкой «Повторить». Офлайн отличаем от ошибки сервера:
  // при пропавшей сети технический detail пользователю бесполезен, а
  // «проверьте интернет» — действие, которое он может выполнить сам.
  function errorBoxHtml(msg, opts) {
    const o = opts || {};
    const offline = (typeof navigator !== 'undefined' && navigator.onLine === false)
      || msg === 'Нет подключения к интернету'
      // Текст сетевого слоя (net.js NET_ERROR_TEXT): нет ответа или истёк срок.
      || msg === 'Нет связи — проверьте интернет и повторите';
    const title = offline ? 'Нет подключения' : 'Не удалось загрузить';
    const body = offline ? 'Проверьте интернет и попробуйте снова.' : escapeHtml(String(msg || ''));
    const retry = o.retry === false ? '' :
      `<button class="btn-primary" ${o.retryAttr || 'data-retry="1"'}>Повторить</button>`;
    return (
      `<div class="error-card"><div class="error-icon">${icon('alert')}</div>` +
      `<div class="error-title">${escapeHtml(title)}</div>` +
      `<div class="error-body">${body}</div>${retry}</div>`
    );
  }

  // Единый формат суммы (UI-WP-05). `Math.round(n).toLocaleString('ru-RU')`
  // был скопирован в четырнадцать локальных `fmt` по app.js — и уже разъезжался:
  // где-то округляли, где-то нет, где-то валюту клеили без пробела.
  //
  // Копейки показываем, когда они ЕСТЬ. Раньше формат округлял всегда, и
  // платёж 12.50 USD на экране был «13 USD»: остаток долга не сходился с
  // квитанцией, а сумма в подтверждении отличалась от введённой. Целые суммы
  // (почти все сделки) остаются без «,00» — хвост нулей в списках только
  // шумит. Сумы в копейках (тийинах) не считают вовсе, поэтому UZS — всегда
  // целым, даже если курс пересчёта дал дробь.
  const WHOLE_CURRENCIES = { UZS: true };
  function formatMoney(n, currency) {
    const num = Number(n);
    if (!isFinite(num)) return '—';
    // Через целые копейки: 0.1 + 0.2 не должно печататься как «0,30000000004»,
    // а 1234.004 — как «1 234,00».
    const cents = Math.round(num * 100);
    const whole = cents % 100 === 0 || WHOLE_CURRENCIES[String(currency || '').toUpperCase()];
    const text = whole
      ? Math.round(cents / 100).toLocaleString('ru-RU')
      : (cents / 100).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    // Валюта — строка с сервера, а результат вставляют в innerHTML: экранируем.
    return currency ? `${text} ${escapeHtml(String(currency))}` : text;
  }

  // Склонение по числу: plural(1, ['клиент', 'клиента', 'клиентов']) → «1 клиент».
  // «1 клиентов» на главной и «отгр.» в отчёте — один и тот же класс ошибки,
  // поэтому форма выбирается в одном месте. Правило русское: 11–14 — всегда
  // родительный множественного, дальше по последней цифре. Число печатается
  // с разделением разрядов, как деньги, чтобы «12 345 позиций» читалось.
  function plural(n, forms) {
    const num = Math.abs(Math.round(Number(n) || 0));
    const f = forms || [];
    const one = f[0] || '', few = f[1] || one, many = f[2] || few;
    const mod10 = num % 10, mod100 = num % 100;
    let word = many;
    if (mod100 < 11 || mod100 > 14) {
      if (mod10 === 1) word = one;
      else if (mod10 >= 2 && mod10 <= 4) word = few;
    }
    return `${num.toLocaleString('ru-RU')} ${word}`;
  }

  // ─── Категории каталога: два уровня из строки «Запчасти/Адаптер» ────────
  // В базе категория — одна строка с «/», и на экране она была плоским
  // облаком чипов разной ширины. Дерево строится на клиенте, названия НЕ
  // меняются (владелец переименует их сам после переноса базы). Первый «/»
  // делит уровень 1 и уровень 2; всё после второго слэша остаётся в имени
  // второго уровня — глубже двух уровней экран не показывает.
  function categoryTree(categories) {
    const roots = [];
    const byName = new Map();
    for (const c of categories || []) {
      const id = c && c.id != null ? String(c.id) : '';
      const full = String((c && c.name) || id).trim();
      if (!full) continue;
      const slash = full.indexOf('/');
      const top = (slash === -1 ? full : full.slice(0, slash)).trim();
      const sub = slash === -1 ? '' : full.slice(slash + 1).trim();
      let root = byName.get(top);
      if (!root) {
        root = { key: top, name: top, ids: [], children: [] };
        byName.set(top, root);
        roots.push(root);
      }
      root.ids.push(id);
      if (sub) root.children.push({ key: id, name: sub });
      else root.key = id;   // категория без «/» — сама себе первый уровень
    }
    return roots;
  }

  // Товар попадает в уровень 1, если его категория — одна из вошедших в
  // корень строк; уровень 2 — точное совпадение.
  function categoryMatches(folderId, root, subKey) {
    if (!root) return true;
    const id = folderId == null ? '' : String(folderId);
    if (subKey) return id === String(subKey);
    return root.ids.indexOf(id) !== -1;
  }


  // Короткая подпись диапазона (UI-BUG-02). Полные даты «01.07.2026—31.07.2026»
  // — это ~150px, из-за которых ряд с сегментом гарантированно переполнялся и
  // кнопку срезал вьюпорт. Год печатаем, только если диапазон выходит за
  // текущий: внутри года он не несёт информации, а место занимает.
  //
  // Формула была продублирована в app.js дважды (заказы и аналитика) — тот
  // самый дубль, который WP-29 обещал убрать, но убрал только разметку.
  function rangeLabel(from, to, today) {
    if (!from || !to) return '';
    const year = String(from).slice(0, 4);
    const yearTo = String(to).slice(0, 4);
    const nowYear = String((today || new Date()).getFullYear());
    const short = (iso) => `${String(iso).slice(8, 10)}.${String(iso).slice(5, 7)}`;
    const sameCurrentYear = year === nowYear && yearTo === nowYear;
    return sameCurrentYear
      ? `${short(from)}—${short(to)}`
      : `${short(from)}.${year.slice(2)}—${short(to)}.${yearTo.slice(2)}`;
  }

  // Единый период-сегмент (WP-29): пресеты + «Период…» (произвольный диапазон)
  // ОДНИМ рядом. Раньше произвольный период был иконкой часов справа от группы,
  // без подписи — вне группы она читалась как «что-то ещё» и была непонятна.
  // Теперь это последний пункт того же сегмента: с подписью «Период…», а при
  // выбранном диапазоне — с самим диапазоном. attr — имя data-атрибута
  // ('data-period'|'data-operiod'). Возвращает .seg-row (seg--scroll: пять
  // пунктов на 360dp не влезают, ряд листается).
  function periodSegHtml(presets, activeId, attr, customActive, customLabel) {
    const seg = (presets || []).map((p) =>
      `<button class="seg-item ${activeId === p.id ? 'active' : ''}" ${attr}="${p.id}" ` +
      `aria-pressed="${activeId === p.id}">${escapeHtml(p.label)}</button>`
    ).join('');
    const label = customActive && customLabel ? escapeHtml(customLabel) : 'Период…';
    const custom =
      `<button class="seg-item seg-item--custom ${customActive ? 'active' : ''}" ${attr}="custom" ` +
      `aria-pressed="${customActive ? 'true' : 'false'}" aria-label="Выбрать период">` +
      `${icon('calendar')} ${label}</button>`;
    return `<div class="seg-row scroll-hint"><div class="seg seg--scroll">${seg}${custom}</div></div>`;
  }

  // ─── Склад ────────────────────────────────────────────────────────────
  // Копейки → «1 234,56 USD».
  //
  // Почему НЕ formatMoney, хотя конвенция велит форматировать деньги им: тот
  // принимает мажорные единицы и ОКРУГЛЯЕТ до целых — это верно для сводок и
  // дашбордов, но накладная обязана совпадать с печатной формой до копейки.
  // «750 USD» под документом, где напечатано 750,00, читается как другая
  // сумма, а расхождение в копейку на длинной накладной — повод для спора с
  // клиентом. Здесь вход в минорных единицах (BIGINT с бэкенда) и всегда два
  // знака. Не заменяйте на formatMoney.
  function whMoney(cents, currency) {
    const v = (Number(cents || 0) / 100).toLocaleString('ru-RU', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    const cur = currency == null ? 'USD' : currency;
    return cur ? `${v} ${escapeHtml(cur)}` : v;
  }

  // Количество без хвостовых нулей: 3, а не 3,000; дробные (кг, метры) —
  // до трёх знаков.
  function whQty(q) {
    return Number(q || 0).toLocaleString('ru-RU', { maximumFractionDigits: 3 });
  }

  // Бейдж остатка. Количества дробные (склад считает не только штуки), поэтому
  // подпись строит whQty, а не toString.
  function whStockBadge(q) {
    const n = Number(q || 0);
    // Состояние — атрибутом, цвет выводится из переменных (см. [data-status]
    // в style.css). Классы badge-* — прежняя система, её разметку не плодим.
    const state = n <= 0 ? 'out' : (n < 20 ? 'low' : 'in_stock');
    return `<span class="stock-badge" data-status="${state}">${n <= 0 ? 'нет' : whQty(n)}</span>`;
  }

  // ─── Техника ──────────────────────────────────────────────────────────────
  // Словарь подписей статусов НЕ дублируем на фронте: он приходит с сервера
  // (`status_labels`), где живёт вместе с самим жизненным циклом машины. Иначе
  // добавленный статус пришлось бы вписывать в двух языках и в одном забыть.
  function machineStatusLabel(status, labels) {
    const key = String(status || '');
    return (labels && labels[key]) || key || '—';
  }

  // Подстрочник строки списка: «JCB7788 · 15 200 м/ч · 25 000 USD».
  // Пустые части выпадают целиком — «—» вместо цены выглядит как «цена ноль»,
  // хотя на самом деле её просто ещё не заводили.
  function machineSubtitle(m) {
    const parts = [];
    if (m && m.vin) parts.push(String(m.vin));
    if (m && m.hours != null && m.hours !== '') {
      parts.push(`${Number(m.hours).toLocaleString('ru-RU')} м/ч`);
    }
    if (m && m.price_cents) {
      parts.push(formatMoney(Number(m.price_cents) / 100, m.currency || 'USD'));
    }
    return escapeHtml(parts.join(' · '));
  }

  // Фильтр по статусу. Показываем только непустые статусы: пункт «Забронированы
  // 0» ничего не отбирает, а место в ряду занимает. `seg--scroll` — потому что
  // статусов шесть и на 360dp они не помещаются.
  function machineStatusSegHtml(counts, active, labels) {
    const c = counts || {};
    const order = ['in_transit', 'in_stock', 'reserved', 'sold', 'on_credit', 'archived'];
    const pill = (id, label, n) =>
      `<button class="seg-item ${active === id ? 'active' : ''}" data-mstatus="${escapeHtml(id)}" ` +
      `aria-pressed="${active === id}">${escapeHtml(label)} ${n}</button>`;
    const pills = order
      .filter((s) => Number(c[s] || 0) > 0)
      .map((s) => pill(s, machineStatusLabel(s, labels), Number(c[s])))
      .join('');
    return `<div class="seg-row scroll-hint"><div class="seg seg--scroll">` +
      `${pill('all', 'Все', Number(c.all || 0))}${pills}</div></div>`;
  }

  // ─── Деньги: дебиторка ────────────────────────────────────────────────────
  // Сумма приходит блоком {by_currency, base_total, partial}. Правило одно на
  // весь фронт: `partial` значит «часть сумм без курса в итог не вошла», и
  // молчать об этом нельзя — по этой цифре принимают решения.
  function moneyBlockLabel(block) {
    if (!block || !block.count) return '—';
    const cur = block.base_currency || 'USD';
    const rows = block.by_currency || [];
    // Одна валюта — показываем её как есть, без псевдоточного «≈».
    if (rows.length === 1) return formatMoney(rows[0].total, rows[0].currency);
    if (block.base_total == null) {
      return rows.map((r) => formatMoney(r.total, r.currency)).join(' · ');
    }
    return `≈ ${formatMoney(block.base_total, cur)}${block.partial ? ' (часть без курса)' : ''}`;
  }

  // Горизонтальные бары по корзинам просрочки. Ширина — доля от самой большой
  // корзины, а не от суммы: сравнивать надо корзины между собой.
  function agingBarsHtml(aging) {
    const buckets = (aging && aging.buckets) || [];
    const values = buckets.map((b) => (b.base_total == null ? 0 : b.base_total));
    const max = Math.max(...values, 0);
    if (!max) return emptyState({ icon: 'check', title: 'Долгов нет', hint: 'Все деньги собраны.' });
    return `<div class="c-surface c-surface--pad">${buckets.map((b, i) => {
      // Пустую корзину рисуем строкой без бара: нулевая полоска выглядит как
      // подтёкший рендер, а исчезнувшая строка — как «не посчитали».
      const pct = max ? Math.round((values[i] / max) * 100) : 0;
      const state = b.key === 'not_due' ? 'upcoming' : 'overdue';
      return `
        <div class="aging-row" data-status="${escapeHtml(b.key)}">
          <div class="aging-head">
            <span class="aging-label">${escapeHtml(b.label)}</span>
            <span class="aging-sum">${escapeHtml(moneyBlockLabel(b))}</span>
          </div>
          <div class="aging-track"><div class="aging-bar" data-status="${state}" style="width:${pct}%"></div></div>
          <div class="aging-count">${plural(b.count, ['документ', 'документа', 'документов'])}</div>
        </div>`;
    }).join('')}</div>`;
  }

  // Прогноз поступлений помесячно. Месяц без ожидаемых денег не выбрасываем:
  // «в ноябре ничего не ждём» — это тоже ответ.
  function forecastRowsHtml(months) {
    const rows = months || [];
    if (!rows.length) return emptyState({ icon: 'calendar', title: 'Нечего прогнозировать' });
    const max = Math.max(...rows.map((m) => m.base_total || 0), 0);
    const MONTHS_RU = ['янв', 'фев', 'мар', 'апр', 'май', 'июн',
      'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
    return `<div class="c-surface c-surface--pad">${rows.map((m) => {
      const [y, mo] = String(m.month || '').split('-');
      const label = `${MONTHS_RU[Number(mo) - 1] || m.month} ${String(y).slice(2)}`;
      const pct = max ? Math.round(((m.base_total || 0) / max) * 100) : 0;
      const share = m.machines && m.machines.count
        ? ` · техника ${escapeHtml(moneyBlockLabel(m.machines))}` : '';
      return `
        <div class="aging-row">
          <div class="aging-head">
            <span class="aging-label">${escapeHtml(label)}</span>
            <span class="aging-sum">${escapeHtml(moneyBlockLabel(m))}</span>
          </div>
          <div class="aging-track"><div class="aging-bar" data-status="approved" style="width:${pct}%"></div></div>
          <div class="aging-count">${plural(m.count, ['платёж', 'платежа', 'платежей'])}${share}</div>
        </div>`;
    }).join('')}</div>`;
  }

  // Ключ покупателя техники: настоящего идентификатора у него нет (в сделке
  // имя и паспорт), поэтому «Иванов  П.» и «иванов п.» обязаны схлопнуться —
  // иначе один человек выглядит как двое должников.
  function buyerKey(name) {
    return String(name == null ? '' : name).trim().replace(/\s+/g, ' ').toLowerCase();
  }

  // ─── Воронка обращений ────────────────────────────────────────────────────
  // Ступени рисуем шириной от ПЕРВОЙ ступени, а не от максимума: воронка по
  // определению сужается, и «обратились» — это её 100%.
  function leadFunnelHtml(f) {
    if (!f || !f.contacted) {
      return emptyState({ icon: 'user', title: 'Обращений пока нет' });
    }
    const steps = [
      { label: 'Обратились', value: f.contacted },
      { label: 'Ответили', value: f.replied },
      { label: 'Купили', value: f.won },
    ];
    const base = f.contacted || 1;
    const rows = steps.map((s) => {
      const pct = Math.round((s.value / base) * 100);
      return `
        <div class="aging-row">
          <div class="aging-head">
            <span class="aging-label">${escapeHtml(s.label)}</span>
            <span class="aging-sum">${s.value} · ${pct}%</span>
          </div>
          <div class="aging-track"><div class="aging-bar" data-status="approved" style="width:${pct}%"></div></div>
        </div>`;
    }).join('');
    // Хвосты воронки — то, что требует действия или объясняет потери.
    const tail = [
      f.awaiting_reply ? `ждут ответа: ${f.awaiting_reply}` : '',
      f.never_answered ? `без ответа вовсе: ${f.never_answered}` : '',
      f.silent ? `замолчали: ${f.silent}` : '',
      f.lost ? `не купили: ${f.lost}` : '',
      f.reengaged ? `вернулись: ${f.reengaged}${
        f.reengaged_won ? ` (из них купили ${f.reengaged_won})` : ''}` : '',
    ].filter(Boolean).join(' · ');
    return `<div class="c-surface c-surface--pad">${rows}` +
      (tail ? `<div class="aging-count">${escapeHtml(tail)}</div>` : '') + '</div>';
  }

  // Длительность словами. Минуты до часа, дальше часы, дальше дни: «за 40 мин»
  // читается, «за 0,67 ч» — нет.
  function durationLabel(minutes) {
    if (minutes == null) return '—';
    const m = Math.round(Number(minutes));
    if (!isFinite(m) || m < 0) return '—';
    if (m < 60) return `${m} мин`;
    const h = m / 60;
    if (h < 24) return `${h < 10 ? h.toFixed(1).replace('.0', '') : Math.round(h)} ч`;
    const d = h / 24;
    return `${d < 10 ? d.toFixed(1).replace('.0', '') : Math.round(d)} дн`;
  }

  // Две ступени вместо одной: клиент написал сам / написали мы первыми.
  // Смешивать их нельзя — у первого интерес уже есть, второго ещё надо
  // заинтересовать, и одной конверсией эти две работы не описать.
  function firstTouchHtml(f) {
    const d = (f && f.by_direction) || {};
    const halves = [
      { key: 'inbound', label: 'Клиент написал сам', hint: 'пришёл сам' },
      { key: 'outbound', label: 'Написали мы первыми', hint: 'после звонка или по своей инициативе' },
    ];
    const rows = halves.map((h) => {
      const b = d[h.key] || {};
      const n = Number(b.contacted) || 0;
      if (!n) return '';
      const win = b.win_rate == null ? '—' : `${Math.round(b.win_rate * 100)}%`;
      return `
        <div class="c-row">
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(h.label)}</div>
            <div class="card-row-sub">${n} — купили ${Number(b.won) || 0} · ${escapeHtml(h.hint)}</div>
          </div>
          <div class="card-row-value">${win}</div>
        </div>`;
    }).filter(Boolean).join('');
    if (!rows) return '';
    return '<div class="c-surface c-surface--list">' + rows + '</div>';
  }

  // Скорость ответа: типичный случай и хвост. Среднее не показываем — один
  // забытый на три дня клиент делает его бессмысленным.
  function replySpeedHtml(speed) {
    const s = speed || {};
    if (!s.answered) return '';
    return `<div class="c-surface c-surface--list">
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-sub">Обычно отвечаем за</div></div>
        <div class="card-row-value">${escapeHtml(durationLabel(s.median_minutes))}</div>
      </div>
      <div class="c-row">
        <div class="card-row-info">
          <div class="card-row-sub">Каждый десятый ждёт дольше</div>
        </div>
        <div class="card-row-value">${escapeHtml(durationLabel(s.p90_minutes))}</div>
      </div>
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-sub">Посчитано по ответам</div></div>
        <div class="card-row-value">${Number(s.answered) || 0}</div>
      </div>
    </div>`;
  }

  // Отклик на пост в канале. Формулировка «после поста» — не «из поста»:
  // ссылка ведёт прямо в личку менеджера и метки не несёт, поэтому кто именно
  // пришёл с публикации, мы не знаем и делать вид не будем.
  function postEffectLabel(effect) {
    if (!effect) return '';
    const after = Number(effect.after) || 0;
    const base = Number(effect.baseline) || 0;
    const window = Number(effect.window_hours) || 24;
    if (!after && !base) return '';
    const baseText = String(base).replace('.', ',');
    return `за ${window} ч после поста — ${plural(after, ['обращение', 'обращения', 'обращений'])} · обычно ${baseText}/день`;
  }

  // ─── «Как получены деньги» (payments.js, services/order_payments.py) ───
  const PAY_METHODS = [['cash', 'Наличные'], ['card', 'Карта'], ['bank', 'На счёт']];
  const PAY_METHOD_LABEL = { cash: 'наличные', card: 'на карту', bank: 'перечислением' };

  // ─── Чистые хелперы (тесты — __tests__/payments.test.js) ───────────────────

  // Ввод суммы → копейки, как services.money.parse_amount: «1 500,50» → 150050.
  function payCents(raw) {
    const t = String(raw == null ? '' : raw).replace(/[\s  ]/g, '').replace(',', '.');
    if (!/^\d+(\.\d+)?$/.test(t)) return null;
    const cents = Math.round(parseFloat(t) * 100);
    return isFinite(cents) && cents > 0 ? cents : null;
  }

  function payRate(raw) {
    const t = String(raw == null ? '' : raw).replace(/[\s  ]/g, '').replace(',', '.');
    if (!/^\d+(\.\d+)?$/.test(t)) return null;
    const v = parseFloat(t);
    return v > 0 && isFinite(v) ? v : null;
  }

  // Чей курс нужен строке: небазовая валюта пары; null — пересчёта нет.
  function payRateCurrency(partCur, orderCur, base) {
    if (!partCur || partCur === orderCur) return null;
    return partCur !== base ? partCur : orderCur;
  }

  // Сумма строки в валюте заказа — та же формула, что convert_to_order на сервере.
  function payConvert(cents, partCur, orderCur, base, quote) {
    if (partCur === orderCur) return cents;
    if (!(quote > 0)) return null;
    return orderCur === base ? Math.round(cents / quote) : Math.round(cents * quote);
  }

  function payMoney(cents, currency) {
    return formatMoney((Number(cents) || 0) / 100, currency);
  }

  // Итог формы: rows [{method, currency, amount, rate}], ctx {currency, base_currency,
  // due_cents, exact, cbu}. → {total, left, over, short, missingRate, valid, tolerance}
  function payPreview(rows, ctx) {
    const base = ctx.base_currency;
    let total = 0;
    let any = false;
    let converted = false;
    let missingRate = false;
    let maxQuote = 0;
    for (const r of rows || []) {
      const cents = payCents(r.amount);
      if (!cents || !r.currency) continue;
      any = true;
      const rc = payRateCurrency(r.currency, ctx.currency, base);
      let quote = null;
      if (rc) {
        converted = true;
        quote = payRate(r.rate != null && r.rate !== '' ? r.rate : (ctx.cbu || {})[rc]);
        if (!quote) { missingRate = true; continue; }
        if (rc === ctx.currency) maxQuote = Math.max(maxQuote, quote);
      }
      total += payConvert(cents, r.currency, ctx.currency, base, quote);
    }
    // Допуск на округление пересчёта — как tolerance_cents на сервере.
    const tolerance = !converted ? 0 : (ctx.currency === base ? 100 : Math.max(100, Math.round(100 * maxQuote)));
    const due = Number(ctx.due_cents) || 0;
    const over = total > due + tolerance;
    const short = !!ctx.exact && total < due - tolerance;
    return {
      any, total, missingRate, over, short, tolerance,
      left: Math.max(0, due - total),
      valid: any && !missingRate && total > 0 && !over && !short && due > 0,
    };
  }

  // Подпись состояния строки разбивки для карточек.
  const PAY_STATE_LABEL = {
    on_hand: 'у менеджера, ждут сдачи в кассу',
    in_deposit: 'сданы в кассу, ждут подтверждения',
    awaiting_bank: 'ждёт проверки банка',
    confirmed: 'подтверждено',
    rejected: 'отклонено',
  };

  // «наличные 5 000 USD — …» / «на карту •••• 1234 (Фаридун М.) · 7 130 USD — …».
  // Подпись «куда» считает сервер (account_label) — та же, что в пуше и дайджесте.
  function payPartLine(p) {
    const money = payMoney(p.amount_cents, p.currency);
    const what = p.account_label ? `${p.account_label} · ${money}` : `${PAY_METHOD_LABEL[p.method] || p.method} ${money}`;
    return `${what} — ${PAY_STATE_LABEL[p.state] || p.state}`;
  }

  // Сколько по заказу подтверждает кнопка «Подтвердить» на экране «Деньги →
  // Подтвердить»: только безнал. Наличные закрывает сдача в кассу, и сервер их
  // из этой суммы уже вычел (`confirmable`). Фолбэк на `pending` — для старых
  // ответов без поля; тогда нуля не бывает и кнопка ведёт себя как раньше.
  function payConfirmable(d) {
    const n = Number((d || {}).confirmable != null ? d.confirmable : (d || {}).pending);
    return Number.isFinite(n) ? n : 0;
  }

  // ─── Куда поступили: карты и счета (services/pay_accounts.py) ────────────
  const PAY_ACCOUNT_KIND = {
    card: { title: 'На какую карту', add: 'Новая карта', empty: 'Карт пока нет — добавьте новую',
      choose: 'Куда поступили — выберите карту', hint: 'Последние 4 цифры и владелец карты', icon: 'card' },
    bank: { title: 'На какой счёт', add: 'Новый счёт', empty: 'Счетов пока нет — добавьте новый',
      choose: 'Куда поступили — выберите счёт', hint: 'Фирма или владелец и номер счёта', icon: 'building' },
  };

  function payNorm(text) {
    return String(text == null ? '' : text).toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();
  }

  // Строки пикера: только нужного вида и не в архиве; счета в валюте строки —
  // первыми (клиент чаще платит в валюту самой карты). Поиск — по владельцу,
  // фирме, банку, хвосту карты и номеру счёта.
  function payAccountItems(accounts, kind, currency) {
    const list = (accounts || []).filter(a => a.kind === kind && !a.archived);
    const rank = a => (currency && a.currency === currency ? 0 : 1);
    return list
      .map((a, i) => ({ a, i }))
      .sort((x, y) => rank(x.a) - rank(y.a) || x.i - y.i)
      .map(({ a }) => ({
        id: a.id,
        name: a.title,
        sub: a.sub,
        search: payNorm([a.title, a.holder, a.bank, a.card_last4, a.account_number, a.company_tin].join(' ')),
        account: a,
      }));
  }

  // Что предложить по умолчанию: последний выбор человека, если запись жива.
  function payDefaultAccountId(state, kind) {
    const id = state && state.last_used ? state.last_used[kind] : null;
    if (!id) return null;
    const a = (state.accounts || []).find(x => Number(x.id) === Number(id));
    return a && !a.archived && a.kind === kind ? a.id : null;
  }

  // Набранное в поиске уезжает в форму: 4 цифры — хвост карты, 20 — номер
  // счёта, остальное — владелец/фирма.
  function payAccountPrefill(kind, typed) {
    const t = String(typed || '').trim();
    const digits = t.replace(/\D/g, '');
    if (!t) return {};
    if (kind === 'card' && /^[\d\s•*]+$/.test(t) && digits.length === 4) return { card_last4: digits };
    if (kind === 'bank' && /^[\d\s-]+$/.test(t) && digits.length === 20) return { account_number: digits };
    if (/^[\d\s•*-]+$/.test(t)) return {};
    return { holder: t };
  }

  // Та же проверка, что на сервере (pay_accounts.validate), — ошибка в форме
  // сразу, без круга до сервера. '' — всё верно.
  function payAccountFormError(kind, data) {
    const d = data || {};
    const holder = String(d.holder || '').trim();
    if (kind === 'card') {
      if (!holder) return 'Укажите владельца карты — например, «Фаридун М.»';
      const raw = String(d.card_last4 || '').trim();
      const digits = raw.replace(/\D/g, '');
      if (digits.length > 4) return 'Нужны только последние 4 цифры карты — полный номер карты не храним';
      if (digits.length !== 4 || !/^[\d\s•*.·-]*$/.test(raw)) return 'Последние цифры карты — ровно 4 цифры';
      return '';
    }
    if (!holder) return 'Укажите фирму или владельца счёта — например, «ООО Farid Impeks»';
    const raw = String(d.account_number || '').trim();
    if (raw.replace(/\D/g, '').length !== 20 || /[^\d\s-]/.test(raw)) return 'Номер расчётного счёта — 20 цифр';
    const tin = String(d.company_tin || '').replace(/\D/g, '');
    if (String(d.company_tin || '').trim() && tin.length !== 9 && tin.length !== 14) return 'ИНН — 9 цифр (ПИНФЛ — 14)';
    const mfo = String(d.mfo || '').replace(/\D/g, '');
    if (String(d.mfo || '').trim() && mfo.length !== 5) return 'МФО банка — 5 цифр';
    return '';
  }

  // Строка «Куда поступили» в строке оплаты: выбранная карта/счёт или приглашение.
  function payAccountFieldHtml(account, kind, rowNo) {
    const meta = PAY_ACCOUNT_KIND[kind] || PAY_ACCOUNT_KIND.card;
    const id = account ? String(account.id) : '';
    return `
      <div class="c-surface c-surface--list pay-account-box">
        <div class="c-row c-row--tap pay-part-account${account ? '' : ' pay-account--empty'}" role="button" tabindex="0"
             data-account-id="${escapeHtml(id)}" data-kind="${escapeHtml(kind)}"
             aria-label="Куда поступили${rowNo ? ', строка ' + Number(rowNo) : ''}">
          <div class="card-row-icon">${icon(meta.icon)}</div>
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(account ? account.title : meta.choose)}</div>
            <div class="card-row-sub">${escapeHtml(account ? (account.sub || account.label || '') : meta.hint)}</div>
          </div>
        </div>
      </div>`;
  }

  // Первая строка карты/перечисления с суммой, но без «куда»; -1 — все указаны.
  function payMissingAccount(rows) {
    return (rows || []).findIndex(r => (r.method === 'card' || r.method === 'bank') && payCents(r.amount) && !r.account_id);
  }

  // «Карты и счета»: список по видам, архив — по переключателю. canManage —
  // правка и архив (руководство; менеджер — пока руководителя нет).
  function payAccountsManagerHtml(accounts, opts) {
    const o = opts || {};
    const all = accounts || [];
    const group = (kind, title) => {
      const rows = all.filter(a => a.kind === kind && (o.showArchived || !a.archived));
      const body = rows.length ? rows.map(a => `
          <div class="c-row${o.canManage ? ' c-row--tap' : ''}" data-pay-account="${Number(a.id)}"${o.canManage ? ' role="button" tabindex="0"' : ''}${a.archived ? ' data-status="rejected"' : ''}>
            <div class="card-row-icon">${icon(kind === 'card' ? 'card' : 'building')}</div>
            <div class="card-row-info">
              <div class="card-row-title">${escapeHtml(a.title)}</div>
              <div class="card-row-sub">${escapeHtml([a.sub, a.archived ? 'в архиве' : ''].filter(Boolean).join(' · ') || a.label || '')}</div>
            </div>
          </div>`).join('')
        : `<div class="c-row"><div class="card-row-info"><div class="card-row-sub">${escapeHtml(PAY_ACCOUNT_KIND[kind].empty)}</div></div></div>`;
      return `<div class="section-label">${escapeHtml(title)}</div><div class="c-surface c-surface--list">${body}</div>`;
    };
    const archived = all.filter(a => a.archived).length;
    return `
      ${group('card', 'Карты')}
      ${group('bank', 'Расчётные счета')}
      ${o.canAdd !== false ? `<div class="c-actions">
        <button type="button" class="btn-secondary" data-pay-account-add="card">${icon('plus')} Новая карта</button>
        <button type="button" class="btn-secondary" data-pay-account-add="bank">${icon('plus')} Новый счёт</button>
      </div>` : ''}
      ${o.canManage && archived ? `<button type="button" class="btn-secondary" data-pay-accounts-archived="${o.showArchived ? '0' : '1'}">${o.showArchived ? 'Скрыть архив' : `Показать архив · ${archived}`}</button>` : ''}
      ${o.hint ? `<div class="c-field-hint">${escapeHtml(o.hint)}</div>` : ''}`;
  }

  // Карточка долга «ждёт подтверждения»: одна фраза без двусмысленности.
  // Было «Ждёт: 12 130 USD · Останется: 0 USD» — читалось как «ждём 12к, а
  // осталось 0?». Смысл: оплата 12 130 ждёт подтверждения, ПОСЛЕ него долг 0.
  function payAwaitingText(d) {
    const cur = d.currency || '';
    const after = Number(d.remaining_after_pending != null
      ? d.remaining_after_pending : Math.max(0, (d.remaining || 0) - (d.pending || 0)));
    let text = `Оплата ${formatMoney(d.pending, cur)} ждёт подтверждения · после подтверждения долг: ${formatMoney(after, cur)}`;
    if (Number(d.overpending) > 0) {
      text += ` (из них ${formatMoney(d.overpending, cur)} сверх долга — был возврат)`;
    }
    return text;
  }

  // Форма «Сдать наличные»: валюта (с суммой на руках), список наличных по
  // заказам с отметками и сумма. Разметка — здесь, проводка — renderCashbox.
  function payHandoverHtml(onHand, baseCurrency) {
    const byCur = {};
    (onHand.by_currency || []).forEach(x => { byCur[x.currency] = x.amount_cents; });
    const currencies = onHand.currencies || [baseCurrency];
    const cur = (onHand.by_currency || []).length ? onHand.by_currency[0].currency : baseCurrency;
    return `
      <div class="section-label">Сдать наличные</div>
      <div class="card pay-handover" data-cur="${escapeHtml(cur)}">
        <div class="form-row">
          <span class="form-label">Валюта</span>
          <div class="seg">${currencies.map(c =>
            `<button type="button" class="seg-item ${c === cur ? 'active' : ''}" data-dep-cur="${escapeHtml(c)}" aria-pressed="${c === cur}">${escapeHtml(c)}${byCur[c] ? ' · ' + escapeHtml(payMoney(byCur[c], c)) : ''}</button>`
          ).join('')}</div>
        </div>
        <div class="dep-onhand"></div>
        <div class="form-row">
          <label class="form-label" for="dep-amount">Сумма (<span class="dep-cur-label">${escapeHtml(cur)}</span>)</label>
          <input type="text" id="dep-amount" class="form-input" placeholder="500" inputmode="decimal" autocomplete="off">
        </div>
        <button id="dep-create" class="btn-primary">${icon('cash')} Сдать в кассу</button>
        <div class="debt-hint dep-hint">Закроет наличные по отмеченным заказам — старые первыми.</div>
      </div>`;
  }

  // Наличные на руках по заказам в валюте `cur` — строки с отметкой (все отмечены).
  function payHandoverOrdersHtml(onHand, cur) {
    const orders = (onHand.orders || []).filter(o => o.currency === cur);
    if (!orders.length) {
      return `<div class="debt-hint">Наличных по заказам в ${escapeHtml(cur)} на руках нет${cur === onHand.base_currency ? ' — сдача уйдёт в счёт старых долгов без разбивки, если они есть' : ''}.</div>`;
    }
    return `
      <div class="form-label">На руках по заказам</div>
      <div class="c-surface c-surface--list">${orders.map(o => `
        <div class="c-row c-row--tap dep-order" role="checkbox" aria-checked="true" tabindex="0" data-order="${Number(o.order_id)}" data-cents="${Number(o.amount_cents)}" data-status="approved">
          <div class="card-row-icon">${icon('check')}</div>
          <div class="card-row-info">
            <div class="card-row-title">Заказ #${Number(o.order_id)}${o.agent_name ? ' · ' + escapeHtml(o.agent_name) : ''}</div>
            <div class="card-row-sub">с ${escapeHtml(o.since || '')}</div>
          </div>
          <div class="card-row-value">${escapeHtml(payMoney(o.amount_cents, o.currency))}</div>
        </div>`).join('')}
      </div>`;
  }

  // Какие заказы отмечены. Отмечены все — null: ручного выбора нет, FIFO сервера.
  function payHandoverPicked(box) {
    const all = Array.from(box.querySelectorAll('.dep-order'));
    const picked = all.filter(el => el.getAttribute('aria-checked') === 'true').map(el => Number(el.dataset.order));
    return picked.length && picked.length < all.length ? picked : null;
  }

  // Что закрывает сдача — для карточек: «#27 — 5 000 USD, #30 — 1 000 USD».
  function payDepositOrdersText(orders) {
    const list = (orders || []).map(o => `#${o.order_id} — ${formatMoney(o.amount_allocated != null ? o.amount_allocated : o.amount, o.currency)}`);
    return list.length ? list.join(', ') : '—';
  }

  // ─── Скидка к прайсу (services/order_discounts.py) ────────────────────────
  // Проценты и прайс считает СЕРВЕР (он знает валюту прайса и порог), фронт
  // только подписывает: вторая формула скидки разошлась бы с карточкой бота.

  // «скидка 4%» / «выше прайса на 4%» / «—» — как в карточке сделки по технике.
  function discountPctLabel(pct) {
    if (pct === null || pct === undefined || pct === '') return '—';
    const n = Number(pct);
    if (!isFinite(n)) return '—';
    if (n > 0) return `скидка ${n}%`;
    if (n < 0) return `выше прайса на ${Math.abs(n)}%`;
    return 'по прайсу';
  }

  // Хвост строки позиции: « · прайс 125 USD · скидка 20%». '' — прайса нет
  // (новый товар, цену которому не задавали): «скидка 0%» на нём соврала бы.
  function discountLineSuffix(item, currency) {
    if (!item || item.ref_price == null || item.discount_pct == null) return '';
    return ` · прайс ${formatMoney(item.ref_price, currency || '')} · ${discountPctLabel(item.discount_pct)}`;
  }

  // Сводка по заказу для карточки решения. '' — сравнивать не с чем.
  function discountSummaryText(d) {
    if (!d || !d.covered_lines) return '';
    let text = `Скидка по заказу: ${discountPctLabel(d.avg_pct)}`;
    if (d.max_pct != null && d.avg_pct != null && Math.abs(Number(d.max_pct) - Number(d.avg_pct)) >= 0.1) {
      text += ` · максимум по позиции ${Number(d.max_pct)}%`;
    }
    const missing = Number(d.total_lines || 0) - Number(d.covered_lines || 0);
    if (missing > 0) text += ` · без прайса: ${missing}`;
    return text;
  }

  // Блок скидки на карточке заявки. Помеченная скидка красится тем же
  // `credit-ctx--bad`, что и превышение лимита: состояние одно — «нужно
  // решение руководителя», и второго языка предупреждений заводить незачем.
  function discountBlockHtml(d) {
    const text = discountSummaryText(d);
    if (!text) return '';
    const flagged = !!(d && d.flagged);
    const tail = flagged
      ? ` ${icon('alert')} порог ${Number(d.threshold_pct)}% — нужно явное решение`
      : '';
    return `<div class="credit-ctx ${flagged ? 'credit-ctx--bad' : 'credit-ctx--ok'}">`
      + `${icon('chart')} ${escapeHtml(text)}${tail}</div>`;
  }

  // Что видит МЕНЕДЖЕР по своей заявке: почему она ждёт руководителя.
  function discountPendingNote(d) {
    if (!d || !d.flagged) return '';
    return `Ждёт одобрения из-за скидки ${Number(d.max_pct)}% (порог ${Number(d.threshold_pct)}%) — решение принимает руководитель.`;
  }

  // ─── Сверка кассы (services/cash_reconciliation.py) ──────────────────────
  //
  // Пересчёт наличных руками против того, что система считает «на руках».
  // Здесь только чистые функции — разметка и проводка в cash_reconcile.js.

  // Ввод формы {валюта: строка} + ожидаемое [{currency, amount_cents}] →
  // строки сверки. Валюта без введённой суммы в пересчёт НЕ попадает: «не
  // считал сумы» и «сумов ноль» — разные утверждения, второе вводят явно.
  // Зеркало `cash_reconciliation.build_lines`; сервер считает заново и
  // остаётся единственным источником истины — здесь это только предпросмотр.
  function reconLines(input, system) {
    const sys = {};
    (system || []).forEach(s => { sys[String(s.currency).toUpperCase()] = Number(s.amount_cents) || 0; });
    const out = [];
    Object.keys(input || {}).sort().forEach(rawCur => {
      const cur = String(rawCur).toUpperCase();
      const raw = String(input[rawCur] == null ? '' : input[rawCur]).trim();
      if (raw === '') return;
      // Свой разбор, а не payCents: тот отдаёт null и на «0», и на мусоре, а
      // пересчитанный НОЛЬ — законный результат («в кассе пусто»), и путать
      // его с опечаткой «12о» нельзя.
      const t = raw.replace(/[\s  ]/g, '').replace(',', '.');
      const valid = /^\d+(\.\d{1,2})?$/.test(t);
      const counted = valid ? Math.round(parseFloat(t) * 100) : null;
      const expected = sys[cur] || 0;
      out.push({
        currency: cur,
        counted_cents: counted,
        system_cents: expected,
        diff_cents: counted == null ? null : counted - expected,
        invalid: !valid,
      });
    });
    return out;
  }

  // Итог предпросмотра: можно ли отправлять и что сказать про расхождение.
  function reconPreview(lines) {
    const rows = lines || [];
    const bad = rows.some(r => r.invalid);
    const filled = rows.filter(r => !r.invalid && r.counted_cents != null);
    const mismatched = filled.filter(r => r.diff_cents !== 0);
    return {
      valid: !bad && filled.length > 0,
      empty: filled.length === 0,
      invalid: bad,
      matched: filled.length > 0 && mismatched.length === 0,
      mismatched,
    };
  }

  // Подпись расхождения. Ноль — это результат, а не пустота: так и пишем.
  function reconDiffLabel(diff, cur) {
    const n = Number(diff) || 0;
    if (n === 0) return 'сходится';
    return (n > 0 ? 'излишек ' : 'недостача ') + payMoney(Math.abs(n), cur);
  }

  // Класс строки: сошлось — зелёная, разошлось — тревожная.
  function reconDiffClass(diff) {
    return (Number(diff) || 0) === 0 ? 'recon-ok' : 'recon-warn';
  }

  // Строки истории (сервер отдаёт по строке на валюту) → карточки пересчётов:
  // строки одного пересчёта склеены ключом, как их и писали.
  function reconGroupHistory(items) {
    const groups = [];
    const byKey = {};
    (items || []).forEach(r => {
      const key = r.request_key ? 'k:' + r.request_key
        : 'u:' + r.counted_by + '|' + (r.created_at || r.count_date || '');
      let g = byKey[key];
      if (!g) {
        g = {
          key, date: r.count_date, created_at: r.created_at,
          who: r.counted_by_name || '', by: r.counted_by, note: r.note || '', lines: [],
        };
        byKey[key] = g;
        groups.push(g);
      }
      if (!g.note && r.note) g.note = r.note;
      g.lines.push(r);
    });
    groups.forEach(g => {
      g.lines.sort((a, b) => String(a.currency).localeCompare(String(b.currency)));
      g.matched = g.lines.every(l => Number(l.diff_cents) === 0);
    });
    return groups;
  }

  function reconHistoryHtml(items, opts) {
    const o = opts || {};
    const groups = reconGroupHistory(items);
    if (!groups.length) {
      return `<div class="debt-hint">${escapeHtml(o.emptyText || 'Пересчётов пока нет.')}</div>`;
    }
    return `<div class="c-surface c-surface--list">${groups.map(g => `
      <div class="c-row recon-row ${g.matched ? 'recon-ok' : 'recon-warn'}">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(formatDateRU(g.date || ''))}${o.showWho && g.who ? ' · ' + escapeHtml(g.who) : ''}</div>
          <div class="card-row-sub">${g.lines.map(l =>
            `${escapeHtml(String(l.currency))}: ${escapeHtml(payMoney(l.counted_cents, l.currency))} · ${escapeHtml(reconDiffLabel(l.diff_cents, l.currency))}`
          ).join(' · ')}</div>
          ${g.note ? `<div class="card-row-sub recon-note">${escapeHtml(g.note)}</div>` : ''}
        </div>
      </div>`).join('')}</div>`;
  }

  // ─── Подсказки цены в форме позиции заказа (B7/D4) ────────────────────────
  // Вход — ответ `/api/orders/price_hint` ({last, default, wholesale}) и
  // валюта, выбранная в форме. Выход — подсказки в порядке приоритета;
  // первая подходящая по валюте становится префиллом.
  //
  // Порядок: «прошлый раз этому клиенту» → «цена товара» → пусто.
  // «Для постоянных» в префилле НЕ участвует — это альтернатива в один тап:
  // правил, кто постоянный, в проекте нет, и выдумывать их в v1 незачем.
  //
  // Валюта важнее подсказки: цена в UZS, подставленная в долларовый заказ,
  // это не подсказка, а ошибка на два порядка. Подсказка с чужой валютой
  // остаётся ВИДНОЙ (знать полезно), но не префиллит и не ставится тапом.
  const PRICE_HINT_LABELS = {
    last: 'Прошлый раз',
    default: 'Цена',
    wholesale: 'Постоянным',
  };

  function priceSuggestions(hint, currency) {
    const cur = String(currency || '').toUpperCase();
    const out = [];
    for (const source of ['last', 'default', 'wholesale']) {
      const s = hint && hint[source];
      if (!s || s.price == null || !(Number(s.price) > 0)) continue;
      const own = String(s.currency || '').toUpperCase();
      // Цена без валюты трактуется как «в валюте формы»: в `product_prices`
      // валюта появилась позже самих цен, у старых строк её нет.
      const matches = !own || !cur || own === cur;
      out.push({
        source,
        price: Number(s.price),
        currency: own || cur || '',
        date: s.date || '',
        matches,
        label: PRICE_HINT_LABELS[source],
      });
    }
    return out;
  }

  // Что подставить в поле цены. Нечего — null, и поле остаётся пустым,
  // ровно как до B7.
  function pricePrefill(hint, currency) {
    const s = priceSuggestions(hint, currency)
      .find(x => x.matches && x.source !== 'wholesale');
    return s ? s.price : null;
  }

  // Подпись подсказки: «Прошлый раз: 45 USD (12.09)».
  function priceHintText(s) {
    if (!s) return '';
    const num = Number(s.price).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
    const cur = s.currency ? ` ${s.currency}` : '';
    const date = s.date ? ` (${s.date})` : '';
    return `${s.label}: ${num}${cur}${date}`;
  }

  // Ряд подсказок под полем цены. Подходящие по валюте — кнопки (тап
  // подставляет), чужая валюта — просто текст.
  function priceHintHtml(hint, currency) {
    const list = priceSuggestions(hint, currency);
    if (!list.length) return '';
    return `<div class="price-hints">${list.map(s => (s.matches
      ? `<button type="button" class="price-hint" data-price-hint="${s.source}"
           data-price="${s.price}">${escapeHtml(priceHintText(s))}</button>`
      : `<span class="price-hint price-hint--other">${escapeHtml(priceHintText(s))}</span>`
    )).join('')}</div>`;
  }

  return {
    priceSuggestions, pricePrefill, priceHintText, priceHintHtml,
    escapeHtml, idemKey, formatDateRU, icon, opsAmount, plural,
    ROLE_ALSO_ACTS_AS, roleIn,
    parseAmount, parsePaymentItems, renderMoneyTotalsHtml, categoryTree, categoryMatches,
    NAV_SECTIONS, navSections, defaultSection, sectionNavHtml,
    NAV_BAR_MAX, navBarLayout, navDrawerHtml,
    salesTabs, stockTabs, moneyTabs, clientsTabs,
    BOSS_NAV_ORDER, isBossLike, workActionsOn, deleteActionsOn, roleSectionTabs, resolveScreen, workSwitchHtml, deleteSwitchHtml,
    debtReminderSwitchHtml,
    periodSegHtml, rangeLabel, formatMoney,
    emptyState, skeleton, errorBoxHtml,
    machineStatusLabel, machineSubtitle, machineStatusSegHtml,
    moneyBlockLabel, agingBarsHtml, forecastRowsHtml, buyerKey,
    leadFunnelHtml, firstTouchHtml, replySpeedHtml, durationLabel, postEffectLabel,
    whMoney, whQty, whStockBadge,
    PAY_METHODS, PAY_METHOD_LABEL, PAY_STATE_LABEL, payCents, payRate, payRateCurrency, payConvert,
    payMoney, payPreview, payPartLine, payConfirmable, payAwaitingText, payHandoverHtml, payHandoverOrdersHtml,
    PAY_ACCOUNT_KIND, payAccountItems, payDefaultAccountId, payAccountPrefill, payAccountFormError,
    payAccountFieldHtml, payMissingAccount, payAccountsManagerHtml,
    payHandoverPicked, payDepositOrdersText,
    discountPctLabel, discountLineSuffix, discountSummaryText, discountBlockHtml,
    discountPendingNote,
    reconLines, reconPreview, reconDiffLabel, reconDiffClass, reconGroupHistory, reconHistoryHtml,
  };
});
