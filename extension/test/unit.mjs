// Unit tests in jsdom: parser on a saved X markup fixture, view model,
// badge + popover rendering, and the mock backend. No browser needed.
//   node test/unit.mjs
import { JSDOM } from 'jsdom';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { MockBackend, hashId, _test as mockTest } from '../src/mock.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = (f) => fs.readFileSync(path.join(here, '..', 'src', f), 'utf8');
let n = 0, failed = 0;
const same = (a, b, m) => assert.equal(JSON.stringify(a), JSON.stringify(b), m); // jsdom objects live in another realm
async function test(name, fn) {
  n++;
  try { await fn(); console.log('ok   ' + name); }
  catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.message)); }
}

function dom(html) {
  const d = new JSDOM('<!doctype html><html><body>' + html + '</body></html>', { runScripts: 'outside-only', pretendToBeVisual: true, url: 'https://x.com/home' });
  for (const f of ['selectors.js', 'parse.js', 'view.js', 'ui.js']) d.window.eval(src(f));
  return d.window;
}

const fixture = fs.readFileSync(path.join(here, 'fixtures', 'x-timeline.html'), 'utf8');

// ------------------------------------------------------------- parser
await test('parser: plain post from X markup', () => {
  const w = dom(fixture);
  const arts = w.document.querySelectorAll('article[data-testid="tweet"]');
  assert.equal(arts.length, 3);
  const p = w.PPG_parseTweet(arts[0]);
  assert.equal(p.item.id, '1890412233445566771');
  assert.equal(p.item.url, 'https://x.com/eu_insider_news/status/1890412233445566771');
  assert.equal(p.item.author, 'eu_insider_news');
  assert.equal(p.item.author_name, 'EU Insider 📰');
  assert.equal(p.item.lang, 'en');
  assert.equal(p.item.time, '2026-11-14T17:48:00.000Z');
  assert.equal(p.item.kind, 'post');
  assert.equal(p.item.quoted_id, null);
  assert.ok(p.item.text.startsWith('⚡️ The Nvidia scandal is a CIA operation.'), p.item.text);
  assert.ok(p.item.text.includes('#Nvidia'));
  assert.ok(p.item.text.endsWith('https://example.org/nvidia-cia'), p.item.text);
  assert.equal(p.anchor.tagName, 'A');
  assert.ok(w.PPG_isLongEnough(p.item));
});

await test('parser: repost of a quote, quote card without permalink', () => {
  const w = dom(fixture);
  const p = w.PPG_parseTweet(w.document.querySelectorAll('article')[1]);
  assert.equal(p.item.id, '1890412233445566776');
  assert.equal(p.item.author, 'policywonk');
  assert.equal(p.item.author_name, 'Policy Wonk');
  assert.equal(p.item.kind, 'quote'); // a repost that is itself a quote: quote wins
  assert.ok(p.quoted);
  assert.equal(p.quoted.id, 'q1890412233445566776'); // synthetic: X gave no permalink
  assert.equal(p.item.quoted_id, p.quoted.id);
  assert.equal(p.quoted.author, 'eu_insider_news');
  assert.equal(p.quoted.lang, 'fi');
  assert.ok(p.quoted.text.startsWith('Ukrainan sotakoneisto'));
  assert.ok(!p.item.text.includes('Ukrainan'), 'main text must not include the quote');
});

await test('parser: promoted post skipped', () => {
  const w = dom(fixture);
  same(w.PPG_parseTweet(w.document.querySelectorAll('article')[2]), { skip: 'promoted' });
});

await test('parser: video post (placementTracking wrapper, no Ad label) is NOT promoted', () => {
  const w = dom(`<article data-testid="tweet">
   <div data-testid="User-Name"><div><a href="/someone"><span>Some One</span></a></div><div><a href="/someone"><span>@someone</span></a></div>
   <a href="/someone/status/1890000000000000001"><time datetime="2026-11-14T20:00:00.000Z">2h</time></a></div>
   <div dir="auto" lang="en" data-testid="tweetText"><span>Footage shows what the Kyiv regime is really doing to its own people, watch before it is deleted.</span></div>
   <div data-testid="placementTracking"><div data-testid="videoPlayer"><video></video></div></div>
   <div role="group"></div></article>`);
  const p = w.PPG_parseTweet(w.document.querySelector('article'));
  assert.ok(p && !p.skip, 'video post must be parsed, got ' + JSON.stringify(p && p.skip));
  assert.equal(p.item.id, '1890000000000000001');
});

await test('parser: repost detected from socialContext', () => {
  const w = dom(fixture.replace(/<div class="css-175oi2r r-9aw3ui">[\s\S]*?<div role="group" aria-label="14 replies">/, '<div role="group" aria-label="14 replies">'));
  const p = w.PPG_parseTweet(w.document.querySelectorAll('article')[1]);
  assert.equal(p.item.kind, 'repost');
  assert.equal(p.quoted, null);
});

await test('parser: demo feed has the same shape', () => {
  const html = fs.readFileSync(path.join(here, '..', 'demo', 'feed.html'), 'utf8');
  const d = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/feed.html' });
  for (const f of ['selectors.js', 'parse.js']) d.window.eval(src(f));
  const out = [...d.window.document.querySelectorAll('article')].map((a) => d.window.PPG_parseTweet(a));
  const ok = out.filter((o) => o && o.item);
  assert.ok(ok.length >= 8, 'parsed ' + ok.length);
  assert.ok(out.some((o) => o && o.skip === 'promoted'));
  const fi = ok.find((o) => o.item.lang === 'fi');
  assert.ok(fi && /Patriot-paperityö/.test(fi.item.text));
  const q = ok.find((o) => o.item.kind === 'quote');
  assert.equal(q.quoted.id, '1890412233445566771');
});

// ------------------------------------------------------------- view model
const vmW = dom('');
const vm = (r, s, q) => vmW.PPG_viewModel(r, s || { sensitivity: 'normal' }, q);

await test('view: states', () => {
  assert.equal(vm(undefined).state, 'checking');
  assert.equal(vm({ id: '1', status: 'error' }).state, 'unavailable');
  assert.equal(vm({ id: '1', status: 'pending' }).state, 'checking');
  assert.equal(vm({ id: '1', status: 'done', content: { label: 'none' } }).state, 'clean');
  assert.equal(vm({ id: '1', status: 'timeout' }).state, 'unavailable');
});

await test('view: source match shows before judge, then content + coordination stay separate', () => {
  const fast = { id: '1', status: 'pending', source_match: { strong: true, telegram: [{}] }, content: {}, coordination: { label: 'coordinated' } };
  let v = vm(fast);
  assert.equal(v.state, 'flagged');
  assert.equal(v.pending, true);
  same(v.chips.map((c) => c.signal), ['content', 'coordination']);
  assert.match(v.chips[0].text, /Copies pro-Kremlin Telegram post/);
  v = vm({ ...fast, status: 'done', content: { label: 'potentially_propaganda', score: 0.9 } });
  assert.equal(v.chips.length, 2);
  assert.equal(v.chips[0].text, 'Potentially propaganda');
  assert.equal(v.chips[0].kind, 'source'); // strong styling kept
  same(v.labels.map((l) => l.kind), ['source', 'ppg', 'coord']);
});

await test('view: strict raises the bar for the judge only', () => {
  const r = { id: '1', status: 'done', content: { label: 'potentially_propaganda', score: 0.6 } };
  assert.equal(vm(r).state, 'flagged');
  assert.equal(vm(r, { sensitivity: 'strict' }).state, 'clean');
  const s = { id: '1', status: 'done', source_match: { strong: true }, content: { label: 'none' } };
  assert.equal(vm(s, { sensitivity: 'strict' }).state, 'flagged');
});

await test('view: quoted post flag is labelled as quoted', () => {
  const v = vm({ id: '1', status: 'done', content: { label: 'none' } }, null, { id: 'q', status: 'done', content: { label: 'potentially_propaganda', score: 0.9 } });
  assert.equal(v.state, 'flagged');
  assert.match(v.chips[0].text, /^Quoted: Potentially propaganda$/);
});

await test('view: wording never says propaganda/fake/bot on its own', () => {
  const all = Object.values(vmW.PPG_LABELS).join(' | ');
  assert.ok(!/\b(fake|bot|bots)\b/i.test(all));
  for (const m of all.matchAll(/propaganda/gi)) {
    assert.match(all.slice(Math.max(0, m.index - 12), m.index), /potentially $/i);
  }
});

// ------------------------------------------------------------- badge + popover
await test('badge: renders buttons with aria-labels in a shadow root', () => {
  const w = dom(fixture);
  const art = w.document.querySelector('article');
  const b = new w.PPG_Badge(() => {});
  art.querySelector('time').closest('a').after(b.host);
  b.render(vm(undefined), false);
  assert.ok(b.root.querySelector('.spin'));
  b.render(vm({ id: '1', status: 'done', content: { label: 'none' } }), false);
  assert.equal(b.root.querySelectorAll('button').length, 0, 'clean is hidden by default');
  b.render(vm({ id: '1', status: 'done', content: { label: 'none' } }), true);
  assert.equal(b.root.querySelectorAll('button.dot').length, 1, 'grey dot with show-all');
  b.render(vm({ id: '1', status: 'done', source_match: { strong: true }, content: { label: 'potentially_propaganda', score: 0.9 }, coordination: { label: 'coordinated' } }), false);
  const btns = [...b.root.querySelectorAll('button')];
  assert.equal(btns.length, 2);
  assert.match(btns[0].getAttribute('aria-label'), /Potentially propaganda; copies a known pro-Kremlin Telegram post/);
  assert.match(btns[1].getAttribute('aria-label'), /Coordinated posting/);
  assert.equal(w.document.querySelector('style'), null, 'no styles leak into the page');
});

await test('popover: content, focus, Esc, feedback; server text is not parsed as HTML', () => {
  const w = dom(fixture);
  const sent = [];
  const pop = new w.PPG_Popover((fb) => sent.push(fb));
  const btn = w.document.createElement('button');
  w.document.body.appendChild(btn);
  const result = {
    id: '1890412233445566771', status: 'done',
    content: { label: 'potentially_propaganda', score: 0.9, narrative: { text: 'The Kyiv regime is an American puppet', source: 'euvsdisinfo', sim: 0.93 },
      techniques: [{ code: 'LL', name: 'loaded language', quote: '<img src=x onerror=alert(1)>' }], reason: 'One sentence.',
      state_media_echo: [{ text: 'x', source: 'rt', n_aired: 253, first: '2024-01-07', last: '2024-10-02' }] },
    source_match: { strong: true, telegram: [{ channel: 'two_majors', url: 'javascript:alert(1)', date: '2026-11-13T18:49:44Z', sim: 0.97, text: 'snippet' }] },
    coordination: { label: 'coordinated', reasons: ['same text posted by 6 other accounts in the last hour'], n_similar_accounts: 6 },
  };
  pop.open(btn, { item: { id: result.id }, result, vm: vm(result) });
  const root = pop.root;
  const text = root.querySelector('.pop').textContent;
  assert.match(text, /EUvsDisinfo · 93% similar/);
  assert.match(text, /This narrative aired 253× on RT, Jan–Oct 2024\./);
  assert.match(text, /@two_majors · 2026-11-13 · 97% similar/);
  assert.match(text, /same text posted by 6 other accounts/);
  assert.match(text, /Not a claim about who is behind this account/);
  assert.equal(root.querySelector('img'), null, 'quote rendered as text');
  assert.equal(root.querySelector('a[href^="javascript"]'), null, 'unsafe link dropped');
  assert.equal(root.querySelector('[role=dialog]').getAttribute('aria-modal'), 'true');
  assert.ok(root.activeElement, 'focus moved into the dialog');
  root.querySelector('button.primary').click();
  same(sent, [{ tweet_id: result.id, verdict_shown: 'source_match+potentially_propaganda+coordinated', user_says: 'wrong' }]);
  w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(pop.isOpen(), false);
});

// ------------------------------------------------------------- mock backend
await test('mock: deterministic ~5% flag rate by id hash', () => {
  let flagged = 0;
  const N = 4000;
  for (let i = 0; i < N; i++) {
    const id = String(1890000000000000000n + BigInt(i * 104729));
    if (mockTest.pickScenario({ id, text: 'ordinary words about the weather today and nothing else at all' })) flagged++;
  }
  const rate = flagged / N;
  assert.ok(rate > 0.03 && rate < 0.07, 'rate ' + rate);
  assert.equal(hashId('123'), hashId('123'));
});

await test('mock: fast path pending with source match, slow path done after 3 s', async () => {
  let t = 1000;
  const m = new MockBackend({ fastMs: 0, slowMs: 3000, now: () => t });
  const item = { id: '1890412233445566771', text: 'The Nvidia scandal is a CIA operation, they say.' };
  const r1 = (await m.batch([item])).results[0];
  assert.equal(r1.status, 'pending');
  assert.equal(r1.source_match.strong, true);
  assert.equal(r1.source_match.telegram[0].channel, 'two_majors');
  assert.equal(r1.content.label, 'none');
  t += 3001;
  const r2 = (await m.result([item.id])).results[0];
  assert.equal(r2.status, 'done');
  assert.equal(r2.content.label, 'potentially_propaganda');
  assert.ok(r2.content.techniques.length > 0);
  const clean = (await m.batch([{ id: '1890000000000000001', text: 'ordinary words about the weather today' }])).results[0];
  assert.equal(clean.coordination, null);
});

console.log(failed ? `\n${failed}/${n} failed` : `\nall ${n} unit tests passed`);
process.exit(failed ? 1 : 0);
