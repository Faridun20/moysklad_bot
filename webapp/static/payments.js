// «Как получены деньги»: разбивка оплаты заказа, сдача наличных по заказам,
// подписи ожидающих оплат. Сервер — services/order_payments.py
// (`/api/orders/payment_context`, `/api/orders/payment`, `/api/deposits/on_hand`).
//
// Отдельный файл, как accounting.js: подключается ПОСЛЕ app.js и пользуется его
// глобалами (api, apiResult, openMachineSheet, toast, haptic, idemKey, icon,
// escapeHtml, formatMoney). Точки входа из app.js — через
// `typeof payX === 'function'`.
//
// Поток (требование владельца): заказ «оплата сразу» менеджер ПЕРЕД отгрузкой
// вносит строками «способ · валюта · сумма (· курс)», итог обязан совпасть с
// суммой к оплате → только тогда отгрузка. Одобрение отгрузки не обязательно:
// у черновика и заявки без решения (`ctx.with_shipment`) оплата и отгрузка
// уходят ОДНИМ запросом `/api/orders/ship` с `parts` — сервер записывает деньги
// только вместе с оформлением отгрузки. Наличные остаются у менеджера до сдачи в
// кассу, карта и перечисление ждут проверки банка.
//
// Карта и перечисление указывают, КУДА пришли деньги: карта (последние 4 цифры
// и владелец) или расчётный счёт (фирма и номер) из справочника «Карты и счета»
// (services/pay_accounts.py, `/api/pay_accounts*`). Выбор — лист с поиском
// (openListPicker), новую запись заводят тут же кнопкой под списком. По
// умолчанию предлагается последняя выбранная человеком карта/счёт.

// ─── Форма «Как получены деньги» ───────────────────────────────────────────

// ship — после записи сразу отгрузить (кнопка «Внести оплату и отгрузить»).
// terms — условия оплаты черновика из редактора ({payment_type, due_date}).
async function payOpenForm({ orderId, ship = false, terms = null, onDone }) {
  let ctx;
  try {
    ctx = await api('/api/orders/payment_context', { order_id: orderId });
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (!ctx.open) { toast('Оплату вносят по заказу, который ещё не отгружен или не оплачен полностью', 'error'); return; }
  if (!(ctx.due_cents > 0)) {
    if (ship) return payShip(orderId, onDone, terms);
    toast('По заказу нечего вносить: всё оплачено или ждёт подтверждения', 'info');
    return;
  }
  const base = ctx.base_currency;
  payAccountsRemember(ctx.pay_accounts);
  const rows = [{ method: 'cash', currency: ctx.currency, amount: '', rate: '', account_id: '' }];
  const key = idemKey();
  const title = ship ? 'Оплата перед отгрузкой' : 'Как получены деньги';
  const hint = `Заказ #${ctx.order_id}${ctx.agent_name ? ' · ' + ctx.agent_name : ''} · `
    + (ctx.exact ? `оплата сразу, к оплате ${payMoney(ctx.due_cents, ctx.currency)}`
      : `в долг, остаток ${payMoney(ctx.due_cents, ctx.currency)}`);

  const sheet = openMachineSheet({
    title, hint, fields: [],
    submitLabel: ship ? 'Записать и отгрузить' : 'Записать оплату',
    onSubmit: async (_data, { showErr }) => {
      const pv = payPreview(rows, ctx);
      if (!pv.valid) {
        showErr(pv.missingRate ? 'Укажите курс'
          : pv.over ? 'Введено больше, чем нужно'
          : pv.short ? `Не хватает ${payMoney(pv.left, ctx.currency)}: при оплате сразу сумма должна совпасть`
          : 'Заполните суммы');
        return false;
      }
      const missing = payMissingAccount(rows);
      if (missing >= 0) { showErr(payMissingAccountText(rows, missing)); return false; }
      const parts = rows.filter(r => payCents(r.amount)).map(r => {
        const out = { method: r.method, currency: r.currency, amount: r.amount };
        if (payRateCurrency(r.currency, ctx.currency, base) && r.rate) out.rate = r.rate;
        if (r.method !== 'cash' && r.account_id) out.account_id = Number(r.account_id);
        return out;
      });
      if (ship && ctx.with_shipment) {
        // Черновик или заявка без решения: оплата и отгрузка — одним запросом,
        // сервер запишет деньги только вместе с оформлением отгрузки.
        const shipped = await apiResult('/api/orders/ship', {
          order_id: ctx.order_id, parts, idempotency_key: key, ...(terms || {}),
        });
        if (!shipped.ok) { showErr(shipped.error); return false; }
        haptic('success');
        sheet.close();
        tg.showAlert(`Заказ #${ctx.order_id} отгружен`);
        if (onDone) await onDone();
        return false;
      }
      const res = await apiResult('/api/orders/payment', { order_id: ctx.order_id, parts, idempotency_key: key });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      if (!ship) {
        toast(`Оплата ${payMoney(res.body.total_cents, ctx.currency)} записана`);
        if (onDone) onDone();
        return true;
      }
      sheet.close();
      await payShip(ctx.order_id, onDone);
      return false;
    },
  });
  const sheetEl = sheet.sheet;
  const block = document.createElement('div');
  block.className = 'acc-receipt pay-parts';
  sheetEl.querySelector('#ms-error').before(block);
  const submitBtn = sheetEl.querySelector('#ms-submit');

  function drawTotal() {
    const box = block.querySelector('.pay-total');
    const pv = payPreview(rows, ctx);
    const missing = payMissingAccount(rows);
    let sub;
    if (pv.missingRate) sub = 'Укажите курс';
    else if (pv.over) sub = `Больше нужного на ${payMoney(pv.total - ctx.due_cents, ctx.currency)}`;
    else if (missing >= 0) sub = payMissingAccountText(rows, missing);
    else if (pv.left > pv.tolerance) sub = `Осталось внести: ${payMoney(pv.left, ctx.currency)}`;
    else sub = 'Сумма сходится';
    const warn = pv.over || pv.missingRate || pv.short || missing >= 0;
    box.innerHTML = `
      <span class="wh-total-label">Внесено из ${escapeHtml(payMoney(ctx.due_cents, ctx.currency))}<br><span class="c-field-hint${warn ? ' acc-warn' : ''}">${escapeHtml(sub)}</span></span>
      <span class="wh-total-sum">${escapeHtml(payMoney(pv.total, ctx.currency))}</span>`;
    submitBtn.disabled = !pv.valid || missing >= 0;
  }

  function rowHtml(r, i) {
    const rc = payRateCurrency(r.currency, ctx.currency, base);
    const cbu = rc ? (ctx.cbu || {})[rc] : null;
    const seg = (items, value, attr) => `<div class="seg">${items.map(([v, l]) =>
      `<button type="button" class="seg-item ${v === value ? 'active' : ''}" ${attr}="${escapeHtml(v)}" aria-pressed="${v === value}">${escapeHtml(l)}</button>`
    ).join('')}</div>`;
    return `
      <div class="c-surface c-surface--pad pay-part" data-part="${i}">
        <div class="seg-row">${seg(PAY_METHODS, r.method, 'data-pay-method')}</div>
        <div class="seg-row">
          ${seg(ctx.currencies.map(c => [c, c]), r.currency, 'data-pay-cur')}
          ${rows.length > 1 ? `<button type="button" class="pay-toggle pay-part-del" aria-label="Убрать строку">${icon('trash')}</button>` : ''}
        </div>
        ${r.method !== 'cash' ? payAccountFieldHtml(payAccountFor(r.account_id, r.method), r.method, i + 1) : ''}
        <input class="form-input pay-part-amount" type="text" inputmode="decimal" autocomplete="off"
               placeholder="Сумма, ${escapeHtml(r.currency)}" aria-label="Сумма, строка ${i + 1}" value="${escapeHtml(r.amount)}">
        ${rc ? `
          <label class="c-field">
            <span>Курс: ${escapeHtml(rc)} за 1 ${escapeHtml(base)}</span>
            <input class="pay-part-rate" type="text" inputmode="decimal" value="${escapeHtml(r.rate !== '' ? r.rate : (cbu || ''))}">
            <span class="c-field-hint">${cbu ? `ЦБ на сегодня: ${escapeHtml(String(cbu))}` : 'Курса ЦБ нет — введите вручную'}</span>
          </label>` : ''}
      </div>`;
  }

  function draw() {
    block.innerHTML = `
      <div class="section-label">Как клиент заплатил</div>
      <div class="debts-list">${rows.map(rowHtml).join('')}</div>
      <button type="button" class="btn-secondary acc-add-line pay-add-part">${icon('plus')} Ещё способ или валюта</button>
      <div class="wh-total pay-total"></div>
      <div class="c-field-hint">Наличные остаются у вас до сдачи в кассу. Карту и перечисление подтвердит руководитель или бухгалтер, сверив банк по выбранной карте или счёту.</div>`;
    block.querySelectorAll('.pay-part').forEach(el => {
      const i = Number(el.dataset.part);
      el.querySelector('.pay-part-amount').addEventListener('input', ev => { rows[i].amount = ev.target.value; drawTotal(); });
      const rate = el.querySelector('.pay-part-rate');
      if (rate) {
        if (rows[i].rate === '') rows[i].rate = rate.value;
        rate.addEventListener('input', ev => { rows[i].rate = ev.target.value; drawTotal(); });
      }
      el.querySelectorAll('[data-pay-method]').forEach(b => b.addEventListener('click', () => {
        haptic('light'); payRowSetMethod(rows[i], b.dataset.payMethod); draw();
      }));
      payOnTap(el.querySelector('.pay-part-account'), () => payOpenAccountPicker({
        kind: rows[i].method, currency: rows[i].currency, currencies: ctx.currencies,
        selectedId: rows[i].account_id,
        onPick: (a) => { rows[i].account_id = a.id; draw(); },
      }));
      el.querySelectorAll('[data-pay-cur]').forEach(b => b.addEventListener('click', () => {
        haptic('light'); rows[i].currency = b.dataset.payCur; rows[i].rate = ''; draw();
      }));
      el.querySelector('.pay-part-del')?.addEventListener('click', () => { rows.splice(i, 1); draw(); });
    });
    block.querySelector('.pay-add-part').addEventListener('click', () => {
      // Вторая строка — обычно другой способ: карта после наличных.
      const pv = payPreview(rows, ctx);
      const row = { method: 'cash', currency: ctx.currency,
        amount: pv.left > 0 ? String(pv.left / 100) : '', rate: '', account_id: '' };
      payRowSetMethod(row, rows.length ? 'card' : 'cash');
      rows.push(row);
      draw();
    });
    drawTotal();
  }
  // Одна строка на всю сумму — самый частый случай: предзаполняем.
  rows[0].amount = String(ctx.due_cents / 100);
  draw();
}

async function payShip(orderId, onDone, terms) {
  const res = await apiResult('/api/orders/ship', { order_id: orderId, idempotency_key: idemKey(), ...(terms || {}) });
  if (res.ok) {
    haptic('success');
    tg.showAlert(`Заказ #${orderId} отгружен`);
  } else {
    tg.showAlert('' + res.error);
  }
  if (onDone) onDone();
}

// Отгрузка из списка: сервер отказал «сначала оплата» — открываем форму.
async function payShipOrOpenForm(orderId, onDone) {
  const res = await apiResult('/api/orders/ship', { order_id: orderId, idempotency_key: idemKey() });
  if (res.ok) {
    haptic('success');
    tg.showAlert(`Заказ #${orderId} отгружен`);
    if (onDone) onDone();
    return;
  }
  if (res.body && res.body.code === 'payment_required') {
    return payOpenForm({ orderId, ship: true, onDone });
  }
  tg.showAlert('' + res.error);
}

// ─── Куда поступили: карты и счета ─────────────────────────────────────────

// Справочник на сессию: приходит с контекстом формы (`pay_accounts`) или
// отдельной ручкой; заведённая/исправленная запись дописывается сюда же, иначе
// следующий выбор её не покажет.
let payAccountsState = null;

function payAccountsRemember(data) {
  if (data) payAccountsState = { accounts: [], last_used: {}, ...data, accounts: [...(data.accounts || [])] };
  return payAccountsState;
}

async function payAccountsLoad(opts) {
  const { force = false } = opts || {};
  if (!force && payAccountsState) return payAccountsState;
  return payAccountsRemember(await api('/api/pay_accounts', {}));
}

function payAccountsUpsert(account) {
  if (!account) return;
  if (!payAccountsState) payAccountsState = { accounts: [], last_used: {} };
  const list = payAccountsState.accounts;
  const at = list.findIndex(a => Number(a.id) === Number(account.id));
  if (at >= 0) list[at] = account; else list.push(account);
}

// Запись по id — только если того же вида, что способ строки.
function payAccountFor(id, kind) {
  if (!id || !payAccountsState) return null;
  const a = (payAccountsState.accounts || []).find(x => Number(x.id) === Number(id));
  return a && (!kind || a.kind === kind) ? a : null;
}

// Смена способа строки: у карты/счёта — последний выбор человека, у наличных «куда» нет.
function payRowSetMethod(row, method) {
  row.method = method;
  if (method === 'cash') { row.account_id = ''; return; }
  if (!payAccountFor(row.account_id, method)) row.account_id = payDefaultAccountId(payAccountsState, method) || '';
}

function payMissingAccountText(rows, index) {
  const r = rows[index] || {};
  const what = r.method === 'card' ? 'на какую карту' : 'на какой счёт';
  return `${rows.length > 1 ? `Строка ${index + 1}: в` : 'В'}ыберите, ${what} пришли деньги`;
}

function payOnTap(el, fn) {
  if (!el) return;
  el.addEventListener('click', (ev) => { ev.preventDefault(); haptic('light'); fn(); });
  el.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); fn(); }
  });
}

// Лист выбора карты/счёта. Новая запись — кнопкой под списком: карту находят
// ровно тогда, когда клиент на неё заплатил, и уводить из формы оплаты нельзя.
function payOpenAccountPicker({ kind, currency, currencies, selectedId, onPick }) {
  const st = payAccountsState || { accounts: [] };
  const meta = PAY_ACCOUNT_KIND[kind] || PAY_ACCOUNT_KIND.card;
  const reopen = () => payOpenAccountPicker({ kind, currency, currencies, selectedId, onPick });
  const sheet = openListPicker({
    title: meta.title,
    hint: kind === 'card' ? 'По этой карте руководитель сверит поступление' : 'По этому счёту руководитель сверит поступление',
    items: payAccountItems(st.accounts, kind, currency),
    selectedId,
    emptyText: meta.empty,
    onPick: (item) => onPick(item.account),
    addLabel: meta.add,
    onAdd: st.can_add === false ? null : (typed) => payOpenAccountForm({
      kind, currency, currencies, prefill: payAccountPrefill(kind, typed), onDone: onPick,
    }),
  });
  const ov = sheet.sheet;
  ov.classList.add('pay-account-picker');
  const input = ov.querySelector('#ms-f-search');
  if (input) input.placeholder = kind === 'card' ? 'Владелец или последние 4 цифры' : 'Фирма, банк или номер счёта';
  // Правка и архив: у руководства — «Настройки → Карты и счета», у менеджера
  // (когда руководителя нет) — отсюда же.
  if (st.can_manage && (st.accounts || []).some(a => a.kind === kind)) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-secondary picker-add pay-accounts-manage';
    btn.innerHTML = `${icon('edit')} Изменить или убрать в архив`;
    (ov.querySelector('.picker-add') || ov.querySelector('.picker-list')).after(btn);
    btn.addEventListener('click', () => { sheet.close(); payOpenAccountsManager({ onClose: reopen }); });
  }
  return sheet;
}

function payAccountFields(kind, account, prefill, currency, currencies) {
  const v = (k) => (account ? account[k] : prefill && prefill[k]) || '';
  if (kind === 'card') {
    const curs = (currencies && currencies.length ? currencies : [currency || 'USD']).map(c => [c, c]);
    return [
      { key: 'holder', label: 'Владелец карты', required: true, value: v('holder'), placeholder: 'Фаридун М.',
        autocomplete: 'off' },
      { key: 'card_last4', label: 'Последние 4 цифры карты', required: true, value: v('card_last4'),
        placeholder: '1234', inputmode: 'numeric', autocomplete: 'off',
        hint: 'Полный номер карты не храним — только последние 4 цифры' },
      { key: 'bank', label: 'Банк', value: v('bank'), placeholder: 'Kapitalbank, Humo, Uzcard' },
      { key: 'currency', label: 'Валюта карты', type: 'select',
        value: (account && account.currency) || currency || '', options: curs },
    ];
  }
  return [
    { key: 'holder', label: 'Фирма или владелец счёта', required: true, value: v('holder'),
      placeholder: 'ООО Farid Impeks' },
    { key: 'account_number', label: 'Номер расчётного счёта', required: true, value: v('account_number'),
      placeholder: '20 цифр', inputmode: 'numeric', maxlength: 24, autocomplete: 'off',
      hint: 'Валюта счёта — по коду в номере (000 — сумы, 840 — доллары)' },
    { key: 'bank', label: 'Банк', value: v('bank'), placeholder: 'Kapitalbank' },
    { key: 'mfo', label: 'МФО банка', value: v('mfo'), placeholder: '01158', inputmode: 'numeric', maxlength: 5 },
    { key: 'company_tin', label: 'ИНН', value: v('company_tin'), placeholder: '301234567', inputmode: 'numeric',
      maxlength: 14 },
  ];
}

// Новая карта/счёт или правка. Ошибка — внутри формы, набранное не теряется.
function payOpenAccountForm({ kind, account = null, currency, currencies, prefill, onDone }) {
  const edit = !!account;
  const key = idemKey();
  const meta = PAY_ACCOUNT_KIND[kind] || PAY_ACCOUNT_KIND.card;
  const sheet = openMachineSheet({
    title: edit ? (kind === 'card' ? 'Карта' : 'Расчётный счёт') : meta.add,
    hint: kind === 'card' ? 'Чья карта — по ней руководитель сверит поступление'
      : 'Чей счёт — по нему руководитель сверит поступление',
    fields: payAccountFields(kind, account, prefill, currency, currencies || (payAccountsState || {}).currencies),
    submitLabel: edit ? 'Сохранить' : 'Добавить',
    onSubmit: async (data, { showErr }) => {
      const err = payAccountFormError(kind, data);
      if (err) { showErr(err); return false; }
      const res = edit
        ? await apiResult('/api/pay_accounts/update', { account_id: account.id, ...data })
        : await apiResult('/api/pay_accounts/create', { kind, ...data, idempotency_key: key });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      payAccountsUpsert(res.body.account);
      toast(edit ? 'Сохранено'
        : res.body.existed ? (kind === 'card' ? 'Такая карта уже есть — выбрана она' : 'Такой счёт уже есть — выбран он')
        : (kind === 'card' ? 'Карта добавлена' : 'Счёт добавлен'));
      if (onDone) onDone(res.body.account);
      return true;
    },
  });
  sheet.sheet.classList.add('pay-account-form');
  if (edit && (payAccountsState || {}).can_manage) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn-secondary pay-account-archive';
    btn.textContent = account.archived ? 'Вернуть из архива' : 'Убрать в архив';
    sheet.sheet.querySelector('.c-actions').appendChild(btn);
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const res = await apiResult('/api/pay_accounts/archive', { account_id: account.id, archived: !account.archived });
      btn.disabled = false;
      if (!res.ok) { sheet.showErr(res.error); return; }
      haptic('success');
      payAccountsUpsert(res.body.account);
      toast(account.archived ? 'Возвращено из архива' : 'Убрано в архив — на старых платежах останется');
      sheet.close();
      if (onDone) onDone(res.body.account);
    });
  }
  return sheet;
}

function payWireAccountsManager(root, data, { redraw, toggleArchived }) {
  root.querySelectorAll('[data-pay-account-add]').forEach(b => payOnTap(b, () => payOpenAccountForm({
    kind: b.dataset.payAccountAdd, currencies: data.currencies, onDone: redraw,
  })));
  payOnTap(root.querySelector('[data-pay-accounts-archived]'), toggleArchived);
  if (!data.can_manage) return;
  root.querySelectorAll('[data-pay-account]').forEach(row => payOnTap(row, () => {
    const a = (data.accounts || []).find(x => Number(x.id) === Number(row.dataset.payAccount));
    if (a) payOpenAccountForm({ kind: a.kind, account: a, currencies: data.currencies, onDone: redraw });
  }));
}

// «Настройки → Карты и счета» (руководство): список, новая запись, правка, архив.
async function payRenderAccountsScreen(onBack) {
  const gen = screenGen();
  setScreenContext('Наши карты и счета');
  showBack(onBack || (() => showScreen('settings')));
  document.getElementById('content').innerHTML = skeleton('list', 3);
  let showArchived = false;
  let data = null;
  const paint = () => {
    const box = document.getElementById('content');
    box.innerHTML = `<div class="pay-accounts-screen">${payAccountsManagerHtml(data.accounts, {
      canManage: data.can_manage, canAdd: data.can_add, showArchived,
      hint: 'Сюда клиенты платят картой и перечислением. Архивная запись не предлагается при оплате, но остаётся на старых платежах.',
    })}</div>`;
    payWireAccountsManager(box, data, {
      redraw: load,
      toggleArchived: () => { showArchived = !showArchived; paint(); },
    });
  };
  async function load() {
    try {
      const got = await api('/api/pay_accounts', { include_archived: true });
      if (gen !== screenGen()) return;
      data = got;
      payAccountsRemember(got);
    } catch (e) {
      if (gen !== screenGen()) return;
      document.getElementById('content').innerHTML = errorBoxHtml(e.message);
      return;
    }
    paint();
  }
  await load();
}

// То же из листа выбора (менеджер без руководителя): шторка поверх формы.
async function payOpenAccountsManager({ onClose }) {
  let data;
  try {
    data = await api('/api/pay_accounts', { include_archived: true });
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  payAccountsRemember(data);
  let showArchived = false;
  const sheet = openMachineSheet({
    title: 'Наши карты и счета', hint: data.manage_hint || '', fields: [], submitLabel: 'Готово',
    onSubmit: async () => { if (onClose) setTimeout(onClose, 0); return true; },
  });
  const block = document.createElement('div');
  block.className = 'pay-accounts-sheet';
  sheet.sheet.querySelector('#ms-error').before(block);
  const paint = () => {
    block.innerHTML = payAccountsManagerHtml(data.accounts, { canManage: data.can_manage, canAdd: data.can_add, showArchived });
    payWireAccountsManager(block, data, {
      redraw: async () => {
        try { data = await api('/api/pay_accounts', { include_archived: true }); payAccountsRemember(data); } catch (_e) { /* покажем прежний список */ }
        paint();
      },
      toggleArchived: () => { showArchived = !showArchived; paint(); },
    });
  };
  paint();
}

// Строка «Куда поступили» в форме с сегментом способа (поступление по
// рассрочке): видна у карты и перечисления, значение — в скрытом поле формы.
async function payMountAccountField(sheetEl, { methodKey, key, currency }) {
  const hidden = sheetEl.querySelector(`#ms-f-${key}`);
  const method = sheetEl.querySelector(`#ms-f-${methodKey}`);
  if (!hidden || !method) return;
  const field = hidden.closest('.c-field');
  const box = document.createElement('div');
  hidden.after(box);
  if (field) field.hidden = true;
  try {
    await payAccountsLoad({ force: true });
  } catch (_e) {
    // Список не пришёл — выбор покажет пусто, сервер всё равно проверит.
  }
  const draw = () => {
    const m = method.value;
    const noncash = m === 'card' || m === 'bank';
    if (field) field.hidden = !noncash;
    if (!noncash) { hidden.value = ''; box.innerHTML = ''; return; }
    if (!payAccountFor(hidden.value, m)) hidden.value = String(payDefaultAccountId(payAccountsState, m) || '');
    box.innerHTML = payAccountFieldHtml(payAccountFor(hidden.value, m), m, 0);
    payOnTap(box.querySelector('.pay-part-account'), () => payOpenAccountPicker({
      kind: m, currency, currencies: (payAccountsState || {}).currencies, selectedId: hidden.value,
      onPick: (a) => { hidden.value = String(a.id); draw(); },
    }));
  };
  sheetEl.querySelectorAll('.seg-item[data-opt]').forEach(b => b.addEventListener('click', () => setTimeout(draw, 0)));
  draw();
}
