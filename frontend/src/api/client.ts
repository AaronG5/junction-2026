/**
 * API client. In normal mode every call goes to the backend at /api.
 * In demo mode (VITE_DEMO_DATA=true, i.e. `npm run dev:demo`) calls are
 * answered by src/demo instead, which is never loaded otherwise.
 */
import { filtersToParams } from "../lib/filters";
import type {
  ChannelReport, ClaimsResponse, Filters, NarrativesResponse, PostReport,
  SourcesResponse, SourceFilter, Story, SystemStatus, TrendingResponse,
} from "./types";

/** The backend can't be reached, isn't running yet, or answered with something that isn't our API. */
export class BackendUnavailable extends Error {
  constructor(reason = "The Veritas Lens backend is not reachable.") {
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

export const httpApi: Api = {
  narratives: (f) => request("/narratives", filtersToParams(f)),
  claims: (f) => request("/claims", { ...filtersToParams(f), ...(f.query ? { q: f.query } : {}) }),
  sources: (platform) => request("/sources", platform === "all" ? {} : { platform }),
  channel: (id) => request(`/sources/${encodeURIComponent(id)}`),
  analyze: async (url, onStep) => {
    onStep?.(0);
    return request("/analyze", {}, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  },
  trending: (f) => request("/trending", filtersToParams(f)),
  story: (id) => request(`/stories/${encodeURIComponent(id)}`),
  traceLink: (url) => request("/trace", { url }),
  system: () => request("/system"),
};

let demoApi: Promise<Api> | null = null;
function loadDemo(): Promise<Api> {
  // The check is written out here (not via isDemoMode) on purpose: Vite turns it
  // into a constant, so normal builds drop the import and never contain demo data.
  if (import.meta.env.VITE_DEMO_DATA !== "true") {
    return Promise.reject(new Error("Demo data is only available in demo mode."));
  }
  demoApi ??= import("../demo").then((m) => {
    console.info(`Veritas Lens is running in demo mode with example data (${m.DEMO_DATA_MARKER}).`);
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
