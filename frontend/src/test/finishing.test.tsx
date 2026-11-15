import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackendUnavailable } from "../api/client";
import { ErrorState } from "../components/States";
import { setDemoDelay } from "../demo";
import { resetSettingsForTests } from "../lib/settings";
import { renderApp } from "./renderApp";

beforeEach(() => {
  resetSettingsForTests();
  setDemoDelay(0);
  vi.stubEnv("VITE_DEMO_DATA", "true");
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("phone menu", () => {
  it("the Menu button opens and closes the menu", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    const button = screen.getByRole("button", { name: "Menu" });
    const sidebar = document.querySelector(".sidebar")!;
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveAttribute("aria-controls", "side-menu");
    expect(sidebar).toHaveAttribute("data-open", "false");
    await user.click(button);
    expect(screen.getByRole("button", { name: "Close" })).toHaveAttribute("aria-expanded", "true");
    expect(sidebar).toHaveAttribute("data-open", "true");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(sidebar).toHaveAttribute("data-open", "false");
  });

  it("choosing a page closes the menu", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/dashboard/narratives");
    await user.click(screen.getByRole("button", { name: "Menu" }));
    await user.click(within(screen.getByRole("navigation", { name: "Main" })).getByRole("link", { name: "Claims" }));
    expect(currentPath()).toBe("/dashboard/claims");
    expect(document.querySelector(".sidebar")).toHaveAttribute("data-open", "false");
  });
});

describe("keyboard", () => {
  it.each(["/dashboard/narratives", "/"])("on %s the first Tab reaches 'Skip to content', which points at the main content", async (path) => {
    const user = userEvent.setup();
    renderApp(path);
    await user.tab();
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", "#main");
    expect(document.getElementById("main")?.tagName).toBe("MAIN");
  });

  it("clusters can be chosen with the keyboard", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    const row = await screen.findByRole("button", { name: /refugees get bigger benefits/i });
    row.focus();
    await user.keyboard("{Enter}");
    expect(row).toHaveAttribute("aria-pressed", "true");
  });

  it("filter chips work with the keyboard", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/claims");
    const chip = within(screen.getByRole("group", { name: /platform/i })).getByRole("button", { name: "Facebook" });
    chip.focus();
    await user.keyboard(" ");
    expect(chip).toHaveAttribute("aria-pressed", "true");
  });
});

describe("browser tab titles", () => {
  it.each([
    ["/", "Providence"],
    ["/dashboard/narratives", "Narratives · Providence"],
    ["/dashboard/analyze", "Analyze a post · Providence"],
    ["/dashboard/claims", "Claims · Providence"],
    ["/dashboard/sources", "Sources · Providence"],
    ["/dashboard/system", "System · Providence"],
    ["/nope", "Page not found · Providence"],
  ])("%s → %s", async (path, title) => {
    renderApp(path);
    await waitFor(() => expect(document.title).toBe(title));
  });

  it("a channel report and a walk-through step name what's on screen", async () => {
    const first = renderApp("/dashboard/sources/kanalas_b");
    await waitFor(() => expect(document.title).toBe("@kanalas_b · Channel report · Providence"));
    first.unmount();
    renderApp("/story/electricity?step=3");
    await waitFor(() => expect(document.title).toBe("Where it started · Energy prices · Providence"));
  });
});

describe("'can't reach the server' message", () => {
  it("developers get the demo-mode hint", () => {
    vi.stubEnv("DEV", true);
    render(<ErrorState error={new BackendUnavailable()} />);
    expect(screen.getByRole("alert")).toHaveTextContent("npm run dev:demo");
  });

  it("everyone else gets launcher guidance, without developer commands", () => {
    vi.stubEnv("DEV", false);
    render(<ErrorState error={new BackendUnavailable()} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Providence can't reach its server right now.");
    expect(alert).toHaveTextContent("launch.bat");
    expect(alert).not.toHaveTextContent("npm");
  });
});
