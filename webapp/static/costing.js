// Себестоимость и прибыль (services/costing.py, ручки /api/costing/*).
//
// Отдельным файлом, а не в app.js: app.js правят несколько параллельных веток,
// и сотни строк в его середине — гарантированный конфликт. Сюда вынесено всё
// новое; в app.js — только точки монтирования (карточка контейнера, «Продажи →
// Отчёт», каталог и карточка цены). Подключается ПОСЛЕ app.js и пользуется
// его глобалами (api, apiResult, openMachineSheet, toast, …).
//
// Всё — только руководству: ручки отвечают менеджеру 403, и блок, который
// гарантированно ответит отказом, не рисуем вовсе (правило навигации).
//
// Деньги с сервера приходят КОПЕЙКАМИ (BIGINT), поэтому здесь `/ 100`.

(function () {
  'use strict';

  function costingIsBoss() {
    return typeof currentUser !== 'undefined' && !!currentUser
      && ['admin', 'boss'].includes(currentUser.role);
  }

  // Сумма со знаком: курсовая разница и маржа бывают отрицательными, и «−8 USD»
  // должно читаться как потеря, а не как опечатка.
  function costingSigned(cents, currency) {
    const n = Number(cents) || 0;
    const sign = n > 0 ? '+' : n < 0 ? '−' : '';
    return `${sign}${formatMoney(Math.abs(n) / 100, currency)}`;
  }

  // Цена за единицу — с копейками: «11,25 USD», а не «11 USD».
  function costingUnit(cents, currency) {
    if (cents == null) return '—';
    return whMoney(cents, currency);
  }

  // Процент маржи: минус — типографский, как у сумм.
  function costingPct(v) {
    return v == null ? '' : `${String(v).replace('-', '−')}%`;
  }

  const MONTHS = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль',
    'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];

  function costingMonthLabel(ym) {
    const [y, m] = String(ym || '').split('-');
    const idx = Number(m) - 1;
    return MONTHS[idx] ? `${MONTHS[idx]} ${y}` : String(ym || '');
  }

  function costingQty(q) {
    return whQty(q);
  }

  // Курсовая разница словами владельца. `fx` — {at_arrival_cents,
  // at_sale_cents, diff_cents}. Возвращает '' если продаж в другой валюте нет.
  function costingFxText(fx, base) {
    if (!fx || (!fx.at_arrival_cents && !fx.at_sale_cents)) return '';
    const diff = Number(fx.diff_cents) || 0;
    const arrival = formatMoney(fx.at_arrival_cents / 100, base);
    const sale = formatMoney(fx.at_sale_cents / 100, base);
    const verdict = diff < 0
      ? `потеряли на курсе ${formatMoney(-diff / 100, base)}: сум подешевел, пока товар лежал на складе`
      : diff > 0
        ? `выиграли на курсе ${formatMoney(diff / 100, base)}: сум подорожал, пока товар лежал на складе`
        : 'курс не изменился';
    return `Выручка в сумах по курсу дня прибытия товара стоила бы ${arrival}, `
      + `по курсу дня продажи — ${sale}. Итог — ${verdict}. Это разница курса, а не цены.`;
  }

  function statHtml(value, label, status) {
    const cls = status === 'bad' ? ' trend-dn' : '';
    return `<div class="stat"><div class="stat-value${cls}">${value}</div>`
      + `<div class="stat-label">${escapeHtml(label)}</div></div>`;
  }

  function rowHtml(title, sub, value, status) {
    return `<div class="c-row"${status ? ` data-status="${status}"` : ''}>
      <div class="card-row-info">
        <div class="card-row-title">${title}</div>
        ${sub ? `<div class="card-row-sub">${sub}</div>` : ''}
      </div>
      ${value != null ? `<div class="card-row-value">${value}</div>` : ''}
    </div>`;
  }

  // ─── Карточка контейнера: «Закупка и себестоимость» ─────────────────────

  function containerSummaryHtml(s, base) {
    if (!s) return '';
    const margin = s.revenue_cents
      ? `${costingSigned(s.margin_cents, base)}${s.margin_pct != null ? ` · ${costingPct(s.margin_pct)}` : ''}`
      : '—';
    const unknown = s.cost_unknown_qty > 0
      ? `<div class="warn-card">${icon('alert', 'warn-ic')} Без цены закупки: ${costingQty(s.cost_unknown_qty)} шт — `
        + 'их себестоимость и маржа не посчитаны</div>'
      : '';
    const fxText = costingFxText(s.fx, base);
    const stockFx = s.stock_fx_diff_cents
      ? rowHtml('Остаток по сегодняшнему курсу',
          'Во что обошёлся бы тот же остаток, если бы валюту закупки меняли сегодня',
          costingSigned(s.stock_fx_diff_cents, base), s.stock_fx_diff_cents < 0 ? 'approved' : 'rejected')
      : '';
    // Без единой цены «0 USD» читалось бы как бесплатная закупка — пишем словами.
    const noPrices = s.cost_unknown_qty > 0 && s.cost_unknown_qty >= s.purchased_qty;
    const money = (cents) => (noPrices ? 'цена не вписана' : formatMoney(cents / 100, base));
    return `
      <div class="stat-grid">
        ${statHtml(`${costingQty(s.purchased_qty)} шт`, `закуплено · ${money(s.purchased_cost_cents)}`)}
        ${statHtml(`${costingQty(s.sold_qty)} шт`, `продано · ${formatMoney(s.revenue_cents / 100, base)}`)}
        ${statHtml(`${costingQty(s.remaining_qty)} шт`, `на складе · ${money(s.remaining_cost_cents)}`)}
        ${statHtml(margin, 'маржа по проданному', s.margin_cents < 0 ? 'bad' : '')}
      </div>
      ${unknown}
      ${fxText || stockFx ? `<div class="c-surface c-surface--list">
        ${fxText ? rowHtml('Курсовая разница', escapeHtml(fxText), costingSigned(s.fx.diff_cents, base),
          s.fx.diff_cents < 0 ? 'rejected' : 'approved') : ''}
        ${stockFx}
      </div>` : ''}`;
  }

  // `host` — пустой узел, который карточка контейнера кладёт перед составом.
  async function mountContainerCosting(containerId, host) {
    if (!costingIsBoss() || !host) return;
    let card;
    try {
      card = await api('/api/costing/container', { container_id: containerId });
    } catch (e) {
      if (host.isConnected) host.innerHTML = '';
      return;
    }
    if (!host.isConnected || !card.enabled) return;
    const base = card.base_currency || baseCur();
    const h = card.header;
    const items = card.items || [];
    const priced = items.filter(i => i.unit_price_cents != null).length;
    const sug = card.suggestion || {};
    const sugUsd = sug.uzs_per && sug.uzs_per.USD;
    const rateLine = h
      ? `1 USD = ${formatMoney(Number(h.uzs_per_usd), 'сум')}`
        + (h.currency !== 'USD' && h.currency !== 'UZS'
          ? ` · 1 ${escapeHtml(h.currency)} = ${formatMoney(Number(h.uzs_per_unit), 'сум')}` : '')
      : '';
    const headerRows = h
      ? rowHtml(`Закупка в ${escapeHtml(h.currency)}`,
          `Курс на ${escapeHtml(formatDateRU(h.rate_date || card.rate_day))}: ${rateLine}`
            + (h.rate_source === 'cbu' ? ' (ЦБ)' : ' (вручную)'),
          `${priced} из ${items.length}`, priced === items.length ? 'approved' : 'pending')
      : rowHtml('Цены закупки не вписаны',
          sugUsd
            ? `Курс ${sug.source === 'cbu' ? 'ЦБ' : 'текущий'} на ${escapeHtml(formatDateRU(sug.date))}: 1 USD = ${formatMoney(Number(sugUsd), 'сум')}`
            : 'Курс на дату прибытия впишете вручную',
          null, 'pending');
    host.innerHTML = `
      <div class="section-label">Закупка и себестоимость</div>
      <div class="c-surface c-surface--list">${headerRows}</div>
      ${containerSummaryHtml(card.summary, base)}
      <div class="c-actions c-actions--wrap">
        <button class="btn-secondary" id="costing-prices">${icon('edit')} Цены закупки</button>
      </div>`;
    host.querySelector('#costing-prices').addEventListener('click', () =>
      openContainerPricesSheet(containerId, card));
  }

  function openContainerPricesSheet(containerId, card) {
    const h = card.header || {};
    const sug = card.suggestion || {};
    const per = sug.uzs_per || {};
    const currency = h.currency || 'USD';
    const fields = [
      { key: 'currency', label: 'Валюта закупки', type: 'select',
        options: (card.currencies || ['USD', 'UZS']).map(c => [c, c]), value: currency },
      { key: 'uzs_per_usd', label: 'Курс на дату прибытия: сум за 1 USD', type: 'number',
        required: true, value: h.uzs_per_usd || per.USD || '',
        hint: per.USD ? `ЦБ на ${formatDateRU(sug.date)}: ${per.USD}` : 'По умолчанию — курс ЦБ на дату прибытия' },
      { key: 'uzs_per_unit', label: 'Сум за 1 единицу валюты закупки', type: 'number',
        value: (h.currency && !['USD', 'UZS'].includes(h.currency)) ? h.uzs_per_unit : (per.CNY || ''),
        hint: 'Нужен только для валюты кроме USD и сум (например, CNY)' },
    ];
    for (const it of card.items || []) {
      fields.push({
        key: `p_${it.id}`,
        label: `${it.name} · ${costingQty(it.qty)} ${it.unit || 'шт'}`,
        type: 'number',
        placeholder: 'цена за единицу',
        value: it.unit_price_cents == null ? '' : String(it.unit_price_cents / 100),
      });
    }
    const sheet = openMachineSheet({
      title: 'Цены закупки',
      hint: 'Цена — за единицу, в валюте закупки. Пустое поле — цену ещё не знаем. '
        + 'Фрахт, таможню и аренду вписывать не обязательно.',
      fields,
      submitLabel: 'Сохранить',
      onSubmit: async (data, { showErr }) => {
        const prices = {};
        for (const it of card.items || []) prices[it.id] = data[`p_${it.id}`];
        const res = await apiResult('/api/costing/container/save', {
          container_id: containerId,
          currency: data.currency,
          uzs_per_usd: data.uzs_per_usd,
          uzs_per_unit: data.uzs_per_unit,
          rate_source: (!card.header && per.USD && data.uzs_per_usd === per.USD && sug.source === 'cbu')
            || (card.header && card.header.rate_source === 'cbu' && data.uzs_per_usd === card.header.uzs_per_usd)
            ? 'cbu' : 'manual',
          prices,
        });
        if (!res.ok) { showErr(res.error); return false; }
        haptic('success');
        toast('Цены закупки сохранены');
        renderContainerCard(containerId);
        return true;
      },
    });
    // Поле курса прочей валюты нужно только ей: для USD и сум оно лишнее, а
    // пустое обязательное поле на телефоне — повод решить, что форма сломана.
    const ov = (sheet && sheet.sheet) || document.querySelector('.c-overlay');
    const unitField = ov && ov.querySelector('#ms-f-uzs_per_unit');
    const syncUnit = () => {
      const cur = ov.querySelector('#ms-f-currency').value;
      const label = unitField.closest('label');
      // style, а не hidden: `.c-field { display: flex }` перебивает атрибут.
      if (label) label.style.display = (cur === 'USD' || cur === 'UZS') ? 'none' : '';
    };
    if (unitField) {
      syncUnit();
      ov.querySelectorAll('.seg-item[data-opt]').forEach(b => b.addEventListener('click', syncUnit));
    }
  }

  // ─── «Продажи → Отчёт»: прибыль, курсовая разница ────────────────────────

  function reportBody() {
    return (analyticsPeriod === 'custom' && analyticsSince && analyticsUntil)
      ? { since: analyticsSince, until: _nextDay(analyticsUntil) }
      : { period: analyticsPeriod };
  }

  function disabledHtml() {
    return `<div class="section-label">Прибыль и себестоимость</div>
      <div class="c-surface c-surface--pad">
        <div class="card-row-sub">Учёт себестоимости выключен. Включите его после переноса истории
        склада: с этого момента цены закупки контейнеров и каждая продажа начнут считать прибыль,
        маржу и курсовую разницу. Прошлые продажи прибыль не получат — их себестоимость неизвестна.</div>
      </div>
      <div class="c-actions"><button class="btn-secondary" id="costing-enable">${icon('check')} Включить учёт</button></div>`;
  }

  function reportHtml(rep) {
    const base = rep.base_currency || baseCur();
    const t = rep.totals || {};
    const fx = rep.fx || {};
    const fxText = costingFxText(fx, base);
    const started = rep.started_at ? ` · учёт с ${escapeHtml(formatDateRU(rep.started_at))}` : '';
    const unknown = t.unknown_cost_lines
      ? `<div class="warn-card">${icon('alert', 'warn-ic')} ${plural(t.unknown_cost_lines, ['позиция', 'позиции', 'позиций'])} без себестоимости на
         ${formatMoney(t.unknown_cost_revenue_cents / 100, base)} выручки — в прибыль не вошли. Впишите цены закупки контейнера.</div>`
      : '';
    const months = (rep.by_month || []).map(m => rowHtml(
      escapeHtml(costingMonthLabel(m.month)),
      `выручка ${formatMoney(m.revenue_cents / 100, base)} · себестоимость ${formatMoney(m.cogs_cents / 100, base)}`
        + (m.fx_diff_cents ? ` · курс ${costingSigned(m.fx_diff_cents, base)}` : ''),
      costingSigned(m.profit_cents, base), m.profit_cents < 0 ? 'rejected' : 'approved',
    )).join('');
    const products = (rep.top_products || []).map(p => rowHtml(
      escapeHtml(p.name),
      `${costingQty(p.qty)} шт · выручка ${formatMoney(p.revenue_cents / 100, base)}`
        + (p.margin_pct != null ? ` · маржа ${costingPct(p.margin_pct)}` : '')
        + (p.partial ? ' · часть без себестоимости' : ''),
      costingSigned(p.profit_cents, base), p.profit_cents < 0 ? 'rejected' : null,
    )).join('');
    const negative = (rep.negative_deals || []).map(d => rowHtml(
      `${escapeHtml(d.invoice_number)} · ${escapeHtml(d.counterparty)}`,
      `${escapeHtml(formatDateRU(d.date))}${d.order_id ? ` · заказ #${d.order_id}` : ''} · выручка ${formatMoney(d.revenue_cents / 100, base)}, себестоимость ${formatMoney(d.cogs_cents / 100, base)}`,
      costingSigned(d.profit_cents, base), 'rejected',
    )).join('');
    const containers = (rep.containers || []).map(c => rowHtml(
      escapeHtml(c.number || `#${c.container_id}`),
      `закуплено ${costingQty(c.purchased_qty)} на ${formatMoney(c.purchased_cost_cents / 100, base)}`
        + ` · продано ${costingQty(c.sold_qty)} · осталось ${costingQty(c.remaining_qty)} на ${formatMoney(c.remaining_cost_cents / 100, base)}`
        + (c.fx && c.fx.diff_cents ? ` · курс ${costingSigned(c.fx.diff_cents, base)}` : ''),
      `${costingSigned(c.margin_cents, base)}${c.margin_pct != null ? ` · ${costingPct(c.margin_pct)}` : ''}`,
      c.margin_cents < 0 ? 'rejected' : 'approved',
    )).join('');
    return `
      <div class="section-label">Прибыль${started}</div>
      <div class="stat-grid">
        ${statHtml(costingSigned(t.profit_cents, base), t.margin_pct != null ? `прибыль · маржа ${costingPct(t.margin_pct)}` : 'прибыль', t.profit_cents < 0 ? 'bad' : '')}
        ${statHtml(formatMoney((t.revenue_cents || 0) / 100, base), 'выручка с себестоимостью')}
        ${statHtml(formatMoney((t.cogs_cents || 0) / 100, base), 'себестоимость проданного')}
        ${statHtml(costingSigned(fx.diff_cents, base), 'курсовая разница', fx.diff_cents < 0 ? 'bad' : '')}
      </div>
      ${unknown}
      ${fxText ? `<div class="c-surface c-surface--pad"><div class="card-row-sub" id="costing-fx-text">${escapeHtml(fxText)}</div></div>` : ''}
      ${months ? `<div class="section-label">Прибыль по месяцам</div><div class="c-surface c-surface--list">${months}</div>` : ''}
      ${products ? `<div class="section-label">Прибыль по товарам</div><div class="c-surface c-surface--list">${products}</div>` : ''}
      ${negative ? `<div class="section-label">Сделки в минус</div><div class="c-surface c-surface--list">${negative}</div>` : ''}
      ${containers ? `<div class="section-label">Контейнеры: закуплено → продано → осталось</div><div class="c-surface c-surface--list">${containers}</div>` : ''}
      ${!months && !unknown ? '<div class="loader">Продаж с себестоимостью за период нет</div>' : ''}`;
  }

  async function mountSalesCosting(host) {
    if (!costingIsBoss() || !host) return;
    const gen = screenGen();
    let rep;
    try {
      rep = await api('/api/costing/report', reportBody());
    } catch (e) {
      if (host.isConnected && gen === screenGen()) host.innerHTML = '';
      return;
    }
    if (!host.isConnected || gen !== screenGen()) return;
    host.innerHTML = rep.enabled ? reportHtml(rep) : disabledHtml();
    const enable = host.querySelector('#costing-enable');
    if (enable) {
      enable.addEventListener('click', async () => {
        if (!await confirmDialog('Включить учёт себестоимости? Включайте после переноса истории склада.')) return;
        const res = await apiResult('/api/costing/settings/set', { enabled: true });
        if (!res.ok) { tg.showAlert ? tg.showAlert(res.error) : alert(res.error); return; }
        haptic('success');
        toast('Учёт себестоимости включён');
        mountSalesCosting(host);
      });
    }
  }

  // ─── Карточка цены товара: себестоимость по партиям ──────────────────────

  async function mountProductCostHistory(host, productId) {
    if (!costingIsBoss() || !host) return;
    let res;
    try {
      res = await api('/api/costing/product', { product_id: productId });
    } catch (e) {
      return;
    }
    if (!host.isConnected || !res.enabled) return;
    const base = res.base_currency || baseCur();
    const batches = res.batches || [];
    if (!batches.length && res.avg_cost_cents == null) return;
    const avgTitle = res.avg_cost_cents != null
      ? `${costingUnit(res.avg_cost_cents, base)} за единицу`
      : 'Партий с ценой на складе нет';
    const avgSub = res.avg_cost_cents != null
      ? 'средняя по остатку на складе'
      : 'действует ручная себестоимость';
    const rows = batches.slice(0, 10).map(b => rowHtml(
      `${escapeHtml(formatDateRU(b.date))}${b.container_number ? ` · ${escapeHtml(b.container_number)}` : ''}`,
      `${costingQty(b.qty)} шт по ${b.unit_price_cents != null ? costingUnit(b.unit_price_cents, b.currency) : 'цена не вписана'}`
        + ` · осталось ${costingQty(b.remaining)}`,
      b.unit_cost_cents != null ? costingUnit(b.unit_cost_cents, base) : '—',
      b.remaining > 0 ? 'approved' : null,
    )).join('');
    host.innerHTML = `<div class="section-label">Себестоимость по партиям</div>
      <div class="c-surface c-surface--list">${rowHtml(escapeHtml(avgTitle), escapeHtml(avgSub), null)}${rows}</div>`;
  }

  Object.assign(window, {
    mountContainerCosting, mountSalesCosting, mountProductCostHistory,
    costingFxText, costingMonthLabel, costingSigned,
  });
})();
