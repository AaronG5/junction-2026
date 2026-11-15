import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { resetSettingsForTests } from "./lib/settings";
import { renderApp } from "./test/renderApp";

beforeEach(() => resetSettingsForTests());

describe("routing", () => {
  it.each([
    ["/", /every lie has a first author/i],
    ["/dashboard/narratives", /^narratives$/i],
    ["/dashboard/analyze", /analyze a post/i],
    ["/dashboard/claims", /^claims$/i],
    ["/dashboard/sources", /^sources$/i],
    ["/dashboard/sources/kanalas_b", /channel report/i],
    ["/dashboard/system", /^system$/i],
  ])("%s renders its page heading", (path, heading) => {
    renderApp(path);
    expect(screen.getByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
  });

  it("/dashboard redirects to the narratives home", () => {
    const { currentPath } = renderApp("/dashboard");
    expect(currentPath()).toBe("/dashboard/narratives");
    expect(screen.getByRole("heading", { level: 1, name: /^narratives$/i })).toBeInTheDocument();
  });

  it.each(["/nope", "/dashboard/nope"])("unknown address %s shows not-found with a way back", (path) => {
    renderApp(path);
    expect(screen.getByRole("heading", { level: 1, name: /page not found/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open the dashboard/i })).toHaveAttribute("href", "/dashboard/narratives");
  });

  it("landing links to the dashboard home", () => {
    renderApp("/");
    const links = screen.getAllByRole("link", { name: /open the dashboard/i });
    expect(links.length).toBeGreaterThanOrEqual(2);
    for (const l of links) expect(l).toHaveAttribute("href", "/dashboard/narratives");
  });
});

describe("dashboard sidebar", () => {
  it("lists Narratives first and marks the current page", () => {
    renderApp("/dashboard/claims");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual(["Narratives", "Analyze a post", "Claims", "Sources", "System"]);
    expect(within(nav).getByRole("link", { name: "Claims" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Narratives" })).not.toHaveAttribute("aria-current");
  });

  it("navigates between pages", async () => {
    const user = userEvent.setup();
    const { currentPath } = renderApp("/dashboard/narratives");
    await user.click(screen.getByRole("link", { name: "Sources" }));
    expect(currentPath()).toBe("/dashboard/sources");
    expect(screen.getByRole("heading", { level: 1, name: /^sources$/i })).toBeInTheDocument();
  });

  it("brand links back to the landing page", () => {
    renderApp("/dashboard/system");
    expect(screen.getByRole("link", { name: /veritas lens/i })).toHaveAttribute("href", "/");
  });

  it("shows the current data source (Hybrid by default)", () => {
    renderApp("/dashboard/narratives");
    expect(screen.getByTestId("data-source")).toHaveTextContent("Hybrid");
  });
});

describe("theme switch", () => {
  it("sets and clears data-theme on the page", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/narratives");
    const group = screen.getByRole("group", { name: /color theme/i });
    expect(within(group).getByRole("button", { name: "Auto" })).toHaveAttribute("aria-pressed", "true");

    await user.click(within(group).getByRole("button", { name: "Dark" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
    expect(within(group).getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");

    await user.click(within(group).getByRole("button", { name: "Light" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");

    await user.click(within(group).getByRole("button", { name: "Auto" }));
    expect(document.documentElement).not.toHaveAttribute("data-theme");
  });

  it("remembers the choice across reloads", async () => {
    const user = userEvent.setup();
    const first = renderApp("/dashboard/narratives");
    await user.click(screen.getByRole("button", { name: "Dark" }));
    first.unmount();
    resetSettingsForTests(); // simulates a page reload: state comes back from storage
    renderApp("/dashboard/narratives");
    expect(screen.getByRole("button", { name: "Dark" })).toHaveAttribute("aria-pressed", "true");
  });
});
