import { FormEvent, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api, isSupportedLink } from "../api/client";
import type { Filters, Story } from "../api/types";
import { FilterBar } from "../components/FilterBar";
import { LandingShell } from "../components/LandingShell";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { StatusDot } from "../components/Verdict";
import { VERDICT_LABEL, defaultFilters } from "../lib/filters";
import { useApi } from "../lib/useApi";
import { useDocumentTitle } from "../lib/useDocumentTitle";

const LANDING_RANGES = [
  { value: "7" as const, label: "This week" },
  { value: "30" as const, label: "This month" },
  { value: "90" as const, label: "3 months" },
];

/** Start page: paste a link, or pick a story that's spreading now. */
export function Landing() {
  useDocumentTitle();
  return (
    <LandingShell>
      <Hero />
      <Trending />
      <FindOut />
      <DashboardBand />
    </LandingShell>
  );
}

function Hero() {
  const navigate = useNavigate();
  const [link, setLink] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const trace = async (e: FormEvent) => {
    e.preventDefault();
    if (!isSupportedLink(link)) {
      setProblem("That doesn't look like a Telegram or Facebook link. Try something like https://t.me/channel/1234.");
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      const story = await api.traceLink(link.trim());
      navigate(`/story/${encodeURIComponent(story.id)}?via=link`);
    } catch (err) {
      setBusy(false);
      setProblem(err instanceof ApiError ? err.message : "We couldn't reach Veritas Lens right now. Please try again in a moment.");
    }
  };

  return (
    <section className="hero">
      <div className="hero-copy">
        <span className="lbl">Telegram · Facebook · news sites · any language</span>
        <h1 className="hero-title">Every post has a story behind it. See where it started.</h1>
        <p className="hero-lede">
          Paste a link. In about a minute you'll know whether it holds up, who said it first, and how it travelled from one language to the next.
        </p>
        <form onSubmit={trace} className="hero-form" noValidate>
          <label htmlFor="trace-link" style={{ fontSize: 14, fontWeight: 600 }}>Link to a post, channel, group or page</label>
          <div className="hero-row">
            <input id="trace-link" className="field mono" value={link} placeholder="https://t.me/channel/1234"
              aria-invalid={Boolean(problem)} aria-describedby="trace-msg"
              onChange={(e) => { setLink(e.target.value); setProblem(null); }} />
            <button type="submit" className="btn" disabled={busy}>{busy ? "Tracing…" : "Trace it"}</button>
          </div>
          <span id="trace-msg" className={problem ? "ferror" : "muted"} style={{ fontSize: 13.5 }} role={problem ? "alert" : undefined}>
            {problem ?? "Works with public Telegram channels and groups, and Facebook pages and groups."}
          </span>
        </form>
        <p className="muted" style={{ fontSize: 15 }}>
          Rather look around first? <Link to="/dashboard/narratives" style={{ fontWeight: 600 }}>Open the dashboard</Link> and see every narrative we track.
        </p>
      </div>
      <TraceIllustration />
    </section>
  );
}

/** A traced post, drawn as a timeline. Decorative: the walk-through explains it properly. */
function TraceIllustration() {
  const nodes = [
    { meta: "08:00 · English", name: "rt.com", text: "“NATO armour stages provocations near Belarus”", filled: true },
    { meta: "09:30 · Russian · translated", name: "@kanal_a", text: "“Танки НАТО снова провоцируют…”" },
    { meta: "11:00 · Lithuanian · forwarded", name: "@kanalas_b", text: "“NATO tankai vėl provokuoja…”" },
  ];
  return (
    <div className="trace-card" aria-hidden="true">
      <span className="lbl">One post, traced</span>
      <ol className="timeline">
        {nodes.map((n) => (
          <li key={n.name} className={n.filled ? "tl-first" : undefined}>
            <span className="mono muted" style={{ fontSize: 12 }}>{n.meta}</span>
            <b>{n.name}</b>
            <span className="muted" style={{ fontSize: 13.5 }}>{n.text}</span>
          </li>
        ))}
      </ol>
      <div className="trace-foot">
        <span>+38 more copies in 6 hours</span>
        <span className="pill"><span className="vdot v-false">{"✕"}</span>Main claim false</span>
      </div>
    </div>
  );
}

function Trending() {
  const defaults = useMemo(() => defaultFilters("7"), []);
  const [filters, setFilters] = useState<Filters>(defaults);
  const { data, error, reload } = useApi(() => api.trending(filters), [filters]);

  return (
    <section className="landing-section" aria-labelledby="trend-h">
      <div className="row-between">
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <h2 id="trend-h" className="section-title">No link at hand? Start with what's spreading now</h2>
          <span className="muted">Stories Veritas Lens is following. Pick one and we'll walk you through it.</span>
        </div>
        <Link to="/dashboard/narratives" style={{ fontWeight: 600 }}>All narratives</Link>
      </div>
      <FilterBar value={filters} onChange={setFilters} compact ranges={LANDING_RANGES}
        platformAllLabel="All platforms" statusAllLabel="Any status" statusLabel="Status of the main claim" dateLabel="Time" />
      {error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : !data ? (
        <Loading label="Loading stories" />
      ) : data.stories.length === 0 ? (
        <EmptyState action={<button type="button" className="link-btn" onClick={() => setFilters(defaults)}>Clear filters</button>}>
          No stories match. Try another platform, a longer period or a different status.
        </EmptyState>
      ) : (
        <ul className="picks">
          {data.stories.map((s) => <StoryCard key={s.id} story={s} />)}
        </ul>
      )}
    </section>
  );
}

function StoryCard({ story: s }: { story: Story }) {
  const trend = s.trend.changePct === null ? "new" : `${s.trend.changePct > 0 ? "+" : ""}${s.trend.changePct}%`;
  return (
    <li>
      <Link to={`/story/${encodeURIComponent(s.id)}`} className="pick">
        <span className="row-between" style={{ alignItems: "center" }}>
          <span className="chip">{s.topic}</span>
          <span className="mono muted" style={{ fontSize: 12 }}>{s.trend.posts.toLocaleString("en-US")} posts · {trend}</span>
        </span>
        <span className="pick-title">{s.headline}</span>
        <span className="pick-status"><StatusDot verdict={s.status} />Main claim: {VERDICT_LABEL[s.status].toLowerCase()}</span>
        <span className="muted" style={{ fontSize: 13.5 }}>{s.sub}</span>
        <span className="pick-cta">Walk me through it →</span>
      </Link>
    </li>
  );
}

function FindOut() {
  const points = [
    { title: "Whether it holds up", text: "Each claim is checked against sources, and you see the evidence, not just a verdict." },
    { title: "Who said it first", text: "We follow copies, forwards and translations back to the earliest source we can find." },
    { title: "What it's part of", text: "Most posts belong to a bigger storyline. See how big it is and whether it's growing." },
    { title: "On your own computer, if you like", text: "Open source. Run the whole system yourself, offline, and nothing leaves your machine." },
  ];
  return (
    <section className="landing-section" aria-labelledby="how-h">
      <h2 id="how-h" className="section-title">What you'll find out</h2>
      <div className="findout">
        {points.map((p) => (
          <div key={p.title}>
            <b>{p.title}</b>
            <span className="muted">{p.text}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function DashboardBand() {
  const { data } = useApi(() => api.narratives(defaultFilters("7")), []);
  return (
    <section className="landing-section" aria-labelledby="dash-h">
      <div className="band">
        <div style={{ flex: "1 1 420px", minWidth: 0, display: "flex", flexDirection: "column", gap: 10 }}>
          <span className="lbl" style={{ color: "var(--accent-soft-text)" }}>The dashboard</span>
          <h2 id="dash-h" className="section-title" style={{ fontSize: 28 }}>See every story we're following, as it grows</h2>
          <span style={{ maxWidth: "60ch" }}>
            A live map of the narratives spreading on Telegram and Facebook, with the claims behind them and the sources pushing them.
            Filter by platform, date and verdict.
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 14, alignItems: "flex-start" }}>
          {data && (
            <div className="band-stats">
              <div><b className="num">{data.run.clusters}</b><span>narratives this week</span></div>
              <div><b className="num">{data.run.posts.toLocaleString("en-US")}</b><span>posts analyzed</span></div>
            </div>
          )}
          <Link to="/dashboard/narratives" className="btn btn-big">Open the dashboard →</Link>
        </div>
      </div>
    </section>
  );
}
