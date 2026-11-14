import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  // Some tests run in plain Node (no page), e.g. the fixture checks.
  if (typeof document === "undefined") return;
  cleanup();
  try { localStorage.clear(); } catch { /* ignore */ }
  document.documentElement.removeAttribute("data-theme");
});
