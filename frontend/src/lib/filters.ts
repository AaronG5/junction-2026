/**
 * Filter logic shared by every list (narratives, claims, trending stories).
 * The backend must implement the same rules; its tests reuse these cases.
 *
 * "Active between" = the item was seen at some point inside the range:
 * it overlaps [from, to] (firstSeen <= to and lastSeen >= from).
 */
import type { Filters, Platform, PlatformFilter, RangePreset, StatusFilter, Verdict } from "../api/types";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Today's date as "YYYY-MM-DD" in UTC. */
export function todayIso(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/** iso + days (negative = earlier). */
export function shiftDays(iso: string, days: number): string {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

/** "2026-11-02" -> "2 Nov" */
export function shortDate(iso: string): string {
  const [, m, d] = iso.split("-").map(Number);
  return `${d} ${MONTHS[m - 1]}`;
}

export function defaultFilters(range: RangePreset = "30", today: string = todayIso()): Filters {
  return { platform: "all", range, from: presetFrom(range === "custom" ? "30" : range, today), to: today, status: "all" };
}

function presetFrom(range: Exclude<RangePreset, "custom">, today: string): string {
  return shiftDays(today, -Number(range));
}

export interface ResolvedRange {
  from: string;
  to: string;
  /** Custom range whose start is after its end. Filtering by date is then skipped. */
  invalid: boolean;
}

export function resolveRange(f: Pick<Filters, "range" | "from" | "to">, today: string = todayIso()): ResolvedRange {
  if (f.range !== "custom") return { from: presetFrom(f.range, today), to: today, invalid: false };
  const invalid = Boolean(f.from && f.to && f.from > f.to);
  return { from: f.from, to: f.to, invalid };
}

/** An empty from/to (half-filled custom range) means no limit on that side. */
export function overlaps(firstSeen: string, lastSeen: string, r: ResolvedRange): boolean {
  if (r.invalid) return true;
  return (!r.from || lastSeen >= r.from) && (!r.to || firstSeen <= r.to);
}

export function matchesPlatform(platforms: Platform[], p: PlatformFilter): boolean {
  return p === "all" || platforms.includes(p);
}

export function matchesStatus(v: Verdict, s: StatusFilter): boolean {
  return s === "all" || v === s;
}

/** True when anything differs from the given defaults. */
export function isFiltered(f: Filters, defaults: Filters): boolean {
  return f.platform !== defaults.platform || f.range !== defaults.range || f.status !== defaults.status;
}

/** Query parameters for the API. Presets are resolved to dates. */
export function filtersToParams(f: Filters, today: string = todayIso()): Record<string, string> {
  const r = resolveRange(f, today);
  const params: Record<string, string> = {};
  if (f.platform !== "all") params.platform = f.platform;
  if (!r.invalid) {
    if (r.from) params.from = r.from;
    if (r.to) params.to = r.to;
  }
  if (f.status !== "all") params.status = f.status;
  return params;
}

export const VERDICT_LABEL: Record<Verdict, string> = {
  true: "True",
  false: "False",
  misleading: "Misleading",
  unverified: "Unverified",
};

/** "Telegram · last 30 days · false" */
export function describeFilters(f: Filters, today: string = todayIso()): string {
  const r = resolveRange(f, today);
  const range = f.range !== "custom" ? `last ${f.range} days`
    : r.from && r.to ? `${shortDate(r.from)} to ${shortDate(r.to)}`
    : r.from ? `from ${shortDate(r.from)}`
    : r.to ? `until ${shortDate(r.to)}`
    : "any time";
  const parts = [f.platform === "all" ? "all platforms" : f.platform, range];
  if (f.status !== "all") parts.push(VERDICT_LABEL[f.status].toLowerCase());
  return parts.join(" · ");
}
