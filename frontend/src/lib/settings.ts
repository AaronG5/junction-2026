/**
 * Per-browser preferences (theme, data source). Stored in localStorage when
 * available; everything still works when storage is blocked.
 * The data source choice will move to the backend once it exists.
 */
import { useSyncExternalStore } from "react";

export type Theme = "system" | "light" | "dark";
export type DataMode = "central" | "hybrid" | "local";

export interface Settings {
  theme: Theme;
  dataMode: DataMode;
  shareAnalyses: boolean;
}

const KEY = "veritas-lens.settings";
const DEFAULTS: Settings = { theme: "system", dataMode: "hybrid", shareAnalyses: false };

function load(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch { /* storage blocked or corrupt: use defaults */ }
  return { ...DEFAULTS };
}

let current: Settings = load();
const listeners = new Set<() => void>();

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function getSettings(): Settings {
  return current;
}

export function updateSettings(patch: Partial<Settings>) {
  current = { ...current, ...patch };
  // Sharing makes no sense when nothing may leave the computer.
  if (current.dataMode === "local") current.shareAnalyses = false;
  try { localStorage.setItem(KEY, JSON.stringify(current)); } catch { /* ignore */ }
  if (patch.theme) applyTheme(current.theme);
  listeners.forEach((l) => l());
}

/** Test helper: forget in-memory state and reload from storage. */
export function resetSettingsForTests() {
  current = load();
  listeners.forEach((l) => l());
}

export function useSettings(): Settings {
  return useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => listeners.delete(cb); },
    () => current,
  );
}

export const DATA_MODES: Record<DataMode, { name: string; short: string; text: string; privacy: string }> = {
  central: {
    name: "Central server",
    short: "Our server answers everything",
    text: "Our server answers using its full collection. Nothing heavy runs on this computer.",
    privacy: "The central server sees the links you analyze.",
  },
  hybrid: {
    name: "Hybrid",
    short: "Central server + this computer",
    text: "Analysis runs here. When local data is thin, the central server's collection fills the gaps.",
    privacy: "The central server is asked about similar posts, not about which links you opened. Your analyses stay here unless you choose to share them.",
  },
  local: {
    name: "Only this computer",
    short: "Fully offline",
    text: "Works offline with your own data only. Nothing leaves this computer.",
    privacy: "No requests leave this computer. Results are limited to what you have collected yourself.",
  },
};
