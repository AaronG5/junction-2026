import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { ChannelReport } from "../api/types";
import { ErrorState, Loading } from "../components/States";
import { VerdictPill } from "../components/Verdict";
import { shortDate } from "../lib/filters";
import { useApi } from "../lib/useApi";

/** Report for one source: what it posts, how often, and where its content comes from. */
export function Channel() {
  const { id = "" } = useParams();
  const { data, error, reload } = useApi(() => api.channel(id), [id]);

  return (
    <div className="page">
      <Link to="/dashboard/sources" style={{ fontSize: 13, fontWeight: 600, textDecoration: "none" }}>← All sources</Link>
      {error ? (
        <>
          <h1 className="page-title">Channel report</h1>
          <ErrorState error={error} onRetry={reload} />
        </>
      ) : !data ? (
        <>
          <h1 className="page-title">Channel report</h1>
          <Loading label="Loading the report" />
        </>
      ) : (
        <Report r={data} />
      )}
    </div>
  );
}

function Report({ r }: { r: ChannelReport }) {
  const [days, setDays] = useState<30 | 90>(30);
  return (
    <>
      <header className="page-head">
        <div style={{ display: "flex", flexDirection: "column", gap: 6, minWidth: 0 }}>
          <span className="lbl">{r.typeLabel} · Channel report</span>
          <h1 className="page-title" style={{ fontSize: 30 }}>{r.name}</h1>
          <span className="muted">{r.subtitle}</span>
        </div>
        <Link to="/dashboard/narratives" className="btn">See its narratives</Link>
      </header>

      <p className="muted" style={{ fontSize: 13, maxWidth: "80ch" }}>
        {r.basedOn} This report describes what the channel publishes and where its content comes from. It does not say who runs it.
      </p>

      <section className="tiles" aria-label="Summary">
        <Tile label="Posts per day" value={String(r.stats.postsPerDay)} note="Last 30 days, average" />
        <Tile label="Original posts" value={`${r.stats.originalPct}%`} note={`${100 - r.stats.originalPct}% first appeared somewhere else`} />
        <Tile label="Near copies" value={`${r.stats.nearCopyPct}%`} note="Almost identical to another source's post" />
        <Tile label="Links to state media" value={`${r.stats.stateMediaLinkPct}%`} note="Of all links it shares" />
      </section>

      <ActivityChart daily={r.daily.slice(-days)} days={days} onDays={setDays} />

      <div className="split">
        <section className="card" aria-labelledby="mix-h" style={{ flex: "1 1 380px" }}>
          <h2 id="mix-h" className="h2">What it posts about</h2>
          <span className="muted" style={{ fontSize: 12.5 }}>Share of its posts per category, last 90 days</span>
          {r.topics.map((t) => (
            <div key={t.name} className="hbar">
              <span>{t.name}</span>
              <div className="meter meter-blue" style={{ height: 10 }} aria-hidden="true"><div style={{ width: `${t.pct}%` }} /></div>
              <span className="mono num">{t.pct}%</span>
            </div>
          ))}
        </section>

        <section className="card" aria-labelledby="up-h" style={{ flex: "1 1 380px" }}>
          <h2 id="up-h" className="h2">Where its posts come from</h2>
          <span className="muted" style={{ fontSize: 12.5 }}>Sources that published the same content first</span>
          <div style={{ overflowX: "auto" }}>
            <table className="table" style={{ minWidth: 420 }}>
              <thead><tr><th>Source</th><th style={{ textAlign: "right" }}>Posts</th><th style={{ textAlign: "right" }}>Usual delay</th></tr></thead>
              <tbody>
                {r.upstream.map((u) => (
                  <tr key={u.name}>
                    <td><b>{u.name}</b><br /><small className="muted">{u.kind}</small></td>
                    <td className="mono num" style={{ textAlign: "right" }}>{u.posts}</td>
                    <td className="mono num" style={{ textAlign: "right" }}>{u.typicalDelay}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section className="card" aria-labelledby="rec-h">
        <div className="row-between">
          <h2 id="rec-h" className="h2">Recent posts</h2>
          <span className="muted" style={{ fontSize: 12.5 }}>With the verdict of their main claim. Open one to analyze it.</span>
        </div>
        <ul className="recent">
          {r.recent.map((p) => (
            <li key={p.id}>
              <Link to={`/dashboard/analyze?url=${encodeURIComponent(p.url)}`} className="recent-link">
                <span className="recent-text">
                  <span style={{ fontWeight: 500 }}>{p.text}</span>
                  <small className="muted">{shortDate(p.publishedAt.slice(0, 10))}, {p.publishedAt.slice(11, 16)} · {p.views.toLocaleString("en-US")} views</small>
                </span>
                <VerdictPill verdict={p.verdict} />
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="card tile">
      <span className="lbl">{label}</span>
      <span className="tile-num num" style={{ fontSize: 26 }}>{value}</span>
      <span className="muted tile-note">{note}</span>
    </div>
  );
}

/** Bar chart of posts per day. The busiest day is drawn full strength and named below. */
function ActivityChart({ daily, days, onDays }: { daily: ChannelReport["daily"]; days: 30 | 90; onDays: (d: 30 | 90) => void }) {
  const max = Math.max(1, ...daily.map((d) => d.posts));
  const top = Math.ceil(max / 20) * 20; // y axis rounded up to a multiple of 20
  const ticks = [top, (top * 2) / 3, top / 3, 0].map((t) => Math.round(t));
  const peak = daily.reduce((a, b) => (b.posts > a.posts ? b : a), daily[0]);
  const first = daily[0]?.date, last = daily[daily.length - 1]?.date, mid = daily[Math.floor(daily.length / 2)]?.date;

  return (
    <section className="card" aria-labelledby="act-h">
      <div className="row-between" style={{ alignItems: "center" }}>
        <div>
          <h2 id="act-h" className="h2">Posts per day</h2>
          <span className="muted" style={{ fontSize: 12.5 }}>{first && last ? `${shortDate(first)} to ${shortDate(last)} · busiest day highlighted` : ""}</span>
        </div>
        <div role="group" aria-label="Period" className="seg">
          <button type="button" aria-pressed={days === 30} onClick={() => onDays(30)}>30 days</button>
          <button type="button" aria-pressed={days === 90} onClick={() => onDays(90)}>90 days</button>
        </div>
      </div>
      <div className="chart">
        <div className="chart-y" aria-hidden="true">{ticks.map((t, i) => <span key={i}>{t}</span>)}</div>
        <div className="chart-plot">
          {[0, 1, 2].map((i) => <div key={i} className="chart-grid" style={{ top: `${(i * 100) / 3}%` }} />)}
          <div className="chart-bars" role="img"
            aria-label={`Bar chart of posts per day over the last ${days} days. Busiest day: ${shortDate(peak.date)} with ${peak.posts} posts.`}>
            {daily.map((d) => (
              <div key={d.date} className={d === peak ? "bar bar-peak" : "bar"} title={`${shortDate(d.date)}: ${d.posts} posts`}
                style={{ height: `${(d.posts / top) * 100}%` }} data-testid="bar" />
            ))}
          </div>
        </div>
      </div>
      <div className="chart-x muted" aria-hidden="true"><span>{first && shortDate(first)}</span><span>{mid && shortDate(mid)}</span><span>{last && shortDate(last)}</span></div>
      <span style={{ fontSize: 13 }}><b>Busiest day: {shortDate(peak.date)}</b>, {peak.posts} posts.</span>
    </section>
  );
}
