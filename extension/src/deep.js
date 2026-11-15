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
/* global chrome, PPG_TOKENS, PPG_theme, PPG_loadFonts, PPG_fontsPending */
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
  // The dashboard's tiers (frontend .hl-clear/-likely/-possible): one colour, the trace red, with
  // the tier in the line itself: solid, dashed, dotted. A middle red that reads on X's white,
  // dim and black grounds alike.
  var CSS_TEXT =
    '::highlight(ppg-clear){background-color:rgba(222,64,38,.16);text-decoration:underline solid 2px rgba(222,64,38,.95);text-underline-offset:3px}' +
    '::highlight(ppg-likely){background-color:rgba(222,64,38,.12);text-decoration:underline dashed 1.5px rgba(222,64,38,.95);text-underline-offset:3px}' +
    '::highlight(ppg-possible){text-decoration:underline dotted 2px rgba(222,64,38,.95);text-underline-offset:3px}';

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
    ':host{position:fixed;z-index:2147483646;pointer-events:none}' +
    '.tip{box-sizing:border-box;max-width:300px;background:var(--card);color:var(--ink);border:1px solid var(--edge);' +
    'box-shadow:var(--lift);padding:8px 11px 9px;font-size:12.5px;line-height:1.45;font-stretch:94%}' +
    '.top{display:flex;gap:8px;align-items:baseline;margin-bottom:3px}' +
    '.tier{font-family:var(--font-data);font-size:9.5px;font-weight:500;text-transform:uppercase;letter-spacing:.1em;color:var(--red);' +
    'padding-bottom:1px;border-bottom:2px solid var(--red)}' +
    '.tier.likely{border-bottom-style:dashed}.tier.possible{border-bottom-style:dotted}' +
    '.tech{font-family:var(--font-display);font-weight:700}.why{color:var(--ink-2)}';
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
  // The dashboard's ghost button, at badge size: hairline, ink, square; ink fill on hover.
  'button{all:unset;box-sizing:border-box;cursor:pointer;display:inline-flex;align-items:center;gap:5px;height:20px;padding:1px 7px 0;' +
  'font-family:var(--font-display);font-size:10px;font-weight:700;font-stretch:96%;letter-spacing:.08em;text-transform:uppercase;' +
  'color:var(--ink-2);border:1px solid var(--rule);background:transparent}' +
  'button:hover{color:var(--card);background:var(--ink);border-color:var(--ink)}' +
  'button:focus-visible{outline:2px solid var(--red);outline-offset:2px}' +
  '.ic{font-size:12px;font-weight:800;letter-spacing:0}' +
  // Tier marks, as in the dashboard: one red, the tier in the line (solid, dashed, dotted).
  '.dot{width:8px;height:8px;display:inline-block;box-sizing:border-box}' +
  '.clear{background:var(--red)}.likely{background:var(--red-wash);border:1.5px dashed var(--red)}.possible{border:1.5px dotted var(--red)}' +
  'button:hover .likely,button:hover .possible{border-color:var(--card)}button:hover .clear{background:var(--card)}' +
  '.n{margin-right:2px;font-family:var(--font-data);font-weight:500;letter-spacing:0}' +
  'button.tiny .tx{display:none}button.tiny{padding:1px 6px 0}' +
  '.spin{width:10px;height:10px;border-radius:50%;border:2px solid var(--rule);border-top-color:var(--red);box-sizing:border-box;animation:r .8s linear infinite}' +
  '@keyframes r{to{transform:rotate(360deg)}}@media (prefers-reduced-motion:reduce){.spin{animation:none}}';

function PPG_DeepButton(onClick) {
  if (typeof PPG_loadFonts === 'function') PPG_loadFonts();
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
  if (typeof PPG_fontsPending !== 'undefined' && PPG_fontsPending && !this._refit) {
    this._refit = true;
    PPG_fontsPending.then(function () { self._refit = false; self.fit(); });
  }
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
  '.deep{display:flex;flex-direction:column;gap:12px}' +
  '.deep section{display:flex;flex-direction:column;gap:5px}' +
  '.deep h3{margin:0;font-family:var(--font-display);font-size:9.5px;font-weight:650;font-stretch:118%;letter-spacing:.2em;' +
  'text-transform:uppercase;color:var(--ink-3);line-height:1.3}' +
  // The head: the dashboard's rule-row, a label sunk into a hairline.
  '.deep .dh h3{display:flex;align-items:center;gap:10px;color:var(--ink-2)}' +
  '.deep .dh h3::after{content:"";flex:1;height:1px;background:var(--rule)}' +
  '.deep h3 .sub{font-family:var(--font-data);font-size:10px;font-weight:400;font-stretch:100%;text-transform:none;letter-spacing:0;color:var(--ink-3)}' +
  '.go-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}' +
  '.engine{font-family:var(--font-data);font-size:10.5px;color:var(--ink-3)}' +
  '.legend{display:flex;gap:12px;flex-wrap:wrap;font-family:var(--font-data);font-size:11px;color:var(--ink-2)}' +
  '.sw{display:inline-block;width:14px;height:8px;margin-right:5px;vertical-align:0;border-bottom:2px solid var(--red)}' +
  '.t-clear{background:var(--red-wash)}.t-likely{background:var(--red-wash);border-bottom-style:dashed}.t-possible{border-bottom-style:dotted}' +
  '.pat{margin:2px 0;padding-left:9px;border-left:2px solid var(--red)}' +
  '.pat.likely{border-left-style:dashed}.pat.possible{border-left-style:dotted}' +
  '.tier{font-family:var(--font-data);font-size:9.5px;text-transform:uppercase;letter-spacing:.1em;margin-right:6px;color:var(--red)}' +
  '.claim{display:flex;flex-direction:column;gap:2px;margin:3px 0}' +
  // Fact-check verdicts: the dashboard's stamps, in the verdict scale.
  '.vd{display:inline-block;font-family:var(--font-display);font-size:9.5px;font-weight:800;font-stretch:104%;letter-spacing:.12em;' +
  'text-transform:uppercase;padding:2px 6px 1px;margin-right:7px;border:1.5px solid currentColor;vertical-align:1px;line-height:1.3}' +
  '.vd.supported{color:var(--verdict-credible)}.vd.refuted{color:var(--verdict-false)}' +
  '.vd.misleading{color:var(--verdict-misleading)}.vd.unverified{color:var(--verdict-unverified)}' +
  '.basis{font-style:italic}' +
  // Where it came from: the thread. Trace nodes in red, this post in ink.
  'ol.tl{list-style:none;margin:2px 0 0;padding:0}' +
  'ol.tl li{position:relative;padding:0 0 10px 18px;border-left:1.5px solid var(--red);margin-left:5px}' +
  'ol.tl li:last-child{border-left-color:transparent;padding-bottom:0}' +
  'ol.tl li::before{content:"";position:absolute;left:-6px;top:4px;width:10px;height:10px;border-radius:50%;background:var(--card);border:2px solid var(--ink-3);box-sizing:border-box}' +
  'ol.tl li.telegram::before,ol.tl li.state_media::before,ol.tl li.narrative::before{border-color:var(--red)}' +
  'ol.tl li:first-child::before{background:var(--red);border-color:var(--red)}' +
  'ol.tl li.this::before{background:var(--ink);border-color:var(--ink)}' +
  'ol.tl b{font-family:var(--font-display);font-stretch:88%}' +
  '.when{font-family:var(--font-data);font-size:10.5px;color:var(--ink-3)}' +
  '.steps{margin:0;padding-left:0;list-style:none;font-family:var(--font-data);font-size:11.5px}' +
  '.steps li{margin:2px 0;color:var(--ink-3)}.steps li.now{color:var(--ink);font-weight:500}' +
  '.err{color:var(--red)}' +
  '.deep-go{align-self:flex-start}';

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

  var engine = s.engine || PPG_deepEngine;
  var engineName = PPG_DEEP_ENGINES[engine] || PPG_DEEP_ENGINES.local;

  if (s.status === 'idle' || s.status === 'error') {
    // Just the button (and where it runs). Not "primary": tests find the feedback link by .wrong.
    var row = ppgD('div', 'go-row');
    var go = ppgD('button', 'btn deep-go', s.status === 'error' ? 'Try again' : 'Analyze in depth');
    go.type = 'button';
    go.setAttribute('aria-label', (s.status === 'error' ? 'Try the in-depth analysis again' : 'Analyze in depth') +
      ': propaganda patterns, fact check and where it came from. Runs on ' + engineName);
    go.addEventListener('click', function () { if (ctx.deepStart) ctx.deepStart(); });
    row.appendChild(go);
    row.appendChild(ppgD('span', 'engine', engineName));
    if (s.status === 'error') box.appendChild(ppgD('p', 'err', s.error || 'The analysis failed.'));
    box.appendChild(row);
    pop.appendChild(box);
    return;
  }

  var head = ppgD('section', 'dh');
  var h = ppgD('h3', null, 'In-depth analysis');
  h.appendChild(ppgD('span', 'sub', engineName));
  head.appendChild(h);
  box.appendChild(head);

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
