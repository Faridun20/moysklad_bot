// Ежедневная сверка кассы: пересчитали наличные руками — записали, что вышло.
// Сервер — services/cash_reconciliation.py (`/api/cash/reconcile*`).
//
// Отдельный файл, как accounting.js и payments.js: подключается ПОСЛЕ app.js и
// пользуется его глобалами (api, apiResult, toast, haptic, idemKey, icon,
// escapeHtml, loading, errorBoxHtml, screenGen) и чистыми хелперами из
// helpers.js (reconLines, reconPreview, reconDiffLabel, reconHistoryHtml).
// Точка входа из app.js — `typeof reconRenderTab === 'function'`.
//
// Почему экран такой:
// * рядом с полем ввода СРАЗУ стоит «по системе» — человек сверяет два числа,
//   а не вспоминает, сколько должно быть;
// * разница считается на каждый ввод: зелёная, когда сходится, тревожная —
//   когда нет, и с точной суммой расхождения, а не «есть расхождение»;
// * записать можно и когда сошлось — «записан пересчёт, расхождений нет» это
//   тоже результат, и без него не отличить «сверили» от «не сверяли»;
// * примечание — ТОЛЬКО пояснение. Оно ничего не исправляет и платежа не
//   создаёт: иначе недостачу можно было бы объяснить текстом самому себе.

let reconCtx = null;
let reconInput = {};
let reconNote = '';
let reconHistoryOnlyDiff = false;
let reconKey = null;

async function reconRenderTab(container) {
  const gen = screenGen();
  container.innerHTML = loading('Загрузка сверки…');
  let ctx;
  let history;
  try {
    [ctx, history] = await Promise.all([
      api('/api/cash/reconcile/context', {}),
      api('/api/cash/reconcile/history', { only_diff: reconHistoryOnlyDiff }),
    ]);
  } catch (e) {
    container.innerHTML = errorBoxHtml(e.message, { retryAttr: 'data-recon-retry="1"' });
    container.querySelector('[data-recon-retry]')
      ?.addEventListener('click', () => reconRenderTab(container));
    return;
  }
  if (gen !== screenGen()) return;
  reconCtx = ctx;
  reconInput = {};
  reconNote = '';
  reconKey = idemKey();
  reconDraw(container, history);
}

function reconSystemFor(cur) {
  const row = ((reconCtx && reconCtx.system) || []).find(s => s.currency === cur);
  return row ? Number(row.amount_cents) || 0 : 0;
}

function reconFormHtml() {
  const curs = (reconCtx && reconCtx.currencies) || [];
  const done = reconCtx && reconCtx.done_today;
  return `
    <div class="section-label">${icon('cash')} Сверка наличных за ${escapeHtml(formatDateRU(reconCtx.date))}</div>
    <div class="c-surface c-surface--pad recon-form">
      <div class="debt-hint">Пересчитайте наличные, которые сейчас у вас на руках, и впишите, сколько получилось. Записываем и когда всё сходится.</div>
      ${curs.map(c => `
        <div class="recon-cur" data-recon-cur="${escapeHtml(c)}">
          <label class="c-field">
            <span>Пересчитано, ${escapeHtml(c)}</span>
            <input class="form-input recon-amount" type="text" inputmode="decimal" autocomplete="off"
                   aria-label="Пересчитано, ${escapeHtml(c)}" placeholder="0">
            <span class="c-field-hint">По системе: ${escapeHtml(payMoney(reconSystemFor(c), c))}</span>
          </label>
          <div class="recon-diff" data-recon-diff="${escapeHtml(c)}"></div>
        </div>`).join('')}
      <label class="c-field">
        <span>Примечание (если разошлось)</span>
        <input class="form-input" id="recon-note" type="text" autocomplete="off"
               placeholder="Забыл занести оплату вчера" maxlength="500">
        <span class="c-field-hint">Пояснение для руководителя. Платёж оно не создаёт — расхождение остаётся расхождением.</span>
      </label>
      <button id="recon-submit" class="btn-primary" disabled>${icon('check')} Записать сверку</button>
      <div class="debt-hint recon-status">${done ? 'Сегодня сверку уже записывали — можно пересчитать ещё раз.' : 'Сверку за сегодня ещё не записывали.'}</div>
    </div>`;
}

function reconDraw(container, history) {
  const canSeeAll = !!(reconCtx && reconCtx.can_see_all);
  container.innerHTML = reconFormHtml() + `
    <div class="section-label">${icon('list')} ${canSeeAll ? 'Сверки' : 'Мои сверки'}</div>
    ${canSeeAll ? `
      <div class="seg-row"><div class="seg recon-filter">
        <button type="button" class="seg-item ${reconHistoryOnlyDiff ? '' : 'active'}" data-recon-filter="" aria-pressed="${!reconHistoryOnlyDiff}">Все</button>
        <button type="button" class="seg-item ${reconHistoryOnlyDiff ? 'active' : ''}" data-recon-filter="diff" aria-pressed="${reconHistoryOnlyDiff}">Только расхождения</button>
      </div></div>` : ''}
    <div id="recon-history">${reconHistoryHtml((history || {}).items, {
      showWho: canSeeAll,
      emptyText: reconHistoryOnlyDiff ? 'Расхождений нет — все сверки сошлись.'
        : (canSeeAll ? 'Сверок пока нет.' : 'Вы ещё не записывали сверки.'),
    })}</div>`;
  reconWire(container);
}

function reconWire(container) {
  container.querySelectorAll('.recon-cur').forEach(box => {
    const cur = box.dataset.reconCur;
    const input = box.querySelector('.recon-amount');
    input.addEventListener('input', () => {
      reconInput[cur] = input.value;
      reconUpdate(container);
    });
  });
  const note = container.querySelector('#recon-note');
  if (note) note.addEventListener('input', () => { reconNote = note.value; });
  container.querySelectorAll('[data-recon-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      reconHistoryOnlyDiff = btn.dataset.reconFilter === 'diff';
      reconRenderTab(container);
    });
  });
  const submit = container.querySelector('#recon-submit');
  if (submit) submit.addEventListener('click', () => reconSubmit(container, submit));
  reconUpdate(container);
}

function reconUpdate(container) {
  const lines = reconLines(reconInput, (reconCtx && reconCtx.system) || []);
  const byCur = {};
  lines.forEach(l => { byCur[l.currency] = l; });
  container.querySelectorAll('[data-recon-diff]').forEach(box => {
    const l = byCur[box.dataset.reconDiff];
    if (!l) { box.textContent = ''; box.className = 'recon-diff'; return; }
    if (l.invalid) {
      box.className = 'recon-diff recon-warn';
      box.textContent = 'Впишите сумму числом — например 1 500 или 12,50';
      return;
    }
    box.className = 'recon-diff ' + reconDiffClass(l.diff_cents);
    box.textContent = reconDiffLabel(l.diff_cents, l.currency);
  });
  const pv = reconPreview(lines);
  const submit = container.querySelector('#recon-submit');
  if (submit) submit.disabled = !pv.valid;
}

async function reconSubmit(container, btn) {
  const lines = reconLines(reconInput, (reconCtx && reconCtx.system) || []);
  const pv = reconPreview(lines);
  if (!pv.valid) {
    toast(pv.invalid ? 'Впишите сумму числом — например 1 500 или 12,50'
      : 'Впишите пересчитанную сумму хотя бы по одной валюте', 'error');
    return;
  }
  const counts = lines.map(l => ({ currency: l.currency, amount: String(l.counted_cents / 100) }));
  btn.disabled = true;
  const res = await apiResult('/api/cash/reconcile', {
    counts, note: reconNote, idempotency_key: reconKey,
  });
  btn.disabled = false;
  if (!res.ok) { toast(res.error, 'error'); return; }
  haptic('success');
  // Сошлось — обычный тост, разошлось — тревожный и с точной суммой: это не
  // ошибка ввода, а факт, который человек должен унести с экрана.
  toast(res.body.message, res.body.matched ? 'success' : 'error');
  await reconRenderTab(container);
}
