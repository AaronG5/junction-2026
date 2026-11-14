import { screen, within } from "@testing-library/react";
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

async function open(id = "kanalas_b") {
  const user = userEvent.setup();
  renderApp(`/dashboard/sources/${id}`);
  return user;
}

describe("Channel report", () => {
  it("shows the channel, its summary and a note that it describes behaviour", async () => {
    await open();
    expect(await screen.findByRole("heading", { level: 1, name: "@kanalas_b" })).toBeInTheDocument();
    const summary = screen.getByRole("region", { name: "Summary" });
    expect(summary).toHaveTextContent("Posts per day23");
    expect(summary).toHaveTextContent("92% first appeared somewhere else");
    expect(screen.getByText(/does not say who runs it/i)).toBeInTheDocument();
  });

  it("the 30/90-day switch changes the number of bars", async () => {
    const user = await open();
    await screen.findByRole("heading", { level: 1, name: "@kanalas_b" });
    expect(screen.getAllByTestId("bar")).toHaveLength(30);
    expect(screen.getByRole("button", { name: "30 days" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "90 days" }));
    expect(screen.getAllByTestId("bar")).toHaveLength(90);
    expect(screen.getByRole("button", { name: "90 days" })).toHaveAttribute("aria-pressed", "true");
  });

  it("highlights and names the busiest day, and the chart has a description", async () => {
    await open();
    await screen.findByRole("heading", { level: 1, name: "@kanalas_b" });
    const peaks = document.querySelectorAll(".bar-peak");
    expect(peaks).toHaveLength(1);
    expect(peaks[0].getAttribute("title")).toMatch(/: 58 posts$/);
    expect(screen.getByRole("img", { name: /busiest day: .* with 58 posts/i })).toBeInTheDocument();
    expect(screen.getByText(/busiest day:/i, { selector: "b" })).toBeInTheDocument();
  });

  it("lists topics and where the content comes from", async () => {
    await open();
    await screen.findByRole("heading", { level: 1, name: "@kanalas_b" });
    expect(screen.getByText("Anti-NATO")).toBeInTheDocument();
    const upstream = screen.getByRole("heading", { name: "Where its posts come from" }).closest("section")!;
    expect(within(upstream).getAllByRole("row")).toHaveLength(5);
    expect(upstream).toHaveTextContent("1 h 40 min");
  });

  it("recent posts open in Analyze a link", async () => {
    const user = await open();
    await screen.findByRole("heading", { level: 1, name: "@kanalas_b" });
    const recent = screen.getByRole("heading", { name: "Recent posts" }).closest("section")!;
    const links = within(recent).getAllByRole("link");
    expect(links).toHaveLength(4);
    expect(links[0]).toHaveAttribute("href", "/dashboard/analyze?url=" + encodeURIComponent("https://t.me/kanalas_b/4521"));
    expect(within(recent).getByText("True")).toBeInTheDocument();
    await user.click(links[0]);
    expect(await screen.findByRole("heading", { name: "How it spread" })).toBeInTheDocument();
  });

  it("an unknown source shows the backend's message and a way back", async () => {
    await open("nobody");
    expect(await screen.findByRole("alert")).toHaveTextContent("There is no report for this source yet.");
    expect(screen.getByRole("link", { name: /all sources/i })).toHaveAttribute("href", "/dashboard/sources");
  });
});
