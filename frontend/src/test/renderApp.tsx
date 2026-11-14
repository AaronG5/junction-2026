import { render } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { AppRoutes } from "../App";

let lastPath = "";
function LocationSpy() {
  lastPath = useLocation().pathname;
  return null;
}

/** Render the whole app at a given address. */
export function renderApp(path = "/") {
  const result = render(
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <AppRoutes />
      <LocationSpy />
    </MemoryRouter>,
  );
  return { ...result, currentPath: () => lastPath };
}
