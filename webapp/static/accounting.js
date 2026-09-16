// Бухгалтерия, этап 1: счета, «Получил деньги», расход, перевод, закрытие дня,
// журнал операций. Сервер — `/api/acc/*` (services/accounting.py).
//
// Отдельный файл, а не очередные сотни строк в app.js: раздел живёт за
// выключателем и меняется независимо от остального фронта. Подключается ПОСЛЕ
// app.js и пользуется его глобалами (api, apiResult, openMachineSheet,
// openListPicker, toast, haptic, idemKey, currentUser, role, screenGen …).
// Точки входа из app.js — через `typeof accX === 'function'`: без этого файла
// приложение работает ровно как до бухгалтерии.
//
// Где что на экране. Вкладка «Касса» раздела «Деньги» при включённой
// бухгалтерии показывает «Сейчас · Операции · Ещё»: остатки и действия,
// журнал с фильтрами и старые формы (возвраты, сдачи), чтобы до них по-прежнему
// можно было дойти. Новой вкладки нет — у руководителя их уже четыре, и пятая
// уехала бы в скролл. Кнопка оплаты в «Долгах» становится «Получил деньги».

let accView = 'now';                 // now | journal | more | accounts
let accJournal = { kind: '', period: 'today', accountId: '' };
let accToday = '';                   // бизнес-дата с сервера (Asia/Tashkent)

const ACC_KIND_LABEL = {
  opening: 'Начальный остаток', receipt: 'Получили', expense: 'Расход',
  transfer: 'Перевод', exchange: 'Обмен', reconcile: 'Закрытие дня',
};
// Состояние строки журнала — атрибутом из общей матрицы статусов (CLAUDE.md:
// не заводить свой класс с цветом): приход зелёный, расход красный, перевод
// синий, служебное серое.
const ACC_KIND_STATUS = {
  receipt: 'approved', expense: 'rejected', transfer: 'shipped', exchange: 'shipped',
  reconcile: 'draft', opening: 'draft',
};
const ACC_STATE_LABEL = {
  void: 'отменено', payment_pending: 'ждёт подтверждения', payment_rejected: 'платёж отклонён',
  receipt_deleted: 'поступление удалено',
};

function accEnabled() { return !!(currentUser && currentUser.accounting_enabled); }
function accCanManage() { return ['admin', 'boss'].includes(role()); }

// ─── Чистые хелперы (тестируются в __tests__/accounting.test.js) ────────────

// Копейки → «1 234,56 USD»; дробная часть — только когда она есть. Не
// formatMoney: тот округляет до целых, а журнал денег и расхождение кассы
// обязаны сходиться до цента (как `whMoney` у накладной, но без «,00» у сумов).
function accMoney(cents, currency) {
  const n = Math.round(Number(cents) || 0);
  const abs = Math.abs(n);
  let text = Math.floor(abs / 100).toLocaleString('ru-RU');
  if (abs % 100) text += ',' + String(abs % 100).padStart(2, '0');
  return (n < 0 ? '−' : '') + text + (currency ? ` ${currency}` : '');
}

// Ввод суммы → копейки (как services.money.parse_amount): «1 500,50» → 150050.
// null — не число или не больше нуля (`allowZero` — для пересчёта кассы).
function accParseCents(raw, allowZero) {
  const t = String(raw == null ? '' : raw).replace(/[\s\u00a0\u202f]/g, '').replace(',', '.');
  if (!/^\d+(\.\d+)?$/.test(t)) return null;
  const cents = Math.round(parseFloat(t) * 100);
  if (!isFinite(cents) || cents < 0 || (cents === 0 && !allowZero)) return null;
  return cents;
}

function accParseRate(raw) {
  const t = String(raw == null ? '' : raw).replace(/[\s\u00a0\u202f]/g, '').replace(',', '.');
  if (!/^\d+(\.\d+)?$/.test(t)) return null;
  const v = parseFloat(t);
  return v > 0 && isFinite(v) ? v : null;
}

// Пересчёт строки в валюту долга через базовую — та же формула, что на сервере
// (`accounting.convert_cents`). `quotes` — единиц валюты за 1 базовую.
function accConvert(cents, from, to, quotes, base) {
  if (from === to) return cents;
  const q = (c) => Number(quotes && quotes[c]);
  if (from !== base && !(q(from) > 0)) return null;
  if (to !== base && !(q(to) > 0)) return null;
  const inBase = from === base ? cents : Math.round(cents / q(from));
  return to === base ? inBase : Math.round(inBase * q(to));
}

// Итог формы «Получил деньги»: сколько зачтётся в долг и что останется.
// lines: [{cents, currency}], target: {currency, available_cents}.
function accReceiptPreview(lines, target, quotes, base) {
  let total = 0;
  let missingRate = false;
  let any = false;
  for (const l of lines || []) {
    if (!(l.cents > 0) || !l.currency) continue;
    any = true;
    const v = accConvert(l.cents, l.currency, target.currency, quotes, base);
    if (v == null) { missingRate = true; continue; }
    total += v;
  }
  const available = Number(target.available_cents) || 0;
  // Допуск на округление пересчёта — как OVERPAY_TOLERANCE_MINOR на сервере.
  const over = total > available + 100;
  return {
    any, total, missingRate, over,
    left: Math.max(0, available - total),
    valid: any && !missingRate && total > 0 && !over,
  };
}

// Какие курсы спрашивать в форме: только те не-базовые валюты, где строка
// пересчитывается в ДРУГУЮ валюту долга. Если всё в валюте долга, курс нужен
// лишь для суммы в базовой — сервер возьмёт ЦБ, спрашивать человека незачем.
function accRatesNeeded(lineCurrencies, targetCurrency, base) {
  const out = new Set();
  for (const c of lineCurrencies || []) {
    if (!c || c === targetCurrency) continue;
    if (c !== base) out.add(c);
    if (targetCurrency !== base) out.add(targetCurrency);
  }
  return [...out].sort();
}

function accAccountSub(a) {
  const parts = [a.kind_label || '', a.currency];
  if (a.bank) parts.push(a.bank);
  if (a.card_last4) parts.push(`··${a.card_last4}`);
  if (a.holder) parts.push(a.holder);
  return parts.filter(Boolean).join(' · ');
}

function accDocDetailsLabel(doc) {
  const es = doc.entries || [];
  if (doc.kind === 'transfer' || doc.kind === 'exchange') {
    const out = es.find(e => e.direction === 'out');
    const inn = es.find(e => e.direction === 'in');
    if (!out || !inn) return '';
    return `${out.account_name} → ${inn.account_name}` +
      (doc.kind === 'exchange' ? ` · получили ${accMoney(inn.amount_cents, inn.currency)}` : '');
  }
  if (doc.kind === 'reconcile' || doc.kind === 'opening') return es.length ? es[0].account_name : '';
  return es.map(e => `${e.account_name} ${accMoney(e.amount_cents, e.currency)}`).join(' · ');
}

function accPeriodSince(period, today) {
  if (!today || period === 'all') return '';
  if (period === 'today') return today;
  const d = new Date(`${today}T00:00:00`);
  d.setDate(d.getDate() - (period === 'week' ? 6 : 29));
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// Сумма документа для списка журнала: справа — ОДНА сумма (у поступления —
// сколько зачтено в долг), разбивка по счетам уезжает в подстроку
// (`accDocDetailsLabel`). Две валюты справа не влезают в 390px и съедают имя.
function accDocAmountLabel(doc) {
  const es = doc.entries || [];
  const byCur = (dir) => {
    const acc = {};
    es.filter(e => e.direction === dir).forEach(e => { acc[e.currency] = (acc[e.currency] || 0) + e.amount_cents; });
    return Object.keys(acc).map(c => accMoney(acc[c], c));
  };
  if (doc.kind === 'transfer' || doc.kind === 'exchange') return byCur('out')[0] || '';
  if (doc.kind === 'receipt' && doc.target_cents != null) {
    return '+' + accMoney(doc.target_cents, doc.target_currency);
  }
  if (doc.kind === 'reconcile') {
    if (!es.length) return 'сошлось';
    const e = es[0];
    return (e.direction === 'in' ? '+' : '−') + accMoney(e.amount_cents, e.currency);
  }
  const dir = doc.kind === 'expense' ? 'out' : 'in';
  const parts = byCur(dir);
  return (dir === 'in' ? '+' : '−') + parts.join(' + ');
}

// ─── Точки входа из app.js ─────────────────────────────────────────────────

// «Долги»: вместо поля «Сумма» + «Отметить» — одна кнопка «Получил деньги».
// Зовётся сразу после отрисовки, ДО навешивания обработчиков старой кнопки,
// поэтому старый обработчик просто не находит своей кнопки.
function accDecorateDebts(container) {
  if (!accEnabled() || !container) return;
  container.querySelectorAll('.pay-input-row').forEach(row => {
    const input = row.querySelector('.pay-amount-input');
    const id = input && input.dataset.id;
    if (!id) return;
    row.innerHTML = `<button class="btn-primary acc-pay" data-acc-order="${escapeHtml(id)}">${icon('cash')} Получил деньги</button>`;
    row.querySelector('.acc-pay').addEventListener('click', () => {
      haptic('light');
      accOpenReceipt({ orderId: Number(id), refresh: () => renderDebts(container) });
    });
  });
}

// Руководителю в «Кассе», пока бухгалтерия выключена: одна строка с кнопкой.
function accToggleCardHtml() {
  return `
    <div class="section-label">Бухгалтерия</div>
    <div class="c-surface c-surface--list">
      <div class="c-row">
        <div class="card-row-info">
          <div class="card-row-title">Где лежат деньги, расходы, закрытие дня</div>
          <div class="card-row-sub">Выключено. Включите, когда перенесёте историю склада.</div>
        </div>
      </div>
    </div>
    <button class="btn-secondary" id="acc-enable">${icon('wallet')} Включить бухгалтерию</button>`;
}

function accMountToggle(container) {
  if (accEnabled() || !accCanManage() || !container) return;
  const box = document.createElement('div');
  box.innerHTML = accToggleCardHtml();
  container.appendChild(box);
  box.querySelector('#acc-enable').addEventListener('click', () => {
    openMachineSheet({
      title: 'Включить бухгалтерию',
      hint: 'Появятся счета и касса, «Получил деньги» в долгах, расходы и закрытие дня. Выключить можно в списке счетов и касс.',
      fields: [{ key: 'start_date', label: 'С какой даты ведём', type: 'date', required: true,
                 value: accToday || new Date().toISOString().slice(0, 10),
                 hint: 'Начальные остатки счетов ставятся на эту дату' }],
      submitLabel: 'Включить',
      onSubmit: async (data, { showErr }) => {
        const res = await apiResult('/api/acc/settings', { enabled: true, start_date: data.start_date });
        if (!res.ok) { showErr(res.error); return false; }
        currentUser.accounting_enabled = true;
        haptic('success');
        toast('Бухгалтерия включена — заведите счета');
        moneyTab = 'ops';
        accView = 'accounts';
        showScreen('money');
        return true;
      },
    });
  });
}

// ─── Вкладка «Касса» при включённой бухгалтерии ─────────────────────────────

function accViewSegHtml() {
  const items = [['now', 'Сейчас'], ['journal', 'Операции'], ['more', 'Ещё']];
  const active = accView === 'accounts' ? 'now' : accView;
  return `<div class="seg-row"><div class="seg">${items.map(([k, l]) =>
    `<button class="seg-item ${active === k ? 'active' : ''}" data-acc-view="${k}" aria-pressed="${active === k}">${l}</button>`
  ).join('')}</div></div>`;
}

function accWireViewSeg(container) {
  container.querySelectorAll('[data-acc-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      accView = btn.dataset.accView;
      renderAccTab(container);
    });
  });
}

async function renderAccTab(container) {
  const gen = screenGen();
  const head = accViewSegHtml();
  container.innerHTML = head + skeleton('list', 3);
  accWireViewSeg(container);
  if (accView === 'more') {
    container.innerHTML = head + '<div id="acc-more"></div>';
    accWireViewSeg(container);
    await renderCashbox(container.querySelector('#acc-more'), 'ops');
    return;
  }
  if (accView === 'journal') return accRenderJournal(container, gen);
  if (accView === 'accounts') return accRenderAccounts(container, gen);
  return accRenderNow(container, gen);
}

async function accRenderNow(container, gen) {
  const head = accViewSegHtml();
  let data;
  try {
    data = await api('/api/acc/balances', {});
  } catch (e) {
    if (gen !== screenGen()) return;
    container.innerHTML = head + errorBox(e.message);
    accWireViewSeg(container);
    return;
  }
  if (gen !== screenGen()) return;
  accToday = data.today || accToday;
  const accounts = data.accounts || [];
  const manage = accCanManage();
  let html = head;
  if (!accounts.length) {
    html += emptyState({
      icon: 'wallet', title: 'Счетов пока нет',
      hint: manage ? 'Заведите кассу, банковский счёт и карты — деньги записываются на них.'
        : 'Счета заводит руководитель.',
    });
    if (manage) html += `<button class="btn-primary" id="acc-open-accounts">${icon('plus')} Завести счёт</button>`;
    container.innerHTML = html;
    accWireViewSeg(container);
    container.querySelector('#acc-open-accounts')?.addEventListener('click', () => { accView = 'accounts'; renderAccTab(container); });
    return;
  }
  const partial = data.partial ? ' <span class="money-placeholder">(часть без курса)</span>' : '';
  html += `
    <div class="c-actions c-actions--wrap">
      <button class="btn-primary" id="acc-receipt">${icon('cash')} Получил деньги</button>
      <button class="btn-secondary" id="acc-expense">${icon('trend-down')} Расход</button>
      <button class="btn-secondary" id="acc-transfer">${icon('return')} Перевод</button>
      <button class="btn-secondary" id="acc-close">${icon('check')} Закрыть день</button>
    </div>
    <div class="wh-total" id="acc-total">
      <span class="wh-total-label">Всего ≈</span>
      <span class="wh-total-sum">${escapeHtml(accMoney(data.total_base_cents, data.base_currency))}${partial}</span>
    </div>
    <div class="section-label">Где лежат деньги · остаток сейчас</div>
    <div class="c-surface c-surface--list">${accounts.map(a => {
      const moves = [];
      if (a.today_in_cents) moves.push('+' + accMoney(a.today_in_cents));
      if (a.today_out_cents) moves.push('−' + accMoney(a.today_out_cents));
      const today = moves.length ? `сегодня ${moves.join(' / ')}` : 'сегодня без движения';
      const closed = a.last_close_date === data.today ? ' · день закрыт' : '';
      return `
      <div class="c-row c-row--tap" data-acc-account="${a.id}" role="button" tabindex="0"
           ${a.balance_cents < 0 ? 'data-status="overdue"' : ''}>
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(a.name)}</div>
          <div class="card-row-sub">${escapeHtml(accAccountSub(a))}</div>
          <div class="card-row-sub">${escapeHtml(today + closed)}</div>
        </div>
        <div class="card-row-value">${escapeHtml(accMoney(a.balance_cents, a.currency))}</div>
      </div>`;
    }).join('')}</div>`;
  if (manage) {
    html += `<button class="btn-secondary" id="acc-open-accounts">${icon('list')} Список счетов и касс</button>`;
  }
  container.innerHTML = html;
  accWireViewSeg(container);
  const refresh = () => renderAccTab(container);
  container.querySelector('#acc-receipt').addEventListener('click', () => accOpenReceipt({ refresh }));
  container.querySelector('#acc-expense').addEventListener('click', () => accOpenExpense(accounts, refresh));
  container.querySelector('#acc-transfer').addEventListener('click', () => accOpenTransfer(accounts, refresh));
  container.querySelector('#acc-close').addEventListener('click', () => accOpenCloseDay(accounts, null, refresh));
  container.querySelector('#acc-open-accounts')?.addEventListener('click', () => { accView = 'accounts'; refresh(); });
  container.querySelectorAll('[data-acc-account]').forEach(row => {
    row.addEventListener('click', () => {
      haptic('light');
      accJournal = { ...accJournal, accountId: row.dataset.accAccount, period: 'today' };
      accView = 'journal';
      refresh();
    });
  });
}

// ─── Выбор счёта внутри формы ───────────────────────────────────────────────

// Поле `type: 'hidden'` у openMachineSheet держит значение (его читает общий
// values() и проверка «обязательно»), а поверх рисуется строка-кнопка: выбор
// идёт листом с поиском (openListPicker), как контрагент в накладной, — не
// нативным списком.
function accMountAccountField(sheetEl, key, accounts, opts) {
  opts = opts || {};
  const hidden = sheetEl.querySelector(`#ms-f-${key}`);
  if (!hidden) return null;
  const box = document.createElement('div');
  box.className = 'c-surface c-surface--list';
  hidden.after(box);
  const draw = () => {
    const a = accounts.find(x => String(x.id) === String(hidden.value));
    box.innerHTML = `
      <div class="c-row c-row--tap acc-account-pick" data-acc-field="${escapeHtml(key)}" role="button" tabindex="0">
        <div class="card-row-info">
          <div class="card-row-title">${a ? escapeHtml(a.name) : 'Выберите счёт'}</div>
          ${a ? `<div class="card-row-sub">${escapeHtml(accAccountSub(a))}</div>` : ''}
        </div>
        <div class="card-row-value">${a ? escapeHtml(a.currency) : ''}</div>
      </div>`;
    box.querySelector('.acc-account-pick').addEventListener('click', (ev) => {
      ev.preventDefault();
      openListPicker({
        title: opts.title || 'Счёт',
        items: accounts.map(x => ({ id: x.id, name: x.name, sub: accAccountSub(x) })),
        selectedId: hidden.value,
        emptyText: 'Счетов нет — их заводит руководитель',
        onPick: (item) => {
          hidden.value = String(item.id);
          draw();
          if (opts.onChange) opts.onChange(accounts.find(x => x.id === item.id));
        },
      });
    });
  };
  draw();
  return { get: () => accounts.find(x => String(x.id) === String(hidden.value)), redraw: draw };
}

function accSetFieldLabel(sheetEl, key, text) {
  const span = sheetEl.querySelector(`#ms-f-${key}`)?.closest('.c-field')?.querySelector('span');
  // Звёздочку обязательного поля ставит openMachineSheet — не теряем её.
  if (span) span.textContent = text + (span.textContent.trim().endsWith('*') ? ' *' : '');
}

function accFieldBox(sheetEl, key) {
  return sheetEl.querySelector(`#ms-f-${key}`)?.closest('.c-field');
}

// ─── «Получил деньги» ──────────────────────────────────────────────────────

async function accOpenReceipt({ orderId, dealId, refresh }) {
  let accounts, rates, targets;
  try {
    [accounts, rates, targets] = await Promise.all([
      api('/api/acc/accounts', {}).then(r => r.accounts || []),
      api('/api/acc/rates', {}),
      api('/api/acc/receipt_targets', {}),
    ]);
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (!accounts.length) {
    toast(accCanManage() ? 'Сначала заведите счёт или кассу — деньги должны куда-то лечь' : 'Счетов нет — их заводит руководитель', 'error');
    return;
  }
  const toTarget = (t) => t.order_id
    ? { kind: 'order', id: t.order_id, currency: t.currency, available_cents: t.claimable_cents,
        title: `Заказ #${t.order_id} · ${t.agent_name}` }
    : { kind: 'deal', id: t.deal_id, currency: t.currency, available_cents: t.remaining_cents,
        title: `${t.machine_name} · ${t.buyer_name}` };
  const all = [...(targets.orders || []), ...(targets.deals || [])];
  const found = orderId ? (targets.orders || []).find(t => t.order_id === orderId)
    : dealId ? (targets.deals || []).find(t => t.deal_id === dealId) : null;
  if (orderId || dealId) {
    if (!found) { toast('По этому долгу нечего принимать: всё оплачено или ждёт подтверждения', 'error'); return; }
    accReceiptForm(toTarget(found), accounts, rates, refresh);
    return;
  }
  if (!all.length) { toast('Открытых долгов нет', 'info'); return; }
  openListPicker({
    title: 'От кого деньги',
    hint: 'Долг по заказу или рассрочке',
    items: all.map((t, i) => {
      const x = toTarget(t);
      return { id: i, name: x.title, sub: `можно принять ${accMoney(x.available_cents, x.currency)}` };
    }),
    onPick: (item) => { setTimeout(() => accReceiptForm(toTarget(all[item.id]), accounts, rates, refresh), 0); },
  });
}

function accReceiptForm(target, accounts, rates, refresh) {
  const base = rates.base_currency || 'USD';
  const cbu = {};
  Object.keys(rates.rates || {}).forEach(c => { cbu[c] = rates.rates[c].cbu; });
  const byId = (id) => accounts.find(a => String(a.id) === String(id));
  const firstFor = accounts.find(a => a.currency === target.currency) || accounts[0];
  const lines = [{ accountId: firstFor.id, amount: '' }];
  const rateInput = {};           // валюта → введённый курс (строка)
  const key = idemKey();

  const sheet = openMachineSheet({
    title: 'Получил деньги',
    hint: `${target.title} · можно принять ${accMoney(target.available_cents, target.currency)}`,
    fields: [{ key: 'note', label: 'Примечание', type: 'textarea', placeholder: 'Необязательно' }],
    submitLabel: 'Записать',
    onSubmit: async (data, { showErr }) => {
      const pv = preview();
      if (!pv.valid) { showErr(pv.over ? 'Получено больше остатка — уменьшите сумму или поправьте курс' : 'Заполните суммы и курс'); return false; }
      const payload = {
        lines: lines.filter(l => accParseCents(l.amount)).map(l => ({ account_id: l.accountId, amount: l.amount })),
        rates: {}, note: data.note, idempotency_key: key,
      };
      accRatesNeeded(lineCurrencies(), target.currency, base).forEach(c => { payload.rates[c] = rateInput[c]; });
      if (target.kind === 'order') payload.order_id = target.id; else payload.deal_id = target.id;
      const res = await apiResult('/api/acc/receipt', payload);
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      const b = res.body;
      const done = b.payment_status === 'confirmed'
        ? (b.remaining_cents === 0 ? 'долг закрыт' : `осталось ${accMoney(b.remaining_cents, b.target_currency)}`)
        : b.payment_status === 'pending' ? 'ждёт подтверждения руководителя'
          : (b.deal_closed ? 'рассрочка закрыта' : 'зачтено в рассрочку');
      toast(`Записано: ${accMoney(b.credited_cents, b.target_currency)} · ${done}`);
      if (refresh) refresh();
      return true;
    },
  });
  const sheetEl = sheet.sheet;
  const noteField = accFieldBox(sheetEl, 'note');
  const block = document.createElement('div');
  block.className = 'acc-receipt';
  noteField.before(block);
  const submitBtn = sheetEl.querySelector('#ms-submit');

  function lineCurrencies() { return lines.map(l => (byId(l.accountId) || {}).currency).filter(Boolean); }
  function quotes() {
    const q = {};
    Object.keys(cbu).forEach(c => { q[c] = accParseRate(rateInput[c] != null ? rateInput[c] : cbu[c]); });
    return q;
  }
  function preview() {
    return accReceiptPreview(
      lines.map(l => ({ cents: accParseCents(l.amount), currency: (byId(l.accountId) || {}).currency })),
      target, quotes(), base,
    );
  }
  function drawTotal() {
    const box = block.querySelector('#acc-total');
    if (!box) return;
    const pv = preview();
    let sub;
    if (pv.missingRate) sub = 'Укажите курс';
    else if (pv.over) sub = `Больше остатка на ${accMoney(pv.total - target.available_cents, target.currency)}`;
    else sub = `Останется: ${accMoney(pv.left, target.currency)}`;
    box.innerHTML = `
      <span class="wh-total-label">Зачтётся в долг<br><span class="c-field-hint${pv.over || pv.missingRate ? ' acc-warn' : ''}">${escapeHtml(sub)}</span></span>
      <span class="wh-total-sum">${escapeHtml(accMoney(pv.total, target.currency))}</span>`;
    submitBtn.disabled = !pv.valid;
  }
  function drawRates() {
    const box = block.querySelector('#acc-rates');
    const need = accRatesNeeded(lineCurrencies(), target.currency, base);
    box.innerHTML = need.map(c => {
      const val = rateInput[c] != null ? rateInput[c] : (cbu[c] || '');
      const manual = rateInput[c] != null && accParseRate(rateInput[c]) !== accParseRate(cbu[c]);
      return `
        <label class="c-field">
          <span>Курс: ${escapeHtml(c)} за 1 ${escapeHtml(base)}</span>
          <input id="acc-rate-${escapeHtml(c)}" data-acc-rate="${escapeHtml(c)}" type="text" inputmode="decimal" value="${escapeHtml(String(val))}">
          <span class="c-field-hint">${cbu[c] ? `ЦБ на сегодня: ${escapeHtml(String(cbu[c]))}` : 'Курса ЦБ нет — введите вручную'}${manual ? ' · свой курс' : ''}</span>
        </label>`;
    }).join('');
    box.querySelectorAll('[data-acc-rate]').forEach(inp => {
      inp.addEventListener('input', () => {
        rateInput[inp.dataset.accRate] = inp.value;
        const hint = inp.parentElement.querySelector('.c-field-hint');
        const c = inp.dataset.accRate;
        const manual = accParseRate(inp.value) !== accParseRate(cbu[c]);
        if (hint) hint.textContent = (cbu[c] ? `ЦБ на сегодня: ${cbu[c]}` : 'Курса ЦБ нет — введите вручную') + (manual ? ' · свой курс' : '');
        drawTotal();
      });
    });
  }
  function drawLines() {
    block.innerHTML = `
      <div class="section-label">Куда пришли деньги</div>
      <div class="c-surface c-surface--list" id="acc-lines">${lines.map((l, i) => {
        const a = byId(l.accountId);
        return `
        <div class="c-row acc-line" data-line="${i}">
          <div class="card-row-info acc-line-account" role="button" tabindex="0">
            <div class="card-row-title">${a ? escapeHtml(a.name) : 'Выберите счёт'}</div>
            <div class="card-row-sub">${a ? escapeHtml(accAccountSub(a)) : ''}</div>
          </div>
          <input class="acc-line-amount" type="text" inputmode="decimal" placeholder="Сумма"
                 aria-label="Сумма, ${a ? escapeHtml(a.currency) : ''}" value="${escapeHtml(l.amount)}">
          ${lines.length > 1 ? `<button type="button" class="pay-toggle acc-line-del" aria-label="Убрать строку">${icon('trash')}</button>` : ''}
        </div>`;
      }).join('')}</div>
      <button type="button" class="btn-secondary acc-add-line" id="acc-add-line">${icon('plus')} Ещё счёт</button>
      <div id="acc-rates"></div>
      <div class="wh-total" id="acc-total"></div>`;
    block.querySelectorAll('.acc-line').forEach(row => {
      const i = Number(row.dataset.line);
      row.querySelector('.acc-line-amount').addEventListener('input', (ev) => {
        lines[i].amount = ev.target.value;
        drawTotal();
      });
      row.querySelector('.acc-line-account').addEventListener('click', () => {
        openListPicker({
          title: 'Счёт',
          items: accounts.map(x => ({ id: x.id, name: x.name, sub: accAccountSub(x) })),
          selectedId: lines[i].accountId,
          onPick: (item) => { lines[i].accountId = item.id; drawLines(); },
        });
      });
      row.querySelector('.acc-line-del')?.addEventListener('click', () => {
        lines.splice(i, 1);
        drawLines();
      });
    });
    block.querySelector('#acc-add-line').addEventListener('click', () => {
      // Вторая строка — обычно другая валюта/карта: предлагаем первый счёт,
      // которого ещё нет в форме.
      const used = new Set(lines.map(l => String(l.accountId)));
      const next = accounts.find(a => !used.has(String(a.id))) || accounts[0];
      lines.push({ accountId: next.id, amount: '' });
      drawLines();
      const inputs = block.querySelectorAll('.acc-line-amount');
      inputs[inputs.length - 1]?.focus();
    });
    drawRates();
    drawTotal();
  }
  drawLines();
  block.querySelector('.acc-line-amount')?.focus();
}

// ─── Расход ────────────────────────────────────────────────────────────────

function accOpenExpense(accounts, refresh) {
  const key = idemKey();
  const first = accounts.find(a => a.kind === 'cash') || accounts[0];
  const sheet = openMachineSheet({
    title: 'Расход',
    hint: 'На что потратили — обязательно: категории нет, и без текста расход потом не объяснить.',
    fields: [
      { key: 'account_id', label: 'С какого счёта', type: 'hidden', required: true, value: first ? first.id : '' },
      { key: 'amount', label: `Сумма${first ? ', ' + first.currency : ''}`, type: 'number', required: true },
      { key: 'note', label: 'На что потратили', type: 'textarea', required: true, placeholder: 'Купил болты и скотч для склада' },
      { key: 'category', label: 'Категория (необязательно)', placeholder: 'Транспорт, хозтовары, питание…' },
      { key: 'doc_date', label: 'Дата', type: 'date', value: accToday || '' },
    ],
    submitLabel: 'Записать расход',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/acc/expense', { ...data, idempotency_key: key });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Расход записан');
      if (refresh) refresh();
      return true;
    },
  });
  accMountAccountField(sheet.sheet, 'account_id', accounts, {
    title: 'С какого счёта',
    onChange: (a) => accSetFieldLabel(sheet.sheet, 'amount', `Сумма, ${a.currency}`),
  });
}

// ─── Перевод / обмен ───────────────────────────────────────────────────────

function accOpenTransfer(accounts, refresh) {
  const key = idemKey();
  const sheet = openMachineSheet({
    title: 'Перевод между счетами',
    hint: 'Сдача наличных в кассу, снятие с карты, обмен валюты.',
    fields: [
      { key: 'from_account_id', label: 'Откуда', type: 'hidden', required: true },
      { key: 'to_account_id', label: 'Куда', type: 'hidden', required: true },
      { key: 'amount', label: 'Сумма', type: 'number', required: true },
      { key: 'amount_in', label: 'Получили', type: 'number' },
      { key: 'note', label: 'Примечание', type: 'textarea' },
      { key: 'doc_date', label: 'Дата', type: 'date', value: accToday || '' },
    ],
    submitLabel: 'Записать перевод',
    onSubmit: async (data, { showErr }) => {
      if (data.from_account_id === data.to_account_id) { showErr('Выберите разные счета: деньги нельзя перевести на тот же счёт'); return false; }
      const res = await apiResult('/api/acc/transfer', { ...data, idempotency_key: key });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast(res.body.kind === 'exchange' ? 'Обмен записан' : 'Перевод записан');
      if (refresh) refresh();
      return true;
    },
  });
  const el = sheet.sheet;
  const inBox = accFieldBox(el, 'amount_in');
  const sync = () => {
    const src = from.get();
    const dst = to.get();
    const exchange = src && dst && src.currency !== dst.currency;
    accSetFieldLabel(el, 'amount', exchange ? `Отдали, ${src.currency}` : `Сумма${src ? ', ' + src.currency : ''}`);
    accSetFieldLabel(el, 'amount_in', `Получили${dst ? ', ' + dst.currency : ''}`);
    if (inBox) inBox.classList.toggle('hidden', !exchange);  // .c-field задаёт display — атрибут hidden его не перебьёт
  };
  const from = accMountAccountField(el, 'from_account_id', accounts, { title: 'Откуда', onChange: sync });
  const to = accMountAccountField(el, 'to_account_id', accounts, { title: 'Куда', onChange: sync });
  sync();
}

// ─── Закрыть день ──────────────────────────────────────────────────────────

function accOpenCloseDay(accounts, accountId, refresh) {
  const key = idemKey();
  const first = (accountId && accounts.find(a => String(a.id) === String(accountId)))
    || accounts.find(a => a.kind === 'cash') || accounts[0];
  const sheet = openMachineSheet({
    title: 'Закрыть день',
    hint: 'Пересчитайте деньги и впишите, сколько есть на самом деле. Расхождение запишется сверкой.',
    fields: [
      { key: 'account_id', label: 'Касса / счёт', type: 'hidden', required: true, value: first ? first.id : '' },
      { key: 'counted', label: 'Пересчитали', type: 'number', required: true },
      { key: 'note', label: 'Примечание (обязательно при расхождении)', type: 'textarea' },
    ],
    submitLabel: 'Закрыть день',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/acc/close_day', { ...data, idempotency_key: key });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      const b = res.body;
      toast(b.diff_cents === 0 ? 'День закрыт — всё сошлось'
        : `День закрыт, расхождение ${b.diff_cents > 0 ? '+' : ''}${accMoney(b.diff_cents, b.currency)}`);
      if (refresh) refresh();
      return true;
    },
  });
  const el = sheet.sheet;
  const counted = el.querySelector('#ms-f-counted');
  const info = document.createElement('div');
  info.className = 'wh-total';
  info.id = 'acc-close-info';
  accFieldBox(el, 'note').before(info);
  const drawInfo = () => {
    const a = field.get();
    if (!a) { info.classList.add('hidden'); return; }
    info.classList.remove('hidden');
    const c = accParseCents(counted.value, true);
    const diff = c == null ? null : c - a.balance_cents;
    const sub = diff == null ? 'Впишите пересчитанную сумму'
      : diff === 0 ? 'Сходится' : `Разница: ${diff > 0 ? '+' : ''}${accMoney(diff, a.currency)}`;
    info.innerHTML = `
      <span class="wh-total-label">По записям<br><span class="c-field-hint${diff ? ' acc-warn' : ''}">${escapeHtml(sub)}</span></span>
      <span class="wh-total-sum">${escapeHtml(accMoney(a.balance_cents, a.currency))}</span>`;
  };
  const field = accMountAccountField(el, 'account_id', accounts, {
    title: 'Касса / счёт',
    onChange: (a) => { accSetFieldLabel(el, 'counted', `Пересчитали, ${a.currency}`); drawInfo(); },
  });
  if (first) accSetFieldLabel(el, 'counted', `Пересчитали, ${first.currency}`);
  counted.addEventListener('input', drawInfo);
  drawInfo();
}

// ─── Журнал операций ───────────────────────────────────────────────────────

async function accRenderJournal(container, gen) {
  const head = accViewSegHtml();
  const since = accPeriodSince(accJournal.period, accToday || new Date().toISOString().slice(0, 10));
  let data, accounts;
  try {
    [data, accounts] = await Promise.all([
      api('/api/acc/journal', { kind: accJournal.kind, since, account_id: accJournal.accountId || null }),
      api('/api/acc/accounts', {}).then(r => r.accounts || []),
    ]);
  } catch (e) {
    if (gen !== screenGen()) return;
    container.innerHTML = head + errorBox(e.message);
    accWireViewSeg(container);
    return;
  }
  if (gen !== screenGen()) return;
  accToday = data.today || accToday;
  const seg = (attr, items, active) => `<div class="seg-row${items.length > 3 ? ' scroll-hint' : ''}"><div class="seg${items.length > 3 ? ' seg--scroll' : ''}">${items.map(([k, l]) =>
    `<button class="seg-item ${active === k ? 'active' : ''}" ${attr}="${k}" aria-pressed="${active === k}">${l}</button>`).join('')}</div></div>`;
  const acc = accounts.find(a => String(a.id) === String(accJournal.accountId));
  let html = head
    + seg('data-acc-kind', [['', 'Все'], ['receipt', 'Приходы'], ['expense', 'Расходы'], ['transfer', 'Переводы'], ['reconcile', 'Сверки']], accJournal.kind)
    + seg('data-acc-period', [['today', 'Сегодня'], ['week', 'Неделя'], ['month', 'Месяц'], ['all', 'Всё']], accJournal.period)
    + `<div class="c-surface c-surface--list"><div class="c-row c-row--tap" id="acc-filter-account" role="button" tabindex="0">
         <div class="card-row-info"><div class="card-row-sub">Счёт</div>
         <div class="card-row-title">${acc ? escapeHtml(acc.name) : 'Все счета'}</div></div>
       </div></div>`;
  const docs = data.docs || [];
  if (!docs.length) {
    html += emptyState({ icon: 'list', title: 'Операций нет', hint: 'За выбранный период и фильтр записей нет.' });
  } else {
    html += '<div class="c-surface c-surface--list">' + docs.map(d => {
      const st = d.state === 'posted' ? ACC_KIND_STATUS[d.kind] : (d.state === 'payment_pending' ? 'pending' : 'archived');
      const who = [d.counterparty, d.note].filter(Boolean).join(' · ');
      const state = ACC_STATE_LABEL[d.state] ? ` · ${ACC_STATE_LABEL[d.state]}` : '';
      return `
        <div class="c-row c-row--tap" data-acc-doc="${d.id}" data-status="${st}" role="button" tabindex="0">
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(ACC_KIND_LABEL[d.kind] || d.kind)}${who ? ' · ' + escapeHtml(who) : ''}</div>
            <div class="card-row-sub">${escapeHtml(accDocDetailsLabel(d))}</div>
            <div class="card-row-sub">${escapeHtml(formatDateRU(d.doc_date))} · №${d.id} · ${escapeHtml(d.created_by_name)}${escapeHtml(state)}</div>
          </div>
          <div class="card-row-value">${escapeHtml(accDocAmountLabel(d))}</div>
        </div>`;
    }).join('') + '</div>';
  }
  container.innerHTML = html;
  accWireViewSeg(container);
  const rerender = () => renderAccTab(container);
  container.querySelectorAll('[data-acc-kind]').forEach(b => b.addEventListener('click', () => { accJournal.kind = b.dataset.accKind; rerender(); }));
  container.querySelectorAll('[data-acc-period]').forEach(b => b.addEventListener('click', () => { accJournal.period = b.dataset.accPeriod; rerender(); }));
  container.querySelector('#acc-filter-account').addEventListener('click', () => {
    openListPicker({
      title: 'Счёт',
      items: [{ id: '', name: 'Все счета' }, ...accounts.map(a => ({ id: a.id, name: a.name, sub: accAccountSub(a) }))],
      selectedId: accJournal.accountId,
      onPick: (item) => { accJournal.accountId = item.id ? String(item.id) : ''; rerender(); },
    });
  });
  container.querySelectorAll('[data-acc-doc]').forEach(row => {
    row.addEventListener('click', () => { haptic('light'); accOpenDoc(Number(row.dataset.accDoc), rerender); });
  });
}

async function accOpenDoc(docId, refresh) {
  let d;
  try { d = await api('/api/acc/doc', { doc_id: docId }); } catch (e) { toast(e.message, 'error'); return; }
  const rateLabel = (e) => {
    if (e.rate_source === 'base') return '';
    const src = e.rate_source === 'manual' ? 'свой курс' : 'курс ЦБ';
    return ` · ${src} ${e.rate}${e.rate_source === 'manual' && e.cbu_rate ? ` (ЦБ ${e.cbu_rate})` : ''}`;
  };
  const rows = (d.entries || []).map(e => `
    <div class="c-row">
      <div class="card-row-info">
        <div class="card-row-title">${escapeHtml(e.account_name)}</div>
        <div class="card-row-sub">${e.direction === 'in' ? 'приход' : 'расход'}${escapeHtml(rateLabel(e))}</div>
      </div>
      <div class="card-row-value">${e.direction === 'in' ? '+' : '−'}${escapeHtml(accMoney(e.amount_cents, e.currency))}</div>
    </div>`).join('');
  const facts = [];
  if (d.order_id) facts.push(`Заказ #${d.order_id}`);
  if (d.counterparty) facts.push(d.counterparty);
  if (d.target_cents != null && d.kind === 'receipt') facts.push(`зачтено ${accMoney(d.target_cents, d.target_currency)}`);
  if (d.close) facts.push(`по записям ${accMoney(d.close.expected_cents, d.target_currency)}, пересчёт ${accMoney(d.close.counted_cents, d.target_currency)}`);
  if (d.category) facts.push(d.category);
  if (ACC_STATE_LABEL[d.state]) facts.push(ACC_STATE_LABEL[d.state]);
  const sheet = openMachineSheet({
    title: `${ACC_KIND_LABEL[d.kind] || d.kind} №${d.id}`,
    hint: `${formatDateRU(d.doc_date)} · ${d.created_by_name}`,
    fields: [],
    submitLabel: 'Закрыть',
    onSubmit: async () => true,
  });
  const box = document.createElement('div');
  box.innerHTML = `
    ${facts.length ? `<div class="c-field-hint">${escapeHtml(facts.join(' · '))}</div>` : ''}
    ${d.note ? `<div class="section-label">Примечание</div><div class="c-surface c-surface--pad">${escapeHtml(d.note)}</div>` : ''}
    ${rows ? `<div class="section-label">Движения</div><div class="c-surface c-surface--list">${rows}</div>` : ''}
    ${d.state === 'void' ? `<div class="c-error">Отменена: ${escapeHtml(d.void_reason)} (${escapeHtml(d.voided_by_name)})</div>` : ''}`;
  sheet.sheet.querySelector('#ms-error').before(box);
  // «Отмена» у просмотра читалась бы рядом с «Отменить запись» как одно и то
  // же действие — оставляем «Закрыть» и отдельную красную «Отменить запись».
  const cancel = sheet.sheet.querySelector('#ms-cancel');
  if (cancel) cancel.classList.add('hidden');
  if (d.can_void) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-secondary btn-danger';
    btn.id = 'acc-void';
    btn.textContent = 'Отменить запись';
    sheet.sheet.querySelector('.c-actions').appendChild(btn);
    btn.addEventListener('click', () => { sheet.close(); accOpenVoid(d, refresh); });
  }
}

function accOpenVoid(d, refresh) {
  openMachineSheet({
    title: `Отменить №${d.id}`,
    hint: d.payment_id ? 'Платёж по заказу тоже будет отклонён, долг вернётся.' : 'Запись останется в журнале с пометкой «отменена».',
    fields: [{ key: 'reason', label: 'Причина', type: 'textarea', required: true }],
    submitLabel: 'Отменить запись',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/acc/void', { doc_id: d.id, reason: data.reason });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Запись отменена');
      if (refresh) refresh();
      return true;
    },
  });
}

// ─── Справочник счетов (руководитель) ──────────────────────────────────────

async function accRenderAccounts(container, gen) {
  const head = accViewSegHtml();
  let data;
  try {
    data = await api('/api/acc/accounts', { include_archived: true });
  } catch (e) {
    if (gen !== screenGen()) return;
    container.innerHTML = head + errorBox(e.message);
    accWireViewSeg(container);
    return;
  }
  if (gen !== screenGen()) return;
  const accounts = data.accounts || [];
  const back = () => { accView = 'now'; renderAccTab(container); };
  let html = head + `
    <div class="section-label">Список счетов и касс</div>`;
  html += accounts.length
    ? '<div class="c-surface c-surface--list">' + accounts.map(a => `
      <div class="c-row c-row--tap" data-acc-edit="${a.id}" role="button" tabindex="0"${a.archived ? ' data-status="archived"' : ''}>
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(a.name)}${a.archived ? ' · в архиве' : ''}</div>
          <div class="card-row-sub">${escapeHtml(accAccountSub(a))}</div>
        </div>
        <div class="card-row-value">${a.opening_cents ? escapeHtml(accMoney(a.opening_cents, a.currency)) : ''}</div>
      </div>`).join('') + '</div>'
    : emptyState({ icon: 'wallet', title: 'Счетов пока нет', hint: 'Касса, банковский счёт, карты — у каждого своя валюта.' });
  html += `
    <div class="c-actions c-actions--stack">
      <button class="btn-primary" id="acc-add-account">${icon('plus')} Добавить счёт</button>
      <button class="btn-secondary" id="acc-back-now">К остаткам</button>
      <button class="btn-secondary btn-danger" id="acc-disable">Выключить бухгалтерию</button>
    </div>`;
  container.innerHTML = html;
  accWireViewSeg(container);
  const rerender = () => renderAccTab(container);
  container.querySelector('#acc-add-account').addEventListener('click', () => accOpenAccountForm(null, data, rerender));
  container.querySelector('#acc-back-now').addEventListener('click', back);
  container.querySelectorAll('[data-acc-edit]').forEach(row => {
    row.addEventListener('click', () => {
      const a = accounts.find(x => String(x.id) === row.dataset.accEdit);
      if (a) accOpenAccountForm(a, data, rerender);
    });
  });
  container.querySelector('#acc-disable').addEventListener('click', async () => {
    if (!await confirmDialog('Выключить бухгалтерию? Записи сохранятся, новые экраны скроются.')) return;
    try {
      await api('/api/acc/settings', { enabled: false });
    } catch (e) { toast(e.message, 'error'); return; }
    currentUser.accounting_enabled = false;
    accView = 'now';
    toast('Бухгалтерия выключена', 'info');
    showScreen('money');
  });
}

function accOpenAccountForm(account, meta, refresh) {
  const a = account || {};
  const editing = !!account;
  const kinds = Object.entries(meta.kinds || { cash: 'Касса', bank: 'Банковский счёт', card: 'Карта' });
  const currencies = (meta.currencies || ['USD', 'UZS']).map(c => [c, c]);
  const sheet = openMachineSheet({
    title: editing ? 'Счёт' : 'Новый счёт',
    hint: editing ? 'Валюту счёта с операциями не меняют — заведите новый.' : 'У счёта одна валюта: наличные доллары и сумы — два счёта.',
    fields: [
      { key: 'name', label: 'Название', required: true, value: a.name || '', placeholder: 'Касса офиса, Humo Алишер' },
      { key: 'kind', label: 'Тип', type: 'select', options: kinds, value: a.kind || '' },
      { key: 'currency', label: 'Валюта', type: 'select', options: currencies, value: a.currency || '' },
      { key: 'bank', label: 'Банк / платёжная система', value: a.bank || '', placeholder: 'Uzcard, Humo, Visa, Капиталбанк' },
      { key: 'card_last4', label: 'Последние 4 цифры карты', value: a.card_last4 || '', placeholder: '1234' },
      { key: 'holder', label: 'У кого карта / чья касса', value: a.holder || '' },
      { key: 'opening', label: 'Начальный остаток', type: 'number', value: a.opening_cents ? String(a.opening_cents / 100) : '' },
      { key: 'opening_date', label: 'На дату', type: 'date', value: a.opening_date || '' },
    ],
    submitLabel: editing ? 'Сохранить' : 'Завести',
    onSubmit: async (data, { showErr }) => {
      const payload = { ...data };
      if (editing) payload.account_id = a.id;
      if (!payload.opening_date) delete payload.opening_date;
      const res = await apiResult('/api/acc/accounts/save', payload);
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast(editing ? 'Счёт сохранён' : 'Счёт заведён');
      if (refresh) refresh();
      return true;
    },
  });
  if (editing) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-secondary btn-danger';
    btn.id = 'acc-archive';
    btn.textContent = a.archived ? 'Вернуть из архива' : 'Убрать в архив';
    sheet.sheet.querySelector('.c-actions').appendChild(btn);
    btn.addEventListener('click', async () => {
      const res = await apiResult('/api/acc/accounts/archive', { account_id: a.id, archived: !a.archived });
      if (!res.ok) { sheet.showErr(res.error); return; }
      toast(a.archived ? 'Счёт возвращён' : 'Счёт в архиве');
      sheet.close();
      if (refresh) refresh();
    });
  }
}
