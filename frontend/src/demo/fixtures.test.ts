// @vitest-environment node
/**
 * The shared contract fixtures must match what the demo data produces.
 * If this fails after changing the demo data or the API types:  npm run export:fixtures
 */
import { readFileSync, readdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { FIXTURE_NOW, buildFixtures, toFileText, type Fixtures } from "./fixtures";

const dir = resolve(__dirname, "../../../shared/fixtures");
/** Read as LF, whatever git's line-ending settings did on checkout. */
const read = (name: string) => readFileSync(join(dir, name), "utf8").replace(/\r\n/g, "\n");
let built: Fixtures;

beforeAll(async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date(FIXTURE_NOW));
  built = await buildFixtures();
});
afterAll(() => vi.useRealTimers());

describe("shared contract fixtures", () => {
  it("every committed file matches the demo data (run `npm run export:fixtures` if not)", () => {
    expect(read("manifest.json")).toBe(toFileText(built.manifest));
    for (const [name, value] of Object.entries(built.files)) {
      expect(read(name), name).toBe(toFileText(value));
    }
  });

  it("the demo data for the backend's loader matches too", () => {
    const seed = readFileSync(resolve(__dirname, "../../../shared/demo/demo-data.json"), "utf8").replace(/\r\n/g, "\n");
    expect(seed).toBe(toFileText(built.seed));
  });

  it("no stale files are left in the folder", () => {
    const expected = new Set(["manifest.json", ...Object.keys(built.files)]);
    const present = readdirSync(dir).filter((n: string) => n.endsWith(".json"));
    expect(present.sort()).toEqual([...expected].sort());
  });

  it("covers every API response type", () => {
    const models = new Set(built.manifest.fixtures.map((e) => e.model));
    expect([...models].sort()).toEqual([
      "ChannelReport", "ClaimsResponse", "Error", "NarrativesResponse", "PostReport", "SourcesResponse", "Story", "SystemStatus", "TrendingResponse",
    ]);
  });

  it("error fixtures carry the status and a plain-language message", () => {
    const errors = built.manifest.fixtures.filter((e) => e.model === "Error");
    expect(errors.map((e) => e.status).sort()).toEqual([400, 400, 404, 404]);
    for (const e of errors) {
      const body = built.files[e.file] as { detail: string };
      expect(Object.keys(body)).toEqual(["detail"]);
      expect(body.detail).toMatch(/^[A-Z].*\.$/);
    }
  });

  it("only live or AI answers are compared by shape instead of exactly", () => {
    const shape = built.manifest.fixtures.filter((e) => e.replay === "shape").map((e) => e.file).sort();
    expect(shape).toEqual(["analyze-kanalas_b-4521.json", "system.json"]);
  });

  it("refuses to build with a real clock (the files would change every day)", async () => {
    vi.setSystemTime(new Date("2030-01-01T00:00:00Z"));
    await expect(buildFixtures()).rejects.toThrow(/clock frozen/);
    vi.setSystemTime(new Date(FIXTURE_NOW));
  });
});
