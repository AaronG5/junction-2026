import type { ReactNode } from "react";
import { ApiError, BackendUnavailable } from "../api/client";

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}…</span>
    </div>
  );
}

export function EmptyState({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="state state-empty">
      <span>{children}</span>
      {action}
    </div>
  );
}

/** Explains why data couldn't be shown, in plain words, with a way forward. */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const retry = onRetry && <button type="button" className="btn btn-plain" onClick={onRetry}>Try again</button>;

  if (error instanceof BackendUnavailable) {
    // Developers (npm run dev) get the demo-mode hint; everyone else gets launcher guidance.
    return import.meta.env.DEV ? (
      <div className="state state-error" role="alert">
        <strong>The Providence backend isn't connected yet.</strong>
        <span className="muted">
          This screen needs the backend to show real data. To explore with example data, start the dashboard in demo mode:
        </span>
        <code className="mono codeline">npm run dev:demo</code>
        {retry}
      </div>
    ) : (
      <div className="state state-error" role="alert">
        <strong>Providence can't reach its server right now.</strong>
        <span className="muted">
          If you run Providence on your own computer, start it with the launcher (launch.bat on Windows, ./launch.sh on
          Linux and macOS), then try again.
        </span>
        {retry}
      </div>
    );
  }
  if (error instanceof ApiError) {
    return (
      <div className="state state-error" role="alert">
        <strong>{error.message}</strong>
        {retry}
      </div>
    );
  }
  return (
    <div className="state state-error" role="alert">
      <strong>Something went wrong while loading this.</strong>
      <span className="muted">{error instanceof Error ? error.message : String(error)}</span>
      {retry}
    </div>
  );
}
