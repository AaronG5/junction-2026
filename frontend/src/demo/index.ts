/**
 * Demo implementation of the API: answers from data.ts with the same
 * filtering rules the backend must follow. Loaded only in demo mode.
 */
import { CLEAN_EXAMPLE } from "../components/NarrativeCheck";
import { ApiError, isSupportedLink, linkKey, textId, type Api } from "../api/client";
import {
  ANALYZE_STEPS, type AppSettings, type Claim, type ClusteringState, type Cluster, type MapPoint, type Source, type StatusCounts, type Story, type Verdict,
} from "../api/types";
import { matchesPlatform, matchesQuery, matchesStatus, overlaps, resolveRange } from "../lib/filters";
import {
  channelReports, claims, clusters, narrativeClean, narrativeFlagged, narrativeStatus, postReport, run, sources, stories, systemStatus,
} from "./data";

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

/**
 * Deterministic pseudo-random map points (stand-in for real UMAP coordinates).
 * Contract details the backend must match (B3): coordinates have 2 decimals
 * (stored as DECIMAL(5,2)); order = posts that fit no cluster first, then each
 * matching cluster's points, clusters in the same order as the cluster list.
 */
function mapPoints(visible: Cluster[]): MapPoint[] {
  let seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const gauss = () => Math.sqrt(-2 * Math.log(rnd() || 1e-9)) * Math.cos(2 * Math.PI * rnd());
  const clamp = (v: number) => Math.round(Math.max(2, Math.min(98, v)) * 100) / 100;
  const points: MapPoint[] = [];
  for (let i = 0; i < 26; i++) points.push({ x: clamp(5 + rnd() * 90), y: clamp(5 + rnd() * 90), clusterId: null });
  // Generate every cluster's points (in data order) so positions stay stable across filters,
  // then emit only the visible clusters', in list order.
  const byCluster = new Map<number, MapPoint[]>();
  for (const c of clusters) {
    const own: MapPoint[] = [];
    for (let i = 0; i < c.map.n; i++) {
      own.push({ x: clamp(c.map.x + gauss() * c.map.sx), y: clamp(c.map.y + gauss() * c.map.sy), clusterId: c.id });
    }
    byCluster.set(c.id, own);
  }
  for (const c of visible) points.push(...(byCluster.get(c.id) ?? []));
  return points;
}

function stripMap({ map: _map, ...rest }: (typeof clusters)[number]): Cluster {
  return rest;
}

/**
 * List orders, the same as the backend's (B3). Ties are broken by id, compared
 * as plain code-unit strings (the same in Python and JavaScript).
 */
const byId = (a: string | number, b: string | number) => (a < b ? -1 : a > b ? 1 : 0);
/** Largest cluster first. */
export const sortClusters = <T extends Cluster>(list: T[]) => [...list].sort((a, b) => b.size - a.size || byId(a.id, b.id));
/** Most recently seen claim first. */
export const sortClaims = (list: Claim[]) => [...list].sort((a, b) => byId(b.lastSeen, a.lastSeen) || byId(a.id, b.id));
/** Most near copies first (the Sources table's default sort). */
export const sortSources = (list: Source[]) => [...list].sort((a, b) => b.nearCopyPct - a.nearCopyPct || byId(a.id, b.id));
/** Most posts first. */
export const sortStories = (list: Story[]) => [...list].sort((a, b) => b.trend.posts - a.trend.posts || byId(a.id, b.id));

/** Everything the backend's demo loader needs (exported to shared/demo/demo-data.json). */
export function demoSeed() {
  const all = sortClusters(clusters);
  return {
    run,
    clusters: all.map(stripMap),
    points: mapPoints(all),
    claims: sortClaims(claims),
    sources: sortSources(sources),
    channelReports,
    stories: sortStories(stories),
  };
}

export const demoApi: Api = {
  async narratives(f) {
    await wait();
    const r = resolveRange(f);
    const base = sortClusters(clusters).filter((c) => matchesPlatform(c.platforms, f.platform) && overlaps(c.firstSeen, c.lastSeen, r));
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
    const base = sortClaims(claims).filter((c: Claim) =>
      matchesQuery(c.text, f.query ?? "") &&
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
    return clone({ sources: sortSources(sources).filter((s) => platform === "all" || s.platform === platform) });
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
      stories: sortStories(stories).filter((s) =>
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
    // Like the backend: the story whose post is this link. Demo only: any other
    // link shows the first example story instead of "not collected yet".
    const key = linkKey(url);
    return clone(stories.find((s) => s.post.url && linkKey(s.post.url) === key) ?? stories[0]);
  },

  async system() {
    await wait();
    return clone(systemStatus);
  },

  async appSettings() {
    await wait();
    return clone(demoSettings);
  },

  async saveAppSettings(s) {
    await wait();
    if (s.dataMode === "local" && s.shareAnalyses) throw new ApiError(400, SHARING_WHILE_LOCAL);
    demoSettings = clone(s);
    return clone(demoSettings);
  },

  async clusteringState() {
    await wait();
    return clone(demoClustering);
  },

  async startClustering() {
    await wait();
    // The example clusters are fixed; a run "finishes" at once and changes nothing.
    const now = new Date().toISOString().slice(0, 19) + "Z";
    demoClustering = { status: "done", message: DEMO_CLUSTERING, mode: "update", days: null, startedAt: now, finishedAt: now };
    return clone(demoClustering);
  },

  async checkText(text, onUpdate) {
    await wait();
    const done = { ...clone(demoNarrativeFor(text)), id: textId(text) };
    if (done.content?.label === "potentially_propaganda") {
      // Like the backend: the fast-path fields first, the judge's verdict later.
      onUpdate?.({ ...done, status: "pending", content: { ...done.content, label: "none", score: 0, techniques: [], reason: "" } });
      await wait(delayMs * 4);
    }
    onUpdate?.(clone(done));
    return done;
  },

  async narrativeStatus() {
    await wait();
    return clone(narrativeStatus);
  },
};

/**
 * Demo only: texts that sound like the flagged example get its result, everything else the clean one.
 * The highlighted quotes only line up with the example text itself.
 */
export const DEMO_FLAG_WORDS = /puppet|kyiv regime|nato'?s war|biolab|nazi|марионет|киевск/i;
function demoNarrativeFor(text: string) {
  return text !== CLEAN_EXAMPLE && DEMO_FLAG_WORDS.test(text) ? narrativeFlagged : narrativeClean;
}

export const DEMO_CLUSTERING = "Demo mode: the example clusters stay as they are.";
let demoClustering: ClusteringState = { status: "idle", message: null, mode: null, days: null, startedAt: null, finishedAt: null };

/** The backend's rule and message (backend/veritas/app_settings.py). */
export const SHARING_WHILE_LOCAL = "Sharing can't be turned on while Veritas Lens works only on this computer. Choose Hybrid or Central server first.";
let demoSettings: AppSettings = { dataMode: "hybrid", shareAnalyses: false };
/** Test helper. */
export function resetDemoSettings() {
  demoSettings = { dataMode: "hybrid", shareAnalyses: false };
  demoClustering = { status: "idle", message: null, mode: null, days: null, startedAt: null, finishedAt: null };
}
