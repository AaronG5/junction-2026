import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
// Fonts are bundled (no requests to Google), so the app also works offline.
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-sans/700.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./styles/app.css";
import { AppRoutes } from "./App";
import { preloadDemo } from "./api/client";
import { applyTheme, getSettings, loadSharedSettings } from "./lib/settings";

applyTheme(getSettings().theme);
preloadDemo();
void loadSharedSettings(); // data source + sharing come from the backend (not in demo mode)

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppRoutes />
    </BrowserRouter>
  </StrictMode>,
);
