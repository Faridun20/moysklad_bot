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
    const section = (title, count, rowsHtml) => {
      blocks.push(
        `<div class="section-label">${escapeHtml(title)} ` +
        `<span class="stock-badge badge-yellow">${count}</span></div>` +
        `<div class="card-list">${rowsHtml}</div>`
      );
    };
    const row = (title, sub) =>
      `<div class="card-row" style="cursor:default;">` +
      `<div class="card-row-info"><div class="card-row-title">${escapeHtml(title)}</div>` +
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
        (ov.items || []).map(o => row(`#${o.id} · ${o.agent_name}`, o.full_name)).join(''));
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
    const bat = summary.expiring_batches || {};
    if (bat.count > 0) {
      section(`Истекают партии (≤${bat.threshold_days}д)`, bat.count,
        (bat.items || []).map(b => row(`${b.code}`, `до ${b.expiry_date} · остаток ${opsAmount(b.qty_remaining)}`)).join(''));
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
                      : `${c.hours_ago}ч назад · ${c.last_status} (порог ${c.threshold_hours}ч)`)).join(''));
    }
    const ms = summary.ms_anomalies || {};
    const msTotal = (ms.drift || 0) + (ms.deleted || 0) + (ms.demand_failed || 0) + (ms.transition_blocked || 0);
    if (msTotal > 0) {
      const items = ms.items || {};
      const rows = []
        .concat((items.demand_failed || []).map(o => row(`📦 Отгрузка не создана · #${o.id}`, o.agent_name)))
        .concat((items.transition_blocked || []).map(o => row(`⛔ Статус застрял · #${o.id}`, `${o.agent_name} · ${o.status}`)))
        .concat((items.drift || []).map(o => row(`✏️ Изменён в МС · #${o.id}`, o.agent_name)))
        .concat((items.deleted || []).map(o => row(`🗑 Удалён в МС · #${o.id}`, `${o.agent_name} · ${o.status}`)));
      section('Рассинхрон с МойСклад', msTotal, rows.join(''));
    }

    if (!blocks.length) {
      return '<div style="padding:32px 16px;text-align:center;color:var(--muted);">' +
        '✅ Всё спокойно — нет требующих внимания позиций.</div>';
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
    if (f.isBoss) tabs.push({ key: 'limits', label: 'Лимиты' });
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

  return {
    escapeHtml, idemKey, formatDateRU, icon, opsAmount,
    renderOpsSummaryHtml, parsePaymentItems, renderMoneyTotalsHtml, financeTabs,
  };
});
