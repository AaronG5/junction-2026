import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { setDemoDelay } from "../demo";
import { resetSettingsForTests } from "../lib/settings";
import { renderApp } from "../test/renderApp";
import { clusteringPolling, windowDays } from "./Narratives";

beforeAll(() => setDemoDelay(0));
beforeEach(() => resetSettingsForTests());
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const list = () => screen.getByRole("heading", { name: "Largest clusters" }).closest("section")!;
const rows = () => within(list()).queryAllByRole("button").filter((b) => b.classList.contains("crow"));
const rowNames = () => rows().map((r) => r.querySelector(".crow-name")!.textContent);
const detailHeading = () => screen.getByRole("heading", { level: 2, name: (_, el) => el.id === "sel-h" });
const summary = () => document.querySelector(".fsummary")!.textContent;
const filterChip = (group: RegExp, name: RegExp) => within(screen.getByRole("group", { name: group })).getByRole("button", { name });

async function openDemo() {
  vi.stubEnv("VITE_DEMO_DATA", "true");
  const user = userEvent.setup();
  renderApp("/dashboard/narratives");
  await waitFor(() => expect(rows().length).toBeGreaterThan(0));
  return user;
}

describe("Narratives (demo data)", () => {
  it("lists the clusters, largest first, and selects the largest", async () => {
    await openDemo();
    expect(rowNames()).toEqual([
      "NATO “provocations” at the Belarus border",
      "EU sanctions will double electricity prices",
      "Election counting is rigged",
      "Refugees get bigger benefits than pensioners",
      "Western media hide the truth",
      "Baltic power grid will collapse this winter",
    ]);
    expect(rows()[0]).toHaveAttribute("aria-pressed", "true");
    expect(detailHeading()).toHaveTextContent("NATO “provocations” at the Belarus border");
    expect(summary()).toContain("6 of 6 large clusters · all platforms · last 7 days");
    expect(screen.getByText(/latest run/i)).toHaveTextContent("18,420 posts · 14 clusters");
  });

  it("each row shows its status as a word, its platforms and its trend", async () => {
    await openDemo();
    const grid = rows()[5];
    expect(grid).toHaveTextContent("Unverified · Telegram · 42 accounts");
    expect(grid).toHaveTextContent("new this week");
    expect(rows()[2]).toHaveTextContent("−5% vs last week");
  });

  it("clicking a cluster shows its details, typical posts and edge cases", async () => {
    const user = await openDemo();
    await user.click(within(list()).getByRole("button", { name: /refugees get bigger benefits/i }));
    expect(detailHeading()).toHaveTextContent("Refugees get bigger benefits than pensioners");
    expect(screen.getByText(/main claim misleading/i)).toBeInTheDocument();
    expect(screen.getByText("Pabėgėliai gauna didesnes išmokas nei pensininkai")).toBeInTheDocument();
    expect(screen.getByText(/gets quoted out of context/i)).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /Russian 44%, Lithuanian 52%/ })).toHaveLength(1);
  });

  it("the map highlights only the selected cluster", async () => {
    const user = await openDemo();
    const map = screen.getByRole("img", { name: /map of 6 clusters/i });
    const selectedBefore = map.querySelectorAll('[data-kind="selected"]').length;
    expect(selectedBefore).toBe(34);
    await user.click(within(list()).getByRole("button", { name: /baltic power grid/i }));
    expect(map.querySelectorAll('[data-kind="selected"]')).toHaveLength(9);
    expect(map).toHaveAttribute("aria-label", expect.stringContaining("Baltic power grid"));
    expect(map.querySelectorAll('[data-kind="noise"]').length).toBeGreaterThan(0);
  });

  it("platform filter narrows the list, map and summary", async () => {
    const user = await openDemo();
    await user.click(filterChip(/platform/i, /facebook/i));
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(summary()).toContain("3 of 6 large clusters · Facebook · last 7 days");
    expect(screen.getByRole("img", { name: /map of 3 clusters/i })).toBeInTheDocument();
  });

  it("status chips show counts and filter by the main claim's verdict", async () => {
    const user = await openDemo();
    expect(filterChip(/status of its main claim/i, /false/i)).toHaveTextContent("False2");
    await user.click(filterChip(/status of its main claim/i, /unverified/i));
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect(rowNames()).toEqual(["Election counting is rigged", "Baltic power grid will collapse this winter"]);
  });

  it("when the selected cluster is filtered out, the first visible one is selected", async () => {
    const user = await openDemo();
    await user.click(within(list()).getByRole("button", { name: /election counting/i })); // Telegram only
    await user.click(filterChip(/platform/i, /facebook/i));
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(rows()[0]).toHaveAttribute("aria-pressed", "true");
    expect(detailHeading()).toHaveTextContent("NATO “provocations”");
  });

  it("filters that match nothing show an empty state; Clear filters brings everything back", async () => {
    const user = await openDemo();
    await user.click(filterChip(/status of its main claim/i, /^true/i));
    await waitFor(() => expect(screen.getByText("No clusters match these filters.")).toBeInTheDocument());
    expect(screen.queryByRole("heading", { level: 2, name: (_, el) => el.id === "sel-h" })).not.toBeInTheDocument();
    await user.click(within(list()).getByRole("button", { name: /clear filters/i }));
    await waitFor(() => expect(rows()).toHaveLength(6));
  });

  it("a custom range in the past shows only clusters active then", async () => {
    const user = await openDemo();
    await user.click(filterChip(/active between/i, /custom/i));
    const from = screen.getByLabelText("From");
    const to = screen.getByLabelText("To");
    const iso = (offset: number) => { const d = new Date(); d.setUTCDate(d.getUTCDate() + offset); return d.toISOString().slice(0, 10); };
    await user.clear(to);
    await user.type(to, iso(-15));
    await user.clear(from);
    await user.type(from, iso(-30));
    await waitFor(() => expect(rows()).toHaveLength(3));
  });

  it("links to Analyze a link and to the claims", async () => {
    await openDemo();
    expect(within(screen.getByRole("main")).getByRole("link", { name: "Analyze a link" })).toHaveAttribute("href", "/dashboard/analyze");
    expect(screen.getByRole("link", { name: /see its claims/i })).toHaveAttribute("href", "/dashboard/claims");
  });
});

describe("Narratives without a backend", () => {
  it("explains that the backend isn't connected and offers a retry", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    renderApp("/dashboard/narratives");
    expect(await screen.findByRole("alert")).toHaveTextContent(/backend isn't connected yet/i);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Largest clusters" })).not.toBeInTheDocument();
  });

  it("shows a loading state before the first answer", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderApp("/dashboard/narratives");
    expect(screen.getByRole("status")).toHaveTextContent(/loading narratives/i);
  });
});

describe("Update clusters", () => {
  const empty = {
    run: { date: "2026-11-15", posts: 0, clusters: 0, unclustered: 0 }, clusters: [], total: 0,
    counts: { all: 0, credible: 0, unverified: 0, misleading: 0, false: 0 }, points: [],
  };
  const grouped = { ...empty, run: { date: "2026-11-15", posts: 2400, clusters: 0 + 1, unclustered: 300 }, total: 0 };
  const idle = { status: "idle", message: null, days: null, startedAt: null, finishedAt: null };
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const note = () => screen.getByTestId("clustering-note");
  const button = () => screen.getByRole("button", { name: /update clusters|updating clusters/i });

  beforeEach(() => { clusteringPolling.intervalMs = 5; });

  /** A fake backend: clustering runs while `running` is true; narratives are empty until a run finished. */
  function backend(opts: { start?: () => Response; finishWith?: object; hold?: { released: boolean } } = {}) {
    const calls: { method: string; url: string; body?: unknown }[] = [];
    let state: object = idle;
    let polls = 0;
    let finished = false;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.startsWith("/api/narratives")) return json(finished ? grouped : empty);
      if (url === "/api/clustering" && method === "POST") {
        if (opts.start) return opts.start();
        state = { status: "running", message: null, days: 7, startedAt: "2026-11-15T10:00:00Z", finishedAt: null };
        return json(state, 202);
      }
      if (url === "/api/clustering") {
        if ((state as { status: string }).status === "running" && ++polls >= 2 && (!opts.hold || opts.hold.released)) {
          state = opts.finishWith ?? { status: "done", message: "1 clusters from 2,400 posts.", days: 7, startedAt: "x", finishedAt: "y" };
          finished = (state as { status: string }).status === "done";
        }
        return json(state);
      }
      if (url === "/api/settings") return json({ dataMode: "hybrid", shareAnalyses: false });
      return json({ detail: "not in this test" }, 404);
    }));
    return calls;
  }

  it("with no run yet, says so and points to the button", async () => {
    backend();
    renderApp("/dashboard/narratives");
    expect(await screen.findByText(/no clusters yet/i)).toBeInTheDocument();
    expect(button()).toBeEnabled();
    expect(screen.queryByText(/latest run/i)).not.toBeInTheDocument();
  });

  it("starts a run for the chosen period, follows it, then reloads the clusters", async () => {
    const hold = { released: false };
    const calls = backend({ hold });
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    await screen.findByText(/no clusters yet/i);
    await user.click(button());
    expect(calls.find((c) => c.method === "POST")).toEqual({ method: "POST", url: "/api/clustering", body: { days: 7 } });
    expect(await screen.findByRole("button", { name: /updating clusters/i })).toBeDisabled();
    expect(note()).toHaveTextContent(/grouping the posts of the last 7 days/i);
    hold.released = true;
    await waitFor(() => expect(note()).toHaveTextContent("1 clusters from 2,400 posts."));
    expect(button()).toBeEnabled();
    expect(await screen.findByText(/latest run/i)).toHaveTextContent("2,400 posts · 1 clusters");
  });

  it("uses the filter's period: last 30 days", async () => {
    const calls = backend();
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    await screen.findByText(/no clusters yet/i);
    await user.click(filterChip(/active between/i, /30 days/i));
    await user.click(button());
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ days: 30 }));
  });

  it("shows why a run made no clusters", async () => {
    backend({ finishWith: { status: "too_few", message: "Only 3 posts in this window; at least 20 are needed. No clusters made.", days: 7, startedAt: "x", finishedAt: "y" } });
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    await screen.findByText(/no clusters yet/i);
    await user.click(button());
    await waitFor(() => expect(note()).toHaveTextContent(/only 3 posts in this window/i));
    expect(screen.getByText(/no clusters yet/i)).toBeInTheDocument();
  });

  it("shows the backend's message when a run can't start", async () => {
    backend({ start: () => json({ detail: "Clustering needs extra packages. Install them with:  uv sync --extra clustering" }, 503) });
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    await screen.findByText(/no clusters yet/i);
    await user.click(button());
    await waitFor(() => expect(note()).toHaveTextContent(/uv sync --extra clustering/));
    expect(button()).toBeEnabled();
  });

  it("in demo mode the button answers at once and the example clusters stay", async () => {
    vi.stubEnv("VITE_DEMO_DATA", "true");
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    await waitFor(() => expect(rows().length).toBeGreaterThan(0));
    await user.click(button());
    await waitFor(() => expect(note()).toHaveTextContent(/example clusters stay as they are/i));
    expect(rows().length).toBe(6);
  });
});

describe("windowDays", () => {
  const base = { platform: "all", status: "all", from: "", to: "2026-11-15" } as const;
  it("is the preset, or from the custom start to today, kept within 1-365", () => {
    expect(windowDays({ ...base, range: "90" })).toBe(90);
    expect(windowDays({ ...base, range: "custom", from: "2026-11-01" }, "2026-11-15")).toBe(14);
    expect(windowDays({ ...base, range: "custom", from: "2026-11-15" }, "2026-11-15")).toBe(1);
    expect(windowDays({ ...base, range: "custom", from: "2020-01-01" }, "2026-11-15")).toBe(365);
    expect(windowDays({ ...base, range: "custom", from: "" }, "2026-11-15")).toBe(1);
  });
});
