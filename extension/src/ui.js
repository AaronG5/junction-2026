// Badge and popover. Both live in Shadow DOM so X's CSS cannot
// reach them, and both are built with textContent only (no innerHTML from
// server data). Colours, type and edges are the dashboard's "RED THREAD" system
// (frontend/src/styles/tokens.css), with the same strict roles:
//   PAPER/CARD  the ground          INK (3 tones)  everything you read
//   RED         the trace: the Telegram source post, the thread, the action that starts one.
//               Never a verdict.
//   VERDICT     a muted scale, only ever inside a badge ("Potentially propaganda" = --verdict-false)
//   Coordination is an account-level signal, neither a verdict nor a trace: it is drawn in ink.
// Square corners, hairline rules, one hard-offset shadow. Light and dark ("darkroom") follow the
// page the badge sits on (content.js reads its background), set as data-theme on each host.
/* global chrome, PPG_viewModel, PPG_ariaLabel, PPG_LABELS, PPG_deepSection, PPG_Deep */
/* exported PPG_Badge, PPG_Popover, PPG_setTheme, PPG_TOKENS, PPG_loadFonts */

var PPG_TOKENS =
  ':host{all:initial;' +
  '--paper:#EDE3D1;--paper-deep:#E2D5BD;--card:#F7F1E4;' +
  '--ink:#17120E;--ink-2:#594A3A;--ink-3:#73624E;--ink-4:#A1907A;--rule:#C9B698;--rule-soft:#DBCDB2;--edge:#17120E;' +
  '--red:#BE2E16;--red-wash:#F2D9CC;--on-red:#FFF6EC;' +
  '--verdict-false:#7C1D12;--verdict-misleading:#7A5207;--verdict-unverified:#5C5248;--verdict-credible:#3A5C22;--on-verdict:#FFF6EC;' +
  '--lift:3px 3px 0 rgba(23,18,14,.16);' +
  '--font-display:"PPG Archivo","Archivo Variable","Archivo","Helvetica Neue",Arial,sans-serif;' +
  '--font-ui:"PPG Archivo","Archivo Variable","Archivo","Segoe UI",system-ui,sans-serif;' +
  '--font-data:"PPG Plex Mono","IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;' +
  // legacy names, for anything not yet redrawn
  '--bg:var(--paper);--surface:var(--card);--border:var(--rule);--text:var(--ink);--text-muted:var(--ink-2);--accent:var(--red);' +
  'font-family:var(--font-ui);font-synthesis-weight:none;-webkit-font-smoothing:antialiased;}' +
  ':host([data-theme="dark"]){' +
  '--paper:#15110D;--paper-deep:#0D0A07;--card:#1F1912;' +
  '--ink:#F2E9DA;--ink-2:#B8A68F;--ink-3:#8C7B66;--ink-4:#6B5C4A;--rule:#3B3024;--rule-soft:#2A2219;--edge:#4E3F2E;' +
  '--red:#FF6A4A;--red-wash:#331810;--on-red:#1B0C06;' +
  '--verdict-false:#E08270;--verdict-misleading:#CCA253;--verdict-unverified:#9C8F7E;--verdict-credible:#8FAE67;--on-verdict:#1B0C06;' +
  '--lift:3px 3px 0 rgba(0,0,0,.55);color-scheme:dark;}';

var PPG_ICON = { source: '⧉', ppg: '!', coord: '∷', unavailable: '–' }; // ⧉ ! ∷ –

var PPG_theme = 'light';
function PPG_setTheme(t) { PPG_theme = t === 'dark' ? 'dark' : 'light'; }

// Fonts: @font-face is ignored inside a shadow root, and the page's CSP may block font URLs, so
// the bundled woff2 files (fonts/, OFL) are read by the content script and handed to the
// document as FontFace objects with the bytes. Unique family names, so the page is not affected.
// Until they load (or if they can't), the stacks above fall back to close system fonts.
var PPG_FONTS = [
  ['PPG Archivo', 'fonts/archivo-latin-wdth.woff2', { weight: '100 900', stretch: '62% 125%',
    unicodeRange: 'U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD' }],
  ['PPG Archivo', 'fonts/archivo-latin-ext-wdth.woff2', { weight: '100 900', stretch: '62% 125%',
    unicodeRange: 'U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF' }],
  ['PPG Plex Mono', 'fonts/plex-mono-latin-400.woff2', { weight: '400' }],
  ['PPG Plex Mono', 'fonts/plex-mono-latin-500.woff2', { weight: '500' }],
];
var PPG_fontsAsked = false;
// While the fonts are on their way: a promise the badges re-measure on (the text gets narrower or
// wider when the real face arrives, see PPG_Badge.fit). null once they are in (or failed).
var PPG_fontsPending = null;
function PPG_loadFonts() {
  if (PPG_fontsAsked) return;
  PPG_fontsAsked = true;
  try {
    if (typeof FontFace !== 'function' || !document.fonts || typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.getURL) return;
    PPG_fontsPending = Promise.all(PPG_FONTS.map(function (f) {
      return fetch(chrome.runtime.getURL(f[1])).then(function (r) { return r.arrayBuffer(); }).then(function (buf) {
        var face = new FontFace(f[0], buf, f[2]);
        return face.load().then(function () { document.fonts.add(face); });
      }).catch(function () { /* system fallback */ });
    })).then(function () { PPG_fontsPending = null; });
  } catch (e) { PPG_fontsPending = null; }
}

// ---------------------------------------------------------------- badge
var BADGE_CSS = PPG_TOKENS +
  // Fixed height and nowrap: the badge can grow sideways inside the header
  // row but never changes the row's height, so X's layout does not move.
  ':host{display:inline-flex;align-items:center;height:20px;max-height:20px;margin-left:6px;vertical-align:middle;' +
  'overflow:visible;white-space:nowrap;line-height:1;contain:layout style;}' +
  '.wrap{display:inline-flex;gap:4px;align-items:center;height:20px}' +
  // A struck stamp, as on the dashboard: square, 1.5px border in the text's own colour.
  'button{all:unset;box-sizing:border-box;cursor:pointer;display:inline-flex;align-items:center;gap:5px;height:20px;' +
  'padding:1px 7px 0 6px;border:1.5px solid currentColor;background:transparent;' +
  'font-family:var(--font-display);font-size:10px;font-weight:800;font-stretch:90%;letter-spacing:.07em;text-transform:uppercase}' +
  'button:hover{background:var(--card)}' +
  'button:focus-visible{outline:2px solid var(--red);outline-offset:2px}' +
  '.ic{font-family:var(--font-data);font-size:11px;font-weight:500;letter-spacing:0;width:9px;text-align:center;margin-top:-1px}' +
  '.ppg{color:var(--verdict-false)}' +
  // Copies a Telegram post: the trace, in red. When the same chip also carries the verdict text,
  // the verdict keeps the verdict colour and only the copy mark stays red.
  '.source{color:var(--red)}' +
  '.source.v{color:var(--verdict-false)}.source.v .ic{color:var(--red);opacity:1}' +
  '.coord{color:var(--ink)}' +
  '.unavailable{color:var(--ink-3);border-color:var(--rule);font-weight:650}' +
  '.dot{padding:0;width:14px;justify-content:center;border:0}' +
  '.dot i{width:6px;height:6px;border:1.5px solid var(--verdict-unverified);opacity:.7;box-sizing:border-box}' +
  '.spin{width:12px;height:12px;border-radius:50%;border:2px solid var(--rule);border-top-color:var(--red);' +
  'box-sizing:border-box;animation:r 0.8s linear infinite}' +
  '.check{padding:0 4px}' +
  '@keyframes r{to{transform:rotate(360deg)}}' +
  '@media (prefers-reduced-motion:reduce){.spin{animation:none;opacity:.6}}' +
  '.quoted{border-style:dashed}' +
  '.sh{display:none}.fit1 .tx{display:none}.fit1 .sh{display:inline}' +
  '.fit2 button:not(:first-child) .sh,.fit3 button .sh{display:none}' +
  '.fit2 button:not(:first-child),.fit3 button{padding:1px 5px 0}';

function PPG_Badge(onOpen) {
  PPG_loadFonts();
  this.host = document.createElement('ppg-badge');
  this.host.setAttribute('data-ppg', 'badge');
  this.root = this.host.attachShadow({ mode: 'open' });
  var st = document.createElement('style');
  st.textContent = BADGE_CSS;
  this.root.appendChild(st);
  this.wrap = document.createElement('span');
  this.wrap.className = 'wrap';
  this.root.appendChild(this.wrap);
  this.onOpen = onOpen;
  this.key = '';
  var self = this;
  // Stop clicks from reaching X's tweet click handler (which opens the post).
  ['click', 'mousedown', 'mouseup', 'pointerdown', 'keydown'].forEach(function (ev) {
    self.host.addEventListener(ev, function (e) {
      if (ev === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
      e.stopPropagation();
    });
  });
}

/** Render; cheap no-op if nothing changed. */
PPG_Badge.prototype.render = function (vm, showAll) {
  this.host.setAttribute('data-theme', PPG_theme);
  var key = PPG_theme + '|' + vm.state + '|' + vm.pending + '|' + showAll + '|' + vm.chips.map(function (c) { return c.kind + c.text; }).join(',');
  if (key === this.key) return;
  this.key = key;
  this.vm = vm;
  var w = this.wrap, self = this;
  while (w.firstChild) w.removeChild(w.firstChild);
  this.host.setAttribute('data-state', vm.state);

  function btn(cls, label) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = cls;
    b.setAttribute('aria-label', label);
    b.setAttribute('aria-haspopup', 'dialog');
    b.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); self.onOpen(b); });
    w.appendChild(b);
    return b;
  }

  if (vm.state === 'checking') {
    var s = document.createElement('span');
    s.className = 'spin';
    s.title = PPG_LABELS.checking;
    s.setAttribute('aria-hidden', 'true'); // dozens of these at once would flood a screen reader
    w.appendChild(s);
  } else if (vm.state === 'clean') {
    if (showAll) { var d = btn('dot', PPG_ariaLabel(vm)); d.title = PPG_LABELS.clean; d.appendChild(document.createElement('i')); }
  } else if (vm.state === 'unavailable') {
    var u = btn('unavailable', PPG_LABELS.unavailable + '. Open details');
    u.title = 'The checking service did not answer';
    addText(u, PPG_ICON.unavailable, PPG_LABELS.unavailable);
  } else {
    vm.chips.forEach(function (c) {
      var b = btn(c.kind + (c.verdict ? ' v' : '') + (c.quoted ? ' quoted' : ''), (c.aria || c.text) + (vm.pending ? '; still checking' : '') + '. Open details');
      b.title = c.aria || c.text;
      addText(b, PPG_ICON[c.kind], c.text, c.short);
    });
    if (vm.pending) {
      var sp = document.createElement('span');
      sp.className = 'spin';
      sp.title = 'Still checking the text';
      sp.setAttribute('aria-hidden', 'true'); // the chip's aria-label already says "still checking"
      w.appendChild(sp);
    }
  }
  // Nothing visible -> zero width, no margin.
  this.host.style.marginLeft = w.childNodes.length ? '' : '0';
  if (vm.state === 'flagged') this.fit();
};

// If the chips would run past the post's right edge (long names, narrow
// column), step down: short labels, then icon-only for all but the first
// chip, then icon-only. aria-label and title always keep the full words.
var FIT_LEVELS = ['', 'fit1', 'fit1 fit2', 'fit1 fit2 fit3'];
PPG_Badge.prototype.fit = function () {
  var self = this;
  if (PPG_fontsPending && !this._refit) {
    this._refit = true;
    PPG_fontsPending.then(function () { self._refit = false; if (self.vm && self.vm.state === 'flagged') self.fit(); });
  }
  requestAnimationFrame(function () {
    var art = self.host.closest && self.host.closest('article');
    if (!art || !self.host.isConnected) return;
    var limit = art.getBoundingClientRect().right - 8;
    for (var i = 0; i < FIT_LEVELS.length; i++) {
      self.wrap.className = 'wrap ' + FIT_LEVELS[i];
      if (self.host.getBoundingClientRect().right <= limit) return;
    }
  });
};

function addText(b, icon, text, short) {
  var i = document.createElement('span');
  i.className = 'ic';
  i.setAttribute('aria-hidden', 'true');
  i.textContent = icon;
  var t = document.createElement('span');
  t.className = 'tx';
  t.textContent = text;
  b.appendChild(i);
  b.appendChild(t);
  var sh = document.createElement('span');
  sh.className = 'sh';
  sh.textContent = short || text;
  b.appendChild(sh);
}

// ---------------------------------------------------------------- popover
var POP_CSS = PPG_TOKENS +
  ':host{position:fixed;inset:0 auto auto 0;z-index:2147483646;}' +
  // The sheet: card ground, the hard edge, one hard-offset shadow, square corners.
  '.pop{position:fixed;box-sizing:border-box;width:360px;max-width:calc(100vw - 16px);max-height:min(560px,calc(100vh - 16px));' +
  'overflow:auto;background:var(--card);color:var(--ink);border:1px solid var(--edge);border-radius:0;' +
  'box-shadow:var(--lift);padding:16px 18px 12px;font-size:13.5px;line-height:1.5;font-stretch:94%;' +
  'display:flex;flex-direction:column;gap:12px}' +
  '.pop:focus{outline:none}' +
  '.pop > *{flex:none}' +
  'p{margin:0}' +
  // The verdict line: the dashboard's big pill. Ink words, the verdict in its round mark.
  'h2{margin:0 30px 0 0;align-self:flex-start;display:inline-flex;align-items:center;gap:8px;padding:5px 12px 4px 5px;' +
  'border:1px solid var(--rule);background:var(--card);font-family:var(--font-display);font-size:13px;font-weight:800;' +
  'font-stretch:94%;letter-spacing:.06em;text-transform:uppercase;line-height:1.25;color:var(--ink)}' +
  '.vdot{width:20px;height:20px;border-radius:50%;flex:none;display:inline-flex;align-items:center;justify-content:center;' +
  'font-family:var(--font-data);font-size:11px;font-weight:500;letter-spacing:0;color:var(--on-verdict);background:var(--verdict-unverified)}' +
  'h2[data-tone="false"] .vdot{background:var(--verdict-false)}' +
  'h2[data-tone="trace"] .vdot{background:var(--red);color:var(--on-red)}' +
  'h2[data-tone="coord"] .vdot{background:var(--ink);color:var(--card)}' +
  'h2[data-tone="none"]{border:0;padding:0;letter-spacing:.04em}' +
  '.reason{font-size:14px;line-height:1.45;color:var(--ink)}' +
  '.meta{color:var(--ink-2);font-size:12px;line-height:1.45}' +
  // The compact two-column list.
  'dl.rows{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:7px 14px;margin:0;align-items:baseline}' +
  'dt,.lbl{font-family:var(--font-display);font-size:9.5px;font-weight:650;font-stretch:112%;letter-spacing:.14em;' +
  'text-transform:uppercase;color:var(--ink-3);line-height:1.3;white-space:nowrap}' +
  'dd{margin:0;font-size:13px;line-height:1.45;overflow-wrap:anywhere}' +
  'dd.claim{font-weight:600}' +
  '.mono{font-family:var(--font-data);font-size:12px;letter-spacing:.01em}' +
  '.num{font-variant-numeric:tabular-nums}' +
  'a{color:var(--red);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}' +
  'a:hover{color:var(--ink)}' +
  'a:focus-visible,button:focus-visible,summary:focus-visible{outline:2px solid var(--red);outline-offset:2px}' +
  // "Techniques and the copied post", collapsed.
  'details.more{font-size:12.5px}' +
  'details.more > summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:7px;' +
  'font-family:var(--font-display);font-size:10px;font-weight:700;font-stretch:112%;letter-spacing:.12em;text-transform:uppercase;color:var(--red)}' +
  'details.more > summary::-webkit-details-marker{display:none}' +
  'details.more > summary::before{content:"▸";font-size:11px;width:8px;letter-spacing:0}' +
  'details.more[open] > summary::before{content:"▾"}' +
  '.more-body{display:flex;flex-direction:column;gap:8px;margin-top:10px}' +
  '.tags{display:flex;flex-direction:column;gap:6px}' +
  '.tag{display:inline-block;font-family:var(--font-data);font-size:10px;letter-spacing:.08em;text-transform:uppercase;' +
  'color:var(--ink-2);border:1px solid var(--rule);padding:1px 6px;margin-right:6px;line-height:1.5}' +
  '.q{color:var(--ink-2);font-style:italic}' +
  'blockquote{margin:0;padding:9px 11px;background:var(--paper-deep);border-left:2px solid var(--red);' +
  'font-size:12.5px;color:var(--ink-2);overflow-wrap:anywhere;line-height:1.45}' +
  '.close{all:unset;position:absolute;top:12px;right:12px;width:26px;height:26px;cursor:pointer;' +
  'display:flex;align-items:center;justify-content:center;color:var(--ink-2);font-size:19px;line-height:1}' +
  '.close:hover{background:var(--paper-deep);color:var(--ink)}' +
  '.close:focus-visible{outline:2px solid var(--red);outline-offset:0}' +
  // Buttons: the dashboard's .btn (red: the one action that starts a trace).
  '.btn{all:unset;box-sizing:border-box;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;gap:8px;' +
  'min-height:30px;padding:1px 14px 0;border:1.5px solid var(--red);background:var(--red);color:var(--on-red);' +
  'font-family:var(--font-display);font-size:10.5px;font-weight:700;font-stretch:108%;letter-spacing:.13em;text-transform:uppercase}' +
  '.btn:hover{background:var(--ink);border-color:var(--ink);color:var(--card)}' +
  // The quiet footer: one tiny line, and "Wrong?".
  'footer{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-top:2px;font-size:10.5px;line-height:1.4;color:var(--ink-3)}' +
  '.wrong{all:unset;cursor:pointer;flex:none;font-size:12px;color:var(--ink-2);text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:3px}' +
  '.wrong:hover{color:var(--ink)}' +
  '.thanks{flex:none;font-size:12px;color:var(--ink-2)}';

function PPG_Popover(sendFeedback) {
  this.sendFeedback = sendFeedback;
  this.host = null;
  this.returnFocus = null;
  this._onDocDown = this._onDocDown.bind(this);
  this._onKey = this._onKey.bind(this);
  this._onScroll = this._onScroll.bind(this);
}

PPG_Popover.prototype.isOpen = function () { return !!(this.host && this.host.isConnected); };

/**
 * @param {Element} anchorBtn   element to position against and return focus to
 * @param {object} ctx          {item, result, quoted, quotedResult, vm}
 */
PPG_Popover.prototype.open = function (anchorBtn, ctx) {
  PPG_loadFonts();
  this.close(false);
  this._place = null;   // placed beside this post (see _position)
  this.returnFocus = anchorBtn;
  this.ctx = ctx;
  var host = document.createElement('ppg-popover');
  host.setAttribute('data-ppg', 'popover');
  host.setAttribute('data-theme', PPG_theme);
  var root = host.attachShadow({ mode: 'open' });
  var st = document.createElement('style');
  st.textContent = POP_CSS;
  root.appendChild(st);
  var pop = el('div', 'pop');
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-modal', 'true');
  pop.setAttribute('aria-labelledby', 'ppg-h');
  pop.tabIndex = -1;
  root.appendChild(pop);
  this.root = root;
  this.pop = pop;
  this.host = host;
  this._fill(pop, ctx);
  // Stop page handlers (X's keyboard shortcuts, tweet click) from seeing our events.
  ['click', 'mousedown', 'pointerdown', 'keydown', 'keyup', 'keypress', 'wheel'].forEach(function (ev) {
    host.addEventListener(ev, function (e) { e.stopPropagation(); });
  });
  pop.addEventListener('keydown', this._onKey);
  document.documentElement.appendChild(host);
  this._position();
  document.addEventListener('mousedown', this._onDocDown, true);
  document.addEventListener('keydown', this._onKey, true);
  window.addEventListener('scroll', this._onScroll, { passive: true, capture: true });
  window.addEventListener('resize', this._onScroll, { passive: true });
  var first = this._focusables()[0];
  (first || pop).focus();
};

PPG_Popover.prototype.close = function (restore) {
  if (!this.host) return;
  document.removeEventListener('mousedown', this._onDocDown, true);
  document.removeEventListener('keydown', this._onKey, true);
  window.removeEventListener('scroll', this._onScroll, { capture: true });
  window.removeEventListener('resize', this._onScroll);
  this.host.remove();
  this.host = null;
  if (restore !== false && this.returnFocus && this.returnFocus.isConnected) {
    try { this.returnFocus.focus(); } catch (e) { /* ignore */ }
  }
};

/** Live update while open (e.g. the judge finished). */
PPG_Popover.prototype.update = function (ctx) {
  if (!this.isOpen()) return;
  var active = this.root.activeElement;
  var cls = active && active.className;
  this.ctx = ctx;
  while (this.pop.firstChild) this.pop.removeChild(this.pop.firstChild);
  this._fill(this.pop, ctx);
  var again = cls && this.pop.querySelector('.' + String(cls).split(' ')[0]);
  (again || this._focusables()[0] || this.pop).focus();
};

PPG_Popover.prototype._focusables = function () {
  return Array.prototype.slice.call(this.pop.querySelectorAll('button, a[href], summary'));
};

PPG_Popover.prototype._onKey = function (e) {
  if (!this.isOpen()) return;
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.close(); return; }
  if (e.key === 'Tab') {
    var f = this._focusables();
    if (!f.length) { e.preventDefault(); return; }
    var active = this.root.activeElement;
    var i = f.indexOf(active);
    if (e.shiftKey && (i <= 0)) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && (i === f.length - 1 || i === -1)) { e.preventDefault(); f[0].focus(); }
    e.stopPropagation();
  }
};

PPG_Popover.prototype._onDocDown = function (e) {
  if (!this.isOpen()) return;
  var path = e.composedPath ? e.composedPath() : [];
  if (path.indexOf(this.host) >= 0) return;
  if (this.returnFocus && path.indexOf(this.returnFocus) >= 0) return;
  this.close(false);
};

PPG_Popover.prototype._onScroll = function (e) {
  var self = this;
  if (e && e.type === 'resize') this._place = null;   // scrolling keeps the panel where it is
  if (this._raf) return;
  this._raf = requestAnimationFrame(function () { self._raf = 0; if (self.isOpen()) self._position(); });
};

// A fixed side panel beside the post: it opens on the side of the post with more room, just below
// the site's top bar, and stays put while the page scrolls, so it never covers the post (or the
// highlighted text in it). Placed once per opening, again when the window is resized. Only when
// neither side has room (a narrow window) is it docked at the right edge, over the page.
var PPG_PANEL_W = 360, PPG_PANEL_MIN_W = 280, PPG_PANEL_GAP = 12;

PPG_Popover.prototype._position = function () {
  var a = this.returnFocus;
  if (!a || !a.isConnected) { this.close(false); return; }
  if (!this._place) this._place = this._pickPlace(a);
  var p = this._place;
  this.pop.style.left = p.left + 'px';
  this.pop.style.top = p.top + 'px';
  this.pop.style.width = p.width + 'px';
  this.pop.style.maxHeight = Math.max(160, window.innerHeight - p.top - 12) + 'px';
};

PPG_Popover.prototype._pickPlace = function (anchor) {
  var vw = window.innerWidth;
  var host = anchor.getRootNode && anchor.getRootNode().host;   // the badge / Analyze button host
  var post = (host || anchor).closest('article, [aria-posinset], div[role="article"], div[role="feed"] > div');
  var r = (post || host || anchor).getBoundingClientRect();
  var right = vw - r.right - PPG_PANEL_GAP - 8, left = r.left - PPG_PANEL_GAP - 8;
  var top = 16;
  var bar = document.querySelector('[role="banner"]');               // Facebook's fixed top bar
  if (bar) { var b = bar.getBoundingClientRect(); if (b.top <= 0 && b.bottom > 0 && b.bottom < 160) top = b.bottom + 8; }
  var width = Math.min(PPG_PANEL_W, vw - 16);
  if (right >= width) return { left: r.right + PPG_PANEL_GAP, top: top, width: width };
  if (left >= width) return { left: r.left - PPG_PANEL_GAP - width, top: top, width: width };
  if (Math.max(right, left) >= PPG_PANEL_MIN_W) {                     // a bit narrower still fits beside it
    width = Math.max(right, left);
    return right >= left ? { left: r.right + PPG_PANEL_GAP, top: top, width: width } : { left: 8, top: top, width: width };
  }
  return { left: vw - width - 8, top: top, width: width };            // narrow window: docked right
};

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function safeUrl(u) {
  try {
    var x = new URL(u);
    return x.protocol === 'https:' ? x.href : null;
  } catch (e) { return null; }
}

function pct(sim) { return typeof sim === 'number' ? Math.round(sim * 100) + '%' : ''; }

var SOURCE_NAMES = { euvsdisinfo: 'EUvsDisinfo', rt: 'RT', sputnik: 'Sputnik', tass: 'TASS', ria: 'RIA Novosti' };
function srcName(s) { return SOURCE_NAMES[String(s || '').toLowerCase()] || String(s || 'unknown source'); }

var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function monYear(d) {
  var m = /^(\d{4})-(\d{2})/.exec(d || '');
  return m ? { y: m[1], m: MONTHS[+m[2] - 1] } : null;
}
function shortDate(d) {
  var t = Date.parse(d || '');
  if (isNaN(t)) return d || '';
  return new Date(t).toISOString().slice(0, 10);
}
function snippet(t, n) { t = String(t || '').replace(/\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n - 1) + '…' : t; }

// The verdict line: the judge's verdict if there is one, else the strongest other finding.
function verdictLine(vm, deepOn) {
  function find(kind, quoted) {
    for (var i = 0; i < vm.labels.length; i++) if (vm.labels[i].kind === kind && !!vm.labels[i].quoted === quoted) return vm.labels[i];
    return null;
  }
  if (vm.state === 'flagged') {
    var l;
    if ((l = find('ppg', false) || find('ppg', true))) return { tone: 'false', icon: PPG_ICON.ppg, text: l.text };
    if ((l = find('source', false) || find('source', true))) return { tone: 'trace', icon: PPG_ICON.source, text: l.text };
    if ((l = find('coord', false))) return { tone: 'coord', icon: PPG_ICON.coord, text: l.text };
  }
  if (vm.state === 'unavailable') return { tone: 'unverified', icon: PPG_ICON.unavailable, text: PPG_LABELS.unavailable };
  if (deepOn) return { tone: 'none', text: 'Analysis of this post' };
  if (vm.state === 'clean') return { tone: 'unverified', icon: '✓', text: PPG_LABELS.clean };
  return { tone: 'none', text: PPG_LABELS.checking };
}

// "6 accounts, same text": the size of the cluster and what they share, from the reasons.
function coordText(co) {
  var why = (co.reasons || []).join(' ');
  var n = co.n_similar_accounts;
  if (!n) return (co.reasons && co.reasons[0]) || PPG_LABELS.coord;
  var what = /translat/i.test(why) ? ', translated copies' : /same (text|post)|cop(y|ies)/i.test(why) ? ', same text' : '';
  return n + ' account' + (n === 1 ? '' : 's') + what;
}

var PPG_DISCLAIMER = 'Automated assessment, may be wrong. Not a claim about who is behind this account.';

PPG_Popover.prototype._fill = function (pop, ctx) {
  var self = this;
  var close = el('button', 'close', '×');
  close.type = 'button';
  close.setAttribute('aria-label', 'Close');
  close.addEventListener('click', function () { self.close(); });

  var vm = ctx.vm;
  // Opened for the in-depth analysis (src/deep.js) on a post the automatic check didn't flag:
  // "Nothing found" as the heading would contradict what the analysis finds below.
  var deepOn = typeof PPG_Deep !== 'undefined' && ctx.item && PPG_Deep.state(ctx.item.id).status !== 'idle';
  var r = ctx.result || {};
  var q = ctx.quotedResult && vm.chips.some(function (c) { return c.quoted; }) ? ctx.quotedResult : null;

  // 1. The verdict line.
  var v = verdictLine(vm, deepOn);
  var h = el('h2');
  h.id = 'ppg-h';
  h.setAttribute('data-tone', v.tone);
  if (v.icon) { var d = el('span', 'vdot', v.icon); d.setAttribute('aria-hidden', 'true'); h.appendChild(d); }
  h.appendChild(document.createTextNode(v.text));
  pop.appendChild(h);
  pop.appendChild(close);

  // 2. One sentence: the judge's reason.
  var rc = r.content || {}, qc = (q && q.content) || {};
  if (rc.reason) pop.appendChild(el('p', 'reason', rc.reason));
  if (qc.reason) pop.appendChild(el('p', 'reason', PPG_LABELS.quoted + qc.reason));

  if (vm.pending) pop.appendChild(el('p', 'meta', 'Text check still running…'));
  if (vm.state === 'unavailable') pop.appendChild(el('p', 'meta', 'The checking service did not answer in time. Nothing is known about this post.'));
  if (deepOn && vm.state === 'clean') pop.appendChild(el('p', 'meta', 'The automatic check found no known narrative, source post or coordination.'));
  if (deepOn && vm.state === 'checking') pop.appendChild(el('p', 'meta', 'The automatic check is still running.'));
  if (r.status === 'timeout') pop.appendChild(el('p', 'meta', 'The full text check did not finish within 60 s.'));

  // 3. The compact list: Narrative, Source, State media, Coordinated.
  var dl = el('dl', 'rows');
  this._rows(dl, r, '');
  if (q) this._rows(dl, q, 'Quoted ');
  var co = r.coordination;
  if (co && co.label === 'coordinated') {
    var cd = row(dl, 'Coordinated');
    cd.textContent = coordText(co);
    if (co.reasons && co.reasons.length) cd.title = co.reasons.join('; ');
  }
  if (dl.childNodes.length) pop.appendChild(dl);

  // 4. Collapsed: the technique tags with their quotes, and the copied Telegram post.
  var techs = [], copies = [];
  [[r, ''], [q, PPG_LABELS.quoted]].forEach(function (x) {
    if (!x[0]) return;
    var c = x[0].content || {}, sm = x[0].source_match;
    (c.techniques || []).slice(0, 6).forEach(function (t) { techs.push({ t: t, pre: x[1] }); });
    if (sm && sm.strong && sm.telegram && sm.telegram.length && sm.telegram[0].text) copies.push({ t: sm.telegram[0], pre: x[1] });
  });
  if (techs.length || copies.length) {
    var det = el('details', 'more');
    det.appendChild(el('summary', null, techs.length && copies.length ? 'Techniques and the copied post' : techs.length ? 'Techniques' : 'The copied post'));
    var body = el('div', 'more-body');
    if (techs.length) {
      body.appendChild(el('div', 'lbl', 'Techniques'));
      var tags = el('div', 'tags');
      techs.forEach(function (x) {
        var line = el('div');
        line.appendChild(el('span', 'tag', x.pre + (x.t.name || x.t.code)));
        if (x.t.quote) line.appendChild(el('span', 'q', '“' + snippet(x.t.quote, 90) + '”'));
        tags.appendChild(line);
      });
      body.appendChild(tags);
    }
    copies.forEach(function (x) {
      body.appendChild(el('div', 'lbl', x.pre + 'Copied Telegram post'));
      body.appendChild(el('blockquote', null, snippet(x.t.text, 280)));
    });
    det.appendChild(body);
    pop.appendChild(det);
  }

  // 5. In-depth analysis on request (src/deep.js): patterns, fact check, where it came from.
  if (typeof PPG_deepSection === 'function') PPG_deepSection(pop, ctx);

  // 6. A quiet "Wrong?" (sends the same feedback as before) and the disclaimer in one tiny line.
  var f = el('footer');
  f.appendChild(el('span', 'disc', PPG_DISCLAIMER));
  var API_NAMES = { source: 'source_match', ppg: 'potentially_propaganda', coord: 'coordinated' };
  var verdict = vm.labels.map(function (c) { return (c.quoted ? 'quoted:' : '') + API_NAMES[c.kind]; }).join('+') || vm.state;
  var wrong = el('button', 'wrong', 'Wrong?');
  wrong.type = 'button';
  wrong.setAttribute('aria-label', 'Wrong? Report a mistake');
  wrong.title = 'Report a mistake. ' + PPG_DISCLAIMER;
  wrong.addEventListener('click', function () {
    self.sendFeedback({ tweet_id: ctx.item.id, verdict_shown: verdict, user_says: 'wrong' });
    var t = el('span', 'thanks', 'Thanks, your report was sent.');
    t.setAttribute('role', 'status');
    f.replaceChild(t, wrong);
    pop.focus();
  });
  f.appendChild(wrong);
  pop.appendChild(f);
};

function row(dl, label) {
  dl.appendChild(el('dt', null, label));
  var dd = el('dd');
  dl.appendChild(dd);
  return dd;
}

PPG_Popover.prototype._rows = function (dl, r, prefix) {
  if (!r) return;
  var c = r.content || {};
  var sm = r.source_match;
  var cap = function (s) { return prefix ? prefix + s.toLowerCase() : s; };

  if (c.narrative && c.narrative.text) {
    var n = row(dl, cap('Narrative'));
    n.className = 'claim';
    n.textContent = c.narrative.text;
  }

  if (sm && sm.strong && sm.telegram && sm.telegram.length) {
    var t = sm.telegram[0];
    var s = row(dl, cap('Source'));
    s.appendChild(el('span', 'mono', '@' + String(t.channel || '').replace(/^@/, '')));
    s.appendChild(document.createTextNode(' on Telegram'));
    var u = safeUrl(t.url);
    if (u) {
      s.appendChild(document.createTextNode(' · '));
      var a = el('a', null, 'open');
      a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.setAttribute('aria-label', 'Open the source post on Telegram');
      s.appendChild(a);
    }
    s.title = shortDate(t.date) + (typeof t.sim === 'number' ? ' · ' + pct(t.sim) + ' similar' : '');
  }

  if (c.state_media_echo && c.state_media_echo.length) {
    var aired = 0;
    c.state_media_echo.forEach(function (x) { if (x.n_aired > 0) aired += x.n_aired; });
    var k = aired > 0 ? aired : c.state_media_echo.length;
    var e = row(dl, cap('State media'));
    e.appendChild(el('span', 'num', String(k)));
    e.appendChild(document.createTextNode(k === 1 ? ' mention' : ' mentions'));
  }
};
