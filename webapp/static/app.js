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

// ─── Высота окна ────────────────────────────────────────────────────
// Оболочку меряем ВЫСОТОЙ ОТ TELEGRAM, а не `100dvh`. WebView сообщает свою
// высоту, не зная про нативную шапку клиента и его панели, поэтому `dvh`
// оказывается больше видимой области: низ приложения уходит за край, страница
// прокручивается на пустоту, а плавающее меню будто «висит» не у дна.
// `viewportStableHeight` — высота БЕЗ учёта временных панелей (клавиатура),
// именно её и надо брать под каркас, иначе он прыгает на каждый ввод.
function syncViewport() {
  const h = tg.viewportStableHeight || tg.viewportHeight;
  // Вне Telegram высоты нет — CSS остаётся на 100dvh, это правильный фолбэк.
  if (!h) return;
  document.documentElement.style.setProperty('--tg-viewport', `${h}px`);
}
syncViewport();
tg.onEvent && tg.onEvent('viewportChanged', syncViewport);

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
  content.innerHTML = emptyState({
    icon: 'lock',
    title: 'Доступ не выдан',
    hint: 'Ваш аккаунт пока без прав. Попросите администратора назначить роль — затем откройте приложение снова.',
    action: { label: 'Обновить', onclick: 'location.reload()' },
  });
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
    showScreen(defaultSection(role()) || 'today');
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
// UI-WP-09: разметка и различение «офлайн / ошибка сервера» переехали в
// helpers.js (тестируются юнитами). Здесь остаётся только то, что специфично
// для приложения — как именно перезагрузить текущий экран.
function errorBox(msg) {
  return errorBoxHtml(msg, { retryAttr: 'onclick="showScreen(currentScreen)"' });
}

// ─── Навигация ──────────────────────────────────────

// Старые адреса экранов. Внешние ссылки — из бота, из пушей, из закладок —
// продолжают работать: алиас переводит на новый раздел и сразу открывает ту
// вкладку, куда содержимое переехало.
const LEGACY_SCREENS = {
  home: 'today',
  orders: 'sales',
  finance: 'money',
  analytics: 'sales:report',
  stock: 'stock:catalog',
  machines: 'stock:machines',
  containers: 'stock:containers',
  debts: 'money:debts',
  payments: 'money:ops',
  cashbox: 'money:ops',
  limits: 'clients:limits',
  leads: 'clients:funnel',
};

const SCREEN_TITLES = {
  today: null, sales: 'Продажи', stock: 'Склад', money: 'Деньги',
  clients: 'Клиенты', ops: 'Требует внимания',
};

// Нижняя панель строится из таблицы разделов: набор кнопок зависит от роли.
// Раньше панель была статикой в разметке, и кладовщик видел «Главную», которая
// отвечала 403 — дверь, которая не открывается.
function buildNav() {
  const nav = document.getElementById('bottom-nav');
  if (!nav) return;
  const role = (currentUser && currentUser.role) || 'guest';
  nav.innerHTML = navSections(role).map(s => `
    <button class="nav-item" data-screen="${s.key}">
      <span class="nav-icon">${icon(s.icon)}</span>
      <span class="nav-label">${escapeHtml(s.label)}</span>
    </button>`).join('');
  nav.querySelectorAll('.nav-item').forEach(btn => {
    btn.addEventListener('click', () => showScreen(btn.dataset.screen));
  });
}

// Экраны, у которых нет своей кнопки, всё равно принадлежат разделу — иначе
// при заходе в них ни один таб не подсвечивался.
const NAV_PARENT = { ops: 'today' };

async function showScreen(screen) {
  // Алиас может нести и вкладку: 'sales:report' — раздел «Продажи», вкладка
  // «Отчёт». Разбираем ДО всего остального, чтобы дальше работать с новым.
  const alias = LEGACY_SCREENS[screen];
  if (alias) {
    const [target, tab] = alias.split(':');
    screen = target;
    if (tab) setSectionTab(target, tab);
  }
  currentScreen = screen;
  // Уходя с любого экрана через нав — снимаем «подтвердить закрытие» (его ставит
  // редактор заказа, пока есть несохранённый черновик).
  tg.disableClosingConfirmation && tg.disableClosingConfirmation();

  const navScreen = NAV_PARENT[screen] || screen;
  document.querySelectorAll('.nav-item').forEach(btn => {
    const isActive = btn.dataset.screen === navScreen;
    btn.classList.toggle('active', isActive);
    // aria-current — активный таб для скринридера (визуально это только цвет).
    if (isActive) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });

  setScreenContext(SCREEN_TITLES[screen]);

  const content = document.getElementById('content');

  // Если был открыт экран с MainButton (например, ввод количества) —
  // скрываем её и снимаем обработчик при переключении, иначе кнопка
  // зависнет на других экранах с устаревшим onClick.
  clearMainButton();
  // Корневые табы не показывают нативную «Назад».
  hideBack();

  try {
    switch (screen) {
      case 'today':
        await renderHome();
        break;
      case 'sales':
        await renderSalesScreen();
        break;
      case 'stock':
        await renderStockScreen();
        break;
      case 'money':
        await renderMoneyScreen();
        break;
      case 'clients':
        await renderClientsScreen();
        break;
      case 'ops':
        await renderOpsSummary();
        break;
      default:
        content.innerHTML = `<div class="error">Неизвестный экран: ${escapeHtml(screen)}</div>`;
    }
  } catch (e) {
    // Если render упал — показываем ошибку, а не оставляем старый контент
    content.innerHTML = errorBox(e.message || String(e));
  }
}

// ─── Разделы и их вкладки ───────────────────────────
//
// Состояние вкладки живёт по одной переменной на раздел. Шелл (sectionNavHtml)
// обязан входить в КАЖДЫЙ innerHTML ветки, включая скелетон и ошибку: иначе
// первый же ре-рендер внутри вкладки уносит переключатель вместе с
// обработчиками (UI-BUG-04).
let salesTab = 'orders';     // orders | report
let stockTab = 'catalog';    // catalog | containers | machines
let moneyTab = 'confirm';    // confirm | debts | ops | report
let clientsTab = 'funnel';   // funnel | limits | channel

function setSectionTab(section, tab) {
  if (section === 'sales') salesTab = tab;
  else if (section === 'stock') stockTab = tab;
  else if (section === 'money') moneyTab = tab;
  else if (section === 'clients') clientsTab = tab;
}

function role() { return (currentUser && currentUser.role) || 'guest'; }
function isBossRole() { return ['admin', 'boss'].includes(role()); }
// Техника, контейнеры и каталог — та же тройка ролей, что у их ручек.
function canSeeMachines() { return ['admin', 'boss', 'manager'].includes(role()); }

function sectionTabsFor(section) {
  const r = role();
  const boss = isBossRole();
  if (section === 'sales') {
    return salesTabs({ canSeeReport: ['admin', 'boss', 'manager'].includes(r) });
  }
  if (section === 'stock') {
    return stockTabs({ canSeeGoods: canSeeMachines() });
  }
  if (section === 'money') {
    return moneyTabs({
      isBoss: boss,
      isConfirmer: ['admin', 'boss', 'bookkeeper', 'warehouse_keeper'].includes(r),
      canSeeDebts: ['admin', 'boss', 'manager'].includes(r),
      hasOps: ['admin', 'boss', 'manager', 'warehouse_keeper'].includes(r),
    });
  }
  return clientsTabs({ isBoss: boss });
}

// Шелл раздела: переключатель вкладок + подпись в шапке. Возвращает HTML;
// активная вкладка нормализуется под доступные роли — недоступную или
// неизвестную откатываем на первую.
function sectionShell(section, active) {
  const tabs = sectionTabsFor(section);
  if (!tabs.find(t => t.key === active)) {
    active = (tabs[0] && tabs[0].key) || '';
    setSectionTab(section, active);
  }
  const label = (tabs.find(t => t.key === active) || {}).label || '';
  setScreenContext(tabs.length > 1 ? `${SCREEN_TITLES[section]} · ${label}` : SCREEN_TITLES[section]);
  return { html: sectionNavHtml(tabs, active), active, tabs };
}

function wireSectionNav(root, section, rerender) {
  (root || document).querySelectorAll('.seg-item[data-sect]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      setSectionTab(section, btn.dataset.sect);
      rerender();
    });
  });
}

// Шелл «Продажи» — используется каждой веткой (см. UI-BUG-04).
function salesShellHtml() { return sectionShell('sales', salesTab).html; }
function stockShellHtml() { return sectionShell('stock', stockTab).html; }

async function renderSalesScreen() {
  salesTab = sectionShell('sales', salesTab).active;
  if (salesTab === 'report') await renderSalesReport();
  else await renderOrders();
}

async function renderStockScreen() {
  stockTab = sectionShell('stock', stockTab).active;
  if (stockTab === 'machines') await renderMachines();
  else if (stockTab === 'containers') await renderContainers();
  else {
    // Фильтр категории не тащим из прошлого захода в каталог.
    stockCurrentCat = 'all';
    await renderStock();
  }
}

// Очередь дел — то, ради чего экран открывают. Порядок и состав считает сервер
// (`services/work_queue`): в шаблоне он разъехался бы с ролями.
function workQueueHtml(queue) {
  if (!queue || !queue.length) {
    // Компактной строкой, а не полноэкранным emptyState: тот занимает треть
    // экрана телефона, и «дел нет» выглядело как «экран не загрузился».
    // Полноэкранная пустота уместна там, где она И ЕСТЬ весь экран.
    return `<div class="section-label">Требует вас</div>`
      + `<div class="c-surface c-surface--list"><div class="c-row queue-empty">`
      + `<div class="card-row-icon">${icon('check')}</div>`
      + `<div class="card-row-info"><div class="card-row-title">Всё разобрано</div>`
      + `<div class="card-row-sub">Ничего не ждёт вашего решения прямо сейчас</div></div>`
      + `</div></div>`;
  }
  const total = queue.reduce((a, i) => a + Number(i.count || 0), 0);
  const rows = queue.map(i => `
    <div class="c-row c-row--tap" data-queue="${escapeHtml(i.screen)}"
         data-status="${i.severity === 'crit' ? 'overdue' : i.severity === 'warn' ? 'pending' : 'draft'}"
         role="button" tabindex="0">
      <div class="queue-count">${Number(i.count) || 0}</div>
      <div class="card-row-info">
        <div class="card-row-title">${escapeHtml(i.title)}</div>
        <div class="card-row-sub">${escapeHtml(i.hint || '')}</div>
      </div>
      ${icon('clock')}
    </div>`).join('');
  return `<div class="section-label">Требует вас · ${total}</div>`
    + `<div class="c-surface c-surface--list">${rows}</div>`;
}

// Клик по строке очереди ведёт туда, где дело закрывается. Адрес приходит с
// сервера вместе с пунктом — счётчик без адреса заставляет искать руками то,
// о чём сам же сообщил.
function wireWorkQueue(root) {
  root.querySelectorAll('[data-queue]').forEach(row => {
    row.addEventListener('click', () => {
      haptic();
      const target = row.dataset.queue;
      if (target === 'requests') {
        showScreen('sales');
        if (typeof renderPendingRequests === 'function') setTimeout(renderPendingRequests, 50);
        return;
      }
      const [screen, tab] = target.split(':');
      if (tab) setSectionTab(screen, tab);
      showScreen(screen);
    });
  });
}

// Приветствие по времени суток. Функция, а не переменная внутри renderHome:
// экран без сводки (кладовщик, бухгалтер) здоровается тем же текстом.
function greetWord() {
  const hh = new Date().getHours();
  return hh < 5 ? 'Доброй ночи' : hh < 12 ? 'Доброе утро'
    : hh < 18 ? 'Добрый день' : 'Добрый вечер';
}

async function renderHome() {
  const content = document.getElementById('content');
  content.innerHTML = `
    ${skeleton('hero')}
    ${skeleton('label')}
    ${skeleton('list', 3)}
  `;

  // Очередь доступна всем рабочим ролям, сводка выручки — только тем, кому
  // отвечает /api/home. Кладовщик и бухгалтер получают экран из одной очереди,
  // а не отказ вместо всего раздела.
  const canSeeHome = ['admin', 'boss', 'manager'].includes(role());
  const queuePromise = api('/api/today', {}).catch(() => ({ queue: [] }));

  let data = null;
  if (canSeeHome) {
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
  }
  const queue = (await queuePromise).queue || [];

  if (!data) {
    // Роль без сводки: экран — это очередь и ничего больше.
    const uname0 = (currentUser && currentUser.first_name) || '';
    content.innerHTML =
      `<div class="home-greeting">${greetWord()}${uname0 ? ', ' + escapeHtml(uname0) : ''}</div>`
      + workQueueHtml(queue);
    wireWorkQueue(content);
    return;
  }

  const cur = data.currency || 'USD';
  const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
  const fmtCur = n => `${fmt(n)} ${cur}`;
  const isBoss = data.role === 'admin' || data.role === 'boss';
  const mo = data.my_orders;

  // ─── Приветствие по времени суток ───────────────────
  const uname = (currentUser && (currentUser.first_name || currentUser.full_name || currentUser.name)) || '';
  const greeting = `<div class="home-greeting">${greetWord()}${uname ? ', ' + escapeHtml(uname) : ''}</div>`;

  // ─── Hero: выручка за сегодня + тренд к вчера ────────
  const todayLabel = data.today.scope === 'personal' ? 'Моя выручка сегодня' : 'Выручка компании сегодня';
  const prevRev = data.today.prev_revenue || 0;
  let heroDelta = `<div class="hero-delta">${data.today.shipments} отгр. · ${data.today.clients} клиентов</div>`;
  if (prevRev > 0) {
    const pct = Math.round((data.today.revenue - prevRev) / prevRev * 100);
    const dir = pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat';
    const arrow = pct > 0 ? icon('trend-up') : pct < 0 ? icon('trend-down') : '';
    heroDelta = `<div class="hero-delta" data-trend="${dir}">${arrow} ${pct > 0 ? '+' : ''}${pct}% к вчера · ${data.today.shipments} отгр.</div>`;
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
  // ─── Предупреждение если не привязан к МойСклад ─────
  const linkWarning = (!data.ms_linked && data.role === 'manager') ? `
    <div class="warn-card">
      ${icon('alert', 'warn-ic')} <b>Аккаунт не привязан к МойСклад.</b><br>
      <span class="u-fs-12">Откройте чат с ботом и нажмите /start. Без привязки персональная аналитика недоступна.</span>
    </div>
  ` : '';

  // ─── Две ветки главной (UI-WP-14) ───────────────────
  // /api/home отдаёт РАЗНЫЕ данные менеджеру (свод по своим заказам) и
  // руководству (что ждёт решения + лидерборд). Раньше обе ветки собирались
  // одной разметкой с if-ами внутри, и по коду не было видно, какой экран
  // получится. Теперь это отдельные функции: читаешь ту, чью роль отлаживаешь.

  // Вход в полную операционную сводку — отдельный экран (раньше уходило
  // дайджестом в Telegram).
  const monitoringHtml = () => `
    <div class="section-label">Мониторинг</div>
    <div class="c-surface c-surface--list">
      <div class="c-row c-row--tap" data-att="ops" role="button" tabindex="0">
        <div class="card-row-icon">${icon('clock')}</div>
        <div class="card-row-info">
          <div class="card-row-title">Операционная сводка</div>
          <div class="card-row-sub">Зависшие заявки · склад · синхронизация</div>
        </div>
      </div>
    </div>
  `;

  const leaderboardHtml = () => {
    const top = data.top_employees || [];
    if (!top.length) return '';
    return `
      <div class="section-label">Топ сотрудники · неделя</div>
      <div class="c-surface c-surface--list">
        ${top.map((e, i) => `
          <div class="c-row">
            <div class="card-row-icon rank-chip">${i + 1}</div>
            <div class="card-row-info">
              <div class="card-row-title">${escapeHtml(e.name)}</div>
              <div class="card-row-sub">${e.count} отгрузок</div>
            </div>
            <div><div class="card-row-value">${fmtCur(e.revenue)}</div></div>
          </div>
        `).join('')}
      </div>
    `;
  };

  // Свод по своим заказам — есть у обеих ролей (у руководства ниже лидерборда).
  const myOrdersHtml = () => {
    if (!(mo.total > 0)) return '';
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
      <div class="c-surface c-surface--list">
        ${mo.recent.map(o => `
          <div class="c-row c-row--tap" data-order-id="${o.id}" role="button" tabindex="0">
            <div class="card-row-icon icon-${o.status}">${icon(STATUS_ICON[o.status] || 'list')}</div>
            <div class="card-row-info">
              <div class="card-row-title">${escapeHtml(o.agent_name || ('Заказ #' + o.id))}</div>
              <div class="card-row-sub">Заказ #${o.id} · ${o.created_at}</div>
            </div>
            <span class="stock-badge ${o.status === 'approved' ? 'badge-green' : o.status === 'rejected' ? 'badge-red' : 'badge-yellow'}">${STATUS_NAME[o.status] || o.status}</span>
          </div>
        `).join('')}
      </div>
    ` : '';
    return `<div class="section-label">Мои заказы</div>${statsRow}${recentList}`;
  };

  const bossHome = () => monitoringHtml() + leaderboardHtml() + myOrdersHtml();
  const managerHome = () => myOrdersHtml();

  content.innerHTML =
    greeting + hero + workQueueHtml(queue) + linkWarning + (isBoss ? bossHome() : managerHome());

  wireWorkQueue(content);

  // Вход в операционную сводку — единственная оставшаяся строка-переход.
  document.querySelectorAll('[data-att="ops"]').forEach(row => {
    row.addEventListener('click', () => { haptic(); showScreen('ops'); });
  });

  // Клик по строке недавнего заказа → Заказы
  document.querySelectorAll('[data-order-id]').forEach(row => {
    row.addEventListener('click', () => showScreen('sales'));
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
  content.innerHTML = stockShellHtml() + loading('Загружаю остатки…');
  wireSectionNav(content, 'stock', renderStockScreen);

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
      content.innerHTML = stockShellHtml() + errorBox(e.message);
      wireSectionNav(content, 'stock', renderStockScreen);
      return;
    }
  }

  renderStockContent();
}

// UI-WP-21: пороги остатка отдают ОДИН из трёх статусов, цвет берётся из общей
// матрицы (UI-WP-02). Раньше цвет выбирался тут же классом, поэтому «сделать
// низкий остаток заметнее» означало править и здесь, и в CSS.
function _stockState(stock) {
  if (stock < 20) return 'out';      // включая ноль: продавать по сути нечего
  if (stock < 100) return 'low';
  return 'in_stock';
}

function _stockBadge(stock) {
  const state = _stockState(stock);
  const text = stock <= 0 ? 'нет' : String(stock);
  return `<span class="stock-badge" data-status="${state}">${text}</span>`;
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
    ? (stockData.ms_unavailable
        ? emptyState({
            icon: 'alert',
            title: 'Каталог недоступен',
            hint: 'Не удалось загрузить товары из МойСклад. Проверьте подключение и токен — затем нажмите «Обновить».',
          })
        : emptyState({
            icon: 'box',
            title: 'Товары не найдены',
            hint: 'Попробуйте изменить категорию или поисковый запрос',
          }))
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
        <div class="c-row stock-row"${editAttr}>
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
  ov.className = 'c-overlay price-overlay';
  ov.innerHTML = `
    <div class="c-sheet price-modal" role="dialog" aria-modal="true" aria-labelledby="pe-title">
      <div class="price-modal-title" id="pe-title">${escapeHtml(product.name)}</div>
      <label class="price-field">
        <span>Цена продажи (минимум)</span>
        <input type="number" inputmode="decimal" id="pe-sale" value="${product.sale_price ?? ''}" placeholder="—">
      </label>
      <label class="price-field">
        <span>Себестоимость</span>
        <input type="number" inputmode="decimal" id="pe-cost" value="${product.cost_price ?? ''}" placeholder="—">
      </label>
      <div id="pe-photos"></div>
      <div class="c-actions c-actions--wrap">
        <button class="btn-secondary" id="pe-photo-add">${icon('plus')} Фото</button>
        <button class="btn-secondary" id="pe-post">${icon('cart')} Пост в канал</button>
      </div>
      <div class="price-actions">
        <button class="btn-secondary" id="pe-cancel">Отмена</button>
        <button class="btn-primary" id="pe-save">Сохранить</button>
      </div>
    </div>`;
  document.body.appendChild(ov);

  // Фото товара — тем же механизмом, что у техники: файл живёт в Telegram, у
  // нас идентификаторы. Из МойСклад картинки не тянем — их там нет.
  const reloadPhotos = async () => {
    const box = ov.querySelector('#pe-photos');
    if (!box) return;
    const res = await apiResult('/api/products/photos', { ms_id: msId });
    if (!res.ok) { box.innerHTML = ''; return; }
    box.innerHTML = photoStripHtml(res.body.photos, {
      addId: 'pe-photo-add-hidden', canUpload: false, alt: 'Фото товара',
    });
    await loadPhotos(box, '/api/products/photo', (photoId) => ({
      ms_id: msId, photo_id: photoId,
    }));
  };
  reloadPhotos();

  ov.querySelector('#pe-photo-add').addEventListener('click', () =>
    pickPhotos('/api/products/photo_upload', { ms_id: msId }, reloadPhotos));
  ov.querySelector('#pe-post').addEventListener('click', () =>
    openChannelComposer('showcase', { ms_id: msId }));
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
    ${stockShellHtml()}
    <div class="form-row">
      <input id="stock-search" class="form-input" placeholder="Поиск товара…" value="${escapeHtml(stockSearch)}">
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
  wireSectionNav(content, 'stock', renderStockScreen);   // UI-BUG-04
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

// ─── Экран: Техника ─────────────────────────────────
// Раздел переехал из бота: карточка машины — это десяток полей, история
// моточасов, сделка с покупателем и фотографии, то есть работа для экрана.
// В боте остались быстрый просмотр и ввод моточасов с площадки.
//
// Данные не кэшируем: парк 10–25 машин, а устаревший статус на экране опаснее
// лишнего запроса — по нему принимают решение о продаже.
let machinesFilter = 'all';
let machinesData = null;

async function renderMachines() {
  const content = document.getElementById('content');
  // Шелл — в КАЖДЫЙ innerHTML, включая скелетон и ветку ошибки (UI-BUG-04):
  // иначе первый же ре-рендер уносит вкладки вместе с обработчиками.
  content.innerHTML = stockShellHtml() + skeleton('label') + skeleton('list', 4);
  wireSectionNav(content, 'stock', renderStockScreen);
  setScreenContext('Заказы · Техника');

  let data;
  try {
    data = await api('/api/machines/list', {
      status: machinesFilter === 'all' ? '' : machinesFilter,
    });
  } catch (e) {
    content.innerHTML = stockShellHtml() + errorBox(e.message);
    wireSectionNav(content, 'stock', renderStockScreen);
    return;
  }
  machinesData = data;
  const labels = data.status_labels || {};
  const rows = (data.machines || []).map(m => `
    <div class="c-row c-row--tap" data-machine="${m.id}" data-status="${escapeHtml(m.status || '')}"
         role="button" tabindex="0">
      <div class="card-row-info">
        <div class="card-row-title">${escapeHtml(m.name || '—')}</div>
        <div class="card-row-sub">${machineSubtitle(m)}</div>
      </div>
      <span class="c-badge">${escapeHtml(machineStatusLabel(m.status, labels))}</span>
    </div>`).join('');

  content.innerHTML = stockShellHtml()
    + machineStatusSegHtml(data.counts, machinesFilter, labels)
    + (rows
      ? `<div class="c-surface c-surface--list">${rows}</div>`
      : emptyState({
          icon: 'box',
          title: 'Техники нет',
          hint: machinesFilter === 'all'
            ? 'Здесь появятся экскаваторы: в пути, на складе и проданные.'
            : 'В этом статусе машин нет — выберите другой фильтр.',
        }))
    + `<div class="c-actions"><button class="btn-secondary" id="machine-new">${icon('plus')} Завести машину</button></div>`;
  wireSectionNav(content, 'stock', renderStockScreen);

  content.querySelector('#machine-new')?.addEventListener('click', () => openMachineForm(null));

  content.querySelectorAll('[data-mstatus]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      machinesFilter = btn.dataset.mstatus;
      renderMachines();
    });
  });
  content.querySelectorAll('[data-machine]').forEach(row => {
    row.addEventListener('click', () => {
      haptic('light');
      renderMachineCard(Number(row.dataset.machine));
    });
  });
}

function machineFactsHtml(m, card) {
  const labels = card.status_labels || {};
  const facts = [
    ['Статус', machineStatusLabel(m.status, labels)],
    ['VIN', m.vin || '—'],
    ['Моточасы', m.hours != null ? `${Number(m.hours).toLocaleString('ru-RU')} м/ч` : '—'],
    ['Цена', m.price_cents ? formatMoney(m.price_cents / 100, m.currency || 'USD') : '—'],
  ];
  // Марка, модель и контейнер больше не заводятся через форму, но у старых
  // карточек они есть — прячем строку, а не показываем прочерк.
  const spec = [m.brand, m.model, m.year].filter(Boolean).join(' · ');
  if (spec) facts.push(['Марка и год', spec]);
  // Себестоимости нет в ответе для менеджера — не «пусто», а поля не существует.
  if ('cost_cents' in m) {
    facts.push(['Себестоимость', m.cost_cents ? formatMoney(m.cost_cents / 100, m.currency || 'USD') : '—']);
  }
  for (const [label, key] of [['Локация', 'location'], ['Контейнер', 'container_no'], ['Прибытие', 'eta_date']]) {
    if (m[key]) facts.push([label, m[key]]);
  }
  if (m.notes) facts.push(['Заметки', m.notes]);
  return `<div class="c-surface c-surface--list">${facts.map(([k, v]) => `
    <div class="c-row">
      <div class="card-row-info"><div class="card-row-sub">${escapeHtml(k)}</div></div>
      <div class="card-row-value">${escapeHtml(String(v))}</div>
    </div>`).join('')}</div>`;
}

function machineHoursHtml(hours) {
  if (!hours || !hours.length) return '';
  return `<div class="section-label">Моточасы</div>
    <div class="c-surface c-surface--list">${hours.map(h => `
      <div class="c-row">
        <div class="card-row-info">
          <div class="card-row-title">${Number(h.hours).toLocaleString('ru-RU')} м/ч</div>
          <div class="card-row-sub">${escapeHtml(String(h.recorded_at || '').slice(0, 16))}</div>
        </div>
      </div>`).join('')}</div>`;
}

// График рассрочки. Показываем всё сразу, а не за отдельным тапом: по нему
// принимают решение «звонить ли клиенту», и прятать его за ещё одним действием
// значит не показывать вовсе.
function machineScheduleHtml(deal, today) {
  const rows = deal.payments || [];
  if (!rows.length) return '';
  const cur = deal.currency || 'USD';
  const paidCents = rows.filter(p => p.paid_at).reduce((s, p) => s + Number(p.amount_cents || 0), 0);
  const totalCents = rows.reduce((s, p) => s + Number(p.amount_cents || 0), 0);
  const boss = isMachineBoss();

  const items = rows.map(p => {
    const due = String(p.due_date || '').slice(0, 10);
    const covered = Number(p.covered_cents || 0);
    const amount = Number(p.amount_cents || 0);
    // Частично внесённый платёж — не «не оплачен»: клиент принёс часть, и это
    // должно быть видно, иначе ему позвонят как ничего не заплатившему.
    const partial = !p.paid_at && covered > 0;
    // Состояние платежа: получен / внесена часть / просрочен / впереди — та же
    // матрица цветов, что у долгов по заказам.
    const state = p.paid_at ? 'approved'
      : partial ? 'partial'
      : (due < today ? 'overdue' : 'upcoming');
    const title = p.seq === 0 ? 'Первоначальный взнос' : `Платёж ${p.seq}`;
    const when = p.paid_at
      ? `получен ${escapeHtml(String(p.paid_at).slice(0, 10))}`
      : partial
        ? `внесено ${formatMoney(covered / 100, cur)} из ${formatMoney(amount / 100, cur)} · до ${escapeHtml(due)}`
        : `до ${escapeHtml(due)}`;
    // Взнос переключать нечем: он получен в момент сделки.
    const toggle = boss && p.seq > 0
      ? `<button class="pay-toggle" data-payment="${p.id}" data-paid="${p.paid_at ? '1' : '0'}"
                 aria-label="${p.paid_at ? 'Снять отметку' : 'Отметить полученным'}">${icon(p.paid_at ? 'close' : 'check')}</button>`
      : '';
    return `
      <div class="c-row" data-status="${state}">
        <div class="card-row-info">
          <div class="card-row-title">${title}</div>
          <div class="card-row-sub">${when}</div>
        </div>
        <div class="card-row-value">${formatMoney(Number(p.amount_cents || 0) / 100, cur)}</div>
        ${toggle}
      </div>`;
  }).join('');

  // Прогресс считает сервер (взнос + поступления), но если его нет — считаем
  // по покрытию строк, чтобы блок не пустовал.
  const prog = deal.progress || {};
  const paid = prog.paid_cents != null
    ? Number(prog.paid_cents)
    : rows.reduce((s, p) => s + Number(p.covered_cents || 0), 0);
  const total = prog.planned_cents != null ? Number(prog.planned_cents) : totalCents;
  const left = Math.max(0, total - paid);
  const addBtn = boss && !deal.closed_at
    ? `<div class="c-actions"><button class="btn-secondary" data-receipt-add="${deal.id}">${icon('cash')} Внести оплату</button></div>`
    : '';
  return `
    <div class="items-total schedule-total">
      <span>Получено ${formatMoney(paid / 100, cur)} из ${formatMoney(total / 100, cur)}</span>
      <b>${left > 0 ? `осталось ${formatMoney(left / 100, cur)}` : 'закрыто'}</b>
    </div>
    <div class="c-surface c-surface--list">${items}</div>
    ${addBtn}
    ${machineReceiptsHtml(deal)}`;
}

// Лента фактических поступлений. Клиент платит частями, и «сколько всего
// внесено» без списка взносов проверить нельзя.
function machineReceiptsHtml(deal) {
  const rows = deal.receipts || [];
  if (!rows.length) return '';
  const cur = deal.currency || 'USD';
  const boss = isMachineBoss();
  return `<div class="section-label">Поступления · ${rows.length}</div>
    <div class="c-surface c-surface--list">${rows.map(r => `
      <div class="c-row">
        <div class="card-row-info">
          <div class="card-row-title">${formatMoney(Number(r.amount_cents || 0) / 100, cur)}</div>
          <div class="card-row-sub">${escapeHtml(String(r.received_at || '').slice(0, 16))}${
            r.note ? ' · ' + escapeHtml(r.note) : ''}</div>
        </div>
        ${boss ? `<button class="pay-toggle" data-receipt-del="${r.id}"
                    aria-label="Удалить поступление">${icon('trash')}</button>` : ''}
      </div>`).join('')}</div>`;
}

function openReceiptForm(machineId, deal) {
  const key = idemKey();
  openMachineSheet({
    title: 'Оплата по рассрочке',
    hint: 'Сумма любая — поступления гасят график по порядку',
    fields: [
      { key: 'amount', label: `Сумма, ${deal.currency || 'USD'}`, type: 'number', required: true },
      { key: 'note', label: 'Комментарий' },
    ],
    submitLabel: 'Записать',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/machines/receipt', {
        deal_id: deal.id, amount: data.amount, note: data.note, idempotency_key: key,
      });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast(res.body.deal_closed ? 'Рассрочка закрыта — всё получено' : 'Оплата записана');
      renderMachineCard(machineId);
      return true;
    },
  });
}

function machineDealsHtml(deals, today) {
  if (!deals || !deals.length) return '';
  return deals.map(d => {
    const kind = d.kind === 'credit' ? 'Рассрочка' : 'Продажа';
    const state = d.closed_at ? 'Закрыта' : (d.kind === 'credit' ? `до ${d.due_date || '—'}` : '');
    const meta = [d.buyer_name, d.buyer_phone, d.buyer_passport, state].filter(Boolean).join(' · ');
    const canClose = d.kind === 'credit' && !d.closed_at && isMachineBoss();
    return `
      <div class="section-label">${kind} · ${escapeHtml(String(d.sold_at || '').slice(0, 10))}</div>
      <div class="c-surface c-surface--list">
        <div class="c-row" data-status="${d.closed_at || d.kind === 'sale' ? 'approved' : 'pending'}">
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(d.buyer_name || '—')}</div>
            <div class="card-row-sub">${escapeHtml(meta)}</div>
          </div>
          <div class="card-row-value">${formatMoney(Number(d.price_cents || 0) / 100, d.currency || 'USD')}</div>
        </div>
      </div>
      ${machineScheduleHtml(d, today)}
      ${canClose ? `<div class="c-actions"><button class="btn-secondary" data-deal-close="${d.id}">Закрыть рассрочку досрочно</button></div>` : ''}`;
  }).join('');
}

async function toggleMachinePayment(machineId, paymentId, wasPaid) {
  const res = await apiResult('/api/machines/payment', {
    payment_id: paymentId, paid: !wasPaid,
  });
  if (!res.ok) {
    tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
    // 409 значит «на сервере уже другое» — перечитываем, а не спорим с экраном.
    if (res.status === 409) renderMachineCard(machineId);
    return;
  }
  haptic('success');
  toast(res.body.deal_closed ? 'Рассрочка закрыта — все платежи получены' : 'Отмечено');
  renderMachineCard(machineId);
}

// Кнопки карточки. Что можно — решает сервер (`can_manage`, `next_statuses`):
// рисовать кнопку, на которую ручка ответит 403, значит обещать пользователю
// действие, которого у него нет.
function machineActionsHtml(m, card) {
  const buttons = [`<button class="btn-secondary" data-mact="hours">${icon('gauge')} Моточасы</button>`];
  if (card.can_manage) {
    buttons.push(`<button class="btn-secondary" data-mact="edit">${icon('edit')} Изменить</button>`);
    for (const opt of card.next_statuses || []) {
      buttons.push(
        `<button class="btn-secondary" data-mstatus-to="${escapeHtml(opt.status)}" ` +
        `data-mstatus-label="${escapeHtml(opt.label)}">${escapeHtml(opt.label)}</button>`
      );
    }
    // Продажа и рассрочка — не переход статуса: им нужны цена и покупатель.
    if (['in_transit', 'in_stock', 'reserved'].includes(m.status)) {
      buttons.push('<button class="btn-secondary" data-mact="sale">Продажа</button>');
      buttons.push('<button class="btn-secondary" data-mact="credit">Рассрочка</button>');
    }
    // Удаление только у машины без сделок: продажа — денежный факт, стирать
    // его вместе с карточкой нельзя, такие уводят в архив. Сервер это тоже
    // проверяет, здесь просто не показываем заведомо отказную кнопку.
    if (!(card.deals || []).length) {
      buttons.push(`<button class="btn-secondary btn-danger" data-mact="delete">${icon('trash')} Удалить</button>`);
    }
  }
  return `<div class="c-actions c-actions--wrap">${buttons.join('')}</div>`;
}

async function deleteMachine(machine) {
  if (!await confirmDialog(
    `Удалить карточку «${machine.name || machine.vin}»? Фото и моточасы удалятся вместе с ней.`
  )) return;
  const res = await apiResult('/api/machines/delete', { machine_id: machine.id });
  if (!res.ok) {
    tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
    return;
  }
  haptic('success');
  toast('Машина удалена');
  stockTab = 'machines';
  showScreen('stock');
}

// ─── Экран: Контейнеры ──────────────────────────────
// Что едет, что уже здесь и сошёлся ли состав. Расхождение показываем на обоих
// уровнях: в списке — счётчиком, в карточке — построчно. Сверка, ради которой
// раздел и заводился, не должна требовать открыть каждый контейнер по очереди.
let containersFilter = 'all';
let containersSearch = '';
let _containersSearchTimer = null;

async function renderContainers() {
  const content = document.getElementById('content');
  content.innerHTML = stockShellHtml() + skeleton('label') + skeleton('list', 3);
  wireSectionNav(content, 'stock', renderStockScreen);
  setScreenContext('Заказы · Контейнеры');

  let data;
  try {
    data = await api('/api/containers/list', {
      status: containersFilter === 'all' ? '' : containersFilter,
      search: containersSearch,
    });
  } catch (e) {
    content.innerHTML = stockShellHtml() + errorBox(e.message);
    wireSectionNav(content, 'stock', renderStockScreen);
    return;
  }
  const labels = data.status_labels || {};
  const rows = (data.containers || []).map(c => {
    const d = c.diff || {};
    // Подстрочник отвечает на «надо ли открывать»: расхождения важнее ETA.
    const parts = [];
    if (c.status === 'arrived' && c.arrived_at) parts.push(`прибыл ${String(c.arrived_at).slice(0, 10)}`);
    else if (c.eta_date) parts.push(`ожидается ${c.eta_date}`);
    if (d.total) parts.push(`${d.total} поз.`);
    if (d.mismatch) parts.push(`расхождений: ${d.mismatch}`);
    else if (d.unchecked && c.status === 'arrived') parts.push('не сверен');
    // Заметка — прямо в списке: «что в этом контейнере» спрашивают чаще, чем
    // открывают карточку, и ради одной строки заходить внутрь незачем.
    const note = c.notes
      ? `<div class="card-row-sub card-row-note">${escapeHtml(c.notes)}</div>` : '';
    return `
      <div class="c-row c-row--tap" data-container="${c.id}"
           data-status="${d.mismatch ? 'rejected' : escapeHtml(c.status || '')}"
           role="button" tabindex="0">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(c.number || '—')}</div>
          <div class="card-row-sub">${escapeHtml(parts.join(' · '))}</div>
          ${note}
        </div>
        <span class="c-badge">${escapeHtml(machineStatusLabel(c.status, labels))}</span>
      </div>`;
  }).join('');

  const counts = data.counts || {};
  const seg = ['all', 'in_transit', 'arrived']
    .filter(s => s === 'all' || Number(counts[s] || 0) > 0)
    .map(s => {
      const label = s === 'all' ? 'Все' : machineStatusLabel(s, labels);
      return `<button class="seg-item ${containersFilter === s ? 'active' : ''}" data-cstatus="${s}" ` +
        `aria-pressed="${containersFilter === s}">${escapeHtml(label)} ${Number(counts[s] || 0)}</button>`;
    }).join('');

  content.innerHTML = stockShellHtml()
    + `<div class="seg-row"><div class="seg seg--scroll">${seg}</div></div>`
    + `<div class="search-wrap"><input type="search" id="container-search" class="search-input"
         placeholder="Номер или заметка…" value="${escapeHtml(containersSearch)}"
         autocomplete="off"></div>`
    + (rows
      ? `<div class="c-surface c-surface--list">${rows}</div>`
      : emptyState({
          icon: 'box',
          title: containersSearch ? 'Ничего не найдено' : 'Контейнеров нет',
          hint: containersSearch
            ? 'Проверьте номер или поищите по слову из заметки.'
            : 'Заведите контейнер, когда он выйдет в путь — и будет видно, чего ждать.',
        }))
    + `<div class="c-actions"><button class="btn-secondary" id="container-new">${icon('plus')} Новый контейнер</button></div>`;
  wireSectionNav(content, 'stock', renderStockScreen);

  content.querySelector('#container-new')?.addEventListener('click', () => openContainerForm());

  const searchInput = content.querySelector('#container-search');
  if (searchInput) {
    // Дебаунс: иначе каждый символ — запрос к серверу.
    searchInput.addEventListener('input', e => {
      containersSearch = e.target.value;
      clearTimeout(_containersSearchTimer);
      _containersSearchTimer = setTimeout(renderContainers, 250);
    });
    // Экран перерисовывается целиком, поэтому поле теряет фокус после каждого
    // поиска — без возврата курсора набирать запрос можно только по одной
    // букве. При первом рендере (запрос пуст) фокус не трогаем, чтобы не
    // открывать клавиатуру.
    if (containersSearch) {
      searchInput.focus();
      const end = searchInput.value.length;
      try { searchInput.setSelectionRange(end, end); } catch { /* type=search */ }
    }
  }
  content.querySelectorAll('[data-cstatus]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      containersFilter = btn.dataset.cstatus;
      renderContainers();
    });
  });
  content.querySelectorAll('[data-container]').forEach(row => {
    row.addEventListener('click', () => {
      haptic('light');
      renderContainerCard(Number(row.dataset.container));
    });
  });
}

function openContainerForm() {
  const key = idemKey();
  openMachineSheet({
    title: 'Новый контейнер',
    fields: [
      { key: 'number', label: 'Номер контейнера', required: true,
        hint: 'Пробелы и дефисы можно не убирать' },
      { key: 'eta_date', label: 'Ожидаемое прибытие', type: 'date' },
      { key: 'notes', label: 'Заметки', type: 'textarea' },
    ],
    submitLabel: 'Завести',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/containers/create', { ...data, idempotency_key: key });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Контейнер заведён');
      renderContainerCard(res.body.container_id);
      return true;
    },
  });
}

// Живой поиск по номенклатуре под текстовым полем. Отдельной функцией, потому
// что нужен дважды: при вводе новой позиции и при исправлении уже заведённой.
// Ходит в снапшот (`/api/products/search`), а не в МойСклад: подсказка
// дёргается на каждое нажатие.
function attachProductSearch(anchor, { onPick }) {
  const list = document.createElement('div');
  list.className = 'c-surface c-surface--list product-suggest';
  anchor.after(list);

  let timer = null;
  let inFlight = null;
  const render = async (raw) => {
    const query = String(raw || '').trim();
    inFlight = query;
    if (query.length < 2) { list.innerHTML = ''; return; }
    list.innerHTML = loading('Ищу в каталоге…');
    let rows = [];
    try {
      const data = await api('/api/products/search', { query });
      // Ответ на устаревший запрос: пока он летел, человек допечатал.
      if (inFlight !== query) return;
      rows = data.products || [];
    } catch (e) {
      if (inFlight === query) list.innerHTML = `<div class="loader">${escapeHtml(e.message)}</div>`;
      return;
    }
    if (!rows.length) {
      list.innerHTML = '<div class="loader">В каталоге не найдено — можно вписать своё название</div>';
      return;
    }
    list.innerHTML = rows.map(p => `
      <div class="c-row c-row--tap" data-ms="${escapeHtml(p.ms_id)}"
           data-name="${escapeHtml(p.name || '')}" data-unit="${escapeHtml(p.unit || 'шт')}"
           role="button" tabindex="0">
        <div class="card-row-info"><div class="card-row-title">${escapeHtml(p.name || '')}</div></div>
        <div class="card-row-value">${escapeHtml(p.unit || 'шт')}</div>
      </div>`).join('');
    list.querySelectorAll('[data-ms]').forEach(row => {
      row.addEventListener('click', () => {
        haptic('light');
        list.querySelectorAll('[data-ms]').forEach(r => r.classList.remove('picked'));
        row.classList.add('picked');
        onPick({ ms_id: row.dataset.ms, name: row.dataset.name, unit: row.dataset.unit });
      });
    });
  };

  return {
    search(value) {
      clearTimeout(timer);
      timer = setTimeout(() => render(value), 300);
    },
    now(value) { render(value); },
    clearPicked() {
      list.querySelectorAll('[data-ms]').forEach(r => r.classList.remove('picked'));
    },
    listEl: list,
  };
}

function openContainerItemForm(containerId, arrived) {
  // Выбранный товар держим здесь, а не в поле формы: в накладной регулярно едет
  // то, чего в номенклатуре ещё нет, и свободный ввод обязан оставаться
  // законным — иначе приёмка встаёт до заведения карточки.
  let picked = null;
  const sheet = openMachineSheet({
    title: arrived ? 'Позиция сверх заявленного' : 'Позиция в контейнере',
    hint: arrived
      ? 'Товар, которого не было в заявленном составе. Начните вводить название — подскажем из каталога.'
      : 'Начните вводить название — подскажем товар из каталога. Нет такого — впишите своё.',
    fields: [
      { key: 'name', label: 'Наименование', required: true },
      arrived
        ? { key: 'arrived_qty', label: 'Прибыло', type: 'number', required: true }
        : { key: 'expected_qty', label: 'Заявлено', type: 'number', required: true },
      { key: 'unit', label: 'Единица', value: 'шт' },
    ],
    submitLabel: 'Добавить',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/containers/item_add', {
        container_id: containerId, ...data,
        ms_id: picked ? picked.ms_id : '',
        ms_name: picked ? picked.name : '',
      });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      renderContainerCard(containerId);
      return true;
    },
  });

  const ov = document.querySelector('.c-overlay');
  const nameInput = ov?.querySelector('#ms-f-name');
  const unitInput = ov?.querySelector('#ms-f-unit');
  if (!nameInput) return;
  const suggest = attachProductSearch(nameInput.parentElement, {
    onPick: (p) => {
      picked = p;
      nameInput.value = p.name;
      if (unitInput) unitInput.value = p.unit || 'шт';
      sheet.showErr('');
    },
  });
  nameInput.addEventListener('input', () => {
    // Правка названия после выбора отвязывает товар: иначе человек уверен, что
    // вписал новую позицию, а приход уйдёт на прежнюю карточку.
    if (picked && nameInput.value.trim() !== picked.name) {
      picked = null;
      suggest.clearPicked();
    }
    suggest.search(nameInput.value);
  });
}

// Исправление привязки уже заведённой позиции: выбрать товар из каталога или
// завести карточку по её названию. Нужно постфактум, потому что состав часто
// заводят раньше, чем товар появляется в номенклатуре.
function openItemLinkSheet(containerId, item) {
  let picked = null;
  const sheet = openMachineSheet({
    title: 'Товар в каталоге',
    hint: item.ms_id
      ? `Сейчас: ${item.ms_name || item.name}`
      : 'Позиция ни с чем не связана — приход по ней не пройдёт',
    fields: [{ key: 'search', label: 'Поиск по каталогу', value: item.name }],
    submitLabel: 'Привязать',
    onSubmit: async (_data, { showErr }) => {
      if (!picked) { showErr('Выберите товар из списка'); return false; }
      const res = await apiResult('/api/containers/item_link', {
        container_id: containerId, item_id: item.id,
        ms_id: picked.ms_id, ms_name: picked.name,
      });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Товар привязан');
      renderContainerCard(containerId);
      return true;
    },
  });

  const ov = document.querySelector('.c-overlay');
  const input = ov?.querySelector('#ms-f-search');
  if (!input) return;
  const suggest = attachProductSearch(input.parentElement, {
    onPick: (p) => { picked = p; sheet.showErr(''); },
  });
  input.addEventListener('input', () => {
    if (picked) { picked = null; suggest.clearPicked(); }
    suggest.search(input.value);
  });
  suggest.now(item.name);

  // Карточку заводит ЧЕЛОВЕК кнопкой: автосоздание из приёмки превратило бы
  // каждую опечатку в новый товар справочника.
  const create = document.createElement('button');
  create.className = 'btn-secondary';
  create.innerHTML = `${icon('plus')} Завести «${escapeHtml(item.name)}» в МойСклад`;
  suggest.listEl.after(create);
  create.addEventListener('click', async () => {
    if (create.disabled) return;
    create.disabled = true;
    const res = await apiResult('/api/containers/item_create_product', {
      container_id: containerId, item_id: item.id,
    });
    create.disabled = false;
    if (!res.ok) { sheet.showErr(res.error); return; }
    haptic('success');
    toast(res.body.existed ? 'Товар уже был в каталоге — привязали' : 'Товар заведён');
    sheet.close();
    renderContainerCard(containerId);
  });
}

// Строка состава: заявлено → прибыло и расхождение. Пока контейнер в пути,
// колонки «прибыло» нет вовсе — заполнять её нечем.
function containerItemsHtml(items, arrived, canManage) {
  if (!items.length) {
    return `<div class="c-surface c-surface--pad"><div class="items-empty">Состав не заполнен</div></div>`;
  }
  const rows = items.map(it => {
    const qty = `${formatMoney(it.expected_qty)} ${escapeHtml(it.unit || 'шт')}`;
    // Про каталог говорим сразу, а не в момент оприходования: непривязанная
    // позиция — это остаток, который не доедет до МойСклад, и узнать об этом
    // лучше пока контейнер грузят, а не когда его уже посчитали.
    const link = it.ms_id ? '' : ' · нет в каталоге';
    const sub = (arrived
      ? (it.state === 'unchecked'
          ? `заявлено ${qty} · не сверено`
          : `заявлено ${qty} · прибыло ${formatMoney(it.arrived_qty)}`)
      : `заявлено ${qty}`) + link;
    const mark = it.state === 'short' ? `${formatMoney(it.delta)}`
      : it.state === 'extra' ? `+${formatMoney(it.delta)}`
      : it.state === 'match' ? '✓' : '—';
    const state = it.state === 'short' ? 'rejected'
      : it.state === 'extra' ? 'pending'
      : it.state === 'match' ? 'approved' : 'draft';
    const input = arrived && canManage
      ? `<input type="number" inputmode="decimal" class="qty-input" data-item="${it.id}"
                value="${it.arrived_qty == null ? '' : it.arrived_qty}" aria-label="Прибыло: ${escapeHtml(it.name)}">`
      : `<div class="card-row-value">${escapeHtml(mark)}</div>`;
    return `
      <div class="c-row" data-status="${state}">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(it.name)}</div>
          <div class="card-row-sub">${sub}</div>
        </div>
        ${input}
        ${canManage ? `<button class="pay-toggle" data-item-link="${it.id}"
             aria-label="Товар в каталоге: ${escapeHtml(it.name)}">${icon(it.ms_id ? 'check' : 'search')}</button>` : ''}
        ${canManage ? `<button class="pay-toggle" data-item-del="${it.id}" aria-label="Убрать позицию">${icon('trash')}</button>` : ''}
      </div>`;
  }).join('');
  return `<div class="c-surface c-surface--list">${rows}</div>`;
}

// Выбор поставщика контейнера. Контрагентов берём той же ручкой, что и клиентов
// заказа: справочник МойСклад один, и второй поиск по нему был бы дублем.
function openSupplierPicker(containerId) {
  let picked = null;
  const sheet = openMachineSheet({
    title: 'Поставщик контейнера',
    hint: '«Приёмке» в МойСклад поставщик обязателен',
    fields: [{ key: 'search', label: 'Поиск по названию', placeholder: 'ООО …' }],
    submitLabel: 'Сохранить',
    onSubmit: async (_data, { showErr }) => {
      if (!picked) { showErr('Выберите контрагента из списка'); return false; }
      const res = await apiResult('/api/containers/supplier', {
        container_id: containerId,
        supplier_ms_id: picked.id,
        supplier_name: picked.name,
      });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Поставщик сохранён');
      renderContainerCard(containerId);
      return true;
    },
  });

  const ov = document.querySelector('.c-overlay');
  const input = ov?.querySelector('#ms-f-search');
  const list = document.createElement('div');
  list.className = 'c-surface c-surface--list supplier-list';
  input?.parentElement?.after(list);

  let timer;
  const load = async (search) => {
    list.innerHTML = loading('Загружаю…');
    try {
      const data = await api('/api/agents', { search });
      const rows = data.agents || [];
      list.innerHTML = rows.length
        ? rows.map(a => `
          <div class="c-row c-row--tap" data-supplier="${escapeHtml(a.id)}"
               data-name="${escapeHtml(a.name || '')}" role="button" tabindex="0">
            <div class="card-row-info"><div class="card-row-title">${escapeHtml(a.name || '')}</div></div>
          </div>`).join('')
        : '<div class="loader">Контрагенты не найдены</div>';
      list.querySelectorAll('[data-supplier]').forEach(row => {
        row.addEventListener('click', () => {
          haptic('light');
          picked = { id: row.dataset.supplier, name: row.dataset.name };
          list.querySelectorAll('[data-supplier]').forEach(r => r.classList.remove('picked'));
          row.classList.add('picked');
          sheet.showErr('');
        });
      });
    } catch (e) {
      list.innerHTML = `<div class="loader">${escapeHtml(e.message)}</div>`;
    }
  };
  input?.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(input.value), 400);
  });
  load('');
}

async function renderContainerCard(containerId) {
  const content = document.getElementById('content');
  content.innerHTML = skeleton('label') + skeleton('list', 4);
  setScreenContext('Контейнер');
  showBack(() => { stockTab = 'containers'; showScreen('stock'); });

  let card;
  try {
    card = await api('/api/containers/card', { container_id: containerId });
  } catch (e) {
    content.innerHTML = errorBox(e.message);
    return;
  }
  const c = card.container || {};
  const d = card.diff || {};
  const arrived = c.status === 'arrived';
  const supply = card.supply || {};
  const win = card.edit_window || { open: true };
  const canEdit = win.open;
  const canManage = canEdit;   // ручки состава открыты всем трём ролям

  const facts = [
    ['Статус', machineStatusLabel(c.status, card.status_labels)],
    arrived ? ['Прибыл', String(c.arrived_at || '').slice(0, 10) || '—']
            : ['Ожидается', c.eta_date || '—'],
    ['Позиций', String(d.total || 0)],
    ['Поставщик', supply.supplier_name || '— не выбран'],
  ];
  if (c.notes) facts.push(['Заметки', c.notes]);

  // Итог сверки — крупно и сверху: ради него раздел и существует.
  const verdict = !arrived ? ''
    : d.mismatch
      ? `<div class="c-error">Расхождений: ${d.mismatch} (недостача ${d.short}, лишнее ${d.extra})</div>`
      : d.unchecked
        ? `<div class="items-total schedule-total"><span>Не сверено позиций</span><b>${d.unchecked}</b></div>`
        : `<div class="items-total schedule-total"><span>Состав сошёлся</span><b>${d.total} поз.</b></div>`;

  // Что уехало в МойСклад и что там не приняли. Несопоставленное показываем
  // явно: молча пропущенная позиция — это остаток, которого нет на складе, но
  // который считают существующим.
  let supplyBlock = '';
  if (arrived) {
    const unmatched = supply.unmatched || [];
    supplyBlock = `<div class="section-label">Остатки в МойСклад</div>
      <div class="c-surface c-surface--list">
        <div class="c-row" data-status="${supply.ms_supply_id ? 'approved' : 'pending'}">
          <div class="card-row-info">
            <div class="card-row-title">${supply.ms_supply_id ? 'Приёмка создана' : 'Ещё не оприходовано'}</div>
            <div class="card-row-sub">${supply.synced_at
              ? escapeHtml(String(supply.synced_at).slice(0, 16))
              : 'Цены впишете в МойСклад, когда будет удобно'}</div>
          </div>
        </div>
        ${unmatched.map(u => `
        <div class="c-row${u.item_id && canEdit ? ' c-row--tap' : ''}" data-status="rejected"
             ${u.item_id && canEdit ? `data-unmatched="${u.item_id}" role="button" tabindex="0"` : ''}>
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(u.name)}</div>
            <div class="card-row-sub">${escapeHtml(u.reason)}${u.item_id && canEdit
              ? ' — выберите товар или заведите карточку' : ' — заведите товар и повторите'}</div>
          </div>
          <div class="card-row-value">${formatMoney(u.quantity)}</div>
        </div>`).join('')}
      </div>`;
  }

  const closedNote = arrived && !canEdit
    ? '<div class="items-total schedule-total"><span>Приёмка закрыта</span><b>правки больше не принимаются</b></div>'
    : arrived && win.hours_left != null
      ? `<div class="card-row-sub">Правки принимаются ещё ${Math.ceil(win.hours_left)} ч</div>`
      : '';

  content.innerHTML = `
    <div class="section-label">${escapeHtml(c.number || 'Контейнер')}</div>
    <div class="c-surface c-surface--list">${facts.map(([k, v]) => `
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-sub">${escapeHtml(k)}</div></div>
        <div class="card-row-value">${escapeHtml(String(v))}</div>
      </div>`).join('')}</div>
    ${verdict}
    ${closedNote}
    <div class="c-actions c-actions--wrap">
      ${canEdit ? `<button class="btn-secondary" id="cont-supplier">${icon('building')} Поставщик</button>` : ''}
      ${canEdit ? `<button class="btn-secondary" id="cont-item-add">${icon('plus')} ${arrived ? 'Лишняя позиция' : 'Позиция'}</button>` : ''}
      ${canEdit && arrived ? '<button class="btn-primary" id="cont-save">Сохранить сверку</button>' : ''}
      ${canEdit && !arrived ? '<button class="btn-primary" id="cont-arrive">Отметить прибытие</button>' : ''}
      ${arrived && !supply.ms_supply_id ? `<button class="btn-secondary" id="cont-supply">${icon('box')} Оприходовать</button>` : ''}
      ${arrived && card.can_manage ? `<button class="btn-secondary" id="cont-post">${icon('cart')} Пост в канал</button>` : ''}
      ${canEdit && card.can_manage ? `<button class="btn-secondary btn-danger" id="cont-del">${icon('trash')} Удалить</button>` : ''}
    </div>
    ${supplyBlock}
    <div class="section-label">Состав</div>
    ${containerItemsHtml(card.items || [], arrived, canManage)}
  `;

  content.querySelector('#cont-supplier')?.addEventListener('click', () =>
    openSupplierPicker(containerId));

  content.querySelector('#cont-post')?.addEventListener('click', () =>
    openChannelComposer('arrival', { container_id: containerId }));

  content.querySelector('#cont-supply')?.addEventListener('click', async () => {
    const res = await apiResult('/api/containers/supply', { container_id: containerId });
    if (!res.ok) {
      tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
      return;
    }
    haptic('success');
    toast(`Оприходовано позиций: ${res.body.matched}`);
    renderContainerCard(containerId);
  });

  content.querySelector('#cont-item-add')?.addEventListener('click', () =>
    openContainerItemForm(containerId, arrived));

  content.querySelector('#cont-arrive')?.addEventListener('click', async () => {
    if (!await confirmDialog('Контейнер прибыл? После этого можно проставить фактические количества.')) return;
    const res = await apiResult('/api/containers/arrive', { container_id: containerId });
    if (!res.ok) {
      tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
      if (res.status === 409) renderContainerCard(containerId);
      return;
    }
    haptic('success');
    toast('Контейнер прибыл');
    renderContainerCard(containerId);
  });

  content.querySelector('#cont-save')?.addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    if (btn.disabled) return;
    // Шлём весь состав одним запросом: приёмщик считает подряд и не должен
    // ждать сети после каждой позиции.
    const quantities = {};
    content.querySelectorAll('.qty-input[data-item]').forEach(el => {
      quantities[el.dataset.item] = el.value.trim();
    });
    if (!Object.keys(quantities).length) return;
    btn.disabled = true;
    const res = await apiResult('/api/containers/check', {
      container_id: containerId, quantities,
    });
    btn.disabled = false;
    if (!res.ok) {
      tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
      return;
    }
    haptic('success');
    toast('Сверка сохранена');
    renderContainerCard(containerId);
  });

  content.querySelector('#cont-del')?.addEventListener('click', async () => {
    if (!await confirmDialog(`Удалить контейнер ${c.number}? Состав удалится вместе с ним.`)) return;
    const res = await apiResult('/api/containers/delete', { container_id: containerId });
    if (!res.ok) {
      tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
      return;
    }
    haptic('success');
    toast('Контейнер удалён');
    stockTab = 'containers';
    showScreen('stock');
  });

  content.querySelectorAll('[data-item-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const res = await apiResult('/api/containers/item_delete', {
        container_id: containerId, item_id: Number(btn.dataset.itemDel),
      });
      if (!res.ok) { tg.showAlert ? tg.showAlert(res.error) : alert(res.error); return; }
      haptic('light');
      renderContainerCard(containerId);
    });
  });

  // Привязка к каталогу открывается и из состава, и из списка несопоставленного:
  // именно там человек узнаёт, что позиция никуда не попала.
  const itemById = new Map((card.items || []).map(i => [String(i.id), i]));
  const openLink = (id) => {
    const it = itemById.get(String(id));
    if (it) openItemLinkSheet(containerId, it);
  };
  content.querySelectorAll('[data-item-link]').forEach(btn =>
    btn.addEventListener('click', () => openLink(btn.dataset.itemLink)));
  content.querySelectorAll('[data-unmatched]').forEach(row =>
    row.addEventListener('click', () => openLink(row.dataset.unmatched)));
}

// ─── Фотографии машины ──────────────────────────────
// Файл едет через нашу ручку (`POST` + initData), поэтому <img src> сюда не
// годится: прямая ссылка Telegram содержит токен бота, а браузер не умеет
// POST'ить за картинкой. Тянем байты fetch'ем и показываем как blob-URL.
//
// Blob-URL живёт до закрытия документа: без revoke каждая пересборка карточки
// оставляет копию снимка в памяти WebView, и за сессию их накапливаются
// десятки мегабайт.
let _photoUrls = [];

function revokePhotoUrls() {
  for (const url of _photoUrls) {
    try { URL.revokeObjectURL(url); } catch { /* уже отозван */ }
  }
  _photoUrls = [];
}

// Лента фото — общая для техники и товаров: механизм один (файл в Telegram,
// у нас идентификаторы), и различаются только ручка и её тело.
function photoStripHtml(photos, { addId, canUpload, alt }) {
  const rows = photos || [];
  const upload = canUpload
    ? `<button class="btn-secondary" id="${addId}">${icon('plus')} Добавить фото</button>`
    : '';
  if (!rows.length) {
    return upload
      ? `<div class="section-label">Фото</div><div class="c-actions">${upload}</div>`
      : '';
  }
  const strip = rows.map(p =>
    `<button class="machine-photo" data-photo="${p.id}" aria-label="${escapeHtml(p.caption || alt)}">` +
    `<img alt="${escapeHtml(p.caption || '')}" loading="lazy"></button>`
  ).join('');
  return `<div class="section-label">Фото · ${rows.length}</div>
    <div class="machine-photos">${strip}</div>
    ${upload ? `<div class="c-actions">${upload}</div>` : ''}`;
}

// Файл едет через нашу ручку (POST + initData), поэтому <img src> не годится:
// прямая ссылка Telegram содержит токен бота, а браузер не умеет POST'ить за
// картинкой. Тянем байты fetch'ем и показываем как blob-URL.
async function loadPhotos(root, endpoint, bodyFor) {
  for (const btn of (root || document).querySelectorAll('.machine-photo[data-photo]')) {
    try {
      const r = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initData: _initData, ...bodyFor(Number(btn.dataset.photo)) }),
      });
      if (!r.ok) throw new Error('нет фото');
      const url = URL.createObjectURL(await r.blob());
      _photoUrls.push(url);
      const img = btn.querySelector('img');
      if (img) img.src = url;
    } catch {
      // Одно недоступное фото не должно ронять ленту — прячем плитку.
      btn.remove();
    }
  }
}

function machinePhotosHtml(card) {
  return photoStripHtml(card.photos, {
    addId: 'machine-photo-add',
    canUpload: card.can_upload_photo,
    alt: 'Фото машины',
  });
}

async function loadMachinePhotos(machineId, root) {
  await loadPhotos(root, '/api/machines/photo', (photoId) => ({
    machine_id: machineId, photo_id: photoId,
  }));
}

// Ужимаем снимок ДО отправки: телефонные 3–5 МБ упираются в лимит ручки, а на
// экране всё равно видно не больше 1600px по длинной стороне.
function shrinkImage(file, maxSide = 1600, quality = 0.8) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('Не удалось прочитать файл'));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('Это не изображение'));
      img.onload = () => {
        const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(img.width * scale);
        canvas.height = Math.round(img.height * scale);
        canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL('image/jpeg', quality));
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

// Загрузка пачкой. Снимки уходят ПО ОДНОМУ и последовательно, а не веером:
// каждый сохраняется отправкой в Telegram, а он ограничивает частоту сообщений
// в один чат — двадцать параллельных запросов упёрлись бы в отказ на середине.
// Последовательность заодно даёт честный ход дела и внятный итог.
//
// Экран перерисовываем ОДИН раз в конце: перерисовка на каждом снимке сбрасывает
// прокрутку и мигает половиной списка.
function pickPhotos(endpoint, body, onDone) {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'image/*';
  input.multiple = true;
  input.addEventListener('change', async () => {
    const files = Array.from(input.files || []);
    if (!files.length) return;

    const single = files.length === 1;
    const progress = toast(
      single ? 'Загружаю фото…' : `Загружаю 1 из ${files.length}…`,
      'info', { sticky: true },
    );
    let added = 0;
    let duplicates = 0;
    const failed = [];

    for (let i = 0; i < files.length; i++) {
      if (!single) progress.update(`Загружаю ${i + 1} из ${files.length}…`);
      let dataUrl;
      try {
        dataUrl = await shrinkImage(files[i]);
      } catch (e) {
        // Не картинка или битый файл — пропускаем, но не молча: остальные
        // снимки не должны страдать из-за одного постороннего.
        failed.push(`${files[i].name}: ${e.message}`);
        continue;
      }
      let res = await apiResult(endpoint, { ...body, data_url: dataUrl });
      if (!res.ok) {
        // Одна повторная попытка: на длинной пачке Telegram притормаживает
        // отправку, и это проходит само за секунду-другую.
        await new Promise(r => setTimeout(r, 1500));
        res = await apiResult(endpoint, { ...body, data_url: dataUrl });
      }
      if (!res.ok) failed.push(`${files[i].name}: ${res.error}`);
      else if (res.body.duplicate) duplicates += 1;
      else added += 1;
    }

    progress.dismiss();
    if (onDone) onDone();
    haptic(failed.length ? 'error' : 'success');

    // Итог одной строкой и без вранья: «загружено 8» при трёх упавших — это
    // ложь, из-за которой недостающие снимки заметят через неделю.
    const parts = [];
    if (added) parts.push(single ? 'Фото добавлено' : `Загружено: ${added}`);
    if (duplicates) parts.push(`уже были: ${duplicates}`);
    if (failed.length) parts.push(`не прошли: ${failed.length}`);
    if (!parts.length) parts.push('Ничего не загрузилось');
    toast(parts.join(' · '), failed.length ? 'error' : 'success');
    if (failed.length) {
      const text = 'Не загрузились:\n' + failed.slice(0, 5).join('\n')
        + (failed.length > 5 ? `\n…и ещё ${failed.length - 5}` : '');
      tg.showAlert ? tg.showAlert(text) : alert(text);
    }
  });
  input.click();
}

function pickMachinePhoto(machineId) {
  pickPhotos('/api/machines/photo_upload', { machine_id: machineId },
    () => renderMachineCard(machineId));
}

async function renderMachineCard(machineId) {
  const content = document.getElementById('content');
  // Прошлые снимки больше не на экране — их blob-URL'ы держат память WebView.
  revokePhotoUrls();
  content.innerHTML = skeleton('label') + skeleton('list', 5);
  setScreenContext('Техника · карточка');
  showBack(() => { stockTab = 'machines'; showScreen('stock'); });

  let card;
  try {
    card = await api('/api/machines/card', { machine_id: machineId });
  } catch (e) {
    content.innerHTML = errorBox(e.message);
    return;
  }
  const m = card.machine || {};
  content.innerHTML = `
    <div class="section-label">${escapeHtml(m.name || 'Машина')}</div>
    ${machineFactsHtml(m, card)}
    ${machineActionsHtml(m, card)}
    ${machinePhotosHtml(card)}
    ${machineHoursHtml(card.hours)}
    ${machineDealsHtml(card.deals, card.today || new Date().toISOString().slice(0, 10))}
  `;

  content.querySelector('#machine-photo-add')?.addEventListener('click', () => {
    haptic('light');
    pickMachinePhoto(machineId);
  });
  loadMachinePhotos(machineId, content);

  content.querySelectorAll('[data-mact]').forEach(btn => {
    btn.addEventListener('click', () => {
      const act = btn.dataset.mact;
      if (act === 'hours') openHoursForm(m);
      else if (act === 'edit') openMachineForm(m);
      else if (act === 'delete') deleteMachine(m);
      else openDealForm(m, act);
    });
  });
  content.querySelectorAll('[data-mstatus-to]').forEach(btn => {
    btn.addEventListener('click', () =>
      changeMachineStatus(m, btn.dataset.mstatusTo, btn.dataset.mstatusLabel));
  });
  content.querySelectorAll('[data-deal-close]').forEach(btn => {
    btn.addEventListener('click', () => closeMachineDeal(m.id, Number(btn.dataset.dealClose)));
  });
  content.querySelectorAll('[data-payment]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      toggleMachinePayment(m.id, Number(btn.dataset.payment), btn.dataset.paid === '1');
    });
  });
  content.querySelectorAll('[data-receipt-add]').forEach(btn => {
    btn.addEventListener('click', () => {
      const deal = (card.deals || []).find(x => String(x.id) === btn.dataset.receiptAdd);
      if (deal) openReceiptForm(machineId, deal);
    });
  });
  content.querySelectorAll('[data-receipt-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!await confirmDialog('Удалить это поступление?')) return;
      const res = await apiResult('/api/machines/receipt_delete', {
        receipt_id: Number(btn.dataset.receiptDel),
      });
      if (!res.ok) {
        tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
        return;
      }
      haptic('success');
      toast(res.body.deal_reopened ? 'Поступление удалено, рассрочка снова открыта' : 'Удалено');
      renderMachineCard(machineId);
    });
  });
}

// Модалка-форма для техники. Одна на все четыре случая (машина, моточасы,
// сделка, подтверждение): поведение оболочки — Esc, ловушка Tab, аппаратная
// «назад», возврат фокуса — писать четыре раза значит забыть его в одном месте.
// Образец — openPriceEditor, но там оно вшито в конкретную форму.
function openMachineSheet({ title, fields, submitLabel, hint, onSubmit }) {
  haptic('light');
  const trigger = document.activeElement;
  const prevBack = _backHandler;
  const ov = document.createElement('div');
  ov.className = 'c-overlay';
  const fieldHtml = (f) => {
    const id = `ms-f-${f.key}`;
    const common = `id="${id}" name="${escapeHtml(f.key)}"`;
    const value = f.value == null ? '' : String(f.value);
    const input = f.type === 'textarea'
      ? `<textarea ${common} rows="2" placeholder="${escapeHtml(f.placeholder || '')}">${escapeHtml(value)}</textarea>`
      : `<input ${common} type="${f.type || 'text'}"${f.type === 'number' ? ' inputmode="decimal"' : ''} ` +
        `value="${escapeHtml(value)}" placeholder="${escapeHtml(f.placeholder || '')}">`;
    return `<label class="c-field"><span>${escapeHtml(f.label)}${f.required ? ' *' : ''}</span>${input}` +
      `${f.hint ? `<span class="c-field-hint">${escapeHtml(f.hint)}</span>` : ''}</label>`;
  };
  ov.innerHTML = `
    <div class="c-sheet" role="dialog" aria-modal="true" aria-labelledby="ms-title">
      <div class="c-sheet-title" id="ms-title">${escapeHtml(title)}</div>
      ${hint ? `<div class="c-field-hint">${escapeHtml(hint)}</div>` : ''}
      ${(fields || []).map(fieldHtml).join('')}
      <div class="c-error" id="ms-error" hidden></div>
      <div class="c-actions">
        <button class="btn-secondary" id="ms-cancel">Отмена</button>
        <button class="btn-primary" id="ms-submit">${escapeHtml(submitLabel || 'Сохранить')}</button>
      </div>
    </div>`;
  document.body.appendChild(ov);

  const close = () => {
    ov.remove();
    document.removeEventListener('keydown', onKey, true);
    if (prevBack) showBack(prevBack); else hideBack();
    if (trigger && trigger.focus) { try { trigger.focus(); } catch (_e) {} }
  };
  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); close(); return; }
    if (e.key !== 'Tab') return;
    const f = ov.querySelectorAll('input, textarea, button');
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  document.addEventListener('keydown', onKey, true);
  showBack(close);
  ov.addEventListener('click', e => { if (e.target === ov) close(); });
  ov.querySelector('#ms-cancel').addEventListener('click', close);
  const firstInput = ov.querySelector('input, textarea');
  if (firstInput && firstInput.focus) firstInput.focus();

  const errEl = ov.querySelector('#ms-error');
  const showErr = (msg) => {
    // Ошибка остаётся в форме вместе с введёнными данными: showAlert закрывает
    // всё поверх, и при опечатке в одном поле пришлось бы набирать заново.
    errEl.textContent = msg;
    errEl.hidden = !msg;
  };
  const values = () => {
    const out = {};
    for (const f of fields || []) {
      const el = ov.querySelector(`#ms-f-${f.key}`);
      out[f.key] = el ? el.value.trim() : '';
    }
    return out;
  };
  const submitBtn = ov.querySelector('#ms-submit');
  submitBtn.addEventListener('click', async () => {
    if (submitBtn.disabled) return;
    const data = values();
    const missing = (fields || []).find(f => f.required && !data[f.key]);
    if (missing) { showErr(`Заполните: ${missing.label}`); return; }
    submitBtn.disabled = true;
    showErr('');
    try {
      const done = await onSubmit(data, { close, showErr });
      if (done !== false) close();
    } finally {
      submitBtn.disabled = false;
    }
  });
  return { close, showErr };
}

function isMachineBoss() {
  return !!currentUser && ['admin', 'boss'].includes(currentUser.role);
}

function openMachineForm(machine) {
  const editing = !!machine;
  const m = machine || {};
  const cents = (v) => (v ? String(v / 100) : '');
  // Марки и модели в форме нет: они и так входят в название («JCB 3CX 2019»),
  // а два поля с теми же словами приходилось заполнять дважды. Контейнер
  // отслеживается отдельной сущностью, а не строкой в карточке машины.
  const fields = [
    { key: 'vin', label: 'VIN / серийный номер', required: true, value: m.vin || '',
      hint: editing ? 'Исправляется только при опечатке' : 'Пробелы и дефисы можно не убирать' },
    { key: 'name', label: 'Название', required: true, value: m.name || '', placeholder: 'JCB 3CX 2019' },
    { key: 'year', label: 'Год', type: 'number', value: m.year || '' },
  ];
  if (!editing) fields.push({ key: 'hours', label: 'Моточасы', type: 'number' });
  fields.push({ key: 'price', label: 'Цена, USD', type: 'number', value: cents(m.price_cents) });
  if (isMachineBoss()) {
    fields.push({ key: 'cost', label: 'Себестоимость, USD', type: 'number', value: cents(m.cost_cents) });
  }
  fields.push(
    { key: 'location', label: 'Локация', value: m.location || '' },
    { key: 'eta_date', label: 'Прибытие', type: 'date', value: m.eta_date || '' },
    { key: 'notes', label: 'Заметки', type: 'textarea', value: m.notes || '' },
  );

  const key = idemKey();
  openMachineSheet({
    title: editing ? 'Изменить машину' : 'Новая машина',
    fields,
    submitLabel: editing ? 'Сохранить' : 'Завести',
    onSubmit: async (data, { showErr }) => {
      const res = editing
        ? await apiResult('/api/machines/update', { machine_id: m.id, fields: data })
        : await apiResult('/api/machines/create', { ...data, idempotency_key: key });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast(editing ? 'Карточка обновлена' : 'Машина заведена');
      if (editing) renderMachineCard(m.id); else renderMachines();
      return true;
    },
  });
}

function openHoursForm(machine) {
  openMachineSheet({
    title: 'Моточасы',
    hint: machine.hours != null ? `Сейчас: ${Number(machine.hours).toLocaleString('ru-RU')} м/ч` : '',
    fields: [{ key: 'hours', label: 'Показание счётчика', type: 'number', required: true }],
    submitLabel: 'Записать',
    onSubmit: async (data, { showErr }) => {
      let res = await apiResult('/api/machines/hours', { machine_id: machine.id, hours: data.hours });
      // 409 + needs_force: показание меньше предыдущего. Обычно это опечатка
      // (1500 вместо 15000), поэтому спрашиваем — но подтвердить замену
      // счётчика сервер разрешит только руководителю.
      if (!res.ok && res.body.needs_force) {
        if (!isMachineBoss()) { showErr(res.error + ' Откат подтверждает руководитель.'); return false; }
        const agreed = await confirmDialog(
          `${res.error}\n\nЗаписать ${data.hours} м/ч как замену счётчика?`
        );
        if (!agreed) return false;
        res = await apiResult('/api/machines/hours', {
          machine_id: machine.id, hours: data.hours, force: true,
        });
      }
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Моточасы записаны');
      renderMachineCard(machine.id);
      return true;
    },
  });
}

function openDealForm(machine, kind) {
  const credit = kind === 'credit';
  const key = idemKey();
  openMachineSheet({
    title: credit ? 'Рассрочка' : 'Продажа',
    hint: credit ? 'График платежей построится сам: остаток разделится поровну по месяцам.' : '',
    fields: [
      { key: 'price', label: 'Цена, USD', type: 'number', required: true,
        value: machine.price_cents ? String(machine.price_cents / 100) : '' },
      // Взнос и срок вместо даты последнего платежа: дату считает сервер по
      // графику — введённая руками, она рано или поздно разошлась бы с ним.
      ...(credit ? [
        { key: 'down_payment', label: 'Первоначальный взнос, USD', type: 'number',
          hint: 'Сколько клиент уже заплатил. Можно оставить пустым' },
        { key: 'months', label: 'Срок, месяцев', type: 'number', required: true },
      ] : []),
      { key: 'buyer_name', label: 'Покупатель', required: true },
      { key: 'buyer_phone', label: 'Телефон', type: 'tel' },
      { key: 'buyer_passport', label: 'Паспорт', hint: 'Виден только руководству' },
      { key: 'buyer_note', label: 'Комментарий', type: 'textarea' },
    ],
    submitLabel: credit ? 'Оформить рассрочку' : 'Оформить продажу',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/machines/deal', {
        machine_id: machine.id, kind, ...data, idempotency_key: key,
      });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast(credit
        ? `Рассрочка оформлена · ${res.body.payments} платежей`
        : 'Машина продана');
      renderMachineCard(machine.id);
      return true;
    },
  });
}

// tg.showConfirm — колбэчный; оборачиваем в промис, чтобы формы читались
// сверху вниз. Вне Telegram (отладка в браузере) падает обратно на confirm().
function confirmDialog(text) {
  return new Promise(resolve => {
    try {
      if (tg && tg.showConfirm) { tg.showConfirm(text, ok => resolve(!!ok)); return; }
    } catch { /* вне Telegram */ }
    resolve(typeof confirm === 'function' ? confirm(text) : true);
  });
}

async function changeMachineStatus(machine, target, label) {
  if (!await confirmDialog(`Перевести машину в статус «${label}»?`)) return;
  const res = await apiResult('/api/machines/status', {
    machine_id: machine.id, status: target, expected: machine.status,
  });
  if (!res.ok) {
    // 409 значит «на сервере уже другой статус» — перечитываем карточку, а не
    // просто показываем текст: пользователь смотрит на устаревшие данные.
    tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
    if (res.status === 409) renderMachineCard(machine.id);
    return;
  }
  haptic('success');
  toast('Статус изменён');
  renderMachineCard(machine.id);
}

async function closeMachineDeal(machineId, dealId) {
  if (!await confirmDialog('Закрыть рассрочку — деньги получены полностью?')) return;
  const res = await apiResult('/api/machines/deal_close', {
    deal_id: dealId, idempotency_key: idemKey(),
  });
  if (!res.ok) {
    tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
    if (res.status === 409) renderMachineCard(machineId);
    return;
  }
  haptic('success');
  toast('Рассрочка закрыта');
  renderMachineCard(machineId);
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
  // `sticky` — тост, который гасит вызывающий код: длинная операция должна
  // показывать ход дела ОДНОЙ строкой, а не сыпать по тосту на шаг.
  if (!opts.sticky) timer = setTimeout(dismiss, duration);
  return {
    dismiss,
    update(text) {
      const msgEl = el.querySelector('.toast-msg');
      if (msgEl) msgEl.textContent = text;
    },
  };
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
  paid:     'check',
  partially_returned: 'return',
  returned: 'return',
  cancelled: 'close',
};

const STATUS_NAME = {
  draft:    'Черновик',
  pending:  'На рассмотрении',
  approved: 'Одобрено',
  rejected: 'Отклонено',
  shipped:  'Отгружено',
  paid:     'Оплачено',
  partially_returned: 'Частичный возврат',
  returned: 'Возврат',
  cancelled: 'Отменён',
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

// Тот же запрос, но с полным телом ответа вместо исключения.
//
// `api()` бросает Error(detail) и остальное тело теряет. Формам техники этого
// мало: на 409 сервер присылает `needs_force` (подтвердить замену счётчика) или
// `current` (машину уже перевели в другой статус) — по ним форма предлагает
// действие, а не просто печатает текст. Отдельная функция, а не перепись
// `api()`: у полусотни её вызовов поведение «бросай на ошибке» правильное.
async function apiResult(path, body) {
  let r;
  try {
    r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: _initData, ...body }),
    });
  } catch {
    return { ok: false, status: 0, body: {}, error: 'Нет подключения к интернету' };
  }
  let data = {};
  try { data = await r.json(); } catch { /* не-JSON: 502/HTML от прокси */ }
  return {
    ok: r.ok,
    status: r.status,
    body: data,
    error: data.detail || `Ошибка сервера (${r.status})`,
  };
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
      <div class="search-item" role="button" tabindex="0" onclick="showScreen('sales')">
        <b>#${o.id}</b> · ${escapeHtml(o.agent_name)} · ${escapeHtml(o.status || '')}
        <span class="search-meta">${escapeHtml(o.full_name)}</span>
      </div>`).join(''));
  }
  if (data.payments && data.payments.length) {
    parts.push(`<div class="search-group-title">${icon('cash')} Платежи</div>`);
    parts.push(data.payments.map(p => `
      <div class="search-item" role="button" tabindex="0" onclick="showScreen('money')">
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
    // Подпись в подвале календаря — полные даты (место есть) и промежуточное
    // состояние «выбрано только начало», которого нет у общего rangeLabel.
    const calRangeText = from
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
          <span class="cal-range">${calRangeText}</span>
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
    content.innerHTML = salesShellHtml() + loading('Загружаю заказы…');
    wireSectionNav(content, 'sales', renderSalesScreen);
    try {
      ordersData = await api('/api/orders', {});
      ordersDataTs = Date.now();
    } catch (e) {
      content.innerHTML = salesShellHtml() + errorBox(e.message);
      wireSectionNav(content, 'sales', renderSalesScreen);
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

  // Единый язык навигации со всеми экранами (база — Аналитика): статус и период
  // — iOS-сегменты .seg / .seg-item с подписью-секцией сверху, а не разнородные
  // пилюли + select + кнопка в один ряд (раньше «кнопки смешаны»).
  const statusSeg = filters.map(f =>
    `<button class="seg-item ${currentOrderFilter === f.id ? 'active' : ''}" data-filter="${f.id}" aria-pressed="${currentOrderFilter === f.id}" aria-label="${escapeHtml(f.name)}" title="${escapeHtml(f.name)}">${f.label}</button>`
  ).join('');

  // Фильтр по периоду — для навигации, когда заказов много. Пресеты — сегмент,
  // «Период…» (произвольный диапазон) — доп-кнопка справа (как в Аналитике).
  const periods = [
    { id: 'all', label: 'Всё' },   // UI-BUG-01: четыре пункта влезают на 360dp
    { id: 'today', label: 'Сегодня' },
    { id: '7d', label: '7 дней' },
    { id: '30d', label: '30 дней' },
  ];
  // Подпись диапазона — общий rangeLabel (UI-BUG-02): формула была скопирована
  // сюда и в шапку Аналитики.
  const orderCustomLabel = (currentOrderPeriod === 'custom' && currentOrderFrom && currentOrderTo)
    ? rangeLabel(currentOrderFrom, currentOrderTo)
    : '';
  const periodRow = periodSegHtml(
    periods, currentOrderPeriod, 'data-operiod', currentOrderPeriod === 'custom', orderCustomLabel
  );
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
      <div class="order-card" data-status="${o.status}" data-id="${o.id}">
        <div class="order-header">
          <div class="order-head-main">
            <div class="order-title">${icon('building')} ${escapeHtml(o.agent_name || 'Без клиента')}</div>
            <div class="order-sub">Заказ #${o.id}${isBoss ? ` · ${escapeHtml(o.full_name)}` : ''}</div>
          </div>
          <span class="order-status c-badge">${STATUS_NAME[o.status] || o.status}</span>
        </div>
        <div class="order-meta">
          <span>${icon('box')} ${o.items_count} тов.</span>
          <span>${(o.created_at || '').slice(11, 16)}</span>
          ${o.total > 0 ? `<span class="order-total">${icon('cash')} ${formatMoney(o.total, escapeHtml(o.currency || ''))}</span>` : ''}
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
          const cur = o.currency ? ' ' + escapeHtml(o.currency) : '';
          const priceStr = (it.price && it.price > 0)
            ? ` × ${formatMoney(it.price)} = <b>${formatMoney(sub)}${cur}</b>`
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
    ${salesShellHtml()}
    <div class="section-label">Статус</div>
    <div class="seg-row"><div class="seg seg--scroll">${statusSeg}</div></div>
    <div class="section-label">Период</div>
    ${periodRow}
    ${periodPanel}
    ${!isBoss ? `<button class="btn-new-order" id="btn-new-order">${icon('plus')} Новый заказ</button>` : ''}

    ${isBoss ? `<button class="requests-btn" id="show-requests">${icon('clock')} Заявки на рассмотрении</button>` : ''}

    <div class="orders-list">${list}</div>
  `;
  wireSectionNav(content, 'sales', renderSalesScreen);   // UI-BUG-04: шелл — часть шаблона, значит и проводка тоже

  // Фильтры по статусу (сегмент).
  document.querySelectorAll('.seg-item[data-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      currentOrderFilter = btn.dataset.filter;
      renderOrdersMain();
    });
  });

  // Фильтр по периоду (сегмент + «Период…»).
  document.querySelectorAll('[data-operiod]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      currentOrderPeriod = btn.dataset.operiod;
      renderOrdersMain();
    });
  });

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
          ? ` · ${formatMoney(it.price)} = <b>${formatMoney(sub)}</b>`
          : '';
        return `
          <div class="c-row editor-item">
            <div class="editor-item-info">
              <div class="editor-item-name">${escapeHtml(it.name)}</div>
              <div class="editor-item-qty">${it.quantity} ${it.unit || 'шт'}${subStr}</div>
            </div>
            <button class="editor-item-del" data-idx="${i}" aria-label="Удалить позицию">${icon('close')}</button>
          </div>
        `;
      }).join('') + (grandTotal > 0 ? `
        <div class="c-row editor-item editor-item--total">
          <div class="editor-item-info">
            <div class="editor-item-name">${icon('cash')} Итого</div>
          </div>
          <div>${formatMoney(grandTotal)}</div>
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
    <div class="seg" role="radiogroup" aria-label="Тип оплаты">
      ${[
        { v: 'paid', label: 'Оплачено сразу', ic: 'cash' },
        { v: 'credit', label: 'В долг', ic: 'card' },
      ].map(o => {
        const on = (currentDraftOrder.payment_type || 'paid') === o.v;
        return `<button type="button" class="seg-item ${on ? 'active' : ''}" data-pay="${o.v}"
          role="radio" aria-checked="${on}">${icon(o.ic)} ${o.label}</button>`;
      }).join('')}
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
        ${icon('check')} Отправить заявку
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

  // Тип оплаты — сегмент (UI-WP-04) + показ/скрытие выбора даты.
  document.querySelectorAll('[data-pay]').forEach(btn => {
    btn.addEventListener('click', () => {
      const value = btn.dataset.pay;
      currentDraftOrder.payment_type = value;
      document.querySelectorAll('[data-pay]').forEach(b => {
        const on = b === btn;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', String(on));
      });
      document.getElementById('due-date-wrap')
        .classList.toggle('hidden', value !== 'credit');
      haptic();
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
      <input type="text" id="agent-search" class="form-input" placeholder="Поиск по имени…">
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
      <div class="c-row agent-row" data-id="${a.id}" data-name="${escapeHtml(a.name || '')}" role="button" tabindex="0">
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
      <input type="text" id="prod-search" class="form-input" placeholder="Поиск товара…">
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
      ? `<button class="btn-secondary u-mt-2" id="prod-more">Показать ещё (${filtered.length - prodLimit})</button>`
      : '';
    list.innerHTML = filtered.length === 0
      ? '<div class="loader">Товары не найдены</div>'
      : filtered.slice(0, prodLimit).map(p => {
          const ind = p.stock >= 100 ? 'green' : p.stock >= 20 ? 'yellow' : 'red';
          return `
            <div class="c-row prod-row" role="button" tabindex="0"
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
  const currencies = ['USD', 'UZS'];
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

      <div class="form-row u-mt-3">
        <label class="form-label">Количество (${unit})</label>
        <input type="number" id="qty-input" class="form-input"
          placeholder="0" inputmode="decimal" min="0.1" step="0.1">
      </div>

      <div class="form-row">
        <label class="form-label">Валюта заказа</label>
        <div class="cur-row">${curButtons}</div>
        ${lockedCurrency
          ? '<div class="qty-stock u-mt-1 u-fs-11">Валюта фиксируется после первой позиции</div>'
          : ''}
      </div>

      <div class="form-row">
        <label class="form-label">Цена за ${unit}</label>
        <input type="number" id="price-input" class="form-input"
          placeholder="0" inputmode="decimal" min="0" step="0.01">
      </div>

      <div id="line-total" class="qty-stock qty-line-total">
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
    // Осознанное исключение из formatMoney (UI-WP-05): это ЖИВОЙ пересчёт под
    // вводом «количество × цена». Округление до рублей скрыло бы то, что
    // пользователь только что набрал (2,5 × 1,20), — здесь нужны копейки.
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
      btn.innerHTML = `${icon('check')} Добавить в заявку`;
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
    showBack(() => showScreen('today'));
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
  const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
  content.innerHTML = loading('Загружаю заявки…');
  try {
    const data = await api('/api/orders/requests', {});
    showBack(renderOrdersMain);
    if (data.requests.length === 0) {
      content.innerHTML = `
        <div class="editor-header">
          <div class="editor-title">Заявки</div>
        </div>
        ${emptyState({
          icon: 'check',
          title: 'Нет заявок на рассмотрении',
          hint: 'Новые заявки появятся здесь автоматически',
        })}
      `;
      return;
    }
    const items = data.requests.map(r => `
      <div class="order-card" data-status="pending">
        <div class="order-header">
          <div>
            <div class="order-title">${icon('clock')} Заявка #${r.id}</div>
            <div class="order-manager">${icon('user')} ${r.full_name}</div>
          </div>
          <span class="order-status c-badge">Ожидает</span>
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
          if (r.total > 0) bits.push(`<span class="order-pay">${icon('cash')} ${formatMoney(r.total, escapeHtml(r.currency || ''))}</span>`);
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
          <button class="btn-draft"   data-req="${r.id}">${icon('edit')} На доработку</button>
        </div>
        <div class="limit-edit draft-box" data-req="${r.id}" hidden>
          <input type="text" class="form-input draft-comment" placeholder="Что исправить?">
          <button class="btn-reject-pay draft-send" data-req="${r.id}">Вернуть менеджеру</button>
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
    // T3.1: «На доработку» — мягкая альтернатива отклонению: заказ возвращается
    // в черновик, менеджер правит и переотправляет ту же заявку. Эндпоинт был,
    // кнопки не было, поэтому босс мог только «Одобрить» или «Отклонить».
    // Причина обязательна (сервер требует ≥3 символов) — раскрываем поле,
    // как в отмене заказа.
    document.querySelectorAll('.btn-draft').forEach(btn =>
      btn.addEventListener('click', () => {
        const box = document.querySelector(`.draft-box[data-req="${btn.dataset.req}"]`);
        if (box) box.hidden = !box.hidden;
      })
    );
    document.querySelectorAll('.draft-send').forEach(btn =>
      btn.addEventListener('click', () => returnRequestToDraft(btn.dataset.req))
    );
  } catch (e) {
    content.innerHTML = errorBox(e.message);
  }
}

async function returnRequestToDraft(reqId) {
  const box = document.querySelector(`.draft-box[data-req="${reqId}"]`);
  const comment = (box?.querySelector('.draft-comment')?.value || '').trim();
  if (comment.length < 3) {
    tg.showAlert('❌ Опишите, что исправить (минимум 3 символа)');
    return;
  }
  document.querySelectorAll('.btn-approve, .btn-reject, .btn-draft, .draft-send')
    .forEach(b => (b.disabled = true));
  try {
    await api('/api/requests/return_to_draft', {
      req_id: Number(reqId),
      comment,
      idempotency_key: idemKey(),
    });
    tg.showAlert('✏️ Заявка возвращена на доработку');
  } catch (e) {
    tg.showAlert(`❌ ${e.message}`);
  }
  await renderPendingRequests();
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
const ANALYTICS_TTL_MS = 60 * 1000;

// Шапка отчёта — только период. Переключатель «Продажи|Деньги» отсюда убран:
// это были два РАЗНЫХ отчёта в одном экране, и именно он делал «Аналитику»
// местом, где непонятно, что где. Теперь отчёт лежит вкладкой внутри своего
// раздела, а период у них общий — одна переменная на оба.
function reportHeaderHtml() {
  const presets = [
    { id: 'week', label: 'Неделя' }, { id: 'month', label: 'Месяц' },
    { id: '3month', label: 'Квартал' }, { id: 'year', label: 'Год' },
  ];
  const customLabel = (analyticsPeriod === 'custom' && analyticsSince && analyticsUntil)
    ? rangeLabel(analyticsSince, analyticsUntil)
    : '';
  const periodBar = periodSegHtml(
    presets, analyticsPeriod, 'data-period', analyticsPeriod === 'custom', customLabel
  );
  const periodPanel = analyticsPeriod === 'custom' ? dateRangeHost() : '';
  return `<div class="section-label">Период</div>${periodBar}${periodPanel}`;
}

// Обработчики периода/календаря. `rerender` — чей это отчёт: у «Продаж» и
// «Денег» он свой, а период общий.
function wireReportHeader(root, rerender) {
  root.querySelectorAll('[data-period]').forEach(btn => {
    btn.addEventListener('click', () => { haptic('light'); analyticsPeriod = btn.dataset.period; rerender(); });
  });
  if (analyticsPeriod === 'custom') {
    mountCalendar(root.querySelector('.cal-host'), analyticsSince, analyticsUntil,
      (from, to) => { analyticsSince = from; analyticsUntil = to; rerender(); });
  }
}

// Отчёт раздела «Продажи» (бывшая «Аналитика → Продажи»).
async function renderSalesReport() {
  const content = document.getElementById('content');

  // «Период…» выбран, но даты ещё не заданы → показываем шапку с календарём и
  // ждём выбор дат (WP-24).
  if (analyticsPeriod === 'custom' && !(analyticsSince && analyticsUntil)) {
    content.innerHTML = salesShellHtml() + reportHeaderHtml() +
      '<div class="loader">Выберите даты периода на календаре выше.</div>';
    wireSectionNav(content, 'sales', renderSalesScreen);
    wireReportHeader(content, renderSalesReport);
    return;
  }

  const custom = analyticsPeriod === 'custom';

  // Короткий кэш (TTL 60с): пресеты и конкретные диапазоны кэшируются отдельно.
  const cacheKey = custom ? `c:${analyticsSince}:${analyticsUntil}` : analyticsPeriod;
  const cached = analyticsCache[cacheKey];
  if (cached && Date.now() - cached.ts < ANALYTICS_TTL_MS) {
    lastAnalyticsData = cached.data;
    renderAnalyticsContent(cached.data);
    return;
  }

  // Шелл входит и в скелетон: без него загрузка уносит переключатель разделa
  // (UI-BUG-04).
  content.innerHTML = salesShellHtml() + loading('Считаю статистику…');
  wireSectionNav(content, 'sales', renderSalesScreen);
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
    content.innerHTML = salesShellHtml() + errorBox(e.message);
    wireSectionNav(content, 'sales', renderSalesScreen);
  }
}

function renderAnalyticsContent(data) {
  const content = document.getElementById('content');
  const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
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
        const unit = p.currency ? escapeHtml(p.currency) : '$';
        return `
          <div class="top-row">
            <span class="top-medal rank-chip">${i + 1}</span>
            <div class="top-info">
              <div class="top-name">${escapeHtml(p.name)}</div>
              <div class="top-sub">${fmt(p.qty)} шт · ${fmt(p.sum)} ${unit}${profitStr}</div>
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
  // Выручка/долг менеджера — РАЗДЕЛЬНО по валютам (не складываем); fallback на
  // суммарное поле, если разбивка не пришла.
  const curList = (arr, fallback, suffix) => {
    const items = (arr || []).filter(x => x.amount > 0);
    if (items.length) return items.map(x => `${fmt(x.amount)} ${escapeHtml(x.currency)}`).join(' · ');
    return fallback ? `${fmt(fallback)} ${suffix}` : '';
  };
  const managerItems = (data.top_managers || []).map((m, i) => {
    const rev = curList(m.revenue_by_currency, m.revenue, '$');
    const debt = curList(m.debt_by_currency, 0, '');
    return `
    <div class="top-row">
      <span class="top-medal rank-chip">${i + 1}</span>
      <div class="top-info">
        <div class="top-name">${escapeHtml(m.name)}</div>
        <div class="top-sub">${rev} · ${m.count} отгр.${m.orders != null ? ` · ${m.orders} зак.` : ''}${debt ? ` · долг ${debt}` : ''}</div>
      </div>
    </div>`;
  }).join('');
  const clientsBlock = clientItems
    ? `<div class="section-label">Топ клиентов</div><div class="c-surface c-surface--pad">${clientItems}</div>` : '';
  const managersBlock = managerItems
    ? `<div class="section-label">Топ менеджеров</div><div class="c-surface c-surface--pad">${managerItems}</div>` : '';
  // Кнопка Excel — только company-scope (boss/admin).
  const exportBlock = data.scope === 'company'
    ? `<button class="btn-primary u-mt-3" id="analytics-export">${icon('chart')} Выгрузить Excel</button>` : '';

  const msWarn = data.ms_unavailable
    ? `<div class="warn-card">${icon('alert', 'warn-ic')} Продажи из МойСклад временно недоступны — показаны нулевые суммы и локальный топ-менеджеров.</div>`
    : '';

  // ── Две ветки показателей (UI-WP-27) ──────────────────────────────────
  // Менеджеру и руководству приходят РАЗНЫЕ данные: у первого выручка списком
  // по валютам (складывать USD+UZS+EUR нельзя), у второго — агрегаты МС в
  // одной базовой валюте. Раньше обе формы собирались одной переменной с
  // ветвлением посередине разметки, и было не видно, какой экран выйдет.

  // Менеджер: по строке на валюту + два счётчика.
  const personalStatsHtml = () => {
    const revLines = data.revenue.length
      ? data.revenue.map(r => {
          const tI = r.trend > 0 ? icon('trend-up') : r.trend < 0 ? icon('trend-down') : '';
          const tC = r.trend > 0 ? 'trend-up' : r.trend < 0 ? 'trend-dn' : '';
          const tS = r.trend ? `<span class="${tC} u-fs-11">${tI} ${r.trend > 0 ? '+' : ''}${r.trend}%</span>` : '';
          return `<div class="rev-row">
            <span class="rev-amount">${fmt(r.total)} ${escapeHtml(r.currency)}</span>
            <span class="rev-meta">${r.count} отгр. ${tS}</span>
          </div>`;
        }).join('')
      : '';
    return `
      <div class="section-label">Выручка по валютам</div>
      ${revLines
        // Пусто — это строка, а не поверхность: карточка с одной серой фразой
        // внутри давала карточку-в-карточке на стыке со счётчиками.
        ? `<div class="c-surface c-surface--pad">${revLines}</div>`
        : '<div class="loader">Нет продаж за период</div>'}
      <div class="stat-grid">
        <div class="stat"><div class="stat-value">${data.count}</div><div class="stat-label">Отгрузок</div></div>
        <div class="stat"><div class="stat-value">${data.clients}</div><div class="stat-label">Клиентов</div></div>
      </div>`;
  };

  // Руководство: четыре агрегата в базовой валюте.
  const companyStatsHtml = () => `
    <div class="stat-grid">
      <div class="stat">
        <div class="stat-value">${fmt(data.total)} $</div>
        <div class="stat-label">Выручка</div>
        ${trendStr ? `<div class="${trendClass} u-fs-11 u-mt-1">${trendStr}</div>` : ''}
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
    </div>`;

  const isPersonal = data.scope === 'personal' && Array.isArray(data.revenue);
  const statsBlock = isPersonal ? personalStatsHtml() : companyStatsHtml();

  content.innerHTML = `
    ${salesShellHtml()}
    ${reportHeaderHtml()}
    ${msWarn}
    ${statsBlock}

    <div class="section-label">Активность по дням</div>
    <div class="c-surface c-surface--pad">${daysBars}</div>

    <div class="section-label">Топ товаров</div>
    <div class="c-surface c-surface--pad">${topItems}</div>
    ${clientsBlock}
    ${managersBlock}
    ${exportBlock}
  `;

  const exportBtn = document.getElementById('analytics-export');
  if (exportBtn) {
    exportBtn.addEventListener('click', async () => {
      haptic('light');
      exportBtn.disabled = true;
      exportBtn.innerHTML = `${icon('clock')} Готовлю файл…`;
      try {
        const exportBody = (analyticsPeriod === 'custom' && analyticsSince && analyticsUntil)
          ? { since: analyticsSince, until: _nextDay(analyticsUntil) }
          : { period: analyticsPeriod };
        await api('/api/analytics/export', exportBody);
        exportBtn.innerHTML = `${icon('check')} Отправлено в чат`;
        tg.showAlert && tg.showAlert('Excel-файл отправлен в чат с ботом');
      } catch (e) {
        exportBtn.disabled = false;
        exportBtn.innerHTML = `${icon('chart')} Выгрузить Excel`;
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

  wireSectionNav(content, 'sales', renderSalesScreen);
  wireReportHeader(content, renderSalesReport);
}

// «Деньги → Отчёт» (быв. «Аналитика → Деньги»): итоги поступлений за период,
// дебиторка, прогноз, дисциплина и лента движения денег. Период общий с отчётом
// по продажам (analyticsPeriod) — это один и тот же вопрос «за какой срок».
//
// Рисует в переданный контейнер (тело раздела), а не в #content: переключатель
// вкладок «Денег» лежит выше и переживает ре-рендер отчёта.
async function renderMoneyReport(container) {
  const content = container || document.getElementById('content');
  if (analyticsPeriod === 'custom' && !(analyticsSince && analyticsUntil)) {
    content.innerHTML = reportHeaderHtml() +
      '<div class="loader">Выберите даты периода на календаре выше.</div>';
    wireReportHeader(content, () => renderMoneyReport(content));
    return;
  }
  content.innerHTML = loading('Считаю деньги…');
  const periodBody = (analyticsPeriod === 'custom' && analyticsSince && analyticsUntil)
    ? { since: analyticsSince, until: _nextDay(analyticsUntil) }
    : { period: analyticsPeriod };
  let summary = null;
  let history = [];
  try {
    summary = await api('/api/money/summary', periodBody);
    // Лента — за ТОТ ЖЕ период, что и итог (WP-11), иначе под заголовком периода
    // висели движения за всё время.
    history = (await api('/api/cash/history', periodBody).catch(() => ({ history: [] }))).history || [];
  } catch (e) {
    content.innerHTML = reportHeaderHtml() + errorBox(e.message);
    wireReportHeader(content, () => renderMoneyReport(content));
    return;
  }
  const label = (summary && summary.period && summary.period.label) || '';
  // Дебиторка, прогноз и дисциплина — отдельные ручки, поэтому сначала рисуем
  // поступления, а разделы «где деньги» дорисовываем следом. Так экран не ждёт
  // самый медленный запрос, чтобы показать хоть что-то.
  content.innerHTML =
    reportHeaderHtml() +
    `<div class="section-label">Поступления · ${escapeHtml(label)}</div>` +
    renderMoneyTotalsHtml(summary) +
    '<div id="money-insights">' + skeleton('list', 3) + '</div>' +
    '<div class="section-label">Движение денег</div>' +
    cashHistoryHtml(history);
  wireReportHeader(content, () => renderMoneyReport(content));

  const box = content.querySelector('#money-insights');
  if (!box) return;
  try {
    box.innerHTML = await moneyInsightsHtml();
  } catch {
    // Разделы аналитики — надстройка: их сбой не должен уносить поступления,
    // которые уже на экране.
    box.innerHTML = '';
  }
  box.querySelectorAll('[data-buyer]').forEach(row => {
    row.addEventListener('click', () => { haptic('light'); renderBuyerCard(row.dataset.buyer); });
  });
  box.querySelectorAll('[data-lead]').forEach(row => {
    row.addEventListener('click', () => { haptic('light'); renderLeadCard(Number(row.dataset.lead)); });
  });
}

// Итог «нам должны» с разбивкой по источникам. Отдаётся только руководству —
// у менеджера в ответе `totals: null`, и блока просто нет.
function receivableTotalsHtml(totals) {
  if (!totals || !totals.all || !totals.all.count) return '';
  const part = (label, block) => block && block.count
    ? `<div class="c-row"><div class="card-row-info"><div class="card-row-sub">${label}</div></div>
       <div class="card-row-value">${escapeHtml(moneyBlockLabel(block))}</div></div>`
    : '';
  return `
    <div class="section-label">Нам должны</div>
    <div class="c-surface c-surface--list">
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-title">Всего</div></div>
        <div class="card-row-value"><b>${escapeHtml(moneyBlockLabel(totals.all))}</b></div>
      </div>
      ${part('По заказам', totals.orders)}
      ${part('По технике', totals.machines)}
    </div>`;
}

// Разделы аналитики «Деньги», отвечающие на вопрос «где деньги и приходят ли
// платежи». Каждый грузится своей ручкой и деградирует молча: сбой прогноза не
// должен уносить экран поступлений.
async function moneyInsightsHtml() {
  const [rec, fc, disc] = await Promise.all([
    api('/api/money/receivables', {}).catch(() => null),
    api('/api/money/forecast', { months: 6 }).catch(() => null),
    api('/api/money/discipline', {}).catch(() => null),
  ]);
  let html = '';
  if (rec) {
    html += receivableTotalsHtml(rec.totals);
    html += '<div class="section-label">Дебиторка по срокам</div>' + agingBarsHtml(rec.aging);
    const top = (rec.by_counterparty || []).filter(r => r.count);
    if (top.length) {
      html += '<div class="section-label">Кто должен больше всех</div>';
      html += '<div class="c-surface c-surface--list">' + top.slice(0, 5).map(r => `
        <div class="c-row">
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(r.name)}</div>
            <div class="card-row-sub">${r.sources.map(s => s === 'machine' ? 'техника' : 'заказы').join(' · ')}</div>
          </div>
          <div class="card-row-value">${escapeHtml(moneyBlockLabel(r))}</div>
        </div>`).join('') + '</div>';
    }
  }
  if (fc) {
    html += '<div class="section-label">Ожидаемые поступления</div>' + forecastRowsHtml(fc.months);
  }
  if (disc && disc.expected_count) {
    const share = disc.on_time_share == null ? '—' : `${Math.round(disc.on_time_share * 100)}%`;
    html += '<div class="section-label">Платёжная дисциплина</div>';
    html += `<div class="c-surface c-surface--list">
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-sub">Собрано из ожидаемого</div></div>
        <div class="card-row-value">${escapeHtml(moneyBlockLabel(disc.collected))} из ${escapeHtml(moneyBlockLabel(disc.expected))}</div>
      </div>
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-sub">Платежей в срок</div></div>
        <div class="card-row-value">${disc.on_time_count} из ${disc.paid_count} · ${share}</div>
      </div>
    </div>`;
    if ((disc.laggards || []).length) {
      html += '<div class="section-label">Систематически задерживают</div>';
      html += '<div class="c-surface c-surface--list">' + disc.laggards.map(l => `
        <div class="c-row c-row--tap" data-buyer="${escapeHtml(l.name)}" role="button" tabindex="0">
          <div class="card-row-info"><div class="card-row-title">${escapeHtml(l.name)}</div></div>
          <div class="card-row-value">${l.late} из ${l.total}</div>
        </div>`).join('') + '</div>';
    }
  }
  return html;
}

// ─── Раздел «Клиенты» ──────────────────────────────────────────────────────
//
// Воронка обращений жила строчками внизу отчёта о деньгах — там её было не
// найти, не зная заранее. Переписка с клиентом не деньги, и у неё должен быть
// свой раздел: воронка, кто ждёт ответа, кредитные лимиты и посты в канал.

async function renderClientsScreen() {
  const content = document.getElementById('content');
  const shell = sectionShell('clients', clientsTab);
  clientsTab = shell.active;
  content.innerHTML = shell.html + '<div id="clients-body">' + skeleton('list', 3) + '</div>';
  wireSectionNav(content, 'clients', renderClientsScreen);

  const body = document.getElementById('clients-body');
  if (clientsTab === 'limits') await renderCreditLimits(body);
  else if (clientsTab === 'channel') await renderChannelHistory(body);
  else if (clientsTab === 'list') await renderLeadsList(body);
  else await renderLeadsFunnel(body);
}

// Два независимых отбора: исход сделки и состояние разговора. Это разные
// вопросы — «купил ли» и «на ком сейчас ход», — и складывать их в один список
// значит заставлять человека помнить, какой из двух он сейчас выбирает.
let leadsFilter = 'all';   // all | new | won | lost
let leadsState = '';       // '' | awaiting_reply | silent | never_answered

// Список обратившихся. До него исход сделки можно было поставить только тому,
// кто прямо сейчас висит без ответа: клиент, которому ответили и который потом
// замолчал, не находился вовсе.
async function renderLeadsList(container) {
  const box = container || document.getElementById('content');
  box.innerHTML = skeleton('list', 4);
  let data;
  try {
    data = await api('/api/leads/list', {
      status: leadsFilter === 'all' ? '' : leadsFilter,
      state: leadsState,
    });
  } catch (e) {
    box.innerHTML = errorBox(e.message);
    return;
  }
  const labels = data.status_labels || {};
  const filters = [['all', 'Все'], ['new', 'В работе'], ['won', 'Купили'], ['lost', 'Не купили']];
  const chips = '<div class="seg-row"><div class="seg seg--scroll">'
    + filters.map(([k, l]) =>
        `<button class="seg-item ${leadsFilter === k ? 'active' : ''}" data-lfilter="${k}" `
        + `aria-pressed="${leadsFilter === k}">${l}</button>`).join('')
    + '</div></div>';
  // Состояние разговора — три готовых списка дел. Раньше они были перемешаны в
  // общем списке, и до «ему ни разу не ответили» руки не доходили никогда.
  const states = [
    ['', 'Любое'],
    ['awaiting_reply', 'Ждут ответа'],
    ['never_answered', 'Без ответа'],
    ['silent', 'Замолчали'],
  ];
  const stateChips = '<div class="seg-row"><div class="seg seg--scroll">'
    + states.map(([k, l]) =>
        `<button class="seg-item ${leadsState === k ? 'active' : ''}" data-lstate="${k}" `
        + `aria-pressed="${leadsState === k}">${l}</button>`).join('')
    + '</div></div>';

  // Состояние выводится из отметок времени, а не хранится — поэтому подпись
  // строки всегда совпадает с фактами (services/leads.lead_state).
  const stateWord = (l) => {
    const st = l.state || {};
    if (st.awaiting_reply) return 'ждёт ответа';
    if (st.never_answered) return 'ему не ответили';
    if (st.silent) return 'замолчал';
    if (st.replied) return 'в переписке';
    return '';
  };
  const rows = (data.leads || []).map(l => {
    const st = l.state || {};
    const tone = st.awaiting_reply || st.never_answered ? 'overdue'
      : l.status === 'won' ? 'approved'
      : l.status === 'lost' ? 'rejected' : 'pending';
    const when = String(l.last_inbound_at || l.first_seen_at || '').slice(0, 16);
    const sub = [stateWord(l), when].filter(Boolean).join(' · ');
    return `
      <div class="c-row c-row--tap" data-lead="${l.id}" data-status="${tone}" role="button" tabindex="0">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(l.display_name || l.username || '—')}</div>
          <div class="card-row-sub">${escapeHtml(sub)}</div>
        </div>
        <div class="card-row-value">${escapeHtml(labels[l.status] || '')}</div>
      </div>`;
  }).join('');

  // Звонки без переписки — свой блок сверху, а не строки среди лидов: это люди,
  // которых в Telegram ещё нет, и путать их с перепиской нельзя.
  const unlinked = (data.unlinked_calls || []);
  const callsBlock = unlinked.length
    ? `<div class="section-label">Звонили, но не пишут · ${unlinked.length}</div>`
      + '<div class="c-surface c-surface--list">' + unlinked.map(c => `
        <div class="c-row" data-status="pending">
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(c.display_name || c.phone || 'Без имени')}</div>
            <div class="card-row-sub">${escapeHtml(String(c.at || '').slice(0, 16))}${
              c.interest ? ' · ' + escapeHtml(c.interest) : ''}${
              c.phone && c.display_name ? ' · ' + escapeHtml(c.phone) : ''}</div>
          </div>
          <button class="pay-toggle" data-call-del="${c.id}" aria-label="Убрать запись">${icon('trash')}</button>
        </div>`).join('') + '</div>'
    : '';
  const addCall = `<div class="c-actions"><button class="btn-secondary" id="call-new">`
    + `${icon('phone')} Записать звонок</button></div>`;

  box.innerHTML = addCall + callsBlock + chips + stateChips + (rows
    ? `<div class="c-surface c-surface--list">${rows}</div>`
    : emptyState({
        icon: 'user',
        title: (leadsFilter === 'all' && !leadsState)
          ? 'Обращений пока нет' : 'По этому отбору никого',
        hint: (leadsFilter === 'all' && !leadsState)
          ? 'Список наполняется из личных переписок менеджеров — нужен Telegram Premium и подключение бота с правом читать сообщения.'
          : 'Смените фильтр, чтобы увидеть остальных.',
      }));

  box.querySelectorAll('[data-lfilter]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      leadsFilter = btn.dataset.lfilter;
      renderLeadsList(box);
    });
  });
  box.querySelectorAll('[data-lstate]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      leadsState = btn.dataset.lstate;
      renderLeadsList(box);
    });
  });
  box.querySelector('#call-new')?.addEventListener('click', () =>
    openCallForm({ onDone: () => renderLeadsList(box) }));
  box.querySelectorAll('[data-call-del]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const res = await apiResult('/api/leads/call_delete', {
        call_id: Number(btn.dataset.callDel),
      });
      if (!res.ok) { tg.showAlert ? tg.showAlert(res.error) : alert(res.error); return; }
      haptic('light');
      renderLeadsList(box);
    });
  });
  box.querySelectorAll('[data-lead]').forEach(row => {
    row.addEventListener('click', () => { haptic('light'); renderLeadCard(Number(row.dataset.lead)); });
  });
}

async function renderLeadsFunnel(container) {
  const box = container || document.getElementById('content');
  box.innerHTML = skeleton('list', 3);
  let lead;
  try {
    lead = await api('/api/leads/funnel', {});
  } catch (e) {
    box.innerHTML = errorBox(e.message);
    return;
  }
  const f = (lead && lead.funnel) || {};
  if (!f.contacted) {
    // Пустая воронка чаще всего значит не «никто не пишет», а «бот не подключён
    // к аккаунту менеджера или ему не дали право читать». Говорим об этом прямо.
    box.innerHTML = emptyState({
      icon: 'user',
      title: 'Обращений пока нет',
      hint: 'Воронка наполняется из личных переписок менеджеров. Нужен Telegram '
          + 'Premium и подключение бота в настройках Telegram для бизнеса — '
          + 'с правом читать сообщения.',
    });
    return;
  }

  let html = '<div class="section-label">Воронка обращений</div>' + leadFunnelHtml(f);
  // Кто заговорил первым. Лид заводит любое первое сообщение, включая наше
  // собственное после звонка, — без этого разреза «обратились» врёт тем сильнее,
  // чем активнее работают по телефону.
  const touch = firstTouchHtml(f);
  if (touch) html += '<div class="section-label">Кто заговорил первым</div>' + touch;
  const speed = replySpeedHtml(f.speed);
  if (speed) html += '<div class="section-label">Скорость ответа</div>' + speed;
  if ((lead.awaiting || []).length) {
    html += `<div class="section-label">Ждут ответа · ${lead.awaiting.length}</div>`;
    html += '<div class="c-surface c-surface--list">' + lead.awaiting.map(l => `
      <div class="c-row c-row--tap" data-lead="${l.id}" data-status="overdue" role="button" tabindex="0">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(l.display_name || l.username || '—')}</div>
          <div class="card-row-sub">написал ${escapeHtml(String(l.last_inbound_at || '').slice(0, 16))}</div>
        </div>${icon('clock')}
      </div>`).join('') + '</div>';
  }
  const mgrs = (lead.by_manager || []).filter(m => m.contacted);
  if (mgrs.length > 1) {
    html += '<div class="section-label">По менеджерам</div>';
    html += '<div class="c-surface c-surface--list">' + mgrs.map(m => `
      <div class="c-row">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(m.name)}</div>
          <div class="card-row-sub">обратились ${m.contacted}${
            m.inbound != null ? ` (сами ${m.inbound})` : ''} · ответили ${m.replied}${
            m.speed && m.speed.median_minutes != null
              ? ` · обычно за ${durationLabel(m.speed.median_minutes)}` : ''}${
            m.awaiting_reply ? ` · ждут ${m.awaiting_reply}` : ''}</div>
        </div>
        <div class="card-row-value">${m.win_rate == null ? '—' : Math.round(m.win_rate * 100) + '%'}</div>
      </div>`).join('') + '</div>';
  }
  box.innerHTML = html;
  box.querySelectorAll('[data-lead]').forEach(row => {
    row.addEventListener('click', () => { haptic('light'); renderLeadCard(Number(row.dataset.lead)); });
  });
}

// История публикаций в канал. Раньше её было неоткуда открыть: пост собирался
// из карточки товара или контейнера, а посмотреть, что уже ушло, — никак.
async function renderChannelHistory(container) {
  const box = container || document.getElementById('content');
  box.innerHTML = skeleton('list', 3);
  let data;
  try {
    data = await api('/api/channel/history', {});
  } catch (e) {
    box.innerHTML = errorBox(e.message);
    return;
  }
  const posts = data.posts || [];
  const labels = data.kind_labels || {};
  const warn = data.can_publish ? '' :
    '<div class="c-error">Канал не настроен: нет CHANNEL_ID. Черновики собираются, публикация выключена.</div>';
  if (!posts.length) {
    box.innerHTML = warn + emptyState({
      icon: 'cart',
      title: 'В канал ещё ничего не уходило',
      hint: 'Пост собирают из карточки товара («Каталог») или прибывшего контейнера — '
          + 'сервер готовит черновик, а публикуете его вы.',
    });
    return;
  }
  // Отклик подписан «после поста», а не «из поста»: ссылка под публикацией ведёт
  // прямо в личку менеджера и метки не несёт, поэтому кто именно пришёл с поста —
  // неизвестно. Это корреляция, и говорить о ней надо ровно так.
  box.innerHTML = warn + '<div class="c-surface c-surface--list">' + posts.map(p => {
    const effect = postEffectLabel(p.effect);
    return `
    <div class="c-row">
      <div class="card-row-info">
        <div class="card-row-title">${escapeHtml(labels[p.kind] || p.kind || '')}</div>
        <div class="card-row-sub">${escapeHtml(String(p.posted_at || '').slice(0, 16))}${
          p.ref ? ' · ' + escapeHtml(String(p.ref)) : ''}</div>
        ${effect ? `<div class="card-row-sub">${escapeHtml(effect)}</div>` : ''}
      </div>
    </div>`;
  }).join('') + '</div>';
}

// ─── Канал ──────────────────────────────────────────────────────────────────
// Черновик собирает СЕРВЕР — правило «наружу не уходят количества» живёт в
// одном месте и проверяется тестом. Здесь только показать, дать поправить и
// опубликовать по нажатию: автопостинга в канал компании нет.
function openChannelComposer(kind, params) {
  const title = { arrival: 'Пост о поступлении', showcase: 'Карточка товара',
                  stale: 'Пост «есть в наличии»' }[kind] || 'Пост в канал';
  openMachineSheet({
    title,
    hint: 'Черновик соберёт сервер — количества в него не попадают',
    fields: [
      { key: 'manager_username', label: 'Telegram менеджера', placeholder: 'без @',
        hint: 'Кнопка под постом приведёт клиента к нему' },
      { key: 'note', label: 'Что добавить от себя', type: 'textarea' },
    ],
    submitLabel: 'Собрать черновик',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/channel/draft', { kind, ...params, ...data });
      if (!res.ok) { showErr(res.error); return false; }
      showChannelPreview(kind, params, res.body);
      return true;
    },
  });
}

function showChannelPreview(kind, params, draft) {
  const warn = draft.already_posted
    ? `<div class="c-error">Это уже публиковали ${escapeHtml(String(draft.already_posted.posted_at || '').slice(0, 16))}</div>`
    : '';
  const blocked = draft.can_publish ? '' :
    '<div class="c-error">Канал не настроен: нет CHANNEL_ID</div>';
  openMachineSheet({
    title: 'Предпросмотр',
    fields: [{ key: 'text', label: 'Текст поста', type: 'textarea', value: draft.text }],
    submitLabel: draft.can_publish ? 'Опубликовать' : 'Нельзя опубликовать',
    onSubmit: async (data, { showErr }) => {
      if (!draft.can_publish) { showErr('Канал не настроен'); return false; }
      if (!await confirmDialog('Опубликовать в канал?')) return false;
      const res = await apiResult('/api/channel/publish', {
        kind, ref: draft.ref, text: data.text,
        photo_id: draft.photo_id, ms_id: params.ms_id,
      });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Опубликовано');
      return true;
    },
  });
  const ov = document.querySelector('.c-overlay .c-sheet');
  if (ov) ov.insertAdjacentHTML('afterbegin', warn + blocked);
}

// Карточка обращения. Переписки здесь нет и не будет — мы её не храним;
// показываем отметки времени и события, по которым считается воронка.
async function renderLeadCard(leadId) {
  const content = document.getElementById('content');
  content.innerHTML = skeleton('label') + skeleton('list', 4);
  setScreenContext('Обращение клиента');
  showBack(() => { clientsTab = 'funnel'; showScreen('clients'); });

  let card;
  try {
    card = await api('/api/leads/card', { lead_id: leadId });
  } catch (e) {
    content.innerHTML = errorBox(e.message);
    return;
  }
  const l = card.lead || {};
  const st = l.state || {};
  const labels = card.status_labels || {};
  const facts = [
    ['Статус', labels[l.status] || l.status],
    ['Первое обращение', String(l.first_seen_at || '').slice(0, 16) || '—'],
    ['Последнее сообщение', String(l.last_inbound_at || '').slice(0, 16) || '—'],
    ['Первый ответ', String(l.first_reply_at || '').slice(0, 16) || 'не отвечали'],
    ['Контрагент', l.agent_ms_id ? 'привязан' : '— не привязан'],
  ];
  const flags = [
    st.awaiting_reply ? '⏳ ждёт ответа' : '',
    st.never_answered ? '⚠️ ни разу не ответили' : '',
    st.silent ? '🔇 замолчал после ответа' : '',
  ].filter(Boolean).join(' · ');

  const EVENTS = {
    inbound: 'Клиент написал', outbound: 'Менеджер ответил',
    reengaged: 'Вернулся после паузы', won: 'Отмечен как купивший',
    lost: 'Отмечен как не купивший', linked: 'Привязан контрагент',
    call: 'Звонок', call_linked: 'Звонок привязан к переписке',
  };

  // Причина отказа — то, ради чего кнопку «Не купил» и стоит нажимать.
  const lostBlock = l.lost
    ? `<div class="c-error">Не купил: ${escapeHtml(l.lost.label || l.lost.reason)}${
        l.lost.note ? ' — ' + escapeHtml(l.lost.note) : ''}</div>`
    : '';
  const dirLabels = card.direction_labels || {};
  const srcLabels = card.source_labels || {};
  const callsBlock = (l.calls || []).length
    ? '<div class="section-label">Звонки</div><div class="c-surface c-surface--list">'
      + l.calls.map(c => `
        <div class="c-row">
          <div class="card-row-info">
            <div class="card-row-title">${escapeHtml(dirLabels[c.direction] || c.direction || '')}${
              c.phone ? ' · ' + escapeHtml(c.phone) : ''}</div>
            <div class="card-row-sub">${escapeHtml(String(c.at || '').slice(0, 16))}${
              c.source ? ' · ' + escapeHtml(srcLabels[c.source] || c.source) : ''}${
              c.interest ? ' · ' + escapeHtml(c.interest) : ''}${
              c.note ? ' · ' + escapeHtml(c.note) : ''}</div>
          </div>
        </div>`).join('') + '</div>'
    : '';

  content.innerHTML = `
    <div class="editor-header"><div class="editor-title">${icon('user')} ${escapeHtml(l.display_name || l.username || '—')}</div></div>
    ${flags ? `<div class="card-row-sub">${escapeHtml(flags)}</div>` : ''}
    ${lostBlock}
    <div class="c-surface c-surface--list">${facts.map(([k, v]) => `
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-sub">${escapeHtml(k)}</div></div>
        <div class="card-row-value">${escapeHtml(String(v))}</div>
      </div>`).join('')}</div>
    <div class="c-actions c-actions--wrap">
      <button class="btn-secondary" data-lead-status="won">${icon('check')} Купил</button>
      <button class="btn-secondary" data-lead-status="lost">${icon('close')} Не купил</button>
      <button class="btn-secondary" data-lead-status="new">Вернуть в работу</button>
      <button class="btn-secondary" id="lead-call">${icon('phone')} Записать звонок</button>
    </div>
    ${callsBlock}
    <div class="section-label">События</div>
    <div class="c-surface c-surface--list">${(l.events || []).map(e => `
      <div class="c-row">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(EVENTS[e.kind] || e.kind)}</div>
          <div class="card-row-sub">${escapeHtml(String(e.at || '').slice(0, 16))}</div>
        </div>
      </div>`).join('') || '<div class="loader">Событий нет</div>'}</div>
  `;

  content.querySelectorAll('[data-lead-status]').forEach(btn => {
    btn.addEventListener('click', async () => {
      // «Не купил» спрашивает причину: без неё отметка говорит только «потеряли»,
      // а решение следует из «почему», а не из «сколько».
      if (btn.dataset.leadStatus === 'lost') {
        openLostReasonSheet(leadId, card.lost_reasons || []);
        return;
      }
      const res = await apiResult('/api/leads/status', {
        lead_id: leadId, status: btn.dataset.leadStatus,
      });
      if (!res.ok) {
        tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
        return;
      }
      haptic('success');
      toast('Отмечено');
      renderLeadCard(leadId);
    });
  });

  content.querySelector('#lead-call')?.addEventListener('click', () =>
    openCallForm({ leadId, onDone: () => renderLeadCard(leadId) }));
}

// Причина отказа. НЕОБЯЗАТЕЛЬНА: «Без причины» закрывает лид как есть.
// Обязательное поле на редко нажимаемой кнопке приводит к тому, что её
// перестают нажимать вовсе, — а потерять сам факт отказа хуже, чем отказ без
// объяснения.
function openLostReasonSheet(leadId, reasons) {
  let picked = null;
  const send = async (reason, note, showErr) => {
    const res = await apiResult('/api/leads/status', {
      lead_id: leadId, status: 'lost', reason: reason || '', note: note || '',
    });
    if (!res.ok) { if (showErr) showErr(res.error); return false; }
    haptic('success');
    toast('Отмечено');
    renderLeadCard(leadId);
    return true;
  };

  const sheet = openMachineSheet({
    title: 'Почему не купил',
    hint: 'Две причины окупают весь список: «нет в наличии» — это про закупку, «дорого» — про цену',
    fields: [{ key: 'note', label: 'Уточнение', type: 'textarea' }],
    submitLabel: 'Сохранить',
    onSubmit: async (data, { showErr }) => {
      if (!picked) { showErr('Выберите причину или нажмите «Без причины»'); return false; }
      return send(picked, data.note, showErr);
    },
  });

  const ov = document.querySelector('.c-overlay');
  const host = ov?.querySelector('#ms-f-note');
  if (!host) return;
  const box = document.createElement('div');
  box.className = 'seg-row';
  box.innerHTML = '<div class="seg seg--scroll">' + reasons.map(r =>
    `<button class="seg-item" data-reason="${escapeHtml(r.key)}" aria-pressed="false">${escapeHtml(r.label)}</button>`
  ).join('') + '</div>';
  host.parentElement.before(box);
  box.querySelectorAll('[data-reason]').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      picked = btn.dataset.reason;
      box.querySelectorAll('[data-reason]').forEach(b => {
        b.classList.toggle('active', b === btn);
        b.setAttribute('aria-pressed', String(b === btn));
      });
      sheet.showErr('');
    });
  });

  const skip = document.createElement('button');
  skip.className = 'btn-secondary';
  skip.type = 'button';
  skip.textContent = 'Без причины';
  skip.addEventListener('click', async () => {
    if (await send(null, null, sheet.showErr)) sheet.close();
  });
  ov.querySelector('#ms-cancel')?.before(skip);
}

// Запись звонка. Обязательного нет ничего: половину звонков заносят постфактум,
// когда номера уже нет под рукой, а «звонок без номера» — всё ещё обращение.
// Обязательное поле здесь означало бы, что звонки перестанут записывать.
function openCallForm({ leadId = null, onDone } = {}) {
  const SOURCES = [
    ['', 'не указан'], ['channel', 'Наш канал'], ['referral', 'Посоветовали'],
    ['ads', 'Реклама'], ['repeat', 'Уже покупал'], ['other', 'Другое'],
  ];
  let source = '';
  let direction = 'in';

  const sheet = openMachineSheet({
    title: leadId ? 'Звонок этому клиенту' : 'Записать звонок',
    hint: leadId ? '' : 'Клиента, которого нет в Telegram, свяжете позже — когда он напишет',
    fields: [
      ...(leadId ? [] : [{ key: 'display_name', label: 'Кто звонил' }]),
      { key: 'phone', label: 'Телефон', type: 'tel', placeholder: '+998 90 123-45-67' },
      { key: 'interest', label: 'Что спрашивал' },
      { key: 'note', label: 'Заметка', type: 'textarea' },
    ],
    submitLabel: 'Записать',
    onSubmit: async (data, { showErr }) => {
      const res = await apiResult('/api/leads/call_add', {
        ...data, lead_id: leadId || '', direction, source,
      });
      if (!res.ok) { showErr(res.error); return false; }
      haptic('success');
      toast('Звонок записан');
      if (onDone) onDone();
      return true;
    },
  });

  const ov = document.querySelector('.c-overlay');
  const anchor = ov?.querySelector('#ms-f-phone');
  if (!anchor) return;
  const box = document.createElement('div');
  box.innerHTML =
    '<div class="seg-row"><div class="seg">'
    + [['in', 'Звонил клиент'], ['out', 'Звонили мы']].map(([k, label]) =>
        `<button class="seg-item ${k === 'in' ? 'active' : ''}" data-dir="${k}" `
        + `aria-pressed="${k === 'in'}">${label}</button>`).join('')
    + '</div></div>'
    + '<div class="section-label">Откуда узнал</div>'
    + '<div class="seg-row"><div class="seg seg--scroll">'
    + SOURCES.map(([k, label]) =>
        `<button class="seg-item ${k === '' ? 'active' : ''}" data-src="${k}" `
        + `aria-pressed="${k === ''}">${escapeHtml(label)}</button>`).join('')
    + '</div></div>';
  anchor.parentElement.before(box);
  const pick = (attr, set) => box.querySelectorAll('[' + attr + ']').forEach(btn => {
    btn.addEventListener('click', () => {
      haptic('light');
      set(btn.getAttribute(attr));
      box.querySelectorAll('[' + attr + ']').forEach(b => {
        b.classList.toggle('active', b === btn);
        b.setAttribute('aria-pressed', String(b === btn));
      });
      sheet.showErr('');
    });
  });
  pick('data-dir', (v) => { direction = v; });
  pick('data-src', (v) => { source = v; });
}

// Карточка покупателя техники: все его сделки, графики и остаток. Покупатель
// опознаётся по имени — другого идентификатора у него пока нет.
async function renderBuyerCard(buyer) {
  const content = document.getElementById('content');
  const back = () => { moneyTab = 'debts'; showScreen('money'); };
  content.innerHTML = skeleton('label') + skeleton('list', 4);
  setScreenContext('Покупатель техники');
  showBack(back);

  let card;
  try {
    card = await api('/api/machines/buyer', { buyer });
  } catch (e) {
    content.innerHTML = errorBox(e.message);
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  const deals = (card.deals || []).map(d => `
    <div class="section-label">${escapeHtml(d.machine_name || '—')} · ${escapeHtml(String(d.sold_at || '').slice(0, 10))}</div>
    ${machineScheduleHtml(d, today)}
  `).join('');

  content.innerHTML = `
    <div class="editor-header"><div class="editor-title">${icon('user')} ${escapeHtml(card.buyer || buyer)}</div></div>
    <div class="section-label">Остаток к получению</div>
    <div class="c-surface c-surface--list">
      <div class="c-row">
        <div class="card-row-info"><div class="card-row-title">Всего по рассрочкам</div>
          <div class="card-row-sub">${card.outstanding.count} ${card.outstanding.count === 1 ? 'платёж' : 'платежей'}</div></div>
        <div class="card-row-value"><b>${escapeHtml(moneyBlockLabel(card.outstanding))}</b></div>
      </div>
    </div>
    ${card.outstanding.count ? agingBarsHtml(card.aging) : ''}
    ${deals}
  `;

  content.querySelectorAll('[data-payment]').forEach(btn => {
    btn.addEventListener('click', async () => {
      await toggleMachinePaymentFromBuyer(buyer, Number(btn.dataset.payment), btn.dataset.paid === '1');
    });
  });
}

async function toggleMachinePaymentFromBuyer(buyer, paymentId, wasPaid) {
  const res = await apiResult('/api/machines/payment', { payment_id: paymentId, paid: !wasPaid });
  if (!res.ok) {
    tg.showAlert ? tg.showAlert(res.error) : alert(res.error);
    if (res.status === 409) renderBuyerCard(buyer);
    return;
  }
  haptic('success');
  toast(res.body.deal_closed ? 'Рассрочка закрыта — все платежи получены' : 'Отмечено');
  renderBuyerCard(buyer);
}

// Лента движения денег (платежи + сдачи + возвраты) — общий рендер для «Денег».
function cashHistoryHtml(history) {
  if (!history || !history.length) return '<div class="loader">Движений пока нет</div>';
  const KIND_META = {
    payment: { ic: 'cash', label: 'Платёж' },
    deposit: { ic: 'cashbox', label: 'Сдача' },
    return: { ic: 'return', label: 'Возврат' },
  };
  const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
  // Статус движения — общая статус-система (UI-WP-02), а не своя тройка классов.
  const histStatus = s => s === 'confirmed'
    ? '<span class="stock-badge" data-status="in_stock">принят</span>'
    : s === 'rejected' ? '<span class="stock-badge" data-status="out">отклонён</span>'
    : '<span class="stock-badge" data-status="low">ожидает</span>';

  const rowHtml = (h) => {
    const m = KIND_META[h.kind] || { ic: 'cash', label: h.kind };
    const ord = h.order_id ? ` · заказ #${h.order_id}` : '';
    const time = String(h.created_at || '').slice(11, 16);
    return `
      <div class="c-row">
        <div class="card-row-icon">${icon(m.ic)}</div>
        <div class="card-row-info">
          <div class="card-row-title">${m.label} · ${fmt(h.amount)} ${escapeHtml(h.currency || baseCur())}</div>
          <div class="card-row-sub">${escapeHtml(h.who || '')}${time ? ' · ' + escapeHtml(time) : ''}${ord}</div>
        </div>
        ${histStatus(h.status)}
      </div>`;
  };

  // UI-WP-28: лента группируется по дням тем же паттерном, что список заказов
  // («Сегодня»/«Вчера»/дата). Раньше это был сплошной поток строк, где дата
  // пряталась в подписи каждой — за период в месяц читать невозможно.
  const groups = [];
  const index = {};
  for (const h of history) {
    const key = String(h.created_at || '').slice(0, 10) || 'no-date';
    if (!(key in index)) { index[key] = groups.length; groups.push({ key, items: [] }); }
    groups[index[key]].items.push(h);
  }
  return groups.map(g =>
    `<div class="section-label order-date-label">${orderDateLabel(g.key)}</div>` +
    `<div class="c-surface c-surface--list">${g.items.map(rowHtml).join('')}</div>`
  ).join('');
}

// (Экран «Платежи» удалён: подтверждение оплат, история движения денег и
//  ручной платёж объединены во вкладку «Касса» — см. renderCashbox.)

function initNav() {
  // Кнопки строит buildNav из таблицы разделов — их набор зависит от роли,
  // поэтому статикой в разметке они быть не могут.
  buildNav();
  // Поиск в топбаре — доступен с любого экрана.
  const searchBtn = document.getElementById('search-btn');
  if (searchBtn) {
    searchBtn.addEventListener('click', openSearch);
  }
}


// ─── Раздел «Деньги» ───────────────────────────────────────────────────────
//
// Бывшие «Финансы» и бывшая «Аналитика → Деньги» — один раздел. Долги и
// дебиторка это один предмет, и держать их в разных разделах значило требовать
// от человека знать, в каком именно лежит нужная ему цифра.
//
// Состояния долгов (state с бекенда):
//   - overdue                — просрочен, оплат нет
//   - due_today              — сегодня к оплате, оплат нет
//   - upcoming               — будущий срок, оплат нет
//   - partial                — есть подтверждённые платежи, но не всё
//   - awaiting_confirmation  — есть pending платежи (босс решает)

let debtsFilter = 'all';   // 'all' | 'today'
let cashboxSubTab = null;  // последняя секция кассы (confirm|ops) — фолбэк
let financePendCache = 0;  // последний счётчик подтверждений (мгновенный бейдж)

async function renderMoneyScreen() {
  const content = document.getElementById('content');
  const r = role();
  const boss = isBossRole();
  const isConfirmer = ['admin', 'boss', 'bookkeeper', 'warehouse_keeper'].includes(r);

  // Миграция старых/внешних ключей вкладок на текущий набор.
  if (['payments', 'cashbox', 'my'].includes(moneyTab)) moneyTab = isConfirmer ? 'confirm' : 'ops';
  if (moneyTab === 'overview') moneyTab = 'report';
  if (moneyTab === 'limits') { clientsTab = 'limits'; return showScreen('clients'); }

  // ВАЖНО: вкладки рисуем СРАЗУ (синхронно). Раньше рендер ждал сетевой подсчёт
  // бейджа (await) — при зависшем запросе вкладки не появлялись до перезагрузки
  // («иногда пропадали вкладки»). Бейдж берём из кэша и освежаем асинхронно ниже.
  const shell = sectionShell('money', moneyTab);
  moneyTab = shell.active;
  const tabs = shell.tabs.map(t =>
    (t.key === 'confirm' && isConfirmer && financePendCache)
      ? { ...t, badge: financePendCache } : t);
  content.innerHTML = sectionNavHtml(tabs, moneyTab) + '<div id="money-body"></div>';
  wireSectionNav(content, 'money', renderMoneyScreen);

  // Активную вкладку подтягиваем в зону видимости, если ряд скроллится.
  const activeSeg = content.querySelector('.seg-item.active');
  if (activeSeg && activeSeg.scrollIntoView) {
    try { activeSeg.scrollIntoView({ inline: 'center', block: 'nearest' }); } catch (e) { /* старый WebView */ }
  }

  // Бейдж ожидающих подтверждений — освежаем АСИНХРОННО, чтобы зависший запрос
  // не блокировал появление вкладок. Обновляем счётчик на месте.
  if (isConfirmer) {
    const cnt = (p, k) => api(p, {}).then(res => (res[k] || []).length).catch(() => 0);
    const parts = [cnt('/api/deposits/pending', 'deposits'), cnt('/api/returns/pending', 'returns')];
    if (boss) parts.push(cnt('/api/payments/pending', 'pending'));
    Promise.all(parts).then(arr => {
      const n = arr.reduce((a, b) => a + b, 0);
      financePendCache = n;
      if (currentScreen !== 'money') return;
      const pill = content.querySelector('.seg-item[data-sect="confirm"]');
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

  const body = document.getElementById('money-body');
  if (moneyTab === 'debts') await renderDebts(body);
  else if (moneyTab === 'report') await renderMoneyReport(body);
  else await renderCashbox(body, moneyTab);  // confirm | ops
}

async function renderCashbox(container, section) {
  container = container || document.getElementById('content');
  // section — плоская вкладка раздела «Деньги»: confirm | ops. Бейдж и
  // переключение — на уровне renderMoneyScreen, тут только тело секции.
  section = section || cashboxSubTab || 'confirm';
  if (section === 'my') section = 'ops';          // «Мои сдачи» — секция внутри ops
  if (section === 'overview') section = 'confirm'; // «Обзор» переехал в Аналитику
  cashboxSubTab = section;
  container.innerHTML = loading('Загрузка кассы…');
  const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
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
          ${r.goods_received
            ? `<span class="debt-meta">${icon('check')} Товар принят</span>`
            : `<button class="btn-reject-pay ret-goods">${icon('box')} Товар получен</button>`}
          <button class="btn-confirm-pay ret-confirm" ${r.goods_received ? '' : 'disabled'}>
            ${icon('check')} Подтвердить возврат
          </button>
        </div>
        ${r.goods_received ? '' : `
          <div class="debt-card-mid">
            <span class="debt-meta">Сначала отметьте приёмку товара — иначе деньги
            уйдут из кассы за непривезённый товар.</span>
          </div>`}
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
          ${['USD', 'UZS'].map(c =>
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
      <div class="c-surface c-surface--pad">
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
        <button id="ret-load" class="cur-btn">${icon('list')} Выбрать позиции</button>
        <div id="ret-positions" class="stock-list" hidden></div>
        <button id="ret-create" class="btn-primary">${icon('return')} Оформить полный возврат</button>
      </div>
  ` : '';

  // Тело активной секции (вкладки на уровне renderMoneyScreen — без 2-го ряда).
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
  // T3.1: частичный возврат. Раньше кнопка всегда оформляла ПОЛНЫЙ возврат —
  // выбрать «вернуть 2 из 5» можно было только в боте (§5.2.6).
  // Позиции подгружаем отдельно (/api/returns/positions), потому что в списке
  // заказов у позиций нет ни id, ни возвращённого количества.
  const retLoad = container.querySelector('#ret-load');
  const retBox = container.querySelector('#ret-positions');
  if (retLoad && retBox) {
    retLoad.addEventListener('click', async () => {
      const orderId = parseInt(container.querySelector('#ret-order').value, 10);
      if (!orderId) { tg.showAlert('❌ Сначала укажите номер заказа'); return; }
      haptic('light');
      retLoad.disabled = true;
      try {
        const d = await api('/api/returns/positions', { order_id: orderId });
        const pos = d.positions || [];
        if (!pos.length) {
          retBox.hidden = true;
          tg.showAlert('⚠️ Нет позиций, доступных к возврату');
          return;
        }
        retBox.innerHTML = `
          <div class="section-label">Что вернуть (доступно к возврату)</div>
          ${pos.map(p => `
            <div class="form-row ret-pos" data-item="${p.item_id}" data-avail="${p.available}">
              <label class="form-label">${escapeHtml(p.name)} · до ${p.available} ${escapeHtml(p.unit)}</label>
              <input type="number" step="any" min="0" max="${p.available}"
                     class="form-input ret-qty" value="${p.available}" inputmode="decimal">
            </div>`).join('')}
          <div class="debt-meta">Обнулите количество, чтобы не возвращать позицию.</div>`;
        retBox.hidden = false;
      } catch (e) {
        tg.showAlert('❌ ' + e.message);
      } finally {
        retLoad.disabled = false;
      }
    });
  }

  const retBtn = container.querySelector('#ret-create');
  if (retBtn) {
    retBtn.addEventListener('click', () => {
      const orderId = parseInt(container.querySelector('#ret-order').value, 10);
      const reason = container.querySelector('#ret-reason').value.trim();
      if (!orderId) { tg.showAlert('❌ Укажите номер заказа'); return; }
      if (reason.length < 3) { tg.showAlert('❌ Опишите причину'); return; }

      // Позиции не подгружали → полный возврат (прежнее поведение).
      // Подгрузили → шлём выбранные; сервер сам решит full/partial.
      let items = null;
      if (retBox && !retBox.hidden) {
        items = [];
        retBox.querySelectorAll('.ret-pos').forEach(row => {
          const qty = parseFloat(row.querySelector('.ret-qty').value);
          if (isFinite(qty) && qty > 0) {
            items.push({ item_id: Number(row.dataset.item), quantity: qty });
          }
        });
        if (!items.length) { tg.showAlert('❌ Укажите количество хотя бы по одной позиции'); return; }
      }

      haptic('light');
      retBtn.disabled = true;
      const payload = { order_id: orderId, reason, refund_method: selectedRefund, idempotency_key: idemKey() };
      if (items) payload.items = items;
      api('/api/returns/create', payload)
        .then(r => { tg.showAlert(`✅ Возврат #${r.return_id} отправлен на подтверждение`); renderMoneyScreen(); })
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
            ${['USD', 'UZS'].map(c => `<option ${c === cur ? 'selected' : ''}>${c}</option>`).join('')}
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
        renderMoneyScreen();
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
        .then(r => { tg.showAlert(`✅ Сдача #${r.deposit_id} отправлена на подтверждение`); renderMoneyScreen(); })
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
        .then(() => { tg.showAlert('✅ Сдача подтверждена'); renderMoneyScreen(); })
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
        .then(() => { tg.showAlert('❌ Сдача отклонена'); renderMoneyScreen(); })
        .catch(e => { b.disabled = false; tg.showAlert('❌ ' + e.message); });
    });
  });

  // Возвраты: отметить приёмку товара, затем подтвердить.
  container.querySelectorAll('.debt-card[data-ret]').forEach(card => {
    // T3.1: кнопка «Товар получен». Эндпоинт был, кнопки не было — после T2.8
    // (подтверждение требует приёмки) отметить её можно было только из бота.
    card.querySelector('.ret-goods')?.addEventListener('click', (ev) => {
      const b = ev.currentTarget;
      if (b.disabled) return;
      b.disabled = true;
      haptic('light');
      api('/api/returns/goods_received', {
        return_id: Number(card.dataset.ret),
        idempotency_key: idemKey(),
      })
        .then(() => { tg.showAlert('📦 Товар отмечен как принятый'); renderMoneyScreen(); })
        .catch(e => { b.disabled = false; tg.showAlert('❌ ' + e.message); });
    });
    card.querySelector('.ret-confirm').addEventListener('click', (ev) => {
      const b = ev.currentTarget;
      if (b.disabled) return;
      b.disabled = true;  // защита от двойного тапа
      haptic('light');
      api('/api/returns/confirm', { return_id: Number(card.dataset.ret), idempotency_key: idemKey() })
        .then(() => { tg.showAlert('✅ Возврат подтверждён'); renderMoneyScreen(); })
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
          renderMoneyScreen();
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
          renderMoneyScreen();
        } catch (e) { tg.showAlert('❌ ' + e.message); btn.disabled = false; }
      });
    });
  });
}

// Список «Клиенты» (boss/admin): контрагенты с МС-балансом + локальным долгом/
// лимитом. Тап по карточке → детальная карточка контрагента.
async function renderCreditLimits(container) {
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
  const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
  const fmtCents = c => opsAmount((Number(c) || 0) / 100);
  if (!clients.length) {
    container.innerHTML = `<div class="empty-state">
      <div class="empty-state-icon">${icon('user')}</div>
      <div class="empty-state-title">Пока нет клиентов</div>
      <div class="empty-state-hint">Контрагенты появятся после синхронизации с МойСклад или первого заказа.</div>
    </div>`;
    return;
  }
  // Сверху — кто больше должен. В МС-балансе (взаиморасчёты) ДОЛГ клиента —
  // отрицательный → должники = самый отрицательный баланс наверху; баланс-нет в
  // конец. Null-safe компаратор (WP-28): прежний Infinity-сентинел давал
  // Infinity−Infinity=NaN на двух null → неопределённый порядок по спецификации.
  const balOf = c => (c.balance_cents != null ? c.balance_cents : null);
  clients.sort((a, b) => {
    const x = balOf(a), y = balOf(b);
    if (x == null) return y == null ? 0 : 1;
    if (y == null) return -1;
    return x - y;
  });
  // UI-WP-05: подпись строит общий хелпер (знак — по конвенции WP-27), экран
  // отвечает только за класс.
  const balStr = (bal) => {
    const b = msBalanceLabel(bal, baseC);
    if (b.tone === 'none') return '<span class="money-placeholder">баланс —</span>';
    if (b.tone === 'owe') return `<span class="bal-owe">${escapeHtml(b.text)}</span>`;
    if (b.tone === 'advance') return `<span class="bal-adv">${escapeHtml(b.text)}</span>`;
    return escapeHtml(b.text);
  };
  // Долг по заказам — РАЗДЕЛЬНО по валютам (не складываем). Лимит — в базовой.
  const debtStr = (c) => {
    const items = (c.debt_by_currency || []).filter(x => x.amount > 0);
    if (!items.length) return 'долг 0';
    return 'долг ' + items.map(x => `${fmt(x.amount)} ${escapeHtml(x.currency)}`).join(' · ');
  };
  const cards = clients.map(c => `
      <div class="c-row c-row--tap" data-agent="${escapeHtml(c.agent_id)}" role="button" tabindex="0">
        <div class="card-row-icon">${icon('building')}</div>
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(c.agent_name)}</div>
          <div class="card-row-sub">${balStr(c.balance_cents)} · ${debtStr(c)} · лимит ${fmt(c.limit)} ${escapeHtml(baseC)}</div>
        </div>
        ${c.over_limit ? '<span class="stock-badge" data-status="out">лимит превышен</span>' : ''}
      </div>`).join('');
  // T3.1: вход на экран курсов валют. Эндпоинты /api/currency/rates{,/set}
  // существовали, но фронт их не звал — курс можно было задать только через
  // /rates в боте. Отдельным подэкраном, а не пятой вкладкой: вкладок в
  // «Финансах» намеренно ≤4, чтобы ряд не переносился.
  const ratesEntry = `
    <div class="c-surface c-surface--list">
      <div class="c-row c-row--tap" id="open-rates" role="button" tabindex="0">
        <div class="card-row-icon">${icon('card')}</div>
        <div class="card-row-info">
          <div class="card-row-title">Курсы валют</div>
          <div class="card-row-sub">Курс к базовой валюте — для сводок в разных валютах</div>
        </div>
      </div>
    </div>`;
  container.innerHTML = `${ratesEntry}<div class="section-label">Клиенты (${clients.length})</div><div class="c-surface c-surface--list">${cards}</div>`;
  container.querySelector('#open-rates')?.addEventListener('click', () => {
    haptic('light');
    renderCurrencyRates();
  });
  container.querySelectorAll('[data-agent]').forEach(card => {
    card.addEventListener('click', () => { haptic('light'); renderAgentDetail(card.dataset.agent); });
  });
}

// Экран курсов валют (T3.1). Открывается из «Финансы → Клиенты».
//
// Семантика rate_to_base: 1 единица валюты = rate_to_base единиц базовой.
// Например при базовой USD: 1 UZS ≈ 0.0000794 USD. Курс задают вручную —
// автоподтяжки нет, поэтому показываем, когда его обновляли в последний раз:
// протухший курс молча искажает все сводки в базовой валюте.
async function renderCurrencyRates() {
  const content = document.getElementById('content');
  content.innerHTML = loading('Загрузка курсов…');
  setScreenContext('Финансы · Курсы валют');
  showBack(() => { clientsTab = 'limits'; showScreen('clients'); });

  let data;
  try {
    data = await api('/api/currency/rates', {});
  } catch (e) {
    content.innerHTML = errorBox(e.message);
    return;
  }
  const base = (data.base || baseCur()).toUpperCase();
  const rates = data.rates || [];
  const canEdit = !!currentUser && ['admin', 'boss'].includes(currentUser.role);

  const rows = rates.map(r => {
    const code = String(r.currency_code || '').toUpperCase();
    const isBase = code === base;
    const upd = r.updated_at ? String(r.updated_at).slice(0, 16) : '—';
    // Строка курса + (для админа/босса) редактор под ней в той же поверхности.
    return `
      <div class="c-row" data-rate="${escapeHtml(code)}">
        <div class="card-row-info">
          <div class="card-row-title">${escapeHtml(code)}</div>
          <div class="card-row-sub">1 ${escapeHtml(code)} = ${escapeHtml(String(r.rate_to_base))} ${escapeHtml(base)} · обновлён ${escapeHtml(upd)}</div>
        </div>
        <div class="card-row-value">${isBase ? 'базовая' : escapeHtml(String(r.rate_to_base))}</div>
      </div>
      ${canEdit && !isBase ? `
        <div class="c-row">
          <input type="number" step="any" class="form-input rate-input"
                 value="${escapeHtml(String(r.rate_to_base))}" inputmode="decimal"
                 aria-label="Курс ${escapeHtml(code)}">
          <button class="btn-confirm-pay rate-save" data-code="${escapeHtml(code)}">Сохранить</button>
        </div>` : ''}`;
  }).join('');

  content.innerHTML = `
    <div class="section-label">Курсы к ${escapeHtml(base)}</div>
    ${rows
      ? `<div class="c-surface c-surface--list">${rows}</div>`
      : emptyState({ icon: 'card', title: 'Курсы не заданы', hint: 'Добавьте курс — сводки в разных валютах считаются через него.' })}
    ${canEdit ? '' : '<div class="debt-meta">Изменять курсы может админ или руководитель.</div>'}
  `;

  content.querySelectorAll('.rate-save').forEach(btn => {
    btn.addEventListener('click', async (ev) => {
      const b = ev.currentTarget;
      // Поле и кнопка лежат в одной строке-примитиве. Раньше здесь искался
      // `.debt-card`, но после пересборки на дизайн-систему такого класса в
      // разметке курсов нет — closest отдавал null и «Сохранить» падала.
      const row = b.closest('.c-row');
      const raw = row.querySelector('.rate-input').value;
      const rate = parseFloat(raw);
      if (!isFinite(rate) || rate <= 0) {
        tg.showAlert('❌ Курс должен быть положительным числом');
        return;
      }
      if (b.disabled) return;
      b.disabled = true;
      haptic('light');
      try {
        await api('/api/currency/rates/set', {
          currency_code: b.dataset.code,
          rate_to_base: rate,
        });
        tg.showAlert(`✅ Курс ${b.dataset.code} обновлён`);
        await renderCurrencyRates();
      } catch (e) {
        b.disabled = false;
        tg.showAlert('❌ ' + e.message);
      }
    });
  });
}

// Состав документа (заказа или отгрузки) — вложенная поверхность со строками
// «товар · количество × цена» и колонкой сумм справа.
//
// Раньше это был мелкий серый список «• Товар: 16 шт × 360 USD = 5 760 USD»:
// он не отличался от подзаголовка строки, которую раскрыли, а суммы не
// выстраивались в колонку — сравнить их глазами было нельзя. Отсюда общий
// рендер: «что уехало» и «что заказывали» должны читаться одинаково.
function itemsBoxHtml(items, currency, opts) {
  const o = opts || {};
  if (!items || !items.length) {
    return `<div class="items-box"><div class="items-empty">${escapeHtml(o.empty || 'Позиций нет')}</div></div>`;
  }
  const cur = escapeHtml(currency || 'USD');
  const money = c => opsAmount((Number(c) || 0) / 100);
  const rows = items.map(it => {
    const qty = `${formatMoney(it.quantity)} ${escapeHtml(it.unit || 'шт')}`;
    const meta = it.price_cents ? `${qty} × ${money(it.price_cents)} ${cur}` : qty;
    // Сумма позиции: у отгрузки её считает сервер, у заказа — количество × цену.
    const sumCents = it.sum_cents != null
      ? it.sum_cents
      : Math.round((it.price_cents || 0) * (it.quantity || 0));
    return `
      <div class="items-row">
        <div class="items-info">
          <div class="items-name">${escapeHtml(it.name)}</div>
          <div class="items-meta">${meta}</div>
        </div>
        <div class="items-sum">${sumCents ? `${money(sumCents)} ${cur}` : '—'}</div>
      </div>`;
  }).join('');
  // Итог печатаем, только когда позиций больше одной: под единственной строкой
  // он дословно её повторяет.
  const total = items.reduce(
    (s, it) => s + (it.sum_cents != null ? it.sum_cents : Math.round((it.price_cents || 0) * (it.quantity || 0))),
    0,
  );
  const totalRow = items.length > 1 && total
    ? `<div class="items-total"><span>Итого · ${items.length} поз.</span><b>${money(total)} ${cur}</b></div>`
    : '';
  return `<div class="items-box">${rows}${totalRow}</div>`;
}

function shipmentItemsHtml(res) {
  return itemsBoxHtml(res.positions, res.currency, { empty: 'В отгрузке нет позиций' });
}

// Карточка контрагента: МС-баланс + локальный долг/лимит (правится) + покупки из
// МС (топ-товары/последние отгрузки, каждая раскрывается в состав) + заказы в
// боте. Открывается из «Клиентов» и из поиска. boss/admin (detail так гейтит).
async function renderAgentDetail(agentId) {
  const content = document.getElementById('content');
  content.innerHTML = loading('Загрузка клиента…');
  showBack(() => { clientsTab = 'limits'; showScreen('clients'); });
  let d;
  try {
    d = await api('/api/clients/detail', { agent_id: agentId });
  } catch (e) {
    content.innerHTML = errorBox(e.message);
    return;
  }
  const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
  const fmtCents = c => opsAmount((Number(c) || 0) / 100);
  const baseC = d.base_currency || baseCur();
  // МС-баланс: подпись — общая (UI-WP-05), формулировка больше не расходится
  // с экраном клиентов.
  const bal = msBalanceLabel(d.balance_cents, baseC);
  const balLine = bal.tone === 'none'
    ? 'Баланс МойСклад: —'
    : `Баланс МойСклад: <b>${escapeHtml(bal.text)}</b>`;

  // Покупки из МС.
  const pur = d.purchases || {};
  const topRows = (pur.top_products || []).map(p =>
    `<div class="c-row"><div class="card-row-info"><div class="card-row-title">${escapeHtml(p.name)}</div>` +
    `<div class="card-row-sub">${fmt(p.qty)} шт · ${fmtCents(p.sum_cents)} ${escapeHtml(baseC)}</div></div></div>`
  ).join('');
  // Отгрузка раскрывается в состав. Позиции тянем по первому тапу, а не сразу
  // все десять: это десять запросов в МойСклад ради строк, которые чаще всего
  // никто не откроет, а бюджет запросов к МС общий на бота, WebApp и cron'ы.
  const recentRows = (pur.recent || []).map(r =>
    `<div class="c-row${r.id ? ' c-row--tap' : ''}"${r.id ? ` data-shipment="${escapeHtml(r.id)}" role="button" tabindex="0" aria-expanded="false"` : ''}>` +
    `<div class="card-row-info"><div class="card-row-title">${fmtCents(r.sum_cents)} ${escapeHtml(baseC)}</div>` +
    `<div class="card-row-sub">${escapeHtml(r.date || '')}</div></div>${r.id ? icon('list') : ''}</div>` +
    (r.id ? `<div class="order-items" id="shipment-${escapeHtml(r.id)}" hidden></div>` : '')
  ).join('');
  const purBlock = pur.count
    ? `<div class="section-label">Покупки · ${pur.count} отгр. · ${fmtCents(pur.total_cents)} ${escapeHtml(baseC)}</div>`
      + (topRows ? `<div class="c-surface c-surface--list">${topRows}</div>` : '')
      + (recentRows ? `<div class="section-label">Последние отгрузки</div><div class="c-surface c-surface--list">${recentRows}</div>` : '')
    : '<div class="section-label">Покупки</div><div class="loader">Покупок в МойСклад нет</div>';

  // Заказы в боте. Строка раскрывается в состав заказа: позиции приходят в том
  // же ответе (их всё равно грузят ради суммы), поэтому раскрытие ничего не
  // запрашивает и работает мгновенно даже без сети.
  const orders = d.orders || [];
  const orderItemsHtml = (o) =>
    itemsBoxHtml(o.items, o.currency || baseC, { empty: 'Позиции не добавлены' });
  const ordersRows = orders.map(o =>
    `<div class="c-row c-row--tap" data-order-open="${o.id}" data-status="${escapeHtml(o.status || '')}" role="button" tabindex="0" aria-expanded="false">` +
    `<div class="card-row-info">` +
    `<div class="card-row-title">#${o.id} · ${fmtCents(o.total_cents)} ${escapeHtml(o.currency || baseC)}</div>` +
    `<div class="card-row-sub">${escapeHtml(o.status || '')} · ${escapeHtml((o.created_at || '').slice(0, 16))} · ${(o.items || []).length} поз.</div>` +
    `</div>${icon('list')}</div>` +
    `<div class="order-items" id="agent-order-${o.id}" hidden>${orderItemsHtml(o)}</div>`
  ).join('');
  const ordersBlock = orders.length
    ? `<div class="section-label">Заказы в боте · ${orders.length}</div><div class="c-surface c-surface--list">${ordersRows}</div>`
    : '<div class="section-label">Заказы в боте</div><div class="loader">Заказов нет</div>';

  // Платежи клиента: та же лента, что на экране «Деньги» (cashHistoryHtml) —
  // платежи, сдачи в части его заказов и возвраты, сгруппированные по дням.
  const history = d.money_history || [];
  const historyBlock = history.length
    ? `<div class="section-label">Платежи · ${history.length}</div>${cashHistoryHtml(history)}`
    : `<div class="section-label">Платежи</div><div class="loader">Движений денег не было</div>`;

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
    ${d.phone ? `<div class="debt-meta agent-phone">${icon('phone')} ${escapeHtml(d.phone)}</div>` : ''}
    <div class="section-label">Взаиморасчёты</div>
    <div class="c-surface c-surface--pad">
      <div class="agent-bal">${balLine}</div>
      <div class="debt-meta">Долг по заказам бота: <b>${fmt(d.debt)} ${escapeHtml(baseC)}</b> · лимит ${fmt(d.limit)} · свободно ${fmt(d.free)}</div>
      ${limitBlock}
    </div>
    ${purBlock}
    ${ordersBlock}
    ${historyBlock}
  `;

  // Раскрытие состава заказа. Данные уже в DOM — только показываем/прячем.
  content.querySelectorAll('[data-order-open]').forEach(row => {
    row.addEventListener('click', () => {
      haptic('light');
      const box = content.querySelector(`#agent-order-${row.dataset.orderOpen}`);
      if (!box) return;
      box.hidden = !box.hidden;
      row.setAttribute('aria-expanded', String(!box.hidden));
    });
  });

  // Раскрытие состава отгрузки. В отличие от заказа позиции лежат в МойСклад,
  // поэтому подгружаем по первому тапу и оставляем в DOM: повторное сворачивание
  // не должно стоить ещё одного запроса.
  content.querySelectorAll('[data-shipment]').forEach(row => {
    row.addEventListener('click', async () => {
      haptic('light');
      // getElementById, а не селектор: id — идентификатор МойСклад, и
      // экранировать его для CSS-селектора здесь незачем.
      const box = document.getElementById(`shipment-${row.dataset.shipment}`);
      if (!box) return;
      box.hidden = !box.hidden;
      row.setAttribute('aria-expanded', String(!box.hidden));
      if (box.hidden || box.dataset.loaded) return;
      box.innerHTML = '<div class="order-item">Загружаю состав…</div>';
      try {
        const res = await api('/api/clients/shipment', { demand_id: row.dataset.shipment });
        box.innerHTML = shipmentItemsHtml(res);
        box.dataset.loaded = '1';
      } catch (e) {
        // Не помечаем загруженным: следующий тап попробует снова.
        box.innerHTML = `<div class="order-item">${escapeHtml(e.message)}</div>`;
      }
    });
  });

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
    // Суммы НЕ складываем между валютами — копим по валюте в каждой корзине.
    let overdueCount = 0, todayCount = 0, upcomingCount = 0;
    const overdueByCur = {}, todayByCur = {}, upcomingByCur = {};
    for (const d of open) {
      const amt = d.remaining > 0 ? d.remaining : d.total;
      const cur = d.currency || 'USD';
      // Бакетим по СРОКУ (due_date vs серверный today), а НЕ по d.state.
      // У частично оплаченного долга state='partial' (это прогресс оплаты,
      // не срок) — раньше он молча проваливался в else → «Будущие», даже если
      // срок сегодня/просрочен. Сравнение строк YYYY-MM-DD корректно; today
      // приходит с сервера (единый TZ). Та же логика, что в handlers/debts.py.
      const due = d.due_date;
      let bucket;
      if (due && due < today) { overdueCount++; bucket = overdueByCur; }
      else if (due && due === today) { todayCount++; bucket = todayByCur; }
      else { upcomingCount++; bucket = upcomingByCur; }
      bucket[cur] = (bucket[cur] || 0) + amt;
    }
    const fmt = n => formatMoney(n);  // UI-WP-05: один формат на весь фронт
    // Суммы корзины по валютам: «380 USD», на каждой строке своя валюта.
    const sumByCur = byCur => {
      const keys = Object.keys(byCur).sort((a, b) => byCur[b] - byCur[a]);
      return keys.length
        ? keys.map(c => `${fmt(byCur[c])} ${escapeHtml(c)}`).join('<br>')
        : '0';
    };

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
        <div class="seg">
          <button class="seg-item ${debtsFilter === 'all' ? 'active' : ''}" data-f="all" aria-pressed="${debtsFilter === 'all'}">Все</button>
          <button class="seg-item ${debtsFilter === 'today' ? 'active' : ''}" data-f="today" aria-pressed="${debtsFilter === 'today'}">К оплате сейчас</button>
        </div>
      </div>
    `;

    // «Нам должны» с разбивкой по источникам. Деньги лежат в двух учётах —
    // заказы в кредит и рассрочки по технике; вопрос к ним один, поэтому и
    // ответ должен быть один, а не два экрана.
    html += receivableTotalsHtml(data.totals);

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
      // Остаток к получению — РАЗДЕЛЬНО по валютам (не складываем); ниже —
      // вспомогательный конвертированный ≈ итог в базовой валюте.
      const remItems = (data.remaining_by_currency || []).filter(x => x.total > 0);
      if (remItems.length) {
        const remRows = remItems
          .map(x => `<b>${fmt(x.total)} ${escapeHtml(x.currency)}</b>`).join(' · ');
        let approx = '';
        if (data.remaining_base_total != null && remItems.length > 1) {
          const partial = data.remaining_base_partial
            ? ' <span class="money-placeholder">(часть без курса)</span>' : '';
          approx = ` <span class="money-placeholder">≈ ${fmt(data.remaining_base_total)} ${escapeHtml(data.base_currency || 'USD')}${partial}</span>`;
        }
        html += `<div class="money-base-total">${icon('cash')} Осталось получить: ${remRows}${approx}</div>`;
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
              <div class="debt-stat-sum">${sumByCur(overdueByCur)}</div>
            </div>
            <div class="debt-stat debt-stat-today">
              <div class="debt-stat-num">${todayCount}</div>
              <div class="debt-stat-label">Сегодня</div>
              <div class="debt-stat-sum">${sumByCur(todayByCur)}</div>
            </div>
            ${debtsFilter === 'all' ? `
            <div class="debt-stat debt-stat-upcoming">
              <div class="debt-stat-num">${upcomingCount}</div>
              <div class="debt-stat-label">Будущие</div>
              <div class="debt-stat-sum">${sumByCur(upcomingByCur)}</div>
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
          const dcur = escapeHtml(d.currency || '');
          const breakdown = `
            <div class="debt-breakdown">
              ${d.confirmed > 0 ? `<span>${icon('check')} Подтверждено: <b>${fmt(d.confirmed)} ${dcur}</b></span>` : ''}
              <span>${icon('clock')} Ждёт: <b>${fmt(d.pending)} ${dcur}</b></span>
              ${d.remaining > 0 ? `<span>Останется: <b>${fmt(d.remaining - d.pending > 0 ? d.remaining - d.pending : 0)} ${dcur}</b></span>` : ''}
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
      html += `<div class="empty-state">
        <div class="empty-state-icon">${icon('check')}</div>
        <div class="empty-state-title">Открытых долгов нет</div>
        <div class="empty-state-hint">Все деньги собраны.</div>
      </div>`;
    } else if (open.length > 0) {
      html += `<div class="section-label">${icon('card')} Открытые (${open.length})</div>`;
      html += '<div class="debts-list">' + open.map(d => {
        // UI-WP-02: состояние — атрибутом, а не отдельным классом: полоса
        // карточки и бейдж выводятся из одной пары переменных.
        const stateLabel = d.state === 'partial' ? `${icon('info')} Частично оплачен`
          : d.state === 'overdue' ? `${icon('alert')} Просрочен`
          : d.state === 'due_today' ? `${icon('clock')} Сегодня` : `${icon('calendar')} Срок`;
        const dueStr = d.due_date ? formatDateRU(d.due_date) : '—';
        const ownerStr = d.is_mine ? '' : ` <span class="debt-owner">· ${escapeHtml(d.full_name)}</span>`;
        // Для partial показываем сколько уже получено и остаток
        const breakdown = d.confirmed > 0 ? `
          <div class="debt-breakdown">
            <span>${icon('check')} Оплачено: <b>${fmt(d.confirmed)} ${escapeHtml(d.currency)}</b></span>
            <span>Остаток: <b>${fmt(d.remaining)}</b> ${escapeHtml(d.currency)}</span>
          </div>
        ` : '';
        return `
          <div class="debt-card" data-status="${d.state}">
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
                       placeholder="Сумма · ост. ${fmt(d.remaining)} ${escapeHtml(d.currency || '')}"
                       min="0" step="0.01" inputmode="decimal">
                <button class="btn-mark-paid" data-id="${d.id}">${icon('check')} Отметить</button>
              </div>
            ` : ''}
          </div>
        `;
      }).join('') + '</div>';
    }

    // ─── Рассрочки по технике ─────────────────────────────────────
    // Второй поток тех же денег. Строка на сделку: здесь нужен ответ «кто и
    // сколько должен», помесячный график живёт в карточке покупателя.
    const machineDebts = data.machine_debts || [];
    if (machineDebts.length) {
      html += `<div class="section-label">${icon('truck')} Рассрочки по технике (${machineDebts.length})</div>`;
      html += '<div class="c-surface c-surface--list">' + machineDebts.map(m => {
        const due = m.next_due ? formatDateRU(m.next_due) : '—';
        const next = m.next_amount != null
          ? `${fmt(m.next_amount)} ${escapeHtml(m.currency)} до ${due}`
          : 'график исчерпан';
        return `
          <div class="c-row c-row--tap" data-buyer="${escapeHtml(m.buyer_name)}"
               data-status="${escapeHtml(m.state)}" role="button" tabindex="0">
            <div class="card-row-info">
              <div class="card-row-title">${escapeHtml(m.machine_name)} · ${escapeHtml(m.buyer_name)}</div>
              <div class="card-row-sub">Следующий: ${escapeHtml(next)}</div>
            </div>
            <div class="card-row-value">${fmt(m.remaining)} ${escapeHtml(m.currency)}</div>
          </div>`;
      }).join('') + '</div>';
    }

    container.innerHTML = html;

    // Tabs (фильтр all/today)
    container.querySelectorAll('.seg-item[data-f]').forEach(t => {
      t.addEventListener('click', () => {
        debtsFilter = t.dataset.f;
        renderDebts(container);
      });
    });

    container.querySelectorAll('[data-buyer]').forEach(row => {
      row.addEventListener('click', () => {
        haptic('light');
        renderBuyerCard(row.dataset.buyer);
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