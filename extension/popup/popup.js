// Toolbar popup. Settings live in chrome.storage.sync; counters come from the worker.
/* global chrome */
const DEFAULTS = {
  enabled: true,
  baseUrl: 'https://augustasrink--ppg-ext-web.us-east.modal.run',
  deepUrl: 'http://127.0.0.1:8000',
  showAll: false,
  sensitivity: 'normal',
  mock: false,
};
const $ = (id) => document.getElementById(id);
const status = (t, cls) => { const s = $('status'); s.textContent = t; s.className = 'status ' + (cls || ''); };

async function load() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  $('enabled').checked = s.enabled;
  $('showAll').checked = s.showAll;
  $('mock').checked = s.mock;
  $('baseUrl').value = s.baseUrl;
  $('deepUrl').value = s.deepUrl;
  document.querySelector(`input[name=sensitivity][value=${s.sensitivity === 'strict' ? 'strict' : 'normal'}]`).checked = true;
  document.body.classList.toggle('off', !s.enabled);
  if (s.mock) status('Mock mode: no requests leave your browser.');
  refreshCounters();
}

function refreshCounters() {
  chrome.runtime.sendMessage({ type: 'counters' }, (c) => {
    if (chrome.runtime.lastError || !c) return;
    $('scanned').textContent = c.scanned || 0;
    $('flagged').textContent = c.flagged || 0;
  });
}

for (const id of ['enabled', 'showAll', 'mock']) {
  $(id).addEventListener('change', async (e) => {
    await chrome.storage.sync.set({ [id]: e.target.checked });
    if (id === 'enabled') document.body.classList.toggle('off', !e.target.checked);
    if (id === 'mock') status(e.target.checked ? 'Mock mode: no requests leave your browser.' : '');
  });
}
for (const r of document.querySelectorAll('input[name=sensitivity]')) {
  r.addEventListener('change', () => chrome.storage.sync.set({ sensitivity: r.value }));
}

async function save() {
  let url;
  try {
    url = new URL($('baseUrl').value.trim());
    if (!/^https?:$/.test(url.protocol)) throw new Error();
  } catch (e) { status('Enter a full URL, like https://example.com', 'err'); return; }
  const base = url.origin + url.pathname.replace(/\/+$/, '');
  // Origins other than *.modal.run and localhost need a runtime grant.
  const pattern = url.origin + '/*';
  const has = await chrome.permissions.contains({ origins: [pattern] });
  if (!has) {
    const ok = await chrome.permissions.request({ origins: [pattern] }).catch(() => false);
    if (!ok) { status('Permission to reach that server was not granted.', 'err'); return; }
  }
  await chrome.storage.sync.set({ baseUrl: base });
  $('baseUrl').value = base;
  status('Saved. Checking the server…');
  chrome.runtime.sendMessage({ type: 'ping', baseUrl: base }, (r) => {
    if (chrome.runtime.lastError) return;
    status(r && r.ok ? 'Saved. Server answered.' : 'Saved, but the server did not answer (' + ((r && r.error) || 'no reply') + ').', r && r.ok ? 'ok' : 'err');
  });
}
// The in-depth analysis address (Veritas Lens with the local Gemma).
async function saveDeep() {
  const out = (t, cls) => { const s = $('deepStatus'); s.textContent = t; s.className = 'status ' + (cls || ''); };
  let url;
  try {
    url = new URL($('deepUrl').value.trim());
    if (!/^https?:$/.test(url.protocol)) throw new Error();
  } catch (e) { out('Enter a full URL, like http://127.0.0.1:8000', 'err'); return; }
  const base = url.origin + url.pathname.replace(/\/+$/, '');
  const pattern = url.origin + '/*';
  if (!(await chrome.permissions.contains({ origins: [pattern] }))) {
    const ok = await chrome.permissions.request({ origins: [pattern] }).catch(() => false);
    if (!ok) { out('Permission to reach that server was not granted.', 'err'); return; }
  }
  await chrome.storage.sync.set({ deepUrl: base });
  $('deepUrl').value = base;
  out('Saved.', 'ok');
}
$('saveDeep').addEventListener('click', saveDeep);
$('deepUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveDeep(); });
$('save').addEventListener('click', save);
$('baseUrl').addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });

load();
setInterval(refreshCounters, 1500);
