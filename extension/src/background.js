// Service worker: all network calls happen here (no page CORS/CSP issues).
// Batches items (16 or 400 ms), caches results by tweet id (memory +
// chrome.storage.session), polls slow-path results every 2 s for up to 60 s,
// and pushes results to the tabs that asked.
import { MockBackend, MockDeep } from './mock.js';

export const DEFAULTS = Object.freeze({
  enabled: true,
  baseUrl: 'https://augustasrink--ppg-ext-web.us-east.modal.run',
  // In-depth analysis ("Analyze" button: patterns, truth check, trace) needs the local Gemma,
  // so it goes to the Providence backend on this computer.
  deepUrl: 'http://127.0.0.1:8000',
  // Where the in-depth analysis runs (the popup's "runs on" switch): 'local' = deepUrl (Providence
  // with Gemma on this computer), 'cloud' = the same analysis with stock Gemma 26B on Modal (DEEP_CLOUD_URL).
  deepEngine: 'cloud',
  // Also keep every post in Providence on this computer (POST {deepUrl}/api/ext/save), so the
  // local database grows while you browse, even when the badges come from another server.
  savePosts: false,   // needs Providence running on this computer; off so the extension works out of the box
  showAll: false,
  sensitivity: 'normal',
  mock: false,
});
const DEEP_TIMEOUT_MS = 15000;
// The cloud in-depth analysis (propaganda/gemma/modal_deep.py). Its first request may wait for a container.
export const DEEP_CLOUD_URL = 'https://augustasrink--ppg-gemma-deep-web.us-east.modal.run';
const DEEP_CLOUD_TIMEOUT_MS = 90000;
const CLIENT = 'ext/0.1';
const BATCH_MAX = 16;
const BATCH_MS = 400;
const POLL_MS = 2000;
const PENDING_MAX_MS = 60000;
const REQ_TIMEOUT_MS = 10000;
const ERROR_RETRY_MS = 30000;
const POLL_CHUNK = 40;

let settings = { ...DEFAULTS };
const settingsReady = chrome.storage.sync.get(DEFAULTS).then((s) => { settings = { ...DEFAULTS, ...s }; });
chrome.storage.onChanged.addListener((ch, area) => {
  if (area !== 'sync') return;
  for (const k of Object.keys(ch)) if (k in DEFAULTS) settings[k] = ch[k].newValue ?? DEFAULTS[k];
});

// ---------------------------------------------------------------- transports
function trimBase(u) { return String(u || '').trim().replace(/\/+$/, ''); }

async function fetchJson(url, init) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), REQ_TIMEOUT_MS);
  try {
    const res = await fetch(url, { ...init, signal: ctl.signal, credentials: 'omit', cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } finally { clearTimeout(t); }
}

class HttpTransport {
  constructor(base) { this.base = trimBase(base); }
  batch(items) {
    return fetchJson(this.base + '/api/ext/batch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client: CLIENT, items }),
    });
  }
  result(ids) {
    return fetchJson(this.base + '/api/ext/result?ids=' + ids.map(encodeURIComponent).join(','), { method: 'GET' });
  }
  feedback(body) {
    return fetchJson(this.base + '/api/ext/feedback', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
  }
}

// ---------------------------------------------------------------- pipeline
// One pipeline per backend (mock, or a base URL) so results never mix.
const pipelines = new Map();
function pipelineFor(key) {
  let p = pipelines.get(key);
  if (!p) {
    p = new Pipeline(key, key === 'mock' ? new MockBackend() : new HttpTransport(key));
    pipelines.set(key, p);
  }
  return p;
}

function isFlagged(r) {
  return !!r && ((r.source_match && r.source_match.strong) ||
    (r.content && r.content.label === 'potentially_propaganda') ||
    (r.coordination && r.coordination.label === 'coordinated'));
}

class Pipeline {
  constructor(key, transport) {
    this.key = key;
    this.t = transport;
    this.prefix = 'r:' + (key === 'mock' ? 'mock' : hash(key)) + ':';
    this.cache = new Map();    // id -> result
    this.subs = new Map();     // id -> Set<port>
    this.inflight = new Set(); // ids queued, in a batch request, or pending
    this.pending = new Map();  // id -> first pending time
    this.errorAt = new Map();  // id -> time of last error
    this.queue = [];
    this.timer = 0;
    this.pollTimer = 0;
  }

  async request(items, port) {
    const need = [];
    const ready = [];
    // Session cache survives worker restarts; read it for ids not in memory.
    const missing = items.filter((it) => !this.cache.has(it.id));
    if (missing.length) {
      try {
        const got = await chrome.storage.session.get(missing.map((it) => this.prefix + it.id));
        for (const [k, v] of Object.entries(got)) this.cache.set(k.slice(this.prefix.length), v);
      } catch (e) { /* session storage unavailable: memory only */ }
    }
    for (const it of items) {
      if (!it || !it.id) continue;
      this.sub(it.id, port);
      const c = this.cache.get(it.id);
      if (c && c.status !== 'error') { ready.push(c); if (c.status === 'pending' && !this.inflight.has(it.id)) this.trackPending(it.id); continue; }
      if (c && c.status === 'error' && Date.now() - (this.errorAt.get(it.id) || 0) < ERROR_RETRY_MS) { ready.push(c); continue; }
      if (this.inflight.has(it.id)) continue;
      need.push(it);
    }
    if (ready.length) send(port, ready);
    if (need.length) {
      bumpCounter('scanned', need.length);
      for (const it of need) { this.inflight.add(it.id); this.queue.push(it); }
      if (this.queue.length >= BATCH_MAX) this.flush();
      else if (!this.timer) this.timer = setTimeout(() => this.flush(), BATCH_MS);
    }
  }

  sub(id, port) {
    let s = this.subs.get(id);
    if (!s) { s = new Set(); this.subs.set(id, s); }
    s.add(port);
  }

  dropPort(port) {
    for (const [id, s] of this.subs) { s.delete(port); if (!s.size) this.subs.delete(id); }
  }

  flush() {
    clearTimeout(this.timer); this.timer = 0;
    while (this.queue.length) {
      const items = this.queue.splice(0, BATCH_MAX);
      this.sendBatch(items);
    }
  }

  async sendBatch(items) {
    if (this.key !== 'mock') mirrorToLocal(this.key, items);
    let res;
    try {
      res = await this.t.batch(items);
    } catch (e) {
      this.fail(items.map((i) => i.id));
      return;
    }
    const byId = new Map(((res && res.results) || []).filter((r) => r && r.id).map((r) => [String(r.id), r]));
    const missing = [];
    for (const it of items) {
      const r = byId.get(it.id);
      if (!r) { missing.push(it.id); continue; }
      this.store(r);
    }
    if (missing.length) this.fail(missing);
  }

  store(r) {
    const id = String(r.id);
    const prev = this.cache.get(id);
    const out = { ...r, id };
    if (!['done', 'pending', 'error'].includes(out.status)) out.status = 'error';
    this.cache.set(id, out);
    if (isFlagged(out) && !(prev && isFlagged(prev))) bumpCounter('flagged', 1);
    if (out.status === 'pending') this.trackPending(id);
    else {
      this.pending.delete(id);
      this.inflight.delete(id);
      if (out.status === 'error') this.errorAt.set(id, Date.now());
    }
    if (out.status !== 'error') this.persist(id, out);
    this.deliver([out]);
  }

  fail(ids) {
    const now = Date.now();
    const out = [];
    for (const id of ids) {
      this.inflight.delete(id);
      this.pending.delete(id);
      this.errorAt.set(id, now);
      // Keep any fast-path signal we already had; only the missing part is "unavailable".
      const prev = this.cache.get(id);
      const r = prev && prev.status === 'pending' ? { ...prev, status: 'timeout' } : { id, status: 'error' };
      this.cache.set(id, r);
      out.push(r);
    }
    this.deliver(out);
  }

  trackPending(id) {
    this.inflight.add(id);
    if (!this.pending.has(id)) this.pending.set(id, Date.now());
    if (!this.pollTimer) this.pollTimer = setTimeout(() => this.poll(), POLL_MS);
  }

  async poll() {
    this.pollTimer = 0;
    const now = Date.now();
    const expired = [];
    const ids = [];
    for (const [id, t0] of this.pending) {
      if (now - t0 > PENDING_MAX_MS) expired.push(id);
      else if (this.subs.has(id)) ids.push(id);
    }
    if (expired.length) this.fail(expired);
    // Nobody is looking at these any more (tab closed): stop polling for them.
    for (const id of this.pending.keys()) if (!this.subs.has(id)) { this.pending.delete(id); this.inflight.delete(id); }
    for (let i = 0; i < ids.length; i += POLL_CHUNK) {
      const chunk = ids.slice(i, i + POLL_CHUNK);
      try {
        const res = await this.t.result(chunk);
        for (const r of (res && res.results) || []) {
          if (!r || !r.id || !this.pending.has(String(r.id))) continue;
          // A transient error while polling is not final; keep waiting until 60 s.
          if (r.status === 'error') continue;
          this.store(r);
        }
      } catch (e) { /* keep polling until the 60 s deadline */ }
    }
    if (this.pending.size && !this.pollTimer) this.pollTimer = setTimeout(() => this.poll(), POLL_MS);
  }

  persist(id, r) {
    chrome.storage.session.set({ [this.prefix + id]: r }).catch(() => {
      // Quota: drop the session copy entirely, memory cache still works.
      chrome.storage.session.clear().catch(() => {});
    });
  }

  deliver(results) {
    const perPort = new Map();
    for (const r of results) {
      const s = this.subs.get(r.id);
      if (!s) continue;
      for (const p of s) {
        if (!perPort.has(p)) perPort.set(p, []);
        perPort.get(p).push(r);
      }
    }
    for (const [p, rs] of perPort) send(p, rs);
  }
}

// The local copy (savePosts). Skipped when the badges already come from that same server (its
// /batch stores them) and in mock mode. Best effort: never retried, never shown as an error.
function mirrorToLocal(batchBase, items) {
  if (!settings.savePosts) return;
  const local = trimBase(settings.deepUrl || DEFAULTS.deepUrl);
  if (!local || local === trimBase(batchBase)) return;
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), REQ_TIMEOUT_MS);
  fetch(local + '/api/ext/save', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'omit', cache: 'no-store', signal: ctl.signal,
    body: JSON.stringify({ client: CLIENT, items }),
  }).catch(() => {}).finally(() => clearTimeout(t));
}

function hash(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
  return h.toString(36);
}

function send(port, results) {
  try { port.postMessage({ type: 'results', results }); } catch (e) { /* port closed */ }
}

// ---------------------------------------------------------------- counters
let counters = null;
let counterTimer = 0;
async function bumpCounter(name, n) {
  if (!counters) {
    try { counters = (await chrome.storage.session.get({ counters: { scanned: 0, flagged: 0 } })).counters; }
    catch (e) { counters = { scanned: 0, flagged: 0 }; }
  }
  counters[name] = (counters[name] || 0) + n;
  if (!counterTimer) counterTimer = setTimeout(() => { counterTimer = 0; chrome.storage.session.set({ counters }).catch(() => {}); }, 300);
}

// ---------------------------------------------------------------- routing
const LIVE_HOSTS = /^https:\/\/(([a-z]+\.)?x\.com|([a-z]+\.)?twitter\.com|(www|web)\.facebook\.com)\//;
// Pages that are not X or Facebook (the offline demo feed) use the mock backend, unless
// opened with ?live (demo/feed.html?live), which uses the configured backend.
function backendKey(senderUrl) {
  const u = senderUrl || '';
  if (!LIVE_HOSTS.test(u) && !/[?&]live\b/.test(u)) return 'mock';
  return trimBase(settings.baseUrl) || DEFAULTS.baseUrl;
}

const portKeys = new Map(); // port -> Set of pipeline keys used
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'ppg') return;
  portKeys.set(port, new Set());
  port.onMessage.addListener(async (msg) => {
    if (!msg || msg.type !== 'scan' || !Array.isArray(msg.items)) return;
    await settingsReady;
    if (!settings.enabled) return;
    const key = backendKey((port.sender && port.sender.url) || msg.page);
    portKeys.get(port)?.add(key);
    const items = msg.items.filter(validItem).slice(0, 200);
    pipelineFor(key).request(items, port);
  });
  port.onDisconnect.addListener(() => {
    for (const key of portKeys.get(port) || []) pipelines.get(key)?.dropPort(port);
    portKeys.delete(port);
  });
});

// ---------------------------------------------------------------- in-depth analysis
// Stateless pass-through: the content script starts the job and polls it (src/deep.js).
let mockDeep = null;
const DEEP_ITEM_KEYS = ['id', 'url', 'author', 'author_name', 'text', 'lang', 'time', 'kind', 'quoted_id', 'platform', 'group'];
const DEEP_OLD = 'Providence on this computer is running an older version without the in-depth analysis. Restart it (launch.bat --stop, then launch.bat).';
const DEEP_DOWN ='The in-depth analysis runs on your own computer, and Providence isn’t answering there. Start it with launch.bat (or launch.sh).';
const DEEP_CLOUD_DOWN = 'The cloud in-depth analysis isn’t answering. Check your internet connection and try again, or switch to “This computer” in the extension’s settings.';

function deepEngine() { return settings.deepEngine === 'cloud' ? 'cloud' : 'local'; }

async function deepCall(msg, sender) {
  const engine = deepEngine();
  const res = await deepCallOn(engine, msg, sender);
  return { ...res, engine };
}

async function deepCallOn(engine, msg, sender) {
  const useMock = backendKey(sender.url || (sender.tab && sender.tab.url)) === 'mock';
  if (useMock && !mockDeep) mockDeep = new MockDeep();
  try {
    if (msg.type === 'deep-start') {
      const b = msg.body || {};
      if (!b.item || !validItem(b.item)) return { ok: false, error: 'This post can’t be analyzed.' };
      const body = {
        item: Object.fromEntries(DEEP_ITEM_KEYS.filter((k) => k in b.item).map((k) => [k, b.item[k]])),
        shared: b.shared || null,
        context: b.context || {},
      };
      if (useMock) return { ok: true, data: await mockDeep.start(body) };
      const res = await deepFetch('/api/ext/deep', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      // A 404 here means the server is up but has no in-depth analysis: a Providence started
      // before it was added (or another server at that address).
      if (res.status === 404) return { ok: false, status: 404, error: engine === 'cloud' ? DEEP_CLOUD_DOWN : DEEP_OLD };
      return res;
    }
    const id = String(msg.id || '');
    if (!/^[A-Za-z0-9]{1,64}$/.test(id)) return { ok: false, error: 'Unknown analysis.' };
    if (useMock) { const j = await mockDeep.get(id); return j ? { ok: true, data: j } : { ok: false, status: 404, error: 'Unknown analysis.' }; }
    return await deepFetch('/api/ext/deep/' + id, { method: 'GET' });
  } catch (e) {
    return { ok: false, status: 0, error: engine === 'cloud' ? DEEP_CLOUD_DOWN : DEEP_DOWN };
  }
}

async function deepFetch(path, init) {
  const cloud = deepEngine() === 'cloud';
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), cloud ? DEEP_CLOUD_TIMEOUT_MS : DEEP_TIMEOUT_MS);
  try {
    const base = cloud ? DEEP_CLOUD_URL : trimBase(settings.deepUrl || DEFAULTS.deepUrl);
    const res = await fetch(base + path, { ...init, signal: ctl.signal, credentials: 'omit', cache: 'no-store' });
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) return { ok: false, status: res.status, error: (data && data.detail) || ('The analysis server answered with error ' + res.status + '.') };
    return { ok: true, data };
  } finally { clearTimeout(t); }
}

function validItem(it) {
  return it && typeof it.id === 'string' && /^[A-Za-z0-9]{1,40}$/.test(it.id) && typeof it.text === 'string';
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return false;
  if (msg.type === 'feedback') {
    (async () => {
      await settingsReady;
      const key = backendKey(sender.url || (sender.tab && sender.tab.url));
      const t = pipelineFor(key).t;
      const b = msg.body || {};
      const body = { tweet_id: String(b.tweet_id || ''), verdict_shown: String(b.verdict_shown || ''), user_says: b.user_says === 'right' ? 'right' : 'wrong' };
      if (b.note) body.note = String(b.note).slice(0, 500);
      try { reply(await t.feedback(body)); } catch (e) { reply({ ok: false }); }
    })();
    return true;
  }
  if (msg.type === 'deep-start' || msg.type === 'deep-get') {
    (async () => {
      await settingsReady;
      reply(await deepCall(msg, sender));
    })();
    return true;
  }
  if (msg.type === 'counters') {
    chrome.storage.session.get({ counters: { scanned: 0, flagged: 0 } })
      .then((s) => reply(counters || s.counters)).catch(() => reply({ scanned: 0, flagged: 0 }));
    return true;
  }
  if (msg.type === 'ping') {
    (async () => {
      await settingsReady;
      try {
        const base = trimBase(msg.baseUrl || settings.baseUrl);
        await fetchJson(base + '/api/ext/result?ids=', { method: 'GET' });
        reply({ ok: true });
      } catch (e) { reply({ ok: false, error: String(e && e.message || e) }); }
    })();
    return true;
  }
  return false;
});

chrome.runtime.onInstalled.addListener(async () => {
  const s = await chrome.storage.sync.get(null);
  const fill = {};
  for (const [k, v] of Object.entries(DEFAULTS)) if (!(k in s)) fill[k] = v;
  if (Object.keys(fill).length) await chrome.storage.sync.set(fill);
});
