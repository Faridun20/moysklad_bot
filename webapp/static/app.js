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

function showScreen(screen) {
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
      content.innerHTML = `<div class="loader">📦 Раздел "Склад" — скоро здесь</div>`;
      break;
    case 'analytics':
      content.innerHTML = `<div class="loader">📊 Раздел "Аналитика" — скоро здесь</div>`;
      break;
    case 'payments':
      content.innerHTML = `<div class="loader">💵 Раздел "Платежи" — скоро здесь</div>`;
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

// Запуск
init();
