import { describe, expect, it } from "vitest";
import type { Filters } from "../api/types";
import {
  defaultFilters, describeFilters, filtersToParams, isFiltered, matchesPlatform, matchesStatus,
  overlaps, resolveRange, shiftDays, shortDate, todayIso,
} from "./filters";

const TODAY = "2026-11-13";
const f = (patch: Partial<Filters> = {}): Filters => ({ ...defaultFilters("30", TODAY), ...patch });

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

describe("resolveRange", () => {
  it.each([
    ["7", "2026-11-06"],
    ["30", "2026-10-14"],
    ["90", "2026-08-15"],
  ] as const)("preset %s days ends today and starts %s", (range, from) => {
    expect(resolveRange(f({ range }), TODAY)).toEqual({ from, to: TODAY, invalid: false });
  });

  it("custom range uses the given dates", () => {
    expect(resolveRange(f({ range: "custom", from: "2026-09-01", to: "2026-09-20" }), TODAY))
      .toEqual({ from: "2026-09-01", to: "2026-09-20", invalid: false });
  });

  it("detects a reversed custom range", () => {
    expect(resolveRange(f({ range: "custom", from: "2026-12-01", to: "2026-11-01" }), TODAY).invalid).toBe(true);
  });

  it("presets ignore stale custom dates", () => {
    expect(resolveRange(f({ range: "7", from: "2000-01-01", to: "2000-01-02" }), TODAY).from).toBe("2026-11-06");
  });
});

describe("overlaps (active between)", () => {
  const r = { from: "2026-11-06", to: "2026-11-13", invalid: false };
  it.each([
    ["inside", "2026-11-07", "2026-11-08", true],
    ["started before, still active", "2026-10-01", "2026-11-07", true],
    ["started inside, continues after", "2026-11-12", "2026-11-20", true],
    ["covers the whole range", "2026-10-01", "2026-12-01", true],
    ["ended the day the range starts", "2026-10-01", "2026-11-06", true],
    ["ended before", "2026-10-01", "2026-11-05", false],
    ["starts after", "2026-11-14", "2026-11-20", false],
  ])("%s", (_, first, last, expected) => {
    expect(overlaps(first, last, r)).toBe(expected);
  });

  it("an invalid range does not filter anything out", () => {
    expect(overlaps("2000-01-01", "2000-01-02", { from: "2026-12-01", to: "2026-11-01", invalid: true })).toBe(true);
  });

  it("half-filled custom range is open on the empty side", () => {
    expect(overlaps("2020-01-01", "2020-01-02", { from: "", to: "2026-01-01", invalid: false })).toBe(true);
    expect(overlaps("2020-01-01", "2020-01-02", { from: "2025-01-01", to: "", invalid: false })).toBe(false);
  });
});

describe("platform and status", () => {
  it("platform 'all' matches everything; otherwise the item must include it", () => {
    expect(matchesPlatform(["Telegram"], "all")).toBe(true);
    expect(matchesPlatform(["Telegram", "Facebook"], "Facebook")).toBe(true);
    expect(matchesPlatform(["Telegram"], "Facebook")).toBe(false);
    expect(matchesPlatform(["Website"], "Telegram")).toBe(false);
  });
  it("status 'all' matches everything; otherwise exact verdict", () => {
    expect(matchesStatus("false", "all")).toBe(true);
    expect(matchesStatus("false", "false")).toBe(true);
    expect(matchesStatus("misleading", "false")).toBe(false);
  });
});

describe("filtersToParams (what the backend receives)", () => {
  it("defaults send only the resolved date range", () => {
    expect(filtersToParams(f(), TODAY)).toEqual({ from: "2026-10-14", to: TODAY });
  });
  it("sends platform and status when set", () => {
    expect(filtersToParams(f({ platform: "Telegram", status: "misleading", range: "7" }), TODAY))
      .toEqual({ platform: "Telegram", status: "misleading", from: "2026-11-06", to: TODAY });
  });
  it("omits dates for a reversed custom range", () => {
    expect(filtersToParams(f({ range: "custom", from: "2026-12-01", to: "2026-11-01" }), TODAY)).toEqual({});
  });
  it("omits the empty side of a half-filled custom range", () => {
    expect(filtersToParams(f({ range: "custom", from: "2026-09-01", to: "" }), TODAY)).toEqual({ from: "2026-09-01" });
  });
});

describe("describeFilters / isFiltered", () => {
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
