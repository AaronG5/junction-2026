import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Source, SourceFilter } from "../api/types";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { useApi } from "../lib/useApi";

const PLATFORMS: { value: SourceFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "Telegram", label: "Telegram" },
  { value: "Facebook", label: "Facebook" },
  { value: "Website", label: "Websites" },
];

type SortKey = "posts30d" | "originalPct" | "nearCopyPct" | "openFlags";
const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "posts30d", label: "Posts, 30 days" },
  { key: "originalPct", label: "Original" },
  { key: "nearCopyPct", label: "Near copies" },
  { key: "openFlags", label: "Open flags" },
];

/** Channels, groups, pages and sites we collect from. */
export function Sources() {
  const [platform, setPlatform] = useState<SourceFilter>("all");
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: "nearCopyPct", dir: -1 });
  const { data, error, reload } = useApi(() => api.sources(platform), [platform]);

  const rows = [...(data?.sources ?? [])].sort((a, b) => (a[sort.key] - b[sort.key]) * sort.dir);
  const toggleSort = (key: SortKey) =>
    setSort((s) => (s.key === key ? { key, dir: s.dir === 1 ? -1 : 1 } : { key, dir: -1 }));

  return (
    <div className="page">
      <header style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <h1 className="page-title">Sources</h1>
        <p className="muted" style={{ maxWidth: "76ch" }}>
          Channels, groups, pages and sites we have collected. "Original" means a post appeared there first; "near copies"
          are posts almost identical to someone else's. Open a source for its full report.
        </p>
      </header>

      <div role="group" aria-label="Platform" className="fchips">
        {PLATFORMS.map((p) => (
          <button key={p.value} type="button" className="flt" aria-pressed={platform === p.value} onClick={() => setPlatform(p.value)}>
            {p.label}
          </button>
        ))}
      </div>

      {error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : !data ? (
        <Loading label="Loading sources" />
      ) : rows.length === 0 ? (
        <EmptyState>No sources on this platform yet.</EmptyState>
      ) : (
        <section className="card" style={{ padding: 0, overflow: "hidden" }} aria-label="Sources">
          <div style={{ overflowX: "auto" }}>
            <table className="table sources-table">
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Platform</th>
                  {COLUMNS.map((c) => (
                    <th key={c.key} scope="col" aria-sort={sort.key === c.key ? (sort.dir === -1 ? "descending" : "ascending") : "none"}
                      style={c.key === "posts30d" || c.key === "openFlags" ? { textAlign: "right" } : undefined}>
                      <button type="button" className="th-sort" onClick={() => toggleSort(c.key)}>
                        {c.label}
                        <span aria-hidden="true">{sort.key === c.key ? (sort.dir === -1 ? " ↓" : " ↑") : ""}</span>
                      </button>
                    </th>
                  ))}
                  <th scope="col">Main topic</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((s) => <SourceRow key={s.id} source={s} />)}
              </tbody>
            </table>
          </div>
        </section>
      )}
      <p className="muted" style={{ fontSize: 12.5 }}>These numbers describe behaviour, not people. No source is labelled as a bot or as "fake".</p>
    </div>
  );
}

function PctBar({ value }: { value: number }) {
  return (
    <div className="pctbar">
      <div className="meter meter-blue" aria-hidden="true"><div style={{ width: `${value}%` }} /></div>
      <span className="mono num">{value}%</span>
    </div>
  );
}

function SourceRow({ source: s }: { source: Source }) {
  return (
    <tr>
      <td>
        <Link to={`/dashboard/sources/${encodeURIComponent(s.id)}`} className="row-link">{s.name}</Link>
        <br /><small className="muted">{s.kind}</small>
      </td>
      <td>{s.platform}</td>
      <td className="mono num" style={{ textAlign: "right" }}>{s.posts30d.toLocaleString("en-US")}</td>
      <td><PctBar value={s.originalPct} /></td>
      <td><PctBar value={s.nearCopyPct} /></td>
      <td className="mono num" style={{ textAlign: "right" }}>{s.openFlags}</td>
      <td>{s.mainTopic}</td>
    </tr>
  );
}
