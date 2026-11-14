import { beforeAll, describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { ANALYZE_STEPS, type Filters } from "../api/types";
import { defaultFilters } from "../lib/filters";
import { claims as allClaims, clusters as allClusters, d } from "./data";
import { demoApi, setDemoDelay } from "./index";

beforeAll(() => setDemoDelay(0));
const f = (patch: Partial<Filters> = {}): Filters => ({ ...defaultFilters("90"), ...patch });

describe("demo narratives behave like the API contract", () => {
  it("returns every cluster for wide filters, largest first, without demo-only fields", async () => {
    const res = await demoApi.narratives(f());
    expect(res.clusters).toHaveLength(allClusters.length);
    expect(res.total).toBe(allClusters.length);
    const sizes = res.clusters.map((c) => c.size);
    expect(sizes).toEqual([...sizes].sort((a, b) => b - a));
    expect(res.clusters[0]).not.toHaveProperty("map");
  });

  it("status counts add up and ignore the status filter itself", async () => {
    const all = await demoApi.narratives(f());
    const { all: total, ...byVerdict } = all.counts;
    expect(Object.values(byVerdict).reduce((a, b) => a + b, 0)).toBe(total);
    const onlyFalse = await demoApi.narratives(f({ status: "false" }));
    expect(onlyFalse.counts).toEqual(all.counts);
    expect(onlyFalse.clusters.every((c) => c.status === "false")).toBe(true);
    expect(onlyFalse.clusters).toHaveLength(all.counts.false);
  });

  it("the map only shows points of matching clusters (plus unclustered posts)", async () => {
    const res = await demoApi.narratives(f({ platform: "Facebook" }));
    const ids = new Set(res.clusters.map((c) => c.id));
    expect(res.clusters.every((c) => c.platforms.includes("Facebook"))).toBe(true);
    expect(res.points.every((p) => p.clusterId === null || ids.has(p.clusterId))).toBe(true);
    expect(res.points.some((p) => p.clusterId === null)).toBe(true);
  });

  it("map positions don't jump when filters change", async () => {
    const wide = await demoApi.narratives(f());
    const narrow = await demoApi.narratives(f({ status: "false" }));
    const key = (p: { x: number; y: number }) => `${p.x.toFixed(3)},${p.y.toFixed(3)}`;
    const wideKeys = new Set(wide.points.map(key));
    expect(narrow.points.every((p) => wideKeys.has(key(p)))).toBe(true);
  });

  it("a range with no activity returns nothing", async () => {
    const res = await demoApi.narratives(f({ range: "custom", from: "2001-01-01", to: "2001-01-31" }));
    expect(res.clusters).toEqual([]);
    expect(res.counts.all).toBe(0);
  });

  it("a past range shows only clusters that were already active then", async () => {
    const past = await demoApi.narratives(f({ range: "custom", from: d(-30), to: d(-15) }));
    const expected = allClusters.filter((c) => c.firstSeen <= d(-15)).map((c) => c.id).sort();
    expect(past.clusters.map((c) => c.id).sort()).toEqual(expected);
    expect(past.clusters.length).toBeGreaterThan(0);
    expect(past.clusters.length).toBeLessThan(allClusters.length);
  });
});

describe("demo claims", () => {
  it("search, platform, date and status combine", async () => {
    const res = await demoApi.claims({ ...f({ platform: "Telegram", status: "false" }), query: "NATO" });
    expect(res.claims.map((c) => c.id)).toEqual([1]);
    expect(res.total).toBe(allClaims.length);
  });
  it("the 30-day default hides the old claim, 90 days shows it", async () => {
    const month = await demoApi.claims(defaultFilters("30"));
    const quarter = await demoApi.claims(defaultFilters("90"));
    expect(month.claims.some((c) => c.id === 7)).toBe(false);
    expect(quarter.claims.some((c) => c.id === 7)).toBe(true);
  });
});

describe("demo sources, channel, analyze, stories, system", () => {
  it("filters sources by platform", async () => {
    expect((await demoApi.sources("all")).sources).toHaveLength(8);
    expect((await demoApi.sources("Facebook")).sources.every((s) => s.platform === "Facebook")).toBe(true);
  });

  it("has a 90-day channel report and a clear 404 for unknown sources", async () => {
    const report = await demoApi.channel("kanalas_b");
    expect(report.daily).toHaveLength(90);
    expect(report.daily.map((x) => x.date)).toEqual([...report.daily.map((x) => x.date)].sort());
    const err = await demoApi.channel("nobody").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(404);
  });

  it("analyze walks through every step and rejects unsupported links", async () => {
    const steps: number[] = [];
    const report = await demoApi.analyze("https://t.me/someone/1", (i) => steps.push(i));
    expect(steps).toEqual(ANALYZE_STEPS.map((_, i) => i));
    expect(report.url).toBe("https://t.me/someone/1");
    await expect(demoApi.analyze("https://example.com", () => {})).rejects.toBeInstanceOf(ApiError);
  });

  it("returned data is a copy: callers can't corrupt the demo data", async () => {
    const a = await demoApi.sources("all");
    a.sources[0].name = "changed";
    const b = await demoApi.sources("all");
    expect(b.sources[0].name).not.toBe("changed");
  });

  it("trending stories filter; story and traceLink resolve", async () => {
    const fb = await demoApi.trending(f({ platform: "Facebook" }));
    expect(fb.stories.map((s) => s.id)).toEqual(["refugee-benefits"]);
    expect((await demoApi.story("electricity")).topic).toBe("Energy prices");
    await expect(demoApi.story("nope")).rejects.toBeInstanceOf(ApiError);
    expect((await demoApi.traceLink("https://t.me/x/1")).id).toBe("nato-border");
    await expect(demoApi.traceLink("hello")).rejects.toBeInstanceOf(ApiError);
  });

  it("system status lists the three services", async () => {
    expect((await demoApi.system()).services.map((s) => s.name)).toEqual(["AI server", "Database", "Vector search"]);
  });
});
