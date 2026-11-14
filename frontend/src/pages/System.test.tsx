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

const radio = (name: RegExp) => screen.getByRole("radio", { name });
const radioNames = () => screen.getAllByRole("radio").map((r) => r.getAttribute("aria-labelledby") && document.getElementById(r.getAttribute("aria-labelledby")!)!.textContent);
const share = () => screen.getByRole("checkbox", { name: /share my analyses/i });

describe("System", () => {
  it("each option is named by its mode only, with the details as a description", () => {
    renderApp("/dashboard/system");
    expect(radioNames()).toEqual(["Central server", "Hybrid", "Only this computer"]);
    expect(screen.getByRole("radio", { name: "Hybrid" })).toHaveAccessibleDescription(/analysis runs here/i);
  });

  it("Hybrid is the default data source", () => {
    renderApp("/dashboard/system");
    expect(radio(/hybrid/i)).toBeChecked();
    expect(screen.getByRole("group", { name: /data source/i })).toBeInTheDocument();
    expect(share()).not.toBeChecked();
    expect(share()).toBeEnabled();
  });

  it("choosing a data source updates the note and the sidebar", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/system");
    await user.click(radio(/central server/i));
    expect(radio(/central server/i)).toBeChecked();
    expect(screen.getByTestId("privacy-note")).toHaveTextContent(/sees the links you analyze/i);
    expect(screen.getByTestId("data-source")).toHaveTextContent("Central server");
  });

  it("sharing can't be on in local-only mode", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/system");
    await user.click(share());
    expect(share()).toBeChecked();
    await user.click(radio(/only this computer/i));
    expect(share()).not.toBeChecked();
    expect(share()).toBeDisabled();
    expect(screen.getByText(/not available when nothing may leave this computer/i)).toBeInTheDocument();
    expect(screen.getByTestId("data-source")).toHaveTextContent("Only this computer");
  });

  it("the choice is remembered after a reload", async () => {
    const user = userEvent.setup();
    const first = renderApp("/dashboard/system");
    await user.click(radio(/only this computer/i));
    first.unmount();
    resetSettingsForTests();
    renderApp("/dashboard/system");
    expect(radio(/only this computer/i)).toBeChecked();
  });

  it("shows the local AI, the services and how to switch to a stronger model", async () => {
    const user = userEvent.setup();
    renderApp("/dashboard/system");
    expect(await screen.findByText("gemma-4-E2B-it")).toBeInTheDocument();
    expect(screen.getByText("embeddinggemma-300m")).toBeInTheDocument();
    const services = screen.getByRole("heading", { name: "Services" }).closest("section")!;
    expect(within(services).getAllByRole("row")).toHaveLength(3);
    expect(within(services).getAllByText("Running")).toHaveLength(3);

    const how = screen.getByRole("button", { name: /how to switch/i });
    expect(how).toHaveAttribute("aria-expanded", "false");
    await user.click(how);
    expect(screen.getByText("launch.bat --reselect")).toBeInTheDocument();
    expect(screen.getByText("./launch.sh --reselect")).toBeInTheDocument();
  });

  it("without a backend: the data source can still be chosen, the AI status explains", async () => {
    vi.unstubAllEnvs();
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    renderApp("/dashboard/system");
    expect(await screen.findByRole("alert")).toHaveTextContent(/backend isn't connected yet/i);
    expect(radio(/hybrid/i)).toBeEnabled();
  });
});
