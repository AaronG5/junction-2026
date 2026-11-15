import { FormEvent, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, MAX_CHECK_TEXT, api } from "../api/client";
import type { Filters, Story } from "../api/types";
import { FilterBar } from "../components/FilterBar";
import { LandingShell } from "../components/LandingShell";
import { HowWellItWorks, MethodSteps } from "../components/Method";
import { INPUT_PROBLEM, NarrativeReport, TEXT_EXAMPLES, inputKind, useNarrativeCheck } from "../components/NarrativeCheck";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { VerdictStamp } from "../components/Verdict";
import { defaultFilters } from "../lib/filters";
import { useApi } from "../lib/useApi";
import { useDocumentTitle } from "../lib/useDocumentTitle";

/** Start page: paste a post (or a link to one), or pick a story off the board. */
export function Landing() {
  useDocumentTitle();
  return (
    <LandingShell>
      <Hero />
      <Exhibit />
      <Board />
      <Method />
      <Sovereignty />
    </LandingShell>
  );
}

/* ------------------------------------------------------------------- hero */

function Hero() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { run, start } = useNarrativeCheck();
  const resultRef = useRef<HTMLDivElement>(null);

  const check = (text: string) => {
    start(text);
    // Move to the answer (it appears under the hero), for keyboard and screen-reader users too.
    requestAnimationFrame(() => resultRef.current?.focus());
  };

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const kind = inputKind(value);
    if (kind !== "link" && kind !== "text") {
      setProblem(INPUT_PROBLEM[kind] ?? null);
      return;
    }
    setProblem(null);
    if (kind === "text") {
      check(value);
      return;
    }
    setBusy(true);
    try {
      const story = await api.traceLink(value.trim());
      navigate(`/story/${encodeURIComponent(story.id)}?via=link`);
    } catch (err) {
      setBusy(false);
      setProblem(err instanceof ApiError ? err.message : "We couldn't reach Providence right now. Please try again in a moment.");
    }
  };

  const checking = run.state === "running";

  return (
    <>
      <section className="hero">
        <div className="shell hero-in">
          <div className="hero-tag">
            <span className="lbl">Any language · Telegram · Facebook · X · news sites</span>
            <span className="hero-live mono num">18.6k narratives on file</span>
          </div>

          <h1 className="d1 hero-title">
            <span className="l1">Every lie has</span>{" "}
            <span className="l2">a first <span className="knock">author.</span></span>
          </h1>

          <p className="lede">
            We compare it with 18.6k documented pro-Kremlin narratives and 42.7k Telegram posts, then a
            fine-tuned Gemma reads it to see whether it asserts one &mdash; not just mentions it.
          </p>

          <form onSubmit={submit} className="intake" noValidate>
            <label htmlFor="trace-link" className="lbl">Text of a post, or a link to one</label>
            <textarea id="trace-link" className="field field-text" value={value} rows={4} maxLength={MAX_CHECK_TEXT}
              placeholder="Paste a post here, in any language. Or a Telegram or Facebook link, to trace where it came from."
              aria-invalid={Boolean(problem)} aria-describedby="trace-msg"
              onChange={(e) => { setValue(e.target.value); setProblem(null); }}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void submit(); }} />
            <div className="intake-foot">
              <button type="submit" className="btn" disabled={busy || checking}>
                {busy ? "Tracing…" : checking ? "Checking…" : "Check it"}
              </button>
              <span className="intake-try">
                {TEXT_EXAMPLES.map((ex) => (
                  <button key={ex.label} type="button" className="chip chip-btn" disabled={checking}
                    onClick={() => { setValue(ex.text); setProblem(null); check(ex.text); }}>{ex.label}</button>
                ))}
              </span>
            </div>
            <span id="trace-msg" className={problem ? "intake-note is-error" : "intake-note"} role={problem ? "alert" : undefined}>
              {problem ?? "Text is checked against known narratives · a link opens the full trace"}
            </span>
          </form>
        </div>
      </section>

      {run.state !== "idle" && (
        <div ref={resultRef} tabIndex={-1} className="shell section result-band" aria-label="Result of the check">
          <NarrativeReport run={run} onRetry={() => check(run.text)} />
        </div>
      )}
    </>
  );
}

/* ----------------------------------------------------------- 01 the trace */

/** One hop of the traced post, placed on the plate's 0-100 grid. */
const HOPS = [
  { no: "01", x: 0, y: 5, meta: "08:00 · EN", name: "rt.com", role: "Russian state media" },
  { no: "02", x: 25, y: 25, meta: "09:30 · RU", name: "@kanal_a", role: "Translated from the first" },
  { no: "03", x: 50, y: 45, meta: "11:00 · LT", name: "@kanalas_b", role: "Forwarded, then translated" },
  { no: "04", x: 75, y: 65, meta: "12:40 · LT", name: "Tiesa be cenzūros", role: "Shared to a Facebook page" },
];
/** Cells of the plate's 5% grid, so every card and elbow lands on a gridline. */
const NODE_W = 20;
const NODE_H = 15;

/**
 * The trace as a real diagram: every card declares a left and a right port, and
 * each thread is routed orthogonally between two of them, so the geometry is
 * derived rather than eyeballed. The threads are drawn in a 0-100 box with
 * preserveAspectRatio="none" — the same coordinate space the cards sit in.
 */
function Exhibit() {
  const route = (i: number) => {
    const a = { x: HOPS[i].x + NODE_W, y: HOPS[i].y + NODE_H / 2 };
    const b = { x: HOPS[i + 1].x, y: HOPS[i + 1].y + NODE_H / 2 };
    const mid = a.x + 2.5; // the centre of the 5% gutter between two cards
    return `M${a.x} ${a.y} H${mid} V${b.y} H${b.x}`;
  };

  return (
    <section className="exhibit" aria-labelledby="trace-h">
      <div className="shell">
        <div className="section-head">
          <span className="lbl">Paste a link instead of text</span>
          <h2 id="trace-h" className="d2 section-title">We walk it back, hop by hop.</h2>
        </div>

        <figure className="plate">
          <div className="plot">
            <svg className="threads" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
              <path className="thread-soft" d={route(0)} />
              <path d={route(1)} />
              <path d={route(2)} />
            </svg>

            {HOPS.map((h, i) => (
              <article key={h.no} className={i === 0 ? "hop hop-origin" : "hop"}
                style={{
                  ["--x" as string]: `${h.x}%`, ["--y" as string]: `${h.y}%`,
                  ["--w" as string]: `${NODE_W}%`, ["--h" as string]: `${NODE_H}%`,
                }}>
                <span className="hop-top"><span>{h.meta}</span></span>
                <span className="hop-name">{h.name}</span>
                <span className="hop-role">{h.role}</span>
                <i className="port port-l" aria-hidden="true" />
                <i className="port port-r" aria-hidden="true" />
              </article>
            ))}

            <div className="plate-aside">
              <p className="plate-method">
                <b className="lbl">How a hop is proven</b>
                Forwards and shares come from the platform&rsquo;s own metadata. Translations are matched on
                meaning &mdash; multilingual embeddings, then confirmed by Gemma.
              </p>
              <ul className="plate-key">
                <li><i className="key-solid" aria-hidden="true" />Forwarded or shared &mdash; from the platform</li>
                <li><i className="key-dash" aria-hidden="true" />Same meaning, another language</li>
              </ul>
            </div>

          </div>
          <dl className="stats plate-sum">
            <div><dt className="lbl">Copies found</dt><dd className="num">41</dd></div>
            <div><dt className="lbl">Accounts</dt><dd className="num">29</dd></div>
            <div><dt className="lbl">Languages</dt><dd className="num">3</dd></div>
            <div><dt className="lbl">First to last</dt><dd className="num">6h</dd></div>
          </dl>
          <figcaption className="visually-hidden">
            A post first published by rt.com at 08:00 in English, translated into Russian by @kanal_a at 09:30, forwarded
            in Lithuanian by @kanalas_b at 11:00, and shared by the Facebook page Tiesa be cenzūros at 12:40.
          </figcaption>
        </figure>
      </div>
    </section>
  );
}

/* -------------------------------------------------------- 02 on the board */

function Board() {
  const defaults = useMemo(() => defaultFilters("7"), []);
  const [filters, setFilters] = useState<Filters>(defaults);
  const { data, error, reload } = useApi(() => api.trending(filters), [filters]);

  return (
    <section className="shell section" aria-labelledby="trend-h">
      <div className="board-head">
        <h2 id="trend-h" className="board-title">What&rsquo;s spreading this week</h2>
      </div>

      {/* One axis here. Three filter rows governing three results is chrome
          outweighing payload; the dashboard is where filtering belongs. */}
      <FilterBar value={filters} onChange={setFilters} compact axes={["platform"]} platformAllLabel="All platforms" />

      {error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : !data ? (
        <Loading label="Loading stories" />
      ) : data.stories.length === 0 ? (
        <EmptyState action={<button type="button" className="link-btn" onClick={() => setFilters(defaults)}>Clear filters</button>}>
          No stories match. Try another platform, a longer period or a different status.
        </EmptyState>
      ) : (
        <>
          <div className="picks-head lbl" aria-hidden="true">
            <span>Narrative</span><span>Topic</span><span>Main claim</span><span>Posts (7d)</span><span>vs prev</span>
          </div>
          <ol className="picks">
            {data.stories.map((s) => <StoryRow key={s.id} story={s} />)}
          </ol>
        </>
      )}
    </section>
  );
}

function StoryRow({ story: s }: { story: Story }) {
  const trend = s.trend.changePct === null ? "new" : `${s.trend.changePct > 0 ? "+" : ""}${s.trend.changePct}%`;
  return (
    <li>
      <Link to={`/story/${encodeURIComponent(s.id)}`} className="pick">
        <span className="pick-title">{s.headline}</span>
        <span className="pick-topic mono-l">{s.topic} · {s.platform}</span>
        <span className="pick-verdict"><VerdictStamp verdict={s.status} /></span>
        <span className="pick-count mono num">{s.trend.posts.toLocaleString("en-US")}</span>
        <span className="pick-trend mono num">{trend}</span>
      </Link>
    </li>
  );
}

/* -------------------------------------------------------------- 03 method */

function Method() {
  return (
    <section className="method" aria-labelledby="how-h">
      <div className="shell">
        <div className="section-head">
          <span className="lbl">Five steps, in order</span>
          <h2 id="how-h" className="d2 section-title">How the check works</h2>
        </div>
        <div className="method-grid">
          <MethodSteps />
          <HowWellItWorks headingId="well-h" />
        </div>
      </div>
    </section>
  );
}

/* --------------------------------------------------------- 04 sovereignty */

function Sovereignty() {
  const { data } = useApi(() => api.narratives(defaultFilters("7")), []);
  return (
    <section className="shell section section-quiet" aria-labelledby="sov-h">
      <div className="sov">
        <div className="sov-main">
          <h2 id="sov-h" className="sov-say">
            Run the whole thing on your own machine.
          </h2>
          <Link to="/dashboard/analyze" className="btn btn-big">Open the dashboard</Link>
        </div>
        <div className="sov-side">
          <dl className="sov-zero">
            <dt className="lbl">Accounts needed</dt><dd className="num">0</dd>
          </dl>
          <dl className="stats sov-stats">
            <div><dt className="lbl">Posts read</dt><dd className="num">{data ? data.run.posts.toLocaleString("en-US") : "—"}</dd></div>
            <div><dt className="lbl">Cost to run</dt><dd className="num">€0</dd></div>
          </dl>
        </div>
      </div>
    </section>
  );
}
