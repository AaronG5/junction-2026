// End-to-end: load the unpacked extension into headless Chromium, open the
// offline demo feed (mock backend), and check badges, popover, keyboard,
// infinite scroll and route changes. Screenshots go to test/out/.
//   node test/e2e.mjs            (CHROMIUM=/path/to/chrome to override)
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
  return undefined; // let playwright-core find one
}

// Static server for demo/ on a random localhost port.
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

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ppg-e2e-'));
const ctx = await chromium.launchPersistentContext(userDataDir, {
  executablePath: findChromium(),
  headless: false, // we pass --headless=new ourselves: the new headless mode supports extensions
  args: ['--headless=new', `--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`, '--no-sandbox'],
  viewport: { width: 1000, height: 1000 },
  deviceScaleFactor: 1,
});

try {
  let sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent('serviceworker', { timeout: 10000 });
  const extId = new URL(sw.url()).host;
  check(!!extId, 'extension loaded, id ' + extId);

  const page = await ctx.newPage();
  const consoleMsgs = [];
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) consoleMsgs.push(m.text()); });
  page.on('pageerror', (e) => consoleMsgs.push('pageerror: ' + e.message));

  await page.goto(feedUrl);
  const art = (id) => page.locator(`article:has(a[href$="/status/${id}"]):not(:has(div[role=link] a[href$="/status/${id}"]))`).first();
  // Header row height before any badge lands.
  const hdrH0 = await page.locator('article [data-testid="User-Name"]').first().evaluate((e) => e.getBoundingClientRect().height);

  // 1. checking spinner shows first
  await page.waitForSelector('ppg-badge', { timeout: 5000 });
  const spinners = await page.locator('ppg-badge .spin').count();
  check(spinners > 0, `checking spinners visible (${spinners})`);
  await page.screenshot({ path: path.join(outDir, '01-checking.png') });

  // 2. fast path: source match chip arrives before the judge
  const nv = art('1890412233445566771');
  await nv.locator('ppg-badge button.source').waitFor({ timeout: 3000 });
  const early = await nv.locator('ppg-badge button.source').getAttribute('aria-label');
  check(/copies a known pro-Kremlin Telegram post/i.test(early) && !/Potentially/.test(early), 'source-match chip shows before the judge verdict');
  check(await nv.locator('ppg-badge button.coord').count() === 1, 'coordination chip is separate');

  // 3. slow path via polling: content chip appears after ~3 s
  await nv.locator('ppg-badge button.source', { hasText: 'Potentially propaganda' }).waitFor({ timeout: 9000 });
  check(await nv.locator('ppg-badge button').count() === 2, 'judge verdict arrives via polling; one content chip + one coordination chip');
  await art('1890412233445566773').locator('ppg-badge button.ppg').waitFor({ timeout: 9000 });
  await art('1890412233445566774').locator('ppg-badge button.source').waitFor({ timeout: 9000 });
  await page.waitForTimeout(600);
  const chipText = await page.locator('ppg-badge button').evaluateAll((bs) => bs.map((b) => b.textContent + ' | ' + b.getAttribute('aria-label')));
  check(chipText.some((t) => /Potentially propaganda/.test(t)), 'wording "Potentially propaganda" used');
  check(!chipText.some((t) => /\b(fake|bot)\b/i.test(t) || /^propaganda/i.test(t.trim())), 'no "fake"/"bot"/bare "propaganda" wording');

  // clean posts: hidden badge
  const clean = art('1890412233445566772');
  check(await clean.locator('ppg-badge button').count() === 0, 'clean post shows nothing by default');
  // promoted + short skipped
  check(await art('1890412233445566775').locator('ppg-badge').count() === 0, 'promoted post skipped');
  check(await art('1890412233445566778').locator('ppg-badge').count() === 0, 'short post skipped');
  // unavailable (below the fold: scroll so the IntersectionObserver sends it)
  await art('1890412233445000099').scrollIntoViewIfNeeded();
  await art('1890412233445000099').locator('ppg-badge button.unavailable').waitFor({ timeout: 6000 });
  check(true, 'error -> "unavailable" state');
  // quote post shows quoted post's content signal
  const qp = art('1890412233445566776');
  await qp.locator('ppg-badge button.quoted').first().waitFor({ timeout: 9000 });
  check(true, 'quote post shows quoted-post chip');

  // No horizontal overflow: chips must stay inside the post.
  const overflow = await page.evaluate(() => [...document.querySelectorAll('ppg-badge')].filter((b) => {
    const a = b.closest('article'); return a && b.getBoundingClientRect().right > a.getBoundingClientRect().right + 0.5; }).length);
  check(overflow === 0, 'no badge runs past the post edge');
  check(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), 'no horizontal page scroll');
  const hdrH1 = await page.locator('article [data-testid="User-Name"]').first().evaluate((e) => e.getBoundingClientRect().height);
  check(Math.abs(hdrH1 - hdrH0) < 0.5, `header row height unchanged (${hdrH0} -> ${hdrH1})`);

  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(outDir, '02-feed-badges.png') });
  await nv.screenshot({ path: path.join(outDir, '03-tweet-badges.png') });

  // 4. popover: open by click, no click leak to the tweet, focus trap, Esc
  const before = await page.evaluate(() => window.__tweetClicks || 0);
  await nv.locator('ppg-badge button.source').click();
  await page.locator('ppg-popover .pop').waitFor();
  const after = await page.evaluate(() => window.__tweetClicks || 0);
  check(after === before, 'badge click does not open the tweet');
  const popText = await page.locator('ppg-popover .pop').innerText();
  check(/Source\s+@two_majors on Telegram · open/i.test(popText), 'popover: Source row "@channel on Telegram · open"');
  check(/Not a claim about who is behind this account/.test(popText), 'popover: disclaimer line');
  check(/Coordinated\s+6 accounts, same text/i.test(popText), 'popover: Coordinated row');
  check(await page.locator('ppg-popover .labels, ppg-popover section:not(.deep section)').count() === 0, 'popover: no chip row, no divided sections');
  check(!/loaded language/i.test(popText), 'popover: techniques collapsed by default');
  await page.screenshot({ path: path.join(outDir, '04-popover-nvidia.png') });
  await page.locator('ppg-popover details.more summary').click();
  const opened = await page.locator('ppg-popover details.more').innerText();
  check(/loaded language/i.test(opened) && /Copied Telegram post/i.test(opened), 'popover: techniques and the copied post in the details');
  await page.screenshot({ path: path.join(outDir, '04b-popover-nvidia-details.png') });

  // focus trap: tab many times, focus stays inside
  for (let i = 0; i < 12; i++) await page.keyboard.press('Tab');
  const inside = await page.evaluate(() => document.activeElement && document.activeElement.tagName === 'PPG-POPOVER');
  check(inside, 'focus trapped in popover');
  // feedback
  await page.locator('ppg-popover button.wrong').click();
  await page.locator('ppg-popover .thanks').waitFor({ timeout: 2000 });
  check(true, 'feedback "Wrong?" sent');
  await page.keyboard.press('Escape');
  check(await page.locator('ppg-popover').count() === 0, 'Esc closes popover');

  // keyboard open on the puppet post, check narrative + state-media echo
  const pp = art('1890412233445566773');
  await pp.locator('ppg-badge button.ppg').focus();
  await page.keyboard.press('Enter');
  await page.locator('ppg-popover .pop').waitFor();
  const t2 = await page.locator('ppg-popover .pop').innerText();
  check(/Narrative\s+The Kyiv regime is an American puppet/i.test(t2), 'popover: Narrative row with the claim only');
  check(/State media\s+\d+ mentions/i.test(t2), 'popover: State media row with the mentions count');
  await page.screenshot({ path: path.join(outDir, '05-popover-narrative.png') });
  await page.mouse.click(5, 990); // outside click
  check(await page.locator('ppg-popover').count() === 0, 'outside click closes popover');

  // Finnish farm post popover
  await art('1890412233445566774').locator('ppg-badge button.source').click();
  await page.locator('ppg-popover .pop').waitFor();
  await page.screenshot({ path: path.join(outDir, '06-popover-finnish.png') });
  await page.keyboard.press('Escape');

  // 5. infinite scroll: 200 more posts, then scroll through them
  await page.click('#more');
  const t0 = Date.now();
  for (let i = 0; i < 40; i++) { await page.mouse.wheel(0, 1600); await page.waitForTimeout(40); }
  await page.waitForTimeout(4500);
  const nBadges = await page.locator('ppg-badge').count();
  const flaggedFiller = await page.locator('article:has(a[href*="/status/18905"]) ppg-badge button').count();
  check(nBadges > 150, `badges mounted on long feed (${nBadges}), scrolled in ${Date.now() - t0} ms`);
  check(flaggedFiller > 0 && flaggedFiller < 40, `hash-chosen ~5% of filler flagged (${flaggedFiller} chips)`);

  // 6. route change: timeline swapped without reload, badges come back from cache
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click('#route');
  await page.waitForTimeout(800);
  const nvAgain = art('1890412233445566771');
  await nvAgain.locator('ppg-badge button.source', { hasText: 'Potentially propaganda' }).waitFor({ timeout: 3000 });
  check(true, 'badges restored after SPA route change (from cache)');

  // 7. dark theme
  await page.click('#theme');
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(400);
  check(await page.locator('ppg-badge[data-theme=dark]').count() > 0, 'badges follow the page dark theme');
  await page.screenshot({ path: path.join(outDir, '07-feed-dark.png') });
  await art('1890412233445566771').locator('ppg-badge button.source').click();
  await page.locator('ppg-popover .pop').waitFor();
  check(await page.locator('ppg-popover[data-theme=dark]').count() === 1, 'popover follows the page dark theme');
  await page.screenshot({ path: path.join(outDir, '07b-popover-dark.png') });
  await page.keyboard.press('Escape');

  check(consoleMsgs.length === 0, 'no console errors or warnings' + (consoleMsgs.length ? ': ' + consoleMsgs.slice(0, 3).join(' | ') : ''));

  // 8. popup
  const popup = await ctx.newPage();
  await popup.setViewportSize({ width: 340, height: 640 });
  await popup.goto(`chrome-extension://${extId}/popup/popup.html`);
  await popup.waitForTimeout(1800);
  const scanned = Number(await popup.locator('#scanned').innerText());
  const flagged = Number(await popup.locator('#flagged').innerText());
  check(scanned > 100 && flagged > 3, `popup counters: scanned ${scanned}, flagged ${flagged}`);
  await popup.screenshot({ path: path.join(outDir, '08-popup.png'), fullPage: true });

  // 8b. "In-depth analysis runs on": This computer (default) | Cloud (Modal), stored in chrome.storage.sync
  check(await popup.locator('input[name=deepEngine][value=local]').isChecked(), 'in-depth analysis runs on this computer by default');
  await popup.locator('label:has(input[name=deepEngine][value=cloud])').click();
  await popup.waitForTimeout(200);
  const stored = await popup.evaluate(() => chrome.storage.sync.get('deepEngine'));
  check(stored.deepEngine === 'cloud' && await popup.locator('#deepUrl').isDisabled() && /Modal GPU/.test(await popup.locator('#deepStatus').innerText()),
    'choosing Cloud (Modal) is saved, and the local address is greyed out');
  await popup.screenshot({ path: path.join(outDir, '08b-popup-cloud.png'), fullPage: true });
  await popup.emulateMedia({ colorScheme: 'dark' });
  await popup.screenshot({ path: path.join(outDir, '08c-popup-dark.png'), fullPage: true });
  await popup.emulateMedia({ colorScheme: 'light' });
  await popup.locator('label:has(input[name=deepEngine][value=local])').click();
  await popup.waitForTimeout(200);
  check((await popup.evaluate(() => chrome.storage.sync.get('deepEngine'))).deepEngine === 'local' && !(await popup.locator('#deepUrl').isDisabled()),
    'switching back to This computer');

  // 9. "show all" setting reaches the page live
  await popup.locator('label.row:has(#showAll)').click();
  await page.bringToFront();
  await page.waitForTimeout(500);
  const dots = await page.locator('ppg-badge button.dot').count();
  check(dots > 0, `show-all draws grey dots (${dots})`);
  await page.click('#theme');            // back to light for the clean popover
  await page.waitForTimeout(2200);
  await page.locator('ppg-badge button.dot').first().click();
  await page.locator('ppg-popover .pop').waitFor();
  check(/Nothing found/i.test(await page.locator('ppg-popover h2').innerText()), 'clean popover: "Nothing found" verdict line');
  await page.screenshot({ path: path.join(outDir, '13-popover-clean.png') });
  await page.keyboard.press('Escape');
  // turning off removes every badge
  await popup.locator('#enabled').click();
  await page.waitForTimeout(400);
  check(await page.locator('ppg-badge').count() === 0, 'switching off removes all badges');
} finally {
  await ctx.close();
  server.close();
  fs.rmSync(userDataDir, { recursive: true, force: true });
}
console.log(failures ? `\n${failures} check(s) failed` : '\nall e2e checks passed');
process.exit(failures ? 1 : 0);
