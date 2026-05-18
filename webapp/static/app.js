// Telegram WebApp SDK
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

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
    const response = await fetch('/api/me', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: tg.initData }),
    });

    if (!response.ok) {
      throw new Error('Ошибка авторизации');
    }

    currentUser = await response.json();
    renderHeader();
    initNav();
    showScreen('home');
  } catch (e) {
    showError('❌ Не удалось подключиться: ' + e.message);
  }
}

function renderHeader() {
  const greeting = document.getElementById('greeting');
  const badge = document.getElementById('role-badge');
  greeting.textContent = `Привет, ${currentUser.first_name}`;
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

  switch (screen) {
    case 'home':
      content.innerHTML = renderHome();
      break;
    case 'stock':
      await renderStock();
      break;
   case 'orders':
      ordersData = null;
      await renderOrders();
      break;
    case 'analytics':
      await renderAnalytics();
      break;
    case 'payments':
      await renderPayments();
      break;
  }
}

function renderHome() {
  return `
    <div class="section-label">Сводка</div>
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-value">—</div>
        <div class="stat-label">Выручка</div>
      </div>
      <div class="stat">
        <div class="stat-value">—</div>
        <div class="stat-label">Отгрузок</div>
      </div>
    </div>

    <div class="section-label">Информация</div>
    <div class="card">
      <p style="margin-bottom: 8px;">
        ✅ <b>WebApp подключён успешно</b>
      </p>
      <p style="font-size: 12px; color: #888;">
        ID: ${currentUser.user_id}<br>
        Роль: ${currentUser.role}
      </p>
    </div>
  `;
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
    body: JSON.stringify({ initData: tg.initData, ...body }),
  });
  if (!r.ok) throw new Error((await r.json()).detail || 'Ошибка');
  return r.json();
}

async function renderOrders() {
  const content = document.getElementById('content');
  content.innerHTML = `<div class="loader">⏳ Загружаю заказы…</div>`;
  try {
    ordersData = await api('/api/orders', {});
  } catch (e) {
    content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
    return;
  }
  renderOrdersMain();
}

function renderOrdersMain() {
  const content = document.getElementById('content');
  const { orders, role } = ordersData;
  const isBoss = role === 'admin' || role === 'boss';

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
    ? '<div class="loader">Нет заказов</div>'
    : filtered.map(o => `
      <div class="order-card" data-id="${o.id}">
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
        </div>
        ${o.items.slice(0, 2).map(it =>
          `<div class="order-item-preview">• ${it.name} — ${it.quantity} ${it.unit}</div>`
        ).join('')}
        ${o.status === 'draft' && !isBoss
          ? `<button class="btn-edit-order" data-id="${o.id}">✏️ Редактировать</button>`
          : ''}
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

  // Заявки для босса
  document.getElementById('show-requests')?.addEventListener('click', renderPendingRequests);
}


// ─── Редактор заказа ────────────────────────────────

async function openOrderEditor(orderId) {
  const content = document.getElementById('content');

  // Создаём черновик если новый
  if (!orderId) {
    content.innerHTML = `<div class="loader">⏳ Создаю заказ…</div>`;
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

  const itemsList = order.items.length === 0
    ? '<div class="editor-empty">Товары не добавлены</div>'
    : order.items.map((it, i) => `
        <div class="editor-item">
          <div class="editor-item-info">
            <div class="editor-item-name">${it.name}</div>
            <div class="editor-item-qty">${it.quantity} ${it.unit || 'шт'}</div>
          </div>
          <button class="editor-item-del" data-idx="${i}">✕</button>
        </div>
      `).join('');

  content.innerHTML = `
    <div class="editor-header">
      <button class="editor-back" id="editor-back">◀️</button>
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

  // Назад
  document.getElementById('editor-back').addEventListener('click', () => {
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

  // Отправить заявку
  document.getElementById('btn-submit').addEventListener('click', submitOrder);
}


// ─── Поиск клиента ──────────────────────────────────

async function openAgentSearch() {
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="editor-header">
      <button id="agent-back">◀️</button>
      <div class="editor-title">Выбор клиента</div>
    </div>
    <div class="agent-search-wrap">
      <input type="text" id="agent-search" class="form-input" placeholder="🔍 Поиск по имени…">
    </div>
    <div id="agent-list" class="orders-list">
      <div class="loader">Введите имя для поиска</div>
    </div>
  `;

  document.getElementById('agent-back').addEventListener('click', renderOrderEditor);

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
  list.innerHTML = `<div class="loader">⏳ Загружаю…</div>`;
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

let stockCache = null;

async function openProductPicker() {
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="editor-header">
      <button id="prod-back">◀️</button>
      <div class="editor-title">Выбор товара</div>
    </div>
    <div class="agent-search-wrap">
      <input type="text" id="prod-search" class="form-input" placeholder="🔍 Поиск товара…">
    </div>
    <div id="cat-filters" class="cat-scroll"></div>
    <div id="prod-list" class="orders-list">
      <div class="loader">⏳ Загружаю…</div>
    </div>
  `;

  document.getElementById('prod-back').addEventListener('click', renderOrderEditor);

  // Загружаем склад
  if (!stockCache) {
    try {
      const data = await api('/api/stock', {});
      stockCache = data;
    } catch (e) {
      document.getElementById('prod-list').innerHTML = `<div class="error">❌ ${e.message}</div>`;
      return;
    }
  }

  let selectedCat = 'all';
  const { products, categories } = stockCache;

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
            <div class="prod-row" data-name="${p.name}" data-unit="${p.unit}" data-stock="${p.stock}">
              <div class="prod-info">
                <div class="prod-name">${p.name}</div>
                ${p.folder_name ? `<div class="prod-folder">${p.folder_name}</div>` : ''}
              </div>
              <span class="stock-badge badge-${ind}">${p.stock} ${p.unit}</span>
            </div>
          `;
        }).join('');

    document.querySelectorAll('.prod-row').forEach(row => {
      row.addEventListener('click', () => openQuantityInput(
        row.dataset.name, row.dataset.unit, parseFloat(row.dataset.stock)
      ));
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
      catFilters.querySelectorAll('.cat-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedCat = btn.dataset.cat;
      renderProducts();
    });
  });

  document.getElementById('prod-search').addEventListener('input', renderProducts);
  renderProducts();
}

function openQuantityInput(name, unit, maxStock) {
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="editor-header">
      <button id="qty-back">◀️</button>
      <div class="editor-title">Количество</div>
    </div>
    <div class="qty-screen">
      <div class="qty-product-name">${name}</div>
      <div class="qty-stock">На складе: ${maxStock} ${unit}</div>
      <input type="number" id="qty-input" class="qty-input"
        placeholder="0" inputmode="decimal" min="0.1" step="0.1">
      <div class="qty-unit">${unit}</div>
      <button class="btn-primary" id="qty-confirm">✅ Добавить</button>
    </div>
  `;

  document.getElementById('qty-back').addEventListener('click', openProductPicker);
  document.getElementById('qty-input').focus();

  document.getElementById('qty-confirm').addEventListener('click', async () => {
    const qty = parseFloat(document.getElementById('qty-input').value);
    if (!qty || qty <= 0) {
      tg.HapticFeedback?.notificationOccurred('error');
      return;
    }

    try {
      const result = await api('/api/orders/add_item', {
        order_id: currentDraftOrder.id,
        product_name: name,
        product_href: '',
        quantity: qty,
        unit: unit,
      });
      currentDraftOrder.items.push({
        name, quantity: qty, unit, item_id: result.item_id,
      });
      tg.HapticFeedback?.notificationOccurred('success');
      renderOrderEditor();
    } catch (e) {
      tg.showAlert('❌ ' + e.message);
    }
  });
}

async function submitOrder() {
  const btn = document.getElementById('btn-submit');
  if (btn) btn.disabled = true;

  try {
    const result = await api('/api/orders/submit', {
      order_id: currentDraftOrder.id,
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
  content.innerHTML = `<div class="loader">⏳ Загружаю заявки…</div>`;
  try {
    const data = await api('/api/orders/requests', {});
    if (data.requests.length === 0) {
      content.innerHTML = `
        <div class="loader">✅ Нет заявок</div>
        <button class="btn-secondary" id="back-orders" style="margin:16px">◀️ К заказам</button>
      `;
      document.getElementById('back-orders').addEventListener('click', renderOrdersMain);
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
        <button id="req-back">◀️</button>
        <div class="editor-title">Заявки (${data.requests.length})</div>
      </div>
      <div class="orders-list">${items}</div>
    `;

    document.getElementById('req-back').addEventListener('click', renderOrdersMain);
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
  tg.sendData(JSON.stringify({ action: `req_${action === 'approve' ? 'ok' : 'no'}:${reqId}` }));
  tg.showAlert(action === 'approve' ? '✅ Одобрено' : '❌ Отклонено');
  await renderPendingRequests();
}
// ─── Экран: Аналитика ───────────────────────────────

let analyticsCache = {};
let analyticsPeriod = 'month';

async function renderAnalytics() {
  const content = document.getElementById('content');
  content.innerHTML = `<div class="loader">⏳ Считаю статистику…</div>`;

  let data = analyticsCache[analyticsPeriod];
  if (!data) {
    try {
      const response = await fetch('/api/analytics', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData, period: analyticsPeriod }),
      });
      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Ошибка');
      }
      data = await response.json();
      analyticsCache[analyticsPeriod] = data;
    } catch (e) {
      content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
      return;
    }
  }

  renderAnalyticsContent(data);
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
      <div class="bar-bg"><div class="bar-fill" style="width: ${(d.count / maxDay) * 100}%"></div></div>
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

  document.querySelectorAll('.cat-btn[data-period]').forEach(btn => {
    btn.addEventListener('click', () => {
      analyticsPeriod = btn.dataset.period;
      renderAnalytics();
    });
  });
}

// ─── Экран: Платежи ─────────────────────────────────

let paymentsCache = null;

async function renderPayments() {
  const content = document.getElementById('content');
  content.innerHTML = `<div class="loader">⏳ Загружаю историю…</div>`;

  try {
    const response = await fetch('/api/payments/history', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: tg.initData }),
    });
    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.detail || 'Ошибка');
    }
    paymentsCache = await response.json();
  } catch (e) {
    content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
    return;
  }

  renderPaymentsContent();
}

function renderPaymentsContent() {
  const content = document.getElementById('content');
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

  content.innerHTML = `
    <div class="section-label">Новый платёж</div>
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

    <div class="section-label">История</div>
    <div class="stock-list">${history}</div>
  `;

  // Выбор валюты
  let selectedCurrency = 'USD';
  document.querySelectorAll('.cur-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.cur-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedCurrency = btn.dataset.cur;
    });
  });

  // Отправка
  document.getElementById('pay-submit').addEventListener('click', async () => {
    const amount = parseFloat(document.getElementById('pay-amount').value);
    const comment = document.getElementById('pay-comment').value.trim();
    const status = document.getElementById('pay-status');
    const submit = document.getElementById('pay-submit');

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
          initData: tg.initData,
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
      document.getElementById('pay-amount').value = '';
      document.getElementById('pay-comment').value = '';
      paymentsCache = null;
      setTimeout(renderPayments, 1500);
    } catch (e) {
      status.textContent = '❌ ' + e.message;
      status.className = 'pay-status pay-error';
    } finally {
      submit.disabled = false;
    }
  });
}

function initNav() {
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      showScreen(btn.dataset.screen);
    });
  });
}

// Запуск
init()