/**
 * Preferences.
 *  - theme: per browser (localStorage).
 *  - dataMode + shareAnalyses: shared by everyone using this install, stored by
 *    the backend (GET/PUT /api/settings). The browser keeps a copy so the page
 *    shows the last choice at once, and uses it when the backend can't be reached.
 *    In demo mode they stay in this browser.
 * Everything still works when storage is blocked.
 */
import { useSyncExternalStore } from "react";
import { ApiError, api, isDemoMode } from "../api/client";
import type { AppSettings } from "../api/types";

export type Theme = "system" | "light" | "dark";
export type DataMode = "central" | "hybrid" | "local";

export interface Settings {
  theme: Theme;
  dataMode: DataMode;
  shareAnalyses: boolean;
}

/** Where the shared settings are kept right now, for the System page. */
export interface SettingsSync {
  state: "browser" | "saving" | "saved" | "failed";
  /** A plain sentence when saving failed or only this browser has the choice. */
  message?: string;
}

const KEY = "veritas-lens.settings";
const DEFAULTS: Settings = { theme: "system", dataMode: "hybrid", shareAnalyses: false };
export const OFFLINE_NOTE = "The backend isn't reachable, so this choice is saved in this browser only for now.";

function load(): Settings {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return { ...DEFAULTS, ...JSON.parse(raw) };
  } catch { /* storage blocked or corrupt: use defaults */ }
  return { ...DEFAULTS };
}

let current: Settings = load();
let sync: SettingsSync = { state: "browser" };
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((l) => l());

function store() {
  try { localStorage.setItem(KEY, JSON.stringify(current)); } catch { /* ignore */ }
}

function setSync(next: SettingsSync) {
  sync = next;
  notify();
}

function applyShared(s: AppSettings) {
  current = { ...current, dataMode: s.dataMode, shareAnalyses: s.dataMode === "local" ? false : s.shareAnalyses };
  store();
  notify();
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function getSettings(): Settings {
  return current;
}

let saving = 0;

export function updateSettings(patch: Partial<Settings>) {
  const before = current;
  current = { ...current, ...patch };
  // Sharing makes no sense when nothing may leave the computer.
  if (current.dataMode === "local") current.shareAnalyses = false;
  store();
  if (patch.theme) applyTheme(current.theme);
  notify();

  const shared = current.dataMode !== before.dataMode || current.shareAnalyses !== before.shareAnalyses;
  if (!shared || isDemoMode()) return;
  const mine = ++saving;
  setSync({ state: "saving" });
  api.saveAppSettings({ dataMode: current.dataMode, shareAnalyses: current.shareAnalyses }).then(
    (saved) => {
      if (mine !== saving) return; // a newer change is on its way
      applyShared(saved);
      setSync({ state: "saved" });
    },
    (error) => {
      if (mine !== saving) return;
      if (error instanceof ApiError) {
        // The backend refused it: go back to what it has.
        applyShared({ dataMode: before.dataMode, shareAnalyses: before.shareAnalyses });
        setSync({ state: "failed", message: error.message });
      } else {
        setSync({ state: "browser", message: OFFLINE_NOTE });
      }
    },
  );
}

/** Normal mode: take the shared settings from the backend (call once at start). */
export async function loadSharedSettings(): Promise<void> {
  if (isDemoMode()) return;
  try {
    applyShared(await api.appSettings());
    setSync({ state: "saved" });
  } catch {
    setSync({ state: "browser", message: OFFLINE_NOTE });
  }
}

/** Test helper: forget in-memory state and reload from storage. */
export function resetSettingsForTests() {
  current = load();
  sync = { state: "browser" };
  saving = 0;
  notify();
}

const subscribe = (cb: () => void) => { listeners.add(cb); return () => { listeners.delete(cb); }; };

export function useSettings(): Settings {
  return useSyncExternalStore(subscribe, () => current);
}

export function useSettingsSync(): SettingsSync {
  return useSyncExternalStore(subscribe, () => sync);
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
