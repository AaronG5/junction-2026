import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { demoApi, setDemoDelay } from "../demo";
import { resetSettingsForTests } from "../lib/settings";
import { renderApp } from "../test/renderApp";

beforeEach(() => {
  resetSettingsForTests();
  setDemoDelay(0);
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const input = () => screen.getByLabelText(/text of a post, or a telegram or facebook link/i);
const analyzeButton = () => within(screen.getByRole("main")).getByRole("button", { name: /^analyz/i });
const reportLoaded = () => screen.findByRole("heading", { name: "How it spread" });

describe("Analyze a link (demo data)", () => {
  beforeEach(() => vi.stubEnv("VITE_DEMO_DATA", "true"));

  it("starts empty with examples, and no report", () => {
    renderApp("/dashboard/analyze");
    expect(input()).toHaveValue("");
    expect(screen.getByRole("link", { name: "Telegram link" })).toHaveAttribute("href", expect.stringContaining("/dashboard/analyze?url="));
    expect(screen.queryByRole("heading", { name: "How it spread" })).not.toBeInTheDocument();
  });

  it("rejects a link that isn't Telegram or Facebook, without analyzing", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    await user.type(input(), "https://example.com/news/1");
    await user.click(analyzeButton());
    expect(screen.getByRole("alert")).toHaveTextContent(/we can only open telegram and facebook links/i);
    expect(input()).toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByText("Analyzing")).not.toBeInTheDocument();
    // Typing again clears the error.
    await user.type(input(), "x");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("an empty link is rejected too", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    await user.click(analyzeButton());
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("shows progress through every step, then the report", async () => {
    setDemoDelay(15);
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    await user.type(input(), "https://t.me/kanalas_b/4521");
    await user.click(analyzeButton());
    const steps = await screen.findByRole("list");
    expect(within(steps).getAllByRole("listitem")).toHaveLength(5);
    expect(analyzeButton()).toBeDisabled();
    expect(analyzeButton()).toHaveTextContent("Analyzing…");
    await waitFor(() => expect(steps.querySelector('[data-status="done"]')).not.toBeNull());
    await reportLoaded();
    expect(analyzeButton()).toBeEnabled();
    expect(screen.queryByText("Try an example:")).not.toBeInTheDocument();
  });

  it("a post that fits no narrative says so instead of showing a cluster", async () => {
    const real = demoApi.analyze.bind(demoApi);
    vi.spyOn(demoApi, "analyze").mockImplementation(async (url, onStep) => ({ ...(await real(url, onStep)), cluster: null }));
    renderApp("/dashboard/analyze?url=" + encodeURIComponent("https://t.me/kanalas_b/4521"));
    await reportLoaded();
    expect(screen.getByRole("heading", { name: "Not part of a known narrative yet" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open in Narratives" })).not.toBeInTheDocument();
    vi.restoreAllMocks();
  });

  it("the report has the summary, claims, similar posts, Gemma's reading and the cluster", async () => {
    renderApp("/dashboard/analyze?url=" + encodeURIComponent("https://t.me/kanalas_b/4521"));
    await reportLoaded();
    const summary = screen.getByRole("region", { name: "Summary" });
    expect(within(summary).getByText("Misleading")).toBeInTheDocument();
    expect(within(summary).getByRole("img", { name: "7 out of 10" })).toBeInTheDocument();
    expect(summary).toHaveTextContent("41 copies");
    expect(summary).toHaveTextContent("rt.com");

    const claims = screen.getByRole("heading", { name: "Claims in this post" }).closest("section")!;
    expect(within(claims).getByText("False")).toBeInTheDocument();
    expect(within(claims).getByText(/NATO tanks are carrying out provocations/)).toBeInTheDocument();

    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(6); // header + 5
    expect(within(table).getByText("0.97")).toBeInTheDocument();

    expect(screen.getByText("Alarmist")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /NATO .provocations. at the Belarus border/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open in Narratives" })).not.toBeInTheDocument();
  });

  it("the spread chain goes earliest first, marks this post and says how each copy was found", async () => {
    renderApp("/dashboard/analyze?url=" + encodeURIComponent("https://t.me/kanalas_b/4521"));
    await reportLoaded();
    const chain = screen.getByRole("heading", { name: "How it spread" }).closest("section")!.querySelector("ol")!;
    const items = within(chain).getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("rt.com");
    expect(items[2]).toHaveTextContent("This post");
    expect(items[2].querySelector(".chain-here")).not.toBeNull();
    expect(within(chain).getByLabelText("then, by translation:")).toBeInTheDocument();
    expect(within(chain).getByText("+37 more copies")).toBeInTheDocument();
  });

  it("switches between the original and the English translation; flagged words are marked", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/analyze?url=" + encodeURIComponent("https://t.me/kanalas_b/4521"));
    await reportLoaded();
    const text = screen.getByTestId("post-text");
    expect(text).toHaveTextContent("NATO tankai vėl provokuoja");
    const flagged = text.querySelector("mark")!;
    expect(flagged).toHaveTextContent("provokuoja (flagged: Loaded language)");

    await user.click(screen.getByRole("button", { name: "English" }));
    expect(text).toHaveTextContent("NATO tanks are provoking");
    expect(text).toHaveAttribute("lang", "en");
    expect(screen.getByRole("button", { name: "English" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: /original \(lithuanian\)/i }));
    expect(text).toHaveTextContent("NATO tankai");
  });

  it("the author links to their source report", async () => {
    renderApp("/dashboard/analyze?url=" + encodeURIComponent("https://t.me/kanalas_b/4521"));
    await reportLoaded();
    expect(screen.getByRole("link", { name: "@kanalas_b" })).toHaveAttribute("href", "/dashboard/sources/kanalas_b");
  });

  it("an example link fills the field and runs", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    await user.click(screen.getByRole("link", { name: "Facebook link" }));
    await reportLoaded();
    expect(input()).toHaveValue("https://www.facebook.com/tiesabecenzuros/posts/1188");
  });

  it("analyzing a new link replaces the old report", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/dashboard/analyze?url=" + encodeURIComponent("https://t.me/a/1"));
    await reportLoaded();
    await user.clear(input());
    await user.type(input(), "https://t.me/b/2");
    await user.click(analyzeButton());
    await reportLoaded();
    expect(currentPath()).toBe("/dashboard/analyze");
    expect(input()).toHaveValue("https://t.me/b/2");
  });
});

describe("Analyze a link without a demo", () => {
  it("backend not reachable: explains it, with a retry", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    await user.type(input(), "https://t.me/kanalas_b/4521");
    await user.click(analyzeButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(/backend isn't connected yet/i);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("an error from the backend is shown in its own words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "This channel is private, so it can't be analyzed." }), {
      status: 422, headers: { "content-type": "application/json" },
    })));
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    await user.type(input(), "https://t.me/private_channel/9");
    await user.click(analyzeButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("This channel is private, so it can't be analyzed.");
  });

  it("never calls the backend for an invalid link", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    await user.type(input(), "not a link");
    await user.click(analyzeButton());
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
