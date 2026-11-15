/**
 * API client. In normal mode every call goes to the backend at /api.
 * In demo mode (VITE_DEMO_DATA=true, i.e. `npm run dev:demo`) calls are
 * answered by src/demo instead, which is never loaded otherwise.
 */
import { filtersToParams } from "../lib/filters";
import type {
  AnalysisJob, AppSettings, ChannelReport, ClusteringState, ClaimsResponse, Filters, NarrativeCheckStatus, NarrativeResult,
  NarrativesResponse, PostReport, SourcesResponse, SourceFilter, Story, SystemStatus, TrendingResponse,
} from "./types";

/** The backend can't be reached, isn't running yet, or answered with something that isn't our API. */
export class BackendUnavailable extends Error {
  constructor(reason = "The Providence backend is not reachable.") {
    super(reason);
    this.name = "BackendUnavailable";
  }
}

/** The backend answered with a real error (e.g. 400 invalid link, 404 unknown source). */
export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

/** Everything the UI can ask for. Implemented by the HTTP client and by the demo data. */
export interface Api {
  narratives(f: Filters): Promise<NarrativesResponse>;
  claims(f: Filters & { query?: string }): Promise<ClaimsResponse>;
  sources(platform: SourceFilter): Promise<SourcesResponse>;
  channel(id: string): Promise<ChannelReport>;
  analyze(url: string, onStep?: (stepIndex: number) => void): Promise<PostReport>;
  trending(f: Filters): Promise<TrendingResponse>;
  story(id: string): Promise<Story>;
  traceLink(url: string): Promise<Story>;
  system(): Promise<SystemStatus>;
  appSettings(): Promise<AppSettings>;
  saveAppSettings(s: AppSettings): Promise<AppSettings>;
  clusteringState(): Promise<ClusteringState>;
  /** Add the posts that aren't clustered yet; resolves at once with the running state. */
  startClustering(): Promise<ClusteringState>;
  /**
   * Run the narrative check on any text. Resolves with the final result; onUpdate
   * gets each answer as it arrives (first the fast-path fields, status "pending").
   */
  checkText(text: string, onUpdate?: (r: NarrativeResult) => void): Promise<NarrativeResult>;
  narrativeStatus(): Promise<NarrativeCheckStatus>;
}

/** Read at call time so tests can switch it; Vite still replaces it with a constant in builds. */
export function isDemoMode(): boolean {
  return import.meta.env.VITE_DEMO_DATA === "true";
}

async function request<T>(path: string, params: Record<string, string> = {}, init?: RequestInit): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  const url = `/api${path}${qs ? `?${qs}` : ""}`;
  let res: Response;
  try {
    res = await fetch(url, { headers: { Accept: "application/json" }, ...init });
  } catch {
    throw new BackendUnavailable();
  }
  const isJson = (res.headers.get("content-type") || "").includes("application/json");
  // No JSON = not our API (dev-server error page, proxy failure, HTML fallback).
  if (!isJson) throw new BackendUnavailable();
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new BackendUnavailable("The backend sent a response that could not be read.");
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown })?.detail;
    throw new ApiError(res.status, typeof detail === "string" ? detail : `Request failed (${res.status}).`);
  }
  return body as T;
}

/** How often a running analysis is checked, and for how long at most. Tests shorten these. */
export const analysisPolling = { intervalMs: 1000, maxWaitMs: 20 * 60 * 1000 };

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Start an analysis, then poll it until it's done; report each step as it changes. */
async function analyzeOverHttp(url: string, onStep?: (stepIndex: number) => void): Promise<PostReport> {
  let job = await request<AnalysisJob>("/analyze", {}, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  const started = Date.now();
  let lastStep = -1;
  for (;;) {
    if (job.status === "done" && job.report) return job.report;
    if (job.status === "failed") throw new ApiError(job.errorStatus ?? 500, job.error ?? "The analysis failed.");
    if (job.step !== lastStep && job.step < 5) {
      lastStep = job.step;
      onStep?.(job.step);
    }
    if (Date.now() - started > analysisPolling.maxWaitMs) {
      throw new ApiError(504, "The analysis is taking too long. Try again later.");
    }
    await sleep(analysisPolling.intervalMs);
    job = await request<AnalysisJob>(`/analyze/${encodeURIComponent(job.id)}`);
  }
}

/** How often a pending narrative check is polled, and for how long at most. Tests shorten these. */
export const narrativePolling = { intervalMs: 1500, maxWaitMs: 60 * 1000 };

/** Longest text sent (the backend's limit per item). */
export const MAX_CHECK_TEXT = 10000;

export const NARRATIVE_UNAVAILABLE = "The narrative check isn't available on this server right now.";
export const NARRATIVE_TIMEOUT = "The check didn't finish within a minute. The results so far are shown. Try again later for the full verdict.";

/** A stable id for a text, as the extension API wants it ([A-Za-z0-9]{1,40}): the same text always gets the same id. */
export function textId(text: string): string {
  // cyrb53-style: two 32-bit FNV/murmur mixes, written as hex.
  let h1 = 0xdeadbeef ^ text.length, h2 = 0x41c6ce57 ^ text.length;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 2654435761);
    h2 = Math.imul(h2 ^ c, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return "dash" + (h1 >>> 0).toString(16).padStart(8, "0") + (h2 >>> 0).toString(16).padStart(8, "0");
}

/** /api/ext/* errors in plain words: 404 = the module isn't deployed, 503 = it can't run (e.g. numpy missing). */
async function extRequest<T>(path: string, params: Record<string, string> = {}, init?: RequestInit): Promise<T> {
  try {
    return await request<T>(`/ext${path}`, params, init);
  } catch (err) {
    if (err instanceof ApiError && (err.status === 404 || err.status === 503)) throw new ApiError(err.status, NARRATIVE_UNAVAILABLE);
    throw err;
  }
}

function itemResult(results: NarrativeResult[] | undefined, id: string): NarrativeResult {
  const r = results?.find((x) => x.id === id);
  if (!r) throw new ApiError(502, "The narrative check sent an answer that could not be read.");
  if (r.status === "error") throw new ApiError(503, r.error ? `The check failed: ${r.error}` : NARRATIVE_UNAVAILABLE);
  return r;
}

/** Thrown when polling gives up; carries the last (pending) answer so the fast-path fields can stay on screen. */
export class CheckTimeout extends ApiError {
  constructor(public partial: NarrativeResult) {
    super(504, NARRATIVE_TIMEOUT);
    this.name = "CheckTimeout";
  }
}

async function checkTextOverHttp(text: string, onUpdate?: (r: NarrativeResult) => void): Promise<NarrativeResult> {
  const body = text.slice(0, MAX_CHECK_TEXT);
  const id = textId(body);
  const first = await extRequest<{ results: NarrativeResult[] }>("/batch", {}, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ client: "dashboard", items: [{ id, author: "", text: body }] }),
  });
  let r = itemResult(first.results, id);
  const started = Date.now();
  while (r.status === "pending") {
    onUpdate?.(r);
    if (Date.now() - started >= narrativePolling.maxWaitMs) throw new CheckTimeout(r);
    await sleep(narrativePolling.intervalMs);
    const next = await extRequest<{ results: NarrativeResult[] }>("/result", { ids: id });
    r = itemResult(next.results, id);
  }
  onUpdate?.(r);
  return r;
}

export const httpApi: Api = {
  narratives: (f) => request("/narratives", filtersToParams(f)),
  claims: (f) => request("/claims", { ...filtersToParams(f), ...(f.query ? { q: f.query } : {}) }),
  sources: (platform) => request("/sources", platform === "all" ? {} : { platform }),
  channel: (id) => request(`/sources/${encodeURIComponent(id)}`),
  analyze: analyzeOverHttp,
  trending: (f) => request("/trending", filtersToParams(f)),
  story: (id) => request(`/stories/${encodeURIComponent(id)}`),
  traceLink: (url) => request("/trace", { url }),
  system: () => request("/system"),
  appSettings: () => request("/settings"),
  saveAppSettings: (s) => request("/settings", {}, {
    method: "PUT",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(s),
  }),
  clusteringState: () => request("/clustering"),
  startClustering: () => request("/clustering", {}, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({}),
  }),
  checkText: checkTextOverHttp,
  narrativeStatus: () => extRequest("/status"),
};

let demoApi: Promise<Api> | null = null;
function loadDemo(): Promise<Api> {
  // The check is written out here (not via isDemoMode) on purpose: Vite turns it
  // into a constant, so normal builds drop the import and never contain demo data.
  if (import.meta.env.VITE_DEMO_DATA !== "true") {
    return Promise.reject(new Error("Demo data is only available in demo mode."));
  }
  demoApi ??= import("../demo").then((m) => {
    console.info(`Providence is running in demo mode with example data (${m.DEMO_DATA_MARKER}).`);
    return m.demoApi;
  });
  return demoApi;
}

/** Demo mode only: start loading the example data right away. */
export function preloadDemo() {
  if (isDemoMode()) void loadDemo();
}

function pick(): Promise<Api> {
  return isDemoMode() ? loadDemo() : Promise.resolve(httpApi);
}

/** The API the app uses: demo data in demo mode, the backend otherwise. */
export const api: Api = {
  narratives: (f) => pick().then((a) => a.narratives(f)),
  claims: (f) => pick().then((a) => a.claims(f)),
  sources: (p) => pick().then((a) => a.sources(p)),
  channel: (id) => pick().then((a) => a.channel(id)),
  analyze: (url, onStep) => pick().then((a) => a.analyze(url, onStep)),
  trending: (f) => pick().then((a) => a.trending(f)),
  story: (id) => pick().then((a) => a.story(id)),
  traceLink: (url) => pick().then((a) => a.traceLink(url)),
  system: () => pick().then((a) => a.system()),
  appSettings: () => pick().then((a) => a.appSettings()),
  saveAppSettings: (s) => pick().then((a) => a.saveAppSettings(s)),
  clusteringState: () => pick().then((a) => a.clusteringState()),
  startClustering: () => pick().then((a) => a.startClustering()),
  checkText: (text, onUpdate) => pick().then((a) => a.checkText(text, onUpdate)),
  narrativeStatus: () => pick().then((a) => a.narrativeStatus()),
};

/** Telegram or Facebook link to a post, channel, group or page. */
/**
 * The rule is spelled out character by character (no \s, \S or trim()), so the
 * backend's copy in backend/veritas/links.py behaves identically: same spaces
 * trimmed, ASCII-only case folding, and the rest of the link must be printable
 * ASCII up to the end. Shared cases: shared/cases/links.json.
 */
const LINK_SPACE = "[ \t\n\r\f\v\u00a0\ufeff]";
const TRIM = new RegExp(`^${LINK_SPACE}+|${LINK_SPACE}+$`, "g");
const LINK = /^(https?:\/\/)?(t\.me|telegram\.me|(www\.|m\.)?facebook\.com|fb\.com)\/[!-~]+$/i;

export function isSupportedLink(value: string): boolean {
  return LINK.test(value.replace(TRIM, ""));
}

const HOST_ALIASES: Record<string, string> = { "telegram.me": "t.me", "fb.com": "facebook.com" };

/**
 * One spelling per link, for looking it up: spaces trimmed, no http(s)://, no
 * www. or m., telegram.me = t.me, fb.com = facebook.com, no #part, no trailing
 * slash, ASCII lowercase, and no ?query (tracking) except on .php pages such as
 * Facebook's story.php?story_fbid=…, where the query says which post it is.
 * "HTTPS://T.me/Kanal/5/?x=1" -> "t.me/kanal/5".
 * Same as link_key() in backend/veritas/links.py (shared cases: links.json "keys").
 */
export function linkKey(value: string): string {
  let s = value.replace(TRIM, "").replace(/^https?:\/\//i, "");
  const hash = s.indexOf("#");
  if (hash >= 0) s = s.slice(0, hash);
  const q = s.indexOf("?");
  let query = "";
  if (q >= 0) {
    query = s.slice(q);
    s = s.slice(0, q);
  }
  s = s.replace(/\/+$/, "");
  if (/\.php$/i.test(s) && query.length > 1) s += query;
  s = s.replace(/[A-Z]/g, (c) => c.toLowerCase());
  const slash = s.indexOf("/");
  let host = slash >= 0 ? s.slice(0, slash) : s;
  const path = slash >= 0 ? s.slice(slash) : "";
  host = host.replace(/^(www\.|m\.)/, "");
  return (HOST_ALIASES[host] ?? host) + path;
}
