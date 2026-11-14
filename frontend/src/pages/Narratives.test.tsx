import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { setDemoDelay } from "../demo";
import { resetSettingsForTests } from "../lib/settings";
import { renderApp } from "../test/renderApp";

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
