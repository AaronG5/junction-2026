import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setDemoDelay } from "../demo";
import { resetSettingsForTests } from "../lib/settings";
import { renderApp } from "../test/renderApp";

beforeEach(() => {
  resetSettingsForTests();
  setDemoDelay(0);
  vi.stubEnv("VITE_DEMO_DATA", "true");
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const claimButtons = () => within(screen.getByRole("region", { name: "Claims" })).queryAllByRole("button", { expanded: false })
  .concat(within(screen.getByRole("region", { name: "Claims" })).queryAllByRole("button", { expanded: true }));
const claimTexts = () => Array.from(document.querySelectorAll(".claim-text > span:first-child")).map((e) => e.textContent);
const summary = () => document.querySelector(".fsummary")!.textContent;
const chip = (group: RegExp, name: RegExp) => within(screen.getByRole("group", { name: group })).getByRole("button", { name });

async function open() {
  const user = userEvent.setup();
  renderApp("/dashboard/claims");
  await waitFor(() => expect(claimTexts().length).toBeGreaterThan(0));
  return user;
}

describe("Claims", () => {
  it("lists claims from the last 30 days, with the old one hidden", async () => {
    await open();
    expect(claimTexts()).toHaveLength(6);
    expect(claimTexts()).not.toContain("Lithuania will start conscripting women next year.");
    expect(summary()).toContain("6 of 7 claims · all platforms · last 30 days");
    expect(claimButtons()).toHaveLength(6);
  });

  it("each claim shows its verdict as a word", async () => {
    await open();
    const first = claimButtons()[0];
    expect(within(first).getByText("False")).toBeInTheDocument();
    expect(first).toHaveTextContent(/23 posts · RU, LT, EN · Telegram \+ Facebook/);
  });

  it("opening a claim shows the explanation and the evidence; closing hides it", async () => {
    const user = await open();
    const btn = screen.getByRole("button", { name: /NATO tanks are carrying out provocations/ });
    expect(btn).toHaveAttribute("aria-expanded", "false");
    await user.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
    const panel = document.getElementById(btn.getAttribute("aria-controls")!)!;
    expect(panel).toHaveTextContent(/exercise announced a week earlier/);
    expect(within(panel).getAllByRole("listitem")).toHaveLength(2);
    expect(within(panel).getAllByText("Contradicts")).toHaveLength(2);
    expect(panel).toHaveTextContent("confidence 0.86");
    expect(within(panel).queryByRole("link", { name: "See its narrative" })).not.toBeInTheDocument();
    await user.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById(btn.getAttribute("aria-controls")!)).toBeNull();
  });

  it("status chips show counts and filter", async () => {
    const user = await open();
    expect(chip(/status/i, /^false/i)).toHaveTextContent("False2");
    expect(chip(/status/i, /^true/i)).toHaveTextContent("True1");
    await user.click(chip(/status/i, /misleading/i));
    await waitFor(() => expect(claimTexts()).toHaveLength(2));
    expect(summary()).toContain("2 of 7 claims · all platforms · last 30 days · misleading");
  });

  it("search combines with the other filters and can be cleared", async () => {
    const user = await open();
    await user.click(chip(/platform/i, /telegram/i));
    await user.type(screen.getByRole("searchbox", { name: /search claims/i }), "nato");
    await waitFor(() => expect(claimTexts()).toEqual(["NATO tanks are carrying out provocations at the Belarusian border."]));
    expect(summary()).toContain("Telegram · last 30 days · matching “nato”");
    await user.click(screen.getByRole("button", { name: /clear filters/i }));
    await waitFor(() => expect(claimTexts()).toHaveLength(6));
    expect(screen.getByRole("searchbox", { name: /search claims/i })).toHaveValue("");
  });

  it("90 days brings back the older claim", async () => {
    const user = await open();
    await user.click(chip(/active between/i, /90 days/i));
    await waitFor(() => expect(claimTexts()).toHaveLength(7));
  });

  it("no matches: empty state with Clear filters", async () => {
    const user = await open();
    await user.type(screen.getByRole("searchbox", { name: /search claims/i }), "zzzz");
    expect(await screen.findByText(/no claims match/i)).toBeInTheDocument();
    await user.click(within(screen.getByRole("region", { name: "Claims" })).getByRole("button", { name: /clear filters/i }));
    await waitFor(() => expect(claimTexts()).toHaveLength(6));
  });

  it("without a backend: explains it", async () => {
    vi.unstubAllEnvs();
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    renderApp("/dashboard/claims");
    expect(await screen.findByRole("alert")).toHaveTextContent(/backend isn't connected yet/i);
  });
});
