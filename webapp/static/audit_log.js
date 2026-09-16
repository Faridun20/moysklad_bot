// Журнал действий (C1): read-only лента аудита для руководства в WebApp.
//
// Раньше `audit_log` смотрели только через бот-команду `/audit`, и только
// admin — нанятый руководитель (boss, не в ADMIN_IDS) не видел вообще ничего.
// Экран — «Настройки → Журнал действий» (settings row, как «Карты и счета»),
// открыт admin и boss (webapp/server.py `/api/audit_log`,
// allowed_roles=("admin","boss")). Фильтр по дате (сегмент, как у заказов) и
// по сотруднику (лист-пикер — нативный `<select>` в Telegram-WebView
// разворачивается системным списком без подстрочника, см. app.js:2994).
//
// Загружен ПОСЛЕ app.js: пользуется его глобалами (api, icon, escapeHtml,
// skeleton, showBack, setScreenContext, screenGen, openListPicker,
// periodSegHtml, dateRangeHost, mountCalendar, emptyState, errorBoxHtml,
// haptic, _ymd).

let auditLogState = {
  entries: [], total: 0, hasMore: false, users: [],
  period: 'all', from: '', to: '', userId: '',
};

const AUDIT_LOG_PAGE = 50;

function _auditPeriodRange(period) {
  const today = new Date();
  if (period === 'custom') return { date_from: auditLogState.from || '', date_to: auditLogState.to || '' };
  if (period === 'today') return { date_from: _ymd(today), date_to: _ymd(today) };
  if (period === '7d' || period === '30d') {
    const cutoff = new Date(today);
    cutoff.setDate(today.getDate() - ((period === '7d' ? 7 : 30) - 1));
    return { date_from: _ymd(cutoff), date_to: '' };
  }
  return { date_from: '', date_to: '' };
}

async function renderAuditLogScreen(onBack) {
  const gen = screenGen();
  setScreenContext('Журнал действий');
  showBack(onBack || (() => showScreen('settings')));
  document.getElementById('content').innerHTML = skeleton('list', 4);
  auditLogState = {
    entries: [], total: 0, hasMore: false, users: [],
    period: 'all', from: '', to: '', userId: '',
  };
  await auditLogLoad(gen, false);
}

async function auditLogLoad(gen, append) {
  const box = document.getElementById('content');
  const s = auditLogState;
  const range = _auditPeriodRange(s.period);
  const body = {
    limit: AUDIT_LOG_PAGE,
    offset: append ? s.entries.length : 0,
    date_from: range.date_from,
    date_to: range.date_to,
  };
  if (s.userId) body.user_id = Number(s.userId);
  try {
    const data = await api('/api/audit_log', body);
    if (gen !== screenGen()) return;
    s.entries = append ? [...s.entries, ...data.entries] : data.entries;
    s.total = data.total;
    s.hasMore = !!data.has_more;
    if (data.users) s.users = data.users;
    auditLogPaint();
  } catch (e) {
    if (gen !== screenGen()) return;
    box.innerHTML = errorBoxHtml(e.message);
  }
}

function _auditUserName(id) {
  if (!id) return 'Все сотрудники';
  const u = auditLogState.users.find(x => String(x.user_id) === String(id));
  return u ? u.full_name : `#${id}`;
}

function auditLogPaint() {
  const box = document.getElementById('content');
  const s = auditLogState;
  const presets = [
    { id: 'all', label: 'Всё' }, { id: 'today', label: 'Сегодня' },
    { id: '7d', label: '7 дней' }, { id: '30d', label: '30 дней' },
  ];
  const customLabel = (s.period === 'custom' && s.from && s.to) ? rangeLabel(s.from, s.to) : '';
  const periodRow = periodSegHtml(presets, s.period, 'data-alperiod', s.period === 'custom', customLabel);
  const periodPanel = s.period === 'custom' ? dateRangeHost() : '';

  const rows = s.entries.length ? s.entries.map(e => `
      <div class="c-row audit-row">
        <div class="card-row-icon">${icon('list')}</div>
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(e.action_label)}</div>
          <div class="card-row-sub">${escapeHtml(e.full_name || '—')}${e.role ? ' · ' + escapeHtml((typeof ROLE_NAMES !== 'undefined' && ROLE_NAMES[e.role]) || e.role) : ''} · ${escapeHtml(e.created_at)}</div>
          ${e.details ? `<div class="card-row-sub audit-row-details">${escapeHtml(e.details)}</div>` : ''}
        </div>
      </div>`).join('')
    : emptyState({ icon: 'list', title: 'Нет записей', hint: 'Попробуйте другой период или сотрудника' });

  box.innerHTML = `
    <div class="form-row">
      <label class="form-label" for="audit-user-btn">Сотрудник</label>
      <button type="button" id="audit-user-btn" class="btn-agent${s.userId ? '' : ' btn-agent--empty'}">
        ${escapeHtml(_auditUserName(s.userId))}
      </button>
    </div>
    ${periodRow}
    ${periodPanel}
    <div class="section-label">Записей: ${s.total}</div>
    <div class="c-surface c-surface--list">${rows}</div>
    ${s.hasMore ? '<button class="btn-secondary" id="audit-log-more">Показать ещё</button>' : ''}
  `;

  if (s.period === 'custom') {
    mountCalendar(box.querySelector('.cal-host'), s.from, s.to, (from, to) => {
      s.from = from; s.to = to;
      auditLogLoad(screenGen(), false);
    });
  }
  box.querySelectorAll('[data-alperiod]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      s.period = btn.dataset.alperiod;
      auditLogLoad(screenGen(), false);
    });
  });
  box.querySelector('#audit-user-btn')?.addEventListener('click', () => {
    haptic('light');
    const items = [{ id: '', name: 'Все сотрудники' }, ...s.users.map(u => ({ id: u.user_id, name: u.full_name }))];
    openListPicker({
      title: 'Сотрудник', items, selectedId: s.userId,
      onPick: async (item) => {
        s.userId = item.id === '' ? '' : String(item.id);
        await auditLogLoad(screenGen(), false);
      },
    });
  });
  box.querySelector('#audit-log-more')?.addEventListener('click', () => {
    haptic('light');
    auditLogLoad(screenGen(), true);
  });
}
