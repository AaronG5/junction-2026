// In-depth analysis, on request: the "Analyze" button next to the badge, the highlights in
// the post, and three popover sections:
//   1. Propaganda patterns   phrases marked clear / likely / possible, highlighted in the post
//   2. Fact check            each checkable claim with the model's verdict (no sources yet)
//   3. Where it came from    a timeline from the earliest known trace to this post in your feed
// Shared by X (src/content.js) and Facebook (src/fb/content.js). Network calls go through the
// worker (messages deep-start / deep-get); the job runs on the Veritas Lens backend on this
// computer (backend/veritas/deep.py), or in src/mock.js in mock mode.
//
// Highlights use the CSS Custom Highlight API: ranges over the post's own text nodes, styled
// with ::highlight(). The page's DOM is never changed, so X's and Facebook's React can't break.
// Everything is built with textContent; nothing from a post or the model is parsed as HTML.
/* global chrome, PPG_TOKENS, PPG_theme */
/* exported PPG_Deep, PPG_highlight, PPG_DeepButton, PPG_deepSection */

var PPG_DEEP_POLL_MS = 1500;
var PPG_DEEP_MAX_MS = 5 * 60 * 1000;
var PPG_DEEP_WAIT_MS = 10000; // wait this long for the fast check before starting without it
var PPG_TIERS = { clear: 'Clear', likely: 'Likely', possible: 'Possible' };
var PPG_VERDICTS = { 'true': 'Accurate', 'false': 'False', misleading: 'Misleading', unverifiable: 'Can’t tell' };

// ---------------------------------------------------------------- jobs
var PPG_Deep = (function () {
  var states = new Map(); // post id -> {status: idle|starting|running|done|error, job, error}
  var listeners = [];

  function emit(id) { listeners.forEach(function (f) { try { f(id); } catch (e) { /* a view went away */ } }); }
  function state(id) { return states.get(id) || { status: 'idle' }; }
  function set(id, s) { states.set(id, s); emit(id); }

  // What the fast check already found, for the trace.
  function context(result) {
    if (!result) return {};
    var c = result.content || {};
    return { narrative: c.narrative || null, state_media_echo: c.state_media_echo || [], source_match: result.source_match || null };
  }

  function send(msg, cb) {
    try {
      chrome.runtime.sendMessage(msg, function (r) {
        if (chrome.runtime.lastError) cb({ ok: false, error: 'The extension was reloaded. Refresh this page.' });
        else cb(r);
      });
    } catch (e) { cb({ ok: false, error: 'The extension was reloaded. Refresh this page.' }); }
  }

  /**
   * getResult() gives the post's fast-check result (or undefined while it hasn't arrived).
   * The trace is built from it, so when Analyze is clicked before the fast check answered,
   * this waits for it (up to PPG_DEEP_WAIT_MS, then goes ahead without it).
   */
  function start(item, getResult, shared) {
    var s = state(item.id);
    if (s.status === 'starting' || s.status === 'running' || s.status === 'done') return;
    set(item.id, { status: 'starting' });
    var t0 = Date.now();
    (function go() {
      var r = getResult();
      if ((!r || (r.status === 'pending' && !r.source_match)) && Date.now() - t0 < PPG_DEEP_WAIT_MS) { setTimeout(go, 250); return; }
      send({ type: 'deep-start', body: { item: item, shared: shared || null, context: context(r) } }, function (res) {
        if (!res || !res.ok || !res.data) { set(item.id, { status: 'error', error: (res && res.error) || 'The analysis could not start.' }); return; }
        track(item.id, res.data, Date.now());
      });
    })();
  }

  function track(id, job, t0) {
    var status = job.status === 'done' ? 'done' : job.status === 'error' ? 'error' : 'running';
    set(id, { status: status, job: job, error: job.error || null });
    if (status !== 'running') return;
    if (Date.now() - t0 > PPG_DEEP_MAX_MS) { set(id, { status: 'error', job: job, error: 'The analysis took too long. Try again.' }); return; }
    setTimeout(function () {
      send({ type: 'deep-get', id: job.id }, function (res) {
        if (!res || !res.ok || !res.data) { set(id, { status: 'error', job: job, error: (res && res.error) || 'The analysis was lost. Try again.' }); return; }
        track(id, res.data, t0);
      });
    }, PPG_DEEP_POLL_MS);
  }

  return { state: state, start: start, onChange: function (f) { listeners.push(f); } };
})();

// ---------------------------------------------------------------- highlights in the post
var PPG_highlight = (function () {
  var supported = typeof CSS !== 'undefined' && !!CSS.highlights && typeof Highlight === 'function';
  var reg = null;
  var ranges = new Map();  // post id -> [{tier, range}]
  var missing = new Map(); // post id -> Set of quotes not found on screen
  var CSS_TEXT =
    '::highlight(ppg-clear){background-color:rgba(208,59,59,.24);text-decoration:underline 2px rgba(208,59,59,.95);text-underline-offset:3px}' +
    '::highlight(ppg-likely){background-color:rgba(236,131,90,.24);text-decoration:underline 2px rgba(236,131,90,.95);text-underline-offset:3px}' +
    '::highlight(ppg-possible){background-color:rgba(224,185,60,.22);text-decoration:underline 2px dotted rgba(200,160,40,.95);text-underline-offset:3px}';

  function ensure() {
    if (reg) return;
    reg = {};
    ['clear', 'likely', 'possible'].forEach(function (t) { reg[t] = new Highlight(); CSS.highlights.set('ppg-' + t, reg[t]); });
    var st = document.createElement('style');
    st.setAttribute('data-ppg', 'highlights');
    st.textContent = CSS_TEXT;
    (document.head || document.documentElement).appendChild(st);
  }

  function clear(id) {
    var old = ranges.get(id);
    if (old && reg) old.forEach(function (r) { reg[r.tier].delete(r.range); });
    ranges.delete(id);
  }

  function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  // Words in order, any (or no) space between them: the post's text nodes are joined without
  // the line breaks the page shows between them.
  function phraseRe(q) {
    var words = String(q || '').trim().split(/\s+/).filter(Boolean);
    return words.length ? new RegExp(words.map(esc).join('\\s*'), 'iu') : null;
  }

  function locate(nodes, i) {
    for (var k = nodes.length - 1; k >= 0; k--) {
      if (nodes[k].start <= i) return { node: nodes[k].node, offset: Math.min(i - nodes[k].start, nodes[k].node.nodeValue.length) };
    }
    return { node: nodes[0].node, offset: 0 };
  }

  /** Highlight each pattern's phrase inside `root` (the post's text). Quotes not on screen
   *  (for example behind "See more") are remembered, so the popover can say so. */
  function apply(id, root, patterns) {
    clear(id);
    if (!root || !patterns || !patterns.length) { missing.set(id, new Set()); return; }
    var nodes = [], full = '', n;
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while ((n = walker.nextNode())) {
      if (n.parentElement && n.parentElement.closest('ppg-badge, ppg-deep, [data-ppg="fb-row"]')) continue;
      nodes.push({ node: n, start: full.length });
      full += n.nodeValue;
    }
    var lost = new Set();
    if (!supported || !nodes.length) {
      patterns.forEach(function (p) { lost.add(p.quote); });
      missing.set(id, lost);
      return;
    }
    ensure();
    var mine = [];
    patterns.forEach(function (p) {
      var re = phraseRe(p.quote);
      var m = re && re.exec(full);
      if (!m || !reg[p.tier]) { lost.add(p.quote); return; }
      var a = locate(nodes, m.index), b = locate(nodes, m.index + m[0].length);
      var range = document.createRange();
      try { range.setStart(a.node, a.offset); range.setEnd(b.node, b.offset); } catch (e) { lost.add(p.quote); return; }
      reg[p.tier].add(range);
      mine.push({ tier: p.tier, range: range });
    });
    ranges.set(id, mine);
    missing.set(id, lost);
  }

  /** Still in place? (The site may have re-rendered the text: then apply again.) */
  function stale(id) {
    var mine = ranges.get(id);
    return !mine || mine.some(function (r) { return !r.range.startContainer.isConnected; });
  }

  return {
    supported: supported, apply: apply, clear: clear, stale: stale,
    missing: function (id) { return missing.get(id) || new Set(); },
  };
})();

// ---------------------------------------------------------------- the "Analyze" button
var PPG_DEEP_BTN_CSS =
  ':host{display:inline-flex;align-items:center;height:20px;max-height:20px;margin-left:6px;vertical-align:middle;white-space:nowrap;line-height:1;contain:layout style}' +
  'button{all:unset;box-sizing:border-box;cursor:pointer;display:inline-flex;align-items:center;gap:5px;height:20px;padding:0 8px;' +
  'border-radius:10px;font-size:11.5px;font-weight:600;color:var(--text-muted);border:1px solid var(--border);background:transparent}' +
  'button:hover{color:var(--text);border-color:var(--text-muted)}' +
  'button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}' +
  '.ic{font-size:12px;font-weight:800}' +
  '.dot{width:7px;height:7px;border-radius:50%;display:inline-block}' +
  '.clear{background:#D03B3B}.likely{background:#EC835A}.possible{background:#E0B93C}' +
  '.n{margin-right:2px}' +
  'button.tiny .tx{display:none}button.tiny{padding:0 6px}' +
  '.spin{width:10px;height:10px;border-radius:50%;border:2px solid var(--border);border-top-color:var(--text-muted);box-sizing:border-box;animation:r .9s linear infinite}' +
  '@keyframes r{to{transform:rotate(360deg)}}@media (prefers-reduced-motion:reduce){.spin{animation:none}}';

function PPG_DeepButton(onClick) {
  this.host = document.createElement('ppg-deep');
  this.host.setAttribute('data-ppg', 'deep');
  this.root = this.host.attachShadow({ mode: 'open' });
  var st = document.createElement('style');
  st.textContent = PPG_TOKENS + PPG_DEEP_BTN_CSS;
  this.root.appendChild(st);
  this.btn = document.createElement('button');
  this.btn.type = 'button';
  this.btn.setAttribute('aria-haspopup', 'dialog');
  this.root.appendChild(this.btn);
  var self = this;
  this.btn.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); onClick(self.btn); });
  // Keep clicks away from the site's own handlers (which would open the post).
  ['click', 'mousedown', 'mouseup', 'pointerdown', 'keydown'].forEach(function (ev) {
    self.host.addEventListener(ev, function (e) {
      if (ev === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
      e.stopPropagation();
    });
  });
  this.key = '';
}

PPG_DeepButton.prototype.render = function (s) {
  this.host.setAttribute('data-theme', PPG_theme);
  var job = s.job || {};
  var counts = { clear: 0, likely: 0, possible: 0 };
  ((job.patterns && job.patterns.items) || []).forEach(function (p) { if (p.tier in counts) counts[p.tier]++; });
  var bad = ((job.truth && job.truth.items) || []).filter(function (c) { return c.verdict === 'false' || c.verdict === 'misleading'; }).length;
  var key = PPG_theme + '|' + s.status + '|' + (job.step || 0) + '|' + counts.clear + counts.likely + counts.possible + '|' + bad;
  if (key === this.key) { this.fit(); return; }
  this.key = key;
  var b = this.btn;
  while (b.firstChild) b.removeChild(b.firstChild);
  function add(cls, text) { var e = document.createElement('span'); if (cls) e.className = cls; if (text != null) e.textContent = text; b.appendChild(e); return e; }

  if (s.status === 'starting' || s.status === 'running') {
    add('spin');
    add('tx', 'Analyzing ' + Math.min(3, (job.step || 0) + 1) + '/3');
    b.setAttribute('aria-label', 'In-depth analysis running. Open details');
  } else if (s.status === 'done') {
    var n = counts.clear + counts.likely + counts.possible;
    ['clear', 'likely', 'possible'].forEach(function (t) {
      if (!counts[t]) return;
      add('dot ' + t).setAttribute('aria-hidden', 'true');
      add('n', String(counts[t]));
    });
    add('tx', n ? (bad ? 'Analysis · ' + bad + ' false/misleading' : 'Analysis') : (bad ? bad + ' false/misleading' : 'No patterns'));
    b.setAttribute('aria-label', 'In-depth analysis: ' + n + ' propaganda pattern' + (n === 1 ? '' : 's') +
      (bad ? ', ' + bad + ' false or misleading claim' + (bad === 1 ? '' : 's') : '') + '. Open details');
  } else {
    add('ic', '⌕').setAttribute('aria-hidden', 'true');
    add('tx', s.status === 'error' ? 'Retry analysis' : 'Analyze');
    b.setAttribute('aria-label', (s.status === 'error' ? 'The analysis failed. Try again' : 'Analyze in depth') +
      ': propaganda patterns, fact check and where it came from');
  }
  b.title = b.getAttribute('aria-label');
  this.fit();
};

// Like the badge: if the button would run past the post's right edge (narrow column, long
// chips before it), drop its words and keep the icon and dots. aria-label keeps the words.
PPG_DeepButton.prototype.fit = function () {
  var self = this;
  requestAnimationFrame(function () {
    if (!self.host.isConnected) return;
    var box = self.host.closest('article, [aria-posinset], div[role="article"]') || self.host.parentElement;
    if (!box) return;
    self.btn.classList.remove('tiny');
    if (self.host.getBoundingClientRect().right > box.getBoundingClientRect().right - 8) self.btn.classList.add('tiny');
  });
};

// ---------------------------------------------------------------- popover sections
var PPG_DEEP_POP_CSS =
  '.deep h3 .sub{text-transform:none;letter-spacing:0;font-weight:500;margin-left:6px}' +
  '.legend{display:flex;gap:10px;flex-wrap:wrap;margin:2px 0 6px;font-size:11.5px;color:var(--text-muted)}' +
  '.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}' +
  '.t-clear{background:rgba(208,59,59,.35);border-bottom:2px solid #D03B3B}' +
  '.t-likely{background:rgba(236,131,90,.35);border-bottom:2px solid #EC835A}' +
  '.t-possible{background:rgba(224,185,60,.32);border-bottom:2px dotted #C8A028}' +
  '.pat{margin:6px 0;padding-left:8px;border-left:3px solid var(--border)}' +
  '.pat.clear{border-left-color:#D03B3B}.pat.likely{border-left-color:#EC835A}.pat.possible{border-left-color:#E0B93C}' +
  '.tier{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-right:6px}' +
  '.tier.clear{color:var(--v-false-text)}.tier.likely{color:var(--v-mis-text)}.tier.possible{color:var(--text-muted)}' +
  '.mark{border-radius:3px;padding:0 2px}' +
  '.claim{margin:8px 0}' +
  '.vd{display:inline-block;font-size:11px;font-weight:700;border-radius:8px;padding:1px 7px;margin-right:6px;border:1px solid}' +
  '.vd.true{color:#2F7D4F;border-color:#2F7D4F;background:rgba(47,125,79,.1)}' +
  '.vd.false{color:var(--v-false-text);border-color:var(--v-false);background:var(--v-false-soft)}' +
  '.vd.misleading{color:var(--v-mis-text);border-color:var(--v-mis);background:var(--v-mis-soft)}' +
  '.vd.unverifiable{color:var(--text-muted);border-color:var(--border)}' +
  '.basis{font-style:italic}' +
  'ol.tl{list-style:none;margin:6px 0 0;padding:0}' +
  'ol.tl li{position:relative;padding:0 0 10px 18px;border-left:2px solid var(--border);margin-left:5px}' +
  'ol.tl li:last-child{border-left-color:transparent;padding-bottom:0}' +
  'ol.tl li::before{content:"";position:absolute;left:-6px;top:3px;width:10px;height:10px;border-radius:50%;background:var(--surface);border:2px solid var(--text-muted);box-sizing:border-box}' +
  'ol.tl li.telegram::before,ol.tl li.state_media::before,ol.tl li.narrative::before{border-color:var(--v-false)}' +
  'ol.tl li.this::before{background:var(--accent);border-color:var(--accent)}' +
  '.when{font-size:11.5px;color:var(--text-muted)}' +
  '.steps{margin:4px 0;padding-left:0;list-style:none}' +
  '.steps li{margin:2px 0;color:var(--text-muted)}.steps li.now{color:var(--text);font-weight:600}' +
  '.err{color:var(--v-false-text)}' +
  '.deep-go{background:var(--accent-soft);color:var(--accent-soft-text);border:1px solid var(--accent);margin-top:4px}' +
  '.deep-go:hover{background:var(--accent);color:var(--on-accent)}';

function ppgD(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function ppgDSafe(u) {
  try { var x = new URL(u); return x.protocol === 'https:' ? x.href : null; } catch (e) { return null; }
}
function ppgDWhen(iso) {
  var t = Date.parse(iso || '');
  if (isNaN(t)) return 'Undated';
  var d = new Date(t);
  var pad = function (n) { return (n < 10 ? '0' : '') + n; };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}
function ppgDSnip(t, n) { t = String(t || '').replace(/\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n - 1) + '…' : t; }

/**
 * The in-depth sections of the popover. ctx.deepStart() starts the analysis (the content
 * script provides it, with the post's fast-check result and quoted or shared original).
 */
function PPG_deepSection(pop, ctx) {
  if (!ctx || !ctx.item || !ctx.item.id) return;
  var s = PPG_Deep.state(ctx.item.id);
  var job = s.job || {};
  var box = ppgD('div', 'deep');
  var st = document.createElement('style');
  st.textContent = PPG_DEEP_POP_CSS;
  box.appendChild(st);

  var head = ppgD('section');
  var h = ppgD('h3', null, 'In-depth analysis');
  h.appendChild(ppgD('span', 'sub', 'Gemma, on your computer'));
  head.appendChild(h);
  box.appendChild(head);

  if (s.status === 'idle' || s.status === 'error') {
    if (s.status === 'error') head.appendChild(ppgD('p', 'err', s.error || 'The analysis failed.'));
    head.appendChild(ppgD('p', 'meta', 'Marks propaganda patterns in the text, checks its claims and traces where it came from. Usually takes 10–60 s.'));
    // Not "primary": that class is the footer's "Report a mistake" (and tests find it by it).
    var go = ppgD('button', 'btn deep-go', s.status === 'error' ? 'Try again' : 'Analyze in depth');
    go.type = 'button';
    go.addEventListener('click', function () { if (ctx.deepStart) ctx.deepStart(); });
    head.appendChild(go);
    pop.appendChild(box);
    return;
  }

  if (s.status !== 'done') {
    var steps = ppgD('ul', 'steps');
    (job.steps || ['Finding propaganda patterns', 'Checking the claims', 'Tracing where it came from']).forEach(function (name, i) {
      var step = job.step || 0;
      var li = ppgD('li', i === step ? 'now' : '', (i < step ? '✓ ' : i === step ? '… ' : '○ ') + name);
      steps.appendChild(li);
    });
    head.appendChild(steps);
  }

  if (job.patterns) box.appendChild(patternsSection(job.patterns, PPG_highlight.missing(ctx.item.id)));
  if (job.truth) box.appendChild(truthSection(job.truth));
  if (job.trace) box.appendChild(traceSection(job.trace));
  pop.appendChild(box);
}

function patternsSection(p, missing) {
  var sec = ppgD('section');
  sec.appendChild(ppgD('h3', null, 'Propaganda patterns'));
  var items = p.items || [];
  if (items.length) {
    var lg = ppgD('div', 'legend');
    ['clear', 'likely', 'possible'].forEach(function (t) {
      var x = ppgD('span');
      x.appendChild(ppgD('span', 'sw t-' + t));
      x.appendChild(document.createTextNode(PPG_TIERS[t]));
      lg.appendChild(x);
    });
    sec.appendChild(lg);
  }
  if (p.overall) sec.appendChild(ppgD('p', null, p.overall));
  if (!items.length) sec.appendChild(ppgD('p', 'meta', 'No propaganda patterns found in the text.'));
  items.forEach(function (x) {
    var row = ppgD('div', 'pat ' + x.tier);
    var top = ppgD('p');
    top.appendChild(ppgD('span', 'tier ' + x.tier, PPG_TIERS[x.tier] || x.tier));
    top.appendChild(ppgD('b', null, x.technique));
    row.appendChild(top);
    var q = ppgD('p');
    q.appendChild(ppgD('span', 'mark t-' + x.tier, '“' + ppgDSnip(x.quote, 140) + '”'));
    row.appendChild(q);
    if (x.why) row.appendChild(ppgD('p', 'meta', x.why));
    if (missing.has(x.quote)) row.appendChild(ppgD('p', 'meta', 'Not highlighted: this part of the post isn’t on screen (try “See more”).'));
    sec.appendChild(row);
  });
  return sec;
}

function truthSection(t) {
  var sec = ppgD('section');
  sec.appendChild(ppgD('h3', null, 'Fact check'));
  sec.appendChild(ppgD('p', 'meta basis', t.basis || 'The model’s own knowledge. No sources were checked.'));
  var items = t.items || [];
  if (!items.length) { sec.appendChild(ppgD('p', 'meta', 'No checkable factual claims found.')); return sec; }
  if (items.every(function (c) { return c.verdict === 'unverifiable'; })) {
    sec.appendChild(ppgD('p', null, 'None of the claims could be checked from what the model knows.'));
  }
  items.forEach(function (c) {
    var row = ppgD('div', 'claim');
    var top = ppgD('p');
    top.appendChild(ppgD('span', 'vd ' + c.verdict, PPG_VERDICTS[c.verdict] || c.verdict));
    top.appendChild(document.createTextNode(c.claim));
    row.appendChild(top);
    if (c.explanation) row.appendChild(ppgD('p', 'meta', c.explanation + (c.verdict !== 'unverifiable' && typeof c.confidence === 'number' ? ' (' + c.confidence + '% sure)' : '')));
    sec.appendChild(row);
  });
  return sec;
}

function traceSection(tr) {
  var sec = ppgD('section');
  sec.appendChild(ppgD('h3', null, 'Where it came from'));
  if (tr.summary) sec.appendChild(ppgD('p', null, tr.summary));
  var ol = ppgD('ol', 'tl');
  (tr.nodes || []).forEach(function (n) {
    var li = ppgD('li', n.kind);
    li.appendChild(ppgD('div', 'when', n.time ? ppgDWhen(n.time) : (n.kind === 'this' ? 'Undated' : 'Background')));
    var t = ppgD('p');
    t.appendChild(ppgD('b', null, n.title));
    li.appendChild(t);
    var who = [n.who, n.where].filter(Boolean).join(' · ');
    if (typeof n.sim === 'number') who += ' · ' + Math.round(n.sim * 100) + '% similar';
    var meta = ppgD('p', 'meta', who);
    var u = ppgDSafe(n.url);
    if (u) {
      meta.appendChild(document.createTextNode(' · '));
      var a = ppgD('a', null, 'open');
      a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer';
      meta.appendChild(a);
    }
    li.appendChild(meta);
    if (n.text) li.appendChild(ppgD('blockquote', null, ppgDSnip(n.text, 200)));
    if (n.note) li.appendChild(ppgD('p', 'meta', n.note));
    ol.appendChild(li);
  });
  sec.appendChild(ol);
  return sec;
}
