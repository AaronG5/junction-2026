import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, CheckTimeout, NARRATIVE_UNAVAILABLE, httpApi, narrativePolling, textId } from "../api/client";
import type { NarrativeResult } from "../api/types";
import { setDemoDelay } from "../demo";
import { narrativeClean, narrativeFlagged } from "../demo/data";
import { resetSettingsForTests } from "../lib/settings";
import { renderApp } from "../test/renderApp";
import { CLEAN_EXAMPLE, FLAGGED_EXAMPLE, echoSentence, highlight, inputKind } from "./NarrativeCheck";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const saved = { ...narrativePolling };
beforeEach(() => {
  resetSettingsForTests();
  setDemoDelay(0);
  narrativePolling.intervalMs = 1;
  narrativePolling.maxWaitMs = 2000;
});
afterEach(() => {
  Object.assign(narrativePolling, saved);
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

/** The flagged demo result as the backend sends it while the judge is still reading. */
const pendingOf = (r: NarrativeResult, id: string): NarrativeResult => ({
  ...r, id, status: "pending", content: { ...r.content!, label: "none", score: 0, techniques: [], reason: "" },
});

/**
 * A fake backend for /api/ext: /batch answers pending, /result stays pending
 * until release() is called, then answers `final`.
 */
function fakeExt(final: NarrativeResult) {
  let released = false;
  const calls: { url: string; body: unknown }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (!url.startsWith("/api/ext/")) return new Response("not here", { status: 502 }); // other screens: no backend
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, body });
    const id = body?.items?.[0]?.id ?? new URL(url, "http://x").searchParams.get("ids");
    if (released) return json({ results: [{ ...final, id }] });
    return json({ results: [pendingOf(final, id)] });
  });
  vi.stubGlobal("fetch", fn);
  return { calls, release: () => { released = true; } };
}

describe("checkText over HTTP", () => {
  it("posts the text with a stable id, polls /api/ext/result until done and reports each answer", async () => {
    const ext = fakeExt(narrativeFlagged);
    const updates: string[] = [];
    const p = httpApi.checkText(FLAGGED_EXAMPLE, (r) => {
      updates.push(r.status);
      if (updates.length === 2) ext.release();
    });
    const r = await p;
    expect(r.status).toBe("done");
    expect(r.content?.label).toBe("potentially_propaganda");
    const [batch, ...polls] = ext.calls;
    expect(batch.url).toBe("/api/ext/batch");
    const item = (batch.body as { client: string; items: { id: string; author: string; text: string }[] });
    expect(item.client).toBe("dashboard");
    expect(item.items).toHaveLength(1);
    expect(item.items[0].id).toMatch(/^[A-Za-z0-9]{1,40}$/);
    expect(item.items[0].author).toBe("");
    expect(polls.length).toBeGreaterThanOrEqual(2);
    expect(polls[0].url).toBe(`/api/ext/result?ids=${item.items[0].id}`);
    expect(updates[0]).toBe("pending");
    expect(updates.at(-1)).toBe("done");
  });

  it("gives up after the time limit with the results so far", async () => {
    fakeExt(narrativeFlagged);
    narrativePolling.maxWaitMs = 20;
    const err = await httpApi.checkText(FLAGGED_EXAMPLE).catch((e) => e);
    expect(err).toBeInstanceOf(CheckTimeout);
    expect((err as CheckTimeout).partial.content?.narrative?.source).toBe("euvsdisinfo");
    expect((err as CheckTimeout).message).toMatch(/didn't finish within a minute/);
  });

  it.each([404, 503])("a %s from /api/ext means the module isn't available, said plainly", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ detail: "Not Found" }, status)));
    const err = await httpApi.checkText(FLAGGED_EXAMPLE).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe(NARRATIVE_UNAVAILABLE);
  });

  it("an item error (the local AI is down) becomes an error", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_i: RequestInfo | URL, init?: RequestInit) => {
      const id = JSON.parse(String(init!.body)).items[0].id;
      return json({ results: [{ id, status: "error", error: "The local AI isn't answering." }] });
    }));
    const err = await httpApi.checkText(FLAGGED_EXAMPLE).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toMatch(/local AI isn't answering/);
  });

  it("textId is stable, distinct and fits the extension API's id rule", () => {
    expect(textId("abc")).toBe(textId("abc"));
    expect(textId("abc")).not.toBe(textId("abd"));
    expect(textId(FLAGGED_EXAMPLE)).toMatch(/^[A-Za-z0-9]{1,40}$/);
  });
});

describe("helpers", () => {
  it("tells links, other links, short text and text apart", () => {
    expect(inputKind("  ")).toBe("empty");
    expect(inputKind("https://t.me/kanal/5")).toBe("link");
    expect(inputKind("https://example.com/news/1")).toBe("otherLink");
    expect(inputKind("too short")).toBe("short");
    expect(inputKind(FLAGGED_EXAMPLE)).toBe("text");
    expect(inputKind("Read this https://example.com/news/1 before they delete it")).toBe("text");
  });

  it("highlights each quote once, case-insensitively, without overlaps", () => {
    const parts = highlight("The Kyiv regime is a puppet. The kyiv REGIME lies.", [
      { quote: "kyiv regime", label: "name-calling" },
      { quote: "regime is", label: "overlap" },
      { quote: "not in the text", label: "x" },
    ]);
    expect(parts.filter((p) => p.flag).map((p) => p.text)).toEqual(["Kyiv regime"]);
    expect(parts.map((p) => p.text).join("")).toBe("The Kyiv regime is a puppet. The kyiv REGIME lies.");
  });

  it("words the state-media echo", () => {
    expect(echoSentence(narrativeFlagged.content!.state_media_echo[0])).toBe("This narrative aired 253× on RT, Jan–Oct 2024.");
    expect(echoSentence({ text: "x", source: "ria", n_aired: null, first: null, last: null, sim: 0.7 })).toBe("This narrative appeared on RIA Novosti.");
    expect(echoSentence({ text: "x", source: "rt", n_aired: 4, first: "2023-11-02", last: "2024-02-01", sim: 0.7 }))
      .toBe("This narrative aired 4× on RT, Nov 2023–Feb 2024.");
  });
});

const box = () => screen.getByLabelText(/text of a post, or a link to one/i);
const report = () => screen.getByRole("region", { name: "Narrative check" });

describe("the start page's text box runs the narrative check", () => {
  it("pending: shows the closest narrative first, then the judge's verdict", async () => {
    const ext = fakeExt(narrativeFlagged);
    const user = userEvent.setup();
    renderApp("/");
    await user.type(box(), FLAGGED_EXAMPLE);
    await user.click(screen.getByRole("button", { name: "Check it" }));
    expect(await screen.findByText("Closest known narrative")).toBeInTheDocument();
    expect(within(report()).getByText(/gemma is reading the post/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Checking…" })).toBeDisabled();
    expect(within(report()).queryByText("Potentially propaganda")).not.toBeInTheDocument();
    ext.release();
    expect(await within(report()).findByText("Potentially propaganda")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Check it" })).toBeEnabled();
  });

  it("flagged (demo): verdict, narrative with source, techniques highlighted, Telegram copy, state-media echo, disclaimer", async () => {
    vi.stubEnv("VITE_DEMO_DATA", "true");
    const user = userEvent.setup();
    renderApp("/");
    await user.click(screen.getByRole("button", { name: "A pro-Kremlin post" }));
    expect(box()).toHaveValue(FLAGGED_EXAMPLE);
    const r = await screen.findByRole("region", { name: "Narrative check" });
    await within(r).findByText("Potentially propaganda");
    expect(r).toHaveTextContent("Narrative it asserts");
    expect(r).toHaveTextContent("Ukraine is a puppet state run by the United States and NATO.");
    expect(r).not.toHaveTextContent(/Documented by/);
    expect(r).toHaveTextContent(narrativeFlagged.content!.reason);
    expect(within(within(r).getByRole("list", { name: "Techniques" })).getAllByRole("listitem")).toHaveLength(4);
    const marks = within(r).getByTestId("checked-text").querySelectorAll("mark");
    expect(marks).toHaveLength(4);
    expect(marks[0]).toHaveTextContent("The Kyiv regime (flagged: name-calling)");
    expect(within(r).getByText("Copies a known pro-Kremlin Telegram post")).toBeInTheDocument();
    expect(within(r).getByRole("link", { name: "@kanal_a" })).toHaveAttribute("href", "https://t.me/kanal_a/58214");
    expect(r).toHaveTextContent("This narrative aired 253× on RT, Jan–Oct 2024.");
    expect(within(r).getByText("Closest matches")).toBeInTheDocument();
    expect(r).toHaveTextContent("Automated assessment, may be wrong. Not a claim about who is behind any account.");
    expect(r).not.toHaveTextContent(/fake|bot\b/i);
  });

  it("clean (demo): says no known narrative was found", async () => {
    vi.stubEnv("VITE_DEMO_DATA", "true");
    const user = userEvent.setup();
    renderApp("/");
    await user.type(box(), CLEAN_EXAMPLE);
    await user.click(screen.getByRole("button", { name: "Check it" }));
    const r = await screen.findByRole("region", { name: "Narrative check" });
    await within(r).findByText("No known propaganda narrative found");
    expect(within(r).queryByText("Potentially propaganda")).not.toBeInTheDocument();
    expect(within(r).queryByText("Copies a known pro-Kremlin Telegram post")).not.toBeInTheDocument();
    expect(within(r).queryByRole("list", { name: "Techniques" })).not.toBeInTheDocument();
    expect(narrativeClean.content!.label).toBe("none");
  });

  it("error: the module isn't deployed (404), with a retry", async () => {
    vi.stubGlobal("fetch", vi.fn(async (u: RequestInfo | URL) =>
      String(u).startsWith("/api/ext/") ? json({ detail: "Not Found" }, 404) : new Response("not here", { status: 502 })));
    const user = userEvent.setup();
    renderApp("/");
    await user.type(box(), FLAGGED_EXAMPLE);
    await user.click(screen.getByRole("button", { name: "Check it" }));
    const alert = await within(await screen.findByRole("region", { name: "Narrative check" })).findByRole("alert");
    expect(alert).toHaveTextContent(NARRATIVE_UNAVAILABLE);
    expect(within(alert).getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("error: no network", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    const user = userEvent.setup();
    renderApp("/");
    await user.type(box(), FLAGGED_EXAMPLE);
    await user.click(screen.getByRole("button", { name: "Check it" }));
    expect(await within(await screen.findByRole("region", { name: "Narrative check" })).findByRole("alert"))
      .toHaveTextContent(/backend isn't connected yet/i);
  });

  it("short text is refused without calling the backend", async () => {
    const fetchMock = vi.fn(async (_u: RequestInfo | URL) => new Response("not here", { status: 502 }));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderApp("/");
    await user.type(box(), "Wake up!");
    await user.click(screen.getByRole("button", { name: "Check it" }));
    expect(document.getElementById("trace-msg")).toHaveTextContent(/too short/i);
    expect(box()).toHaveAttribute("aria-invalid", "true");
    expect(fetchMock.mock.calls.some(([u]) => String(u).startsWith("/api/ext/"))).toBe(false);
  });
});

describe("Analyze runs the narrative check for text", () => {
  it("text gets the check; a link afterwards gets the link report instead", async () => {
    vi.stubEnv("VITE_DEMO_DATA", "true");
    const user = userEvent.setup();
    renderApp("/dashboard/analyze");
    const input = screen.getByLabelText(/text of a post, or a telegram or facebook link/i);
    await user.type(input, FLAGGED_EXAMPLE);
    await user.click(within(screen.getByRole("main")).getByRole("button", { name: /^analyz/i }));
    const r = await screen.findByRole("region", { name: "Narrative check" });
    await within(r).findByText("Potentially propaganda");

    await user.clear(input);
    await user.type(input, "https://t.me/kanalas_b/4521");
    await user.click(within(screen.getByRole("main")).getByRole("button", { name: /^analyz/i }));
    await screen.findByRole("heading", { name: "How it spread" });
    await waitFor(() => expect(screen.queryByRole("region", { name: "Narrative check" })).not.toBeInTheDocument());
  });
});
