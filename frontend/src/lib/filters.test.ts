import { describe, expect, it } from "vitest";
import cases from "../../../shared/cases/filters.json";
import type { Filters, Platform, PlatformFilter, StatusFilter, Verdict } from "../api/types";
import {
  defaultFilters, describeFilters, filtersToParams, isFiltered, matchesPlatform, matchesQuery, matchesStatus,
  overlaps, resolveRange, shiftDays, shortDate, todayIso,
} from "./filters";

/* The rule tables live in shared/cases/filters.json; the backend runs the same cases. */
const TODAY = cases.today;

describe("shared cases: resolveRange", () => {
  it.each(cases.resolveRange.map((c) => [c.name, c] as const))("%s", (_, c) => {
    expect(resolveRange(c.filters as Pick<Filters, "range" | "from" | "to">, TODAY)).toEqual(c.expected);
  });
});

describe("shared cases: overlaps (active between)", () => {
  it.each(cases.overlaps.map((c) => [c.name, c] as const))("%s", (_, c) => {
    expect(overlaps(c.firstSeen, c.lastSeen, c.range)).toBe(c.expected);
  });
});

describe("shared cases: platform and status", () => {
  it.each(cases.matchesPlatform)("platforms %j with filter $filter -> $expected", (c) => {
    expect(matchesPlatform(c.platforms as Platform[], c.filter as PlatformFilter)).toBe(c.expected);
  });
  it.each(cases.matchesStatus)("verdict $verdict with filter $filter -> $expected", (c) => {
    expect(matchesStatus(c.verdict as Verdict, c.filter as StatusFilter)).toBe(c.expected);
  });
});

describe("shared cases: matchesQuery (claim search)", () => {
  it.each(cases.matchesQuery.map((c) => [c.name, c] as const))("%s", (_, c) => {
    expect(matchesQuery(c.text, c.query)).toBe(c.expected);
  });
});

describe("shared cases: filtersToParams (what the backend receives)", () => {
  it.each(cases.filtersToParams.map((c) => [c.name, c] as const))("%s", (_, c) => {
    expect(filtersToParams(c.filters as Filters, TODAY)).toEqual(c.expected);
  });
});

/* Frontend-only helpers. */

describe("dates", () => {
  it("todayIso uses UTC", () => {
    expect(todayIso(new Date("2026-11-13T23:30:00Z"))).toBe("2026-11-13");
  });
  it("shiftDays crosses month and year boundaries", () => {
    expect(shiftDays("2026-11-01", -1)).toBe("2026-10-31");
    expect(shiftDays("2026-01-01", -1)).toBe("2025-12-31");
    expect(shiftDays("2026-02-28", 1)).toBe("2026-03-01");
  });
  it("shortDate formats day and month", () => {
    expect(shortDate("2026-11-02")).toBe("2 Nov");
  });
});

describe("describeFilters / isFiltered", () => {
  const f = (patch: Partial<Filters> = {}): Filters => ({ ...defaultFilters("30", TODAY), ...patch });
  it("describes the active filters in words", () => {
    expect(describeFilters(f(), TODAY)).toBe("all platforms · last 30 days");
    expect(describeFilters(f({ platform: "Telegram", status: "false" }), TODAY)).toBe("Telegram · last 30 days · false");
    expect(describeFilters(f({ range: "custom", from: "2026-09-01", to: "2026-09-20" }), TODAY)).toBe("all platforms · 1 Sep to 20 Sep");
    expect(describeFilters(f({ range: "custom", from: "", to: "" }), TODAY)).toBe("all platforms · any time");
  });
  it("isFiltered compares with the page's defaults", () => {
    const defaults = f();
    expect(isFiltered(f(), defaults)).toBe(false);
    expect(isFiltered(f({ status: "true" }), defaults)).toBe(true);
    expect(isFiltered(f({ range: "7" }), defaults)).toBe(true);
  });
});
