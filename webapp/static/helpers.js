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

  // Набор вкладок раздела «Финансы» по роли — чистая функция (тестируется).
  // Плоская навигация: ≤4 вкладки на любую роль, чтобы ряд не переносился.
  // «Обзор» переехал в Аналитику→Деньги; «Мои сдачи» свёрнуты в «Платежи и сдачи».
  function financeTabs(f) {
    f = f || {};
    const tabs = [];
    if (f.isConfirmer) tabs.push({ key: 'confirm', label: 'Подтверждения' });
    tabs.push({ key: 'debts', label: 'Долги' });
    if (f.hasOps) tabs.push({ key: 'ops', label: 'Платежи и сдачи' });
    if (f.isBoss) tabs.push({ key: 'limits', label: 'Клиенты' });
    return tabs;
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
      `<button class="seg-aux ${customActive ? 'active' : ''}" ${attr}="custom" ` +
      `aria-pressed="${customActive}">${icon('clock')} ${escapeHtml(customLabel || 'Период…')}</button></div>`
    );
  }

  return {
    escapeHtml, idemKey, formatDateRU, icon, opsAmount,
    renderOpsSummaryHtml, parsePaymentItems, renderMoneyTotalsHtml, financeTabs,
    balanceParts, periodSegHtml, formatMoney, msBalanceLabel,
    emptyState, skeleton, errorBoxHtml,
  };
});
