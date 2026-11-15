/**
 * How the narrative check works, and how well, in plain words. Shown on the start page and the System page.
 * Numbers come from the team's synthetic and held-out tests (propaganda/gemma); keep them in step with the bank's manifest.
 */

export const METHOD_STEPS = [
  {
    title: "Find the closest known narratives",
    text: "EmbeddingGemma turns the post into a 768-number fingerprint and compares it with 18.6k documented narrative claims from EUvsDisinfo, Overload and Antibot4Navalny, in any language.",
  },
  {
    title: "Check that the post asserts one",
    text: "A fine-tuned Gemma 4 E2B model reads the post next to those claims. Reporting on a claim, quoting it to reject it or debunking it doesn't count.",
  },
  {
    title: "Match it with Telegram sources",
    text: "We compare the whole post with 42.7k posts from 65 pro-Kremlin Telegram channels. A near-identical post or translation is shown as a copy.",
  },
  {
    title: "Show the state-media echo",
    text: "When RT or RIA Novosti pushed the same narrative (77k RT narratives from 2024 broadcast transcripts, 9k from RIA), we say so. This is context only and never decides the verdict.",
  },
  {
    title: "Spot coordination",
    text: "The same text posted by several accounts within an hour is shown as a separate signal.",
  },
];

export function MethodSteps() {
  return (
    <ol className="findout method-steps">
      {METHOD_STEPS.map((s) => (
        <li key={s.title}>
          <b>{s.title}</b>
          <span className="muted">{s.text}</span>
        </li>
      ))}
    </ol>
  );
}

/** Measured results, with the test sets named honestly. */
export function HowWellItWorks({ headingId, level = 2 }: { headingId: string; level?: 2 | 3 }) {
  const H = level === 2 ? "h2" : "h3";
  return (
    <div className="card" aria-labelledby={headingId} role="region" style={{ gap: 16 }}>
      <H id={headingId} className="h2" style={{ fontSize: 18 }}>How well it works</H>
      <div className="facts">
        <div>
          <b className="num">0.15%</b>
          <span className="muted">false alarms on hard negatives: posts that report, quote or debunk a narrative (synthetic test set)</span>
        </div>
        <div>
          <b className="num">99%</b>
          <span className="muted">of test posts that assert a known narrative are caught by the judge (held-out set)</span>
        </div>
      </div>
      <div className="kv">
        <span className="lbl">Case: a persona farm on Bluesky</span>
        <span>
          On a real persona farm (accounts run as invented personas), text matching alone flagged <b className="num">31 of 45</b> farm accounts.
          It flagged <b className="num">0 of 962</b> random accounts and <b className="num">0 of 2,703</b> accounts posting on the same topics.
        </span>
      </div>
      <small className="muted">
        One farm and test sets we built, so treat these as a guide, not a guarantee. Every result is an automated assessment and may be wrong.
        It never says who is behind an account.
      </small>
    </div>
  );
}
