// «Как получены деньги»: разбивка оплаты заказа, сдача наличных по заказам,
// подписи ожидающих оплат. Сервер — services/order_payments.py
// (`/api/orders/payment_context`, `/api/orders/payment`, `/api/deposits/on_hand`).
//
// Отдельный файл, как accounting.js: подключается ПОСЛЕ app.js и пользуется его
// глобалами (api, apiResult, openMachineSheet, toast, haptic, idemKey, icon,
// escapeHtml, formatMoney). Точки входа из app.js — через
// `typeof payX === 'function'`.
//
// Поток (требование владельца): заказ «оплата сразу» одобрен → менеджер ПЕРЕД
// отгрузкой вносит строки «способ · валюта · сумма (· курс)», итог обязан
// совпасть с суммой к оплате → только тогда «Отгрузить». Наличные остаются у
// менеджера до сдачи в кассу, карта и перечисление ждут проверки банка.

// ─── Форма «Как получены деньги» ───────────────────────────────────────────

// ship — после записи сразу отгрузить (кнопка «Внести оплату и отгрузить»).
async function payOpenForm({ orderId, ship = false, onDone }) {
  let ctx;
  try {
    ctx = await api('/api/orders/payment_context', { order_id: orderId });
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  if (!ctx.open) { toast('Оплату вносят по одобренному, ещё не оплаченному заказу', 'error'); return; }
  if (!(ctx.due_cents > 0)) {
    if (ship) return payShip(orderId, onDone);
    toast('По заказу нечего вносить: всё оплачено или ждёт подтверждения', 'info');
    return;
  }
  const base = ctx.base_currency;
  const rows = [{ method: 'cash', currency: ctx.currency, amount: '', rate: '' }];
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
      const parts = rows.filter(r => payCents(r.amount)).map(r => {
        const out = { method: r.method, currency: r.currency, amount: r.amount };
        if (payRateCurrency(r.currency, ctx.currency, base) && r.rate) out.rate = r.rate;
        return out;
      });
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
    let sub;
    if (pv.missingRate) sub = 'Укажите курс';
    else if (pv.over) sub = `Больше нужного на ${payMoney(pv.total - ctx.due_cents, ctx.currency)}`;
    else if (pv.left > pv.tolerance) sub = `Осталось внести: ${payMoney(pv.left, ctx.currency)}`;
    else sub = 'Сумма сходится';
    box.innerHTML = `
      <span class="wh-total-label">Внесено из ${escapeHtml(payMoney(ctx.due_cents, ctx.currency))}<br><span class="c-field-hint${pv.over || pv.missingRate || pv.short ? ' acc-warn' : ''}">${escapeHtml(sub)}</span></span>
      <span class="wh-total-sum">${escapeHtml(payMoney(pv.total, ctx.currency))}</span>`;
    submitBtn.disabled = !pv.valid;
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
        <div class="pay-row">
          <input class="form-input pay-row-amount pay-part-amount" type="text" inputmode="decimal" autocomplete="off"
                 placeholder="Сумма" aria-label="Сумма, строка ${i + 1}" value="${escapeHtml(r.amount)}">
          ${seg(ctx.currencies.map(c => [c, c]), r.currency, 'data-pay-cur')}
          ${rows.length > 1 ? `<button type="button" class="pay-toggle pay-part-del" aria-label="Убрать строку">${icon('trash')}</button>` : ''}
        </div>
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
      <div class="c-field-hint">Наличные остаются у вас до сдачи в кассу. Карту и перечисление подтвердит руководитель или бухгалтер, сверив банк.</div>`;
    block.querySelectorAll('.pay-part').forEach(el => {
      const i = Number(el.dataset.part);
      el.querySelector('.pay-part-amount').addEventListener('input', ev => { rows[i].amount = ev.target.value; drawTotal(); });
      const rate = el.querySelector('.pay-part-rate');
      if (rate) {
        if (rows[i].rate === '') rows[i].rate = rate.value;
        rate.addEventListener('input', ev => { rows[i].rate = ev.target.value; drawTotal(); });
      }
      el.querySelectorAll('[data-pay-method]').forEach(b => b.addEventListener('click', () => {
        haptic('light'); rows[i].method = b.dataset.payMethod; draw();
      }));
      el.querySelectorAll('[data-pay-cur]').forEach(b => b.addEventListener('click', () => {
        haptic('light'); rows[i].currency = b.dataset.payCur; rows[i].rate = ''; draw();
      }));
      el.querySelector('.pay-part-del')?.addEventListener('click', () => { rows.splice(i, 1); draw(); });
    });
    block.querySelector('.pay-add-part').addEventListener('click', () => {
      // Вторая строка — обычно другой способ: карта после наличных.
      const pv = payPreview(rows, ctx);
      rows.push({ method: rows.length ? 'card' : 'cash', currency: ctx.currency,
        amount: pv.left > 0 ? String(pv.left / 100) : '', rate: '' });
      draw();
    });
    drawTotal();
  }
  // Одна строка на всю сумму — самый частый случай: предзаполняем.
  rows[0].amount = String(ctx.due_cents / 100);
  draw();
}

async function payShip(orderId, onDone) {
  const res = await apiResult('/api/orders/ship', { order_id: orderId, idempotency_key: idemKey() });
  if (res.ok) {
    haptic('success');
    tg.showAlert(`🚚 Заказ #${orderId} отгружен`);
  } else {
    tg.showAlert('❌ ' + res.error);
  }
  if (onDone) onDone();
}

// Отгрузка из списка: сервер отказал «сначала оплата» — открываем форму.
async function payShipOrOpenForm(orderId, onDone) {
  const res = await apiResult('/api/orders/ship', { order_id: orderId, idempotency_key: idemKey() });
  if (res.ok) {
    haptic('success');
    tg.showAlert(`🚚 Заказ #${orderId} отгружен`);
    if (onDone) onDone();
    return;
  }
  if (res.body && res.body.code === 'payment_required') {
    return payOpenForm({ orderId, ship: true, onDone });
  }
  tg.showAlert('❌ ' + res.error);
}
