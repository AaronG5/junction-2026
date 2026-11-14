import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Claim, Filters, Stance } from "../api/types";
import { FilterBar, FilterSummary } from "../components/FilterBar";
import { EmptyState, ErrorState, Loading } from "../components/States";
import { VerdictPill } from "../components/Verdict";
import { defaultFilters, shortDate } from "../lib/filters";
import { useApi } from "../lib/useApi";
import { useDebounced } from "../lib/useDebounced";
import { useDocumentTitle } from "../lib/useDocumentTitle";

const STANCE_LABEL: Record<Stance, string> = {
  supports: "Supports",
  contradicts: "Contradicts",
  context: "Context",
  neither: "Neither",
};

/** Claims: each statement checked once, however many posts repeat it. */
export function Claims() {
  useDocumentTitle("Claims");
  const defaults = useMemo(() => defaultFilters("30"), []);
  const [filters, setFilters] = useState<Filters>(defaults);
  const [query, setQuery] = useState("");
  const [openId, setOpenId] = useState<number | null>(null);
  const q = useDebounced(query.trim(), 250);
  const { data, error, reload } = useApi(() => api.claims({ ...filters, query: q }), [filters, q]);

  const clearAll = () => {
    setFilters(defaults);
    setQuery("");
  };

  return (
    <div className="page">
      <header className="page-head">
        <div style={{ display: "flex", flexDirection: "column", gap: 6, flex: "1 1 420px", minWidth: 0 }}>
          <h1 className="page-title">Claims</h1>
          <p className="muted" style={{ maxWidth: "72ch" }}>
            Each statement is checked once, however many posts and languages repeat it. Open a claim to see the evidence.
          </p>
        </div>
        <div style={{ flex: "0 1 300px", minWidth: 0, display: "flex", flexDirection: "column", gap: 6 }}>
          <label htmlFor="claim-search" style={{ fontSize: 13, fontWeight: 600 }}>Search claims</label>
          <input id="claim-search" type="search" className="field" value={query} placeholder="e.g. electricity, NATO, elections"
            onChange={(e) => setQuery(e.target.value)} />
        </div>
      </header>

      <FilterBar value={filters} onChange={setFilters} counts={data?.counts} />

      {error ? (
        <ErrorState error={error} onRetry={reload} />
      ) : !data ? (
        <Loading label="Loading claims" />
      ) : (
        <>
          <FilterSummary shown={data.claims.length} total={data.total} noun="claims" value={filters} defaults={defaults}
            onClear={clearAll} extra={q ? `matching “${q}”` : undefined} extraActive={Boolean(q)} />
          <section className="card claims-list" aria-label="Claims">
            {data.claims.length === 0 ? (
              <EmptyState action={<button type="button" className="link-btn" onClick={clearAll}>Clear filters</button>}>
                No claims match. Try a longer time range, another status or a different search.
              </EmptyState>
            ) : (
              data.claims.map((c) => (
                <ClaimRow key={c.id} claim={c} open={openId === c.id} onToggle={() => setOpenId(openId === c.id ? null : c.id)} />
              ))
            )}
          </section>
        </>
      )}
    </div>
  );
}

function ClaimRow({ claim: c, open, onToggle }: { claim: Claim; open: boolean; onToggle: () => void }) {
  const panelId = `claim-${c.id}-details`;
  return (
    <div className="claim-item">
      <button type="button" className="claim-head" aria-expanded={open} aria-controls={panelId} onClick={onToggle}>
        <span className="claim-text">
          <span style={{ fontWeight: 600, fontSize: 15 }}>{c.text}</span>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {c.posts} posts · {c.languages.map((l) => l.toUpperCase()).join(", ")} · {c.platforms.join(" + ")} · {shortDate(c.firstSeen)} to {shortDate(c.lastSeen)}
          </span>
        </span>
        <VerdictPill verdict={c.verdict} />
      </button>
      {open && (
        <div id={panelId} className="claim-body">
          <p style={{ maxWidth: "75ch" }}>{c.explanation}</p>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <span className="lbl">Evidence</span>
            <ul className="evidence">
              {c.evidence.map((e, i) => (
                <li key={i}>
                  <b>{e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.source}</a> : e.source}</b>
                  <span className="stance" data-stance={e.stance}>{STANCE_LABEL[e.stance]}</span>
                  <span className="muted evidence-note">{e.note}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="muted" style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", fontSize: 12.5 }}>
            <span>Checked by {c.checkedBy} · confidence {c.confidence.toFixed(2)} · {shortDate(c.checkedAt)}</span>
            {c.clusterId !== null && <Link to="/dashboard/narratives" style={{ fontWeight: 600 }}>See its narrative</Link>}
          </div>
        </div>
      )}
    </div>
  );
}
