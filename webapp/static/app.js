// Telegram WebApp SDK. Гард: вне Telegram (или если telegram-web-app.js не
// загрузился) window.Telegram отсутствует — без заглушки первая же строка
// роняла весь скрипт в белый экран. Остальной код защищён (tg.X && / ?. /
// try-catch / showAlert?…:alert), стаб лишь не даёт упасть на старте и даёт
// рабочие диалоги в обычном браузере.
const tg = (window.Telegram && window.Telegram.WebApp) || {
  ready() {}, expand() {},
  showAlert(m) { try { window.alert(m); } catch (_e) {} },
  showConfirm(m, cb) { try { cb(window.confirm(m)); } catch (_e) { if (cb) cb(false); } },
};
tg.ready();
tg.expand();

// ─── Интеграция темы Telegram (PR E) ────────────────────────────────
// CSS уже использует var(--tg-theme-*), но JS-сторона раньше не
// синхронизировала header/background и не реагировала на смену темы.
// Без этого хедер Telegram (нативный) не совпадал с фоном WebApp,
// а переключение light↔dark на лету не подхватывалось.
function applyTelegramTheme() {
  try {
    // colorScheme → класс на <html> для тёмо-специфичных CSS-правок.
    const scheme = tg.colorScheme || 'light';
    document.documentElement.setAttribute('data-theme', scheme);
    // Хедер и фон WebApp = secondary_bg (наш --bg-page). Методы есть
    // не во всех версиях клиента — оборачиваем в try.
    if (tg.setHeaderColor) tg.setHeaderColor('secondary_bg_color');
    if (tg.setBackgroundColor) {
      const bg = (tg.themeParams && tg.themeParams.secondary_bg_color) || '#f2f2f7';
      tg.setBackgroundColor(bg);
    }
  } catch (_e) { /* старый клиент без theme API — CSS-fallback'и справятся */ }
}
applyTelegramTheme();
// Пользователь сменил тему системы/Telegram, пока WebApp открыт.
tg.onEvent && tg.onEvent('themeChanged', applyTelegramTheme);

// Клавиатурная активация tap-строк: строки-карточки — это div role="button"
// tabindex="0" (не нативные кнопки, чтобы не ломать вёрстку), поэтому Enter/Space
// сами по себе не кликают. Один делегированный хендлер на документ закрывает это
// для скринридеров/клавиатуры разом.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  const el = e.target;
  if (el && el.tagName !== 'BUTTON' && el.getAttribute && el.getAttribute('role') === 'button') {
    e.preventDefault();
    el.click();
  }
});

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
// Базовая валюта (касса/сдачи хранятся в ней — нет currency-колонки). Берём из
// /api/me; показываем её код вместо хардкода «USD».
function baseCur() { return (currentUser && currentUser.base_currency) || 'USD'; }
// RU-раскладка вводит десятичную запятую; parseFloat('1,5')→1. Нормализуем
// запятую и пробелы-разделители тысяч перед разбором (как parsePaymentItems).
function parseNum(v) { return parseFloat(String(v == null ? '' : v).replace(/\s/g, '').replace(',', '.')); }

// Роль-бейдж и так визуально выделен (.role-badge) — эмодзи убраны ради единого
// стиля (иконографика — только SVG-спрайт).
const ROLE_NAMES = {
  admin: 'Админ',
  boss: 'Руководитель',
  manager: 'Менеджер',
  warehouse_keeper: 'Кладовщик',
  bookkeeper: 'Бухгалтер',
  employee: 'Сотрудник',
  guest: 'Гость',
};

// Экран «нет прав»: новый/деактивированный юзер (роль guest — нулевые права).
// Раньше он падал на главную с нерабочими кнопками/403. Прячем нав и поиск.
function renderNoAccess() {
  const nav = document.querySelector('.bottom-nav');
  if (nav) nav.classList.add('hidden');
  const sb = document.getElementById('search-btn');
  if (sb) sb.classList.add('hidden');
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="empty-state" style="padding-top:48px;">
      <div class="empty-state-icon">${icon('lock')}</div>
      <div class="empty-state-title">Доступ не выдан</div>
      <div class="empty-state-hint">Ваш аккаунт пока без прав. Попросите администратора назначить роль — затем откройте приложение снова.</div>
      <button class="btn-primary" style="margin-top:16px;" onclick="location.reload()">Обновить</button>
    </div>`;
}

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
          <div class="error-icon">${icon('lock')}</div>
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
    if (currentUser.role === 'guest') {
      renderNoAccess();
      return;
    }
    initNav();
    showScreen('home');
  } catch (e) {
    document.getElementById('content').innerHTML = `
      <div class="error-card">
        <div class="error-icon">${icon('alert')}</div>
        <div class="error-title">Нет связи</div>
        <div class="error-body">${escapeHtml(e.message)}</div>
        <button class="btn-primary" onclick="location.reload()">Повторить</button>
      </div>`;
  }
}

// Приветствие в шапке (показывается на «Главной»). Сохраняем, чтобы при
// возврате на home восстановить после контекст-подписи раздела.
let _greetingText = 'Добро пожаловать!';

function renderHeader() {
  const greeting = document.getElementById('greeting');
  const badge = document.getElementById('role-badge');
  const name = currentUser.first_name || '';
  _greetingText = name ? `Привет, ${name}!` : 'Добро пожаловать!';
  greeting.textContent = _greetingText;
  badge.textContent = ROLE_NAMES[currentUser.role] || currentUser.role;
}

// Контекст-подпись в шапке: на «Главной» — приветствие, на остальных экранах
// текущий раздел (и под-раздел), чтобы было видно, где находишься.
function setScreenContext(text) {
  const greeting = document.getElementById('greeting');
  if (greeting) greeting.textContent = text || _greetingText;
}

function showError(msg) {
  document.getElementById('content').innerHTML = errorBox(msg);
}

// PR E: единый error-блок с кнопкой «Повторить». Раньше экраны при сбое
// показывали голый текст без способа повторить (кроме ре-навигации).
// Retry перезагружает текущий экран через showScreen(currentScreen).
function errorBox(msg) {
  // Различаем «нет сети» и «ошибка сервера»: при офлайне нет смысла показывать
  // технический detail — даём понятную причину и подсказку. api() при сетевом
  // сбое кидает именно это сообщение; плюс страхуемся navigator.onLine.
  const offline = (typeof navigator !== 'undefined' && navigator.onLine === false)
    || msg === 'Нет подключения к интернету';
  const title = offline ? 'Нет подключения' : 'Не удалось загрузить';
  const body = offline ? 'Проверьте интернет и попробуйте снова.' : escapeHtml(msg);
  return `
    <div class="error-card">
      <div class="error-icon">${icon('alert')}</div>
      <div class="error-title">${title}</div>
      <div class="error-body">${body}</div>
      <button class="btn-primary" onclick="showScreen(currentScreen)">Повторить</button>
    </div>`;
}

// ─── Навигация ──────────────────────────────────────

async function showScreen(screen) {
  currentScreen = screen;
  // Уходя с любого экрана через нав — снимаем «подтвердить закрытие» (его ставит
  // редактор заказа, пока есть несохранённый черновик).
  tg.disableClosingConfirmation && tg.disableClosingConfirmation();

  // Подсветка нижнего таба: вложенные/legacy-экраны принадлежат корневому
  // табу (каталог → «Заказы», долги/платежи → «Финансы»). Иначе при заходе
  // в каталог ни один таб не подсвечивался.
  const navScreen = { stock: 'orders', debts: 'finance', payments: 'finance', ops: 'home' }[screen] || screen;
  document.querySelectorAll('.nav-item').forEach(btn => {
    const isActive = btn.dataset.screen === navScreen;
    btn.classList.toggle('active', isActive);
    // aria-current — активный таб для скринридера (визуально это только цвет).
    if (isActive) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
    // Переинициализируем обработчик на случай если DOM обновился
    btn.onclick = () => showScreen(btn.dataset.screen);
  });

  // Подпись раздела в шапке. «Финансы» уточняет под-раздел внутри renderFinance.
  const SCREEN_TITLES = {
    home: null, orders: 'Заказы и склад', stock: 'Заказы и склад',
    finance: 'Финансы', debts: 'Финансы', payments: 'Финансы', analytics: 'Аналитика',
    ops: 'Операционная сводка',
  };
  setScreenContext(SCREEN_TITLES[screen]);

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
        // ordersData не сбрасываем — TTL-кэш отдаёт свежие данные без рефетча.
        ordersSubTab = 'stock';
        await renderOrdersScreen();
        break;
      case 'orders':
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
      case 'ops':
        await renderOpsSummary();
        break;
      default:
        content.innerHTML = `<div class="error">Неизвестный экран: ${screen}</div>`;
    }
  } catch (e) {
    // Если render упал — показываем ошибку, а не оставляем старый контент
    content.innerHTML = errorBox(e.message || String(e));
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
  // Поверх их вывода — переключатель под-вкладок. Единый стиль с Аналитикой:
  // iOS-сегмент .seg (равные доли, приподнятая активная), а не отдельный .sub-tabs.
  const content = document.getElementById('content');
  const tabsHtml = `
    <div class="seg-row" style="margin-bottom:10px;">
      <div class="seg">
        <button class="seg-item ${ordersSubTab === 'orders' ? 'active' : ''}" data-sub="orders" aria-pressed="${ordersSubTab === 'orders'}">${icon('list')} Заказы</button>
        <button class="seg-item ${ordersSubTab === 'stock' ? 'active' : ''}" data-sub="stock" aria-pressed="${ordersSubTab === 'stock'}">${icon('box')} Каталог</button>
      </div>
    </div>
  `;
  content.insertAdjacentHTML('afterbegin', tabsHtml);
  document.querySelectorAll('.seg-item[data-sub]').forEach(btn => {
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
    content.innerHTML = errorBox(e.message);
    return;
  }

  const cur = data.currency || 'USD';
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const fmtCur = n => `${fmt(n)} ${cur}`;
  const isBoss = data.role === 'admin' || data.role === 'boss';
  const mo = data.my_orders;

  // ─── Приветствие по времени суток ───────────────────
  const hh = new Date().getHours();
  const greetWord = hh < 5 ? 'Доброй ночи' : hh < 12 ? 'Доброе утро'
    : hh < 18 ? 'Добрый день' : 'Добрый вечер';
  const uname = (currentUser && (currentUser.first_name || currentUser.full_name || currentUser.name)) || '';
  const greeting = `<div class="home-greeting">${greetWord}${uname ? ', ' + escapeHtml(uname) : ''}</div>`;

  // ─── Hero: выручка за сегодня + тренд к вчера ────────
  const todayLabel = data.today.scope === 'personal' ? 'Моя выручка сегодня' : 'Выручка компании сегодня';
  const prevRev = data.today.prev_revenue || 0;
  let heroDelta = `<div class="hero-delta">${data.today.shipments} отгр. · ${data.today.clients} клиентов</div>`;
  if (prevRev > 0) {
    const pct = Math.round((data.today.revenue - prevRev) / prevRev * 100);
    const dir = pct > 0 ? 'up' : pct < 0 ? 'down' : '';
    const arrow = pct > 0 ? '↑' : pct < 0 ? '↓' : '→';
    heroDelta = `<div class="hero-delta ${dir}">${arrow} ${pct > 0 ? '+' : ''}${pct}% к вчера · ${data.today.shipments} отгр.</div>`;
  }
  const hero = `
    <div class="hero">
      <div class="hero-label">${todayLabel}</div>
      <div class="hero-value">${fmt(data.today.revenue)}<span class="hero-currency">${cur}</span></div>
      ${heroDelta}
    </div>
  `;

  // ─── Action-grid: 4 быстрых действия. Склад и Заказы объединены
  //    в одной вкладке «Склад и заказы», поэтому в гриде разделяем
  //    «открыть каталог» и «создать заказ» по data-new флагу.
  const actions = isBoss ? `
    <div class="action-grid">
      <button class="action-btn" data-go="stock">
        <span class="action-btn-icon icon-orange">${icon('box')}</span>Каталог
      </button>
      <button class="action-btn" data-go="requests">
        <span class="action-btn-icon icon-amber">${icon('clock')}</span>Заявки
      </button>
      <button class="action-btn" data-go="analytics">
        <span class="action-btn-icon icon-purple">${icon('chart')}</span>Аналитика
      </button>
      <button class="action-btn" data-go="payments">
        <span class="action-btn-icon icon-green">${icon('cash')}</span>Платежи
      </button>
    </div>
  ` : `
    <div class="action-grid">
      <button class="action-btn" data-go="stock">
        <span class="action-btn-icon icon-orange">${icon('box')}</span>Каталог
      </button>
      <button class="action-btn" data-go="orders" data-new="1">
        <span class="action-btn-icon icon-blue">${icon('plus')}</span>Заказ
      </button>
      <button class="action-btn" data-go="analytics">
        <span class="action-btn-icon icon-purple">${icon('chart')}</span>Аналитика
      </button>
      <button class="action-btn" data-go="payments">
        <span class="action-btn-icon icon-green">${icon('cash')}</span>Платёж
      </button>
    </div>
  `;

  // ─── Предупреждение если не привязан к МойСклад ─────
  const linkWarning = (!data.ms_linked && data.role === 'manager') ? `
    <div class="warn-card">
      ${icon('alert', 'warn-ic')} <b>Аккаунт не привязан к МойСклад.</b><br>
      <span style="font-size:12px;">Откройте чат с ботом и нажмите /start. Без привязки персональная аналитика недоступна.</span>
    </div>
  ` : '';

  // ─── Босс: ожидающие заявки + лидерборд ─────────────
  let bossBlock = '';
  if (isBoss) {
    // Дашборд «Требует внимания»: всё, что ждёт действия босса, одним блоком
    // с переходом в нужный раздел. Показываем только ненулевое.
    const att = data.attention || {};
    const attItems = [
      { n: att.requests, label: 'Заявки на апрув', ic: 'clock', go: 'requests' },
      { n: att.payments, label: 'Платежи на подтверждении', ic: 'cash', go: 'finance:confirm' },
      { n: att.deposits, label: 'Сдачи на подтверждении', ic: 'cashbox', go: 'finance:confirm' },
      { n: att.returns, label: 'Возвраты на подтверждении', ic: 'return', go: 'finance:confirm' },
      { n: att.debts, label: 'Открытые долги', ic: 'wallet', go: 'finance:debts' },
    ].filter(x => x.n > 0);
    if (attItems.length) {
      bossBlock += `
        <div class="section-label">Требует внимания</div>
        <div class="card-list">
          ${attItems.map(x => `
            <div class="card-row" data-att="${x.go}" role="button" tabindex="0">
              <div class="card-row-icon card-row-icon--warn">${icon(x.ic)}</div>
              <div class="card-row-info"><div class="card-row-title">${x.label}</div></div>
              <span class="stock-badge badge-yellow">${x.n}</span>
            </div>
          `).join('')}
        </div>
      `;
    }
    // Вход в полную операционную сводку (зависшие заявки, склад, cron, МС) —
    // отдельный экран. Раньше это уходило дайджестом в Telegram.
    bossBlock += `
      <div class="section-label">Мониторинг</div>
      <div class="card-list">
        <div class="card-row" data-att="ops" role="button" tabindex="0">
          <div class="card-row-icon">${icon('clock')}</div>
          <div class="card-row-info">
            <div class="card-row-title">Операционная сводка</div>
            <div class="card-row-sub">Зависшие заявки · склад · синхронизация</div>
          </div>
        </div>
      </div>
    `;
    const topEmp = data.top_employees || [];
    if (topEmp.length > 0) {
      bossBlock += `
        <div class="section-label">Топ сотрудники · неделя</div>
        <div class="card-list">
          ${topEmp.map((e, i) => `
            <div class="card-row card-row--static">
              <div class="card-row-icon rank-chip">${i + 1}</div>
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
          <div class="stat-label">${icon('edit')} Черновики</div>
        </div>
        <div class="stat">
          <div class="stat-value ${mo.pending > 0 ? 'stat-value-amber' : ''}">${mo.pending}</div>
          <div class="stat-label">${icon('clock')} Ожидают</div>
        </div>
        <div class="stat">
          <div class="stat-value ${mo.approved > 0 ? 'stat-value-green' : ''}">${mo.approved}</div>
          <div class="stat-label">${icon('check')} Одобрено</div>
        </div>
      </div>
    `;
    const recentList = mo.recent.length > 0 ? `
      <div class="card-list">
        ${mo.recent.map(o => `
          <div class="card-row" data-order-id="${o.id}" role="button" tabindex="0">
            <div class="card-row-icon icon-${o.status}">${icon(STATUS_ICON[o.status] || 'list')}</div>
            <div class="card-row-info">
              <div class="card-row-title">${escapeHtml(o.agent_name || ('Заказ #' + o.id))}</div>
              <div class="card-row-sub">Заказ #${o.id} · ${o.created_at}</div>
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

  content.innerHTML = greeting + hero + actions + linkWarning + bossBlock + ordersBlock;

  // Action-grid → переход на нужный таб (и опционально открыть новый заказ)
  document.querySelectorAll('[data-go]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic();
      const target = btn.dataset.go;
      const isNew = btn.dataset.new === '1';
      // «Заявки» (boss) → экран заявок на рассмотрении (под вкладкой «Заказы»),
      // а не голый список заказов. Сначала корневой таб (для подсветки nav),
      // затем поверх — список заявок.
      if (target === 'requests') {
        showScreen('orders');
        if (typeof renderPendingRequests === 'function') {
          setTimeout(renderPendingRequests, 50);
        }
        return;
      }
      showScreen(target);
      // Если кликнули по «+ Заказ» — после загрузки экрана откроем редактор
      if (isNew && typeof openOrderEditor === 'function') {
        // Дать времени renderOrders выполниться, потом инициировать создание
        setTimeout(() => openOrderEditor(null), 50);
      }
    });
  });

  // Дашборд «Требует внимания» → переход в нужный раздел.
  document.querySelectorAll('[data-att]').forEach(row => {
    row.addEventListener('click', () => {
      haptic();
      const go = row.dataset.att;
      if (go === 'ops') {
        showScreen('ops');
        return;
      }
      if (go === 'requests') {
        showScreen('orders');
        if (typeof renderPendingRequests === 'function') setTimeout(renderPendingRequests, 50);
        return;
      }
      if (go.indexOf('finance:') === 0) {
        financeTab = go.slice('finance:'.length);   // глоб. состояние вкладки финансов
        showScreen('finance');
      }
    });
  });

  // Клик по строке недавнего заказа → Заказы
  document.querySelectorAll('[data-order-id]').forEach(row => {
    row.addEventListener('click', () => showScreen('orders'));
  });
}

// ─── Экран: Склад ───────────────────────────────────

let stockData = null;          // { products, categories }
let stockCurrentCat = 'all';   // id выбранной категории или 'all'
let stockSearch = '';
let stockInStockOnly = false;  // фильтр «только в наличии»
let stockLimit = 200;          // сколько товаров показываем («Показать ещё» +200)
let _stockSearchTimer = null;  // дебаунс ввода в поиске по складу

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
      content.innerHTML = errorBox(e.message);
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
    if (stockInStockOnly && !(p.stock > 0)) return false;
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
  const isBoss = currentUser && (currentUser.role === 'admin' || currentUser.role === 'boss');

  listEl.innerHTML = filtered.length === 0
    ? `<div class="empty-state">
        <div class="empty-state-icon">${icon('box')}</div>
        <div class="empty-state-title">Товары не найдены</div>
        <div class="empty-state-hint">Попробуйте изменить категорию или поисковый запрос</div>
      </div>`
    : filtered.slice(0, stockLimit).map((p, i) => {
        // PR C: цена продажи (минимум) — всем; себестоимость — только boss.
        const priceLines = [];
        if (p.sale_price != null) priceLines.push(`мин. ${p.sale_price}`);
        if (isBoss && p.cost_price != null) priceLines.push(`себест. ${p.cost_price}`);
        const priceHtml = priceLines.length
          ? `<div class="stock-price">${escapeHtml(priceLines.join(' · '))}</div>` : '';
        // Boss может тапнуть товар → редактор цен.
        const editAttr = isBoss ? ` data-price-idx="${i}" role="button" tabindex="0" aria-label="Изменить цену: ${escapeHtml(p.name)}"` : '';
        const editHint = isBoss ? `<span class="stock-edit-hint">${icon('edit')}</span>` : '';
        return `
        <div class="stock-row"${editAttr}>
          <div class="stock-info">
            <div class="stock-name">${escapeHtml(p.name)}</div>
            <div class="stock-folder">${escapeHtml(p.folder_name || '—')} · ${p.unit}</div>
            ${priceHtml}
          </div>
          ${_stockBadge(p.stock)}${editHint}
        </div>`;
      }).join('');

  // Boss: тап по строке → редактор цены. Слушатель НЕ вешаем здесь (был бы
  // re-attach N строк на каждое нажатие в поиске) — делегирование на #stock-list
  // навешено один раз в renderStockContent.

  const truncEl = document.getElementById('stock-trunc');
  if (truncEl) {
    if (filtered.length > stockLimit) {
      const remaining = filtered.length - stockLimit;
      truncEl.innerHTML =
        `<button class="btn-secondary" id="stock-more">Показать ещё (${remaining})</button>`;
      document.getElementById('stock-more').addEventListener('click', () => {
        haptic('light');
        stockLimit += 200;
        renderStockList();
      });
    } else {
      truncEl.innerHTML = '';
    }
  }
}

// PR C: редактор цены товара (boss/admin). Overlay с двумя полями.
function openPriceEditor(product) {
  haptic('light');
  const msId = (product.href || '').split('/').filter(Boolean).pop() || '';
  if (!msId) { tg.showAlert && tg.showAlert('Нет ms_id у товара'); return; }
  const trigger = document.activeElement;  // вернём фокус сюда при закрытии
  const prevBack = _backHandler;           // восстановим back-кнопку экрана
  const ov = document.createElement('div');
  ov.className = 'price-overlay';
  ov.innerHTML = `
    <div class="price-modal" role="dialog" aria-modal="true" aria-labelledby="pe-title">
      <div class="price-modal-title" id="pe-title">${escapeHtml(product.name)}</div>
      <label class="price-field">
        <span>Цена продажи (минимум)</span>
        <input type="number" inputmode="decimal" id="pe-sale" value="${product.sale_price ?? ''}" placeholder="—">
      </label>
      <label class="price-field">
        <span>Себестоимость</span>
        <input type="number" inputmode="decimal" id="pe-cost" value="${product.cost_price ?? ''}" placeholder="—">
      </label>
      <div class="price-actions">
        <button class="btn-secondary" id="pe-cancel">Отмена</button>
        <button class="btn-primary" id="pe-save">Сохранить</button>
      </div>
    </div>`;
  document.body.appendChild(ov);
  const close = () => {
    ov.remove();
    document.removeEventListener('keydown', onKey, true);
    // Восстанавливаем back-кнопку экрана и фокус на инициатора.
    if (prevBack) showBack(prevBack); else hideBack();
    if (trigger && trigger.focus) { try { trigger.focus(); } catch (_e) {} }
  };
  // Esc + ловушка Tab внутри модалки (фокус не уходит на фон).
  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key !== 'Tab') return;
    const f = ov.querySelectorAll('input, button');
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  document.addEventListener('keydown', onKey, true);
  showBack(close);                         // аппаратная «назад» закрывает модалку
  ov.addEventListener('click', e => { if (e.target === ov) close(); });
  document.getElementById('pe-cancel').addEventListener('click', close);
  const saleEl = document.getElementById('pe-sale');
  if (saleEl && saleEl.focus) saleEl.focus();   // фокус внутрь при открытии
  document.getElementById('pe-save').addEventListener('click', async (ev) => {
    const saveBtn = ev.currentTarget;
    if (saveBtn.disabled) return;          // защита от дабл-клика
    saveBtn.disabled = true;
    const saleRaw = document.getElementById('pe-sale').value.trim();
    const costRaw = document.getElementById('pe-cost').value.trim();
    try {
      await api('/api/products/prices/set', {
        ms_id: msId,
        product_name: product.name,
        sale_price: saleRaw === '' ? null : parseNum(saleRaw),
        cost_price: costRaw === '' ? null : parseNum(costRaw),
      });
    } catch (e) {
      saveBtn.disabled = false;
      tg.showAlert ? tg.showAlert(e.message) : alert(e.message);
      return;
    }
    // Обновляем локально, чтобы список сразу показал новые цены.
    product.sale_price = saleRaw === '' ? null : parseNum(saleRaw);
    product.cost_price = costRaw === '' ? null : parseNum(costRaw);
    close();
    haptic('success');
    toast('Цена сохранена');
    renderStockList();
  });
}

function renderStockContent() {
  const content = document.getElementById('content');
  const { products, categories } = stockData;
  stockLimit = 200;   // новый рендер каркаса — сбрасываем «показать ещё»

  // Категории — таблетки сверху
  const catBtns = [{ id: 'all', name: `Все (${products.length})` }, ...categories]
    .map(c =>
      `<button class="cat-btn ${stockCurrentCat === c.id ? 'active' : ''}" data-cat="${c.id}" aria-pressed="${stockCurrentCat === c.id}">${c.name}</button>`
    ).join('');

  content.innerHTML = `
    <div class="form-row" style="margin: 4px 0 8px;">
      <input id="stock-search" class="form-input" placeholder="🔎 Поиск товара…" value="${escapeHtml(stockSearch)}">
    </div>
    <div class="section-label">Категории</div>
    <div class="cat-row">${catBtns}</div>
    <div class="cat-row">
      <button class="cat-btn ${!stockInStockOnly ? 'active' : ''}" data-instock="0" aria-pressed="${!stockInStockOnly}">Все</button>
      <button class="cat-btn ${stockInStockOnly ? 'active' : ''}" data-instock="1" aria-pressed="${stockInStockOnly}">${icon('box')} В наличии</button>
    </div>
    <div class="section-label">Товары</div>
    <div class="stock-list" id="stock-list"></div>
    <div id="stock-trunc"></div>
  `;
  renderStockList();

  // Boss: делегированный клик по строке товара → редактор цены. Вешаем ОДИН
  // раз на контейнер (строки пересоздаются в renderStockList — индивидуальные
  // слушатели пришлось бы перевешивать на каждое нажатие в поиске).
  const isBoss = currentUser && (currentUser.role === 'admin' || currentUser.role === 'boss');
  if (isBoss) {
    const listEl = document.getElementById('stock-list');
    if (listEl) {
      listEl.addEventListener('click', e => {
        const row = e.target.closest('[data-price-idx]');
        if (!row) return;
        const p = _stockFiltered()[parseInt(row.dataset.priceIdx, 10)];
        if (p) openPriceEditor(p);
      });
    }
  }

  document.querySelectorAll('[data-cat]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      stockCurrentCat = btn.dataset.cat;
      stockLimit = 200;   // смена категории — список с начала
      // Подсветить активную таблетку без полного ре-рендера каркаса.
      document.querySelectorAll('[data-cat]').forEach(b => {
        const on = b.dataset.cat === stockCurrentCat;
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', String(on));
      });
      renderStockList();
    });
  });
  const searchInput = document.getElementById('stock-search');
  if (searchInput) {
    // Дебаунс: на каждое нажатие фильтровали весь каталог и перестраивали до
    // 200 строк → джанк на больших списках. Ждём паузу в наборе ~180мс.
    searchInput.addEventListener('input', e => {
      stockSearch = e.target.value;
      stockLimit = 200;   // новый запрос — список с начала
      clearTimeout(_stockSearchTimer);
      _stockSearchTimer = setTimeout(renderStockList, 180);  // поле держит фокус
    });
    // не дёргаем фокус, чтобы не открывать клавиатуру при первом рендере
  }
  document.querySelectorAll('[data-instock]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      stockInStockOnly = btn.dataset.instock === '1';
      stockLimit = 200;
      document.querySelectorAll('[data-instock]').forEach(b => {
        const on = b.dataset.instock === (stockInStockOnly ? '1' : '0');
        b.classList.toggle('active', on);
        b.setAttribute('aria-pressed', String(on));
      });
      renderStockList();
    });
  });
}

// escapeHtml — в helpers.js (глобал, подключается ПЕРЕД app.js). Юнит-тестируется.

function loading(msg = 'Загрузка…') {
  return `<div class="spinner-wrap"><div class="spinner"></div><span>${msg}</span></div>`;
}

function haptic(type = 'light') {
  try { tg.HapticFeedback?.impactOccurred(type); } catch {}
}

// ─── Тосты ──────────────────────────────────────────
// Лёгкая неблокирующая обратная связь для «тихих» действий (добавил
// товар, сохранил цену, отметил оплату). В отличие от tg.showAlert не
// прерывает поток модалкой. Хост вне #content — переживает ре-рендеры.
function toast(msg, type = 'success', opts = {}) {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    host.className = 'toast-host';
    // Озвучивание скринридером: тосты — единственный фидбэк денежных действий.
    host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  // Ошибки — настойчивее (assertive), успехи/инфо — вежливо (polite).
  host.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
  const el = document.createElement('div');
  el.className = `toast toast--${type}`;
  // Иконка из спрайта (вместо эмодзи ⚠️/ℹ️/✅): тинтуется под тему, единый рендер.
  const glyph = type === 'error' ? 'alert' : type === 'info' ? 'info' : 'check';
  // Денежные/важные подтверждения держим дольше; ошибку — ещё дольше. Плюс
  // ручной крестик, чтобы можно было перечитать (раньше гасло за 2.4с молча).
  const duration = opts.duration || (type === 'error' ? 4500 : 3200);
  el.innerHTML =
    `<span class="toast-ic">${icon(glyph)}</span>` +
    `<span class="toast-msg">${escapeHtml(msg)}</span>` +
    `<button class="toast-close" aria-label="Закрыть">${icon('close')}</button>`;
  host.appendChild(el);
  let timer;
  const dismiss = () => {
    if (el.classList.contains('toast--out')) return;
    clearTimeout(timer);
    el.classList.add('toast--out');
    setTimeout(() => el.remove(), 250);
  };
  el.querySelector('.toast-close').addEventListener('click', dismiss);
  timer = setTimeout(dismiss, duration);
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
let ordersDataTs = 0;            // отметка времени загрузки ordersData (TTL-кэш)
const ORDERS_TTL_MS = 60000;     // как у analyticsCache — переключение вкладок не рефетчит
let currentOrderFilter = 'all';
let currentOrderPeriod = 'all';  // 'all' | 'today' | '7d' | '30d' | 'custom'
let currentOrderFrom = '';       // YYYY-MM-DD — кастомный диапазон (period='custom')
let currentOrderTo = '';
let currentDraftOrder = null; // активный черновик

// Имена иконок спрайта по статусу заказа (вместо прежних эмодзи: рендерятся
// одинаково на всех клиентах и тинтуются под тему). Цвет задаёт .icon-<status>.
const STATUS_ICON = {
  draft:    'edit',
  pending:  'clock',
  approved: 'check',
  rejected: 'close',
  shipped:  'truck',
};

const STATUS_NAME = {
  draft:    'Черновик',
  pending:  'На рассмотрении',
  approved: 'Одобрено',
  rejected: 'Отклонено',
  shipped:  'Отгружено',
};

async function api(path, body) {
  let r;
  try {
    r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: _initData, ...body }),
    });
  } catch {
    // fetch отклоняется только при сетевом сбое (нет интернета, CORS, abort) —
    // не при HTTP-ошибке. Помечаем, чтобы errorBox показал «нет связи».
    const err = new Error('Нет подключения к интернету');
    err.network = true;
    throw err;
  }
  if (!r.ok) {
    // Тело ошибки может быть не-JSON (502/HTML от прокси) — не падаем на парсе.
    let detail = `Ошибка сервера (${r.status})`;
    try { detail = (await r.json()).detail || detail; } catch { /* не-JSON */ }
    throw new Error(detail);
  }
  return r.json();
}

// Ключ идемпотентности для денежных действий: защищает от double-submit
// (две строки платежа / два уведомления). Сервер дедуплицирует по нему.
// idemKey — в helpers.js (глобал, подключается ПЕРЕД app.js). Юнит-тестируется.

// ─── Глобальный поиск ───────────────────────────────
let _searchTimer = null;

function openSearch() {
  haptic('light');
  showBack(() => showScreen(currentScreen));
  const content = document.getElementById('content');
  content.innerHTML = `
    <div class="search-wrap">
      <input type="search" id="search-input" class="search-input"
             placeholder="Заказ, платёж или клиент…" autocomplete="off" />
    </div>
    <div id="search-results" class="search-results">
      <div class="empty-hint">Введите минимум 2 символа</div>
    </div>`;
  const input = document.getElementById('search-input');
  input.focus();
  input.addEventListener('input', () => {
    clearTimeout(_searchTimer);
    const q = input.value.trim();
    if (q.length < 2) {
      document.getElementById('search-results').innerHTML =
        '<div class="empty-hint">Введите минимум 2 символа</div>';
      return;
    }
    _searchTimer = setTimeout(() => runSearch(q), 300);
  });
}

async function runSearch(query) {
  const box = document.getElementById('search-results');
  if (!box) return;
  box.innerHTML = loading('Ищу…');
  let data;
  try {
    data = await api('/api/search', { query });
  } catch (e) {
    box.innerHTML = errorBox(e.message);
    return;
  }
  // Гонка: пока ждали ответ, пользователь мог стереть/сменить запрос.
  const input = document.getElementById('search-input');
  if (!input || input.value.trim() !== query) return;

  const parts = [];
  if (data.orders && data.orders.length) {
    parts.push(`<div class="search-group-title">${icon('box')} Заказы</div>`);
    parts.push(data.orders.map(o => `
      <div class="search-item" role="button" tabindex="0" onclick="showScreen('orders')">
        <b>#${o.id}</b> · ${escapeHtml(o.agent_name)} · ${escapeHtml(o.status || '')}
        <span class="search-meta">${escapeHtml(o.full_name)}</span>
      </div>`).join(''));
  }
  if (data.payments && data.payments.length) {
    parts.push(`<div class="search-group-title">${icon('cash')} Платежи</div>`);
    parts.push(data.payments.map(p => `
      <div class="search-item" role="button" tabindex="0" onclick="showScreen('finance')">
        <b>#${p.id}</b> · ${p.amount} ${escapeHtml(p.currency)} · ${escapeHtml(p.status || '')}
        <span class="search-meta">${escapeHtml(p.full_name)}${p.comment ? ' · ' + escapeHtml(p.comment) : ''}</span>
      </div>`).join(''));
  }
  if (data.agents && data.agents.length) {
    // Карточка контрагента — только начальству (эндпоинт detail boss/admin).
    const canCard = currentUser && (currentUser.role === 'admin' || currentUser.role === 'boss');
    parts.push(`<div class="search-group-title">${icon('user')} Клиенты</div>`);
    parts.push(data.agents.map(a => {
      const label = `${escapeHtml(a.name || '—')}${a.phone ? ' · ' + escapeHtml(a.phone) : ''}`;
      return canCard
        ? `<div class="search-item" role="button" tabindex="0" data-agent="${escapeHtml(a.ms_id || '')}">${label}</div>`
        : `<div class="search-item">${label}</div>`;
    }).join(''));
  }
  box.innerHTML = parts.length
    ? parts.join('')
    : '<div class="empty-hint">Ничего не найдено</div>';
  // Тап по контрагенту → карточка (рендерит в #content, поиск закрывается).
  box.querySelectorAll('.search-item[data-agent]').forEach(el => {
    el.addEventListener('click', () => {
      const id = el.dataset.agent;
      if (id) { haptic('light'); renderAgentDetail(id); }
    });
  });
}

// Заголовок группы заказов по дате. key = 'YYYY-MM-DD' (из created_at).
// «Сегодня»/«Вчера» для свежих, иначе DD.MM.YYYY (formatDateRU из helpers.js).
// Попадает ли заказ (created_at='YYYY-MM-DD HH:MM') в выбранный период.
// Строковое сравнение YYYY-MM-DD корректно (лексикографически = хронологически).
function orderInPeriod(createdAt, period) {
  if (period === 'all') return true;
  const key = (createdAt || '').slice(0, 10);
  if (!key) return false;
  if (period === 'custom') {
    return (!currentOrderFrom || key >= currentOrderFrom)
        && (!currentOrderTo || key <= currentOrderTo);
  }
  const pad = n => String(n).padStart(2, '0');
  const dkey = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const today = new Date();
  if (period === 'today') return key === dkey(today);
  const days = period === '7d' ? 7 : 30;
  const cutoff = new Date(today);
  cutoff.setDate(today.getDate() - (days - 1));
  return key >= dkey(cutoff);
}

// ─── Календарь выбора диапазона дат (от–до) ──────────────────────────
// Самодостаточный инлайн-компонент: рисует свой DOM в host, сам управляет
// навигацией по месяцам и выбором диапазона, по «Применить» зовёт onApply(from,to).
// Даты — строки 'YYYY-MM-DD'. new Date() в браузере доступен (это app.js).
const _MONTHS_RU = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];
const _WD_RU = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];

function _ymd(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// День «по» в календаре включительный, а бэкенд аналитики трактует until как
// полночь (end-exclusive) и требует until>since → шлём следующий день.
function _nextDay(ymd) {
  const d = new Date(ymd + 'T00:00:00');
  d.setDate(d.getDate() + 1);
  return _ymd(d);
}

// Хост-заглушка в разметке; календарь монтируется в неё после innerHTML.
function dateRangeHost() {
  return '<div class="cal-host"></div>';
}

function mountCalendar(host, initFrom, initTo, onApply) {
  if (!host) return;
  let from = initFrom || null;
  let to = initTo || null;
  const base = from ? new Date(from + 'T00:00:00') : new Date();
  let vy = base.getFullYear();
  let vm = base.getMonth();

  function render() {
    const first = new Date(vy, vm, 1);
    const lead = (first.getDay() + 6) % 7;            // неделя с понедельника
    const daysInMonth = new Date(vy, vm + 1, 0).getDate();
    const todayKey = _ymd(new Date());
    const p2 = n => String(n).padStart(2, '0');
    const cells = [];
    for (let i = 0; i < lead; i++) cells.push('<div class="cal-cell cal-empty"></div>');
    for (let d = 1; d <= daysInMonth; d++) {
      const key = `${vy}-${p2(vm + 1)}-${p2(d)}`;
      const cls = ['cal-cell', 'cal-day'];
      const isStart = from && key === from;
      const isEnd = to && key === to;
      if (key === todayKey) cls.push('cal-today');
      if (isStart || isEnd) {
        cls.push('cal-sel');
        if (from && to && from !== to) {
          if (isStart) cls.push('cal-sel-start');
          if (isEnd) cls.push('cal-sel-end');
        }
      }
      if (from && to && key > from && key < to) cls.push('cal-in-range');
      cells.push(`<button type="button" class="${cls.join(' ')}" data-day="${key}">${d}</button>`);
    }
    const rangeLabel = from
      ? (to ? `${formatDateRU(from)} — ${formatDateRU(to)}` : `${formatDateRU(from)} — …`)
      : 'Выберите начало и конец';
    host.innerHTML = `
      <div class="cal">
        <div class="cal-head">
          <button type="button" class="cal-nav" data-nav="-1" aria-label="Предыдущий месяц">‹</button>
          <div class="cal-title">${_MONTHS_RU[vm]} ${vy}</div>
          <button type="button" class="cal-nav" data-nav="1" aria-label="Следующий месяц">›</button>
        </div>
        <div class="cal-grid cal-wd">${_WD_RU.map(w => `<div class="cal-wd-cell">${w}</div>`).join('')}</div>
        <div class="cal-grid cal-days">${cells.join('')}</div>
        <div class="cal-foot">
          <span class="cal-range">${rangeLabel}</span>
          <button type="button" class="btn-primary cal-apply" ${from && to ? '' : 'disabled'}>Применить</button>
        </div>
      </div>`;
    wire();
  }

  function wire() {
    host.querySelectorAll('[data-nav]').forEach(b => b.addEventListener('click', () => {
      vm += parseInt(b.dataset.nav, 10);
      if (vm < 0) { vm = 11; vy--; }
      if (vm > 11) { vm = 0; vy++; }
      render();
    }));
    host.querySelectorAll('[data-day]').forEach(b => b.addEventListener('click', () => {
      const key = b.dataset.day;
      if (!from || (from && to)) { from = key; to = null; }   // новый выбор
      else if (key < from) { from = key; }                    // раньше начала → новое начало
      else { to = key; }
      haptic('light');
      render();
    }));
    const apply = host.querySelector('.cal-apply');
    if (apply) apply.addEventListener('click', () => {
      if (from && to) { haptic('light'); onApply(from, to); }
    });
  }

  render();
}

function orderDateLabel(key) {
  if (!key || key.length < 10) return 'Без даты';
  const pad = n => String(n).padStart(2, '0');
  const dkey = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const today = new Date();
  const yest = new Date(today);
  yest.setDate(today.getDate() - 1);
  if (key === dkey(today)) return 'Сегодня';
  if (key === dkey(yest)) return 'Вчера';
  return formatDateRU(key);
}

async function renderOrders() {
  const content = document.getElementById('content');
  // Кэш заказов: переключение вкладок (Заказы↔Каталог↔Финансы) не должно
  // каждый раз дёргать /api/orders. Мутации (delete/ship/cancel) ставят
  // ordersData = null — это форсит свежую загрузку ниже.
  if (!ordersData || Date.now() - ordersDataTs > ORDERS_TTL_MS) {
    content.innerHTML = loading('Загружаю заказы…');
    try {
      ordersData = await api('/api/orders', {});
      ordersDataTs = Date.now();
    } catch (e) {
      content.innerHTML = errorBox(e.message);
      return;
    }
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

  // Боссу фильтр «черновики» бесполезен (это незавершённые заявки менеджеров) —
  // заменяем на «отгружено». Менеджеру черновики нужны (свои незаконченные).
  const filters = isBoss
    ? [
        { id: 'all', label: 'Все', name: 'Все' },
        { id: 'pending', label: icon('clock'), name: STATUS_NAME.pending },
        { id: 'approved', label: icon('check'), name: STATUS_NAME.approved },
        { id: 'shipped', label: icon('truck'), name: STATUS_NAME.shipped },
        { id: 'rejected', label: icon('close'), name: STATUS_NAME.rejected },
      ]
    : [
        { id: 'all', label: 'Все', name: 'Все' },
        { id: 'draft', label: icon('edit'), name: STATUS_NAME.draft },
        { id: 'pending', label: icon('clock'), name: STATUS_NAME.pending },
        { id: 'approved', label: icon('check'), name: STATUS_NAME.approved },
        { id: 'rejected', label: icon('close'), name: STATUS_NAME.rejected },
      ];

  const filterBtns = filters.map(f =>
    `<button class="cat-btn ${currentOrderFilter === f.id ? 'active' : ''}" data-filter="${f.id}" aria-pressed="${currentOrderFilter === f.id}" aria-label="${escapeHtml(f.name)}" title="${escapeHtml(f.name)}">${f.label}</button>`
  ).join('');

  // Фильтр по периоду — для навигации, когда заказов много.
  const periods = [
    { id: 'all', label: 'Всё время' },
    { id: 'today', label: 'Сегодня' },
    { id: '7d', label: '7 дней' },
    { id: '30d', label: '30 дней' },
    { id: 'custom', label: 'Период…' },
  ];
  // Период — компактный нативный select в тулбаре (а не второй ряд пилюль):
  // один ряд фильтров вместо двух. «Период…» открывает календарь.
  const periodOptions = periods.map(p =>
    `<option value="${p.id}" ${currentOrderPeriod === p.id ? 'selected' : ''}>${p.label}</option>`
  ).join('');
  const periodPanel = currentOrderPeriod === 'custom' ? dateRangeHost() : '';

  // Если активный статус-фильтр недоступен для роли (сменилась роль/состояние) —
  // откатываем на 'all', чтобы не показать пусто из-за исчезнувшего фильтра.
  if (!filters.some(f => f.id === currentOrderFilter)) currentOrderFilter = 'all';

  const filtered = orders.filter(o =>
    (currentOrderFilter === 'all' || o.status === currentOrderFilter) &&
    orderInPeriod(o.created_at, currentOrderPeriod)
  );

  const list = filtered.length === 0
    ? `<div class="empty-state">
        <div class="empty-state-icon">${icon('list')}</div>
        <div class="empty-state-title">Нет заказов</div>
        <div class="empty-state-hint">${(currentOrderFilter !== 'all' || currentOrderPeriod !== 'all')
          ? 'Нет заказов по выбранным фильтрам'
          : isBoss ? 'Менеджеры ещё не создавали заказов' : 'Нажмите «+ Новый заказ» чтобы начать'
        }</div>
      </div>`
    : (() => {
        // Группируем по дате (created_at='YYYY-MM-DD HH:MM') и выводим клиента
        // заголовком: легче ориентироваться, когда не помнишь ни номер, ни имя.
        const groups = [];
        const gidx = {};
        for (const o of filtered) {
          const k = (o.created_at || '').slice(0, 10);
          if (!(k in gidx)) { gidx[k] = groups.length; groups.push({ k, items: [] }); }
          groups[gidx[k]].items.push(o);
        }
        const orderCard = o => `
      <div class="order-card order-card--${o.status}" data-id="${o.id}">
        <div class="order-header">
          <div class="order-head-main">
            <div class="order-title">${icon('building')} ${escapeHtml(o.agent_name || 'Без клиента')}</div>
            <div class="order-sub">Заказ #${o.id}${isBoss ? ` · ${escapeHtml(o.full_name)}` : ''}</div>
          </div>
          <span class="order-status status-${o.status}">${STATUS_NAME[o.status]}</span>
        </div>
        <div class="order-meta">
          <span>${icon('box')} ${o.items_count} тов.</span>
          <span>${(o.created_at || '').slice(11, 16)}</span>
          ${o.total > 0 ? `<span class="order-total">${icon('cash')} ${Math.round(o.total).toLocaleString('ru-RU')}</span>` : ''}
        </div>
        ${(() => {
          // UX: тип оплаты / срок / статус оплаты / заморозка / причина возврата —
          // прямо на карточке, без открытия редактора. Поля из /api/orders.
          const bits = [];
          if (o.payment_type === 'credit') {
            const due = o.due_date ? ' до ' + String(o.due_date).split('-').reverse().join('.') : '';
            bits.push(`<span class="order-pay order-pay--credit">${icon('card')} В долг${due}</span>`);
          } else {
            bits.push(`<span class="order-pay">${icon('cash')} Оплата сразу</span>`);
          }
          if (o.paid_confirmed_at) bits.push(`<span class="order-pay order-pay--ok">${icon('check')} Оплачен</span>`);
          else if (o.paid_at) bits.push(`<span class="order-pay order-pay--wait">${icon('clock')} На подтверждении</span>`);
          if (o.frozen) bits.push(`<span class="order-pay order-pay--bad">${icon('snow')} Заморожен</span>`);
          if (o.status === 'draft' && o.rejection_comment)
            bits.push(`<span class="order-pay order-pay--bad">${icon('return')} ${escapeHtml(o.rejection_comment)}</span>`);
          return bits.length ? `<div class="order-pay-row">${bits.join('')}</div>` : '';
        })()}
        ${o.items.slice(0, 2).map(it => {
          const sub = (it.quantity || 0) * (it.price || 0);
          const priceStr = (it.price && it.price > 0)
            ? ` × ${it.price.toLocaleString('ru-RU')} = <b>${Math.round(sub).toLocaleString('ru-RU')}</b>`
            : '';
          return `<div class="order-item-preview">• ${escapeHtml(it.name)} — ${it.quantity} ${it.unit}${priceStr}</div>`;
        }).join('')}
        ${o.status === 'draft' && !isBoss ? `
          <div class="draft-actions">
            <button class="btn-edit-order" data-id="${o.id}">${icon('edit')} Редактировать</button>
            <button class="btn-delete-draft" data-id="${o.id}">${icon('trash')} Удалить</button>
          </div>
        ` : ''}
        ${o.status === 'approved' && canShip ? `
          <div class="draft-actions">
            <button class="btn-confirm-pay btn-ship-order" data-id="${o.id}">${icon('truck')} Отгрузить</button>
          </div>
        ` : ''}
        ${o.status === 'approved' && isBoss ? `
          <div class="draft-actions">
            <button class="btn-reject-pay btn-cancel-order" data-id="${o.id}">${icon('close')} Отменить заказ</button>
          </div>
          <div class="limit-edit cancel-box" data-id="${o.id}" hidden>
            <input type="text" class="form-input cancel-reason" placeholder="Причина отмены">
            <button class="btn-reject-pay cancel-send" data-id="${o.id}">Подтвердить отмену</button>
          </div>
        ` : ''}
      </div>
    `;
        return groups.map(g => `
          <div class="section-label order-date-label">${orderDateLabel(g.k)}</div>
          ${g.items.map(orderCard).join('')}
        `).join('');
      })();

  content.innerHTML = `
    <div class="orders-toolbar">
      <div class="cat-scroll">${filterBtns}</div>
      <select class="period-select" id="order-period" aria-label="Период">${periodOptions}</select>
      ${!isBoss ? `<button class="btn-new-order" id="btn-new-order">+ Новый заказ</button>` : ''}
    </div>
    ${periodPanel}

    ${isBoss ? `<button class="requests-btn" id="show-requests">${icon('clock')} Заявки на рассмотрении</button>` : ''}

    <div class="orders-list">${list}</div>
  `;

  // Фильтры по статусу
  document.querySelectorAll('.cat-btn[data-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      currentOrderFilter = btn.dataset.filter;
      renderOrdersMain();
    });
  });

  // Фильтр по периоду — нативный select.
  const periodSel = document.getElementById('order-period');
  if (periodSel) {
    periodSel.addEventListener('change', () => {
      haptic('light');
      currentOrderPeriod = periodSel.value;
      renderOrdersMain();
    });
  }

  // Кастомный диапазон дат: монтируем календарь, по «Применить» фильтруем.
  if (currentOrderPeriod === 'custom') {
    mountCalendar(content.querySelector('.cal-host'), currentOrderFrom, currentOrderTo,
      (from, to) => {
        currentOrderFrom = from;
        currentOrderTo = to;
        renderOrdersMain();
      });
  }

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
          await api('/api/orders/ship', { order_id: id, idempotency_key: idemKey() });
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
      content.innerHTML = errorBox(e.message);
      return;
    }
  }

  currentDraftOrder = {
    id: orderId,
    items: [],
    agent_id: null,
    agent_name: null,
  };

  // Черновик с несохранёнными позициями — просим подтверждение, если юзер
  // свайпает WebApp закрытым (снимается в showScreen и после отправки заказа).
  tg.enableClosingConfirmation && tg.enableClosingConfirmation();

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
            <button class="editor-item-del" data-idx="${i}" aria-label="Удалить позицию">✕</button>
          </div>
        `;
      }).join('') + (grandTotal > 0 ? `
        <div class="editor-item editor-item--total">
          <div class="editor-item-info">
            <div class="editor-item-name">${icon('cash')} Итого</div>
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
        ? `<div class="agent-selected">${icon('building')} ${order.agent_name} <button id="change-agent">Изменить</button></div>`
        : `<button class="btn-agent" id="choose-agent">${icon('user')} Выбрать клиента</button>`
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
        <span>${icon('cash')} Оплачено сразу</span>
      </label>
      <label class="payment-option">
        <input type="radio" name="payment_type" value="credit"
          ${currentDraftOrder.payment_type === 'credit' ? 'checked' : ''}>
        <span>${icon('card')} В долг</span>
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
      <div class="agent-row" data-id="${a.id}" data-name="${escapeHtml(a.name || '')}" role="button" tabindex="0">
        <div class="agent-name">${icon('user')} ${escapeHtml(a.name || '')}</div>
        ${a.phone ? `<div class="agent-phone">${escapeHtml(a.phone)}</div>` : ''}
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
    list.innerHTML = errorBox(e.message);
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
      document.getElementById('prod-list').innerHTML = errorBox(e.message);
      return;
    }
  }

  let selectedCat = 'all';
  let prodLimit = 50;   // сколько товаров показываем («Показать ещё» +50)
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
    const moreBtn = filtered.length > prodLimit
      ? `<button class="btn-secondary" id="prod-more" style="margin-top:8px;">Показать ещё (${filtered.length - prodLimit})</button>`
      : '';
    list.innerHTML = filtered.length === 0
      ? '<div class="loader">Товары не найдены</div>'
      : filtered.slice(0, prodLimit).map(p => {
          const ind = p.stock >= 100 ? 'green' : p.stock >= 20 ? 'yellow' : 'red';
          return `
            <div class="prod-row" role="button" tabindex="0"
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
        }).join('') + moreBtn;

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
    document.getElementById('prod-more')?.addEventListener('click', () => {
      haptic('light');
      prodLimit += 50;
      renderProducts();
    });
  }

  // Категории
  const catFilters = document.getElementById('cat-filters');
  catFilters.innerHTML = [
    `<button class="cat-btn active" data-cat="all" aria-pressed="true">Все</button>`,
    ...categories.map(c => `<button class="cat-btn" data-cat="${c.id}" aria-pressed="false">${escapeHtml(c.name)}</button>`),
  ].join('');

  catFilters.querySelectorAll('.cat-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      catFilters.querySelectorAll('.cat-btn').forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-pressed', 'false');
      });
      btn.classList.add('active');
      btn.setAttribute('aria-pressed', 'true');
      selectedCat = btn.dataset.cat;
      prodLimit = 50;   // смена категории — список с начала
      renderProducts();
    });
  });

  // Дебаунс поиска товара — как в поиске клиентов (иначе фильтрация
  // дёргается на каждое нажатие при больших каталогах).
  const prodSearch = document.getElementById('prod-search');
  let prodTimer;
  prodSearch.addEventListener('input', () => {
    clearTimeout(prodTimer);
    prodTimer = setTimeout(() => { prodLimit = 50; renderProducts(); }, 250);
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
    const q = parseNum(qtyEl.value) || 0;
    const p = parseNum(priceEl.value) || 0;
    const t = q * p;
    totalEl.innerHTML = `Итого: <b>${t.toLocaleString('ru-RU', {maximumFractionDigits: 2})} ${selectedCurrency}</b>`;
  }
  qtyEl.addEventListener('input', updateTotal);
  priceEl.addEventListener('input', updateTotal);

  // ─── MainButton: «Добавить в заявку» ─────────────────────────
  // Нативная кнопка Telegram — всегда видна над виртуальной клавиатурой,
  // в отличие от HTML-кнопки внизу формы, которую клавиатура перекрывала.
  async function onConfirm() {
    const qty = parseNum(qtyEl.value);
    const price = parseNum(priceEl.value) || 0;
    const draftId = currentDraftOrder && currentDraftOrder.id;
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
      // За время сетевого запроса пользователь мог уйти с экрана / сменить
      // черновик — не пишем результат в чужой DOM (ghost-контент).
      if (!currentDraftOrder || currentDraftOrder.id !== draftId || currentScreen !== 'orders') {
        clearMainButton();
        return;
      }
      currentDraftOrder.items.push({
        name, quantity: qty, unit, price, item_id: result.item_id,
      });
      if (!currentDraftOrder.currency) currentDraftOrder.currency = selectedCurrency;
      tg.HapticFeedback?.notificationOccurred('success');
      tg.MainButton?.hideProgress?.();
      clearMainButton();
      toast('Товар добавлен в заявку');
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
      idempotency_key: idemKey(),
    });
    tg.HapticFeedback?.notificationOccurred('success');
    tg.showAlert(`✅ Заявка #${result.req_id} отправлена руководителю!`);
    ordersData = null;
    currentDraftOrder = null;
    // Черновик отправлен — снимаем подтверждение закрытия.
    tg.disableClosingConfirmation && tg.disableClosingConfirmation();
    await renderOrders();
  } catch (e) {
    tg.HapticFeedback?.notificationOccurred('error');
    tg.showAlert('❌ ' + e.message);
    if (btn) btn.disabled = false;
  }
}


async function renderOpsSummary() {
  // Операционная сводка (boss/admin): зависшие заявки, несданные деньги,
  // складские алерты, здоровье cron, рассинхрон с МС. Раньше уходило большим
  // дайджестом в Telegram — теперь смотрим тут, бот шлёт лишь дневной пинг.
  const content = document.getElementById('content');
  content.innerHTML = loading('Загружаю сводку…');
  try {
    const data = await api('/api/ops-summary', {});
    showBack(() => showScreen('home'));
    content.innerHTML =
      `<div class="editor-header"><div class="editor-title">Операционная сводка</div></div>` +
      renderOpsSummaryHtml(data);
  } catch (e) {
    content.innerHTML = errorBox(e.message || String(e));
  }
}

async function renderPendingRequests() {
  const content = document.getElementById('content');
  // fmt был локальным в других рендерах, но не здесь → кредит-блок заявки
  // (fmt(...)) кидал ReferenceError, и весь экран заявок падал в errorBox.
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
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
          <div class="empty-state-icon">${icon('check')}</div>
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
            <div class="order-title">${icon('clock')} Заявка #${r.id}</div>
            <div class="order-manager">${icon('user')} ${r.full_name}</div>
          </div>
          <span class="order-status status-pending">Ожидает</span>
        </div>
        ${r.agent_name ? `<div class="order-agent">${icon('building')} ${r.agent_name}</div>` : ''}
        <div class="order-meta"><span>${r.created_at}</span></div>
        ${(() => {
          const bits = [];
          if (r.payment_type === 'credit') {
            const due = r.due_date ? ' до ' + String(r.due_date).split('-').reverse().join('.') : '';
            bits.push(`<span class="order-pay order-pay--credit">${icon('card')} В долг${due}</span>`);
          } else {
            bits.push(`<span class="order-pay">${icon('cash')} Оплата сразу</span>`);
          }
          if (r.total > 0) bits.push(`<span class="order-pay">${icon('cash')} ${Math.round(r.total).toLocaleString('ru-RU')}</span>`);
          return `<div class="order-pay-row">${bits.join('')}</div>`;
        })()}
        ${r.credit ? `
          <div class="credit-ctx ${r.credit.over_limit ? 'credit-ctx--bad' : 'credit-ctx--ok'}">
            ${icon('chart')} Кредит клиента: долг с учётом заявки <b>${fmt(r.credit.effective_debt)}</b>
            / лимит <b>${fmt(r.credit.limit)}</b>
            ${r.credit.over_limit ? `${icon('alert')} превышение` : `${icon('check')} в пределах`}
          </div>` : ''}
        <div class="order-items">
          ${r.items.slice(0, 5).map(it =>
            `<div class="order-item">• ${it.name}: <b>${it.quantity} ${it.unit}</b></div>`
          ).join('')}
        </div>
        <div class="req-actions">
          <button class="btn-approve" data-req="${r.id}">${icon('check')} Одобрить</button>
          <button class="btn-reject"  data-req="${r.id}">${icon('close')} Отклонить</button>
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
      btn.addEventListener('click', () => {
        // Отклонение заявки — консеквентно (менеджер переделывает): подтверждаем.
        tg.showConfirm('Отклонить заявку? Менеджеру придётся создать её заново.', ok => {
          if (ok) handleRequest(btn.dataset.req, 'reject');
        });
      })
    );
  } catch (e) {
    content.innerHTML = errorBox(e.message);
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
    await api(path, { req_id: Number(reqId), idempotency_key: idemKey() });
    tg.showAlert(action === 'approve' ? '✅ Заявка одобрена' : '❌ Заявка отклонена');
  } catch (e) {
    tg.showAlert(`❌ ${e.message}`);
  }
  await renderPendingRequests();
}
// ─── Экран: Аналитика ───────────────────────────────

let analyticsCache = {};  // cacheKey -> { ts, data }
let analyticsPeriod = 'month';   // preset id | 'custom'
let analyticsSince = '';         // YYYY-MM-DD — кастомный диапазон (period='custom')
let analyticsUntil = '';
let lastAnalyticsData = null;    // последний успешный ответ — чтобы показать поля дат без перезапроса
let analyticsView = 'sales';     // 'sales' | 'money' (boss): продажи vs деньги (быв. «Обзор»)
const ANALYTICS_TTL_MS = 60 * 1000;

// Шапка Аналитики: переключатель вида «Продажи|Деньги» (boss) + период-сегмент.
// Общая для обоих видов, чтобы период/вид не расходились. Период — iOS-сегмент
// .seg (фиксированные 2–4 — навигация), не переносится.
function analyticsHeaderHtml(isBoss) {
  const viewSeg = isBoss ? `
    <div class="seg-row" style="margin-bottom:10px;">
      <div class="seg">
        <button class="seg-item ${analyticsView === 'sales' ? 'active' : ''}" data-aview="sales" aria-pressed="${analyticsView === 'sales'}">Продажи</button>
        <button class="seg-item ${analyticsView === 'money' ? 'active' : ''}" data-aview="money" aria-pressed="${analyticsView === 'money'}">Деньги</button>
      </div>
    </div>` : '';
  const presets = [
    { id: 'week', label: 'Неделя' }, { id: 'month', label: 'Месяц' },
    { id: '3month', label: 'Квартал' }, { id: 'year', label: 'Год' },
  ];
  const presetSeg = presets.map(p =>
    `<button class="seg-item ${analyticsPeriod === p.id ? 'active' : ''}" data-period="${p.id}" aria-pressed="${analyticsPeriod === p.id}">${p.label}</button>`
  ).join('');
  const customLabel = (analyticsPeriod === 'custom' && analyticsSince && analyticsUntil)
    ? `${formatDateRU(analyticsSince)}—${formatDateRU(analyticsUntil)}`
    : 'Период…';
  const periodBar = `
    <div class="seg-row">
      <div class="seg">${presetSeg}</div>
      <button class="seg-aux ${analyticsPeriod === 'custom' ? 'active' : ''}" data-period="custom" aria-pressed="${analyticsPeriod === 'custom'}">${icon('clock')} ${customLabel}</button>
    </div>`;
  const periodPanel = analyticsPeriod === 'custom' ? dateRangeHost() : '';
  return `${viewSeg}<div class="section-label">Период</div>${periodBar}${periodPanel}`;
}

// Навешивает обработчики шапки Аналитики (вид/период/календарь) — общие для видов.
function wireAnalyticsHeader(content) {
  content.querySelectorAll('[data-aview]').forEach(btn => {
    btn.addEventListener('click', () => { haptic('light'); analyticsView = btn.dataset.aview; renderAnalytics(); });
  });
  content.querySelectorAll('[data-period]').forEach(btn => {
    btn.addEventListener('click', () => { haptic('light'); analyticsPeriod = btn.dataset.period; renderAnalytics(); });
  });
  if (analyticsPeriod === 'custom') {
    mountCalendar(content.querySelector('.cal-host'), analyticsSince, analyticsUntil,
      (from, to) => { analyticsSince = from; analyticsUntil = to; renderAnalytics(); });
  }
}

async function renderAnalytics() {
  const content = document.getElementById('content');
  const isBoss = currentUser && (currentUser.role === 'admin' || currentUser.role === 'boss');
  if (!isBoss) analyticsView = 'sales';  // не-боссу доступны только «Продажи»
  if (analyticsView === 'money' && isBoss) return renderMoneyView();
  const custom = analyticsPeriod === 'custom';

  // «Период…» выбран, но даты ещё не заданы → не дёргаем бэкенд: показываем поля
  // дат поверх последних данных (или подгружаем месяц как базу, если данных нет).
  if (custom && !(analyticsSince && analyticsUntil)) {
    if (lastAnalyticsData) { renderAnalyticsContent(lastAnalyticsData); return; }
    analyticsPeriod = 'month';
    return renderAnalytics();
  }

  // Короткий кэш (TTL 60с): пресеты и конкретные диапазоны кэшируются отдельно.
  const cacheKey = custom ? `c:${analyticsSince}:${analyticsUntil}` : analyticsPeriod;
  const cached = analyticsCache[cacheKey];
  if (cached && Date.now() - cached.ts < ANALYTICS_TTL_MS) {
    lastAnalyticsData = cached.data;
    renderAnalyticsContent(cached.data);
    return;
  }

  content.innerHTML = loading('Считаю статистику…');
  try {
    const body = custom
      ? { initData: _initData, since: analyticsSince, until: _nextDay(analyticsUntil) }
      : { initData: _initData, period: analyticsPeriod };
    const response = await fetch('/api/analytics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const err = await response.json();
      throw new Error(err.detail || 'Ошибка');
    }
    const data = await response.json();
    analyticsCache[cacheKey] = { ts: Date.now(), data };
    lastAnalyticsData = data;
    renderAnalyticsContent(data);
  } catch (e) {
    content.innerHTML = errorBox(e.message);
  }
}

function renderAnalyticsContent(data) {
  const content = document.getElementById('content');
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const isBoss = currentUser && (currentUser.role === 'admin' || currentUser.role === 'boss');

  const trendIcon = data.trend > 0 ? icon('trend-up') : data.trend < 0 ? icon('trend-down') : '';
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
        // PR D: прибыль по товару (если задана себестоимость) — boss/admin.
        const profitStr = (p.margin_known && p.profit != null)
          ? ` · <span class="top-profit">прибыль ${fmt(p.profit)} $</span>` : '';
        return `
          <div class="top-row">
            <span class="top-medal rank-chip">${i + 1}</span>
            <div class="top-info">
              <div class="top-name">${escapeHtml(p.name)}</div>
              <div class="top-sub">${fmt(p.qty)} шт · ${fmt(p.sum)} $${profitStr}</div>
            </div>
          </div>
        `;
      }).join('');

  // PR D: топ клиентов / менеджеров (company-scope, boss/admin).
  const clientItems = (data.top_clients || []).map((c, i) => `
    <div class="top-row">
      <span class="top-medal rank-chip">${i + 1}</span>
      <div class="top-info">
        <div class="top-name">${escapeHtml(c.name)}</div>
        <div class="top-sub">${fmt(c.revenue)} $ · ${c.count} отгр.</div>
      </div>
    </div>`).join('');
  const managerItems = (data.top_managers || []).map((m, i) => `
    <div class="top-row">
      <span class="top-medal rank-chip">${i + 1}</span>
      <div class="top-info">
        <div class="top-name">${escapeHtml(m.name)}</div>
        <div class="top-sub">${fmt(m.revenue)} $ · ${m.count} отгр.${m.orders != null ? ` · ${m.orders} зак.` : ''}${m.debt ? ` · долг ${fmt(m.debt)} $` : ''}</div>
      </div>
    </div>`).join('');
  const clientsBlock = clientItems
    ? `<div class="section-label">Топ клиентов</div><div class="card">${clientItems}</div>` : '';
  const managersBlock = managerItems
    ? `<div class="section-label">Топ менеджеров</div><div class="card">${managerItems}</div>` : '';
  // Кнопка Excel — только company-scope (boss/admin).
  const exportBlock = data.scope === 'company'
    ? `<button class="btn-primary" id="analytics-export" style="margin-top:12px">${icon('chart')} Выгрузить Excel</button>` : '';

  content.innerHTML = `
    ${analyticsHeaderHtml(isBoss)}

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
    ${clientsBlock}
    ${managersBlock}
    ${exportBlock}
  `;

  const exportBtn = document.getElementById('analytics-export');
  if (exportBtn) {
    exportBtn.addEventListener('click', async () => {
      haptic('light');
      exportBtn.disabled = true;
      exportBtn.textContent = '⏳ Готовлю файл…';
      try {
        const exportBody = (analyticsPeriod === 'custom' && analyticsSince && analyticsUntil)
          ? { since: analyticsSince, until: _nextDay(analyticsUntil) }
          : { period: analyticsPeriod };
        await api('/api/analytics/export', exportBody);
        exportBtn.textContent = '✅ Отправлено в чат';
        tg.showAlert && tg.showAlert('Excel-файл отправлен в чат с ботом');
      } catch (e) {
        exportBtn.disabled = false;
        exportBtn.textContent = 'Выгрузить Excel';
        tg.showAlert ? tg.showAlert(e.message) : alert(e.message);
      }
    });
  }

  // Bars animate from 0 → target after paint
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.querySelectorAll('.bar-fill[data-w]').forEach(b => {
      b.style.width = b.dataset.w + '%';
    });
  }));

  wireAnalyticsHeader(content);  // вид/период/календарь — общая шапка
}

// Аналитика → «Деньги» (быв. Финансы→«Обзор», boss): итоги поступлений за период
// + лента движения денег. Период — общий с «Продажами» (analyticsPeriod).
async function renderMoneyView() {
  const content = document.getElementById('content');
  content.innerHTML = loading('Считаю деньги…');
  // Кастомный период выбран, но даты не заданы — откатываемся на месяц.
  if (analyticsPeriod === 'custom' && !(analyticsSince && analyticsUntil)) analyticsPeriod = 'month';
  const periodBody = (analyticsPeriod === 'custom' && analyticsSince && analyticsUntil)
    ? { since: analyticsSince, until: _nextDay(analyticsUntil) }
    : { period: analyticsPeriod };
  let summary = null;
  let history = [];
  try {
    summary = await api('/api/money/summary', periodBody);
    history = (await api('/api/cash/history', {}).catch(() => ({ history: [] }))).history || [];
  } catch (e) {
    content.innerHTML = analyticsHeaderHtml(true) + errorBox(e.message);
    wireAnalyticsHeader(content);
    return;
  }
  const label = (summary && summary.period && summary.period.label) || '';
  content.innerHTML =
    analyticsHeaderHtml(true) +
    `<div class="section-label">Поступления · ${escapeHtml(label)}</div>` +
    renderMoneyTotalsHtml(summary) +
    '<div class="section-label">Движение денег</div>' +
    cashHistoryHtml(history);
  wireAnalyticsHeader(content);
}

// Лента движения денег (платежи + сдачи + возвраты) — общий рендер для «Денег».
function cashHistoryHtml(history) {
  if (!history || !history.length) return '<div class="loader">Движений пока нет</div>';
  const KIND_META = {
    payment: { ic: 'cash', label: 'Платёж' },
    deposit: { ic: 'cashbox', label: 'Сдача' },
    return: { ic: 'return', label: 'Возврат' },
  };
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const histStatus = s => s === 'confirmed'
    ? '<span class="stock-badge badge-green">принят</span>'
    : s === 'rejected' ? '<span class="stock-badge badge-red">отклонён</span>'
    : '<span class="stock-badge badge-yellow">ожидает</span>';
  const rows = history.map(h => {
    const m = KIND_META[h.kind] || { ic: 'cash', label: h.kind };
    const ord = h.order_id ? ` · заказ #${h.order_id}` : '';
    return `
      <div class="stock-row">
        <div class="card-row-icon">${icon(m.ic)}</div>
        <div class="stock-info">
          <div class="stock-name">${m.label} · ${fmt(h.amount)} ${escapeHtml(h.currency || baseCur())}</div>
          <div class="stock-folder">${escapeHtml(h.who || '')} · ${escapeHtml(h.created_at || '')}${ord}</div>
        </div>
        ${histStatus(h.status)}
      </div>`;
  }).join('');
  return `<div class="stock-list">${rows}</div>`;
}

// (Экран «Платежи» удалён: подтверждение оплат, история движения денег и
//  ручной платёж объединены во вкладку «Касса» — см. renderCashbox.)

function initNav() {
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic();
      document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      showScreen(btn.dataset.screen);
    });
  });
  // Поиск в топбаре — доступен с любого экрана.
  const searchBtn = document.getElementById('search-btn');
  if (searchBtn) {
    searchBtn.addEventListener('click', openSearch);
  }
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

let financeTab = 'debts';  // confirm | debts | ops | limits (плоско, по задачам)
let debtsFilter = 'all';   // 'all' | 'today'
let cashboxSubTab = null;  // последняя секция кассы (confirm|ops) — фолбэк
let financePendCache = 0;  // последний счётчик подтверждений (мгновенный бейдж)

async function renderFinance() {
  const content = document.getElementById('content');
  const role = currentUser && currentUser.role;
  const isBoss = role === 'admin' || role === 'boss';
  const canDeposit = role === 'manager' || role === 'warehouse_keeper';
  const isConfirmer = ['admin', 'boss', 'bookkeeper', 'warehouse_keeper'].includes(role);
  const canReturn = ['admin', 'boss', 'warehouse_keeper', 'manager'].includes(role);
  // «Платежи и сдачи» = формы (сдать наличные / возврат / новый платёж) + «Мои сдачи».
  const hasOps = canDeposit || canReturn || !isBoss;

  // Миграция старых/внешних ключей вкладок на текущий плоский набор.
  if (financeTab === 'payments' || financeTab === 'cashbox') financeTab = isConfirmer ? 'confirm' : 'ops';
  if (financeTab === 'my') financeTab = 'ops';          // свёрнуто в «Платежи и сдачи»
  if (financeTab === 'overview') financeTab = 'debts';  // переехало в Аналитику → Деньги

  const tabs = financeTabs({ isBoss, isConfirmer, hasOps, canDeposit });
  // Недоступную/неизвестную вкладку откатываем на первую доступную.
  if (!tabs.find(t => t.key === financeTab)) financeTab = (tabs[0] && tabs[0].key) || 'debts';
  const LABELS = {};
  tabs.forEach(t => { LABELS[t.key] = t.label; });
  setScreenContext(`Финансы · ${LABELS[financeTab] || ''}`);

  // Под-навигация = подчёркнутый горизонт-скролл сегмент (.subseg) — НЕ переносится
  // «по три в ряд» (раньше .cat-row с flex-wrap). Пилюли (.cat-btn) теперь только
  // для фильтров — визуальная иерархия: подчёркнутое = навигация, пилюли = фильтры.
  // ВАЖНО: вкладки рисуем СРАЗУ (синхронно). Раньше рендер ждал сетевой подсчёт
  // бейджа (await) — при зависшем запросе вкладки не появлялись до перезагрузки
  // («иногда пропадали вкладки»). Бейдж берём из кэша и освежаем асинхронно ниже.
  const tabBarHtml = (pendN) => `
    <div class="subseg">
      ${tabs.map(t => {
        const on = financeTab === t.key;
        const badge = (t.key === 'confirm' && pendN)
          ? ` <span class="stock-badge badge-yellow">${pendN}</span>` : '';
        return `<button class="subseg-item ${on ? 'active' : ''}" data-tab="${t.key}" aria-pressed="${on}">${t.label}${badge}</button>`;
      }).join('')}
    </div>
    <div id="finance-body"></div>`;
  content.innerHTML = tabBarHtml(isConfirmer ? financePendCache : 0);
  content.querySelectorAll('[data-tab]').forEach(t => {
    t.addEventListener('click', () => {
      haptic('light');
      financeTab = t.dataset.tab;
      renderFinance();
    });
  });
  // Активную вкладку подтягиваем в зону видимости, если ряд скроллится.
  const activeSeg = content.querySelector('.subseg-item.active');
  if (activeSeg && activeSeg.scrollIntoView) {
    try { activeSeg.scrollIntoView({ inline: 'center', block: 'nearest' }); } catch (e) { /* старый WebView */ }
  }

  // Бейдж ожидающих подтверждений — освежаем АСИНХРОННО, чтобы зависший запрос
  // не блокировал появление вкладок. Обновляем счётчик на месте.
  if (isConfirmer) {
    const cnt = (p, k) => api(p, {}).then(r => (r[k] || []).length).catch(() => 0);
    const parts = [cnt('/api/deposits/pending', 'deposits'), cnt('/api/returns/pending', 'returns')];
    if (isBoss) parts.push(cnt('/api/payments/pending', 'pending'));
    Promise.all(parts).then(arr => {
      const n = arr.reduce((a, b) => a + b, 0);
      financePendCache = n;
      if (currentScreen !== 'finance') return;
      const pill = content.querySelector('.subseg-item[data-tab="confirm"]');
      if (!pill) return;
      const old = pill.querySelector('.stock-badge');
      if (old) old.remove();
      if (n) {
        const b = document.createElement('span');
        b.className = 'stock-badge badge-yellow';
        b.textContent = n;
        pill.appendChild(b);
      }
    });
  }

  const body = document.getElementById('finance-body');
  if (financeTab === 'debts') await renderDebts(body);
  else if (financeTab === 'limits') await renderClients(body);
  else await renderCashbox(body, financeTab);  // confirm | ops
}

async function renderCashbox(container, section) {
  container = container || document.getElementById('content');
  // section — плоская вкладка из renderFinance: confirm | ops. Бейдж/переключение
  // на уровне renderFinance, тут только тело секции. Легаси-ключи свёрнуты.
  section = section || cashboxSubTab || 'confirm';
  if (section === 'my') section = 'ops';          // «Мои сдачи» — секция внутри ops
  if (section === 'overview') section = 'confirm'; // «Обзор» переехал в Аналитику
  cashboxSubTab = section;
  container.innerHTML = loading('Загрузка кассы…');
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const role = currentUser && currentUser.role;
  // Наличные сдаёт тот, кто их физически принимает от клиента (менеджер,
  // кладовщик). Начальство/бухгалтер только ПОДТВЕРЖДАЮТ.
  const canDeposit = role === 'manager' || role === 'warehouse_keeper';
  const isBoss = role === 'admin' || role === 'boss';

  // Тянем ТОЛЬКО то, что нужно активной секции (раньше грузилось всё сразу).
  let deposits = [];
  let returns = [];
  let myDeposits = [];
  let payPending = [];   // paid-заказы с pending-оплатой (подтверждает босс)
  const _grab = (path, key) => api(path, {}).then(r => r[key] || []).catch(() => []);
  const tasks = [];
  if (section === 'confirm') {
    tasks.push(_grab('/api/deposits/pending', 'deposits').then(v => { deposits = v; }));
    tasks.push(_grab('/api/returns/pending', 'returns').then(v => { returns = v; }));
    if (isBoss) tasks.push(_grab('/api/payments/pending', 'pending').then(v => { payPending = v; }));
  }
  // «Мои сдачи» — секция внутри «Платежи и сдачи» (ops) для тех, кто сдаёт.
  if (section === 'ops' && canDeposit) {
    tasks.push(_grab('/api/deposits/my', 'deposits').then(v => { myDeposits = v; }));
  }
  await Promise.all(tasks);

  const depCards = deposits.map(d => {
    const orders = (d.orders || [])
      .map(o => `#${o.order_id} — ${fmt(o.amount_allocated)} ${baseCur()}`).join(', ') || '—';
    return `
      <div class="debt-card" data-dep="${d.id}">
        <div class="debt-card-top">
          <div class="debt-agent">${icon('cash')} Сдача #${d.id}</div>
          <div class="debt-amount">${fmt(d.amount)} ${baseCur()}</div>
        </div>
        <div class="debt-card-mid"><span class="debt-meta">Заказы: ${escapeHtml(orders)}</span></div>
        <div class="debt-actions">
          <button class="btn-confirm-pay dep-confirm">${icon('check')} Подтвердить</button>
          <button class="btn-reject-pay dep-reject">${icon('close')} Отклонить</button>
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
          <div class="debt-agent">${icon('return')} Возврат #${r.id}</div>
          <div class="debt-amount">${fmt(r.total_amount)} ${baseCur()}</div>
        </div>
        <div class="debt-card-mid">
          <span class="debt-meta">Заказ #${r.order_id} · ${escapeHtml(r.reason || '')}</span>
        </div>
        <div class="debt-actions">
          <button class="btn-confirm-pay ret-confirm">${icon('check')} Подтвердить возврат</button>
        </div>
      </div>
  `).join('');

  // Платежи по paid-заказам, ждущие подтверждения боссом (раньше — вкладка
  // «Платежи»; теперь часть «Подтверждений» кассы).
  const payCards = payPending.map(d => `
      <div class="debt-card debt-awaiting" data-pay="${d.order_id}">
        <div class="debt-card-top">
          <div class="debt-agent">${icon('building')} ${escapeHtml(d.agent_name || '—')}</div>
          <div class="debt-amount">${fmt(d.pending)} ${escapeHtml(d.currency || 'USD')}</div>
        </div>
        <div class="debt-card-mid">
          <span class="debt-meta">Заказ #${d.order_id} · из ${fmt(d.total)} ${escapeHtml(d.currency || 'USD')} · ${escapeHtml(d.full_name || '')}</span>
        </div>
        <div class="debt-actions">
          <button class="btn-confirm-pay pay-confirm" data-id="${d.order_id}">${icon('check')} Подтвердить оплату</button>
          <button class="btn-reject-pay pay-reject" data-id="${d.order_id}">${icon('close')} Отклонить</button>
        </div>
      </div>
  `).join('');

  const payBlock = payPending.length
    ? `<div class="section-label section-awaiting">${icon('clock')} Оплаты на подтверждении (${payPending.length})</div><div class="debts-list">${payCards}</div>`
    : '';
  const depBlock = deposits.length
    ? `<div class="section-label">${icon('cash')} Сдачи на подтверждении (${deposits.length})</div><div class="debts-list">${depCards}</div>`
    : '';
  const retBlock = returns.length
    ? `<div class="section-label">${icon('return')} Возвраты на подтверждении (${returns.length})</div><div class="debts-list">${retCards}</div>`
    : '';

  // (Итоги поступлений за период + лента движения денег переехали в Аналитику →
  // «Деньги» — см. renderMoneyView. Здесь касса = только подтверждения и формы.)

  // Блок «сдать наличные» + «мои сдачи» — только для тех, кто физически сдаёт
  // (менеджер/кладовщик). Боссу/бухгалтеру self-deposit бессмысленен.
  let createBlock = '';
  let myBlock = '';
  if (canDeposit) {
    createBlock = `
      <div class="section-label">Сдать наличные</div>
      <div class="card">
        <div class="form-row">
          <label class="form-label" for="dep-amount">Сумма (${baseCur()})</label>
          <input type="number" id="dep-amount" class="form-input" placeholder="500" inputmode="decimal">
        </div>
        <button id="dep-create" class="btn-primary">${icon('cash')} Сдать в кассу</button>
        <div class="debt-hint">Распределится по вашим открытым заказам автоматически.</div>
      </div>
    `;
    const stIcon = { pending: 'clock', confirmed: 'check', rejected: 'close' };
    const rows = myDeposits.map(d => `
      <div class="stock-row">
        <div class="stock-info">
          <div class="stock-name">${icon(stIcon[d.status] || 'cash')} #${d.id} — ${fmt(d.amount)} ${baseCur()}</div>
          <div class="stock-folder">${(d.created_at || '').slice(0, 16)}${d.status === 'rejected' && d.reject_reason ? ' · ' + escapeHtml(d.reject_reason) : ''}</div>
        </div>
      </div>
    `).join('');
    myBlock = myDeposits.length
      ? `<div class="section-label">Мои сдачи</div><div class="stock-list">${rows}</div>`
      : '';
  }

  // Ручной платёж (не связан с заказом: аренда и т.п.) — для не-боссов
  // (бухгалтер/менеджер/кладовщик). Босс только подтверждает, ему не нужен.
  // Несколько валютных строк за один сабмит → отдельные платежи (одно
  // уведомление боссу с кнопкой на каждый). Строки добавляются динамически.
  const payRowHtml = (cur) => `
      <div class="form-row pay-row">
        <input type="number" class="form-input pay-row-amount" placeholder="1500" inputmode="decimal">
        <select class="form-input pay-row-cur">
          ${['USD', 'UZS', 'RUB', 'EUR'].map(c =>
            `<option ${c === (cur || 'USD') ? 'selected' : ''}>${c}</option>`
          ).join('')}
        </select>
        <button class="cur-btn pay-row-del" title="Убрать строку">${icon('close')}</button>
      </div>`;
  const payFormBlock = !isBoss ? `
      <div class="section-label">Новый платёж (не связан с заказом)</div>
      <div class="card">
        <div id="pay-rows">${payRowHtml('USD')}</div>
        <button id="pay-add-row" class="cur-btn">${icon('plus')} Ещё валюта</button>
        <div class="form-row">
          <label class="form-label">Комментарий</label>
          <input type="text" id="pay-comment" class="form-input" placeholder="За май, оплата аренды">
        </div>
        <button id="pay-submit" class="btn-primary">Отправить</button>
        <div id="pay-status" class="pay-status"></div>
      </div>
  ` : '';

  // Блок оформления возврата (менеджер/кладовщик/босс).
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
            <button class="cur-btn active" data-refund="debt_reduction" aria-pressed="true">${icon('trend-down')} В счёт долга</button>
            <button class="cur-btn" data-refund="cash" aria-pressed="false">${icon('cash')} Наличными</button>
            <button class="cur-btn" data-refund="no_refund" aria-pressed="false">${icon('ban')} Без возврата</button>
          </div>
        </div>
        <button id="ret-create" class="btn-primary">${icon('return')} Оформить полный возврат</button>
      </div>
  ` : '';

  // Тело активной секции (вкладки на уровне renderFinance — без 2-го ряда).
  // ops = формы + «Мои сдачи»; иначе (confirm) = подтверждения.
  let bodyHtml;
  if (section === 'ops') {
    bodyHtml = (createBlock + payFormBlock + returnBlock + myBlock)
      || '<div class="loader">Нет доступных операций</div>';
  } else {
    bodyHtml = (payBlock + depBlock + retBlock)
      || '<div class="loader">Нет записей на подтверждении</div>';
  }
  container.innerHTML = bodyHtml;

  // Оформление возврата.
  let selectedRefund = 'debt_reduction';
  container.querySelectorAll('[data-refund]').forEach(b => {
    b.addEventListener('click', () => {
      container.querySelectorAll('[data-refund]').forEach(x => {
        x.classList.remove('active');
        x.setAttribute('aria-pressed', 'false');
      });
      b.classList.add('active');
      b.setAttribute('aria-pressed', 'true');
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
      api('/api/returns/create', { order_id: orderId, reason, refund_method: selectedRefund, idempotency_key: idemKey() })
        .then(r => { tg.showAlert(`✅ Возврат #${r.return_id} отправлен на подтверждение`); renderFinance(); })
        .catch(e => { tg.showAlert('❌ ' + e.message); retBtn.disabled = false; });
    });
  }

  // Мульти-валютный платёж: динамические строки (сумма+валюта) за один сабмит.
  const addRowBtn = container.querySelector('#pay-add-row');
  if (addRowBtn) {
    const rowsBox = container.querySelector('#pay-rows');
    const wireDelete = () => {
      rowsBox.querySelectorAll('.pay-row-del').forEach(btn => {
        btn.onclick = () => {
          // Последнюю строку не удаляем — хотя бы одна нужна.
          if (rowsBox.querySelectorAll('.pay-row').length > 1) btn.closest('.pay-row').remove();
        };
      });
    };
    wireDelete();
    addRowBtn.addEventListener('click', () => {
      // Новая строка с той же валютой, что в последней (удобно вводить серию).
      const last = rowsBox.querySelector('.pay-row:last-child .pay-row-cur');
      const cur = last ? last.value : 'USD';
      const tmp = document.createElement('div');
      tmp.innerHTML = `
        <div class="form-row pay-row">
          <input type="number" class="form-input pay-row-amount" placeholder="1500" inputmode="decimal">
          <select class="form-input pay-row-cur">
            ${['USD', 'UZS', 'RUB', 'EUR'].map(c => `<option ${c === cur ? 'selected' : ''}>${c}</option>`).join('')}
          </select>
          <button class="cur-btn pay-row-del" title="Убрать строку">${icon('close')}</button>
        </div>`;
      rowsBox.appendChild(tmp.firstElementChild);
      wireDelete();
    });
  }
  const paySubmit = container.querySelector('#pay-submit');
  if (paySubmit) {
    paySubmit.addEventListener('click', async () => {
      const status = container.querySelector('#pay-status');
      const comment = container.querySelector('#pay-comment').value.trim();
      const rawRows = Array.from(container.querySelectorAll('.pay-row')).map(r => ({
        amount: r.querySelector('.pay-row-amount').value,
        currency: r.querySelector('.pay-row-cur').value,
      }));
      const parsed = parsePaymentItems(rawRows);
      if (parsed.error) { status.textContent = '❌ ' + parsed.error; status.className = 'pay-status pay-error'; return; }
      if (!comment) { status.textContent = '❌ Укажите комментарий'; status.className = 'pay-status pay-error'; return; }
      paySubmit.disabled = true;
      status.textContent = '⏳ Отправка…'; status.className = 'pay-status';
      try {
        await api('/api/payments/send', { items: parsed.items, comment, idempotency_key: idemKey() });
        tg.HapticFeedback?.notificationOccurred('success');
        renderFinance();
      } catch (e) {
        status.textContent = '❌ ' + e.message; status.className = 'pay-status pay-error';
        paySubmit.disabled = false;
      }
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
      api('/api/deposits/create', { amount, idempotency_key: idemKey() })
        .then(r => { tg.showAlert(`✅ Сдача #${r.deposit_id} отправлена на подтверждение`); renderFinance(); })
        .catch(e => { tg.showAlert('❌ ' + e.message); createBtn.disabled = false; });
    });
  }

  // Сдачи: подтвердить / отклонить (причина — inline).
  container.querySelectorAll('.debt-card[data-dep]').forEach(card => {
    const id = card.dataset.dep;
    card.querySelector('.dep-confirm').addEventListener('click', (ev) => {
      const b = ev.currentTarget;
      if (b.disabled) return;
      b.disabled = true;  // защита от двойного тапа (сервер идемпотентен, UX — нет)
      haptic('light');
      api('/api/deposits/confirm', { deposit_id: Number(id), idempotency_key: idemKey() })
        .then(() => { tg.showAlert('✅ Сдача подтверждена'); renderFinance(); })
        .catch(e => { b.disabled = false; tg.showAlert('❌ ' + e.message); });
    });
    const box = card.querySelector('.dep-reject-box');
    card.querySelector('.dep-reject').addEventListener('click', () => { box.hidden = !box.hidden; });
    card.querySelector('.dep-reject-send').addEventListener('click', (ev) => {
      const b = ev.currentTarget;
      const reason = card.querySelector('.dep-reason').value.trim();
      if (reason.length < 3) { tg.showAlert('❌ Укажите причину'); return; }
      if (b.disabled) return;
      b.disabled = true;
      api('/api/deposits/reject', { deposit_id: Number(id), reason })
        .then(() => { tg.showAlert('❌ Сдача отклонена'); renderFinance(); })
        .catch(e => { b.disabled = false; tg.showAlert('❌ ' + e.message); });
    });
  });

  // Возвраты: подтвердить.
  container.querySelectorAll('.debt-card[data-ret]').forEach(card => {
    card.querySelector('.ret-confirm').addEventListener('click', (ev) => {
      const b = ev.currentTarget;
      if (b.disabled) return;
      b.disabled = true;  // защита от двойного тапа
      haptic('light');
      api('/api/returns/confirm', { return_id: Number(card.dataset.ret), idempotency_key: idemKey() })
        .then(() => { tg.showAlert('✅ Возврат подтверждён'); renderFinance(); })
        .catch(e => { b.disabled = false; tg.showAlert('❌ ' + e.message); });
    });
  });

  // Оплаты по paid-заказам: подтвердить / отклонить (перенесено из «Платежей»).
  container.querySelectorAll('.pay-confirm').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = parseInt(btn.dataset.id, 10);
      tg.showConfirm('Подтверждаете поступление оплаты по заказу #' + id + '?', async ok => {
        if (!ok) return;
        btn.disabled = true;
        try {
          await api('/api/orders/confirm_payment', { order_id: id, idempotency_key: idemKey() });
          tg.HapticFeedback?.notificationOccurred('success');
          renderFinance();
        } catch (e) { tg.showAlert('❌ ' + e.message); btn.disabled = false; }
      });
    });
  });
  container.querySelectorAll('.pay-reject').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = parseInt(btn.dataset.id, 10);
      tg.showConfirm('Отклонить оплату по заказу #' + id + '?', async ok => {
        if (!ok) return;
        btn.disabled = true;
        try {
          await api('/api/orders/reject_payment', { order_id: id, idempotency_key: idemKey() });
          tg.HapticFeedback?.notificationOccurred('warning');
          renderFinance();
        } catch (e) { tg.showAlert('❌ ' + e.message); btn.disabled = false; }
      });
    });
  });
}

// Список «Клиенты» (boss/admin): контрагенты с МС-балансом + локальным долгом/
// лимитом. Тап по карточке → детальная карточка контрагента.
async function renderClients(container) {
  container = container || document.getElementById('content');
  container.innerHTML = loading('Загрузка клиентов…');
  let clients = [];
  let baseC = baseCur();
  try {
    const data = await api('/api/clients/overview', {});
    clients = data.clients || [];
    baseC = data.base_currency || baseC;
  } catch (e) {
    container.innerHTML = errorBox(e.message);
    return;
  }
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const fmtCents = c => opsAmount((Number(c) || 0) / 100);
  if (!clients.length) {
    container.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon">${icon('user')}</div>
      <div class="empty-state-title">Пока нет клиентов</div>
      <div class="empty-state-hint">Контрагенты появятся после синхронизации с МойСклад или первого заказа.</div>
    </div>`;
    return;
  }
  // Сверху — кто больше должен (по МС-балансу), затем без баланса.
  const balOf = c => (c.balance_cents != null ? c.balance_cents : -Infinity);
  clients.sort((a, b) => balOf(b) - balOf(a));
  const balStr = (bal) => bal == null
    ? '<span class="money-placeholder">баланс —</span>'
    : bal > 0 ? `<span class="bal-owe">должен ${fmtCents(bal)} ${escapeHtml(baseC)}</span>`
    : bal < 0 ? `<span class="bal-adv">аванс ${fmtCents(-bal)} ${escapeHtml(baseC)}</span>`
    : `0 ${escapeHtml(baseC)}`;
  const cards = clients.map(c => `
      <div class="debt-card" data-agent="${escapeHtml(c.agent_id)}" role="button" tabindex="0">
        <div class="debt-card-top">
          <div class="debt-agent">${icon('building')} ${escapeHtml(c.agent_name)}</div>
          ${c.over_limit ? '<span class="stock-badge badge-red">лимит превышен</span>' : ''}
        </div>
        <div class="debt-card-mid">
          <span class="debt-meta">${balStr(c.balance_cents)} · долг по заказам ${fmt(c.debt)} · лимит ${fmt(c.limit)}</span>
        </div>
      </div>`).join('');
  container.innerHTML = `<div class="section-label">Клиенты (${clients.length})</div><div class="debts-list">${cards}</div>`;
  container.querySelectorAll('.debt-card[data-agent]').forEach(card => {
    card.addEventListener('click', () => { haptic('light'); renderAgentDetail(card.dataset.agent); });
  });
}

// Карточка контрагента: МС-баланс + локальный долг/лимит (правится) + покупки из
// МС (топ-товары/последние отгрузки) + заказы в боте. Открывается из «Клиентов»
// и из поиска. boss/admin (эндпоинт detail так гейтит).
async function renderAgentDetail(agentId) {
  const content = document.getElementById('content');
  content.innerHTML = loading('Загрузка клиента…');
  showBack(() => { financeTab = 'limits'; showScreen('finance'); });
  let d;
  try {
    d = await api('/api/clients/detail', { agent_id: agentId });
  } catch (e) {
    content.innerHTML = errorBox(e.message);
    return;
  }
  const fmt = n => Math.round(n).toLocaleString('ru-RU');
  const fmtCents = c => opsAmount((Number(c) || 0) / 100);
  const baseC = d.base_currency || baseCur();
  const bal = d.balance_cents;
  const balLine = bal == null ? 'Баланс МойСклад: —'
    : bal > 0 ? `Баланс МойСклад: должен нам <b>${fmtCents(bal)} ${escapeHtml(baseC)}</b>`
    : bal < 0 ? `Баланс МойСклад: аванс <b>${fmtCents(-bal)} ${escapeHtml(baseC)}</b>`
    : `Баланс МойСклад: <b>0 ${escapeHtml(baseC)}</b>`;

  // Покупки из МС.
  const pur = d.purchases || {};
  const topRows = (pur.top_products || []).map(p =>
    `<div class="stock-row"><div class="stock-info"><div class="stock-name">${escapeHtml(p.name)}</div>` +
    `<div class="stock-folder">${fmt(p.qty)} шт · ${fmtCents(p.sum_cents)} ${escapeHtml(baseC)}</div></div></div>`
  ).join('');
  const recentRows = (pur.recent || []).map(r =>
    `<div class="stock-row"><div class="stock-info"><div class="stock-name">${fmtCents(r.sum_cents)} ${escapeHtml(baseC)}</div>` +
    `<div class="stock-folder">${escapeHtml(r.date || '')}</div></div></div>`
  ).join('');
  const purBlock = pur.count
    ? `<div class="section-label">Покупки · ${pur.count} отгр. · ${fmtCents(pur.total_cents)} ${escapeHtml(baseC)}</div>`
      + (topRows ? `<div class="stock-list">${topRows}</div>` : '')
      + (recentRows ? `<div class="section-label">Последние отгрузки</div><div class="stock-list">${recentRows}</div>` : '')
    : '<div class="section-label">Покупки</div><div class="loader">Покупок в МойСклад нет</div>';

  // Заказы в боте.
  const orders = d.orders || [];
  const ordersRows = orders.map(o =>
    `<div class="stock-row"><div class="stock-info">` +
    `<div class="stock-name">#${o.id} · ${fmtCents(o.total_cents)} ${escapeHtml(o.currency || baseC)}</div>` +
    `<div class="stock-folder">${escapeHtml(o.status || '')} · ${escapeHtml((o.created_at || '').slice(0, 16))}</div>` +
    `</div></div>`
  ).join('');
  const ordersBlock = orders.length
    ? `<div class="section-label">Заказы в боте · ${orders.length}</div><div class="stock-list">${ordersRows}</div>`
    : '<div class="section-label">Заказы в боте</div><div class="loader">Заказов нет</div>';

  // Лимит правится только у контрагента с заказами (эндпоинт credit/set это гейтит).
  const limitBlock = orders.length ? `
      <div class="debt-actions"><button class="btn-edit-limit" id="cl-edit">${icon('edit')} Изменить лимит</button></div>
      <div class="limit-edit" id="cl-box" hidden>
        <input type="number" class="form-input" id="cl-input" inputmode="decimal" value="${d.limit}">
        <div class="debt-actions">
          <button class="btn-confirm-pay" id="cl-save">Сохранить</button>
          <button class="btn-reject-pay" id="cl-cancel">Отмена</button>
        </div>
      </div>` : '';

  content.innerHTML = `
    <div class="editor-header"><div class="editor-title">${icon('building')} ${escapeHtml(d.name || '—')}</div></div>
    ${d.phone ? `<div class="debt-meta" style="padding:0 2px 8px;">${icon('phone')} ${escapeHtml(d.phone)}</div>` : ''}
    <div class="card">
      <div class="agent-bal">${balLine}</div>
      <div class="debt-meta">Долг по заказам бота: <b>${fmt(d.debt)} ${escapeHtml(baseC)}</b> · лимит ${fmt(d.limit)} · свободно ${fmt(d.free)}</div>
      ${limitBlock}
    </div>
    ${purBlock}
    ${ordersBlock}
  `;

  if (orders.length) {
    const box = content.querySelector('#cl-box');
    content.querySelector('#cl-edit').addEventListener('click', () => { haptic('light'); box.hidden = !box.hidden; });
    content.querySelector('#cl-cancel').addEventListener('click', () => { box.hidden = true; });
    content.querySelector('#cl-save').addEventListener('click', async (ev) => {
      const btn = ev.currentTarget;
      if (btn.disabled) return;
      const amount = parseNum(content.querySelector('#cl-input').value);
      if (isNaN(amount) || amount < 0) { tg.showAlert('❌ Лимит должен быть неотрицательным числом.'); return; }
      btn.disabled = true;
      try {
        await api('/api/credit/set', { agent_id: d.agent_id, agent_name: d.name, limit_amount: amount });
        toast(`Лимит обновлён: ${fmt(amount)} ${baseC}`);
        renderAgentDetail(agentId);
      } catch (e) { btn.disabled = false; tg.showAlert('❌ ' + e.message); }
    });
  }
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
      // Бакетим по СРОКУ (due_date vs серверный today), а НЕ по d.state.
      // У частично оплаченного долга state='partial' (это прогресс оплаты,
      // не срок) — раньше он молча проваливался в else → «Будущие», даже если
      // срок сегодня/просрочен. Сравнение строк YYYY-MM-DD корректно; today
      // приходит с сервера (единый TZ). Та же логика, что в handlers/debts.py.
      const due = d.due_date;
      if (due && due < today) { overdueCount++; overdueSum += amt; }
      else if (due && due === today) { todayCount++; todaySum += amt; }
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
          <button class="debts-tab ${debtsFilter === 'all' ? 'active' : ''}" data-f="all" aria-pressed="${debtsFilter === 'all'}">Все</button>
          <button class="debts-tab ${debtsFilter === 'today' ? 'active' : ''}" data-f="today" aria-pressed="${debtsFilter === 'today'}">К оплате сейчас</button>
        </div>
      </div>
    `;

    if (totalEmpty) {
      html += `
        <div class="finance-empty">
          <div class="finance-empty-icon">${icon('card')}</div>
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
            <div class="money-label">${icon('cash')} Получено</div>
            <div class="money-value">${receivedRows || '<span class="money-placeholder">пока пусто</span>'}</div>
          </div>
          <div class="money-block money-pending ${pendingItems.length === 0 ? 'money-empty' : ''}">
            <div class="money-label">${icon('clock')} Ждёт подтверждения</div>
            <div class="money-value">${pendingRows || '<span class="money-placeholder">пусто</span>'}</div>
          </div>
        </div>
      `;
      // Единый остаток к получению в базовой валюте — не складывать валюты в уме.
      if (data.remaining_base_total != null) {
        const partial = data.remaining_base_partial
          ? ' <span class="money-placeholder">(часть без курса)</span>' : '';
        html += `<div class="money-base-total">${icon('cash')} Осталось получить: ≈ <b>${fmt(data.remaining_base_total)} ${escapeHtml(data.base_currency || 'USD')}</b>${partial}</div>`;
      }
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
        <div class="section-label section-awaiting">${icon('clock')} Ожидают подтверждения (${awaiting.length})</div>
        <div class="debts-list">${awaiting.map(d => {
          const ownerStr = d.is_mine ? '' : ` <span class="debt-owner">· ${escapeHtml(d.full_name)}</span>`;
          // Покажем разбиение: оплачено / в подтверждении / остаток
          const breakdown = `
            <div class="debt-breakdown">
              ${d.confirmed > 0 ? `<span>${icon('check')} Подтверждено: <b>${fmt(d.confirmed)}</b></span>` : ''}
              <span>${icon('clock')} Ждёт: <b>${fmt(d.pending)}</b></span>
              ${d.remaining > 0 ? `<span>Останется: <b>${fmt(d.remaining - d.pending > 0 ? d.remaining - d.pending : 0)}</b></span>` : ''}
            </div>
          `;
          return `
            <div class="debt-card debt-awaiting">
              <div class="debt-card-top">
                <div class="debt-agent">${icon('building')} ${escapeHtml(d.agent_name)}</div>
                <div class="debt-amount">${fmt(d.total)} ${escapeHtml(d.currency)}</div>
              </div>
              ${breakdown}
              <div class="debt-card-mid">
                <span class="debt-meta">#${d.id} · ${d.items_count} поз.${ownerStr}</span>
              </div>
              ${isBoss ? `
                <div class="debt-actions">
                  <button class="btn-confirm-pay" data-id="${d.id}">${icon('check')} Подтвердить</button>
                  <button class="btn-reject-pay"  data-id="${d.id}">${icon('close')} Отклонить</button>
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
      html += `<div class="section-label">${icon('card')} Открытые (${open.length})</div>`;
      html += '<div class="debts-list">' + open.map(d => {
        const stateClass = `debt-${d.state}`;
        const stateLabel = d.state === 'partial' ? `${icon('info')} Частично оплачен`
          : d.state === 'overdue' ? `${icon('alert')} Просрочен`
          : d.state === 'due_today' ? `${icon('clock')} Сегодня` : `${icon('calendar')} Срок`;
        const dueStr = d.due_date ? formatDateRU(d.due_date) : '—';
        const ownerStr = d.is_mine ? '' : ` <span class="debt-owner">· ${escapeHtml(d.full_name)}</span>`;
        // Для partial показываем сколько уже получено и остаток
        const breakdown = d.confirmed > 0 ? `
          <div class="debt-breakdown">
            <span>${icon('check')} Оплачено: <b>${fmt(d.confirmed)}</b></span>
            <span>Остаток: <b>${fmt(d.remaining)}</b> ${escapeHtml(d.currency)}</span>
          </div>
        ` : '';
        return `
          <div class="debt-card ${stateClass}">
            <div class="debt-card-top">
              <div class="debt-agent">${icon('building')} ${escapeHtml(d.agent_name)}</div>
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
                <button class="btn-mark-paid" data-id="${d.id}">${icon('check')} Отметить</button>
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
          ? 'Отметить полную оплату остатка?\nБосс должен будет подтвердить.'
          : `Отметить получение ${amount}?\nБосс должен будет подтвердить.`;
        tg.showConfirm(msg, async ok => {
          if (!ok) return;
          btn.disabled = true;
          try {
            const payload = { order_id: id, idempotency_key: idemKey() };
            if (amount !== null) payload.amount = amount;
            await api('/api/orders/mark_paid', payload);
            tg.HapticFeedback?.notificationOccurred('success');
            toast('Оплата отмечена, ждёт подтверждения');
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
            await api('/api/orders/confirm_payment', { order_id: id, idempotency_key: idemKey() });
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
            await api('/api/orders/reject_payment', { order_id: id, idempotency_key: idemKey() });
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
    container.innerHTML = errorBox(e.message || String(e));
  }
}

// formatDateRU — в helpers.js (глобал, подключается ПЕРЕД app.js). Юнит-тестируется.


// Тень топбара при прокрутке
window.addEventListener('scroll', () => {
  document.querySelector('.topbar')?.classList.toggle('topbar--shadow', window.scrollY > 4);
}, { passive: true });

// Запуск
init()