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
  viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1,   // wide enough for the panel beside the post
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
  check(/Gemma, on your computer/.test(await pop.locator('.deep h3').first().textContent()), 'heading says the analysis ran on this computer (default engine)');
  // The existing content is still there, above the new sections.
  check(await pop.locator('blockquote').first().isVisible() && /Telegram source post/.test(await pop.locator('.pop').textContent()), 'existing popover content kept (Telegram source)');

  const legend = await pop.locator('.legend').first().textContent();
  check(/2 clear/.test(legend) && /likely/.test(legend), 'patterns summed up per tier: ' + legend);
  check(await pop.locator('.pat').count() === 0, 'highlighted phrases are not repeated in the popover (hover them instead)');
  check(/Hover the highlighted text/.test(await pop.locator('.deep').textContent()), 'popover points to the hover explanations');
  check(await pop.locator('.vd').count() >= 1 && /never against the post/.test(await pop.locator('.basis').textContent()), 'fact check: verdict chip and its basis');
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

  // The window is a fixed panel beside the post: it doesn't cover the post, and stays put on scroll.
  const rects = async () => page.evaluate(() => {
    const p = document.querySelector('ppg-popover').shadowRoot.querySelector('.pop').getBoundingClientRect();
    const a = [...document.querySelectorAll('article')].find((x) => /Nvidia scandal/.test(x.textContent)).getBoundingClientRect();
    return { pop: { left: p.left, right: p.right, top: p.top }, post: { left: a.left, right: a.right } };
  });
  const r1 = await rects();
  check(r1.pop.left >= r1.post.right || r1.pop.right <= r1.post.left, `the analysis window doesn't cover the post (window ${Math.round(r1.pop.left)}-${Math.round(r1.pop.right)}, post ${Math.round(r1.post.left)}-${Math.round(r1.post.right)})`);
  await page.mouse.move(r1.post.left + 20, 600);
  await page.mouse.wheel(0, 250);
  await page.waitForTimeout(300);
  const r2 = await rects();
  check(r2.pop.top === r1.pop.top && r2.pop.left === r1.pop.left, 'the window stays in place while the page scrolls');
  await page.mouse.wheel(0, -250);
  await page.waitForTimeout(300);
  await page.locator('ppg-popover .pop').waitFor({ timeout: 1000 });

  await page.keyboard.press('Escape');

  // Hovering a highlighted phrase explains it, right there in the post.
  const box = await page.evaluate(() => {
    const r = [...CSS.highlights.get('ppg-clear')].find((x) => /CIA operation/i.test(x.toString()));
    const b = r.getClientRects()[0];
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 };
  });
  await page.mouse.move(box.x, box.y);
  const tip = page.locator('ppg-tip .tip');
  await tip.waitFor({ timeout: 2000 });
  const tipText = await tip.textContent();
  check(/^Clear/.test(tipText) && /loaded language|Conspiracy/i.test(tipText), 'hover shows the tier, technique and why: ' + tipText);
  await page.screenshot({ path: path.join(outDir, '12-deep-hover.png') });
  await page.mouse.move(5, 5);
  await page.waitForTimeout(150);
  check(await page.locator('ppg-tip').count() === 0, 'the explanation goes away when the pointer leaves');

  // Something lying over the highlighted text (the site's menus, our window): no explanation through it.
  await page.evaluate((b) => {
    const d = document.createElement('div');
    d.id = 'cover';
    d.style.cssText = `position:fixed;left:${b.x - 40}px;top:${b.y - 20}px;width:80px;height:40px;background:#fff;z-index:10`;
    document.body.appendChild(d);
  }, box);
  await page.mouse.move(box.x, box.y);
  await page.waitForTimeout(200);
  check(await page.locator('ppg-tip').count() === 0, 'no explanation through something covering the text');
  await page.evaluate(() => document.getElementById('cover').remove());
  await page.mouse.move(5, 5);

  check((await btn.locator('.dot').count()) >= 1 && /pattern/.test(await btn.getAttribute('aria-label')), 'button summarizes the result: ' + (await btn.getAttribute('aria-label')));
  const over = await nv.evaluate((a) => { const h = a.querySelector('ppg-deep'); return h.getBoundingClientRect().right - a.getBoundingClientRect().right; });
  check(over <= 0, 'Analyze button stays inside the post (' + Math.round(over) + ' px)');
  const hdr2 = await nv.locator('[data-testid="User-Name"]').evaluate((e) => e.getBoundingClientRect().height);
  check(Math.abs(hdr2 - hdr) < 1, `header row height unchanged (${hdr} -> ${hdr2})`);
  await nv.scrollIntoViewIfNeeded();
  await nv.screenshot({ path: path.join(outDir, '11-deep-highlights.png') });

  // A clean post: the analysis runs, finds nothing, and says so plainly.
  // ...analyzed with the "Cloud (Modal)" engine chosen in the settings popup: the heading says so.
  const [sw] = ctx.serviceWorkers().length ? ctx.serviceWorkers() : [await ctx.waitForEvent('serviceworker')];
  await sw.evaluate(() => chrome.storage.sync.set({ deepEngine: 'cloud' }));
  await page.waitForTimeout(200);
  const plain = page.locator('article').filter({ hasNotText: /Nvidia|puppet|regime|Patriot/i }).first();
  await plain.locator('ppg-deep button').click();
  await page.locator('ppg-popover .deep h3', { hasText: /^Where it came from$/ }).waitFor({ timeout: 8000 });
  const t = await page.locator('ppg-popover .deep').innerText();   // innerText: not the <style> text
  check(await page.locator('ppg-popover ol.tl').count() === 0, 'no timeline when this post is all there is');
  check(/No checkable factual claims found|No trusted source found/.test(t) && /No earlier trace found|earliest trace/.test(t),
    'a plain post gets plain answers: ' + t.replace(/\s+/g, ' ').slice(0, 200));
  check(/Gemma 26B, in the cloud/.test(t), 'heading says the analysis ran in the cloud when that engine is chosen');
  await sw.evaluate(() => chrome.storage.sync.set({ deepEngine: 'local' }));
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
