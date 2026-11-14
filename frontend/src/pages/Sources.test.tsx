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

const names = () => within(screen.getByRole("table")).getAllByRole("link").map((a) => a.textContent);
const header = (name: RegExp) => screen.getByRole("columnheader", { name });

async function open() {
  const user = userEvent.setup();
  renderApp("/dashboard/sources");
  await screen.findByRole("table");
  return user;
}

describe("Sources", () => {
  it("lists every source, sorted by near copies, most first", async () => {
    await open();
    expect(names()).toEqual(["Tiesa be cenzūros", "@kanalas_b", "@prawda_wschod", "Baltijos balsas", "@novosti_baltii", "@kanal_a", "Taupi šeima", "rt.com"]);
    expect(header(/near copies/i)).toHaveAttribute("aria-sort", "descending");
    expect(header(/posts, 30 days/i)).toHaveAttribute("aria-sort", "none");
  });

  it("clicking a column sorts by it; clicking again reverses", async () => {
    const user = await open();
    await user.click(within(header(/posts, 30 days/i)).getByRole("button"));
    expect(header(/posts, 30 days/i)).toHaveAttribute("aria-sort", "descending");
    expect(names()[0]).toBe("Baltijos balsas");
    await user.click(within(header(/posts, 30 days/i)).getByRole("button"));
    expect(header(/posts, 30 days/i)).toHaveAttribute("aria-sort", "ascending");
    expect(names()[0]).toBe("Taupi šeima");
    expect(header(/near copies/i)).toHaveAttribute("aria-sort", "none");
  });

  it("the platform filter narrows the list, including websites", async () => {
    const user = await open();
    const group = screen.getByRole("group", { name: "Platform" });
    await user.click(within(group).getByRole("button", { name: "Facebook" }));
    await waitFor(() => expect(names()).toEqual(["Tiesa be cenzūros", "Taupi šeima"]));
    expect(within(group).getByRole("button", { name: "Facebook" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(group).getByRole("button", { name: "Websites" }));
    await waitFor(() => expect(names()).toEqual(["rt.com"]));
  });

  it("each source links to its report", async () => {
    await open();
    expect(screen.getByRole("link", { name: "@kanalas_b" })).toHaveAttribute("href", "/dashboard/sources/kanalas_b");
  });

  it("says that numbers describe behaviour, not people", async () => {
    await open();
    expect(screen.getByText(/describe behaviour, not people/i)).toBeInTheDocument();
  });
});
