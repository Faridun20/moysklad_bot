// Сетевой слой WebApp: запрос с таймаутом, разбор ошибок, истёкшая сессия,
// черновики форм и таблица «какая роль что может вызвать».
//
// Отдельным файлом, а не внутри app.js, по двум причинам: (1) это код без DOM,
// и его можно гонять юнитами в Node (UMD-обёртка как у helpers.js); (2) app.js
// правят сразу несколько веток, а сетевой слой — общий для всех экранов, и
// держать его в одном месте значит не размазывать одни и те же try/fetch по
// полусотне вызовов (так и появились прямые fetch в обход api() без таймаута).
//
// Подключается в index.html ПОСЛЕ helpers.js и ПЕРЕД app.js.
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api; // Node / Vitest
  } else {
    for (const k in api) root[k] = api[k]; // Браузер: глобалы
  }
})(typeof self !== 'undefined' ? self : this, function () {
  // 20 секунд: на площадке мобильный интернет, и честный ответ ручки аналитики
  // бывает небыстрым, но висящий без конца спиннер хуже любой ошибки — человек
  // не знает, ждать ему или жать ещё раз. Загрузка фото и выгрузка Excel
  // передают свой, более длинный таймаут.
  const NET_TIMEOUT_MS = 20000;
  const NET_ERROR_TEXT = 'Нет связи — проверьте интернет и повторите';
  const SESSION_EXPIRED_TEXT = 'Сессия истекла — закройте и откройте приложение';

  // Результат запроса — объект, а не исключение: `apiResult` отдаёт его как
  // есть (формам нужно тело 409), `api` превращает неуспех в Error.
  //   ok, status, body       — как у ответа сервера;
  //   error                  — текст для человека (detail сервера или наш);
  //   network / timeout      — ответа не было вовсе (status 0);
  //   sessionExpired         — 401: подпись Telegram устарела;
  //   response               — сырой Response (только opts.raw, для blob).
  function createNet(deps) {
    const d = deps || {};
    const doFetch = d.fetch;
    const getInitData = d.getInitData || (() => '');
    const onSessionExpired = d.onSessionExpired || (() => {});
    const defaultTimeout = d.timeoutMs || NET_TIMEOUT_MS;
    const Ctl = d.AbortController || (typeof AbortController !== 'undefined' ? AbortController : null);

    async function request(path, body, opts) {
      const o = opts || {};
      const ctl = Ctl ? new Ctl() : null;
      let timedOut = false;
      let timer;
      // Срок — отдельным промисом в гонке, а не только abort(): старый WebView
      // без AbortController и fetch, который сигнал не слушает, иначе висели
      // бы вечно. abort() всё равно зовём — он освобождает соединение.
      const deadline = new Promise((_resolve, reject) => {
        timer = setTimeout(() => {
          timedOut = true;
          try { if (ctl) ctl.abort(); } catch (_e) { /* уже завершён */ }
          reject(new Error('timeout'));
        }, o.timeoutMs || defaultTimeout);
      });
      // Промис срока может отклониться, когда его уже никто не ждёт (ответ
      // пришёл, тело читается дальше) — глушим, чтобы не было unhandled.
      deadline.catch(() => {});
      const offline = () => ({
        ok: false, status: 0, body: {}, error: NET_ERROR_TEXT, network: true, timeout: timedOut,
      });
      // Таймер держим и на чтении тела: заголовки могут прийти, а тело
      // застрять — тогда await r.json() висел бы так же вечно, как fetch.
      try {
        let r;
        try {
          const init = {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ initData: getInitData(), ...(body || {}) }),
          };
          if (ctl) init.signal = ctl.signal;
          r = await Promise.race([doFetch(path, init), deadline]);
        } catch (_e) {
          // fetch отклоняется только без ответа: нет сети, обрыв, наш срок.
          // HTTP-ошибка сюда не попадает.
          return offline();
        }
        if (r.status === 401) {
          onSessionExpired();
          return { ok: false, status: 401, body: {}, error: SESSION_EXPIRED_TEXT, sessionExpired: true };
        }
        if (o.raw) {
          return { ok: !!r.ok, status: r.status, body: {}, error: r.ok ? '' : `Ошибка сервера (${r.status})`, response: r };
        }
        let data = {};
        try {
          data = (await Promise.race([r.json(), deadline])) || {};
        } catch (_e) {
          if (timedOut) return offline();
          // Не-JSON (502/HTML от прокси) — для успеха это сбой формата.
          if (r.ok) return { ok: false, status: r.status, body: {}, error: `Ошибка сервера (${r.status})` };
        }
        return {
          ok: !!r.ok,
          status: r.status,
          body: data,
          // Текст есть и у ответа 200: ручки печати и отправки отвечают
          // 200 с {ok:false}, и вызывающий показывает `error`, если в теле
          // своего текста нет — пустой тост хуже общего.
          error: detailText(data.detail) || `Ошибка сервера (${r.status})`,
        };
      } finally {
        clearTimeout(timer);
      }
    }

    return { request };
  }

  // detail FastAPI бывает списком ошибок валидации (422) — строкой «[object
  // Object]» его показывать нельзя.
  function detailText(detail) {
    if (detail == null) return '';
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail.map((x) => (x && (x.msg || x.message)) || '').filter(Boolean).join('; ');
    }
    return '';
  }

  // Неуспех как исключение — для `api()`: полсотни вызовов пишут try/catch и
  // показывают e.message. Флаги остаются на объекте ошибки: errorBox по ним
  // отличает «нет связи» от отказа сервера.
  function netError(res) {
    const err = new Error(res.error || 'Ошибка');
    err.status = res.status;
    if (res.network) err.network = true;
    if (res.timeout) err.timeout = true;
    if (res.sessionExpired) err.sessionExpired = true;
    return err;
  }

  // ─── Черновики форм ──────────────────────────────────────────────────────
  // initData живёт час; дальше сервер отвечает 401, и человеку остаётся
  // закрыть и открыть приложение. Всё набранное в форме при этом пропадало:
  // накладная на двадцать позиций, платёж с комментарием. Черновик пишется в
  // localStorage (sessionStorage не переживает закрытие WebView) под ключом
  // пользователя — на общем телефоне чужой черновик не всплывёт. Хранилище
  // бывает недоступно (приватный режим, квота) — тогда форма просто работает
  // без черновиков, а не падает.
  const DRAFT_PREFIX = 'draft:';
  const DRAFT_MAX_AGE_MS = 3 * 24 * 3600 * 1000;

  function draftStore(storage, userKey) {
    const key = (name) => `${DRAFT_PREFIX}${userKey || 'anon'}:${name}`;
    return {
      save(name, data) {
        try {
          if (!storage) return false;
          storage.setItem(key(name), JSON.stringify({ ts: Date.now(), data }));
          return true;
        } catch (_e) { return false; }
      },
      load(name, maxAgeMs) {
        try {
          if (!storage) return null;
          const raw = storage.getItem(key(name));
          if (!raw) return null;
          const rec = JSON.parse(raw);
          const age = Date.now() - Number(rec && rec.ts);
          // Старый черновик опаснее пустой формы: цены и остатки за три дня
          // уехали, а форма выглядит заполненной «как надо».
          if (!rec || !(age >= 0) || age > (maxAgeMs || DRAFT_MAX_AGE_MS)) {
            storage.removeItem(key(name));
            return null;
          }
          return rec.data == null ? null : rec.data;
        } catch (_e) { return null; }
      },
      clear(name) {
        try { if (storage) storage.removeItem(key(name)); } catch (_e) { /* нет хранилища */ }
      },
    };
  }

  // ─── Какая роль что может вызвать ────────────────────────────────────────
  // Зеркало allowed_roles ручек сервера для тех кнопок и запросов, которые
  // фронт рисует или шлёт по роли. Кнопка, которая гарантированно ответит 403,
  // хуже отсутствующей, а запрос, который гарантированно ответит 403, — лишняя
  // ошибка в логах и пустой список, неотличимый от «нечего показывать».
  // Сторож расхождения — net.test.js: он читает webapp/server.py и сверяет
  // каждую строку таблицы. Меняешь права ручки — поменяй и здесь.
  const API_ROLES = {
    '/api/home': ['admin', 'boss', 'manager'],
    '/api/search': ['admin', 'boss', 'manager'],
    '/api/today': ['admin', 'boss', 'manager', 'warehouse_keeper', 'bookkeeper'],
    '/api/payments/pending': ['admin', 'boss', 'bookkeeper'],
    '/api/payments/send': ['admin', 'manager'],
    '/api/deposits/pending': ['admin', 'boss', 'bookkeeper'],
    '/api/deposits/confirm': ['admin', 'boss', 'bookkeeper'],
    '/api/deposits/reject': ['admin', 'boss', 'bookkeeper'],
    '/api/deposits/create': ['admin', 'boss', 'manager'],
    '/api/deposits/my': ['admin', 'boss', 'manager'],
    '/api/deposits/on_hand': ['admin', 'boss', 'manager'],
    '/api/returns/pending': ['admin', 'boss', 'warehouse_keeper'],
    '/api/returns/confirm': ['admin', 'boss'],
    '/api/returns/goods_received': ['admin', 'boss', 'warehouse_keeper'],
    '/api/returns/create': ['admin', 'boss', 'warehouse_keeper', 'manager'],
    '/api/orders/create': ['admin', 'boss', 'manager'],
    '/api/orders/ship': ['admin', 'boss', 'warehouse_keeper'],
    '/api/orders/cancel': ['admin', 'boss'],
    '/api/orders/delete_draft': ['admin', 'boss', 'manager'],
    '/api/orders/requests': ['admin', 'boss'],
    '/api/orders/mark_paid': ['admin', 'boss', 'manager'],
    '/api/orders/payment': ['admin', 'boss', 'manager'],
    '/api/orders/payment_context': ['admin', 'boss', 'manager'],
    '/api/orders/confirm_payment': ['admin', 'boss', 'bookkeeper'],
    '/api/orders/reject_payment': ['admin', 'boss', 'bookkeeper'],
    '/api/debts': ['admin', 'boss', 'manager'],
    '/api/suppliers/debts': ['admin', 'boss'],
    '/api/suppliers/payment': ['admin', 'boss'],
    '/api/suppliers/terms': ['admin', 'boss'],
    '/api/cash/history': ['admin', 'boss'],
    '/api/cash/reconcile': ['admin', 'boss', 'manager'],
    '/api/cash/reconcile/context': ['admin', 'boss', 'manager'],
    '/api/cash/reconcile/history': ['admin', 'boss', 'manager'],
    '/api/money/summary': ['admin', 'boss'],
    '/api/wh/invoices/cancel': ['admin', 'boss', 'manager'],
    // Списание и пересчёт — физическая работа со складом: менеджеру открыта
    // (он же кладовщик), сторно — под тем же `delete_requires_boss`.
    '/api/stock/writeoffs/create': ['admin', 'boss', 'manager'],
    '/api/stock/writeoffs/void': ['admin', 'boss', 'manager'],
    '/api/stock/counts/confirm': ['admin', 'boss', 'manager'],
    '/api/machines/delete': ['admin', 'boss', 'manager'],
    '/api/machines/deal': ['admin', 'boss', 'manager'],
    '/api/machines/deals/pending': ['admin', 'boss', 'manager'],
    '/api/machines/deals/approve': ['admin', 'boss', 'manager'],
    '/api/machines/receipt': ['admin', 'boss', 'manager'],
    '/api/machines/payment': ['admin', 'boss', 'manager'],
    '/api/machines/unreserve': ['admin', 'boss', 'manager'],
    '/api/settings/delete_requires_boss': ['admin', 'boss'],
    '/api/settings/client_debt_reminders': ['admin', 'boss'],
    '/api/currency/rates/set': ['admin', 'boss'],
    '/api/prefs/set': ['admin', 'boss'],
  };

  function canCall(path, role) {
    const roles = API_ROLES[path];
    // Ручки нет в таблице — решение не за фронтом: пусть ответит сервер.
    if (!roles) return true;
    // Таблица — копия allowed_roles сервера как есть, а сервер ещё и совмещает
    // роли (пока менеджер делает работу кладовщика и бухгалтера —
    // ROLE_ALSO_ACTS_AS в helpers.js/services/roles.py). Без этого менеджеру не
    // запрашивались бы списки сдач и возвратов, которые он теперь подтверждает.
    const roleIn = (typeof require === 'function' && typeof module !== 'undefined' && module.exports)
      ? require('./helpers.js').roleIn
      : (typeof self !== 'undefined' ? self.roleIn : undefined);
    if (typeof roleIn === 'function') return roleIn(role, roles);
    return roles.indexOf(role) !== -1;
  }

  return {
    NET_TIMEOUT_MS, NET_ERROR_TEXT, SESSION_EXPIRED_TEXT,
    createNet, netError, detailText,
    draftStore, DRAFT_MAX_AGE_MS,
    API_ROLES, canCall,
  };
});
