import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, BackendUnavailable } from "../api/client";
import type { Filters, StatusCounts } from "../api/types";
import { defaultFilters } from "../lib/filters";
import { useApi } from "../lib/useApi";
import { FilterBar, FilterSummary } from "./FilterBar";
import { LanguageBar } from "./LanguageBar";
import { EmptyState, ErrorState, Loading } from "./States";
import { StatusDot, VerdictPill } from "./Verdict";

const TODAY = "2026-11-13";

function Harness({ initial, counts, onChange, compact }: { initial: Filters; counts?: StatusCounts; onChange?: (f: Filters) => void; compact?: boolean }) {
  const [f, setF] = useState(initial);
  return <FilterBar value={f} counts={counts} compact={compact} onChange={(n) => { setF(n); onChange?.(n); }} />;
}
const group = (name: RegExp) => screen.getByRole("group", { name });
const chip = (g: RegExp, name: string | RegExp) => within(group(g)).getByRole("button", { name });

describe("FilterBar", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(TODAY + "T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("shows the current choice in each group", () => {
    render(<Harness initial={defaultFilters("30", TODAY)} />);
    expect(chip(/platform/i, "All")).toHaveAttribute("aria-pressed", "true");
    expect(chip(/active between/i, "30 days")).toHaveAttribute("aria-pressed", "true");
    expect(chip(/status/i, /all/i)).toHaveAttribute("aria-pressed", "true");
    expect(chip(/platform/i, "Telegram")).toHaveAttribute("aria-pressed", "false");
  });

  it("clicking chips updates the filters", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={defaultFilters("30", TODAY)} onChange={onChange} />);

    await user.click(chip(/platform/i, "Facebook"));
    expect(chip(/platform/i, "Facebook")).toHaveAttribute("aria-pressed", "true");
    expect(chip(/platform/i, "All")).toHaveAttribute("aria-pressed", "false");

    await user.click(chip(/status/i, /misleading/i));
    await user.click(chip(/active between/i, "7 days"));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ platform: "Facebook", status: "misleading", range: "7" }));
  });

  it("date inputs appear only for Custom, prefilled with the range on screen", async () => {
    const user = userEvent.setup();
    render(<Harness initial={defaultFilters("7", TODAY)} />);
    expect(screen.queryByLabelText("From")).not.toBeInTheDocument();

    await user.click(chip(/active between/i, "Custom"));
    expect(screen.getByLabelText("From")).toHaveValue("2026-11-06");
    expect(screen.getByLabelText("To")).toHaveValue(TODAY);

    await user.click(chip(/active between/i, "30 days"));
    expect(screen.queryByLabelText("From")).not.toBeInTheDocument();
  });

  it("warns when the start date is after the end date", () => {
    render(<Harness initial={{ ...defaultFilters("30", TODAY), range: "custom", from: "2026-12-01", to: "2026-11-01" }} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/start date is after the end date/i);
  });

  it("no warning for a valid custom range", () => {
    render(<Harness initial={{ ...defaultFilters("30", TODAY), range: "custom", from: "2026-11-01", to: "2026-11-10" }} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows counts on status chips, and every status has a text label", () => {
    const counts = { all: 7, true: 1, false: 3, misleading: 2, unverified: 1 };
    render(<Harness initial={defaultFilters("30", TODAY)} counts={counts} />);
    expect(chip(/status/i, /false/i)).toHaveTextContent("False3");
    expect(chip(/status/i, /true/i)).toHaveTextContent("True1");
    expect(within(group(/status/i)).getAllByRole("button").map((b) => b.textContent)).toEqual(["All7", "True1", "False3", "Misleading2", "Unverified1"]);
  });

  it("compact mode drops the card but keeps every axis labelled on screen", () => {
    render(<Harness initial={defaultFilters("30", TODAY)} compact />);
    expect(group(/platform/i)).toBeInTheDocument();
    // Two filter axes side by side are only readable if each one is named.
    const label = screen.getByText("Platform");
    expect(label).toHaveClass("lbl");
    expect(label).not.toHaveClass("visually-hidden");
    expect(group(/platform/i)).toHaveAttribute("aria-labelledby", label.id);
  });
});

describe("FilterSummary", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(TODAY + "T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("says how many are shown and offers Clear only when filtered", async () => {
    const defaults = defaultFilters("30", TODAY);
    const onClear = vi.fn();
    const { rerender } = render(<FilterSummary shown={7} total={7} noun="claims" value={defaults} defaults={defaults} onClear={onClear} />);
    expect(screen.getByText(/of 7 claims/)).toHaveTextContent("7 of 7 claims · all platforms · last 30 days");
    expect(screen.queryByRole("button", { name: /clear filters/i })).not.toBeInTheDocument();

    rerender(<FilterSummary shown={2} total={7} noun="claims" value={{ ...defaults, platform: "Telegram", status: "false" }} defaults={defaults} onClear={onClear} />);
    expect(screen.getByText(/of 7 claims/)).toHaveTextContent("2 of 7 claims · Telegram · last 30 days · false");
    await userEvent.setup().click(screen.getByRole("button", { name: /clear filters/i }));
    expect(onClear).toHaveBeenCalledOnce();
  });
});

describe("verdict display", () => {
  it.each([
    ["true", "True", "✓"], ["false", "False", "✕"], ["misleading", "Misleading", "!"], ["unverified", "Unverified", "?"],
  ] as const)("%s shows the label '%s', not just a color", (verdict, label, icon) => {
    render(<VerdictPill verdict={verdict} />);
    const pill = screen.getByText(label);
    expect(pill).toHaveAttribute("data-verdict", verdict);
    const dot = pill.querySelector(".vdot")!;
    expect(dot).toHaveTextContent(icon);
    expect(dot).toHaveAttribute("aria-hidden", "true");
    expect(dot).toHaveClass(`v-${verdict}`);
  });

  it("accepts a custom label", () => {
    render(<VerdictPill verdict="false" label="Main claim false" />);
    expect(screen.getByText("Main claim false")).toBeInTheDocument();
  });

  it("status dots are decorative", () => {
    const { container } = render(<StatusDot verdict="false" />);
    expect(container.firstChild).toHaveAttribute("aria-hidden", "true");
  });
});

describe("LanguageBar", () => {
  const langs = [
    { code: "ru", name: "Russian", share: 47 }, { code: "lt", name: "Lithuanian", share: 41 },
    { code: "en", name: "English", share: 12 }, { code: "pl", name: "Polish", share: 1 },
  ];
  it("describes the split for screen readers and has a text legend", () => {
    render(<LanguageBar languages={langs} />);
    expect(screen.getByRole("img")).toHaveAttribute("aria-label", "Russian 47%, Lithuanian 41%, English 12%");
    expect(screen.getByText("Russian 47%")).toBeInTheDocument();
  });
  it("shows at most three slices", () => {
    render(<LanguageBar languages={langs} />);
    expect(screen.getByRole("img").children).toHaveLength(3);
    expect(screen.queryByText(/Polish/)).not.toBeInTheDocument();
  });
});

describe("states", () => {
  it("loading is announced", () => {
    render(<Loading label="Loading narratives" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading narratives…");
  });

  it("empty state shows the message and action", () => {
    render(<EmptyState action={<button>Clear filters</button>}>Nothing matches.</EmptyState>);
    expect(screen.getByText("Nothing matches.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("backend not connected: explains it and how to use demo mode", async () => {
    const retry = vi.fn();
    render(<ErrorState error={new BackendUnavailable()} onRetry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/backend isn't connected yet/i);
    expect(screen.getByText("npm run dev:demo")).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: /try again/i }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("backend error: shows its message", () => {
    render(<ErrorState error={new ApiError(404, "There is no report for this source yet.")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("There is no report for this source yet.");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("unexpected error: generic message with details", () => {
    render(<ErrorState error={new Error("boom")} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/something went wrong/i);
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
  });
});

describe("useApi", () => {
  function deferred<T>() {
    let resolve!: (v: T) => void, reject!: (e: unknown) => void;
    const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
  }

  it("loads, then shows data", async () => {
    const { result } = renderHook(() => useApi(() => Promise.resolve(42), []));
    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.data).toBe(42));
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("ignores a slow old answer that arrives after a newer one", async () => {
    const calls: ReturnType<typeof deferred<string>>[] = [];
    const { result, rerender } = renderHook(({ q }) => useApi(() => { const d = deferred<string>(); calls.push(d); return d.promise; }, [q]), { initialProps: { q: "a" } });
    rerender({ q: "b" });
    await act(async () => { calls[1].resolve("answer for b"); });
    await act(async () => { calls[0].resolve("stale answer for a"); });
    expect(result.current.data).toBe("answer for b");
  });

  it("reports errors and can reload", async () => {
    let fail = true;
    const { result } = renderHook(() => useApi(() => (fail ? Promise.reject(new BackendUnavailable()) : Promise.resolve("ok")), []));
    await waitFor(() => expect(result.current.error).toBeInstanceOf(BackendUnavailable));
    fail = false;
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.data).toBe("ok"));
    expect(result.current.error).toBeNull();
  });

  it("keeps showing the previous data while reloading", async () => {
    let n = 0;
    const later = deferred<number>();
    const { result, rerender } = renderHook(({ k }) => useApi(() => (++n === 1 ? Promise.resolve(1) : later.promise), [k]), { initialProps: { k: 1 } });
    await waitFor(() => expect(result.current.data).toBe(1));
    rerender({ k: 2 });
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBe(1);
    await act(async () => later.resolve(2));
    expect(result.current.data).toBe(2);
  });
});
