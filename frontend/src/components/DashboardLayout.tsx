import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { DATA_MODES, useSettings } from "../lib/settings";
import { ThemeSwitch } from "./ThemeSwitch";

const NAV = [
  { to: "/dashboard/narratives", label: "Narratives" },
  { to: "/dashboard/analyze", label: "Analyze a link" },
  { to: "/dashboard/claims", label: "Claims" },
  { to: "/dashboard/sources", label: "Sources" },
  { to: "/dashboard/system", label: "System" },
];

/**
 * Dashboard frame. Desktop: a sidebar that stays in view.
 * Phone: a compact top bar; the menu opens with the Menu button.
 */
export function DashboardLayout() {
  const { dataMode } = useSettings();
  const mode = DATA_MODES[dataMode];
  const [menuOpen, setMenuOpen] = useState(false);
  const { pathname } = useLocation();

  useEffect(() => setMenuOpen(false), [pathname]); // close the phone menu after navigating

  return (
    <div className="layout">
      <a href="#main" className="skip-link">Skip to content</a>
      <aside className="sidebar" data-open={menuOpen}>
        <div className="sidebar-top">
          <Link to="/" className="brand">
            <b>Veritas Lens</b>
            <span>Disinformation analysis</span>
          </Link>
          <button type="button" className="btn btn-plain menu-btn" aria-expanded={menuOpen} aria-controls="side-menu"
            onClick={() => setMenuOpen(!menuOpen)}>
            {menuOpen ? "Close" : "Menu"}
          </button>
        </div>
        <div id="side-menu" className="side-menu">
          <nav className="nav" aria-label="Main">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to}>{n.label}</NavLink>
            ))}
          </nav>
          <div className="side-bottom">
            <div className="side-box" data-testid="data-source">
              <span className="lbl">Data source</span>
              <span style={{ fontWeight: 600 }}>{mode.name}</span>
              <span className="muted" style={{ fontSize: 12.5 }}>{mode.short}</span>
              <Link to="/dashboard/system" style={{ fontSize: 12.5, fontWeight: 600 }}>Change</Link>
            </div>
            <ThemeSwitch />
          </div>
        </div>
      </aside>
      <main className="main" id="main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
