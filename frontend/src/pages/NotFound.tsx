import { Link } from "react-router-dom";

export function NotFound() {
  return (
    <div className="page" style={{ padding: "64px 24px", maxWidth: 640 }}>
      <h1 className="page-title">Page not found</h1>
      <p className="muted">This address doesn't exist in Veritas Lens.</p>
      <p style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <Link to="/" className="btn btn-plain">Go to the start page</Link>
        <Link to="/dashboard/narratives" className="btn">Open the dashboard</Link>
      </p>
    </div>
  );
}
