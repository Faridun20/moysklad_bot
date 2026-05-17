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
  });

  const content = document.getElementById('content');

  switch (screen) {
    case 'home':
      content.innerHTML = renderHome();
      break;
    case 'stock':
      await renderStock();
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

// ─── Обработчики ────────────────────────────────────

document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => showScreen(btn.dataset.screen));
});

// ─── Экран: Склад ───────────────────────────────────

let stockData = null;
let currentCategory = 'all';

async function renderStock() {
  const content = document.getElementById('content');
  content.innerHTML = `<div class="loader">⏳ Загружаю остатки…</div>`;

  if (!stockData) {
    try {
      const response = await fetch('/api/stock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: tg.initData }),
      });
      if (!response.ok) {
        const err = await response.json();
        throw new Error(err.detail || 'Ошибка');
      }
      stockData = await response.json();
    } catch (e) {
      content.innerHTML = `<div class="error">❌ ${e.message}</div>`;
      return;
    }
  }

  renderStockList();
}

function renderStockList() {
  const content = document.getElementById('content');
  const { products, categories } = stockData;

  // Фильтр по категории
  let filtered = products;
  if (currentCategory !== 'all') {
    filtered = products.filter(p => p.folder_id === currentCategory);
  }

  // Кнопки категорий
  const catButtons = [
    `<button class="cat-btn ${currentCategory === 'all' ? 'active' : ''}" data-cat="all">Все</button>`,
    ...categories.map(c =>
      `<button class="cat-btn ${currentCategory === c.id ? 'active' : ''}" data-cat="${c.id}">${c.name}</button>`
    )
  ].join('');

  // Индикатор остатка
  const indicator = stock => {
    if (stock >= 100) return 'green';
    if (stock >= 20) return 'yellow';
    return 'red';
  };

  const items = filtered.map(p => `
    <div class="stock-row">
      <div class="stock-info">
        <div class="stock-name">${p.name}</div>
        ${p.folder_name ? `<div class="stock-folder">${p.folder_name}</div>` : ''}
      </div>
      <div class="stock-badge badge-${indicator(p.stock)}">
        ${p.stock} ${p.unit}
      </div>
    </div>
  `).join('');

  content.innerHTML = `
    <div class="section-label">Категории · ${filtered.length} поз.</div>
    <div class="cat-scroll">${catButtons}</div>
    <div class="stock-list">${items || '<div class="loader">Нет товаров</div>'}</div>
  `;

  // Обработчики на кнопки категорий
  document.querySelectorAll('.cat-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      currentCategory = btn.dataset.cat;
      renderStockList();
    });
  });
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

// Запуск
init();
