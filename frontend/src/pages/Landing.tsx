import { Link } from "react-router-dom";

/** Landing page. Placeholder until Phase 7 (hero, trending stories, walk-through). */
export function Landing() {
  return (
    <div style={{ maxWidth: 1280, margin: "0 auto", padding: "56px 40px" }}>
      <h1 style={{ fontSize: 48, letterSpacing: "-0.02em" }}>Every post has a story behind it. See where it started.</h1>
      <p style={{ marginTop: 20 }}>
        <Link to="/dashboard/narratives" className="btn">Open the dashboard →</Link>
      </p>
    </div>
  );
}
