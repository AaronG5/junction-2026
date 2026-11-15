// In-depth analysis, on request: the "Analyze" button next to the badge, the highlights in
// the post, and three popover sections:
//   1. Propaganda patterns   phrases marked clear / likely / possible, highlighted in the post
//   2. Fact check            each checkable claim with the model's verdict (no sources yet)
//   3. Where it came from    a timeline from the earliest known trace to this post in your feed
// Shared by X (src/content.js) and Facebook (src/fb/content.js). Network calls go through the
// worker (messages deep-start / deep-get); the job runs on the Providence backend on this
// computer (backend/veritas/deep.py), or, when the settings popup says "Cloud (Modal)", the same
// code with stock Gemma 26B on Modal (propaganda/gemma/modal_deep.py); in src/mock.js in mock mode.
// The popover's heading says which one ran.
//
// Highlights use the CSS Custom Highlight API: ranges over the post's own text nodes, styled
// with ::highlight(). The page's DOM is never changed, so X's and Facebook's React can't break.
// Everything is built with textContent; nothing from a post or the model is parsed as HTML.
/* global chrome, PPG_TOKENS, PPG_theme */
/* exported PPG_Deep, PPG_highlight, PPG_DeepButton, PPG_deepSection */

var PPG_DEEP_POLL_MS = 1500;
var PPG_DEEP_MAX_MS = 5 * 60 * 1000;
var PPG_DEEP_CLOUD_MAX_MS = 15 * 60 * 1000; // the cloud model may first need a few minutes to start
var PPG_DEEP_ENGINES = { local: 'Gemma, on your computer', cloud: 'Gemma 26B, in the cloud' };
// The engine chosen in the settings popup (chrome.storage.sync deepEngine), for posts not analyzed yet.
var PPG_deepEngine = 'local';
try {
  chrome.storage.sync.get({ deepEngine: 'local' }, function (s) { if (s && s.deepEngine === 'cloud') PPG_deepEngine = 'cloud'; });
  chrome.storage.onChanged.addListener(function (ch, area) {
    if (area === 'sync' && ch.deepEngine) PPG_deepEngine = ch.deepEngine.newValue === 'cloud' ? 'cloud' : 'local';
  });
} catch (e) { /* no extension context (tests) */ }
var PPG_DEEP_WAIT_MS = 10000; // wait this long for the fast check before starting without it
var PPG_TIERS = { clear: 'Clear', likely: 'Likely', possible: 'Possible' };
var PPG_VERDICTS = { supported: 'Supported', refuted: 'Refuted', misleading: 'Misleading', unverified: 'Unverified' };

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
    set(item.id, { status: 'starting', engine: PPG_deepEngine });
    var t0 = Date.now();
    (function go() {
      var r = getResult();
      if ((!r || (r.status === 'pending' && !r.source_match)) && Date.now() - t0 < PPG_DEEP_WAIT_MS) { setTimeout(go, 250); return; }
      send({ type: 'deep-start', body: { item: item, shared: shared || null, context: context(r) } }, function (res) {
        var engine = res && res.engine === 'cloud' ? 'cloud' : 'local';
        if (!res || !res.ok || !res.data) { set(item.id, { status: 'error', engine: engine, error: (res && res.error) || 'The analysis could not start.' }); return; }
        track(item.id, res.data, Date.now(), engine);
      });
    })();
  }

  function track(id, job, t0, engine) {
    var status = job.status === 'done' ? 'done' : job.status === 'error' ? 'error' : 'running';
    set(id, { status: status, job: job, engine: engine, error: job.error || null });
    if (status !== 'running') return;
    if (Date.now() - t0 > (engine === 'cloud' ? PPG_DEEP_CLOUD_MAX_MS : PPG_DEEP_MAX_MS)) {
      set(id, { status: 'error', job: job, engine: engine, error: 'The analysis took too long. Try again.' }); return;
    }
    setTimeout(function () {
      send({ type: 'deep-get', id: job.id }, function (res) {
        if (!res || !res.ok || !res.data) { set(id, { status: 'error', job: job, engine: engine, error: (res && res.error) || 'The analysis was lost. Try again.' }); return; }
        track(id, res.data, t0, engine);
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
    if (old && shown && old.indexOf(shown) >= 0) hide();
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
      mine.push({ tier: p.tier, range: range, pattern: p });
    });
    ranges.set(id, mine);
    missing.set(id, lost);
    listen();
  }

  // ---- hover: the explanation of a highlighted phrase, right under it.
  // Highlights have no events of their own, so one listener checks the pointer against the
  // highlighted ranges' boxes (throttled to one check per frame).
  var TIER_ORDER = { clear: 0, likely: 1, possible: 2 };
  var TIP_CSS =
    ':host{all:initial;position:fixed;z-index:2147483646;pointer-events:none;' +
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}' +
    '.tip{box-sizing:border-box;max-width:300px;background:var(--surface);color:var(--text);border:1px solid var(--border);' +
    'border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.18);padding:8px 10px;font-size:12.5px;line-height:1.4}' +
    '.top{display:flex;gap:6px;align-items:baseline;margin-bottom:2px}' +
    '.tier{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}' +
    '.tier.clear{color:#D03B3B}.tier.likely{color:#D9692F}.tier.possible{color:#B08A17}' +
    '.tech{font-weight:700}.why{color:var(--text-muted)}';
  var tip = null, shown = null, frame = 0, px = 0, py = 0, listening = false;

  function listen() {
    if (listening) return;
    listening = true;
    document.addEventListener('mousemove', function (e) {
      px = e.clientX; py = e.clientY;
      if (!frame) frame = requestAnimationFrame(check);
    }, { passive: true, capture: true });
    window.addEventListener('scroll', hide, { passive: true, capture: true });
  }

  function hitAt(x, y) {
    var best = null;
    ranges.forEach(function (list) {
      list.forEach(function (r) {
        if (best && TIER_ORDER[best.r.tier] <= TIER_ORDER[r.tier]) return;
        var rects = r.range.getClientRects();
        for (var i = 0; i < rects.length; i++) {
          var b = rects[i];
          if (x >= b.left && x <= b.right && y >= b.top - 1 && y <= b.bottom + 1) { best = { r: r, rect: b }; return; }
        }
      });
    });
    return best;
  }

  function check() {
    frame = 0;
    // Only when the highlighted text itself is what the pointer is on: not through the analysis
    // window, our tooltip, or the site's own menus and dialogs lying over it.
    var top = document.elementFromPoint(px, py);
    if (!top || (top.closest && top.closest('ppg-popover, ppg-tip, ppg-badge, ppg-deep'))) { hide(); return; }
    var hit = hitAt(px, py);
    if (hit) {
      var holder = hit.r.range.commonAncestorContainer;
      if (holder.nodeType !== 1) holder = holder.parentNode;
      if (!(holder && (holder.contains(top) || top.contains(holder)))) hit = null;
    }
    if (!hit) { hide(); return; }
    if (shown !== hit.r) show(hit.r);
    place(hit.rect);
  }

  function show(r) {
    if (!tip) {
      tip = document.createElement('ppg-tip');
      tip.setAttribute('data-ppg', 'tip');
      tip.attachShadow({ mode: 'open' });
    }
    var root = tip.shadowRoot;
    while (root.firstChild) root.removeChild(root.firstChild);
    var st = document.createElement('style');
    st.textContent = PPG_TOKENS + TIP_CSS;
    root.appendChild(st);
    var box = document.createElement('div');
    box.className = 'tip';
    var top = document.createElement('div');
    top.className = 'top';
    var t = document.createElement('span');
    t.className = 'tier ' + r.tier;
    t.textContent = PPG_TIERS[r.tier] || r.tier;
    var n = document.createElement('span');
    n.className = 'tech';
    n.textContent = r.pattern.technique;
    top.appendChild(t);
    top.appendChild(n);
    box.appendChild(top);
    if (r.pattern.why) {
      var w = document.createElement('div');
      w.className = 'why';
      w.textContent = r.pattern.why;
      box.appendChild(w);
    }
    root.appendChild(box);
    tip.setAttribute('data-theme', PPG_theme);
    if (!tip.isConnected) document.documentElement.appendChild(tip);
    shown = r;
  }

  function place(rect) {
    var box = tip.shadowRoot.querySelector('.tip');
    var w = box.offsetWidth || 260, h = box.offsetHeight || 60;
    var left = Math.max(8, Math.min(rect.left, window.innerWidth - w - 8));
    var top = rect.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, rect.top - h - 6);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  function hide() {
    if (tip && tip.isConnected) tip.remove();
    shown = null;
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
  var bad = ((job.truth && job.truth.items) || []).filter(function (c) { return c.verdict === 'refuted' || c.verdict === 'misleading'; }).length;
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
    add('tx', n ? (bad ? 'Analysis · ' + bad + ' refuted/misleading' : 'Analysis') : (bad ? bad + ' refuted/misleading' : 'No patterns'));
    b.setAttribute('aria-label', 'In-depth analysis: ' + n + ' propaganda pattern' + (n === 1 ? '' : 's') +
      (bad ? ', ' + bad + ' refuted or misleading claim' + (bad === 1 ? '' : 's') : '') + '. Open details');
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
  '.vd.supported{color:#2F7D4F;border-color:#2F7D4F;background:rgba(47,125,79,.1)}' +
  '.vd.refuted{color:var(--v-false-text);border-color:var(--v-false);background:var(--v-false-soft)}' +
  '.vd.misleading{color:var(--v-mis-text);border-color:var(--v-mis);background:var(--v-mis-soft)}' +
  '.vd.unverified{color:var(--text-muted);border-color:var(--border)}' +
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
  var engine = s.engine || PPG_deepEngine;
  h.appendChild(ppgD('span', 'sub', PPG_DEEP_ENGINES[engine] || PPG_DEEP_ENGINES.local));
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
    // The cloud model asleep: its first answer waits for a GPU (the server says so in job.note).
    if (job.note) head.appendChild(ppgD('p', 'meta deep-note', job.note));
  }

  if (job.patterns) box.appendChild(patternsSection(job.patterns, PPG_highlight.missing(ctx.item.id)));
  if (job.truth) box.appendChild(truthSection(job.truth));
  if (job.trace) box.appendChild(traceSection(job.trace));
  pop.appendChild(box);
}

// Patterns: the summary, a count per tier and a hint; each phrase is explained by hovering it
// in the post. Only phrases that couldn't be highlighted (not on screen) are listed here.
function patternsSection(p, missing) {
  var sec = ppgD('section');
  sec.appendChild(ppgD('h3', null, 'Propaganda patterns'));
  var items = p.items || [];
  if (p.overall) sec.appendChild(ppgD('p', null, p.overall));
  if (!items.length) { sec.appendChild(ppgD('p', 'meta', 'No propaganda patterns found in the text.')); return sec; }
  var counts = ppgD('div', 'legend');
  ['clear', 'likely', 'possible'].forEach(function (t) {
    var n = items.filter(function (x) { return x.tier === t; }).length;
    if (!n) return;
    var x = ppgD('span');
    x.appendChild(ppgD('span', 'sw t-' + t));
    x.appendChild(document.createTextNode(n + ' ' + PPG_TIERS[t].toLowerCase()));
    counts.appendChild(x);
  });
  sec.appendChild(counts);
  var hidden = items.filter(function (x) { return missing.has(x.quote); });
  if (hidden.length < items.length) sec.appendChild(ppgD('p', 'meta', 'Hover the highlighted text in the post to see why each part is marked.'));
  if (hidden.length) {
    sec.appendChild(ppgD('p', 'meta', 'Not on screen (open “See more” to highlight them):'));
    hidden.forEach(function (x) {
      var row = ppgD('div', 'pat ' + x.tier);
      var top = ppgD('p');
      top.appendChild(ppgD('span', 'tier ' + x.tier, PPG_TIERS[x.tier] || x.tier));
      top.appendChild(ppgD('b', null, x.technique));
      top.appendChild(document.createTextNode(' — “' + ppgDSnip(x.quote, 80) + '”'));
      row.appendChild(top);
      if (x.why) row.appendChild(ppgD('p', 'meta', x.why));
      sec.appendChild(row);
    });
  }
  return sec;
}

// Fact check: the claims that sources could judge, each with its verdict and the sources it
// cites; the claims no source covered are summed up in one line.
function truthSection(t) {
  var sec = ppgD('section');
  sec.appendChild(ppgD('h3', null, 'Fact check'));
  var items = t.items || [];
  if (!items.length) { sec.appendChild(ppgD('p', 'meta', 'No checkable factual claims found.')); return sec; }
  var judged = items.filter(function (c) { return (c.sources && c.sources.length) || c.basis === 'general_knowledge'; });
  var open = items.filter(function (c) { return judged.indexOf(c) < 0; });
  judged.forEach(function (c) {
    var row = ppgD('div', 'claim');
    var top = ppgD('p');
    top.appendChild(ppgD('span', 'vd ' + c.verdict, PPG_VERDICTS[c.verdict] || c.verdict));
    top.appendChild(document.createTextNode(c.claim));
    row.appendChild(top);
    if (c.explanation) row.appendChild(ppgD('p', 'meta', c.explanation));
    var src = ppgD('p', 'meta srcs');
    if (c.sources && c.sources.length) {
      src.appendChild(document.createTextNode('Sources: '));
      c.sources.forEach(function (s, i) {
        if (i) src.appendChild(document.createTextNode(' · '));
        var label = s.source + (s.kind === 'disinfo' ? ' (documented disinformation)' : '') + (s.date ? ', ' + String(s.date).slice(0, 10) : '');
        var u = ppgDSafe(s.url);
        if (u) {
          var a = ppgD('a', null, label);
          a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer';
          a.title = s.title || '';
          src.appendChild(a);
        } else {
          src.appendChild(ppgD('span', null, label));
        }
      });
    } else {
      src.appendChild(document.createTextNode('General knowledge (no source covers it).'));
    }
    row.appendChild(src);
    sec.appendChild(row);
  });
  if (open.length) {
    var p = ppgD('p', 'meta');
    p.appendChild(ppgD('span', 'vd unverified', PPG_VERDICTS.unverified));
    p.appendChild(document.createTextNode('No trusted source found for ' +
      (open.length === 1 ? 'this claim: ' : 'these ' + open.length + ' claims: ') +
      open.map(function (c) { return ppgDSnip(c.claim, 70); }).join('; ')));
    sec.appendChild(p);
  }
  sec.appendChild(ppgD('p', 'meta basis', t.basis || 'Checked against trusted sources, never against the post itself.'));
  return sec;
}

// Where it came from: a compact timeline, oldest first.
function traceSection(tr) {
  var sec = ppgD('section');
  sec.appendChild(ppgD('h3', null, 'Where it came from'));
  if (tr.summary) sec.appendChild(ppgD('p', null, tr.summary));
  var nodes = tr.nodes || [];
  if (nodes.length <= 1) return sec;   // only this post: the summary says it all
  var ol = ppgD('ol', 'tl');
  nodes.forEach(function (n) {
    var li = ppgD('li', n.kind);
    var top = ppgD('p');
    top.appendChild(ppgD('b', null, n.title));
    top.appendChild(ppgD('span', 'when', '  ' + (n.time ? ppgDWhen(n.time) : (n.kind === 'this' ? '' : 'background'))));
    li.appendChild(top);
    var who = [n.who, n.where].filter(Boolean).join(' · ');
    if (typeof n.sim === 'number') who += ' · ' + Math.round(n.sim * 100) + '% similar';
    var meta = ppgD('p', 'meta', who);
    var u = ppgDSafe(n.url);
    if (u && n.kind !== 'this') {
      meta.appendChild(document.createTextNode(' · '));
      var a = ppgD('a', null, 'open');
      a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer';
      meta.appendChild(a);
    }
    li.appendChild(meta);
    if (n.text && n.kind !== 'this') li.appendChild(ppgD('p', 'q', '“' + ppgDSnip(n.text, 120) + '”'));
    ol.appendChild(li);
  });
  sec.appendChild(ol);
  return sec;
}
