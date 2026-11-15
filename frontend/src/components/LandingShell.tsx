import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ProvidenceMark } from "./Mark";

/**
 * Masthead and footer shared by the landing page and the guided walk-through.
 * The mark and the word are one lockup, and one link: see Mark.tsx.
 */
export function LandingShell({ children }: { children: ReactNode }) {
  return (
    <div className="landing">
      <a href="#main" className="skip-link">Skip to content</a>
      <header className="masthead">
        <div className="shell masthead-in">
          <Link to="/" className="wordmark" aria-label="Providence, home">
            <ProvidenceMark />
            <b>Providence</b>
          </Link>
          {/* Short in the masthead, but still "Open the dashboard" to a screen reader. */}
          <Link to="/dashboard/analyze" className="btn btn-plain">
            <span className="visually-hidden">Open the </span>Dashboard
          </Link>
        </div>
      </header>
      <main id="main" tabIndex={-1} style={{ outline: "none" }}>{children}</main>
      <footer className="landing-foot">
        <div className="shell landing-foot-in">
          <span>Providence · Synthetic Logic LAB · Junction 2026</span>
          <span className="foot-links">
            <a href="https://github.com/AaronG5/junction-2026">Source code</a>
            <a href="https://github.com/AaronG5/junction-2026/blob/main/LICENSE">Licence</a>
            <a href="https://github.com/AaronG5/junction-2026#readme">Run it yourself</a>
          </span>
        </div>
      </footer>
    </div>
  );
}
