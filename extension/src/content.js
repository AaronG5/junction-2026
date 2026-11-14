// Content script: finds posts as X renders them, sends near-viewport ones to
// the service worker, and draws badges. No network calls here.
/* global chrome, PPG_SEL, PPG_parseTweet, PPG_isLongEnough, PPG_viewModel, PPG_Badge, PPG_Popover, PPG_setTheme */
(function () {
  'use strict';
  if (window.__ppgLoaded) return; // guard against double injection
  window.__ppgLoaded = true;

  var settings = { enabled: true, showAll: false, sensitivity: 'normal' };
  var results = new Map();   // id -> result
  var sent = new Set();      // ids already sent to the worker this page life
  var views = new Map();     // main id -> {article, badge, item, quoted}
  var tracked = new WeakMap(); // article -> main id
  var sigs = new WeakMap();    // article -> permalink seen at last parse
  var port = null;
  var queue = [];            // items waiting to be posted to the worker
  var queueTimer = 0;
  var scanTimer = 0;
  var lastPath = location.href;

  var popover = new PPG_Popover(function (fb) {
    try { chrome.runtime.sendMessage({ type: 'feedback', body: fb }); } catch (e) { /* worker gone */ }
  });

  // ------------------------------------------------------------ worker link
  function getPort() {
    if (port) return port;
    try {
      port = chrome.runtime.connect({ name: 'ppg' });
    } catch (e) { port = null; return null; } // extension reloaded: this script is orphaned
    port.onMessage.addListener(onWorkerMessage);
    port.onDisconnect.addListener(function () {
      port = null;
      // Worker restarted. Re-ask for anything still open; its cache answers fast.
      var again = [];
      views.forEach(function (v) {
        [v.item, v.quoted].forEach(function (it) {
          if (!it) return;
          var r = results.get(it.id);
          if (!r || r.status === 'pending') again.push(it);
        });
      });
      if (again.length && settings.enabled) setTimeout(function () { post(again); }, 500);
    });
    return port;
  }

  function post(items) {
    var p = getPort();
    if (!p) return;
    try { p.postMessage({ type: 'scan', items: items, page: location.href }); } catch (e) { port = null; }
  }

  function enqueue(item) {
    if (sent.has(item.id)) return;
    sent.add(item.id);
    queue.push(item);
    if (!queueTimer) queueTimer = setTimeout(flushQueue, 50);
  }
  function flushQueue() {
    queueTimer = 0;
    if (!queue.length) return;
    var items = queue; queue = [];
    post(items);
  }

  function onWorkerMessage(msg) {
    if (!msg || msg.type !== 'results') return;
    var touched = new Set();
    msg.results.forEach(function (r) {
      if (!r || !r.id) return;
      results.set(r.id, r);
      touched.add(r.id);
    });
    views.forEach(function (v, id) {
      if (touched.has(id) || (v.quoted && touched.has(v.quoted.id))) renderView(v);
    });
  }

  // ------------------------------------------------------------ rendering
  function ctxFor(v) {
    var r = results.get(v.item.id);
    var q = v.quoted ? results.get(v.quoted.id) : undefined;
    return { item: v.item, quoted: v.quoted, result: r, quotedResult: q, vm: PPG_viewModel(r, settings, q) };
  }

  function renderView(v) {
    if (!v.badge) return;
    var ctx = ctxFor(v);
    v.badge.render(ctx.vm, settings.showAll);
    if (popover.isOpen() && popover.ctx && popover.ctx.item.id === v.item.id) popover.update(ctx);
  }

  function mountBadge(v, anchor) {
    if (!v.badge) {
      v.badge = new PPG_Badge(function (btn) {
        if (popover.isOpen() && popover.ctx && popover.ctx.item.id === v.item.id) { popover.close(); return; }
        popover.open(btn, ctxFor(v));
      });
    }
    var host = v.badge.host;
    if (host.isConnected && v.article.contains(host)) return;
    // Right after the timestamp link, inside the same flex row.
    if (anchor && anchor.parentNode) {
      if (anchor.getAttribute && anchor.getAttribute('role') === 'group') anchor.appendChild(host);
      else anchor.parentNode.insertBefore(host, anchor.nextSibling);
    } else {
      v.article.appendChild(host);
    }
  }

  // ------------------------------------------------------------ discovery
  // One IntersectionObserver for all posts; rootMargin pre-loads a little
  // ahead of the viewport so badges are usually ready when a post scrolls in.
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      var id = tracked.get(e.target);
      var v = id && views.get(id);
      io.unobserve(e.target);
      if (!v || !settings.enabled) return;
      enqueue(v.item);
      if (v.quoted && PPG_isLongEnough(v.quoted)) enqueue(v.quoted);
    });
  }, { rootMargin: '300px 0px 300px 0px', threshold: 0 });

  function scan() {
    scanTimer = 0;
    if (!settings.enabled) return;
    if (location.href !== lastPath) { lastPath = location.href; popover.close(false); }
    if (popover.isOpen() && !(popover.returnFocus && popover.returnFocus.isConnected)) popover.close(false);
    detectTheme();
    var arts = document.querySelectorAll(PPG_SEL.tweet);
    for (var i = 0; i < arts.length; i++) {
      var a = arts[i];
      // Skip nested articles (X does not nest them today, but be safe).
      if (a.parentElement && a.parentElement.closest(PPG_SEL.tweet)) continue;
      // Fast path: same permalink as last time and our badge still in place.
      var sig = sigOf(a);
      var known = tracked.get(a);
      if (known && sigs.get(a) === sig) {
        var kv = views.get(known);
        if (kv && kv.badge && kv.badge.host.isConnected && a.contains(kv.badge.host)) continue;
      }
      sigs.set(a, sig);
      var parsed;
      try { parsed = PPG_parseTweet(a); } catch (e) { continue; }
      if (!parsed || parsed.skip) { if (tracked.has(a)) untrack(a); continue; }
      var item = parsed.item;
      var long = PPG_isLongEnough(item) || (parsed.quoted && PPG_isLongEnough(parsed.quoted));
      if (!long) { if (tracked.has(a)) untrack(a); continue; }

      var prevId = tracked.get(a);
      if (prevId === item.id) {
        // Same post; React may have dropped our node. Put it back.
        var pv = views.get(item.id);
        if (pv) { pv.article = a; mountBadge(pv, parsed.anchor); }
        continue;
      }
      if (prevId) untrack(a); // X recycled this article for another post

      var v = views.get(item.id);
      if (!v) { v = { item: item, quoted: parsed.quoted, article: a, badge: null }; views.set(item.id, v); }
      else { v.article = a; }
      tracked.set(a, item.id);
      mountBadge(v, parsed.anchor);
      renderView(v);
      if (!sent.has(item.id)) io.observe(a);
    }
    // Forget views whose article left the DOM, to keep memory flat on long sessions.
    if (views.size > 400) {
      views.forEach(function (v, id) { if (!v.article.isConnected) views.delete(id); });
    }
  }

  function sigOf(a) {
    var t = a.querySelector(PPG_SEL.time);
    var l = t && t.closest('a');
    return (l && l.getAttribute('href')) || '';
  }

  function untrack(a) {
    var id = tracked.get(a);
    tracked.delete(a);
    io.unobserve(a);
    var v = id && views.get(id);
    if (v && v.article === a) {
      if (v.badge) v.badge.host.remove();
      views.delete(id);
    }
  }

  function scheduleScan() {
    if (!scanTimer) scanTimer = setTimeout(scan, 120);
  }

  var lastThemeCheck = 0;
  var theme = 'light';
  function detectTheme() {
    var now = Date.now();
    if (now - lastThemeCheck < 2000) return;
    lastThemeCheck = now;
    var bg = getComputedStyle(document.body || document.documentElement).backgroundColor || '';
    var m = /(\d+),\s*(\d+),\s*(\d+)/.exec(bg);
    var dark = m ? (0.299 * m[1] + 0.587 * m[2] + 0.114 * m[3]) < 128 : false;
    var t = dark ? 'dark' : 'light';
    if (t !== theme) { theme = t; PPG_setTheme(t); views.forEach(renderView); }
  }

  // One MutationObserver on the whole document; work is debounced into scan().
  var mo = new MutationObserver(function (muts) {
    for (var i = 0; i < muts.length; i++) {
      var t = muts[i].target;
      // Our own badge/popover changes: ignore.
      if (t.nodeName === 'PPG-BADGE' || t.nodeName === 'PPG-POPOVER') continue;
      scheduleScan();
      return;
    }
  });

  // X switches themes by restyling <body>/<html>; watch just those attributes.
  var themeMo = new MutationObserver(function () { lastThemeCheck = 0; detectTheme(); });

  function start() {
    mo.observe(document.documentElement, { childList: true, subtree: true });
    themeMo.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'style', 'data-theme'] });
    if (document.body) themeMo.observe(document.body, { attributes: true, attributeFilter: ['class', 'style'] });
    scheduleScan();
  }
  function stop() {
    mo.disconnect();
    themeMo.disconnect();
    popover.close(false);
    views.forEach(function (v) { if (v.badge) v.badge.host.remove(); io.unobserve(v.article); });
    views.clear();
    tracked = new WeakMap();
  }

  function applySettings(s, first) {
    var wasOn = settings.enabled;
    settings = {
      enabled: s.enabled !== false,
      showAll: !!s.showAll,
      sensitivity: s.sensitivity === 'strict' ? 'strict' : 'normal',
    };
    if (settings.enabled && (first || !wasOn)) start();
    else if (!settings.enabled && wasOn && !first) stop();
    else views.forEach(renderView);
  }

  try {
    chrome.storage.sync.get({ enabled: true, showAll: false, sensitivity: 'normal' }, function (s) { applySettings(s || {}, true); });
    chrome.storage.onChanged.addListener(function (ch, area) {
      if (area !== 'sync') return;
      if (!ch.enabled && !ch.showAll && !ch.sensitivity) return;
      chrome.storage.sync.get({ enabled: true, showAll: false, sensitivity: 'normal' }, function (s) { applySettings(s, false); });
    });
  } catch (e) { /* not in an extension context */ }
})();
