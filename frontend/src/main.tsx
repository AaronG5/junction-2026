import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
// Fonts are bundled (no requests to Google), so the app also works offline.
// Archivo is loaded as one variable font: the design uses both of its axes,
// weight 100-900 and width 62-125%, which is where the display/label contrast
// comes from. Plex Mono is the second and last family.
import "@fontsource-variable/archivo/wdth.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "./styles/app.css";
import "./styles/landing.css"; // after app.css: the landing overrides the shared type scale
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
