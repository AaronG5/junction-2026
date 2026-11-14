import { useEffect, useRef } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, isDemoMode } from "../api/client";
import type { Story } from "../api/types";
import { LandingShell } from "../components/LandingShell";
import { ErrorState, Loading } from "../components/States";
import { VerdictPill } from "../components/Verdict";
import { useApi } from "../lib/useApi";

export const STEP_NAMES = ["What it says", "Is it true?", "Where it started", "How far it went", "The bigger picture"];
const LAST = STEP_NAMES.length;

/** Guided walk-through of one story, one question per step. The step lives in the address (?step=2). */
export function StoryTour() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { data: story, error, reload } = useApi(() => api.story(id), [id]);

  const raw = Number(params.get("step") ?? "1");
  const step = Number.isInteger(raw) ? Math.min(Math.max(raw, 1), LAST) : 1;
  const viaLink = params.get("via") === "link";

  const goTo = (n: number) => {
    const next = new URLSearchParams(params);
    next.set("step", String(n));
    setParams(next); // a new history entry, so the browser's Back button goes to the previous step
  };

  // Move focus to the step's heading so keyboard and screen-reader users land on the new content.
  const heading = useRef<HTMLHeadingElement>(null);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    heading.current?.focus();
  }, [step]);

  return (
    <LandingShell>
      <div className="tour">
        <nav aria-label="Walk-through steps" className="tour-steps">
          <ol>
            {STEP_NAMES.map((name, i) => {
              const n = i + 1;
              return (
                <li key={name}>
                  <button type="button" className="stepdot" data-state={n === step ? "on" : n < step ? "done" : "todo"}
                    aria-current={n === step ? "step" : undefined} onClick={() => goTo(n)}>
                    <span className="n" aria-hidden="true">{n}</span>{name}
                  </button>
                </li>
              );
            })}
          </ol>
        </nav>

        {viaLink && isDemoMode() && <p className="muted" style={{ fontSize: 13 }}>Demo mode: any link shows this example story.</p>}

        {error ? (
          <>
            <ErrorState error={error} onRetry={reload} />
            <Link to="/" className="btn btn-plain" style={{ alignSelf: "flex-start" }}>← Back to the start page</Link>
          </>
        ) : !story ? (
          <Loading label="Loading the story" />
        ) : (
          <>
            <Step story={story} step={step} headingRef={heading} />
            <div className="tour-nav">
              {step === 1 ? (
                <button type="button" className="btn btn-plain" onClick={() => navigate("/")}>← Pick another</button>
              ) : (
                <button type="button" className="btn btn-plain" onClick={() => goTo(step - 1)}>← Back</button>
              )}
              {step < LAST && (
                <button type="button" className="btn" onClick={() => goTo(step + 1)}>Next: {STEP_NAMES[step]}</button>
              )}
            </div>
          </>
        )}
      </div>
    </LandingShell>
  );
}

function Step({ story: s, step, headingRef }: { story: Story; step: number; headingRef: React.RefObject<HTMLHeadingElement> }) {
  const title = (text: string) => <h1 ref={headingRef} tabIndex={-1} className="tour-title">{text}</h1>;
  const eyebrow = <span className="lbl">Step {step} · {STEP_NAMES[step - 1]}</span>;

  if (step === 1) {
    return (
      <section className="tour-step">
        {eyebrow}
        {title("Here's the post. First, just read it.")}
        <article className="tour-card">
          <div className="row-between"><b>{s.post.author}</b><span className="muted" style={{ fontSize: 13 }}>{s.post.where}</span></div>
          <p style={{ fontSize: 22, lineHeight: 1.45 }}>{s.post.text}</p>
          <div className="tour-translation">
            <span className="lbl">In English</span>
            <span lang="en" className="muted" style={{ fontSize: 16 }}>{s.post.translation}</span>
          </div>
        </article>
        <p className="muted tour-why">We read posts in their own language. The translation is just for you; the checks run on the original.</p>
      </section>
    );
  }
  if (step === 2) {
    return (
      <section className="tour-step">
        {eyebrow}
        {title(s.claim.headline)}
        <div className="tour-card">
          <div className="row-between" style={{ alignItems: "center" }}>
            <span style={{ fontSize: 18, fontWeight: 600, flex: "1 1 360px", minWidth: 0 }}>{"“"}{s.claim.text}{"”"}</span>
            <VerdictPill verdict={s.claim.verdict} />
          </div>
          <p style={{ fontSize: 16, maxWidth: "64ch" }}>{s.claim.explanation}</p>
          <div className="tour-evidence"><span className="lbl">Evidence</span><span>{s.claim.evidence}</span></div>
        </div>
        <p className="muted tour-why">A verdict alone isn't worth much. You always see what it's based on, and you can disagree.</p>
      </section>
    );
  }
  if (step === 3) {
    return (
      <section className="tour-step">
        {eyebrow}
        {title(s.origin.headline)}
        <ol className="tour-card timeline timeline-big" aria-label="How it spread, earliest first">
          {s.origin.chain.map((n, i) => (
            <li key={i} className={i === 0 ? "tl-first" : undefined}>
              <span className="mono muted" style={{ fontSize: 12.5 }}>{n.meta}</span>
              <b style={{ fontSize: 16 }}>{n.name}</b>
              <span className="muted" style={{ fontSize: 14 }}>{n.kind}</span>
            </li>
          ))}
        </ol>
        <p className="muted tour-why">We match posts by meaning, not exact words, so a translation still counts as a copy. Forwards and shares come straight from the platform.</p>
      </section>
    );
  }
  if (step === 4) {
    const max = Math.max(1, ...s.travel.languages.map((l) => l.count));
    const colors = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)"];
    return (
      <section className="tour-step">
        {eyebrow}
        {title(s.travel.headline)}
        <div className="tour-stats">
          <div className="tour-card"><b className="num">{s.travel.copies}</b><span className="muted">copies found</span></div>
          <div className="tour-card"><b className="num">{s.travel.accounts}</b><span className="muted">channels, groups and pages</span></div>
          <div className="tour-card"><b>{s.travel.timeLabel}</b><span className="muted">from first post to last copy</span></div>
        </div>
        <div className="tour-card">
          <span className="lbl" id="bylang">Copies by language</span>
          <ul className="langcounts" aria-labelledby="bylang">
            {s.travel.languages.map((l, i) => (
              <li key={l.name}>
                <b>{l.name}</b>
                <div className="meter" style={{ height: 12 }} aria-hidden="true">
                  <div style={{ width: `${(l.count / max) * 100}%`, background: colors[i % 3] }} />
                </div>
                <span className="mono num">{l.count}</span>
              </li>
            ))}
          </ul>
        </div>
      </section>
    );
  }
  return (
    <section className="tour-step">
      {eyebrow}
      {title(s.narrative.headline)}
      <div className="tour-card tour-narrative">
        <span className="lbl" style={{ color: "var(--accent-soft-text)" }}>Narrative</span>
        <b style={{ fontSize: 22 }}>{s.narrative.name}</b>
        <span>{s.narrative.stats}</span>
      </div>
      <p className="muted tour-why">
        Nobody sorted these posts by hand. The system groups posts that say the same thing, in any language, and shows you where the story is heading.
      </p>
      <div className="tour-card tour-end">
        <div style={{ flex: "1 1 340px", minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
          <b style={{ fontSize: 20 }}>That's the whole picture, one step at a time.</b>
          <span className="muted">The full report puts it all on one screen, with every source and copy.</span>
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10 }}>
          <Link to="/" className="btn btn-ghost">Trace another post</Link>
          <Link to="/dashboard/narratives" className="btn btn-ghost">Open the dashboard</Link>
          <Link to={s.post.url ? `/dashboard/analyze?url=${encodeURIComponent(s.post.url)}` : "/dashboard/analyze"} className="btn">Open the full report</Link>
        </div>
      </div>
    </section>
  );
}
