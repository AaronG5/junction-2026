// Badge and popover. Both live in Shadow DOM so X's CSS cannot
// reach them, and both are built with textContent only (no innerHTML from
// server data). Colors are the team's Baltic tokens (frontend/src/styles/tokens.css).
/* global PPG_viewModel, PPG_ariaLabel, PPG_LABELS, PPG_deepSection, PPG_Deep */
/* exported PPG_Badge, PPG_Popover, PPG_setTheme */

var PPG_TOKENS =
  ':host{all:initial;' +
  '--bg:#F6F6F4;--surface:#FFFFFF;--border:#E4E1DA;--text:#1A1814;--text-muted:#625D55;' +
  '--accent:#663B6A;--accent-hover:#522E55;--on-accent:#FFFFFF;--accent-soft:#F1E7F0;--accent-soft-text:#522E55;' +
  '--v-false:#D03B3B;--v-mis:#EC835A;--v-unv:#8A8F98;' +
  '--v-false-soft:#FBEBEB;--v-mis-soft:#FDF0EA;--v-false-text:#A12A2A;--v-mis-text:#9A4520;' +
  'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}' +
  ':host([data-theme="dark"]){' +
  '--bg:#121110;--surface:#1B1A18;--border:#312E29;--text:#EFECE6;--text-muted:#A39D92;' +
  '--accent:#D2ADD3;--accent-hover:#E2C8E2;--on-accent:#121110;--accent-soft:#2A1F2B;--accent-soft-text:#E2C8E2;' +
  '--v-false-soft:#3A1A1A;--v-mis-soft:#3A2418;--v-false-text:#F2A3A3;--v-mis-text:#F5B79C;color-scheme:dark;}';

var PPG_ICON = { source: '⧉', ppg: '!', coord: '∷', unavailable: '–' }; // ⧉ ! ∷ –

var PPG_theme = 'light';
function PPG_setTheme(t) { PPG_theme = t === 'dark' ? 'dark' : 'light'; }

// ---------------------------------------------------------------- badge
var BADGE_CSS = PPG_TOKENS +
  // Fixed height and nowrap: the badge can grow sideways inside the header
  // row but never changes the row's height, so X's layout does not move.
  ':host{display:inline-flex;align-items:center;height:20px;max-height:20px;margin-left:6px;vertical-align:middle;' +
  'overflow:visible;white-space:nowrap;line-height:1;contain:layout style;}' +
  '.wrap{display:inline-flex;gap:4px;align-items:center;height:20px}' +
  'button{all:unset;box-sizing:border-box;cursor:pointer;display:inline-flex;align-items:center;gap:4px;height:20px;' +
  'padding:0 7px;border-radius:10px;font-size:11.5px;font-weight:600;letter-spacing:.01em;border:1px solid transparent}' +
  'button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}' +
  '.ic{font-size:11px;font-weight:800;width:12px;text-align:center}' +
  '.source{background:var(--v-false-soft);color:var(--v-false-text);border-color:var(--v-false)}' +
  '.ppg{background:var(--v-mis-soft);color:var(--v-mis-text);border-color:var(--v-mis)}' +
  '.coord{background:var(--accent-soft);color:var(--accent-soft-text);border-color:var(--accent)}' +
  '.unavailable{background:transparent;color:var(--text-muted);border-color:var(--border);font-weight:500}' +
  '.dot{padding:0;width:14px;justify-content:center}' +
  '.dot i{width:6px;height:6px;border-radius:50%;background:var(--v-unv);opacity:.6}' +
  '.spin{width:12px;height:12px;border-radius:50%;border:2px solid var(--border);border-top-color:var(--text-muted);' +
  'box-sizing:border-box;animation:r 0.9s linear infinite}' +
  '.check{padding:0 4px}' +
  '@keyframes r{to{transform:rotate(360deg)}}' +
  '@media (prefers-reduced-motion:reduce){.spin{animation:none;opacity:.6}}' +
  '.quoted{font-weight:500;border-style:dashed}' +
  '.sh{display:none}.fit1 .tx{display:none}.fit1 .sh{display:inline}' +
  '.fit2 button:not(:first-child) .sh,.fit3 button .sh{display:none}' +
  '.fit2 button:not(:first-child),.fit3 button{padding:0 5px}';

function PPG_Badge(onOpen) {
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
      var b = btn(c.kind + (c.quoted ? ' quoted' : ''), (c.aria || c.text) + (vm.pending ? '; still checking' : '') + '. Open details');
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
  '.pop{position:fixed;box-sizing:border-box;width:360px;max-width:calc(100vw - 16px);max-height:min(560px,calc(100vh - 16px));' +
  'overflow:auto;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:12px;' +
  'box-shadow:0 8px 28px rgba(0,0,0,.18);padding:14px 16px 12px;font-size:13px;line-height:1.45}' +
  '.pop:focus{outline:none}' +
  'h2{margin:0 26px 8px 0;font-size:15px;line-height:1.3;font-weight:700}' +
  '.labels{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}' +
  '.lab{display:inline-flex;gap:4px;align-items:center;border-radius:10px;padding:2px 8px;font-size:12px;font-weight:600;border:1px solid}' +
  '.lab.source{background:var(--v-false-soft);color:var(--v-false-text);border-color:var(--v-false)}' +
  '.lab.ppg{background:var(--v-mis-soft);color:var(--v-mis-text);border-color:var(--v-mis)}' +
  '.lab.coord{background:var(--accent-soft);color:var(--accent-soft-text);border-color:var(--accent)}' +
  '.lab.muted{color:var(--text-muted);border-color:var(--border)}' +
  'section{border-top:1px solid var(--border);padding:8px 0}' +
  'h3{margin:0 0 4px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-muted);font-weight:600}' +
  'p{margin:0 0 4px}' +
  '.meta{color:var(--text-muted);font-size:12px}' +
  'blockquote{margin:4px 0;padding:6px 8px;border-left:3px solid var(--border);background:var(--bg);border-radius:4px;' +
  'font-size:12.5px;color:var(--text);overflow-wrap:anywhere}' +
  'a{color:var(--accent);text-decoration:underline}' +
  'a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}' +
  '.tags{display:flex;flex-direction:column;gap:4px}' +
  '.tag{display:inline-block;background:var(--accent-soft);color:var(--accent-soft-text);border-radius:6px;padding:1px 6px;' +
  'font-size:11.5px;font-weight:600;margin-right:6px}' +
  '.q{color:var(--text-muted);font-style:italic}' +
  'ul{margin:0;padding-left:18px}' +
  '.close{all:unset;position:absolute;top:10px;right:10px;width:24px;height:24px;border-radius:6px;cursor:pointer;' +
  'display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:18px}' +
  '.close:hover{background:var(--bg)}' +
  'footer{border-top:1px solid var(--border);padding-top:8px;margin-top:2px;font-size:11.5px;color:var(--text-muted)}' +
  '.fb{display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap}' +
  '.btn{all:unset;cursor:pointer;font-size:12px;font-weight:600;padding:5px 10px;border-radius:8px}' +
  '.primary{background:var(--accent);color:var(--on-accent)}.primary:hover{background:var(--accent-hover)}' +
  '.ghost{color:var(--accent);border:1px solid var(--border)}' +
  '.thanks{font-size:12px;color:var(--text-muted)}';

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
  return Array.prototype.slice.call(this.pop.querySelectorAll('button, a[href]'));
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
function rangeText(first, last) {
  var a = monYear(first), b = monYear(last);
  if (!a || !b) return '';
  if (a.y === b.y) return a.m === b.m ? a.m + ' ' + a.y : a.m + '–' + b.m + ' ' + a.y;
  return a.m + ' ' + a.y + '–' + b.m + ' ' + b.y;
}
function shortDate(d) {
  var t = Date.parse(d || '');
  if (isNaN(t)) return d || '';
  return new Date(t).toISOString().slice(0, 10);
}
function snippet(t, n) { t = String(t || '').replace(/\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n - 1) + '…' : t; }

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
  var h = el('h2');
  h.id = 'ppg-h';
  h.textContent = vm.state === 'flagged' ? 'Why this post is marked' :
    vm.state === 'unavailable' ? PPG_LABELS.unavailable :
    deepOn ? 'Analysis of this post' : PPG_LABELS.clean;
  pop.appendChild(h);
  pop.appendChild(close);
  if (deepOn && vm.state === 'clean') pop.appendChild(el('p', 'meta', 'The automatic check found no known narrative, source post or coordination.'));
  if (deepOn && vm.state === 'checking') pop.appendChild(el('p', 'meta', 'The automatic check is still running.'));

  var labs = el('div', 'labels');
  vm.labels.forEach(function (c) { labs.appendChild(el('span', 'lab ' + c.kind, c.text)); });
  if (vm.pending) labs.appendChild(el('span', 'lab muted', 'Text check still running…'));
  if (labs.childNodes.length) pop.appendChild(labs);

  if (vm.state === 'unavailable') {
    pop.appendChild(el('p', 'meta', 'The checking service did not answer in time. Nothing is known about this post.'));
  }

  this._details(pop, ctx.result, '');
  if (ctx.quotedResult && ctx.vm.chips.some(function (c) { return c.quoted; })) {
    this._details(pop, ctx.quotedResult, 'Quoted post: ');
  }

  var r = ctx.result || {};
  var co = r.coordination;
  if (co && co.label === 'coordinated') {
    var s = el('section');
    s.appendChild(el('h3', null, 'Coordination (account-level signal)'));
    if (co.reasons && co.reasons.length) {
      var ul = el('ul');
      co.reasons.forEach(function (x) { ul.appendChild(el('li', null, x)); });
      s.appendChild(ul);
    }
    if (co.n_similar_accounts) s.appendChild(el('p', 'meta', co.n_similar_accounts + ' similar accounts in the cluster'));
    pop.appendChild(s);
  }
  if (r.status === 'timeout') {
    pop.appendChild(el('p', 'meta', 'The full text check did not finish within 60 s.'));
  }

  // In-depth analysis on request (src/deep.js): patterns, fact check, where it came from.
  if (typeof PPG_deepSection === 'function') PPG_deepSection(pop, ctx);

  var f = el('footer');
  f.appendChild(el('div', null, 'Automated assessment, may be wrong. Not a claim about who is behind this account.'));
  var fb = el('div', 'fb');
  var API_NAMES = { source: 'source_match', ppg: 'potentially_propaganda', coord: 'coordinated' };
  var verdict = vm.labels.map(function (c) { return (c.quoted ? 'quoted:' : '') + API_NAMES[c.kind]; }).join('+') || vm.state;
  var wrong = el('button', 'btn primary', 'Report a mistake');
  wrong.type = 'button';
  var right = el('button', 'btn ghost', 'Looks right');
  right.type = 'button';
  function send(says) {
    self.sendFeedback({ tweet_id: ctx.item.id, verdict_shown: verdict, user_says: says });
    while (fb.firstChild) fb.removeChild(fb.firstChild);
    var t = el('span', 'thanks', 'Thanks, your report was sent.');
    t.setAttribute('role', 'status');
    fb.appendChild(t);
    pop.focus();
  }
  wrong.addEventListener('click', function () { send('wrong'); });
  right.addEventListener('click', function () { send('right'); });
  fb.appendChild(wrong);
  if (vm.state === 'flagged') fb.appendChild(right);
  f.appendChild(fb);
  pop.appendChild(f);
};

PPG_Popover.prototype._details = function (pop, r, prefix) {
  if (!r) return;
  var c = r.content || {};
  var sm = r.source_match;

  if (sm && sm.telegram && sm.telegram.length && sm.strong) {
    var s = el('section');
    s.appendChild(el('h3', null, prefix + 'Telegram source post'));
    sm.telegram.slice(0, 2).forEach(function (t) {
      var p = el('p', 'meta');
      p.textContent = '@' + String(t.channel || '').replace(/^@/, '') + ' · ' + shortDate(t.date) + ' · ' + pct(t.sim) + ' similar · ';
      var u = safeUrl(t.url);
      if (u) {
        var a = el('a', null, 'open on Telegram');
        a.href = u; a.target = '_blank'; a.rel = 'noopener noreferrer';
        p.appendChild(a);
      }
      s.appendChild(p);
      if (t.text) s.appendChild(el('blockquote', null, snippet(t.text, 220)));
    });
    pop.appendChild(s);
  }

  if (c.narrative && c.narrative.text) {
    var n = el('section');
    n.appendChild(el('h3', null, prefix + 'Matches a known narrative'));
    n.appendChild(el('blockquote', null, '“' + c.narrative.text + '”'));
    n.appendChild(el('p', 'meta', 'Source: ' + srcName(c.narrative.source) + (c.narrative.sim != null ? ' · ' + pct(c.narrative.sim) + ' similar' : '')));
    pop.appendChild(n);
  }

  if (c.state_media_echo && c.state_media_echo.length) {
    var e = el('section');
    e.appendChild(el('h3', null, prefix + 'State-media echo'));
    c.state_media_echo.slice(0, 2).forEach(function (x) {
      var line = 'This narrative aired ' + x.n_aired + '× on ' + srcName(x.source);
      var rg = rangeText(x.first, x.last);
      e.appendChild(el('p', null, line + (rg ? ', ' + rg : '') + '.'));
      if (x.text) e.appendChild(el('p', 'meta', snippet(x.text, 140)));
    });
    pop.appendChild(e);
  }

  if (c.techniques && c.techniques.length) {
    var t = el('section');
    t.appendChild(el('h3', null, prefix + 'Techniques'));
    var tags = el('div', 'tags');
    c.techniques.slice(0, 6).forEach(function (x) {
      var row = el('div');
      row.appendChild(el('span', 'tag', x.name || x.code));
      if (x.quote) row.appendChild(el('span', 'q', '“' + snippet(x.quote, 90) + '”'));
      tags.appendChild(row);
    });
    t.appendChild(tags);
    pop.appendChild(t);
  }

  if (c.reason) {
    var rs = el('section');
    rs.appendChild(el('h3', null, prefix + 'Reason'));
    rs.appendChild(el('p', null, c.reason));
    pop.appendChild(rs);
  }
};
