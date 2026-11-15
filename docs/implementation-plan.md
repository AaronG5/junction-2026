# Implementation plan: Providence frontend (and the backend after it)

Goal: turn the prototype (https://claude.ai/artifact/DMV2DxZRyEHyjfS2ZqcxHb) into a real React + Vite + TypeScript app in `frontend/`. Each phase has tests that must pass before the next phase starts.

**Ground rules**
- Every phase ends green: `npm run typecheck`, `npm test`, and `npm run build`.
- **Demo data only loads with the demo flag** (`npm run dev:demo` / `build:demo`). Without it, screens call the real API at `/api/...`. Until the backend exists, they show a clear "backend not connected" state.
- **The contract between frontend and backend is `frontend/src/api/types.ts`.** The demo data is written in exactly those types, so it doubles as the backend's test fixture.
- **Python always runs through uv.**

## Phase 0: launcher runs on uv
- `pyproject.toml` at the repo root (no dependencies yet). `launch.bat` and `launch.sh` run `uv run python -m local_ai`, and offer to install uv when it's missing.
- **Tests:**
  - `launch.bat --status` and `launch.sh --status` work through uv.
  - `uv run python -m local_ai --list-models` prints the model table.

## Phase 1: scaffold
- Vite + React + TS, palette tokens from `docs/design/tokens.css`, self-hosted IBM Plex fonts (no Google requests, so it works offline), router, dashboard layout with sidebar, and a light/dark/system theme switch.
- Routes:
  - `/` (landing)
  - `/dashboard/narratives` (dashboard home)
  - `/dashboard/analyze`, `/dashboard/claims`, `/dashboard/sources`, `/dashboard/sources/:id`, `/dashboard/system`
- **Tests (Vitest + Testing Library):**
  - Every route renders its heading.
  - Unknown routes show a not-found page.
  - The sidebar marks the current page (`aria-current`).
  - `/dashboard` redirects to narratives.
  - The theme switch sets `data-theme`.

## Phase 2: data layer
- `types.ts` (the contract), `client.ts` (real API or demo), `lib/filters.ts` (filter state, presets, date logic), `demo/` (example data, loaded only in demo mode).
- **Tests:**
  - **Filter logic:** each preset gives the right dates; custom ranges work; a reversed range is detected; platform and status matching work; "active between" means the item overlaps the range.
  - **Client errors:** a 404, an HTML response (no backend), a network error and invalid JSON all become `BackendUnavailable`. A successful JSON response passes through, and filters are sent as query parameters.
  - **Demo isolation:** the demo module isn't requested when the flag is off. A normal `npm run build` contains no demo data (the build output is checked for a demo-only string).

## Phase 3: shared components
- FilterBar (platform / date range / status), VerdictPill, StatusDot, LanguageBar, and the loading, empty, error and backend-not-connected states.
- **Tests:**
  - **FilterBar:** clicking a chip updates the filters and `aria-pressed`. Custom dates appear only when chosen, and a reversed range shows the error. Status counts render. "Clear filters" resets everything.
  - **VerdictPill:** always shows a label as well as the icon.

## Phase 4: Narratives (dashboard home)
- Filter bar, cluster map, cluster list, and the selected cluster's typical and edge posts.
- **Tests (demo data):**
  - The list renders and the first cluster is selected.
  - Clicking a cluster updates the details.
  - A filter that excludes everything shows the empty state.
  - The selection moves to the first visible cluster when the current one is filtered out.
  - Without the backend, "not connected" is shown.

## Phase 5: Analyze a link
- Link input with validation, progress steps, and the full post report (summary, post with original/translation toggle, spread chain, claims, similar posts, categories, Gemma's reading, cluster).
- **Tests:**
  - A link that isn't Telegram or Facebook shows an error and no request is made.
  - A valid link shows progress, then the report.
  - The translation toggle works.
  - A backend failure shows an error state.

## Phase 6: Claims, Sources, Channel report, System
- **Tests:**
  - **Claims:** search and filters combine, expanding a claim shows its evidence, and the counts are correct.
  - **Sources:** platform filter, and column sorting both ways.
  - **Channel:** the 30/90-day switch changes the number of bars, and recent posts link to Analyze.
  - **System:** choosing a data source updates the sidebar and its note; sharing can't be turned on in local-only mode.

## Phase 7: Landing page + guided walk-through
- **Tests:**
  - Link validation, and the trending filters with their empty state.
  - Picking a story starts at step 1. Next/Back and the step buttons move correctly.
  - Step 5 links to the dashboard and the full report.
  - "Open the dashboard" is present in the header, hero, banner and final step.

## Phase 8: finish
- Responsive check at phone width, keyboard focus, dark mode, a final build, and a manual run-through in a browser (demo and normal mode).

## Backend (after the frontend)
- FastAPI app run with `uv run`, reading MySQL (PyMySQL, MIT-licensed) and Qdrant, serving `/api/*` exactly as `types.ts` describes, and serving the built frontend.
- **Tests (pytest via `uv run pytest`):**
  - **Contract tests:** every endpoint's JSON is validated against schemas generated from the same types, using the demo data as fixtures.
  - **Filter tests:** the same cases as Phase 2, so frontend and backend agree on what "last 30 days" means.
  - **Integration tests:** against the Docker MySQL and Qdrant, using a throwaway database.
- The launcher then gains frontend build/serve steps and a `--demo-data` flag.
