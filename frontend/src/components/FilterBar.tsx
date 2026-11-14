import { useId } from "react";
import type { Filters, PlatformFilter, RangePreset, StatusCounts, StatusFilter } from "../api/types";
import { VERDICT_LABEL, describeFilters, isFiltered, resolveRange } from "../lib/filters";
import { StatusDot } from "./Verdict";

const PLATFORMS: { value: PlatformFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "Telegram", label: "Telegram" },
  { value: "Facebook", label: "Facebook" },
];

export const DEFAULT_RANGES: { value: RangePreset; label: string }[] = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "custom", label: "Custom" },
];

const STATUSES: StatusFilter[] = ["all", "true", "false", "misleading", "unverified"];

interface FilterBarProps {
  value: Filters;
  onChange: (next: Filters) => void;
  /** Counts per status (after platform + date filters). Shown on the status chips. */
  counts?: StatusCounts;
  statusLabel?: string;
  dateLabel?: string;
  ranges?: { value: RangePreset; label: string }[];
  /** Lighter look for the landing page: no card, labels only for screen readers. */
  compact?: boolean;
  platformAllLabel?: string;
  statusAllLabel?: string;
}

export function FilterBar({
  value, onChange, counts, statusLabel = "Status", dateLabel = "Active between",
  ranges = DEFAULT_RANGES, compact = false, platformAllLabel = "All", statusAllLabel = "All",
}: FilterBarProps) {
  const id = useId();
  const set = (patch: Partial<Filters>) => onChange({ ...value, ...patch });
  const range = resolveRange(value);

  const pickRange = (r: RangePreset) => {
    if (r === "custom" && value.range !== "custom") {
      // Start the custom range from what was on screen, so the dates aren't empty.
      set({ range: r, from: range.from, to: range.to });
    } else {
      set({ range: r });
    }
  };

  const groupLabel = (text: string, gid: string) =>
    <span id={gid} className={compact ? "visually-hidden" : "lbl"}>{text}</span>;

  return (
    <section className={compact ? "fbar fbar-compact" : "card fbar"} aria-label="Filters">
      <div className="fgroup" role="group" aria-labelledby={`${id}-p`}>
        {groupLabel("Platform", `${id}-p`)}
        <div className="fchips">
          {PLATFORMS.map((p) => (
            <button key={p.value} type="button" className="flt" aria-pressed={value.platform === p.value} onClick={() => set({ platform: p.value })}>
              {p.value === "all" ? platformAllLabel : p.label}
            </button>
          ))}
        </div>
      </div>

      <div className="fgroup" role="group" aria-labelledby={`${id}-d`}>
        {groupLabel(dateLabel, `${id}-d`)}
        <div className="fchips">
          {ranges.map((r) => (
            <button key={r.value} type="button" className="flt" aria-pressed={value.range === r.value} onClick={() => pickRange(r.value)}>
              {r.label}
            </button>
          ))}
        </div>
        {value.range === "custom" && (
          <div className="fdates">
            <label>
              <span>From</span>
              <input type="date" className="field field-date" value={value.from} max={value.to || undefined}
                onChange={(e) => set({ from: e.target.value })} />
            </label>
            <label>
              <span>To</span>
              <input type="date" className="field field-date" value={value.to} min={value.from || undefined}
                onChange={(e) => set({ to: e.target.value })} />
            </label>
          </div>
        )}
        {range.invalid && <span className="ferror" role="alert">The start date is after the end date.</span>}
      </div>

      <div className="fgroup" role="group" aria-labelledby={`${id}-s`}>
        {groupLabel(statusLabel, `${id}-s`)}
        <div className="fchips">
          {STATUSES.map((s) => (
            <button key={s} type="button" className="flt" aria-pressed={value.status === s} onClick={() => set({ status: s })}>
              {s !== "all" && <StatusDot verdict={s} />}
              {s === "all" ? statusAllLabel : VERDICT_LABEL[s]}
              {counts && <span className="mono muted fcount">{counts[s]}</span>}
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}

/** "4 of 7 claims · Telegram · last 30 days" with a Clear button when filtered. */
export function FilterSummary({ shown, total, noun, value, defaults, onClear, extra, extraActive = false }: {
  shown: number; total: number; noun: string; value: Filters; defaults: Filters; onClear: () => void;
  /** Extra condition shown at the end, e.g. a search: 'matching "nato"'. */
  extra?: string;
  extraActive?: boolean;
}) {
  return (
    <div className="fsummary" aria-live="polite">
      <span><strong>{shown}</strong> of {total} {noun} · {describeFilters(value)}{extra ? ` · ${extra}` : ""}</span>
      {(isFiltered(value, defaults) || extraActive) && (
        <button type="button" className="link-btn" onClick={onClear}>Clear filters</button>
      )}
    </div>
  );
}
