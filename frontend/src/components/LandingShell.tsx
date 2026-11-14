import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/** Header and footer shared by the landing page and the guided walk-through. */
export function LandingShell({ children }: { children: ReactNode }) {
  return (
    <div className="landing">
      <a href="#main" className="skip-link">Skip to content</a>
      <header className="landing-head">
        <Link to="/" className="brand" style={{ padding: 0 }}>
          <b style={{ fontSize: 17 }}>Veritas Lens</b>
          <span>Disinformation analysis</span>
        </Link>
        <Link to="/dashboard/narratives" className="btn">Open the dashboard →</Link>
      </header>
      <main id="main" tabIndex={-1} style={{ outline: "none" }}>{children}</main>
      <footer className="landing-foot">
        <div>Veritas Lens · built at Junction 2026 by Synthetic Logic LAB · open source</div>
      </footer>
    </div>
  );
}
