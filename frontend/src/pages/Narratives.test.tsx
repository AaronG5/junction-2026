import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { setDemoDelay } from "../demo";
import { resetSettingsForTests } from "../lib/settings";
import { renderApp } from "../test/renderApp";
import { PostCard, clusteringPolling } from "./Narratives";

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
    expect(within(screen.getByRole("main")).getByRole("link", { name: "Analyze a post" })).toHaveAttribute("href", "/dashboard/analyze");
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
  const grouped = { ...empty, run: { date: "2026-11-15", posts: 2400, clusters: 1, unclustered: 300 } };
  const idle = { status: "idle", message: null, mode: null, days: null, startedAt: null, finishedAt: null };
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const note = () => screen.getByTestId("clustering-note");
  const button = () => screen.getByRole("button", { name: /update clusters|updating clusters/i });
  const done = { status: "done", message: "Added 2,400 new posts: 2,100 joined existing clusters.", mode: "update", days: null,
    startedAt: "x", finishedAt: "y" };

  beforeEach(() => { clusteringPolling.intervalMs = 5; });

  /** A fake backend: a run reports progress, then finishes once `hold` is released; narratives are empty until then. */
  function backend(opts: { start?: () => Response; finishWith?: object; hold?: { released: boolean } } = {}) {
    const calls: { method: string; url: string; body?: unknown }[] = [];
    let state: object = idle;
    let finished = false;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.startsWith("/api/narratives")) return json(finished ? grouped : empty);
      if (url === "/api/clustering" && method === "POST") {
        if (opts.start) return opts.start();
        state = { ...idle, status: "running", mode: "update", startedAt: "2026-11-15T10:00:00Z" };
        return json(state, 202);
      }
      if (url === "/api/clustering") {
        if ((state as { status: string }).status === "running") {
          if (!opts.hold || opts.hold.released) {
            state = opts.finishWith ?? done;
            finished = (state as { status: string }).status === "done";
          } else {
            state = { ...state, message: "Adding 2,400 new posts to the clusters." };
          }
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
    expect(screen.getAllByRole("button", { name: /update clusters/i })).toHaveLength(1);
    expect(button()).toBeEnabled();
    expect(screen.queryByText(/latest run/i)).not.toBeInTheDocument();
  });

  it("adds the new posts, shows what it is doing, then reloads the clusters", async () => {
    const hold = { released: false };
    const calls = backend({ hold });
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    await screen.findByText(/no clusters yet/i);
    await user.click(button());
    expect(calls.find((c) => c.method === "POST")).toEqual({ method: "POST", url: "/api/clustering", body: {} });
    expect(await screen.findByRole("button", { name: /updating clusters/i })).toBeDisabled();
    await waitFor(() => expect(note()).toHaveTextContent("Adding 2,400 new posts to the clusters."));
    hold.released = true;
    await waitFor(() => expect(note()).toHaveTextContent("Added 2,400 new posts: 2,100 joined existing clusters."));
    expect(button()).toBeEnabled();
    expect(await screen.findByText(/latest run/i)).toHaveTextContent("2,400 posts · 1 clusters");
  });

  it("says when there is nothing new", async () => {
    backend({ finishWith: { ...done, status: "up_to_date", message: "No new posts since the last update." } });
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    await screen.findByText(/no clusters yet/i);
    await user.click(button());
    await waitFor(() => expect(note()).toHaveTextContent("No new posts since the last update."));
  });

  it("shows why a run made no clusters", async () => {
    backend({ finishWith: { ...done, status: "too_few", message: "Only 3 posts in this window; at least 20 are needed. No clusters made." } });
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

describe("Typical and edge posts", () => {
  it("show the highlighted phrases, how many per tier, and a link to the post", () => {
    render(<PostCard post={{
      id: "1", url: "https://t.me/chan/5", text: "They lie. Everyone knows.", author: "@chan", where: "Telegram channel · English",
      segments: [
        { text: "They lie", technique: "Name-calling", tier: "clear", why: "Calls them liars." },
        { text: ". " },
        { text: "Everyone knows", technique: "Bandwagon", tier: "possible", why: "Appeals to the crowd." },
        { text: "." },
      ],
      highlights: { clear: 1, likely: 0, possible: 1 },
    }} />);
    const marks = document.querySelectorAll("mark.hl");
    expect([...marks].map((m) => m.className)).toEqual(["hl hl-clear", "hl hl-possible"]);
    expect(marks[0]).toHaveAttribute("title", "Name-calling (clear): Calls them liars.");
    expect(marks[0]).toHaveTextContent("They lie (Name-calling, clear)");   // the tier is in words too, not colour alone
    expect(screen.getByTestId("highlight-counts")).toHaveTextContent("2 highlights1 clear1 possible");
    const link = screen.getByRole("link", { name: "Open the post by @chan" });
    expect(link).toHaveAttribute("href", "https://t.me/chan/5");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("before Gemma has read a post: its plain text, no counts; no link without a URL", () => {
    render(<PostCard post={{ id: "2", text: "Plain text.", author: "A", where: "Website" }} />);
    expect(screen.getByText("Plain text.")).toBeInTheDocument();
    expect(screen.queryByTestId("highlight-counts")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("a post with nothing flagged says so; an edge post keeps its note", () => {
    render(<PostCard edge post={{ id: "3", text: "Calm news.", author: "A", where: "Website", note: "Far from the centre.",
      segments: [{ text: "Calm news." }], highlights: { clear: 0, likely: 0, possible: 0 } }} />);
    expect(screen.getByTestId("highlight-counts")).toHaveTextContent("No manipulation techniques found");
    expect(screen.getByText("Far from the centre.")).toBeInTheDocument();
    expect(document.querySelector(".postcard-edge")).not.toBeNull();
  });
});
