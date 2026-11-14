import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Cluster, Filters, MapPoint } from "../api/types";
import { FilterBar, FilterSummary } from "../components/FilterBar";
import { LanguageBar } from "../components/LanguageBar";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { StatusDot } from "../components/Verdict";
import { VERDICT_LABEL, defaultFilters, shortDate } from "../lib/filters";
import { useApi } from "../lib/useApi";

/** Dashboard home: posts grouped by what they say, in any language. */
export function Narratives() {
  const defaults = useMemo(() => defaultFilters("7"), []);
  const [filters, setFilters] = useState<Filters>(defaults);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const { data, error, loading, reload } = useApi(() => api.narratives(filters), [filters]);

  const clusters = data?.clusters ?? [];
  // Keep the chosen cluster while it's visible; otherwise fall back to the largest.
  const selected = clusters.find((c) => c.id === selectedId) ?? clusters[0] ?? null;
  const clear = () => setFilters(defaults);

  return (
    <div className="page">
      <header className="page-head">
        <div style={{ display: "flex", flexDirection: "column", gap: 6, flex: "1 1 480px", minWidth: 0 }}>
          <span className="lbl">What's spreading now</span>
          <h1 className="page-title">Narratives</h1>
          <p className="muted" style={{ maxWidth: "70ch" }}>
            Posts grouped automatically by what they say, in any language. Nobody reads them first: you pick a group,
            then read its most typical posts and its edge cases.
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-end" }}>
          <Link to="/dashboard/analyze" className="btn">Analyze a link</Link>
          {data && (
            <span className="mono muted" style={{ fontSize: 12.5 }}>
              Latest run {shortDate(data.run.date)} · {data.run.posts.toLocaleString("en-US")} posts · {data.run.clusters} clusters
            </span>
          )}
        </div>
      </header>

      <FilterBar value={filters} onChange={setFilters} counts={data?.counts} statusLabel="Status of its main claim" />

      {error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : !data ? (
        <Loading label="Loading narratives" />
      ) : (
        <>
          <FilterSummary shown={clusters.length} total={data.total} noun="large clusters" value={filters} defaults={defaults} onClear={clear} />

          <div className="split" aria-busy={loading}>
            <section className="card" aria-labelledby="map-h" style={{ flex: "999 1 520px" }}>
              <div className="row-between">
                <h2 id="map-h" className="h2">Cluster map</h2>
                <span className="muted" style={{ fontSize: 12.5 }}>Close dots say similar things</span>
              </div>
              <ClusterMap points={data.points} clusters={clusters} selected={selected} />
            </section>

            <section className="card cluster-list" aria-labelledby="list-h">
              <h2 id="list-h" className="h2" style={{ padding: "6px 12px 8px" }}>Largest clusters</h2>
              {clusters.length === 0 ? (
                <EmptyState action={<button type="button" className="link-btn" onClick={clear}>Clear filters</button>}>
                  No clusters match these filters.
                </EmptyState>
              ) : (
                clusters.map((c) => (
                  <button key={c.id} type="button" className="crow" aria-pressed={selected?.id === c.id} onClick={() => setSelectedId(c.id)}>
                    <span className="crow-name">{c.name}</span>
                    <span className="mono num crow-size">{c.size.toLocaleString("en-US")}</span>
                    <span className="crow-meta">
                      <StatusDot verdict={c.status} />
                      {VERDICT_LABEL[c.status]} · {c.platforms.join(" + ")} · {c.accounts} accounts
                    </span>
                    <span className="crow-change num">{changeText(c)}</span>
                  </button>
                ))
              )}
              <span className="muted" style={{ padding: "8px 12px 4px", fontSize: 12.5 }}>
                {data.run.clusters - data.total > 0 && `+ ${data.run.clusters - data.total} smaller clusters · `}
                {data.run.unclustered.toLocaleString("en-US")} posts fit no cluster
              </span>
            </section>
          </div>

          {selected && <ClusterDetail cluster={selected} />}
        </>
      )}
    </div>
  );
}

function changeText(c: Cluster): string {
  if (c.changePct === null) return "new this week";
  return `${c.changePct > 0 ? "+" : c.changePct < 0 ? "−" : ""}${Math.abs(c.changePct)}% vs last week`;
}

function ClusterMap({ points, clusters, selected }: { points: MapPoint[]; clusters: Cluster[]; selected: Cluster | null }) {
  // Label each cluster at the middle of its dots.
  const labels = useMemo(() => {
    const sums = new Map<number, { x: number; y: number; n: number; minY: number }>();
    for (const p of points) {
      if (p.clusterId === null) continue;
      const s = sums.get(p.clusterId) ?? { x: 0, y: 0, n: 0, minY: 100 };
      sums.set(p.clusterId, { x: s.x + p.x, y: s.y + p.y, n: s.n + 1, minY: Math.min(s.minY, p.y) });
    }
    return clusters.flatMap((c) => {
      const s = sums.get(c.id);
      return s ? [{ id: c.id, name: c.name, x: s.x / s.n, y: Math.max(4, s.minY - 4) }] : [];
    });
  }, [points, clusters]);

  const description = selected
    ? `Map of ${clusters.length} clusters. Selected: ${selected.name}, ${selected.size} posts.`
    : "Map: no clusters match the filters.";

  return (
    <>
      <div className="cmap" role="img" aria-label={description}>
        {points.map((p, i) => {
          const kind = p.clusterId === null ? "noise" : p.clusterId === selected?.id ? "selected" : "other";
          return <span key={i} className={`pt pt-${kind}`} data-kind={kind} style={{ left: `${p.x}%`, top: `${p.y}%` }} />;
        })}
        {labels.map((l) => (
          <span key={l.id} className={`maptag ${l.id === selected?.id ? "maptag-on " : ""}${l.x < 30 ? "maptag-left" : l.x > 70 ? "maptag-right" : ""}`}
            style={{ left: `${l.x}%`, top: `${l.y}%` }} aria-hidden="true">
            {l.id === selected?.id ? l.name : `#${l.id}`}
          </span>
        ))}
      </div>
      <div className="legend">
        <span><i style={{ background: "var(--chart-1)" }} aria-hidden="true" />Selected cluster</span>
        <span><i style={{ background: "var(--other)" }} aria-hidden="true" />Other clusters</span>
        <span><i style={{ background: "var(--noise)", outline: "1px solid var(--other)" }} aria-hidden="true" />Fits no cluster</span>
      </div>
    </>
  );
}

function ClusterDetail({ cluster: c }: { cluster: Cluster }) {
  return (
    <section className="card" aria-labelledby="sel-h" style={{ gap: 18 }}>
      <div className="row-between" style={{ alignItems: "flex-start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0, flex: "1 1 420px" }}>
          <span className="lbl" style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
            Cluster {c.id} · <StatusDot verdict={c.status} /> Main claim {VERDICT_LABEL[c.status].toLowerCase()}
          </span>
          <h2 id="sel-h" style={{ fontSize: 22 }}>{c.name}</h2>
          <span className="muted" style={{ fontSize: 13 }}>
            {c.size.toLocaleString("en-US")} posts from {c.accounts} accounts · {c.platforms.join(" + ")} · active {shortDate(c.firstSeen)} to{" "}
            {shortDate(c.lastSeen)} · {changeText(c)}
          </span>
          <span className="muted" style={{ fontSize: 12.5 }}>Name suggested by Gemma from its 5 most typical posts</span>
        </div>
        <Link to="/dashboard/claims" className="btn btn-ghost">See its claims</Link>
      </div>

      <LanguageBar languages={c.languages} />

      <div className="split">
        <div className="post-col">
          <div>
            <h3 style={{ fontSize: 15 }}>Most typical posts</h3>
            <span className="muted" style={{ fontSize: 12.5 }}>Closest to the cluster's centre. Read these to know what it's about.</span>
          </div>
          {c.typical.map((p) => (
            <article key={p.id} className="postcard">
              <span style={{ fontWeight: 500 }}>{p.text}</span>
              <span className="muted" style={{ fontSize: 12 }}>{p.author} · {p.where}</span>
            </article>
          ))}
        </div>
        <div className="post-col">
          <div>
            <h3 style={{ fontSize: 15 }}>Edge cases</h3>
            <span className="muted" style={{ fontSize: 12.5 }}>Farthest from the centre but still inside. Often where a story is being reframed.</span>
          </div>
          {c.edge.map((p) => (
            <article key={p.id} className="postcard postcard-edge">
              <span style={{ fontWeight: 500 }}>{p.text}</span>
              <span className="muted" style={{ fontSize: 12 }}>{p.author} · {p.where}</span>
              <span style={{ fontSize: 12.5, color: "var(--accent-soft-text)" }}>{p.note}</span>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
