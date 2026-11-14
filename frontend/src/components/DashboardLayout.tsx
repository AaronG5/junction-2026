import { Link, NavLink, Outlet } from "react-router-dom";
import { DATA_MODES, useSettings } from "../lib/settings";
import { ThemeSwitch } from "./ThemeSwitch";

const NAV = [
  { to: "/dashboard/narratives", label: "Narratives" },
  { to: "/dashboard/analyze", label: "Analyze a link" },
  { to: "/dashboard/claims", label: "Claims" },
  { to: "/dashboard/sources", label: "Sources" },
  { to: "/dashboard/system", label: "System" },
];

export function DashboardLayout() {
  const { dataMode } = useSettings();
  const mode = DATA_MODES[dataMode];
  return (
    <div className="layout">
      <aside className="sidebar">
        <Link to="/" className="brand">
          <b>Veritas Lens</b>
          <span>Disinformation analysis</span>
        </Link>
        <nav className="nav" aria-label="Main">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to}>{n.label}</NavLink>
          ))}
        </nav>
        <div style={{ marginTop: "auto", display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="side-box" data-testid="data-source">
            <span className="lbl">Data source</span>
            <span style={{ fontWeight: 600 }}>{mode.name}</span>
            <span className="muted" style={{ fontSize: 12.5 }}>{mode.short}</span>
            <Link to="/dashboard/system" style={{ fontSize: 12.5, fontWeight: 600 }}>Change</Link>
          </div>
          <ThemeSwitch />
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
