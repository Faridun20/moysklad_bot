// Telegram WebApp SDK
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// initData живёт в URL-хэше — теряется при reload/навигации.
// Кэшируем в sessionStorage: переживает SPA-переходы, но не закрытие вкладки.
// Данные всё равно проверяются подписью + auth_date на сервере.
const _SESSION_KEY = 'tg_init_data';
let _initData = tg.initData || '';
if (_initData) {
  sessionStorage.setItem(_SESSION_KEY, _initData);
} else {
  _initData = sessionStorage.getItem(_SESSION_KEY) || '';
}

// Telegram Desktop передаёт initData через postMessage из родительского
// фрейма — асинхронно, уже ПОСЛЕ того как скрипт спарсился. Повторно
// читаем tg.initData в нужный момент и обновляем кэш.
function _refreshInitData() {
  const live = tg.initData || '';
  if (live && live !== _initData) {
    _initData = live;
    sessionStorage.setItem(_SESSION_KEY, live);
  }
  return _initData;
}

// Состояние приложения
let currentUser = null;
let currentScreen = 'home';

const ROLE_NAMES = {
  admin: '👑 Админ',
  boss: '🏆 Руководитель',
  manager: '💼 Менеджер',
  employee: '👤 Сотрудник',
};

// ─── Инициализация ──────────────────────────────────

async function init() {
  try {
    // Перечитываем tg.initData — на Telegram Desktop он приходит через
    // postMessage после загрузки страницы. Если ещё пуст — ждём 350 мс
    // (достаточно для одного round-trip Desktop ↔ WebView) и пробуем снова.
    _refreshInitData();
    if (!_initData) {
      await new Promise(r => setTimeout(r, 350));
      _refreshInitData();
    }

    const response = await fetch('/api/me', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: _initData }),
    });

    if (response.status === 401) {
      const isEmpty = !_initData;
      document.getElementById('content').innerHTML = `
        <div class="error-card">
          <div class="error-icon">🔐</div>
          <div class="error-title">Нет доступа</div>
          <div class="error-body">${isEmpty
            ? 'Откройте приложение через кнопку <b>«Открыть»</b> в боте — не через браузер.'
            : 'Ошибка авторизации. Попробуйте закрыть и открыть снова.'
          }</div>
          <button class="btn-primary" onclick="location.reload()">Повторить</button>
        </div>`;
      return;
    }
    if (!response.ok) {
      throw new Error(`Ошибка сервера (${response.status})`);
    }

    currentUser = await response.json();
    renderHeader();
    initNav();
    showScreen('home');
  } catch (e) {
    document.getElementById('content').innerHTML = `
      <div class="error-card">
        <div class="error-icon">⚠️</div>
        <div class="error-title">Нет связи</div>
        <div class="error-body">${escapeHtml(e.message)}</div>
        <button class="btn-primary" onclick="location.reload()">Повторить</button>
      </div>`;
  }
}

function renderHeader() {
  const greeting = document.getElementById('greeting');
  const badge = document.getElementById('role-badge');
  const name = currentUser.first_name || '';
  greeting.textContent = name ? `Привет, ${name}!` : 'Добро пожаловать!';
  badge.textContent = ROLE_NAMES[currentUser.role] || currentUser.role;
}

function showError(msg) {
  document.getElementById('content').innerHTML = `<div class="error">${msg}</div>`;
}

// ─── Навигация ──────────────────────────────────────

async function showScreen(screen) {
  currentScreen = screen;

  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.screen === screen);
    // Переинициализируем обработчик на случай если DOM обновился
    btn.onclick = () => showScreen(btn.dataset.screen);
  });

  const content = document.getElementById('content');

  // Если был открыт экран с MainButton (например, ввод количества) —
  // скрываем её и снимаем обработчик при переключении, иначе кнопка
  // зависнет на других экранах с устаревшим onClick.
  clearMainButton();
  // Корневые табы не показывают нативную «Назад».
  hideBack();

  // Сбрасываем фильтр категории при каждом входе в "Склад",
  // чтобы не показывать последнюю открытую категорию из прошлой сессии.
  if (screen === 'stock') {
    stockCurrentCat = 'all';
  }

  try {
    switch (screen) {
      case 'home':
        await renderHome();
        break;
      case 'stock':
        // Лега̀cy: внешние ссылки могут звать stock, перенаправляем
        // в объединённый таб «Склад и заказы» сразу на под-вкладку каталога.
        ordersSubTab = 'stock';
        ordersData = null;
        await renderOrdersScreen();
        break;
      case 'orders':
        ordersData = null;
        await renderOrdersScreen();
        break;
      case 'finance':
        await renderFinance();
        break;
      // Legacy ссылки на отдельные «долги» и «платежи» — теперь оба
      // под одной вкладкой «Финансы». Открываем нужную подвкладку.
      case 'debts':
        financeTab = 'debts';
        await renderFinance();
        break;
      case 'payments':
        financeTab = 'payments';
        await renderFinance();
        break;
      case 'analytics':
        await renderAnalytics();
        break;
      default:
        content.innerHTML = `<div class="error">Неизвестный экран: ${screen}</div>`;
    }
  } catch (e) {
    // Если render упал — показываем ошибку, а не оставляем старый контент
    content.innerHTML = `<div class="error">❌ ${e.message || e}</div>`;
  }
}

// Текущая под-вкладка для объединённого экрана «Склад и заказы»
let ordersSubTab = 'orders';

async function renderOrdersScreen() {
  // Делегируем в существующие render-функции (они пишут в #content).
  if (ordersSubTab === 'orders') {
    await renderOrders();
  } else {
    await renderStock();
  }
  // Поверх их вывода — прикрепляем переключатель под-вкладок.
  const content = document.getElementById('content');
  const tabsHtml = `
    <div class="sub-tabs">
      <button class="sub-tab ${ordersSubTab === 'orders' ? 'active' : ''}"
              data-sub="orders">📋 Заказы</button>
      <button class="sub-tab ${ordersSubTab === 'stock' ? 'active' : ''}"
              data-sub="stock">📦 Каталог</button>
    </div>
  `;
  content.insertAdjacentHTML('afterbegin', tabsHtml);
  document.querySelectorAll('.sub-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      ordersSubTab = btn.dataset.sub;
      renderOrdersScreen();
    });
  });
}

async function renderHome() {
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="sk sk-hero"></div>
    <div class="sk-grid">${Array(4).fill('<div class="sk sk-action"></div>').join('')}</div>
    <div class="sk sk-label"></div>
    ${Array(3).fill('<div class="sk sk-card"></div>').join('')}
  `;

  let data;
  try {
    const r = await fetch('/api/home', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: _initData }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    data = await r.json();
  } catch (e) {
    content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
    return;
  }

  const cur = data.currency || 'USD';
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const fmtCur = n => `${fmt(n)} ${cur}`;
  const isBoss = data.role === 'admin' || data.role === 'boss';
  const mo = data.my_orders;

  // ─── Hero: большая «балансовая» цифра + кратко ───────
  const todayLabel = data.today.scope === 'personal' ? 'Моя выручка сегодня' : 'Выручка компании сегодня';
  const heroSub = `${data.today.shipments} отгр. · ${data.today.clients} клиентов`;
  const hero = `
    <div class="hero">
      <div class="hero-label">${todayLabel}</div>
      <div class="hero-value">${fmt(data.today.revenue)}<span class="hero-currency">${cur}</span></div>
      <div class="hero-delta">${heroSub}</div>
    </div>
  `;

  // ─── Action-grid: 4 быстрых действия. Склад и Заказы объединены
  //    в одной вкладке «Склад и заказы», поэтому в гриде разделяем
  //    «открыть каталог» и «создать заказ» по data-new флагу.
  const actions = isBoss ? `
    <div class="action-grid">
      <button class="action-btn" data-go="orders">
        <span class="action-btn-icon icon-orange">📦</span>Каталог
      </button>
      <button class="action-btn" data-go="orders">
        <span class="action-btn-icon icon-amber">⏳</span>Заявки
      </button>
      <button class="action-btn" data-go="analytics">
        <span class="action-btn-icon icon-purple">📊</span>Аналитика
      </button>
      <button class="action-btn" data-go="payments">
        <span class="action-btn-icon icon-green">💵</span>Платежи
      </button>
    </div>
  ` : `
    <div class="action-grid">
      <button class="action-btn" data-go="orders">
        <span class="action-btn-icon icon-orange">📦</span>Каталог
      </button>
      <button class="action-btn" data-go="orders" data-new="1">
        <span class="action-btn-icon icon-blue">➕</span>Заказ
      </button>
      <button class="action-btn" data-go="analytics">
        <span class="action-btn-icon icon-purple">📊</span>Аналитика
      </button>
      <button class="action-btn" data-go="payments">
        <span class="action-btn-icon icon-green">💵</span>Платёж
      </button>
    </div>
  `;

  // ─── Предупреждение если не привязан к МойСклад ─────
  const linkWarning = (!data.ms_linked && data.role === 'manager') ? `
    <div class="warn-card">
      ⚠️ <b>Аккаунт не привязан к МойСклад.</b><br>
      <span style="font-size:12px;">Откройте чат с ботом и нажмите /start. Без привязки персональная аналитика недоступна.</span>
    </div>
  ` : '';

  // ─── Босс: ожидающие заявки + лидерборд ─────────────
  let bossBlock = '';
  if (isBoss) {
    if (data.pending_requests > 0) {
      bossBlock += `
        <div class="card" style="cursor:pointer;" id="go-requests">
          <div style="display:flex; align-items:center; gap:12px;">
            <div class="card-row-icon" style="background:#fff1ce; color:#6f4a06;">⏳</div>
            <div style="flex:1;">
              <div style="font-weight:600;">${data.pending_requests} заявок на апрув</div>
              <div style="font-size:12px; color: var(--text-mute); margin-top:2px;">Нажмите чтобы открыть</div>
            </div>
          </div>
        </div>
      `;
    }
    const topEmp = data.top_employees || [];
    if (topEmp.length > 0) {
      const medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣'];
      bossBlock += `
        <div class="section-label">Топ сотрудники · неделя</div>
        <div class="card-list">
          ${topEmp.map((e, i) => `
            <div class="card-row" style="cursor:default;">
              <div class="card-row-icon">${medals[i] || ''}</div>
              <div class="card-row-info">
                <div class="card-row-title">${escapeHtml(e.name)}</div>
                <div class="card-row-sub">${e.count} отгрузок</div>
              </div>
              <div>
                <div class="card-row-value">${fmtCur(e.revenue)}</div>
              </div>
            </div>
          `).join('')}
        </div>
      `;
    }
  }

  // ─── Мои заказы (для менеджера и босса показываем его собственные) ─
  let ordersBlock = '';
  if (mo.total > 0) {
    const statsRow = `
      <div class="stat-grid stat-grid--three">
        <div class="stat">
          <div class="stat-value">${mo.draft}</div>
          <div class="stat-label">📝 Черновики</div>
        </div>
        <div class="stat">
          <div class="stat-value ${mo.pending > 0 ? 'stat-value-amber' : ''}">${mo.pending}</div>
          <div class="stat-label">⏳ Ожидают</div>
        </div>
        <div class="stat">
          <div class="stat-value ${mo.approved > 0 ? 'stat-value-green' : ''}">${mo.approved}</div>
          <div class="stat-label">✅ Одобрено</div>
        </div>
      </div>
    `;
    const recentList = mo.recent.length > 0 ? `
      <div class="card-list">
        ${mo.recent.map(o => `
          <div class="card-row" data-order-id="${o.id}">
            <div class="card-row-icon icon-${o.status}">${STATUS_EMOJI[o.status] || '📋'}</div>
            <div class="card-row-info">
              <div class="card-row-title">Заказ #${o.id}${o.agent_name ? ' · ' + escapeHtml(o.agent_name) : ''}</div>
              <div class="card-row-sub">${o.created_at}</div>
            </div>
            <span class="stock-badge ${o.status === 'approved' ? 'badge-green' : o.status === 'rejected' ? 'badge-red' : 'badge-yellow'}">${STATUS_NAME[o.status]}</span>
          </div>
        `).join('')}
      </div>
    ` : '';
    ordersBlock = `
      <div class="section-label">Мои заказы</div>
      ${statsRow}
      ${recentList}
    `;
  }

  content.innerHTML = hero + actions + linkWarning + bossBlock + ordersBlock;

  // Action-grid → переход на нужный таб (и опционально открыть новый заказ)
  document.querySelectorAll('[data-go]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic();
      const target = btn.dataset.go;
      const isNew = btn.dataset.new === '1';
      showScreen(target);
      // Если кликнули по «+ Заказ» — после загрузки экрана откроем редактор
      if (isNew && typeof openOrderEditor === 'function') {
        // Дать времени renderOrders выполниться, потом инициировать создание
        setTimeout(() => openOrderEditor(null), 50);
      }
    });
  });

  // Клик по карточке заявок → Заказы
  const goReq = document.getElementById('go-requests');
  if (goReq) goReq.addEventListener('click', () => showScreen('orders'));

  // Клик по строке недавнего заказа → Заказы
  document.querySelectorAll('[data-order-id]').forEach(row => {
    row.addEventListener('click', () => showScreen('orders'));
  });
}

// ─── Экран: Склад ───────────────────────────────────

let stockData = null;          // { products, categories }
let stockCurrentCat = 'all';   // id выбранной категории или 'all'
let stockSearch = '';

async function renderStock() {
  const content = document.getElementById('content');
  content.innerHTML = loading('Загружаю остатки…');

  if (!stockData) {
    try {
      const r = await fetch('/api/stock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: _initData }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || 'Ошибка загрузки склада');
      }
      stockData = await r.json();
    } catch (e) {
      content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
      return;
    }
  }

  renderStockContent();
}

function _stockBadge(stock) {
  if (stock <= 0) return '<span class="stock-badge badge-red">нет</span>';
  if (stock < 20) return `<span class="stock-badge badge-red">${stock}</span>`;
  if (stock < 100) return `<span class="stock-badge badge-yellow">${stock}</span>`;
  return `<span class="stock-badge badge-green">${stock}</span>`;
}

function _stockFiltered() {
  const { products } = stockData;
  const search = stockSearch.toLowerCase();
  return products.filter(p => {
    if (stockCurrentCat !== 'all' && p.folder_id !== stockCurrentCat) return false;
    if (search && !p.name.toLowerCase().includes(search)) return false;
    return true;
  });
}

// Перерисовать ТОЛЬКО список товаров + строку «показаны первые N».
// Каркас экрана (поле поиска, таблетки категорий) не трогаем — иначе
// поиск теряет фокус и сбрасывается скролл при каждом нажатии клавиши.
function renderStockList() {
  const listEl = document.getElementById('stock-list');
  if (!listEl) return;
  const filtered = _stockFiltered();

  listEl.innerHTML = filtered.length === 0
    ? `<div class="empty-state">
        <div class="empty-state-icon">📦</div>
        <div class="empty-state-title">Товары не найдены</div>
        <div class="empty-state-hint">Попробуйте изменить категорию или поисковый запрос</div>
      </div>`
    : filtered.slice(0, 200).map(p => `
        <div class="stock-row">
          <div class="stock-info">
            <div class="stock-name">${escapeHtml(p.name)}</div>
            <div class="stock-folder">${escapeHtml(p.folder_name || '—')} · ${p.unit}</div>
          </div>
          ${_stockBadge(p.stock)}
        </div>
      `).join('');

  const truncEl = document.getElementById('stock-trunc');
  if (truncEl) {
    truncEl.innerHTML = filtered.length > 200
      ? `<div class="loader">Показаны первые 200 из ${filtered.length}. Уточните фильтр.</div>`
      : '';
  }
}

function renderStockContent() {
  const content = document.getElementById('content');
  const { products, categories } = stockData;

  // Категории — таблетки сверху
  const catBtns = [{ id: 'all', name: `Все (${products.length})` }, ...categories]
    .map(c =>
      `<button class="cat-btn ${stockCurrentCat === c.id ? 'active' : ''}" data-cat="${c.id}">${c.name}</button>`
    ).join('');

  content.innerHTML = `
    <div class="section-label">Категории</div>
    <div class="cat-row">${catBtns}</div>
    <div class="form-row" style="margin: 8px 0;">
      <input id="stock-search" class="form-input" placeholder="🔎 Поиск товара…" value="${escapeHtml(stockSearch)}">
    </div>
    <div class="section-label">Товары</div>
    <div class="stock-list" id="stock-list"></div>
    <div id="stock-trunc"></div>
  `;
  renderStockList();

  document.querySelectorAll('[data-cat]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      stockCurrentCat = btn.dataset.cat;
      // Подсветить активную таблетку без полного ре-рендера каркаса.
      document.querySelectorAll('[data-cat]').forEach(b =>
        b.classList.toggle('active', b.dataset.cat === stockCurrentCat)
      );
      renderStockList();
    });
  });
  const searchInput = document.getElementById('stock-search');
  if (searchInput) {
    searchInput.addEventListener('input', e => {
      stockSearch = e.target.value;
      renderStockList();  // обновляем только список — поле держит фокус
    });
    // не дёргаем фокус, чтобы не открывать клавиатуру при первом рендере
  }
}

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function loading(msg = 'Загрузка…') {
  return `<div class="spinner-wrap"><div class="spinner"></div><span>${msg}</span></div>`;
}

function haptic(type = 'light') {
  try { tg.HapticFeedback?.impactOccurred(type); } catch {}
}

// ─── Нативная кнопка «Назад» Telegram ───────────────
// Используем родную tg.BackButton на вложенных экранах вместо
// самодельных ◀️. Храним последний хендлер, чтобы снять его перед
// привязкой нового — иначе обработчики копятся и срабатывают разом.
let _backHandler = null;
function showBack(handler) {
  try {
    if (_backHandler) tg.BackButton.offClick(_backHandler);
    _backHandler = handler;
    tg.BackButton.onClick(handler);
    tg.BackButton.show();
  } catch {}
}
function hideBack() {
  try {
    if (_backHandler) tg.BackButton.offClick(_backHandler);
    _backHandler = null;
    tg.BackButton.hide();
  } catch {}
}

// ─── Очистка зависшей MainButton ────────────────────
// onConfirm из openQuantityInput — замыкание; при уходе с экрана без
// нажатия «назад» обработчик оставался привязанным и срабатывал на
// чужих экранах. Снимаем его централизованно.
let _mainBtnHandler = null;
function clearMainButton() {
  try {
    if (_mainBtnHandler) tg.MainButton.offClick(_mainBtnHandler);
    _mainBtnHandler = null;
    tg.MainButton?.hide();
  } catch {}
}

// ─── Экран: Заказы ──────────────────────────────────

let ordersData = null;
let currentOrderFilter = 'all';
let currentDraftOrder = null; // активный черновик

const STATUS_EMOJI = {
  draft:    '📝',
  pending:  '⏳',
  approved: '✅',
  rejected: '❌',
  shipped:  '🚚',
};

const STATUS_NAME = {
  draft:    'Черновик',
  pending:  'На рассмотрении',
  approved: 'Одобрено',
  rejected: 'Отклонено',
  shipped:  'Отгружено',
};

async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ initData: _initData, ...body }),
  });
  if (!r.ok) throw new Error((await r.json()).detail || 'Ошибка');
  return r.json();
}

async function renderOrders() {
  const content = document.getElementById('content');
  content.innerHTML = loading('Загружаю заказы…');
  try {
    ordersData = await api('/api/orders', {});
  } catch (e) {
    content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
    return;
  }
  renderOrdersMain();
}

function renderOrdersMain() {
  // Список заказов — корневой вид вкладки. Прячем нативную «Назад»
  // на случай возврата из вложенного экрана (редактор, заявки).
  hideBack();
  const content = document.getElementById('content');
  const { orders, role } = ordersData;
  const isBoss = role === 'admin' || role === 'boss';
  const canShip = isBoss || role === 'warehouse_keeper';

  const filters = [
    { id: 'all', label: 'Все' },
    { id: 'draft', label: '📝' },
    { id: 'pending', label: '⏳' },
    { id: 'approved', label: '✅' },
    { id: 'rejected', label: '❌' },
  ];

  const filterBtns = filters.map(f =>
    `<button class="cat-btn ${currentOrderFilter === f.id ? 'active' : ''}" data-filter="${f.id}">${f.label}</button>`
  ).join('');

  const filtered = currentOrderFilter === 'all'
    ? orders
    : orders.filter(o => o.status === currentOrderFilter);

  const list = filtered.length === 0
    ? `<div class="empty-state">
        <div class="empty-state-icon">📋</div>
        <div class="empty-state-title">Нет заказов</div>
        <div class="empty-state-hint">${currentOrderFilter !== 'all'
          ? 'Нет заказов с этим статусом'
          : isBoss ? 'Менеджеры ещё не создавали заказов' : 'Нажмите «+ Новый заказ» чтобы начать'
        }</div>
      </div>`
    : filtered.map(o => `
      <div class="order-card order-card--${o.status}" data-id="${o.id}">
        <div class="order-header">
          <div>
            <div class="order-title">${STATUS_EMOJI[o.status] || '📋'} Заказ #${o.id}</div>
            ${isBoss ? `<div class="order-manager">👤 ${o.full_name}</div>` : ''}
          </div>
          <span class="order-status status-${o.status}">${STATUS_NAME[o.status]}</span>
        </div>
        ${o.agent_name ? `<div class="order-agent">🏢 ${o.agent_name}</div>` : ''}
        <div class="order-meta">
          <span>📦 ${o.items_count} тов.</span>
          <span>${o.created_at}</span>
          ${o.total > 0 ? `<span class="order-total">💰 ${Math.round(o.total).toLocaleString('ru-RU')}</span>` : ''}
        </div>
        ${o.items.slice(0, 2).map(it => {
          const sub = (it.quantity || 0) * (it.price || 0);
          const priceStr = (it.price && it.price > 0)
            ? ` × ${it.price.toLocaleString('ru-RU')} = <b>${Math.round(sub).toLocaleString('ru-RU')}</b>`
            : '';
          return `<div class="order-item-preview">• ${escapeHtml(it.name)} — ${it.quantity} ${it.unit}${priceStr}</div>`;
        }).join('')}
        ${o.status === 'draft' && !isBoss ? `
          <div class="draft-actions">
            <button class="btn-edit-order" data-id="${o.id}">✏️ Редактировать</button>
            <button class="btn-delete-draft" data-id="${o.id}">🗑️ Удалить</button>
          </div>
        ` : ''}
        ${o.status === 'approved' && canShip ? `
          <div class="draft-actions">
            <button class="btn-confirm-pay btn-ship-order" data-id="${o.id}">🚚 Отгрузить</button>
          </div>
        ` : ''}
        ${o.status === 'approved' && isBoss ? `
          <div class="draft-actions">
            <button class="btn-reject-pay btn-cancel-order" data-id="${o.id}">🚫 Отменить заказ</button>
          </div>
          <div class="limit-edit cancel-box" data-id="${o.id}" hidden>
            <input type="text" class="form-input cancel-reason" placeholder="Причина отмены">
            <button class="btn-reject-pay cancel-send" data-id="${o.id}">Подтвердить отмену</button>
          </div>
        ` : ''}
      </div>
    `).join('');

  content.innerHTML = `
    <div class="orders-toolbar">
      <div class="cat-scroll">${filterBtns}</div>
      ${!isBoss ? `<button class="btn-new-order" id="btn-new-order">+ Новый заказ</button>` : ''}
    </div>

    ${isBoss ? `<button class="requests-btn" id="show-requests">⏳ Заявки на рассмотрении</button>` : ''}

    <div class="orders-list">${list}</div>
  `;

  // Фильтры
  document.querySelectorAll('.cat-btn[data-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      currentOrderFilter = btn.dataset.filter;
      renderOrdersMain();
    });
  });

  // Новый заказ
  document.getElementById('btn-new-order')?.addEventListener('click', () => openOrderEditor(null));

  // Редактировать черновик
  document.querySelectorAll('.btn-edit-order').forEach(btn => {
    btn.addEventListener('click', () => openOrderEditor(parseInt(btn.dataset.id)));
  });

  // Удалить черновик
  document.querySelectorAll('.btn-delete-draft').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = parseInt(btn.dataset.id);
      tg.showConfirm(`Удалить черновик #${id}? Восстановить нельзя.`, async ok => {
        if (!ok) return;
        btn.disabled = true;
        try {
          await api('/api/orders/delete_draft', { order_id: id });
          tg.HapticFeedback?.notificationOccurred('success');
          ordersData = null;
          await renderOrders();
        } catch (e) {
          tg.HapticFeedback?.notificationOccurred('error');
          tg.showAlert('❌ ' + e.message);
          btn.disabled = false;
        }
      });
    });
  });

  // Отгрузка заказа (босс/кладовщик).
  document.querySelectorAll('.btn-ship-order').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const id = parseInt(btn.dataset.id);
      tg.showConfirm(`Отметить заказ #${id} отгруженным?`, async ok => {
        if (!ok) return;
        btn.disabled = true;
        try {
          await api('/api/orders/ship', { order_id: id });
          tg.HapticFeedback?.notificationOccurred('success');
          tg.showAlert(`🚚 Заказ #${id} отгружен`);
          ordersData = null;
          await renderOrders();
        } catch (err) {
          tg.HapticFeedback?.notificationOccurred('error');
          tg.showAlert('❌ ' + err.message);
          btn.disabled = false;
        }
      });
    });
  });

  // Отмена заказа (босс): раскрыть поле причины.
  document.querySelectorAll('.btn-cancel-order').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const box = document.querySelector(`.cancel-box[data-id="${btn.dataset.id}"]`);
      if (box) box.hidden = !box.hidden;
    });
  });
  document.querySelectorAll('.cancel-send').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = parseInt(btn.dataset.id);
      const box = document.querySelector(`.cancel-box[data-id="${btn.dataset.id}"]`);
      const reason = box.querySelector('.cancel-reason').value.trim();
      if (reason.length < 3) { tg.showAlert('❌ Укажите причину'); return; }
      btn.disabled = true;
      try {
        await api('/api/orders/cancel', { order_id: id, reason });
        tg.HapticFeedback?.notificationOccurred('success');
        tg.showAlert(`🚫 Заказ #${id} отменён`);
        ordersData = null;
        await renderOrders();
      } catch (err) {
        tg.HapticFeedback?.notificationOccurred('error');
        tg.showAlert('❌ ' + err.message);
        btn.disabled = false;
      }
    });
  });

  // Заявки для босса
  document.getElementById('show-requests')?.addEventListener('click', renderPendingRequests);
}


// ─── Редактор заказа ────────────────────────────────

async function openOrderEditor(orderId) {
  const content = document.getElementById('content');

  // Создаём черновик если новый
  if (!orderId) {
    content.innerHTML = loading('Создаю заказ…');
    try {
      const result = await api('/api/orders/create', {});
      orderId = result.order_id;
    } catch (e) {
      content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
      return;
    }
  }

  currentDraftOrder = {
    id: orderId,
    items: [],
    agent_id: null,
    agent_name: null,
  };

  // Загружаем уже добавленные товары если редактируем
  if (ordersData) {
    const existing = ordersData.orders.find(o => o.id === orderId);
    if (existing) {
      currentDraftOrder.items = existing.items.map((it, i) => ({ ...it, item_id: i }));
      currentDraftOrder.agent_name = existing.agent_name;
    }
  }

  renderOrderEditor();
}

function renderOrderEditor() {
  const content = document.getElementById('content');
  const order = currentDraftOrder;

  const grandTotal = order.items.reduce(
    (sum, it) => sum + (it.quantity || 0) * (it.price || 0), 0
  );
  const itemsList = order.items.length === 0
    ? '<div class="editor-empty">Товары не добавлены</div>'
    : order.items.map((it, i) => {
        const sub = (it.quantity || 0) * (it.price || 0);
        const subStr = it.price > 0
          ? ` · ${it.price.toLocaleString('ru-RU')} = <b>${Math.round(sub).toLocaleString('ru-RU')}</b>`
          : '';
        return `
          <div class="editor-item">
            <div class="editor-item-info">
              <div class="editor-item-name">${escapeHtml(it.name)}</div>
              <div class="editor-item-qty">${it.quantity} ${it.unit || 'шт'}${subStr}</div>
            </div>
            <button class="editor-item-del" data-idx="${i}">✕</button>
          </div>
        `;
      }).join('') + (grandTotal > 0 ? `
        <div class="editor-item" style="background: var(--tg-theme-secondary-bg-color, #f5f5f5); font-weight: 600;">
          <div class="editor-item-info">
            <div class="editor-item-name">💰 Итого</div>
          </div>
          <div>${Math.round(grandTotal).toLocaleString('ru-RU')}</div>
        </div>
      ` : '');

  content.innerHTML = `
    <div class="editor-header">
      <div class="editor-title">Заказ #${order.id}</div>
    </div>

    <div class="section-label">Клиент</div>
    <div class="agent-selector" id="agent-selector">
      ${order.agent_name
        ? `<div class="agent-selected">🏢 ${order.agent_name} <button id="change-agent">Изменить</button></div>`
        : `<button class="btn-agent" id="choose-agent">👤 Выбрать клиента</button>`
      }
    </div>

    <div class="section-label">Товары</div>
    <div class="editor-items" id="editor-items">${itemsList}</div>
    <button class="btn-add-product" id="btn-add-product">+ Добавить товар</button>

    <div class="section-label">Оплата</div>
    <div class="payment-selector">
      <label class="payment-option">
        <input type="radio" name="payment_type" value="paid"
          ${(currentDraftOrder.payment_type || 'paid') === 'paid' ? 'checked' : ''}>
        <span>💵 Оплачено сразу</span>
      </label>
      <label class="payment-option">
        <input type="radio" name="payment_type" value="credit"
          ${currentDraftOrder.payment_type === 'credit' ? 'checked' : ''}>
        <span>💳 В долг</span>
      </label>
    </div>
    <div class="due-date-wrap ${currentDraftOrder.payment_type === 'credit' ? '' : 'hidden'}" id="due-date-wrap">
      <label class="due-date-label">Дата возврата долга:</label>
      <input type="date" id="due-date-input" class="due-date-input"
        value="${currentDraftOrder.due_date || ''}"
        min="${new Date().toISOString().slice(0,10)}">
    </div>

    <div class="editor-footer">
      <button class="btn-submit-order" id="btn-submit"
        ${order.items.length === 0 || !order.agent_name ? 'disabled' : ''}>
        🚀 Отправить заявку
      </button>
      ${order.items.length === 0 || !order.agent_name
        ? '<div class="editor-hint">Добавьте товары и выберите клиента</div>'
        : ''}
    </div>
  `;

  // Назад — нативная кнопка Telegram
  showBack(() => {
    ordersData = null;
    renderOrders();
  });

  // Выбор клиента
  document.getElementById('choose-agent')?.addEventListener('click', openAgentSearch);
  document.getElementById('change-agent')?.addEventListener('click', openAgentSearch);

  // Добавить товар
  document.getElementById('btn-add-product').addEventListener('click', openProductPicker);

  // Удалить товар
  document.querySelectorAll('.editor-item-del').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx);
      const item = currentDraftOrder.items[idx];
      if (item.item_id) {
        try { await api('/api/orders/remove_item', { item_id: item.item_id }); } catch {}
      }
      currentDraftOrder.items.splice(idx, 1);
      renderOrderEditor();
    });
  });

  // Тип оплаты — радио + показ/скрытие date picker
  document.querySelectorAll('input[name="payment_type"]').forEach(r => {
    r.addEventListener('change', () => {
      currentDraftOrder.payment_type = r.value;
      document.getElementById('due-date-wrap')
        .classList.toggle('hidden', r.value !== 'credit');
    });
  });
  document.getElementById('due-date-input')?.addEventListener('change', e => {
    currentDraftOrder.due_date = e.target.value;
  });

  // Отправить заявку
  document.getElementById('btn-submit').addEventListener('click', submitOrder);
}


// ─── Поиск клиента ──────────────────────────────────

async function openAgentSearch() {
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="editor-header">
      <div class="editor-title">Выбор клиента</div>
    </div>
    <div class="agent-search-wrap">
      <input type="text" id="agent-search" class="form-input" placeholder="🔍 Поиск по имени…">
    </div>
    <div id="agent-list" class="orders-list">
      <div class="loader">Введите имя для поиска</div>
    </div>
  `;

  showBack(renderOrderEditor);

  const input = document.getElementById('agent-search');
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => loadAgents(input.value), 400);
  });

  // Загружаем первых 20 сразу
  loadAgents('');
}

async function loadAgents(search) {
  const list = document.getElementById('agent-list');
  if (!list) return;
  list.innerHTML = loading('Загружаю…');
  try {
    const data = await api('/api/agents', { search });
    if (data.agents.length === 0) {
      list.innerHTML = `<div class="loader">Клиенты не найдены</div>`;
      return;
    }
    list.innerHTML = data.agents.map(a => `
      <div class="agent-row" data-id="${a.id}" data-name="${a.name}">
        <div class="agent-name">👤 ${a.name}</div>
        ${a.phone ? `<div class="agent-phone">${a.phone}</div>` : ''}
      </div>
    `).join('');

    document.querySelectorAll('.agent-row').forEach(row => {
      row.addEventListener('click', async () => {
        haptic('light');
        currentDraftOrder.agent_id = row.dataset.id;
        currentDraftOrder.agent_name = row.dataset.name;
        try {
          await api('/api/orders/set_agent', {
            order_id: currentDraftOrder.id,
            agent_id: currentDraftOrder.agent_id,
            agent_name: currentDraftOrder.agent_name,
          });
        } catch {}
        renderOrderEditor();
      });
    });
  } catch (e) {
    list.innerHTML = `<div class="error">❌ ${e.message}</div>`;
  }
}


// ─── Выбор товара ───────────────────────────────────

let orderStockCache = null;

async function openProductPicker() {
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="editor-header">
      <div class="editor-title">Выбор товара</div>
    </div>
    <div class="agent-search-wrap">
      <input type="text" id="prod-search" class="form-input" placeholder="🔍 Поиск товара…">
    </div>
    <div id="cat-filters" class="cat-scroll"></div>
    <div id="prod-list" class="orders-list">${loading('Загружаю…')}</div>
  `;

  showBack(renderOrderEditor);

  // Загружаем склад
  if (!orderStockCache) {
    try {
      const data = await api('/api/stock', {});
      orderStockCache = data;
    } catch (e) {
      document.getElementById('prod-list').innerHTML = `<div class="error">❌ ${e.message}</div>`;
      return;
    }
  }

  let selectedCat = 'all';
  const { products, categories } = orderStockCache;

  function renderProducts() {
    const search = document.getElementById('prod-search')?.value.toLowerCase() || '';
    let filtered = selectedCat === 'all'
      ? products
      : products.filter(p => p.folder_id === selectedCat);
    if (search) {
      filtered = filtered.filter(p => p.name.toLowerCase().includes(search));
    }

    const list = document.getElementById('prod-list');
    if (!list) return;
    list.innerHTML = filtered.length === 0
      ? '<div class="loader">Товары не найдены</div>'
      : filtered.slice(0, 50).map(p => {
          const ind = p.stock >= 100 ? 'green' : p.stock >= 20 ? 'yellow' : 'red';
          return `
            <div class="prod-row"
                 data-name="${escapeHtml(p.name)}"
                 data-unit="${escapeHtml(p.unit)}"
                 data-stock="${p.stock}"
                 data-href="${escapeHtml(p.href || '')}">
              <div class="prod-info">
                <div class="prod-name">${escapeHtml(p.name)}</div>
                ${p.folder_name ? `<div class="prod-folder">${escapeHtml(p.folder_name)}</div>` : ''}
              </div>
              <span class="stock-badge badge-${ind}">${p.stock} ${p.unit}</span>
            </div>
          `;
        }).join('');

    document.querySelectorAll('.prod-row').forEach(row => {
      row.addEventListener('click', () => {
        haptic('light');
        openQuantityInput(
          row.dataset.name, row.dataset.unit,
          parseFloat(row.dataset.stock),
          row.dataset.href || ''
        );
      });
    });
  }

  // Категории
  const catFilters = document.getElementById('cat-filters');
  catFilters.innerHTML = [
    `<button class="cat-btn active" data-cat="all">Все</button>`,
    ...categories.map(c => `<button class="cat-btn" data-cat="${c.id}">${c.name}</button>`),
  ].join('');

  catFilters.querySelectorAll('.cat-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      catFilters.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedCat = btn.dataset.cat;
      renderProducts();
    });
  });

  // Дебаунс поиска товара — как в поиске клиентов (иначе фильтрация
  // дёргается на каждое нажатие при больших каталогах).
  const prodSearch = document.getElementById('prod-search');
  let prodTimer;
  prodSearch.addEventListener('input', () => {
    clearTimeout(prodTimer);
    prodTimer = setTimeout(renderProducts, 250);
  });
  renderProducts();
}

function openQuantityInput(name, unit, maxStock, href) {
  const content = document.getElementById('content');
  const currencies = ['USD', 'UZS', 'RUB', 'EUR'];
  // Если у заказа уже была валюта (после первой позиции) — берём её и
  // блокируем переключение. Все позиции одного ордера в одной валюте.
  const lockedCurrency = (currentDraftOrder && currentDraftOrder.currency) || null;
  const initialCur = lockedCurrency || 'USD';

  const curButtons = currencies.map(c =>
    `<button class="cur-btn ${c === initialCur ? 'active' : ''}"
             data-cur="${c}" ${lockedCurrency ? 'disabled' : ''}>${c}</button>`
  ).join('');

  content.innerHTML = `
    <div class="editor-header">
      <div class="editor-title">Количество и цена</div>
    </div>
    <div class="qty-screen">
      <div class="qty-product-name">${escapeHtml(name)}</div>
      <div class="qty-stock">На складе: ${maxStock} ${unit}</div>

      <div class="form-row" style="margin-top:12px;">
        <label class="form-label">Количество (${unit})</label>
        <input type="number" id="qty-input" class="form-input"
          placeholder="0" inputmode="decimal" min="0.1" step="0.1">
      </div>

      <div class="form-row">
        <label class="form-label">Валюта заказа</label>
        <div class="cur-row">${curButtons}</div>
        ${lockedCurrency
          ? '<div class="qty-stock" style="margin-top:4px;font-size:11px;">Валюта фиксируется после первой позиции</div>'
          : ''}
      </div>

      <div class="form-row">
        <label class="form-label">Цена за ${unit}</label>
        <input type="number" id="price-input" class="form-input"
          placeholder="0" inputmode="decimal" min="0" step="0.01">
      </div>

      <div id="line-total" class="qty-stock" style="margin: 8px 0 80px;">
        Итого: <b>0 ${initialCur}</b>
      </div>
    </div>
  `;

  showBack(() => {
    clearMainButton();
    openProductPicker();
  });
  const qtyEl = document.getElementById('qty-input');
  const priceEl = document.getElementById('price-input');
  const totalEl = document.getElementById('line-total');
  qtyEl.focus();

  let selectedCurrency = initialCur;
  document.querySelectorAll('.cur-btn[data-cur]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (lockedCurrency) return;
      document.querySelectorAll('.cur-btn[data-cur]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedCurrency = btn.dataset.cur;
      updateTotal();
    });
  });

  function updateTotal() {
    const q = parseFloat(qtyEl.value) || 0;
    const p = parseFloat(priceEl.value) || 0;
    const t = q * p;
    totalEl.innerHTML = `Итого: <b>${t.toLocaleString('ru-RU', {maximumFractionDigits: 2})} ${selectedCurrency}</b>`;
  }
  qtyEl.addEventListener('input', updateTotal);
  priceEl.addEventListener('input', updateTotal);

  // ─── MainButton: «Добавить в заявку» ─────────────────────────
  // Нативная кнопка Telegram — всегда видна над виртуальной клавиатурой,
  // в отличие от HTML-кнопки внизу формы, которую клавиатура перекрывала.
  async function onConfirm() {
    const qty = parseFloat(qtyEl.value);
    const price = parseFloat(priceEl.value) || 0;
    if (!qty || qty <= 0) {
      tg.HapticFeedback?.notificationOccurred('error');
      tg.showAlert('Введите количество');
      return;
    }
    if (price < 0) {
      tg.HapticFeedback?.notificationOccurred('error');
      tg.showAlert('Цена не может быть отрицательной');
      return;
    }
    tg.MainButton?.showProgress?.();
    try {
      const result = await api('/api/orders/add_item', {
        order_id: currentDraftOrder.id,
        product_name: name,
        product_href: href || '',
        quantity: qty,
        unit: unit,
        price: price,
        currency: selectedCurrency,
      });
      currentDraftOrder.items.push({
        name, quantity: qty, unit, price, item_id: result.item_id,
      });
      if (!currentDraftOrder.currency) currentDraftOrder.currency = selectedCurrency;
      tg.HapticFeedback?.notificationOccurred('success');
      tg.MainButton?.hideProgress?.();
      clearMainButton();
      renderOrderEditor();
    } catch (e) {
      tg.MainButton?.hideProgress?.();
      tg.showAlert('❌ ' + e.message);
    }
  }

  if (tg.MainButton) {
    tg.MainButton.setText('✅ Добавить в заявку');
    tg.MainButton.show();
    // Запоминаем хендлер, чтобы clearMainButton() мог его снять при
    // уходе с экрана (см. showScreen / qty-back).
    _mainBtnHandler = onConfirm;
    tg.MainButton.onClick(onConfirm);
  } else {
    // Fallback для окружений без MainButton API (типа браузера) —
    // отрисуем обычную кнопку
    const totalEl2 = document.getElementById('line-total');
    if (totalEl2) {
      const btn = document.createElement('button');
      btn.className = 'btn-primary';
      btn.id = 'qty-confirm-fallback';
      btn.textContent = '✅ Добавить в заявку';
      btn.addEventListener('click', onConfirm);
      totalEl2.parentNode.appendChild(btn);
    }
  }
}

async function submitOrder() {
  const btn = document.getElementById('btn-submit');
  const paymentType = currentDraftOrder.payment_type || 'paid';
  const dueDate = currentDraftOrder.due_date || '';

  // Frontend-валидация: для credit обязательна дата возврата.
  // Бекенд тоже проверяет, но здесь короче UX-фидбек.
  if (paymentType === 'credit' && !dueDate) {
    tg.showAlert('⚠️ Укажите дату возврата долга');
    return;
  }

  if (btn) btn.disabled = true;
  try {
    const result = await api('/api/orders/submit', {
      order_id: currentDraftOrder.id,
      payment_type: paymentType,
      due_date: dueDate || null,
    });
    tg.HapticFeedback?.notificationOccurred('success');
    tg.showAlert(`✅ Заявка #${result.req_id} отправлена руководителю!`);
    ordersData = null;
    currentDraftOrder = null;
    await renderOrders();
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    tg.showAlert('❌ ' + e.message);
    if (btn) btn.disabled = false;
  }
}


async function renderPendingRequests() {
  const content = document.getElementById('content');
  content.innerHTML = loading('Загружаю заявки…');
  try {
    const data = await api('/api/orders/requests', {});
    showBack(renderOrdersMain);
    if (data.requests.length === 0) {
      content.innerHTML = `
        <div class="editor-header">
          <div class="editor-title">Заявки</div>
        </div>
        <div class="empty-state">
          <div class="empty-state-icon">✅</div>
          <div class="empty-state-title">Нет заявок на рассмотрении</div>
          <div class="empty-state-hint">Новые заявки появятся здесь автоматически</div>
        </div>
      `;
      return;
    }
    const items = data.requests.map(r => `
      <div class="order-card">
        <div class="order-header">
          <div>
            <div class="order-title">⏳ Заявка #${r.id}</div>
            <div class="order-manager">👤 ${r.full_name}</div>
          </div>
          <span class="order-status status-pending">Ожидает</span>
        </div>
        ${r.agent_name ? `<div class="order-agent">🏢 ${r.agent_name}</div>` : ''}
        <div class="order-meta"><span>${r.created_at}</span></div>
        <div class="order-items">
          ${r.items.slice(0, 5).map(it =>
            `<div class="order-item">• ${it.name}: <b>${it.quantity} ${it.unit}</b></div>`
          ).join('')}
        </div>
        <div class="req-actions">
          <button class="btn-approve" data-req="${r.id}">✅ Одобрить</button>
          <button class="btn-reject"  data-req="${r.id}">❌ Отклонить</button>
        </div>
      </div>
    `).join('');

    content.innerHTML = `
      <div class="editor-header">
        <div class="editor-title">Заявки (${data.requests.length})</div>
      </div>
      <div class="orders-list">${items}</div>
    `;

    document.querySelectorAll('.btn-approve').forEach(btn =>
      btn.addEventListener('click', () => handleRequest(btn.dataset.req, 'approve'))
    );
    document.querySelectorAll('.btn-reject').forEach(btn =>
      btn.addEventListener('click', () => handleRequest(btn.dataset.req, 'reject'))
    );
  } catch (e) {
    content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
  }
}

async function handleRequest(reqId, action) {
  // Раньше тут был tg.sendData() — он работает ТОЛЬКО когда WebApp
  // открыт из ReplyKeyboardButton. У нас WebApp открывается из меню,
  // поэтому sendData молча игнорировался и кнопка «не работала».
  // Теперь — обычный HTTP-вызов, как у всех остальных операций.
  const path = action === 'approve' ? '/api/requests/approve' : '/api/requests/reject';
  // Блокируем повторные клики, пока запрос в полёте.
  document.querySelectorAll('.btn-approve, .btn-reject').forEach(b => (b.disabled = true));
  try {
    await api(path, { req_id: Number(reqId) });
    tg.showAlert(action === 'approve' ? '✅ Заявка одобрена' : '❌ Заявка отклонена');
  } catch (e) {
    tg.showAlert(`❌ ${e.message}`);
  }
  await renderPendingRequests();
}
// ─── Экран: Аналитика ───────────────────────────────

let analyticsCache = {};  // period -> { ts, data }
let analyticsPeriod = 'month';
const ANALYTICS_TTL_MS = 60 * 1000;

async function renderAnalytics() {
  const content = document.getElementById('content');

  // Короткий кэш по периоду (TTL 60с): переключение неделя/месяц/3мес туда-обратно
  // отдаётся мгновенно, без повторного запроса. TTL намеренно короткий — аналитика
  // строится на отгрузках МойСклад, показывать сильно устаревшие цифры не годится.
  const cached = analyticsCache[analyticsPeriod];
  if (cached && Date.now() - cached.ts < ANALYTICS_TTL_MS) {
    renderAnalyticsContent(cached.data);
    return;
  }

  content.innerHTML = loading('Считаю статистику…');
  try {
    const response = await fetch('/api/analytics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: _initData, period: analyticsPeriod }),
    });
    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.detail || 'Ошибка');
    }
    const data = await response.json();
    analyticsCache[analyticsPeriod] = { ts: Date.now(), data };
    renderAnalyticsContent(data);
  } catch (e) {
    content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
  }
}

function renderAnalyticsContent(data) {
  const content = document.getElementById('content');
  const fmt = n => Math.round(n).toLocaleString('ru-RU');

  const periods = [
    { id: 'week', label: 'Неделя' },
    { id: 'month', label: 'Месяц' },
    { id: '3month', label: '3 мес' },
    { id: 'year', label: 'Год' },
  ];

  const periodButtons = periods.map(p =>
    `<button class="cat-btn ${analyticsPeriod === p.id ? 'active' : ''}" data-period="${p.id}">${p.label}</button>`
  ).join('');

  const trendIcon = data.trend > 0 ? '📈' : data.trend < 0 ? '📉' : '➡️';
  const trendClass = data.trend > 0 ? 'trend-up' : data.trend < 0 ? 'trend-dn' : '';
  const trendStr = data.trend !== 0 ? `${trendIcon} ${data.trend > 0 ? '+' : ''}${data.trend}%` : '';

  const maxDay = Math.max(...data.by_day.map(d => d.count), 1);
  const daysBars = data.by_day.map(d => `
    <div class="bar-row">
      <span class="bar-day">${d.day}</span>
      <div class="bar-bg"><div class="bar-fill" style="width:0" data-w="${Math.round((d.count / maxDay) * 100)}"></div></div>
      <span class="bar-val">${d.count}</span>
    </div>
  `).join('');

  const topItems = data.top_products.length === 0
    ? '<div class="loader">Нет данных</div>'
    : data.top_products.map((p, i) => {
        const medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣'];
        return `
          <div class="top-row">
            <span class="top-medal">${medals[i] || (i + 1)}</span>
            <div class="top-info">
              <div class="top-name">${p.name}</div>
              <div class="top-sub">${fmt(p.qty)} шт · ${fmt(p.sum)} $</div>
            </div>
          </div>
        `;
      }).join('');

  content.innerHTML = `
    <div class="section-label">Период</div>
    <div class="cat-scroll">${periodButtons}</div>

    <div class="stat-grid">
      <div class="stat">
        <div class="stat-value">${fmt(data.total)} $</div>
        <div class="stat-label">Выручка</div>
        ${trendStr ? `<div class="${trendClass}" style="font-size:11px;margin-top:4px">${trendStr}</div>` : ''}
      </div>
      <div class="stat">
        <div class="stat-value">${data.count}</div>
        <div class="stat-label">Отгрузок</div>
      </div>
      <div class="stat">
        <div class="stat-value">${data.clients}</div>
        <div class="stat-label">Клиентов</div>
      </div>
      <div class="stat">
        <div class="stat-value">${fmt(data.avg_check)} $</div>
        <div class="stat-label">Средний чек</div>
      </div>
    </div>

    <div class="section-label">Активность по дням</div>
    <div class="card">${daysBars}</div>

    <div class="section-label">Топ товаров</div>
    <div class="card">${topItems}</div>
  `;

  // Bars animate from 0 → target after paint
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.querySelectorAll('.bar-fill[data-w]').forEach(b => {
      b.style.width = b.dataset.w + '%';
    });
  }));

  document.querySelectorAll('.cat-btn[data-period]').forEach(btn => {
    btn.addEventListener('click', () => {
      analyticsPeriod = btn.dataset.period;
      renderAnalytics();
    });
  });
}

// ─── Экран: Платежи ─────────────────────────────────

let paymentsCache = null;
let paymentsPending = [];  // paid-заказы, ждущие подтверждения боссом

async function renderPayments(container) {
  container = container || document.getElementById('content');
  container.innerHTML = loading('Загружаю историю…');

  try {
    const response = await fetch('/api/payments/history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: _initData }),
    });
    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.detail || 'Ошибка');
    }
    paymentsCache = await response.json();
  } catch (e) {
    container.innerHTML = `<div class="error">❌ ${e.message}</div>`;
    return;
  }

  // Для босса — подтянуть paid-заказы, ожидающие подтверждения оплаты.
  // Менеджеру эндпоинт вернёт 403 — тихо пропускаем.
  paymentsPending = [];
  const isBoss = currentUser && (currentUser.role === 'admin' || currentUser.role === 'boss');
  if (isBoss) {
    try {
      const data = await api('/api/payments/pending', {});
      paymentsPending = data.pending || [];
    } catch { /* нет доступа / нет данных — блок просто не покажем */ }
  }

  renderPaymentsContent(container);
}

function renderPaymentsContent(container) {
  container = container || document.getElementById('content');
  const fmt = n => Math.round(n).toLocaleString('ru-RU');

  const statusBadge = s => {
    if (s === 'confirmed') return '<span class="stock-badge badge-green">принят</span>';
    if (s === 'rejected')  return '<span class="stock-badge badge-red">отклонён</span>';
    return '<span class="stock-badge badge-yellow">ожидает</span>';
  };

  const history = paymentsCache.payments.length === 0
    ? '<div class="loader">Платежей пока нет</div>'
    : paymentsCache.payments.map(p => `
        <div class="stock-row">
          <div class="stock-info">
            <div class="stock-name">${fmt(p.amount)} ${p.currency} · ${p.comment}</div>
            <div class="stock-folder">${p.created_at.slice(0, 16)}</div>
          </div>
          ${statusBadge(p.status)}
        </div>
      `).join('');

  // Блок «На подтверждение» — paid-заказы с pending-оплатой (только босс).
  const pendingHtml = paymentsPending.length === 0 ? '' : `
    <div class="section-label section-awaiting">⏳ На подтверждение (${paymentsPending.length})</div>
    <div class="debts-list">${paymentsPending.map(d => `
      <div class="debt-card debt-awaiting">
        <div class="debt-card-top">
          <div class="debt-agent">🏢 ${escapeHtml(d.agent_name)}</div>
          <div class="debt-amount">${fmt(d.pending)} ${escapeHtml(d.currency)}</div>
        </div>
        <div class="debt-card-mid">
          <span class="debt-meta">#${d.order_id} · ${d.items_count} поз. · ${escapeHtml(d.full_name)}</span>
        </div>
        <div class="debt-actions">
          <button class="btn-confirm-pay" data-id="${d.order_id}">✅ Подтвердить</button>
          <button class="btn-reject-pay"  data-id="${d.order_id}">❌ Отклонить</button>
        </div>
      </div>
    `).join('')}</div>
  `;

  container.innerHTML = `
    ${pendingHtml}
    <div class="section-label">Новый платёж (не связан с заказом)</div>
    <div class="card">
      <div class="form-row">
        <label class="form-label">Сумма</label>
        <input type="number" id="pay-amount" class="form-input" placeholder="1500" inputmode="decimal">
      </div>
      <div class="form-row">
        <label class="form-label">Валюта</label>
        <div class="cur-row">
          ${['USD', 'UZS', 'RUB', 'EUR'].map((c, i) =>
            `<button class="cur-btn ${i === 0 ? 'active' : ''}" data-cur="${c}">${c}</button>`
          ).join('')}
        </div>
      </div>
      <div class="form-row">
        <label class="form-label">Комментарий</label>
        <input type="text" id="pay-comment" class="form-input" placeholder="За май, оплата аренды">
      </div>
      <button id="pay-submit" class="btn-primary">Отправить</button>
      <div id="pay-status" class="pay-status"></div>
    </div>

    <div class="section-label">История платежей</div>
    <div class="stock-list">${history}</div>
  `;

  // Выбор валюты — селекторы в рамках container
  let selectedCurrency = 'USD';
  container.querySelectorAll('.cur-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      container.querySelectorAll('.cur-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedCurrency = btn.dataset.cur;
    });
  });

  // Отправка
  container.querySelector('#pay-submit').addEventListener('click', async () => {
    const amount = parseFloat(container.querySelector('#pay-amount').value);
    const comment = container.querySelector('#pay-comment').value.trim();
    const status = container.querySelector('#pay-status');
    const submit = container.querySelector('#pay-submit');

    if (!amount || amount <= 0) {
      status.textContent = '❌ Введите сумму';
      status.className = 'pay-status pay-error';
      return;
    }
    if (!comment) {
      status.textContent = '❌ Укажите комментарий';
      status.className = 'pay-status pay-error';
      return;
    }

    submit.disabled = true;
    status.textContent = '⏳ Отправка…';
    status.className = 'pay-status';

    try {
      const response = await fetch('/api/payments/send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          initData: _initData,
          amount, currency: selectedCurrency, comment,
        }),
      });
      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Ошибка');
      }

      tg.HapticFeedback?.notificationOccurred('success');
      status.textContent = '✅ Платёж отправлен на подтверждение';
      status.className = 'pay-status pay-ok';
      container.querySelector('#pay-amount').value = '';
      container.querySelector('#pay-comment').value = '';
      paymentsCache = null;
      setTimeout(() => renderPayments(container), 1500);
    } catch (e) {
      status.textContent = '❌ ' + e.message;
      status.className = 'pay-status pay-error';
    } finally {
      submit.disabled = false;
    }
  });

  // Подтверждение/отклонение оплаты по paid-заказам (блок «На подтверждение»).
  // Реюз тех же эндпоинтов, что и в «Долги».
  container.querySelectorAll('.btn-confirm-pay').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = parseInt(btn.dataset.id);
      tg.showConfirm('Подтверждаете поступление оплаты по этому заказу?', async ok => {
        if (!ok) return;
        btn.disabled = true;
        try {
          await api('/api/orders/confirm_payment', { order_id: id });
          tg.HapticFeedback?.notificationOccurred('success');
          await renderPayments(container);
        } catch (e) {
          tg.HapticFeedback?.notificationOccurred('error');
          tg.showAlert('❌ ' + e.message);
          btn.disabled = false;
        }
      });
    });
  });
  container.querySelectorAll('.btn-reject-pay').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = parseInt(btn.dataset.id);
      tg.showConfirm('Отклонить оплату по этому заказу?', async ok => {
        if (!ok) return;
        btn.disabled = true;
        try {
          await api('/api/orders/reject_payment', { order_id: id });
          tg.HapticFeedback?.notificationOccurred('warning');
          await renderPayments(container);
        } catch (e) {
          tg.HapticFeedback?.notificationOccurred('error');
          tg.showAlert('❌ ' + e.message);
          btn.disabled = false;
        }
      });
    });
  });
}

function initNav() {
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic();
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      showScreen(btn.dataset.screen);
    });
  });
}


// ─── Экран «Финансы» (объединённые «Долги» + «Платежи») ────────────────────
//
// Один таб в нижнем меню, две подвкладки. Сделано чтобы:
//   1) не плодить 5 кнопок в bottom-nav (визуально переносило)
//   2) долги и платежи семантически связаны: закрытие долга
//      создаёт payment-запись, которая проходит через тот же
//      approve-флоу
//
// Состояния долгов (state с бекенда):
//   - overdue                — просрочен, оплат нет
//   - due_today              — сегодня к оплате, оплат нет
//   - upcoming               — будущий срок, оплат нет
//   - partial                — есть подтверждённые платежи, но не всё
//   - awaiting_confirmation  — есть pending платежи (босс решает)

let financeTab = 'debts';  // 'debts' | 'payments'
let debtsFilter = 'all';   // 'all' | 'today'

async function renderFinance() {
  const content = document.getElementById('content');
  const role = currentUser && currentUser.role;
  const isBoss = role === 'admin' || role === 'boss';
  // Вкладка «Лимиты» — только начальству (эндпоинт всё равно отдаст 403 другим).
  const limitsTab = isBoss
    ? `<button class="finance-tab ${financeTab === 'limits' ? 'active' : ''}" data-tab="limits">📊 Лимиты</button>`
    : '';
  // Вкладка «Касса» — подтверждающие (сдачи/возвраты) и менеджеры (сдают наличные).
  const canCashbox = isBoss || role === 'bookkeeper' || role === 'warehouse_keeper' || role === 'manager';
  const cashboxTab = canCashbox
    ? `<button class="finance-tab ${financeTab === 'cashbox' ? 'active' : ''}" data-tab="cashbox">🧾 Касса</button>`
    : '';
  if (financeTab === 'limits' && !isBoss) financeTab = 'debts';
  if (financeTab === 'cashbox' && !canCashbox) financeTab = 'debts';
  content.innerHTML = `
    <div class="finance-tabs">
      <button class="finance-tab ${financeTab === 'debts' ? 'active' : ''}" data-tab="debts">💳 Долги</button>
      <button class="finance-tab ${financeTab === 'payments' ? 'active' : ''}" data-tab="payments">💵 Платежи</button>
      ${cashboxTab}
      ${limitsTab}
    </div>
    <div id="finance-body"></div>
  `;
  // Переключение подвкладок без перезагрузки header
  document.querySelectorAll('.finance-tab').forEach(t => {
    t.addEventListener('click', () => {
      financeTab = t.dataset.tab;
      renderFinance();
    });
  });
  // Контент подгружаем в #finance-body
  const body = document.getElementById('finance-body');
  if (financeTab === 'debts') {
    await renderDebts(body);
  } else if (financeTab === 'limits') {
    await renderCreditLimits(body);
  } else if (financeTab === 'cashbox') {
    await renderCashbox(body);
  } else {
    await renderPayments(body);
  }
}

async function renderCashbox(container) {
  container = container || document.getElementById('content');
  container.innerHTML = loading('Загрузка кассы…');
  const fmt = n => Math.round(n).toLocaleString('ru-RU');

  // Каждый список может вернуть 403 (роль не видит) — тихо пропускаем.
  let deposits = [];
  let returns = [];
  let myDeposits = null;  // null = роль не может сдавать (нет блока «Сдать наличные»)
  try { deposits = (await api('/api/deposits/pending', {})).deposits || []; } catch {}
  try { returns = (await api('/api/returns/pending', {})).returns || []; } catch {}
  try { myDeposits = (await api('/api/deposits/my', {})).deposits || []; } catch {}

  const depCards = deposits.map(d => {
    const orders = (d.orders || [])
      .map(o => `#${o.order_id} — ${fmt(o.amount_allocated)} USD`).join(', ') || '—';
    return `
      <div class="debt-card" data-dep="${d.id}">
        <div class="debt-card-top">
          <div class="debt-agent">💵 Сдача #${d.id}</div>
          <div class="debt-amount">${fmt(d.amount)} USD</div>
        </div>
        <div class="debt-card-mid"><span class="debt-meta">Заказы: ${escapeHtml(orders)}</span></div>
        <div class="debt-actions">
          <button class="btn-confirm-pay dep-confirm">✅ Подтвердить</button>
          <button class="btn-reject-pay dep-reject">❌ Отклонить</button>
        </div>
        <div class="limit-edit dep-reject-box" hidden>
          <input type="text" class="form-input dep-reason" placeholder="Причина отклонения">
          <button class="btn-reject-pay dep-reject-send">Отклонить сдачу</button>
        </div>
      </div>
    `;
  }).join('');

  const retCards = returns.map(r => `
      <div class="debt-card" data-ret="${r.id}">
        <div class="debt-card-top">
          <div class="debt-agent">↩️ Возврат #${r.id}</div>
          <div class="debt-amount">${fmt(r.total_amount)} USD</div>
        </div>
        <div class="debt-card-mid">
          <span class="debt-meta">Заказ #${r.order_id} · ${escapeHtml(r.reason || '')}</span>
        </div>
        <div class="debt-actions">
          <button class="btn-confirm-pay ret-confirm">✅ Подтвердить возврат</button>
        </div>
      </div>
  `).join('');

  const depBlock = deposits.length
    ? `<div class="section-label">💵 Сдачи на подтверждении (${deposits.length})</div><div class="debts-list">${depCards}</div>`
    : '';
  const retBlock = returns.length
    ? `<div class="section-label">↩️ Возвраты на подтверждении (${returns.length})</div><div class="debts-list">${retCards}</div>`
    : '';

  // Блок менеджера: сдать наличные + свои сдачи (если роль может сдавать).
  let createBlock = '';
  let myBlock = '';
  if (myDeposits !== null) {
    createBlock = `
      <div class="section-label">Сдать наличные</div>
      <div class="card">
        <div class="form-row">
          <label class="form-label">Сумма (USD)</label>
          <input type="number" id="dep-amount" class="form-input" placeholder="500" inputmode="decimal">
        </div>
        <button id="dep-create" class="btn-primary">💵 Сдать в кассу</button>
        <div class="debt-hint">Распределится по вашим открытым заказам автоматически.</div>
      </div>
    `;
    const stEmoji = { pending: '⏳', confirmed: '✅', rejected: '❌' };
    const rows = myDeposits.map(d => `
      <div class="stock-row">
        <div class="stock-info">
          <div class="stock-name">${stEmoji[d.status] || '•'} #${d.id} — ${fmt(d.amount)} USD</div>
          <div class="stock-folder">${(d.created_at || '').slice(0, 16)}${d.status === 'rejected' && d.reject_reason ? ' · ' + escapeHtml(d.reject_reason) : ''}</div>
        </div>
      </div>
    `).join('');
    myBlock = myDeposits.length
      ? `<div class="section-label">Мои сдачи</div><div class="stock-list">${rows}</div>`
      : '';
  }

  // Блок оформления возврата (менеджер/кладовщик/босс).
  const role = currentUser && currentUser.role;
  const canReturn = ['admin', 'boss', 'warehouse_keeper', 'manager'].includes(role);
  const returnBlock = canReturn ? `
      <div class="section-label">Оформить возврат</div>
      <div class="card">
        <div class="form-row">
          <label class="form-label">Номер заказа</label>
          <input type="number" id="ret-order" class="form-input" placeholder="142" inputmode="numeric">
        </div>
        <div class="form-row">
          <label class="form-label">Причина</label>
          <input type="text" id="ret-reason" class="form-input" placeholder="Брак партии">
        </div>
        <div class="form-row">
          <label class="form-label">Возврат денег</label>
          <div class="cur-row">
            <button class="cur-btn active" data-refund="debt_reduction">📉 В счёт долга</button>
            <button class="cur-btn" data-refund="cash">💵 Наличными</button>
            <button class="cur-btn" data-refund="no_refund">🚫 Без возврата</button>
          </div>
        </div>
        <button id="ret-create" class="btn-primary">↩️ Оформить полный возврат</button>
      </div>
  ` : '';

  const confirmBlocks = depBlock + retBlock;
  container.innerHTML = createBlock + returnBlock + myBlock + confirmBlocks
    || '<div class="loader">Нет записей на подтверждении</div>';

  // Оформление возврата.
  let selectedRefund = 'debt_reduction';
  container.querySelectorAll('[data-refund]').forEach(b => {
    b.addEventListener('click', () => {
      container.querySelectorAll('[data-refund]').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      selectedRefund = b.dataset.refund;
    });
  });
  const retBtn = container.querySelector('#ret-create');
  if (retBtn) {
    retBtn.addEventListener('click', () => {
      const orderId = parseInt(container.querySelector('#ret-order').value, 10);
      const reason = container.querySelector('#ret-reason').value.trim();
      if (!orderId) { tg.showAlert('❌ Укажите номер заказа'); return; }
      if (reason.length < 3) { tg.showAlert('❌ Опишите причину'); return; }
      haptic('light');
      retBtn.disabled = true;
      api('/api/returns/create', { order_id: orderId, reason, refund_method: selectedRefund })
        .then(r => { tg.showAlert(`✅ Возврат #${r.return_id} отправлен на подтверждение`); renderCashbox(container); })
        .catch(e => { tg.showAlert('❌ ' + e.message); retBtn.disabled = false; });
    });
  }

  // Создание сдачи (менеджер).
  const createBtn = container.querySelector('#dep-create');
  if (createBtn) {
    createBtn.addEventListener('click', () => {
      const raw = container.querySelector('#dep-amount').value;
      const amount = parseFloat(String(raw).replace(',', '.').replace(/\s/g, ''));
      if (isNaN(amount) || amount <= 0) { tg.showAlert('❌ Введите положительную сумму'); return; }
      haptic('light');
      createBtn.disabled = true;
      api('/api/deposits/create', { amount })
        .then(r => { tg.showAlert(`✅ Сдача #${r.deposit_id} отправлена на подтверждение`); renderCashbox(container); })
        .catch(e => { tg.showAlert('❌ ' + e.message); createBtn.disabled = false; });
    });
  }

  // Сдачи: подтвердить / отклонить (причина — inline).
  container.querySelectorAll('.debt-card[data-dep]').forEach(card => {
    const id = card.dataset.dep;
    card.querySelector('.dep-confirm').addEventListener('click', () => {
      haptic('light');
      api('/api/deposits/confirm', { deposit_id: Number(id) })
        .then(() => { tg.showAlert('✅ Сдача подтверждена'); renderCashbox(container); })
        .catch(e => tg.showAlert('❌ ' + e.message));
    });
    const box = card.querySelector('.dep-reject-box');
    card.querySelector('.dep-reject').addEventListener('click', () => { box.hidden = !box.hidden; });
    card.querySelector('.dep-reject-send').addEventListener('click', () => {
      const reason = card.querySelector('.dep-reason').value.trim();
      if (reason.length < 3) { tg.showAlert('❌ Укажите причину'); return; }
      api('/api/deposits/reject', { deposit_id: Number(id), reason })
        .then(() => { tg.showAlert('❌ Сдача отклонена'); renderCashbox(container); })
        .catch(e => tg.showAlert('❌ ' + e.message));
    });
  });

  // Возвраты: подтвердить.
  container.querySelectorAll('.debt-card[data-ret]').forEach(card => {
    card.querySelector('.ret-confirm').addEventListener('click', () => {
      haptic('light');
      api('/api/returns/confirm', { return_id: Number(card.dataset.ret) })
        .then(() => { tg.showAlert('✅ Возврат подтверждён'); renderCashbox(container); })
        .catch(e => tg.showAlert('❌ ' + e.message));
    });
  });
}

async function renderCreditLimits(container) {
  container = container || document.getElementById('content');
  container.innerHTML = loading('Загрузка лимитов…');
  let agents = [];
  try {
    const data = await api('/api/credit/overview', {});
    agents = data.agents || [];
  } catch (e) {
    container.innerHTML = `<div class="error">❌ ${e.message}</div>`;
    return;
  }

  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  if (agents.length === 0) {
    container.innerHTML = '<div class="loader">Нет контрагентов с активными заказами</div>';
    return;
  }

  const cards = agents.map(a => {
    const badge = a.over_limit
      ? '<span class="stock-badge badge-red">превышен</span>'
      : '<span class="stock-badge badge-green">в норме</span>';
    return `
      <div class="debt-card" data-agent="${escapeHtml(a.agent_id)}" data-name="${escapeHtml(a.agent_name)}" data-limit="${a.limit}">
        <div class="debt-card-top">
          <div class="debt-agent">🏢 ${escapeHtml(a.agent_name)}</div>
          ${badge}
        </div>
        <div class="debt-card-mid">
          <span class="debt-meta">лимит ${fmt(a.limit)} · долг ${fmt(a.debt)} · свободно ${fmt(a.free)} USD</span>
        </div>
        <div class="debt-actions">
          <button class="btn-edit-limit">✏️ Изменить лимит</button>
        </div>
        <div class="limit-edit" hidden>
          <input type="number" class="form-input limit-input" inputmode="decimal" value="${a.limit}">
          <div class="debt-actions">
            <button class="btn-confirm-pay limit-save">Сохранить</button>
            <button class="btn-reject-pay limit-cancel">Отмена</button>
          </div>
        </div>
      </div>
    `;
  }).join('');

  container.innerHTML = `<div class="section-label">Кредитные лимиты</div><div class="debts-list">${cards}</div>`;

  // Inline-редактирование: prompt() в Telegram WebApp ненадёжен, поэтому
  // показываем поле ввода прямо в карточке.
  container.querySelectorAll('.debt-card').forEach(card => {
    const editBox = card.querySelector('.limit-edit');
    const editBtn = card.querySelector('.btn-edit-limit');
    editBtn.addEventListener('click', () => {
      haptic('light');
      editBox.hidden = !editBox.hidden;
    });
    card.querySelector('.limit-cancel').addEventListener('click', () => { editBox.hidden = true; });
    card.querySelector('.limit-save').addEventListener('click', () => {
      const raw = card.querySelector('.limit-input').value;
      const amount = parseFloat(String(raw).replace(',', '.').replace(/\s/g, ''));
      if (isNaN(amount) || amount < 0) {
        tg.showAlert('❌ Лимит должен быть неотрицательным числом.');
        return;
      }
      api('/api/credit/set', {
        agent_id: card.dataset.agent,
        agent_name: card.dataset.name,
        limit_amount: amount,
      })
        .then(() => { tg.showAlert(`✅ Лимит обновлён: ${fmt(amount)} USD`); renderCreditLimits(container); })
        .catch(e => tg.showAlert('❌ ' + e.message));
    });
  });
}

async function renderDebts(container) {
  container = container || document.getElementById('content');
  container.innerHTML = loading('Загрузка долгов…');
  try {
    const data = await api('/api/debts', { mode: debtsFilter });
    const debts = data.debts || [];
    const isBoss = data.role === 'boss' || data.role === 'admin';
    const scopeLabel = data.scope === 'company' ? 'Все долги' : 'Мои долги';
    const today = data.today;

    // Разделяем долги по состояниям. partial — в основном списке.
    const awaiting = debts.filter(d => d.state === 'awaiting_confirmation');
    const open = debts.filter(d => d.state !== 'awaiting_confirmation');

    // Сводка по open-долгам — по remaining (а не total), это честнее
    // показывает «сколько ещё должны»: частично оплаченный — меньше.
    let overdueCount = 0, todayCount = 0, upcomingCount = 0;
    let overdueSum = 0, todaySum = 0, upcomingSum = 0;
    for (const d of open) {
      const amt = d.remaining > 0 ? d.remaining : d.total;
      if (d.state === 'overdue') { overdueCount++; overdueSum += amt; }
      else if (d.state === 'due_today') { todayCount++; todaySum += amt; }
      else { upcomingCount++; upcomingSum += amt; }
    }
    const fmt = n => Math.round(n).toLocaleString('ru-RU');

    // Сводка «получено» / «ожидает подтверждения» по валютам
    const receivedItems = (data.money_received || []).filter(x => x.total > 0);
    const pendingItems  = (data.money_pending  || []).filter(x => x.total > 0);
    const receivedRows = receivedItems
      .map(x => `${fmt(x.total)} ${escapeHtml(x.currency)}`).join(' · ');
    const pendingRows = pendingItems
      .map(x => `${fmt(x.total)} ${escapeHtml(x.currency)}`).join(' · ');

    // ─── Полностью пустое состояние ───────────────────────────────
    // Когда нет ни долгов, ни подтверждённых платежей, ни ожидающих —
    // экран выглядел «полузагруженным» (две карточки с тоненькими «—»
    // и три нолика). Заменяем на единственный дружелюбный блок.
    const totalEmpty = (
      debts.length === 0 &&
      receivedItems.length === 0 &&
      pendingItems.length === 0
    );

    let html = `
      <div class="debts-header">
        <div class="debts-tabs">
          <button class="debts-tab ${debtsFilter === 'all' ? 'active' : ''}" data-f="all">Все</button>
          <button class="debts-tab ${debtsFilter === 'today' ? 'active' : ''}" data-f="today">К оплате сейчас</button>
        </div>
      </div>
    `;

    if (totalEmpty) {
      html += `
        <div class="finance-empty">
          <div class="finance-empty-icon">💳</div>
          <div class="finance-empty-title">Долгов и платежей пока нет</div>
          <div class="finance-empty-hint">
            Когда менеджер оформит заказ «в долг» — он появится здесь.
            А подтверждённые поступления попадут в сводку.
          </div>
        </div>
      `;
    } else {
      // Если есть хоть что-то — показываем money-блоки, но компактно.
      html += `
        <div class="money-summary">
          <div class="money-block money-received ${receivedItems.length === 0 ? 'money-empty' : ''}">
            <div class="money-label">💵 Получено</div>
            <div class="money-value">${receivedRows || '<span class="money-placeholder">пока пусто</span>'}</div>
          </div>
          <div class="money-block money-pending ${pendingItems.length === 0 ? 'money-empty' : ''}">
            <div class="money-label">⏳ Ждёт подтверждения</div>
            <div class="money-value">${pendingRows || '<span class="money-placeholder">пусто</span>'}</div>
          </div>
        </div>
      `;
      // Stat-плитки показываем только если хоть один счётчик не ноль,
      // иначе три «0 0 0» лишь засоряют экран.
      const hasAnyStat = overdueCount + todayCount + upcomingCount > 0;
      if (hasAnyStat) {
        html += `
          <div class="debts-summary">
            <div class="debt-stat debt-stat-overdue">
              <div class="debt-stat-num">${overdueCount}</div>
              <div class="debt-stat-label">Просрочено</div>
              <div class="debt-stat-sum">${fmt(overdueSum)}</div>
            </div>
            <div class="debt-stat debt-stat-today">
              <div class="debt-stat-num">${todayCount}</div>
              <div class="debt-stat-label">Сегодня</div>
              <div class="debt-stat-sum">${fmt(todaySum)}</div>
            </div>
            ${debtsFilter === 'all' ? `
            <div class="debt-stat debt-stat-upcoming">
              <div class="debt-stat-num">${upcomingCount}</div>
              <div class="debt-stat-label">Будущие</div>
              <div class="debt-stat-sum">${fmt(upcomingSum)}</div>
            </div>
            ` : ''}
          </div>
        `;
      }
    }

    // ─── Блок «На подтверждении» ──────────────────────────────────
    if (awaiting.length > 0) {
      html += `
        <div class="section-label section-awaiting">⏳ Ожидают подтверждения (${awaiting.length})</div>
        <div class="debts-list">${awaiting.map(d => {
          const ownerStr = d.is_mine ? '' : ` <span class="debt-owner">· ${escapeHtml(d.full_name)}</span>`;
          // Покажем разбиение: оплачено / в подтверждении / остаток
          const breakdown = `
            <div class="debt-breakdown">
              ${d.confirmed > 0 ? `<span>✅ Подтверждено: <b>${fmt(d.confirmed)}</b></span>` : ''}
              <span>⏳ Ждёт: <b>${fmt(d.pending)}</b></span>
              ${d.remaining > 0 ? `<span>📎 Останется: <b>${fmt(d.remaining - d.pending > 0 ? d.remaining - d.pending : 0)}</b></span>` : ''}
            </div>
          `;
          return `
            <div class="debt-card debt-awaiting">
              <div class="debt-card-top">
                <div class="debt-agent">🏢 ${escapeHtml(d.agent_name)}</div>
                <div class="debt-amount">${fmt(d.total)} ${escapeHtml(d.currency)}</div>
              </div>
              ${breakdown}
              <div class="debt-card-mid">
                <span class="debt-meta">#${d.id} · ${d.items_count} поз.${ownerStr}</span>
              </div>
              ${isBoss ? `
                <div class="debt-actions">
                  <button class="btn-confirm-pay" data-id="${d.id}">✅ Подтвердить</button>
                  <button class="btn-reject-pay"  data-id="${d.id}">❌ Отклонить</button>
                </div>
              ` : `
                <div class="debt-hint">Босс должен подтвердить</div>
              `}
            </div>
          `;
        }).join('')}</div>
      `;
    }

    // ─── Открытые долги (с partial — частично оплаченные тоже здесь) ─
    if (open.length === 0 && awaiting.length === 0 && !totalEmpty) {
      // Долгов нет, но есть деньги получено/ожидается — отдельно скажем
      html += '<div class="empty">Открытых долгов нет — все деньги собраны 🎉</div>';
    } else if (open.length > 0) {
      html += `<div class="section-label">💳 Открытые (${open.length})</div>`;
      html += '<div class="debts-list">' + open.map(d => {
        const stateClass = `debt-${d.state}`;
        const stateLabel = d.state === 'partial' ? '🟡 Частично оплачен'
          : d.state === 'overdue' ? '⚠️ Просрочен'
          : d.state === 'due_today' ? '⏰ Сегодня' : '📅 Срок';
        const dueStr = d.due_date ? formatDateRU(d.due_date) : '—';
        const ownerStr = d.is_mine ? '' : ` <span class="debt-owner">· ${escapeHtml(d.full_name)}</span>`;
        // Для partial показываем сколько уже получено и остаток
        const breakdown = d.confirmed > 0 ? `
          <div class="debt-breakdown">
            <span>✅ Оплачено: <b>${fmt(d.confirmed)}</b></span>
            <span>📎 Остаток: <b>${fmt(d.remaining)}</b> ${escapeHtml(d.currency)}</span>
          </div>
        ` : '';
        return `
          <div class="debt-card ${stateClass}">
            <div class="debt-card-top">
              <div class="debt-agent">🏢 ${escapeHtml(d.agent_name)}</div>
              <div class="debt-amount">${fmt(d.total)} ${escapeHtml(d.currency)}</div>
            </div>
            ${breakdown}
            <div class="debt-card-mid">
              <span class="debt-state">${stateLabel}: <b>${dueStr}</b></span>
              <span class="debt-meta">#${d.id} · ${d.items_count} поз.${ownerStr}</span>
            </div>
            ${d.is_mine || isBoss ? `
              <div class="pay-input-row">
                <input type="number" class="pay-amount-input" data-id="${d.id}"
                       placeholder="Сумма · ост. ${fmt(d.remaining)}"
                       min="0" step="0.01" inputmode="decimal">
                <button class="btn-mark-paid" data-id="${d.id}">✅ Отметить</button>
              </div>
            ` : ''}
          </div>
        `;
      }).join('') + '</div>';
    }

    container.innerHTML = html;

    // Tabs (фильтр all/today)
    container.querySelectorAll('.debts-tab').forEach(t => {
      t.addEventListener('click', () => {
        debtsFilter = t.dataset.f;
        renderDebts(container);
      });
    });

    // Mark-paid (менеджер отмечает оплату; amount опционален —
    // если пусто, закрывает остаток целиком)
    container.querySelectorAll('.btn-mark-paid').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = parseInt(btn.dataset.id);
        const input = container.querySelector(`.pay-amount-input[data-id="${id}"]`);
        const amountRaw = input?.value.trim();
        const amount = amountRaw ? parseFloat(amountRaw) : null;
        if (amount !== null && (!(amount > 0))) {
          tg.showAlert('Сумма должна быть больше нуля или пустой (по умолч. весь остаток)');
          return;
        }
        const msg = amount === null
          ? 'Отметить полную оплату остатка?\\nБосс должен будет подтвердить.'
          : `Отметить получение ${amount}?\\nБосс должен будет подтвердить.`;
        tg.showConfirm(msg, async ok => {
          if (!ok) return;
          btn.disabled = true;
          try {
            const payload = { order_id: id };
            if (amount !== null) payload.amount = amount;
            await api('/api/orders/mark_paid', payload);
            tg.HapticFeedback?.notificationOccurred('success');
            await renderDebts(container);
          } catch (e) {
            tg.HapticFeedback?.notificationOccurred('error');
            tg.showAlert('❌ ' + e.message);
            btn.disabled = false;
          }
        });
      });
    });

    // Confirm-payment (босс подтверждает все pending по заказу)
    container.querySelectorAll('.btn-confirm-pay').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = parseInt(btn.dataset.id);
        tg.showConfirm('Подтверждаете все ожидающие платежи по этому заказу?', async ok => {
          if (!ok) return;
          btn.disabled = true;
          try {
            await api('/api/orders/confirm_payment', { order_id: id });
            tg.HapticFeedback?.notificationOccurred('success');
            await renderDebts(container);
          } catch (e) {
            tg.HapticFeedback?.notificationOccurred('error');
            tg.showAlert('❌ ' + e.message);
            btn.disabled = false;
          }
        });
      });
    });

    // Reject-payment (босс отклоняет все pending по заказу)
    container.querySelectorAll('.btn-reject-pay').forEach(btn => {
      btn.addEventListener('click', async () => {
        const id = parseInt(btn.dataset.id);
        tg.showConfirm('Отклонить ожидающие платежи? Долг останется в той сумме, что была до отметки.', async ok => {
          if (!ok) return;
          btn.disabled = true;
          try {
            await api('/api/orders/reject_payment', { order_id: id });
            tg.HapticFeedback?.notificationOccurred('warning');
            await renderDebts(container);
          } catch (e) {
            tg.HapticFeedback?.notificationOccurred('error');
            tg.showAlert('❌ ' + e.message);
            btn.disabled = false;
          }
        });
      });
    });
  } catch (e) {
    container.innerHTML = `<div class="error">❌ ${e.message || e}</div>`;
  }
}

// Маленький хелпер — дата YYYY-MM-DD → ДД.ММ.ГГГГ
function formatDateRU(iso) {
  if (!iso || iso.length < 10) return iso || '—';
  const [y, m, d] = iso.slice(0, 10).split('-');
  return `${d}.${m}.${y}`;
}


// Тень топбара при прокрутке
window.addEventListener('scroll', () => {
  document.querySelector('.topbar')?.classList.toggle('topbar--shadow', window.scrollY > 4);
}, { passive: true });

// Запуск
init()