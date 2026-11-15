// End-to-end against a stub HTTP backend that follows API.md, to exercise the
// real network path: batching, fast/slow results, polling, missing results,
// feedback. Opens demo/feed.html?live (which uses the configured backend URL).
//   node test/e2e-live.mjs
import { chromium } from 'playwright-core';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const extDir = path.resolve(here, '..');
let failures = 0;
const check = (c, m) => { console.log((c ? 'ok   ' : 'FAIL ') + m); if (!c) failures++; };

function findChromium() {
  if (process.env.CHROMIUM) return process.env.CHROMIUM;
  const base = path.join(os.homedir(), '.cache', 'ms-playwright');
  for (const d of fs.existsSync(base) ? fs.readdirSync(base) : []) {
    const p = path.join(base, d, 'chrome-linux', 'chrome');
    if (d.startsWith('chromium-') && fs.existsSync(p)) return p;
  }
}

// --- stub backend + demo file server on one port
const log = { batches: [], polls: [], feedback: [], cookies: 0, saves: [] };
const firstSeen = new Map();
function resultFor(id, text) {
  const t0 = firstSeen.get(id) ?? Date.now();
  firstSeen.set(id, t0);
  const judged = Date.now() - t0 > 2500;
  const flagged = /Nvidia/.test(text || '');
  return {
    id, status: flagged && !judged ? 'pending' : 'done',
    content: { label: flagged && judged ? 'potentially_propaganda' : 'none', score: flagged ? 0.9 : 0.1, narrative: null, techniques: [], reason: flagged && judged ? 'stub reason' : '', state_media_echo: [] },
    source_match: flagged ? { strong: true, telegram: [{ channel: 'two_majors', url: 'https://t.me/two_majors/1', date: '2026-11-13', sim: 0.97, text: 'stub' }] } : { strong: false, telegram: [] },
    coordination: null,
  };
}
const texts = new Map();
const server = http.createServer(async (req, res) => {
  if (req.headers.cookie) log.cookies++;
  const u = new URL(req.url, 'http://x');
  const json = (o) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(o)); };
  let body = '';
  for await (const c of req) body += c;
  if (u.pathname === '/api/ext/batch') {
    const b = JSON.parse(body);
    log.batches.push(b);
    // Leave out the "000099" post: the extension must show it as unavailable.
    return json({ results: b.items.filter((i) => !/000099$/.test(i.id)).map((i) => { texts.set(i.id, i.text); return resultFor(i.id, i.text); }) });
  }
  if (u.pathname === '/api/ext/result') {
    const ids = (u.searchParams.get('ids') || '').split(',').filter(Boolean);
    log.polls.push(ids);
    return json({ results: ids.map((id) => resultFor(id, texts.get(id))) });
  }
  if (u.pathname === '/api/ext/feedback') { log.feedback.push(JSON.parse(body)); return json({ ok: true }); }
  if (u.pathname === '/local/api/ext/save') { log.saves.push(JSON.parse(body)); return json({ queued: JSON.parse(body).items.length }); }
  if (u.pathname === '/feed.html') { res.writeHead(200, { 'Content-Type': 'text/html' }); return fs.createReadStream(path.join(extDir, 'demo', 'feed.html')).pipe(res); }
  res.writeHead(404); res.end();
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ppg-live-'));
const ctx = await chromium.launchPersistentContext(userDataDir, {
  executablePath: findChromium(), headless: false,
  args: ['--headless=new', `--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`, '--no-sandbox'],
  viewport: { width: 1000, height: 1400 },
});
try {
  const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent('serviceworker');
  const extId = new URL(sw.url()).host;
  const pop = await ctx.newPage();
  await pop.goto(`chrome-extension://${extId}/popup/popup.html`);
  // Wait for the worker's onInstalled to finish filling in defaults; otherwise it can
  // overwrite the stub URL set below (read-then-write race), and the test talks to the
  // real default backend instead of the stub.
  await pop.waitForFunction(async () => !!(await chrome.storage.sync.get('baseUrl')).baseUrl, null, { timeout: 5000 });
  await pop.evaluate((b) => chrome.storage.sync.set({ baseUrl: b, deepUrl: b + '/local', mock: false }), base);
  // deepUrl = the stub's /local: the posts' local copy (savePosts) lands in the stub, never in a real Veritas Lens.
  await pop.close();

  const page = await ctx.newPage();
  await page.goto(`${base}/feed.html?live`);
  const art = (id) => page.locator(`article:has(a[href$="/status/${id}"]):not(:has(div[role=link] a[href$="/status/${id}"]))`).first();
  await art('1890412233445566771').locator('ppg-badge button.source').waitFor({ timeout: 5000 });
  check(true, 'fast-path source match from backend shown');
  check(log.batches.length >= 1 && log.batches[0].client === 'ext/0.1', `batch POSTed (${log.batches.length} request(s))`);
  check(log.batches.every((b) => b.items.length <= 16), 'batches hold at most 16 items');
  await new Promise((r) => setTimeout(r, 500));
  const savedIds = new Set(log.saves.flatMap((s) => s.items.map((i) => i.id)));
  check(log.batches.flatMap((b) => b.items).every((i) => savedIds.has(i.id)), `every post also copied to the local Veritas Lens (${savedIds.size} posts)`);
  const it = log.batches.flatMap((b) => b.items).find((i) => i.id === '1890412233445566771');
  check(it && it.author === 'eu_insider_news' && it.kind === 'post' && it.lang === 'en' && it.url.startsWith('https://x.com/') && it.time, 'item fields match API.md');
  const q = log.batches.flatMap((b) => b.items).find((i) => i.kind === 'quote');
  check(!q || q.quoted_id === '1890412233445566771', 'quote item carries quoted_id');
  const ids = log.batches.flatMap((b) => b.items.map((i) => i.id));
  check(new Set(ids).size === ids.length, 'no id sent twice');
  await art('1890412233445566771').locator('ppg-badge button', { hasText: 'Potentially propaganda' }).waitFor({ timeout: 8000 });
  check(log.polls.length > 0 && log.polls.every((p) => p.length > 0), `judge verdict arrived via GET /result polling (${log.polls.length} polls)`);
  await art('1890412233445000099').scrollIntoViewIfNeeded();
  await art('1890412233445000099').locator('ppg-badge button.unavailable').waitFor({ timeout: 5000 });
  check(true, 'missing result -> unavailable');
  await art('1890412233445566771').locator('ppg-badge button').first().click();
  await page.locator('ppg-popover button.primary').click();
  await page.waitForTimeout(500);
  check(log.feedback.length === 1 && log.feedback[0].tweet_id === '1890412233445566771' && log.feedback[0].user_says === 'wrong', 'feedback POSTed');
  check(log.cookies === 0, 'no cookies sent to the backend');
  const before = log.polls.length;
  await page.waitForTimeout(4500);
  check(log.polls.length === before, 'polling stops when nothing is pending');

  // Backend down: every badge goes to "unavailable", the page keeps working.
  const pop2 = await ctx.newPage();
  await pop2.goto(`chrome-extension://${extId}/popup/popup.html`);
  await pop2.evaluate(() => chrome.storage.sync.set({ baseUrl: 'http://127.0.0.1:9' }));
  await pop2.close();
  const p2 = await ctx.newPage();
  const errs = [];
  p2.on('pageerror', (e) => errs.push(e.message));
  await p2.goto(`${base}/feed.html?live`);
  await p2.locator('ppg-badge button.unavailable').first().waitFor({ timeout: 8000 });
  const n = await p2.locator('ppg-badge button.unavailable').count();
  check(n >= 3 && errs.length === 0, `backend down -> ${n} "unavailable" badges, no page errors`);
} finally {
  await ctx.close(); server.close(); fs.rmSync(userDataDir, { recursive: true, force: true });
}
console.log(failures ? `\n${failures} check(s) failed` : '\nall live e2e checks passed');
process.exit(failures ? 1 : 0);
