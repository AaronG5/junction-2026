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
