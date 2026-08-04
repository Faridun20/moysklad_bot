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

  // Рендер операционной сводки (данные /api/ops-summary, см. services/ops_summary).
  // Чистая функция (тестируется в Vitest): принимает dict секций, возвращает HTML.
  // Показываем только непустые секции; если всё пусто — «всё спокойно».
  function renderOpsSummaryHtml(summary) {
    summary = summary || {};
    const blocks = [];
    // UI-WP-29: секции сводки — общие примитивы поверхности и строки.
    // `tone` — критичность алерта в языке общей статус-системы: просроченные
    // деньги и рассинхрон с МС не должны выглядеть так же, как «низкий
    // остаток», а раньше все счётчики были одинаково жёлтыми.
    const section = (title, count, rowsHtml, tone) => {
      blocks.push(
        `<div class="section-label">${escapeHtml(title)} ` +
        `<span class="stock-badge" data-status="${tone || 'low'}">${count}</span></div>` +
        `<div class="c-surface c-surface--list">${rowsHtml}</div>`
      );
    };
    const row = (title, sub, ic) =>
      `<div class="c-row">` +
      `<div class="card-row-info"><div class="card-row-title">${ic ? icon(ic) + ' ' : ''}${escapeHtml(title)}</div>` +
      (sub ? `<div class="card-row-sub">${escapeHtml(sub)}</div>` : '') +
      `</div></div>`;

    const so = summary.stale_orders || {};
    if (so.count > 0) {
      section(`Зависшие заявки (>${so.threshold_hours}ч)`, so.count,
        (so.items || []).map(o => row(`#${o.id} · ${o.agent_name}`, o.full_name)).join(''));
    }
    const ov = summary.overdue_undeposited || {};
    if (ov.count > 0) {
      section(`Отгружено, деньги не сданы (>${ov.threshold_days}д)`, ov.count,
        (ov.items || []).map(o => row(`#${o.id} · ${o.agent_name}`, o.full_name)).join(''), 'out');
    }
    const dep = summary.deposits || {};
    if (dep.count > 0) {
      section(`Сдачи на подтверждении · ${opsAmount(dep.total)} USD`, dep.count,
        (dep.items || []).map(d => row(`Сдача #${d.id}`, `${opsAmount(d.amount)} USD`)).join(''));
    }
    const ret = summary.returns || {};
    if (ret.count > 0) {
      section('Возвраты на подтверждении', ret.count,
        (ret.items || []).map(r =>
          row(`Возврат #${r.id} · заказ #${r.order_id != null ? r.order_id : '?'}`,
            `${opsAmount(r.total_amount)} USD`)).join(''));
    }
    const low = summary.low_stock || {};
    if (low.count > 0) {
      section(`Низкий остаток (≤${opsAmount(low.threshold)})`, low.count,
        (low.items || []).map(r => row(`${r.name}`, `${opsAmount(r.available)} ${r.unit}`)).join(''));
    }
    const cr = summary.stale_crons || {};
    if (cr.count > 0) {
      section('Cron: не отчитались', cr.count,
        (cr.items || []).map(c => row(c.task_name,
          c.never_ran ? `ни разу не запускался (порог ${c.threshold_hours}ч)`
                      : `${c.hours_ago}ч назад · ${c.last_status} (порог ${c.threshold_hours}ч)`)).join(''), 'out');
    }
    const ms = summary.ms_anomalies || {};
    const msTotal = (ms.drift || 0) + (ms.deleted || 0) + (ms.demand_failed || 0) + (ms.transition_blocked || 0);
    if (msTotal > 0) {
      const items = ms.items || {};
      const rows = []
        .concat((items.demand_failed || []).map(o => row(`Отгрузка не создана · #${o.id}`, o.agent_name, 'box')))
        .concat((items.transition_blocked || []).map(o => row(`Статус застрял · #${o.id}`, `${o.agent_name} · ${o.status}`, 'ban')))
        .concat((items.drift || []).map(o => row(`Изменён в МС · #${o.id}`, o.agent_name, 'edit')))
        .concat((items.deleted || []).map(o => row(`Удалён в МС · #${o.id}`, `${o.agent_name} · ${o.status}`, 'trash')));
      section('Рассинхрон с МойСклад', msTotal, rows.join(''), 'out');
    }

    if (!blocks.length) {
      return `<div class="loader">${icon('check')} Всё спокойно — нет требующих внимания позиций.</div>`;
    }
    return blocks.join('');
  }

  // Парсинг строк мульти-валютной формы платежа в payload для /api/payments/send.
  // Чистая функция (тестируется): принимает [{amount, currency}] как ввёл юзер
  // (amount — строка/число), возвращает {items:[{amount:Number, currency}]} или
  // {error}. Запятая как десятичный разделитель, пробелы игнорируются.
  function parsePaymentItems(rows) {
    const items = [];
    for (const r of rows || []) {
      const amt = parseFloat(String((r && r.amount) || '').replace(',', '.').replace(/\s/g, ''));
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
    { key: 'sales',   label: 'Продажи',  icon: 'cart',
      roles: ['admin', 'boss', 'manager', 'warehouse_keeper', 'bookkeeper'] },
    { key: 'stock',   label: 'Склад',    icon: 'box',
      roles: ['admin', 'boss', 'manager'] },
    { key: 'money',   label: 'Деньги',   icon: 'wallet',
      roles: ['admin', 'boss', 'manager', 'warehouse_keeper', 'bookkeeper'] },
    { key: 'clients', label: 'Клиенты',  icon: 'user',
      roles: ['admin', 'boss', 'manager'] },
  ];

  function navSections(role) {
    return NAV_SECTIONS.filter((s) => s.roles.indexOf(role) !== -1);
  }

  // Экран по умолчанию для роли: первый доступный ей раздел. У кладовщика нет
  // «Сегодня» (ручка /api/home ему не отвечает), и открывать его на экране с
  // ошибкой — худшее, что можно сделать при входе.
  function defaultSection(role) {
    const list = navSections(role);
    return list.length ? list[0].key : null;
  }

  // Продажи: заказы и отчёт по ним. Каталог отсюда уехал на «Склад» — как
  // отдельный экран это остатки, а внутри заказа товар выбирают в форме.
  function salesTabs(f) {
    f = f || {};
    const tabs = [{ key: 'orders', label: 'Заказы' }];
    if (f.canSeeReport) tabs.push({ key: 'report', label: 'Отчёт' });
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
    // «Что лежит без движения» — вопрос про склад, а не про отчёт, поэтому
    // вкладка здесь. Ручка отвечает только руководству, у остальных её нет.
    if (f.isBoss) tabs.push({ key: 'stale', label: 'Залежалось' });
    return tabs;
  }

  // Деньги: бывшие «Финансы» + бывшая «Аналитика → Деньги». Долги и дебиторка —
  // один предмет, и держать их в разных разделах значило требовать от человека
  // знать, в каком именно.
  function moneyTabs(f) {
    f = f || {};
    const tabs = [];
    if (f.isConfirmer) tabs.push({ key: 'confirm', label: 'Подтвердить' });
    if (f.canSeeDebts) tabs.push({ key: 'debts', label: 'Долги' });
    if (f.hasOps) tabs.push({ key: 'ops', label: 'Касса' });
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
    const tabs = [{ key: 'funnel', label: 'Воронка' }, { key: 'list', label: 'Лиды' }];
    if (f.isBoss) {
      tabs.push({ key: 'limits', label: 'Лимиты' });
      tabs.push({ key: 'channel', label: 'Канал' });
    }
    return tabs;
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
    return `<div class="seg-row"><div class="seg${scroll}">${items}</div></div>`;
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
    }
    if (missing.length) {
      const list = missing.map((m) => `${m.currency} ${opsAmount(m.amount)}`).join(', ');
      head +=
        `<div class="money-total-note">Без курса не учтено: ` +
        `${escapeHtml(list)} — задайте курс валют.</div>`;
    }
    const rows = pays
      .map((p) => row(`${p.currency} · ${fmtC(p.total_cents)}`, `${p.count} платеж.`))
      .join('');
    const depRow = row(
      `Наличные (сдачи) · ${fmtC(dep.total_cents)} ${baseCurrency}`,
      `${dep.count || 0} сдач.`
    );
    return `${head}<div class="stock-list">${rows}${depRow}</div>`;
  }

  // Разбор МС-баланса контрагента (взаиморасчёты) для отображения — ЕДИНЫЙ
  // источник инвертированной конвенции знака (WP-27): <0 — клиент ДОЛЖЕН нам,
  // >0 — аванс/переплата. Раньше тернарники дублировались в renderClients и
  // renderAgentDetail и уже разъезжались (был sign-баг). Возвращает {state,
  // amount, currency}; разметку каждый экран строит сам.
  function balanceParts(cents, baseCurrency) {
    const cur = String(baseCurrency || 'USD');
    if (cents == null) return { state: 'none', amount: '', currency: cur };
    const c = Number(cents) || 0;
    if (c < 0) return { state: 'owe', amount: opsAmount(-c / 100), currency: cur };
    if (c > 0) return { state: 'adv', amount: opsAmount(c / 100), currency: cur };
    return { state: 'zero', amount: '0', currency: cur };
  }

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
      || msg === 'Нет подключения к интернету';
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
  // где-то округляли, где-то нет, где-то валюту клеили без пробела. Копейки в
  // UI не показываем осознанно: суммы сделок — тысячи, дробная часть только
  // шумит; точные значения живут в БД.
  function formatMoney(n, currency) {
    const num = Number(n);
    if (!isFinite(num)) return '—';
    const text = Math.round(num).toLocaleString('ru-RU');
    return currency ? `${text} ${currency}` : text;
  }

  // Готовая подпись МС-баланса (UI-WP-05). balanceParts даёт знак и сумму, но
  // САМА ПОДПИСЬ дублировалась в renderClients (balStr) и renderAgentDetail
  // (balLine) двумя разными тернарниками — и формулировки уже разошлись
  // («должен» против «должен нам»). tone отдаём отдельно, чтобы экран сам решал
  // про класс/вёрстку и не парсил текст.
  function msBalanceLabel(cents, baseCurrency) {
    const p = balanceParts(cents, baseCurrency);
    if (p.state === 'none') return { text: '—', tone: 'none' };
    if (p.state === 'owe') return { text: `должен ${p.amount} ${p.currency}`, tone: 'owe' };
    if (p.state === 'adv') return { text: `аванс ${p.amount} ${p.currency}`, tone: 'advance' };
    return { text: `0 ${p.currency}`, tone: 'zero' };
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

  // Единый период-сегмент (WP-29): пресеты .seg-item + доп-кнопка «Период…»
  // (произвольный диапазон). Раньше разметка дублировалась в analyticsHeaderHtml
  // (data-period) и renderOrdersMain (data-operiod) и уже разъехалась — в Заказах
  // кнопка не показывала выбранный диапазон. attr — имя data-атрибута
  // ('data-period'|'data-operiod'); customLabel — подпись доп-кнопки (даты или
  // «Период…»). Возвращает .seg-row (seg + aux).
  function periodSegHtml(presets, activeId, attr, customActive, customLabel) {
    const seg = (presets || []).map((p) =>
      `<button class="seg-item ${activeId === p.id ? 'active' : ''}" ${attr}="${p.id}" ` +
      `aria-pressed="${activeId === p.id}">${escapeHtml(p.label)}</button>`
    ).join('');
    return (
      // UI-BUG-01: именно `seg--scroll` — у периода четыре пункта плюс
      // доп-кнопка справа, и на 360dp они не влезают. Без варианта подписи
      // резались.
      `<div class="seg-row"><div class="seg seg--scroll">${seg}</div>` +
      // UI-BUG-02: пока активен пресет, кнопка — только иконка с aria-label.
      // Подпись «Период…» занимала ~62px, из-за которых ряд и рушился; текст
      // нужен лишь когда выбран произвольный диапазон и его надо показать.
      `<button class="seg-aux ${customActive ? 'active' : ''}" ${attr}="custom" ` +
      `aria-pressed="${customActive}" aria-label="Выбрать период"` +
      `${customActive ? '' : ' title="Выбрать период"'}>` +
      `${icon('clock')}${customActive && customLabel ? ' ' + escapeHtml(customLabel) : ''}` +
      `</button></div>`
    );
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
    return `<div class="seg-row"><div class="seg seg--scroll">` +
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
          <div class="aging-count">${b.count} ${b.count === 1 ? 'документ' : 'документов'}</div>
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
          <div class="aging-count">${m.count} ${m.count === 1 ? 'платёж' : 'платежей'}${share}</div>
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
    return `за ${window} ч после поста — ${after} обращ. · обычно ${baseText}/день`;
  }

  return {
    escapeHtml, idemKey, formatDateRU, icon, opsAmount,
    renderOpsSummaryHtml, parsePaymentItems, renderMoneyTotalsHtml,
    NAV_SECTIONS, navSections, defaultSection, sectionNavHtml,
    salesTabs, stockTabs, moneyTabs, clientsTabs,
    balanceParts, periodSegHtml, rangeLabel, formatMoney, msBalanceLabel,
    emptyState, skeleton, errorBoxHtml,
    machineStatusLabel, machineSubtitle, machineStatusSegHtml,
    moneyBlockLabel, agingBarsHtml, forecastRowsHtml, buyerKey,
    leadFunnelHtml, firstTouchHtml, replySpeedHtml, durationLabel, postEffectLabel,
  };
});
