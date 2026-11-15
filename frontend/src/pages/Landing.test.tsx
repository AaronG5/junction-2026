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

const linkInput = () => screen.getByLabelText(/text of a post, or a link to one/i);
const storyCards = () => Array.from(document.querySelectorAll<HTMLAnchorElement>("a.pick"));
const stepHeading = () => screen.getByRole("heading", { level: 1 });
const stepButton = (name: RegExp) => within(screen.getByRole("navigation", { name: /walk-through steps/i })).getByRole("button", { name });

describe("landing page", () => {
  it("has the pitch, the link box, 'Open the dashboard' in the masthead and in the band", async () => {
    renderApp("/");
    expect(screen.getByRole("heading", { level: 1, name: /every lie has a first author/i })).toBeInTheDocument();
    expect(linkInput()).toBeInTheDocument();
    const dashboardLinks = screen.getAllByRole("link", { name: /open the dashboard/i });
    expect(dashboardLinks).toHaveLength(2);
    dashboardLinks.forEach((l) => expect(l).toHaveAttribute("href", "/dashboard/analyze"));
    expect(await screen.findByText("18,420")).toBeInTheDocument(); // the posts-read figure in the sovereignty band
  });

  it("rejects a link that isn't Telegram or Facebook", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/");
    await user.type(linkInput(), "https://example.com/x");
    await user.click(screen.getByRole("button", { name: "Check it" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/we can only open telegram and facebook links/i);
    expect(linkInput()).toHaveAttribute("aria-invalid", "true");
    expect(currentPath()).toBe("/");
  });

  it("a valid link opens the walk-through at step 1", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/");
    await user.type(linkInput(), "https://t.me/someone/42");
    await user.click(screen.getByRole("button", { name: "Check it" }));
    await waitFor(() => expect(currentPath()).toBe("/story/nato-border"));
    expect(await screen.findByRole("heading", { level: 1, name: /here's the post/i })).toBeInTheDocument();
    expect(screen.getByText(/demo mode: any link shows this example story/i)).toBeInTheDocument();
  });

  it("lists trending stories with their status", async () => {
    renderApp("/");
    await waitFor(() => expect(storyCards()).toHaveLength(3));
    expect(storyCards()[2]).toHaveTextContent("Misleading");
    expect(storyCards()[0]).toHaveAttribute("href", "/story/nato-border");
  });

  it("the platform filter narrows the stories, and All brings them back", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await waitFor(() => expect(storyCards()).toHaveLength(3));
    const platform = screen.getByRole("group", { name: "Platform" });
    expect(within(platform).getByRole("button", { name: "All platforms" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(platform).getByRole("button", { name: "Facebook" }));
    await waitFor(() => expect(storyCards()).toHaveLength(1));
    await user.click(within(platform).getByRole("button", { name: "All platforms" }));
    await waitFor(() => expect(storyCards()).toHaveLength(3));
  });

  it("the start page offers one filter axis; date and status live in the dashboard", () => {
    renderApp("/");
    expect(screen.getByRole("group", { name: "Platform" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Time" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /status of the main claim/i })).not.toBeInTheDocument();
  });

  it("without a backend, tracing a link explains the problem and stays on the page", async () => {
    vi.unstubAllEnvs();
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    const user = userEvent.setup();
    const { currentPath } = renderApp("/");
    await user.type(linkInput(), "https://t.me/someone/42");
    await user.click(screen.getByRole("button", { name: "Check it" }));
    await waitFor(() => expect(within(screen.getByRole("main")).getAllByRole("alert").some((a) => /couldn't reach providence/i.test(a.textContent!))).toBe(true));
    expect(currentPath()).toBe("/");
    expect(screen.getByRole("button", { name: "Check it" })).toBeEnabled();
  });
});

describe("guided walk-through", () => {
  it("picking a story starts at step 1", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/");
    await waitFor(() => expect(storyCards()).toHaveLength(3));
    await user.click(storyCards()[1]);
    expect(currentPath()).toBe("/story/electricity");
    expect(await screen.findByRole("heading", { level: 1, name: /here's the post/i })).toBeInTheDocument();
    expect(stepButton(/what it says/i)).toHaveAttribute("aria-current", "step");
    expect(screen.getByText(/Из-за санкций ЕС/)).toBeInTheDocument();
  });

  it("Next and Back move one step; step buttons jump; each step answers its question", async () => {
    const user = userEvent.setup();
    renderApp("/story/nato-border");
    await screen.findByRole("heading", { level: 1, name: /here's the post/i });

    await user.click(screen.getByRole("button", { name: /next: is it true/i }));
    expect(stepHeading()).toHaveTextContent("Its main claim doesn't hold up.");
    expect(screen.getByText("False")).toBeInTheDocument();
    expect(stepHeading()).toHaveFocus();

    await user.click(screen.getByRole("button", { name: /next: where it started/i }));
    expect(stepHeading()).toHaveTextContent(/started on Russian state media/);
    const chain = within(screen.getByRole("list", { name: /how it spread, earliest first/i })).getAllByRole("listitem");
    expect(chain).toHaveLength(4);
    expect(chain[0]).toHaveTextContent("rt.com");
    expect(chain[2]).toHaveTextContent("The post you picked");

    await user.click(screen.getByRole("button", { name: /← back/i }));
    expect(stepHeading()).toHaveTextContent("Its main claim doesn't hold up.");

    await user.click(stepButton(/how far it went/i));
    expect(stepHeading()).toHaveTextContent(/copied 41 times/);
    expect(screen.getByRole("list", { name: /copies by language/i })).toBeInTheDocument();
    expect(stepButton(/is it true/i)).toHaveAttribute("data-state", "done");
  });

  it("the step lives in the address, so links can open any step", async () => {
    renderApp("/story/refugee-benefits?step=2");
    expect(await screen.findByRole("heading", { level: 1, name: /not false, but it's misleading/i })).toBeInTheDocument();
    expect(screen.getByText("Misleading")).toBeInTheDocument();
  });

  it("an out-of-range step falls back to a valid one", async () => {
    renderApp("/story/nato-border?step=99");
    expect(await screen.findByRole("heading", { level: 1, name: /one of 1,284 posts/i })).toBeInTheDocument();
  });

  it("step 1's Back returns to the start page", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/story/nato-border");
    await screen.findByRole("heading", { level: 1, name: /here's the post/i });
    await user.click(screen.getByRole("button", { name: /pick another/i }));
    expect(currentPath()).toBe("/");
  });

  it("the last step ends with the dashboard, the full report and tracing another post", async () => {
    renderApp("/story/nato-border?step=5");
    await screen.findByRole("heading", { level: 1, name: /one of 1,284 posts/i });
    expect(screen.queryByRole("button", { name: /^next/i })).not.toBeInTheDocument();
    const main = screen.getByRole("main");
    expect(within(main).getByRole("link", { name: "Open the dashboard" })).toHaveAttribute("href", "/dashboard/analyze");
    expect(within(main).getByRole("link", { name: "Open the full report" }))
      .toHaveAttribute("href", "/dashboard/analyze?url=" + encodeURIComponent("https://t.me/kanalas_b/4521"));
    expect(within(main).getByRole("link", { name: "Trace another post" })).toHaveAttribute("href", "/");
  });

  it("'Open the full report' leads to the analysis of that post", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/story/nato-border?step=5");
    await user.click(await screen.findByRole("link", { name: "Open the full report" }));
    expect(currentPath()).toBe("/dashboard/analyze");
    expect(await screen.findByRole("heading", { name: "How it spread" })).toBeInTheDocument();
  });

  it("an unknown story explains itself and offers a way back", async () => {
    renderApp("/story/nope");
    expect(await screen.findByRole("alert")).toHaveTextContent("That story isn't available.");
    expect(screen.getByRole("link", { name: /back to the start page/i })).toHaveAttribute("href", "/");
  });
});
