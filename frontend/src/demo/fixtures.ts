/**
 * Builds the shared contract fixtures (shared/fixtures/*.json) from the demo data.
 *
 * Each fixture is exactly what the frontend's demo mode answers for one API call.
 * The manifest records the call (method, path, query, body) and the response type,
 * so the backend can validate the files (B1) and replay the same calls (B3).
 *
 * MUST run with the clock frozen at FIXTURE_NOW (the demo data's dates are
 * relative to "today"): scripts/export-fixtures.ts and fixtures.test.ts do that.
 */
import { ApiError } from "../api/client";
import { filtersToParams, defaultFilters } from "../lib/filters";
import type { Filters } from "../api/types";

export const FIXTURE_NOW = "2026-11-14T12:00:00.000Z";
export const FIXTURE_TODAY = FIXTURE_NOW.slice(0, 10);

export interface FixtureEntry {
  file: string;
  /** Name of the response model on both sides, e.g. "NarrativesResponse"; "Error" for {detail}. */
  model: string;
  method: "GET" | "POST";
  path: string;
  query: Record<string, string>;
  body?: Record<string, string>;
  /** HTTP status the backend must answer with. */
  status: number;
  /**
   * "exact": the real backend, loaded with the demo data, must return equal JSON (B3).
   * "shape": only the model must validate (live status, AI output, timestamps).
   */
  replay: "exact" | "shape";
}

export interface Fixtures {
  manifest: { generatedAt: string; today: string; note: string; fixtures: FixtureEntry[] };
  files: Record<string, unknown>;
  /** All demo data, for the backend's demo loader: shared/demo/demo-data.json. */
  seed: unknown;
}

const f = (patch: Partial<Filters> & { range?: Filters["range"] } = {}): Filters => ({ ...defaultFilters(patch.range ?? "30", FIXTURE_TODAY), ...patch });

export async function buildFixtures(): Promise<Fixtures> {
  if (new Date().toISOString() !== FIXTURE_NOW) {
    throw new Error(`buildFixtures() needs the clock frozen at ${FIXTURE_NOW}`);
  }
  const { demoApi, demoSeed, setDemoDelay } = await import("./index");
  setDemoDelay(0);

  const entries: FixtureEntry[] = [];
  const files: Record<string, unknown> = {};
  const add = async (
    file: string, model: string, method: "GET" | "POST", path: string, query: Record<string, string>,
    call: () => Promise<unknown>, opts: { body?: Record<string, string>; replay?: "exact" | "shape" } = {},
  ) => {
    let status = 200;
    let data: unknown;
    try {
      data = await call();
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      status = err.status;
      data = { detail: err.message };
    }
    entries.push({ file, model, method, path, query, ...(opts.body ? { body: opts.body } : {}), status, replay: opts.replay ?? "exact" });
    files[file] = data;
  };

  const nar7 = f({ range: "7" });
  const nar90False = f({ range: "90", status: "false" });
  const narFb = f({ range: "7", platform: "Facebook" });
  await add("narratives-7d.json", "NarrativesResponse", "GET", "/api/narratives", filtersToParams(nar7, FIXTURE_TODAY), () => demoApi.narratives(nar7));
  await add("narratives-90d-false.json", "NarrativesResponse", "GET", "/api/narratives", filtersToParams(nar90False, FIXTURE_TODAY), () => demoApi.narratives(nar90False));
  await add("narratives-7d-facebook.json", "NarrativesResponse", "GET", "/api/narratives", filtersToParams(narFb, FIXTURE_TODAY), () => demoApi.narratives(narFb));

  const cl30 = f({ range: "30" });
  const clTg = f({ range: "30", platform: "Telegram" });
  await add("claims-30d.json", "ClaimsResponse", "GET", "/api/claims", filtersToParams(cl30, FIXTURE_TODAY), () => demoApi.claims(cl30));
  await add("claims-30d-telegram-nato.json", "ClaimsResponse", "GET", "/api/claims", { ...filtersToParams(clTg, FIXTURE_TODAY), q: "nato" }, () => demoApi.claims({ ...clTg, query: "nato" }));

  await add("sources-all.json", "SourcesResponse", "GET", "/api/sources", {}, () => demoApi.sources("all"));
  await add("sources-facebook.json", "SourcesResponse", "GET", "/api/sources", { platform: "Facebook" }, () => demoApi.sources("Facebook"));
  await add("source-kanalas_b.json", "ChannelReport", "GET", "/api/sources/kanalas_b", {}, () => demoApi.channel("kanalas_b"));

  // The backend answers POST /api/analyze with a job; a post analyzed before comes back finished, like this.
  const url = "https://t.me/kanalas_b/4521";
  await add("analyze-kanalas_b-4521.json", "AnalysisJob", "POST", "/api/analyze", {}, async () => ({
    id: "demo-analysis", status: "done", step: 5, report: await demoApi.analyze(url), error: null, errorStatus: null,
  }), { body: { url }, replay: "shape" });

  const tr7 = f({ range: "7" });
  await add("trending-7d.json", "TrendingResponse", "GET", "/api/trending", filtersToParams(tr7, FIXTURE_TODAY), () => demoApi.trending(tr7));
  for (const id of ["nato-border", "electricity", "refugee-benefits"]) {
    await add(`story-${id}.json`, "Story", "GET", `/api/stories/${id}`, {}, () => demoApi.story(id));
  }
  // Written differently from the stored link on purpose: the lookup must use linkKey().
  const traced = "t.me/Novosti_Baltii/20871/?single";
  await add("trace-electricity.json", "Story", "GET", "/api/trace", { url: traced }, () => demoApi.traceLink(traced));
  await add("system.json", "SystemStatus", "GET", "/api/system", {}, () => demoApi.system(), { replay: "shape" });

  // Error answers: the backend must use the same status and the same plain-language message.
  await add("error-source-unknown.json", "Error", "GET", "/api/sources/nobody", {}, () => demoApi.channel("nobody"));
  await add("error-story-unknown.json", "Error", "GET", "/api/stories/nope", {}, () => demoApi.story("nope"));
  await add("error-trace-invalid-link.json", "Error", "GET", "/api/trace", { url: "hello" }, () => demoApi.traceLink("hello"));
  await add("error-analyze-invalid-link.json", "Error", "POST", "/api/analyze", {}, () => demoApi.analyze("hello"), { body: { url: "hello" } });

  return {
    manifest: {
      generatedAt: FIXTURE_NOW,
      today: FIXTURE_TODAY,
      note: "Generated by `npm run export:fixtures` (frontend) from the demo data. Do not edit by hand.",
      fixtures: entries,
    },
    files,
    seed: { today: FIXTURE_TODAY, note: "Generated by `npm run export:fixtures` from the demo data. Do not edit by hand.", ...demoSeed() },
  };
}

/** Stable text form: 2-space JSON plus a trailing newline. */
export function toFileText(value: unknown): string {
  return JSON.stringify(value, null, 2) + "\n";
}
