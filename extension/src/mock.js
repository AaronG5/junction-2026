// Mock backend: same contract as API.md, no network.
// About 5% of posts (chosen by a hash of the id) come back flagged, plus any
// post whose text matches one of the demo stories below. Latency: the batch
// call answers after ~300 ms (fast path), the judge "finishes" ~3 s after a
// post was first seen (slow path, picked up by polling /result).

export const MOCK_FAST_MS = 300;
export const MOCK_SLOW_MS = 3000;

export function hashId(id) {
  // FNV-1a, 32-bit. Deterministic across runs and machines.
  let h = 0x811c9dc5;
  const s = String(id);
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

const TG_NVIDIA = {
  channel: 'two_majors',
  url: 'https://t.me/two_majors/58214',
  date: '2026-11-13T18:49:44Z',
  sim: 0.97,
  text: 'The scandal around Nvidia CEO Jensen Huang is a CIA operation: the "Chinese spy" story was fed to the press to remove an executive who stood for cooperation with China. Washington is purging anyone who gets in the way.',
};

const TG_PATRIOT = {
  channel: 'two_majors',
  url: 'https://t.me/two_majors/58290',
  date: '2026-11-14T16:31:00Z',
  sim: 0.95,
  text: 'Blocked Patriot paperwork. Even on a basic permit question, the Ukrainian war machine still depends on someone else’s bureaucracy. The decision agreed by Rubio and Hegseth in October is still stuck at Michael Duffy’s level in the Pentagon.',
};

const NARR_PUPPET = { text: 'The Kyiv regime is an American puppet', source: 'euvsdisinfo', sim: 0.93 };
const NARR_CIA = { text: 'Western intelligence services stage scandals to purge inconvenient figures', source: 'euvsdisinfo', sim: 0.88 };

const ECHO_PUPPET = [{ text: 'Kiev is a puppet of Washington and acts on its orders', source: 'rt', n_aired: 253, first: '2024-01-07', last: '2024-10-02' }];

const COORD_6 = {
  label: 'coordinated',
  reasons: [
    'same text posted by 6 other accounts in the last hour',
    'these accounts were created within the same 3-day window',
  ],
  n_similar_accounts: 6,
};

// Each scenario is a function of the item so quotes/snippets stay specific.
const SCENARIOS = {
  nvidia: () => ({
    fast: {
      source_match: { strong: true, telegram: [TG_NVIDIA] },
      content: { narrative: NARR_CIA },
      coordination: COORD_6,
    },
    judge: {
      label: 'potentially_propaganda',
      score: 0.91,
      techniques: [
        { code: 'LL', name: 'loaded language', quote: 'a CIA operation' },
        { code: 'CO', name: 'conspiracy framing', quote: 'Washington is purging anyone who gets in the way' },
      ],
      reason: 'Restates a pro-Kremlin Telegram post that frames a news story as a US intelligence plot, without evidence.',
    },
  }),
  puppet: () => ({
    fast: { source_match: { strong: false, telegram: [] }, content: { narrative: NARR_PUPPET, state_media_echo: ECHO_PUPPET }, coordination: null },
    judge: {
      label: 'potentially_propaganda',
      score: 0.84,
      techniques: [
        { code: 'NC', name: 'name calling', quote: 'the Kyiv regime' },
        { code: 'LL', name: 'loaded language', quote: 'puppet' },
      ],
      reason: 'Repeats a documented disinformation narrative that Ukraine has no agency of its own.',
    },
  }),
  patriot: () => ({
    fast: { source_match: { strong: true, telegram: [TG_PATRIOT] }, content: {}, coordination: { ...COORD_6, reasons: ['machine-translated copies of the same post appeared on 11 accounts in 9 languages within 2 hours'], n_similar_accounts: 11 } },
    judge: {
      label: 'potentially_propaganda',
      score: 0.78,
      techniques: [{ code: 'LL', name: 'loaded language', quote: 'Ukrainan sotakoneisto' }],
      reason: 'A Finnish machine translation of a pro-Kremlin Telegram post that casts Ukraine as dependent and its support as failing.',
    },
  }),
  coordOnly: () => ({
    fast: { source_match: { strong: false, telegram: [] }, content: {}, coordination: COORD_6 },
    judge: { label: 'none', score: 0.22, techniques: [], reason: 'The text itself is ordinary; only the posting pattern stands out.' },
  }),
  sourceOnly: () => ({
    fast: { source_match: { strong: true, telegram: [{ ...TG_NVIDIA, url: 'https://t.me/two_majors/58101', sim: 0.96, text: 'Russia’s national curling team held a training camp before the world championship. Subscribe!' }] }, content: {}, coordination: null },
    judge: { label: 'none', score: 0.12, techniques: [], reason: 'A near-copy of a Telegram post, but the content itself is neutral sports news.' },
  }),
};
const HASH_SCENARIOS = ['nvidia', 'puppet', 'coordOnly', 'sourceOnly'];

function pickScenario(item) {
  const t = (item.text || '').toLowerCase();
  if (/nvidia|jensen huang/.test(t)) return 'nvidia';
  if (/puppet|kyiv regime|kiev regime|nukke|marionet/.test(t)) return 'puppet';
  if (/patriot-paperity|patriot paperwork/.test(t)) return 'patriot';
  if (/same text, six accounts/.test(t)) return 'coordOnly';
  const h = hashId(item.id);
  if (h % 100 < 5) return HASH_SCENARIOS[(h >>> 8) % HASH_SCENARIOS.length];
  return null;
}

function emptyContent() {
  return { label: 'none', score: 0, narrative: null, techniques: [], reason: '', state_media_echo: [] };
}

function build(item, scen, judged) {
  const r = {
    id: item.id,
    status: judged ? 'done' : 'pending',
    content: emptyContent(),
    source_match: { strong: false, telegram: [] },
    coordination: null,
  };
  if (!scen) {
    if (judged) r.content.reason = 'No known narrative, source post or manipulation technique found.';
    return r;
  }
  const s = SCENARIOS[scen](item);
  Object.assign(r.content, s.fast.content || {});
  r.source_match = s.fast.source_match;
  r.coordination = s.fast.coordination;
  if (judged) Object.assign(r.content, s.judge);
  return r;
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

/** A stateful mock with the same two calls as the real backend. */
export class MockBackend {
  constructor({ fastMs = MOCK_FAST_MS, slowMs = MOCK_SLOW_MS, now = () => Date.now() } = {}) {
    this.fastMs = fastMs;
    this.slowMs = slowMs;
    this.now = now;
    this.seen = new Map(); // id -> {item, t0, scen}
  }

  async batch(items) {
    await sleep(this.fastMs);
    return { results: items.map((it) => this._result(it)) };
  }

  async result(ids) {
    await sleep(40);
    return {
      results: ids.map((id) => {
        const s = this.seen.get(id);
        return s ? this._result(s.item) : { id, status: 'error' };
      }),
    };
  }

  async feedback() { await sleep(50); return { ok: true }; }

  _result(item) {
    let s = this.seen.get(item.id);
    if (!s) { s = { item, t0: this.now(), scen: pickScenario(item) }; this.seen.set(item.id, s); }
    if (/000099$/.test(item.id)) return { id: item.id, status: 'error' }; // demo: "unavailable"
    return build(item, s.scen, this.now() - s.t0 >= this.slowMs);
  }
}

export const _test = { pickScenario, build };

// ---------------------------------------------------------------- in-depth analysis (POST/GET /api/ext/deep)
// Same job shape as backend/veritas/deep.py. Patterns come from the scenario's judge
// techniques (plus a few loaded words found in the text), the truth check from the
// scenario, the trace from the fast-check context the extension sends along.

export const MOCK_DEEP_STEP_MS = 700;
const DEEP_STEPS = ['Finding propaganda patterns', 'Checking the claims', 'Tracing where it came from'];
const BASIS = 'The model’s own knowledge. No sources were checked, and it may be wrong or out of date.';

const LOADED = [
  ['regime', 'Name-calling', 'likely', 'Labels a government as illegitimate.'],
  ['puppet', 'Name-calling', 'clear', 'Says a country has no will of its own.'],
  ['operation', 'Conspiracy framing', 'likely', 'Frames events as a secret plot.'],
  ['purging', 'Loaded language', 'likely', 'Uses a charged word for ordinary staff changes.'],
  ['everyone knows', 'Bandwagon', 'clear', 'Claims agreement instead of giving evidence.'],
  ['wake up', 'Appeal to fear', 'possible', 'Urges alarm without saying why.'],
];

const MOCK_CLAIMS = {
  nvidia: [{ claim: 'The Nvidia scandal was staged by the CIA.', quote: 'a CIA operation', verdict: 'unverifiable', confidence: 70,
    explanation: 'No evidence for a CIA role is known; the post gives none.' }],
  puppet: [{ claim: 'Ukraine’s government is controlled by the United States.', quote: 'puppet', verdict: 'false', confidence: 85,
    explanation: 'Ukraine has its own elected government and makes its own decisions; this is a documented disinformation narrative.' }],
  patriot: [{ claim: 'Patriot paperwork agreed in October is stuck in the Pentagon.', quote: '', verdict: 'unverifiable', confidence: 60,
    explanation: 'Recent internal paperwork can’t be checked from general knowledge.' }],
};

function findIn(text, phrase) {
  if (!phrase) return null;
  const i = text.toLowerCase().indexOf(phrase.toLowerCase());
  return i < 0 ? null : text.slice(i, i + phrase.length);
}

function mockTrace(item, context, shared) {
  const nodes = [];
  const n = context.narrative;
  if (n && n.text) {
    nodes.push({ kind: 'narrative', title: 'Known disinformation narrative', who: n.source === 'euvsdisinfo' ? 'EUvsDisinfo' : (n.source || 'a fact-checking database'),
      where: 'Documented narrative', time: null, url: null, sim: n.sim ?? null, text: n.text, note: 'This post matches a documented narrative.' });
  }
  for (const e of (context.state_media_echo || []).slice(0, 2)) {
    const src = String(e.source || '').toUpperCase();
    nodes.push({ kind: 'state_media', title: 'State media: ' + src, who: src, where: 'State media', time: e.first ? e.first + 'T00:00:00Z' : null,
      url: null, sim: e.sim ?? null, text: e.text, note: `The same narrative aired ${e.n_aired || 'several'} times.` });
  }
  for (const t of ((context.source_match || {}).telegram || []).filter((x) => (x.sim || 0) >= 0.85).slice(0, 3)) {
    nodes.push({ kind: 'telegram', title: 'Telegram post', who: '@' + t.channel, where: 'Pro-Kremlin Telegram channel', time: t.date, url: t.url,
      sim: t.sim, text: t.text, note: t.sim >= 0.95 ? 'Same text (or a translation of it).' : 'Very similar text.' });
  }
  if (shared && (shared.text || shared.url)) {
    nodes.push({ kind: 'original', title: 'Original post (shared or quoted here)', who: shared.author_name || shared.author || 'Unknown author',
      where: shared.platform === 'facebook' ? 'Facebook' : 'X', time: shared.time || null, url: shared.url || null, sim: null, text: shared.text, note: 'This post passes it on.' });
  }
  nodes.push({ kind: 'this', title: 'This post', who: item.author_name || item.author || 'Unknown author',
    where: item.platform === 'facebook' ? 'Facebook' + (item.group ? ` group “${item.group}”` : '') : 'X', time: item.time || null, url: item.url || null,
    sim: null, text: null, note: item.kind === 'repost' ? 'Reposted into your feed.' : 'How it reached your feed: posted by this account.' });
  const bg = nodes.filter((x) => !x.time && x.kind !== 'this' && x.kind !== 'original');
  const dated = nodes.filter((x) => x.time && x.kind !== 'this').sort((a, b) => String(a.time).localeCompare(String(b.time)));
  const tail = nodes.filter((x) => !x.time && x.kind === 'original').concat(nodes.filter((x) => x.kind === 'this'));
  const earlier = dated.filter((x) => item.time && x.time < item.time);
  const summary = earlier.length
    ? `The earliest trace found is ${earlier[0].who} (${earlier[0].where}). ${earlier.length} earlier appearance${earlier.length === 1 ? '' : 's'} found.`
    : 'No earlier trace found: in what we know, this post is the first appearance of this text.';
  return { nodes: bg.concat(dated, tail), summary };
}

export class MockDeep {
  constructor({ stepMs = MOCK_DEEP_STEP_MS, now = () => Date.now() } = {}) {
    this.stepMs = stepMs;
    this.now = now;
    this.jobs = new Map(); // job id -> {t0, item, context, shared}
    this.byPost = new Map();
  }

  async start(body) {
    await sleep(60);
    const item = (body && body.item) || {};
    const known = this.byPost.get(item.id);
    if (known) return this.get(known);
    const id = 'mock' + hashId(item.id + ':' + this.now()).toString(36);
    this.jobs.set(id, { t0: this.now(), item, context: body.context || {}, shared: body.shared || null });
    this.byPost.set(item.id, id);
    return this.get(id);
  }

  async get(id) {
    const j = this.jobs.get(id);
    if (!j) return null;
    const step = Math.min(3, Math.floor((this.now() - j.t0) / this.stepMs));
    const job = { id, post_id: j.item.id, status: step >= 3 ? 'done' : 'running', step, steps: DEEP_STEPS.slice(),
      patterns: null, truth: null, trace: null, error: null };
    const text = j.item.text || '';
    const scen = pickScenario(j.item);
    if (step >= 1) {
      const items = [];
      const seen = new Set();
      const add = (quote, technique, tier, why) => {
        const q = findIn(text, quote);
        if (q && !seen.has(q.toLowerCase())) { seen.add(q.toLowerCase()); items.push({ quote: q, technique, tier, why }); }
      };
      if (scen) {
        for (const t of SCENARIOS[scen](j.item).judge.techniques || []) {
          add(t.quote, t.name.charAt(0).toUpperCase() + t.name.slice(1), 'clear', 'Flagged by the judge as ' + t.name + '.');
        }
      }
      for (const [w, tech, tier, why] of LOADED) add(w, tech, tier, why);
      const order = { clear: 0, likely: 1, possible: 2 };
      items.sort((a, b) => order[a.tier] - order[b.tier]);
      job.patterns = { items, model: 'mock', prompt: 'patterns.v1',
        overall: items.length ? 'Uses charged labels and framing to steer how readers see the story. (Mock answer.)' : 'No persuasion techniques stand out. (Mock answer.)' };
    }
    if (step >= 2) {
      job.truth = { items: (MOCK_CLAIMS[scen] || []).map((c) => ({ ...c, quote: findIn(text, c.quote) || '' })), model: 'mock', prompt: 'truth.v1', basis: BASIS };
    }
    if (step >= 3) job.trace = mockTrace(j.item, j.context, j.shared);
    return job;
  }
}
