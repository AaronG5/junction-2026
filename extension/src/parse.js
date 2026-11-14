// Turns one rendered X post (<article data-testid="tweet">) into API items.
// Pure DOM reading; depends only on selectors.js. Safe to run in jsdom.
/* global PPG_SEL, PPG_STATUS_RE, PPG_PROMOTED_TEXT, PPG_REPOST_TEXT */
/* exported PPG_parseTweet, PPG_textOf */

var PPG_MIN_CHARS = 40;

// Text of a tweetText node: text nodes, emoji <img alt>, link text. Whitespace kept
// as X renders it, then trimmed. X wraps each run in spans; we walk all of them.
function PPG_textOf(node) {
  if (!node) return '';
  var out = '';
  (function walk(n) {
    for (var c = n.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) out += c.nodeValue;
      else if (c.nodeType === 1) {
        if (c.tagName === 'IMG') out += c.getAttribute('alt') || '';
        else if (c.tagName === 'BR') out += '\n';
        else walk(c);
      }
    }
  })(node);
  return out.replace(/ /g, ' ').trim();
}

function ppgStatusFromHref(href) {
  if (!href) return null;
  var path;
  try { path = new URL(href, 'https://x.com').pathname; } catch (e) { return null; }
  var m = PPG_STATUS_RE.exec(path);
  return m ? { handle: m[1], id: m[2] } : null;
}

function ppgIsPromoted(article) {
  if (article.querySelector(PPG_SEL.promotedMarkers)) return true;
  // X's "Ad" label is a bare span in the header; check short leaf spans only.
  var spans = article.querySelectorAll(PPG_SEL.userName + ' ~ * span, ' + PPG_SEL.userName + ' span');
  for (var i = 0; i < spans.length && i < 40; i++) {
    var s = spans[i];
    if (!s.firstElementChild && PPG_PROMOTED_TEXT.test((s.textContent || '').trim())) return true;
  }
  return false;
}

// Display name: first text in User-Name that is not the @handle.
function ppgDisplayName(userNameEl) {
  if (!userNameEl) return '';
  var spans = userNameEl.querySelectorAll('span');
  for (var i = 0; i < spans.length; i++) {
    var t = PPG_textOf(spans[i]);
    if (t && t.charAt(0) !== '@' && t !== '·') return t;
  }
  return '';
}

// Quote card = div[role=link] inside the article that has its own tweetText.
function ppgFindQuoteCard(article, mainText) {
  var cards = article.querySelectorAll(PPG_SEL.quoteCard);
  for (var i = 0; i < cards.length; i++) {
    var tt = cards[i].querySelector(PPG_SEL.tweetText);
    if (tt && tt !== mainText) return cards[i];
  }
  return null;
}

/**
 * Parse one article.
 * @returns {null | {skip: string} | {item, quoted, anchor}}
 *   item/quoted follow the API item shape (see API.md); anchor is the element
 *   the badge goes after (the timestamp link), or the action row.
 */
function PPG_parseTweet(article) {
  if (!article || !article.querySelector) return null;
  if (ppgIsPromoted(article)) return { skip: 'promoted' };

  var quoteCard = null;
  // Main text: the first tweetText that is not inside the quote card.
  var texts = article.querySelectorAll(PPG_SEL.tweetText);
  var mainText = null;
  for (var i = 0; i < texts.length; i++) {
    if (!texts[i].closest(PPG_SEL.quoteCard)) { mainText = texts[i]; break; }
  }
  quoteCard = ppgFindQuoteCard(article, mainText);

  // Main permalink: the <a> around the first <time> outside the quote card.
  var times = article.querySelectorAll(PPG_SEL.time);
  var timeEl = null, link = null, st = null;
  for (var j = 0; j < times.length; j++) {
    if (quoteCard && quoteCard.contains(times[j])) continue;
    var a = times[j].closest('a');
    var s = a && ppgStatusFromHref(a.getAttribute('href'));
    if (s) { timeEl = times[j]; link = a; st = s; break; }
  }
  if (!st) return { skip: 'no-status' }; // ads and some cards have no permalink

  var social = article.querySelector(PPG_SEL.socialContext);
  var isRepost = !!(social && PPG_REPOST_TEXT.test(social.textContent || ''));

  var userName = article.querySelector(PPG_SEL.userName);
  var item = {
    id: st.id,
    url: 'https://x.com/' + st.handle + '/status/' + st.id,
    author: st.handle,
    author_name: ppgDisplayName(userName),
    text: PPG_textOf(mainText),
    lang: (mainText && mainText.getAttribute('lang')) || null,
    time: timeEl.getAttribute('datetime') || null,
    kind: quoteCard ? 'quote' : (isRepost ? 'repost' : 'post'),
    quoted_id: null,
  };

  var quoted = null;
  if (quoteCard) {
    var qText = quoteCard.querySelector(PPG_SEL.tweetText);
    var qLink = quoteCard.querySelector(PPG_SEL.statusLink);
    var qs = qLink && ppgStatusFromHref(qLink.getAttribute('href'));
    var qTime = quoteCard.querySelector(PPG_SEL.time);
    var qUser = quoteCard.querySelector(PPG_SEL.userName);
    var qHandle = qs ? qs.handle : null;
    if (!qHandle && qUser) {
      var mh = /@([A-Za-z0-9_]{1,15})/.exec(qUser.textContent || '');
      qHandle = mh ? mh[1] : null;
    }
    // X often renders the quote card without a permalink. Then we use a
    // synthetic id so the item can still be cached and deduped.
    var qid = qs ? qs.id : 'q' + st.id;
    quoted = {
      id: qid,
      url: qs ? 'https://x.com/' + qs.handle + '/status/' + qs.id : null,
      author: qHandle,
      author_name: ppgDisplayName(qUser),
      text: PPG_textOf(qText),
      lang: (qText && qText.getAttribute('lang')) || null,
      time: qTime ? qTime.getAttribute('datetime') : null,
      kind: 'post',
      quoted_id: null,
    };
    item.quoted_id = qid;
  }

  // Badge anchor: after the timestamp link; fallback the action row.
  var anchor = link || article.querySelector(PPG_SEL.actionRow);
  return { item: item, quoted: quoted, anchor: anchor };
}

function PPG_isLongEnough(item) {
  return !!item && (item.text || '').length >= PPG_MIN_CHARS;
}
