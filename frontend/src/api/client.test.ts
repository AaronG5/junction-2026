import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import linkCases from "../../../shared/cases/links.json";
import { defaultFilters } from "../lib/filters";
import { ApiError, BackendUnavailable, api, httpApi, isDemoMode, isSupportedLink, linkKey } from "./client";

// Records whether the demo module was ever loaded.
const demoState = vi.hoisted(() => ({ loaded: false }));
vi.mock("../demo", () => {
  demoState.loaded = true;
  return {
    DEMO_DATA_MARKER: "test-marker",
    demoApi: {
      narratives: async () => ({ run: { date: "x", posts: 1, clusters: 1, unclustered: 0 }, clusters: [], total: 0, counts: {}, points: [], fromDemo: true }),
    },
  };
});

function mockFetch(impl: () => Promise<Response> | Response) {
  const fn = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => impl());
  vi.stubGlobal("fetch", fn);
  return fn;
}
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  vi.unstubAllEnvs();
  // Presets ("last 7 days") resolve against the real clock; pin it.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-11-13T12:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("normal mode (no demo flag)", () => {
  it("is not demo mode by default", () => {
    expect(isDemoMode()).toBe(false);
  });

  it("calls the backend and never loads the demo data", async () => {
    const fetchMock = mockFetch(() => json({ ok: true }));
    await api.narratives(defaultFilters("7", "2026-11-13"));
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/narratives?from=2026-11-06&to=2026-11-13");
    expect(demoState.loaded).toBe(false);
  });

  it("passes JSON through on success", async () => {
    mockFetch(() => json({ stories: [] }));
    await expect(httpApi.trending(defaultFilters())).resolves.toEqual({ stories: [] });
  });

  it("sends filters, the search query and the platform as query parameters", async () => {
    const fetchMock = mockFetch(() => json({}));
    await httpApi.claims({ ...defaultFilters("30", "2026-11-13"), platform: "Facebook", status: "false", query: "nato" });
    const url = new URL(String(fetchMock.mock.calls[0][0]), "http://x");
    expect(url.pathname).toBe("/api/claims");
    expect(Object.fromEntries(url.searchParams)).toEqual({ platform: "Facebook", status: "false", from: "2026-10-14", to: "2026-11-13", q: "nato" });

    await httpApi.sources("all");
    expect(String(fetchMock.mock.calls[1][0])).toBe("/api/sources");
    await httpApi.sources("Telegram");
    expect(String(fetchMock.mock.calls[2][0])).toBe("/api/sources?platform=Telegram");
  });

  it("escapes ids in paths", async () => {
    const fetchMock = mockFetch(() => json({}));
    await httpApi.channel("a/b c");
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/sources/a%2Fb%20c");
  });

  it("analyze POSTs the link as JSON and reports the first step", async () => {
    const fetchMock = mockFetch(() => json({ url: "u" }));
    const steps: number[] = [];
    await httpApi.analyze("https://t.me/x/1", (i) => steps.push(i));
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ url: "https://t.me/x/1" });
    expect(steps).toEqual([0]);
  });
});

describe("errors", () => {
  it.each([
    ["network error (nothing listening)", () => Promise.reject(new TypeError("Failed to fetch"))],
    ["proxy error page (500, text)", () => new Response("Proxy error", { status: 500, headers: { "content-type": "text/plain" } })],
    ["HTML fallback page (200, html)", () => new Response("<!doctype html>", { status: 200, headers: { "content-type": "text/html" } })],
    ["404 without JSON (endpoint missing)", () => new Response("Not Found", { status: 404 })],
    ["broken JSON", () => new Response("{oops", { status: 200, headers: { "content-type": "application/json" } })],
  ])("%s becomes BackendUnavailable", async (_, impl) => {
    mockFetch(impl as () => Promise<Response>);
    await expect(httpApi.system()).rejects.toBeInstanceOf(BackendUnavailable);
  });

  it("a JSON error from the backend becomes ApiError with its message", async () => {
    mockFetch(() => json({ detail: "There is no report for this source yet." }, 404));
    const err = await httpApi.channel("nobody").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
    expect(err.message).toBe("There is no report for this source yet.");
  });

  it("a JSON error without a message gets a generic one", async () => {
    mockFetch(() => json({}, 400));
    await expect(httpApi.system()).rejects.toThrow("Request failed (400).");
  });
});

describe("demo mode", () => {
  it("answers from the demo data and makes no network requests", async () => {
    vi.stubEnv("VITE_DEMO_DATA", "true");
    const fetchMock = mockFetch(() => json({}));
    const res = await api.narratives(defaultFilters());
    expect(isDemoMode()).toBe(true);
    expect(demoState.loaded).toBe(true);
    expect((res as unknown as { fromDemo: boolean }).fromDemo).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("isSupportedLink (shared cases: shared/cases/links.json)", () => {
  it.each(linkCases.accept)("accepts %j", (url) => expect(isSupportedLink(url)).toBe(true));
  it.each(linkCases.reject)("rejects %j", (url) => expect(isSupportedLink(url)).toBe(false));
});

describe("linkKey (shared cases: shared/cases/links.json keys)", () => {
  it.each(linkCases.keys)("%j -> %j", (url, key) => expect(linkKey(url)).toBe(key));
  it("is stable: a key is its own key", () => {
    for (const [, key] of linkCases.keys) expect(linkKey(key)).toBe(key);
  });
});
