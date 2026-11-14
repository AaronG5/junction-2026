// Content script for Facebook: the Facebook counterpart of src/content.js (X).
//
// Where the posts come from: not from the page's markup, but from the data Facebook
// downloads to build it (src/fb/posts.js reads it):
//   - the JSON built into the page when it loads (the first posts), and
//   - every GraphQL answer while you scroll (src/fb/page-hook.js passes a copy).
// That data has each post's id, link, exact time, author, group and full text.
//
// Where the badge goes: Facebook doesn't mark which post on screen has which id, so a
// post's text block (data-ad-rendering-role="story_message") is matched to its data by
// the letters and digits of its text (PPG_fbTextKey). The badge goes right under the text.
//
// From there it works like X: posts near the viewport go to the service worker (port
// "ppg", same pipeline and backend API), results come back, the badge and popover
// (src/ui.js, src/view.js) show them.
/* global chrome, PPG_isLongEnough, PPG_viewModel, PPG_Badge, PPG_Popover, PPG_setTheme,
          PPG_fbParseJson, PPG_fbExtract, PPG_fbTextKey */
(function () {
  'use strict';
  if (window.__ppgFbLoaded) return;
  window.__ppgFbLoaded = true;

  var SOURCE = 'ppg-fb';
  var MESSAGE = "[data-ad-rendering-role='story_message']";
  var POST_ROOT = "[aria-posinset], div[role='feed'] > div, div[role='article']";
  var MATCH_CHARS = 60;   // how much of the text on screen must match the data
  var MIN_MATCH = 12;     // shorter texts are too ambiguous to match

  var settings = { enabled: true, showAll: false, sensitivity: 'normal' };
  var items = new Map();    // id -> item (everything Facebook has loaded)
  var keys = new Map();     // id -> PPG_fbTextKey(item.text)
  var results = new Map();  // id -> result
  var sent = new Set();     // ids already sent to the worker
  var views = new Map();    // id -> {item, msg, row, badge}
  var bound = new WeakMap(); // story_message element -> id
  var port = null, queue = [], queueTimer = 0, scanTimer = 0, running = false;

  var popover = new PPG_Popover(function (fb) {
    try { chrome.runtime.sendMessage({ type: 'feedback', body: fb }); } catch (e) { /* worker gone */ }
  });

  // ------------------------------------------------------------ data from Facebook
  function takeData(text) {
    var added = false;
    PPG_fbParseJson(text).forEach(function (doc) {
      PPG_fbExtract(doc).forEach(function (item) {
        if (items.has(item.id)) return;
        items.set(item.id, item);
        keys.set(item.id, PPG_fbTextKey(item.text));
        added = true;
      });
    });
    if (added) scheduleScan();
  }

  window.addEventListener('message', function (e) {
    if (e.source !== window || !e.data || e.data.source !== SOURCE) return;
    takeData(e.data.body);
  });

  function readEmbedded() {
    document.querySelectorAll("script[type='application/json']").forEach(function (s) {
      if (s.textContent.indexOf('"Story"') >= 0) takeData(s.textContent);
    });
  }

  // ------------------------------------------------------------ worker link (as on X)
  function getPort() {
    if (port) return port;
    try { port = chrome.runtime.connect({ name: 'ppg' }); } catch (e) { port = null; return null; }
    port.onMessage.addListener(onWorkerMessage);
    port.onDisconnect.addListener(function () {
      port = null;
      var again = [];
      views.forEach(function (v) { var r = results.get(v.item.id); if (!r || r.status === 'pending') again.push(v.item); });
      if (again.length && settings.enabled) setTimeout(function () { post(again); }, 500);
    });
    return port;
  }
  function post(list) {
    var p = getPort();
    if (!p) return;
    try { p.postMessage({ type: 'scan', items: list, page: location.href }); } catch (e) { port = null; }
  }
  function enqueue(item) {
    if (sent.has(item.id)) return;
    sent.add(item.id);
    queue.push(item);
    if (!queueTimer) queueTimer = setTimeout(function () { queueTimer = 0; var l = queue; queue = []; if (l.length) post(l); }, 50);
  }
  function onWorkerMessage(msg) {
    if (!msg || msg.type !== 'results') return;
    msg.results.forEach(function (r) {
      if (!r || !r.id) return;
      results.set(r.id, r);
      var v = views.get(r.id);
      if (v) renderView(v);
    });
  }

  // ------------------------------------------------------------ badges
  function ctxFor(v) {
    var r = results.get(v.item.id);
    return { item: v.item, quoted: null, result: r, quotedResult: undefined, vm: PPG_viewModel(r, settings, undefined) };
  }
  function renderView(v) {
    if (!v.badge) return;
    var ctx = ctxFor(v);
    v.badge.render(ctx.vm, settings.showAll);
    if (popover.isOpen() && popover.ctx && popover.ctx.item.id === v.item.id) popover.update(ctx);
  }
  function mount(v) {
    if (!v.badge) {
      v.badge = new PPG_Badge(function (btn) {
        if (popover.isOpen() && popover.ctx && popover.ctx.item.id === v.item.id) { popover.close(); return; }
        popover.open(btn, ctxFor(v));
      });
      v.row = document.createElement('div');
      v.row.setAttribute('data-ppg', 'fb-row');
      v.row.style.cssText = 'display:flex;align-items:center;min-height:20px;margin:6px 16px 2px;';
      v.row.appendChild(v.badge.host);
    }
    if (v.row.isConnected && v.row.previousElementSibling === v.msg) return;
    v.msg.insertAdjacentElement('afterend', v.row);
  }

  // ------------------------------------------------------------ matching posts on screen to their data
  // The text on screen may be cut at "See more", so its start must match the start of the data's text.
  function findItem(msg) {
    var shown = PPG_fbTextKey(msg.innerText.replace(/(…|\.\.\.)?\s*(see more|žiūrėti daugiau|rodyti daugiau)\s*$/i, ''));
    if (shown.length < MIN_MATCH) return null;
    var head = shown.slice(0, MATCH_CHARS);
    var found = null;
    keys.forEach(function (k, id) {
      if (!found && k.indexOf(head) === 0) found = id;
    });
    return found;
  }

  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      io.unobserve(e.target);
      var id = bound.get(e.target);
      var v = id && views.get(id);
      if (v && settings.enabled) enqueue(v.item);
    });
  }, { rootMargin: '300px 0px 300px 0px', threshold: 0 });

  function scan() {
    scanTimer = 0;
    if (!settings.enabled) return;
    detectTheme();
    var msgs = document.querySelectorAll(MESSAGE);
    for (var i = 0; i < msgs.length; i++) {
      var msg = msgs[i];
      if (msg.parentElement && msg.parentElement.closest(MESSAGE)) continue; // nested (shared post's text)
      if (!msg.closest(POST_ROOT)) continue;
      var id = bound.get(msg);
      if (id) { var known = views.get(id); if (known) { known.msg = msg; mount(known); } continue; }
      id = findItem(msg);
      if (!id) continue; // its data hasn't arrived yet; the next scan tries again
      var item = items.get(id);
      if (!PPG_isLongEnough(item)) { msg.dataset.ppg = 'skip:short'; bound.set(msg, id); continue; }
      bound.set(msg, id);
      msg.dataset.ppg = 'bound:' + id;
      var v = views.get(id);
      if (!v) { v = { item: item, msg: msg, row: null, badge: null }; views.set(id, v); }
      else v.msg = msg;
      mount(v);
      renderView(v);
      if (!sent.has(id)) io.observe(msg);
    }
    if (views.size > 400) views.forEach(function (v, k) { if (!v.msg.isConnected) views.delete(k); });
  }
  // No badges until React has taken over the server-rendered page ("hydration"): adding
  // anything to the first posts before that makes React throw error #418. Data is still
  // collected from the start; only the drawing waits.
  var hydrated = false;
  function afterHydration() { setTimeout(function () { hydrated = true; scheduleScan(); }, 1500); }
  if (document.readyState === 'complete') afterHydration();
  else window.addEventListener('load', afterHydration, { once: true });

  function scheduleScan() { if (!scanTimer && running && hydrated) scanTimer = setTimeout(scan, 150); }

  var lastThemeCheck = 0, theme = 'light';
  function detectTheme() {
    var now = Date.now();
    if (now - lastThemeCheck < 2000) return;
    lastThemeCheck = now;
    var t = document.documentElement.classList.contains('__fb-dark-mode') ? 'dark' : 'light';
    if (t !== theme) { theme = t; PPG_setTheme(t); views.forEach(renderView); }
  }

  var mo = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var n = muts[i].target;
      if (n.nodeName === 'PPG-BADGE' || n.nodeName === 'PPG-POPOVER' || (n.dataset && n.dataset.ppg === 'fb-row')) continue;
      scheduleScan();
      return;
    }
  });

  function start() {
    running = true;
    mo.observe(document.documentElement, { childList: true, subtree: true });
    scheduleScan();
  }
  function stop() {
    running = false;
    mo.disconnect();
    popover.close(false);
    views.forEach(function (v) { if (v.row) v.row.remove(); io.unobserve(v.msg); });
    views.clear();
    bound = new WeakMap();
  }
  function applySettings(s, first) {
    var wasOn = settings.enabled;
    settings = { enabled: s.enabled !== false, showAll: !!s.showAll, sensitivity: s.sensitivity === 'strict' ? 'strict' : 'normal' };
    if (settings.enabled && (first || !wasOn)) start();
    else if (!settings.enabled && wasOn && !first) stop();
    else views.forEach(renderView);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { readEmbedded(); scheduleScan(); });
  else readEmbedded();
  try {
    chrome.storage.sync.get({ enabled: true, showAll: false, sensitivity: 'normal' }, function (s) { applySettings(s || {}, true); });
    chrome.storage.onChanged.addListener(function (ch, area) {
      if (area !== 'sync' || (!ch.enabled && !ch.showAll && !ch.sensitivity)) return;
      chrome.storage.sync.get({ enabled: true, showAll: false, sensitivity: 'normal' }, function (s) { applySettings(s, false); });
    });
  } catch (e) { /* not in an extension context */ }
})();
