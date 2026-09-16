// Сетевой слой (webapp/static/net.js): таймаут, «нет связи», 401, черновики и
// таблица ролей. Юниты в Node — fetch подменяется функцией, DOM не нужен.
import fs from 'node:fs';
import path from 'node:path';

import { describe, it, expect, vi, afterEach } from 'vitest';

import net from '../net.js';

const {
  createNet, netError, detailText, draftStore, API_ROLES, canCall,
  NET_ERROR_TEXT, SESSION_EXPIRED_TEXT, NET_TIMEOUT_MS,
} = net;

const json = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('createNet().request', () => {
  afterEach(() => { vi.useRealTimers(); });

  it('шлёт POST с initData и телом, отдаёт тело ответа', async () => {
    const calls = [];
    const n = createNet({
      fetch: async (p, init) => { calls.push([p, init]); return json(200, { ok: true, x: 1 }); },
      getInitData: () => 'sig',
    });
    const res = await n.request('/api/x', { a: 2 });
    expect(res).toMatchObject({ ok: true, status: 200, body: { ok: true, x: 1 } });
    expect(calls[0][0]).toBe('/api/x');
    expect(calls[0][1].method).toBe('POST');
    expect(JSON.parse(calls[0][1].body)).toEqual({ initData: 'sig', a: 2 });
    // Сигнал отмены передаётся — иначе таймаут не освободит соединение.
    expect(calls[0][1].signal).toBeDefined();
  });

  it('по умолчанию срок — 20 секунд', () => {
    expect(NET_TIMEOUT_MS).toBe(20000);
  });

  it('висящий запрос через срок превращается в «Нет связи», а не в вечный спиннер', async () => {
    vi.useFakeTimers();
    let aborted = false;
    const n = createNet({
      // fetch, который НЕ слушает сигнал: гонка со сроком всё равно должна
      // завершиться (старый WebView без AbortController ведёт себя так же).
      fetch: (_p, init) => {
        init.signal.addEventListener('abort', () => { aborted = true; });
        return new Promise(() => {});
      },
    });
    const pending = n.request('/api/slow', {});
    await vi.advanceTimersByTimeAsync(NET_TIMEOUT_MS + 1);
    const res = await pending;
    expect(res).toMatchObject({ ok: false, status: 0, network: true, timeout: true, error: NET_ERROR_TEXT });
    expect(aborted).toBe(true);
  });

  it('свой срок для долгих ручек', async () => {
    vi.useFakeTimers();
    const n = createNet({ fetch: () => new Promise(() => {}) });
    let done = false;
    const pending = n.request('/api/export', {}, { timeoutMs: 60000 }).then((r) => { done = true; return r; });
    await vi.advanceTimersByTimeAsync(NET_TIMEOUT_MS + 1);
    expect(done).toBe(false);
    await vi.advanceTimersByTimeAsync(40000);
    expect((await pending).timeout).toBe(true);
  });

  it('тело, застрявшее после заголовков, тоже ограничено сроком', async () => {
    vi.useFakeTimers();
    const n = createNet({
      fetch: async () => ({ ok: true, status: 200, json: () => new Promise(() => {}) }),
    });
    const pending = n.request('/api/x', {});
    await vi.advanceTimersByTimeAsync(NET_TIMEOUT_MS + 1);
    expect(await pending).toMatchObject({ ok: false, network: true, timeout: true });
  });

  it('обрыв сети — «Нет связи — проверьте интернет и повторите»', async () => {
    const n = createNet({ fetch: async () => { throw new TypeError('Failed to fetch'); } });
    const res = await n.request('/api/x', {});
    expect(res).toMatchObject({ ok: false, status: 0, network: true, timeout: false });
    expect(res.error).toBe('Нет связи — проверьте интернет и повторите');
  });

  it('401 зовёт onSessionExpired и отдаёт понятный текст', async () => {
    const expired = vi.fn();
    const n = createNet({
      fetch: async () => json(401, { detail: 'Invalid Telegram data' }),
      onSessionExpired: expired,
    });
    const res = await n.request('/api/x', {});
    expect(expired).toHaveBeenCalledTimes(1);
    expect(res).toMatchObject({ ok: false, status: 401, sessionExpired: true, error: SESSION_EXPIRED_TEXT });
  });

  it('HTTP-ошибка несёт detail сервера и тело (409 needs_force)', async () => {
    const n = createNet({ fetch: async () => json(409, { detail: 'Уже переведена', needs_force: true }) });
    const res = await n.request('/api/x', {});
    expect(res).toMatchObject({ ok: false, status: 409, error: 'Уже переведена', body: { needs_force: true } });
  });

  it('не-JSON от прокси: 502 — «Сервер не ответил (код 502)», 200 без JSON — тоже сбой', async () => {
    const bad = (status) => ({ ok: status < 300, status, json: async () => { throw new SyntaxError('<html>'); } });
    let n = createNet({ fetch: async () => bad(502) });
    expect((await n.request('/api/x', {})).error).toBe('Сервер не ответил (код 502) — повторите через минуту');
    n = createNet({ fetch: async () => bad(200) });
    expect(await n.request('/api/x', {})).toMatchObject({ ok: false, error: 'Сервер прислал непонятный ответ (код 200) — повторите через минуту' });
  });

  it('raw отдаёт сам ответ — для картинок (blob)', async () => {
    const resp = { ok: true, status: 200, blob: async () => 'bytes' };
    const n = createNet({ fetch: async () => resp });
    const res = await n.request('/api/photo', {}, { raw: true });
    expect(res.ok).toBe(true);
    expect(await res.response.blob()).toBe('bytes');
  });
});

describe('detailText / netError', () => {
  it('422 FastAPI — список ошибок, а не «[object Object]»', () => {
    expect(detailText([{ msg: 'field required' }, { msg: 'bad' }])).toBe('field required; bad');
    expect(detailText({ x: 1 })).toBe('');
  });

  it('флаги результата переезжают на исключение', () => {
    const e = netError({ error: NET_ERROR_TEXT, status: 0, network: true, timeout: true });
    expect(e).toBeInstanceOf(Error);
    expect(e.message).toBe(NET_ERROR_TEXT);
    expect(e.network && e.timeout).toBe(true);
    expect(netError({ error: 'x', status: 401, sessionExpired: true }).sessionExpired).toBe(true);
  });
});

describe('draftStore — черновики форм', () => {
  const memory = () => {
    const m = new Map();
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => m.set(k, String(v)),
      removeItem: (k) => m.delete(k),
      m,
    };
  };

  it('сохраняет и возвращает черновик под ключом пользователя', () => {
    const st = memory();
    draftStore(st, '42').save('wh', { items: [1] });
    expect(draftStore(st, '42').load('wh')).toEqual({ items: [1] });
    // Чужой пользователь на том же телефоне черновика не видит.
    expect(draftStore(st, '43').load('wh')).toBeNull();
  });

  it('clear стирает', () => {
    const st = memory();
    const d = draftStore(st, '1');
    d.save('x', { a: 1 });
    d.clear('x');
    expect(d.load('x')).toBeNull();
  });

  it('старый черновик не возвращается и удаляется', () => {
    const st = memory();
    const d = draftStore(st, '1');
    d.save('x', { a: 1 });
    const key = [...st.m.keys()][0];
    const rec = JSON.parse(st.m.get(key));
    rec.ts -= 4 * 24 * 3600 * 1000;
    st.m.set(key, JSON.stringify(rec));
    expect(d.load('x')).toBeNull();
    expect(st.m.size).toBe(0);
  });

  it('недоступное хранилище не роняет форму', () => {
    const boom = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('quota'); }, removeItem() { throw new Error('x'); } };
    const d = draftStore(boom, '1');
    expect(d.save('x', { a: 1 })).toBe(false);
    expect(d.load('x')).toBeNull();
    expect(() => d.clear('x')).not.toThrow();
    expect(draftStore(null, '1').load('x')).toBeNull();
  });

  it('битый JSON — как отсутствие черновика', () => {
    const st = memory();
    st.setItem('draft:1:x', '{oops');
    expect(draftStore(st, '1').load('x')).toBeNull();
  });
});

// Таблица ролей фронта обязана совпадать с allowed_roles ручек сервера: кнопка,
// которая гарантированно ответит 403, хуже отсутствующей, а скрытая кнопка,
// которую сервер разрешает, — потерянная функция. Читаем server.py как текст:
// ручки объявлены декоратором @app.post("..."), права — в _authorize(...).
describe('API_ROLES совпадает с allowed_roles сервера', () => {
  const src = fs.readFileSync(path.resolve(process.cwd(), 'webapp', 'server.py'), 'utf8');
  const consts = {};
  for (const m of src.matchAll(/^(_[A-Z_]+ROLES|_[A-Z_]+BOSS)\s*=\s*\(([^)]*)\)/gm)) {
    consts[m[1]] = [...m[2].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
  }
  const serverRoles = (route) => {
    const start = src.indexOf(`@app.post("${route}")`);
    if (start === -1) return null;
    const rest = src.slice(start + 10);
    const end = rest.search(/@app\.(post|get)\(/);
    const body = end === -1 ? rest : rest.slice(0, end);
    const m = body.match(/allowed_roles\s*=\s*(\([^)]*\)|[A-Z_]+|None)/);
    if (!m) return null;
    if (m[1] === 'None') return 'any';
    if (m[1].startsWith('(')) return [...m[1].matchAll(/"([a-z_]+)"/g)].map((x) => x[1]);
    return consts[m[1]] || null;
  };

  for (const [route, roles] of Object.entries(API_ROLES)) {
    it(route, () => {
      const server = serverRoles(route);
      expect(server, `ручка ${route} не найдена в server.py`).not.toBeNull();
      expect([...roles].sort()).toEqual([...server].sort());
    });
  }

  it('canCall: ручки нет в таблице — решает сервер', () => {
    expect(canCall('/api/unknown', 'guest')).toBe(true);
    expect(canCall('/api/returns/confirm', 'warehouse_keeper')).toBe(false);
    expect(canCall('/api/returns/confirm', 'boss')).toBe(true);
  });
});


describe('canCall учитывает временное совмещение ролей', () => {
  it('менеджер получает списки кладовщика и бухгалтера, гость — нет', () => {
    expect(canCall('/api/deposits/pending', 'manager')).toBe(true);
    expect(canCall('/api/returns/pending', 'manager')).toBe(true);
    expect(canCall('/api/orders/ship', 'manager')).toBe(true);
    expect(canCall('/api/deposits/pending', 'guest')).toBe(false);
  });
});
