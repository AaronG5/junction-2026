/**
 * Demo implementation of the API: answers from data.ts with the same
 * filtering rules the backend must follow. Loaded only in demo mode.
 */
import { ApiError, isSupportedLink, type Api } from "../api/client";
import { ANALYZE_STEPS, type Claim, type Cluster, type MapPoint, type StatusCounts, type Verdict } from "../api/types";
import { matchesPlatform, matchesStatus, overlaps, resolveRange } from "../lib/filters";
import { channelReports, claims, clusters, postReport, run, sources, stories, systemStatus } from "./data";

export { DEMO_DATA_MARKER } from "./data";

/** Simulated network delay, so loading states are visible. Tests set it to 0. */
let delayMs = 250;
export function setDemoDelay(ms: number) {
  delayMs = ms;
}
const wait = (ms = delayMs) => new Promise((r) => setTimeout(r, ms));
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v));

function countStatuses<T>(items: T[], verdictOf: (t: T) => Verdict): StatusCounts {
  const counts: StatusCounts = { all: items.length, true: 0, false: 0, misleading: 0, unverified: 0 };
  for (const it of items) counts[verdictOf(it)] += 1;
  return counts;
}

/** Deterministic pseudo-random map points (stand-in for real UMAP coordinates). */
function mapPoints(visible: Cluster[]): MapPoint[] {
  let seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const gauss = () => Math.sqrt(-2 * Math.log(rnd() || 1e-9)) * Math.cos(2 * Math.PI * rnd());
  const clamp = (v: number) => Math.max(2, Math.min(98, v));
  const points: MapPoint[] = [];
  for (let i = 0; i < 26; i++) points.push({ x: clamp(5 + rnd() * 90), y: clamp(5 + rnd() * 90), clusterId: null });
  const ids = new Set(visible.map((c) => c.id));
  for (const c of clusters) {
    for (let i = 0; i < c.map.n; i++) {
      const p = { x: clamp(c.map.x + gauss() * c.map.sx), y: clamp(c.map.y + gauss() * c.map.sy), clusterId: c.id };
      if (ids.has(c.id)) points.push(p); // generate all to keep positions stable across filters
    }
  }
  return points;
}

function stripMap({ map: _map, ...rest }: (typeof clusters)[number]): Cluster {
  return rest;
}

export const demoApi: Api = {
  async narratives(f) {
    await wait();
    const r = resolveRange(f);
    const base = clusters.filter((c) => matchesPlatform(c.platforms, f.platform) && overlaps(c.firstSeen, c.lastSeen, r));
    const visible = base.filter((c) => matchesStatus(c.status, f.status));
    return clone({
      run,
      clusters: visible.map(stripMap),
      total: clusters.length,
      counts: countStatuses(base, (c) => c.status),
      points: mapPoints(visible),
    });
  },

  async claims(f) {
    await wait();
    const r = resolveRange(f);
    const q = (f.query ?? "").trim().toLowerCase();
    const base = claims.filter((c: Claim) =>
      (!q || c.text.toLowerCase().includes(q)) &&
      matchesPlatform(c.platforms, f.platform) &&
      overlaps(c.firstSeen, c.lastSeen, r));
    return clone({
      claims: base.filter((c) => matchesStatus(c.verdict, f.status)),
      total: claims.length,
      counts: countStatuses(base, (c) => c.verdict),
    });
  },

  async sources(platform) {
    await wait();
    return clone({ sources: sources.filter((s) => platform === "all" || s.platform === platform) });
  },

  async channel(id) {
    await wait();
    const report = channelReports[id];
    if (!report) throw new ApiError(404, "There is no report for this source yet.");
    return clone(report);
  },

  async analyze(url, onStep) {
    if (!isSupportedLink(url)) throw new ApiError(400, "That isn't a Telegram or Facebook link.");
    for (let i = 0; i < ANALYZE_STEPS.length; i++) {
      onStep?.(i);
      await wait(delayMs * 2);
    }
    return clone({ ...postReport, url: url.trim(), analyzedAt: new Date().toISOString() });
  },

  async trending(f) {
    await wait();
    const r = resolveRange(f);
    return clone({
      stories: stories.filter((s) =>
        matchesPlatform([s.platform], f.platform) && matchesStatus(s.status, f.status) && overlaps(s.firstSeen, s.lastSeen, r)),
    });
  },

  async story(id) {
    await wait();
    const s = stories.find((x) => x.id === id);
    if (!s) throw new ApiError(404, "That story isn't available.");
    return clone(s);
  },

  async traceLink(url) {
    await wait();
    if (!isSupportedLink(url)) throw new ApiError(400, "That isn't a Telegram or Facebook link.");
    return clone(stories[0]);
  },

  async system() {
    await wait();
    return clone(systemStatus);
  },
};
