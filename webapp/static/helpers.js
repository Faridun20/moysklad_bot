// Чистые утилиты WebApp-фронта, вынесенные из app.js, чтобы их можно было
// юнит-тестировать (Vitest) в Node без браузера. UMD-обёртка:
//   • в браузере — функции становятся глобальными (как были объявлены в app.js
//     classic-script'ом), helpers.js подключается в index.html ПЕРЕД app.js;
//   • в Node/Vitest — экспортируются через module.exports.
// Поведение функций идентично прежним определениям в app.js (байт-в-байт тела).
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api; // Node / Vitest
  } else {
    for (const k in api) root[k] = api[k]; // Браузер: глобалы (как было в app.js)
  }
})(typeof self !== 'undefined' ? self : this, function () {
  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Ключ идемпотентности для денежных/складских действий: защищает от
  // double-submit. Сервер дедуплицирует по нему.
  function idemKey() {
    try {
      if (self.crypto && self.crypto.randomUUID) return self.crypto.randomUUID();
    } catch (e) { /* старый WebView без crypto.randomUUID / нет self в Node */ }
    return Date.now() + '-' + Math.random().toString(16).slice(2);
  }

  // Дата YYYY-MM-DD → ДД.ММ.ГГГГ
  function formatDateRU(iso) {
    if (!iso || iso.length < 10) return iso || '—';
    const [y, m, d] = iso.slice(0, 10).split('-');
    return `${d}.${m}.${y}`;
  }

  // SVG-иконка из спрайта (см. <defs> в index.html). Возвращает <svg><use>,
  // который красится currentColor → тематизируется под тему/активный таб.
  // Имя санитизируется (только [a-z0-9-]), чтобы name не мог сломать разметку.
  function icon(name, cls) {
    const safe = String(name || '').replace(/[^a-z0-9-]/g, '');
    const extra = cls ? ' ' + String(cls).replace(/[^a-z0-9 _-]/g, '') : '';
    return `<svg class="ic${extra}" aria-hidden="true"><use href="#ic-${safe}"/></svg>`;
  }

  return { escapeHtml, idemKey, formatDateRU, icon };
});
