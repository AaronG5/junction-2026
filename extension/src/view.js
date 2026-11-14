// Result -> what the badge shows. Pure logic, no DOM. Shared by the content
// script and the unit tests. Two separate signals, never merged:
//   content      (source match, judge verdict)   and   coordination.
/* exported PPG_viewModel, PPG_ariaLabel, PPG_LABELS */

var PPG_LABELS = Object.freeze({
  source: 'Copies a known pro-Kremlin Telegram post',
  sourceShort: 'Copies pro-Kremlin Telegram post',
  ppg: 'Potentially propaganda',
  coord: 'Coordinated posting',
  coordShort: 'Coordinated',
  sourceTiny: 'Telegram copy',
  unavailable: 'Check unavailable',
  checking: 'Checking this post',
  clean: 'Nothing found',
  quoted: 'Quoted post: ',
  quotedShort: 'Quoted: ',
});

// Strict = fewer flags: the judge's verdict must also clear this score.
// Normal = trust the backend's labels. A source match counts only when the
// backend marks it "strong", in both modes.
var PPG_STRICT_SCORE = 0.75;

/**
 * @param {object|undefined} r  API result for the post (undefined = not back yet)
 * @param {{sensitivity:'strict'|'normal'}} settings
 * @param {object|undefined} q  API result for the quoted post, if any
 * @returns {{state:'checking'|'clean'|'flagged'|'unavailable', chips:Array, labels:Array, pending:boolean}}
 *   chips  = what the badge draws: at most one content chip and one
 *            coordination chip (plus one for a flagged quoted post);
 *   labels = every individual finding, listed in the popover.
 */
function PPG_viewModel(r, settings, q) {
  var strict = settings && settings.sensitivity === 'strict';
  var none = { chips: [], labels: [] };
  if (!r) return mk('checking', none, true);
  if (r.status === 'error' && !hasAnything(r)) return mk('unavailable', none, false);

  var out = { chips: [], labels: [] };
  contentSignal(r, strict, false, out);
  var co = r.coordination;
  if (co && co.label === 'coordinated') {
    out.chips.push({ kind: 'coord', text: PPG_LABELS.coordShort, short: PPG_LABELS.coordShort, aria: PPG_LABELS.coord, signal: 'coordination' });
    out.labels.push({ kind: 'coord', text: PPG_LABELS.coord });
  }
  // The quoted post's content signal, clearly marked as being about the quoted post.
  if (q && q.status !== 'error') contentSignal(q, strict, true, out);

  var pending = r.status === 'pending';
  if (out.chips.length) return mk('flagged', out, pending);
  if (pending) return mk('checking', none, true);
  if (r.status === 'timeout' || r.status === 'error') return mk('unavailable', none, false);
  return mk('clean', none, false);
}

function mk(state, o, pending) { return { state: state, chips: o.chips, labels: o.labels, pending: pending }; }

function hasAnything(r) {
  return !!((r.source_match && r.source_match.strong) || (r.content && r.content.label === 'potentially_propaganda') ||
    (r.coordination && r.coordination.label === 'coordinated'));
}

// One content chip: the judge's verdict if it flagged the post, else the
// source match. A source match also makes the chip "strong" (red, ⧉ icon).
function contentSignal(r, strict, quoted, out) {
  var sm = !!(r.source_match && r.source_match.strong);
  var c = r.content;
  var ppg = !!(c && c.label === 'potentially_propaganda' && (!strict || (c.score || 0) >= PPG_STRICT_SCORE));
  if (!sm && !ppg) return;
  var pre = quoted ? PPG_LABELS.quoted : '';
  var preShort = quoted ? PPG_LABELS.quotedShort : '';
  if (sm) out.labels.push({ kind: 'source', text: pre + PPG_LABELS.source, quoted: quoted });
  if (ppg) out.labels.push({ kind: 'ppg', text: pre + PPG_LABELS.ppg, quoted: quoted });
  var aria = [];
  if (ppg) aria.push(PPG_LABELS.ppg);
  if (sm) aria.push(PPG_LABELS.source.charAt(0).toLowerCase() + PPG_LABELS.source.slice(1));
  aria[0] = aria[0].charAt(0).toUpperCase() + aria[0].slice(1);
  out.chips.push({
    kind: sm ? 'source' : 'ppg',
    text: preShort + (ppg ? PPG_LABELS.ppg : PPG_LABELS.sourceShort),
    short: preShort + (ppg ? PPG_LABELS.ppg : PPG_LABELS.sourceTiny),
    aria: pre + aria.join('; '),
    signal: 'content',
    quoted: quoted,
  });
}

function PPG_ariaLabel(vm) {
  if (vm.state === 'checking') return PPG_LABELS.checking;
  if (vm.state === 'unavailable') return PPG_LABELS.unavailable;
  if (vm.state === 'clean') return PPG_LABELS.clean + '. Open details';
  var parts = vm.chips.map(function (c) { return c.aria || c.text; });
  if (vm.pending) parts.push('still checking');
  return parts.join('; ') + '. Open details';
}


