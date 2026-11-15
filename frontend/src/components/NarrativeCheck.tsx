/**
 * The narrative check of a pasted post (backend: /api/ext/*, veritas/narrative.py).
 * Used by the start page and by Analyze: a hook that runs the check, and the result view.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { CheckTimeout, api, isSupportedLink } from "../api/client";
import type { NarrativeResult, StateMediaEcho, TelegramMatch, TextSegment } from "../api/types";
import { ErrorState } from "./States";

/** Example texts for "Try an example" (real backend and demo mode alike). */
export const FLAGGED_EXAMPLE =
  "The Kyiv regime is nothing but an American puppet. Washington gives the orders and Zelensky obeys, while ordinary Ukrainians die for NATO's war. Wake up!";
export const CLEAN_EXAMPLE =
  "Lovely autumn day in Vilnius. The market at Halės turgus had the first cranberries of the season, and the queue for kibinai was worth it.";
export const TEXT_EXAMPLES = [
  { label: "A pro-Kremlin post", text: FLAGGED_EXAMPLE },
  { label: "An everyday post", text: CLEAN_EXAMPLE },
];

/** Shortest text worth checking (the backend answers "nothing found" below 20 characters). */
export const MIN_CHECK_CHARS = 20;

export type InputKind = "empty" | "link" | "otherLink" | "short" | "text";

/** What the user pasted: a supported post link, some other link, or text to check. */
export function inputKind(value: string): InputKind {
  const v = value.trim();
  if (!v) return "empty";
  if (isSupportedLink(v)) return "link";
  if (/^(https?:\/\/|www\.)[^ \t\n]+$/i.test(v)) return "otherLink";
  if (v.length < MIN_CHECK_CHARS) return "short";
  return "text";
}

export const INPUT_PROBLEM: Partial<Record<InputKind, string>> = {
  empty: "Paste the text of a post, or a Telegram or Facebook link.",
  otherLink: "We can only open Telegram and Facebook links. Paste the post's text instead.",
  short: "That's too short to check. Paste at least a full sentence.",
};

export type CheckRun =
  | { state: "idle" }
  | { state: "running"; text: string; partial: NarrativeResult | null }
  | { state: "done"; text: string; result: NarrativeResult }
  | { state: "failed"; text: string; error: unknown; partial: NarrativeResult | null };

/** Runs the check; answers for an older text (or after unmount) are ignored. */
export function useNarrativeCheck() {
  const [run, setRun] = useState<CheckRun>({ state: "idle" });
  const latest = useRef(0);
  useEffect(() => () => { latest.current += 1; }, []);

  const start = useCallback((raw: string) => {
    const text = raw.trim();
    const id = ++latest.current;
    let partial: NarrativeResult | null = null;
    setRun({ state: "running", text, partial: null });
    api.checkText(text, (r) => {
      if (id !== latest.current || r.status !== "pending") return;
      partial = r;
      setRun({ state: "running", text, partial: r });
    }).then(
      (result) => { if (id === latest.current) setRun({ state: "done", text, result }); },
      (error) => {
        if (id !== latest.current) return;
        setRun({ state: "failed", text, error, partial: error instanceof CheckTimeout ? error.partial : partial });
      },
    );
  }, []);
  const reset = useCallback(() => { latest.current += 1; setRun({ state: "idle" }); }, []);
  return { run, start, reset };
}

const SOURCE_NAMES: Record<string, string> = {
  euvsdisinfo: "EUvsDisinfo", a4n: "Antibot4Navalny", overload: "Overload",
  rt: "RT", ria: "RIA Novosti", sputnik: "Sputnik", tass: "TASS",
};
export const sourceName = (s: string | null | undefined) => (s ? SOURCE_NAMES[s.toLowerCase()] ?? s : "an unnamed source");

/** Total airings on Russian state media (RT, 2024) across the matching narratives; falls back to the number of matches. */
export function echoCount(echo: StateMediaEcho[]): number {
  const n = echo.reduce((sum, e) => sum + (e.n_aired && e.n_aired > 0 ? e.n_aired : 0), 0);
  return n > 0 ? n : echo.length;
}

const pct = (sim: number) => sim.toFixed(2);
const day = (iso: string | null) => (iso ? iso.slice(0, 10) : "date unknown");
const safeLink = (url: string | null) => (url && /^https:\/\//i.test(url) ? url : null);

/** Split the text into plain and flagged parts: each quote is found once, case-insensitively, first come first served. */
export function highlight(text: string, quotes: { quote: string; label: string }[]): TextSegment[] {
  const lower = text.toLowerCase();
  const spans: { start: number; end: number; flag: string }[] = [];
  for (const q of quotes) {
    const needle = q.quote.trim().toLowerCase();
    if (!needle) continue;
    let from = 0;
    for (;;) {
      const at = lower.indexOf(needle, from);
      if (at < 0) break;
      const end = at + needle.length;
      if (!spans.some((s) => at < s.end && end > s.start)) {
        spans.push({ start: at, end, flag: q.label });
        break;
      }
      from = at + 1;
    }
  }
  spans.sort((a, b) => a.start - b.start);
  const out: TextSegment[] = [];
  let pos = 0;
  for (const s of spans) {
    if (s.start > pos) out.push({ text: text.slice(pos, s.start) });
    out.push({ text: text.slice(s.start, s.end), flag: s.flag });
    pos = s.end;
  }
  if (pos < text.length) out.push({ text: text.slice(pos) });
  return out;
}

/** The check's answer for one text: pending, done or failed. */
export function NarrativeReport({ run, onRetry }: { run: Exclude<CheckRun, { state: "idle" }>; onRetry?: () => void }) {
  const result = run.state === "done" ? run.result : run.partial;
  const pending = run.state === "running";
  return (
    <section className="card ncheck" aria-labelledby="ncheck-h" aria-busy={pending}>
      <h2 id="ncheck-h" className="visually-hidden">Narrative check</h2>
      {run.state === "failed" && <ErrorState error={run.error} onRetry={onRetry} />}
      {pending && !result && (
        <div className="state" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" />
          <span>Comparing the text with 18,600 known narratives…</span>
        </div>
      )}
      {result && <ResultBody text={run.text} r={result} judging={pending} />}
      <p className="muted ncheck-foot">Automated assessment, may be wrong. Not a claim about who is behind any account.</p>
    </section>
  );
}

function ResultBody({ text, r, judging }: { text: string; r: NarrativeResult; judging: boolean }) {
  const c = r.content;
  const flagged = !judging && (c?.label === "potentially_propaganda" || Boolean(r.source_match?.strong));
  const asserted = c?.label === "potentially_propaganda";
  const tg = r.source_match?.telegram ?? [];
  const strong = r.source_match?.strong ? tg[0] : undefined;
  const weaker = strong ? tg.slice(1) : tg;
  const techniques = c?.techniques ?? [];
  const echo = c?.state_media_echo ?? [];
  const segments = highlight(text, techniques.map((t) => ({ quote: t.quote, label: t.name })));
  const anyHighlight = segments.some((s) => s.flag);

  return (
    <div className="ncheck-body">
      <div className="ncheck-verdict" aria-live="polite">
        {judging ? (
          <>
            <span className="pill"><span className="spinner spinner-sm" aria-hidden="true" />Checking…</span>
            <span className="muted">Gemma is reading the post to see whether it asserts the narrative below.</span>
          </>
        ) : flagged ? (
          <span className="pill pill-big" data-verdict="flagged">
            <span className="vdot v-false" aria-hidden="true">!</span>Potentially propaganda
          </span>
        ) : (
          <span className="pill pill-big" data-verdict="clean">
            <span className="vdot v-unverified" aria-hidden="true">✓</span>No known propaganda narrative found
          </span>
        )}
        {!judging && c?.reason && <span className="ncheck-reason">{c.reason}</span>}
      </div>

      {c?.narrative && (asserted || judging) && (
        <div className="kv">
          <span className="lbl">{judging ? "Closest known narrative" : "Narratives"}</span>
          <b>{c.narrative.text}</b>
        </div>
      )}

      {strong && (
        <div className="ncheck-copy">
          <span className="chip chip-warn">Copies a known pro-Kremlin Telegram post</span>
          <TelegramRow t={strong} />
        </div>
      )}

      {r.coordination?.label === "coordinated" && (
        <div className="kv">
          <span className="chip chip-warn" style={{ alignSelf: "flex-start" }}>Coordinated posting</span>
          {r.coordination.reasons.map((x) => <span key={x} className="muted" style={{ fontSize: 13 }}>{x}</span>)}
        </div>
      )}

      {techniques.length > 0 && (
        <div className="kv">
          <span className="lbl">Techniques</span>
          <ul className="tags" aria-label="Techniques">
            {techniques.map((t, i) => (
              <li key={`${t.code}-${i}`} className="chip" title={t.quote ? `“${t.quote}”` : undefined}>{t.name}</li>
            ))}
          </ul>
        </div>
      )}

      {echo.length > 0 && (
        <p className="ncheck-stat">State Media Mentions: <span className="num">{echoCount(echo).toLocaleString("en-US")}</span></p>
      )}

      <div className="kv">
        <span className="lbl">The text you checked</span>
        <p className="post-text ncheck-text" data-testid="checked-text">
          {segments.map((p, i) =>
            p.flag ? (
              <mark key={i} className="flagged" title={p.flag}>
                {p.text}<span className="visually-hidden"> (flagged: {p.flag})</span>
              </mark>
            ) : <span key={i}>{p.text}</span>,
          )}
        </p>
        {anyHighlight && <span className="muted" style={{ fontSize: 12.5 }}>Underlined: the words behind each technique</span>}
      </div>

      {(weaker.length > 0 || (!asserted && !judging && c?.narrative)) && (
        <details className="ncheck-more">
          <summary>Closest matches</summary>
          {!asserted && !judging && c?.narrative && (
            <div className="kv">
              <span className="lbl">Closest known narrative</span>
              <span>{c.narrative.text}</span>
              <span className="muted" style={{ fontSize: 13 }}>
                {sourceName(c.narrative.source)} · similarity <span className="mono num">{pct(c.narrative.sim)}</span>
              </span>
            </div>
          )}
          {weaker.length > 0 && (
            <div className="kv">
              <span className="lbl">Closest Telegram posts</span>
              <ul className="ncheck-tg">{weaker.map((t, i) => <li key={i}><TelegramRow t={t} /></li>)}</ul>
            </div>
          )}
        </details>
      )}
    </div>
  );
}

function TelegramRow({ t }: { t: TelegramMatch }) {
  const href = safeLink(t.url);
  const name = t.channel ? `@${t.channel}` : "Telegram channel";
  return (
    <div className="ncheck-tgrow">
      <span>
        {href ? <a href={href} target="_blank" rel="noopener noreferrer" style={{ fontWeight: 700 }}>{name}</a> : <b>{name}</b>}
        <span className="muted mono" style={{ fontSize: 12.5 }}> · {day(t.date)} · similarity {pct(t.sim)}</span>
      </span>
      {t.text && <span className="muted" style={{ fontSize: 13.5 }}>{t.text}</span>}
    </div>
  );
}
