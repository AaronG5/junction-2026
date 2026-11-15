// End-to-end for the in-depth analysis (src/deep.js) on the offline demo feed, mock backend:
// the Analyze button, live progress in the popover, the three sections (patterns, fact check,
// where it came from), highlights in the post's own text, and the button's summary.
//   node test/e2e-deep.mjs            (CHROMIUM=/path/to/chrome to override)
import { chromium } from 'playwright-core';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const extDir = path.resolve(here, '..');
const outDir = path.join(here, 'out');
fs.mkdirSync(outDir, { recursive: true });

function findChromium() {
  if (process.env.CHROMIUM) return process.env.CHROMIUM;
  const base = path.join(os.homedir(), '.cache', 'ms-playwright');
  for (const d of fs.existsSync(base) ? fs.readdirSync(base) : []) {
    const p = path.join(base, d, 'chrome-linux', 'chrome');
    if (d.startsWith('chromium-') && fs.existsSync(p)) return p;
  }
  return undefined;
}

const server = http.createServer((req, res) => {
  const p = path.join(extDir, 'demo', decodeURIComponent(new URL(req.url, 'http://x').pathname));
  if (!p.startsWith(path.join(extDir, 'demo')) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  fs.createReadStream(p).pipe(res);
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const feedUrl = `http://localhost:${server.address().port}/feed.html`;

let failures = 0;
function check(cond, msg) { console.log((cond ? 'ok   ' : 'FAIL ') + msg); if (!cond) failures++; }

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ppg-deep-'));
const ctx = await chromium.launchPersistentContext(userDataDir, {
  executablePath: findChromium(), headless: false,
  args: ['--headless=new', `--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`, '--no-sandbox'],
  viewport: { width: 1000, height: 1100 }, deviceScaleFactor: 1,
});

try {
  const page = await ctx.newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  await page.goto(feedUrl);

  const art = (id) => page.locator(`article:has(a[href$="/status/${id}"]):not(:has(div[role=link] a[href$="/status/${id}"]))`).first();
  const nv = art('1890412233445566771');               // the Nvidia "CIA operation" story
  const btn = nv.locator('ppg-deep button');
  await btn.waitFor({ timeout: 5000 });
  check(/Analyze/.test(await btn.textContent()), 'Analyze button next to the badge');
  const hdr = await nv.locator('[data-testid="User-Name"]').evaluate((e) => e.getBoundingClientRect().height);

  await btn.click();
  const pop = page.locator('ppg-popover');
  await page.locator('ppg-popover .pop').waitFor({ timeout: 3000 });
  await pop.locator('.steps').waitFor({ timeout: 3000 });
  check(await pop.locator('.steps li').count() === 3, 'popover shows the three steps while it runs');
  check(/Analyzing/.test(await btn.textContent()), 'button shows progress');
  await page.screenshot({ path: path.join(outDir, '09-deep-running.png') });

  await pop.locator('ol.tl').waitFor({ timeout: 8000 });   // the trace is the last part
  const sections = await pop.locator('.deep section h3').allTextContents();
  check(sections.join('|').includes('Propaganda patterns') && sections.join('|').includes('Fact check') &&
        sections.join('|').includes('Where it came from'), 'all three sections: ' + sections.slice(1).join(', '));
  check(await pop.locator('.steps').count() === 0, 'progress list is gone when done');
  // The existing content is still there, above the new sections.
  check(await pop.locator('blockquote').first().isVisible() && /Telegram source post/.test(await pop.locator('.pop').textContent()), 'existing popover content kept (Telegram source)');

  const tiers = await pop.locator('.pat .tier').allTextContents();
  check(tiers.length >= 2 && tiers[0] === 'Clear', 'patterns listed with tiers, clearest first: ' + tiers.join(', '));
  check(await pop.locator('.vd').count() >= 1 && /No sources were checked/.test(await pop.locator('.basis').textContent()), 'fact check: verdict chip and its basis');
  const tl = await pop.locator('ol.tl li').evaluateAll((ls) => ls.map((l) => l.className));
  check(tl.includes('telegram') && tl[tl.length - 1] === 'this', 'trace: Telegram source before this post: ' + tl.join(' > '));
  check(/earliest trace found is @two_majors/.test(await pop.locator('.deep').textContent()), 'trace summary names the earliest trace');

  const hl = await page.evaluate(() => ['clear', 'likely', 'possible'].map((t) => (CSS.highlights.get('ppg-' + t) || { size: 0 }).size));
  check(hl[0] + hl[1] + hl[2] >= 2, 'phrases highlighted in the post text (clear/likely/possible: ' + hl.join('/') + ')');
  const marked = await page.evaluate(() => {
    const h = CSS.highlights.get('ppg-clear');
    return h ? [...h].map((r) => r.toString()) : [];
  });
  check(marked.some((t) => /CIA operation/i.test(t)), 'the highlighted text is the flagged phrase: ' + JSON.stringify(marked));
  await page.screenshot({ path: path.join(outDir, '10-deep-popover.png') });

  await page.keyboard.press('Escape');
  check((await btn.locator('.dot').count()) >= 1 && /pattern/.test(await btn.getAttribute('aria-label')), 'button summarizes the result: ' + (await btn.getAttribute('aria-label')));
  const over = await nv.evaluate((a) => { const h = a.querySelector('ppg-deep'); return h.getBoundingClientRect().right - a.getBoundingClientRect().right; });
  check(over <= 0, 'Analyze button stays inside the post (' + Math.round(over) + ' px)');
  const hdr2 = await nv.locator('[data-testid="User-Name"]').evaluate((e) => e.getBoundingClientRect().height);
  check(Math.abs(hdr2 - hdr) < 1, `header row height unchanged (${hdr} -> ${hdr2})`);
  await nv.scrollIntoViewIfNeeded();
  await nv.screenshot({ path: path.join(outDir, '11-deep-highlights.png') });

  // A clean post: the analysis runs, finds nothing, and says so plainly.
  const plain = page.locator('article').filter({ hasNotText: /Nvidia|puppet|regime|Patriot/i }).first();
  await plain.locator('ppg-deep button').click();
  await page.locator('ppg-popover ol.tl').waitFor({ timeout: 8000 });
  const t = await page.locator('ppg-popover .deep').textContent();
  check(/No checkable factual claims found|could be checked/.test(t) && /No earlier trace found|earliest trace/.test(t), 'a plain post gets plain answers');
  await page.keyboard.press('Escape');

  check(errors.length === 0, 'no console errors' + (errors.length ? ': ' + errors.join(' | ') : ''));
} catch (e) {
  console.log('FAIL ' + (e && e.message || e));
  failures++;
} finally {
  await ctx.close();
  server.close();
}
console.log(failures ? `\n${failures} check(s) failed` : '\nall in-depth checks passed');
process.exit(failures ? 1 : 0);
